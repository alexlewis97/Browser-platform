"""Local reverse proxy in front of api.openai.com that records usage and enforces a spend cap.

The OpenAI key is forwarded in Authorization and is never written to logs or the ledger file.
"""

from __future__ import annotations

import json
import logging
import os
import socket
import ssl
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from browser_platform.pomerado.usage import (
    empty_ledger,
    extract_usage,
    model_from_payload,
    parse_sse_usage,
    record_usage,
)

logger = logging.getLogger("browser_platform.pomerado")

UPSTREAM_HOST = "api.openai.com"
UPSTREAM_PORT = 443


class BudgetExceeded(Exception):
    def __init__(self, cost: float, cap: float):
        super().__init__(f"OpenAI spend cap reached (${cost:.4f} >= ${cap})")
        self.cost = cost
        self.cap = cap


class UsageTracker:
    def __init__(self, *, cap_usd: float | None, ledger_path: Path | None = None):
        self.cap_usd = cap_usd
        self.ledger_path = ledger_path
        self.ledger = empty_ledger()
        self._lock = threading.Lock()
        if ledger_path and ledger_path.is_file():
            try:
                loaded = json.loads(ledger_path.read_text())
                if isinstance(loaded, dict):
                    self.ledger = {**empty_ledger(), **loaded}
            except Exception:
                pass

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return json.loads(json.dumps(self.ledger))

    def would_exceed(self) -> bool:
        if self.cap_usd is None:
            return False
        with self._lock:
            return float(self.ledger.get("cost_usd") or 0) >= self.cap_usd

    def record(self, *, model: str | None, input_tokens: int, output_tokens: int, cached_input_tokens: int = 0) -> float:
        with self._lock:
            cost = record_usage(
                self.ledger,
                model=model,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cached_input_tokens=cached_input_tokens,
            )
            if self.cap_usd is not None and float(self.ledger["cost_usd"]) >= self.cap_usd:
                self.ledger["capped"] = True
            if self.ledger_path is not None:
                self.ledger_path.parent.mkdir(parents=True, exist_ok=True)
                self.ledger_path.write_text(json.dumps(self.ledger, indent=2) + "\n")
            return cost


def _upstream(method: str, path: str, headers: list[tuple[str, str]], body: bytes, timeout: float = 600):
    context = ssl.create_default_context()
    conn = socket.create_connection((UPSTREAM_HOST, UPSTREAM_PORT), timeout=timeout)
    sock = context.wrap_socket(conn, server_hostname=UPSTREAM_HOST)
    sock.settimeout(timeout)
    # Rebuild a raw HTTP/1.1 request. Hop-by-hop headers are dropped by the caller.
    request = [f"{method} {path} HTTP/1.1", f"Host: {UPSTREAM_HOST}", "Connection: close"]
    for name, value in headers:
        request.append(f"{name}: {value}")
    request.append(f"Content-Length: {len(body)}")
    raw = ("\r\n".join(request) + "\r\n\r\n").encode("utf-8") + body
    sock.sendall(raw)
    chunks: list[bytes] = []
    while True:
        try:
            data = sock.recv(65536)
        except TimeoutError:
            break
        if not data:
            break
        chunks.append(data)
    sock.close()
    return b"".join(chunks)


def _split_http(raw: bytes) -> tuple[int, list[tuple[str, str]], bytes]:
    header_blob, sep, body = raw.partition(b"\r\n\r\n")
    if not sep:
        return 502, [("Content-Type", "text/plain")], b"upstream parse error"
    lines = header_blob.split(b"\r\n")
    status_line = lines[0].decode("iso-8859-1", "replace")
    parts = status_line.split(" ", 2)
    try:
        status = int(parts[1])
    except Exception:
        status = 502
    raw_headers: list[tuple[str, str]] = []
    for line in lines[1:]:
        if b":" not in line:
            continue
        name, value = line.split(b":", 1)
        raw_headers.append((name.decode("iso-8859-1").strip(), value.decode("iso-8859-1").strip()))
    chunked = any(n.lower() == "transfer-encoding" and "chunked" in v.lower() for n, v in raw_headers)
    if chunked:
        body = _dechunk(body)
    headers = [
        (n, v)
        for n, v in raw_headers
        if n.lower() not in {"transfer-encoding", "connection", "keep-alive", "proxy-connection"}
    ]
    headers = [(n, v) for n, v in headers if n.lower() != "content-length"]
    headers.append(("Content-Length", str(len(body))))
    return status, headers, body


def _dechunk(body: bytes) -> bytes:
    out = bytearray()
    rest = body
    while rest:
        line, _, rest = rest.partition(b"\r\n")
        try:
            size = int(line.split(b";", 1)[0].strip() or b"0", 16)
        except ValueError:
            return body
        if size == 0:
            break
        out.extend(rest[:size])
        rest = rest[size:]
        if rest.startswith(b"\r\n"):
            rest = rest[2:]
    return bytes(out)


def _record_from_body(tracker: UsageTracker, content_type: str, body: bytes) -> None:
    text = body.decode("utf-8", "replace")
    usage = None
    model = None
    if "text/event-stream" in content_type or text.lstrip().startswith("data:"):
        usage, model = parse_sse_usage(text)
    else:
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            return
        usage = extract_usage(payload)
        model = model_from_payload(payload)
    if usage:
        tracker.record(
            model=model,
            input_tokens=usage["input_tokens"],
            output_tokens=usage["output_tokens"],
            cached_input_tokens=usage["cached_input_tokens"],
        )


def make_handler(tracker: UsageTracker):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt: str, *args: Any) -> None:
            # Never print headers or the request line if it could carry a query key.
            logger.info("openai-usage-proxy %s %s", self.command, self.path.split("?", 1)[0])

        def _forward(self) -> None:
            if tracker.would_exceed():
                body = json.dumps({"error": {"message": "OpenAI spend cap reached", "type": "budget_exceeded"}}).encode()
                self.send_response(429)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length else b""
            forwarded: list[tuple[str, str]] = []
            for name, value in self.headers.items():
                if name.lower() in {"host", "content-length", "connection", "transfer-encoding", "proxy-connection"}:
                    continue
                forwarded.append((name, value))
            path = self.path if self.path.startswith("/") else "/" + self.path
            try:
                raw = _upstream(self.command, path, forwarded, body)
            except Exception as exc:
                # Do not include exception text that might echo headers.
                msg = json.dumps({"error": {"message": f"usage proxy upstream failed ({type(exc).__name__})"}}).encode()
                self.send_response(502)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(msg)))
                self.end_headers()
                self.wfile.write(msg)
                return
            status, headers, resp_body = _split_http(raw)
            content_type = next((v for n, v in headers if n.lower() == "content-type"), "")
            try:
                _record_from_body(tracker, content_type, resp_body)
            except Exception:
                logger.exception("openai-usage-proxy failed to record usage")
            if "Content-Length" not in {n for n, _ in headers}:
                headers.append(("Content-Length", str(len(resp_body))))
            self.send_response(status)
            for name, value in headers:
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(resp_body)

        def do_GET(self):  # noqa: N802
            self._forward()

        def do_POST(self):  # noqa: N802
            self._forward()

        def do_PUT(self):  # noqa: N802
            self._forward()

        def do_DELETE(self):  # noqa: N802
            self._forward()

        def do_HEAD(self):  # noqa: N802
            self._forward()

        def do_OPTIONS(self):  # noqa: N802
            self._forward()

    return Handler


class UsageProxy:
    def __init__(self, tracker: UsageTracker, server: ThreadingHTTPServer, thread: threading.Thread):
        self.tracker = tracker
        self.server = server
        self.thread = thread

    @property
    def base_url(self) -> str:
        host, port = self.server.server_address
        return f"http://127.0.0.1:{port}/v1"

    def snapshot(self) -> dict[str, Any]:
        return self.tracker.snapshot()

    def close(self) -> None:
        try:
            self.server.shutdown()
        except Exception:
            pass


def start_usage_proxy(*, cap_usd: float | None, ledger_path: Path | None) -> UsageProxy:
    tracker = UsageTracker(cap_usd=cap_usd, ledger_path=ledger_path)
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(tracker))
    thread = threading.Thread(target=server.serve_forever, name="openai-usage-proxy", daemon=True)
    thread.start()
    return UsageProxy(tracker, server, thread)


def spend_cap_from_env() -> float | None:
    raw = os.environ.get("POMERADO_OPENAI_SPEND_CAP")
    if raw is None or raw.strip() == "":
        return None
    try:
        value = float(raw)
    except ValueError:
        return None
    return value if value >= 0 else None
