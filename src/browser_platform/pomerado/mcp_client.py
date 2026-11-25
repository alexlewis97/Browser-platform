"""Minimal MCP stdio client (Content-Length framing + NDJSON fallback)."""

from __future__ import annotations

import asyncio
import json
import os
from typing import Any


class McpError(RuntimeError):
    def __init__(self, message: str, data: Any = None):
        super().__init__(message)
        self.data = data


class McpClient:
    def __init__(self, command: list[str], env: dict[str, str] | None = None, cwd: str | None = None):
        self.command = command
        self.env = env
        self.cwd = cwd
        self._proc: asyncio.subprocess.Process | None = None
        self._id = 0
        self._pending: dict[int, asyncio.Future] = {}
        self._reader_task: asyncio.Task | None = None
        self._stderr_task: asyncio.Task | None = None
        self._stderr_file = None
        self._notification_listeners: list = []
        self._stderr_listeners: list = []

    async def start(self) -> None:
        log_path = os.environ.get("POMERADO_MCP_STDERR") or "/tmp/browser-platform-logs/pomerado-mcp.stderr.log"
        os.makedirs(os.path.dirname(log_path), exist_ok=True)
        self._stderr_file = open(log_path, "ab")
        self._proc = await asyncio.create_subprocess_exec(
            *self.command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=self.env,
            cwd=self.cwd,
        )
        self._reader_task = asyncio.create_task(self._read_loop())
        self._stderr_task = asyncio.create_task(self._stderr_loop())
        await self.request(
            "initialize",
            {
                "protocolVersion": "2025-11-25",
                "capabilities": {},
                "clientInfo": {"name": "browser-platform", "version": "0.1.0"},
            },
        )
        await self.notify("notifications/initialized", {})

    async def close(self) -> None:
        if self._proc and self._proc.stdin:
            try:
                self._proc.stdin.close()
            except Exception:
                pass
        if self._reader_task:
            self._reader_task.cancel()
        if self._stderr_task:
            self._stderr_task.cancel()
        if self._stderr_file:
            try:
                self._stderr_file.close()
            except Exception:
                pass
            self._stderr_file = None
        if self._proc and self._proc.returncode is None:
            self._proc.terminate()
            try:
                await asyncio.wait_for(self._proc.wait(), timeout=3)
            except Exception:
                self._proc.kill()
        self._proc = None

    async def request(self, method: str, params: dict[str, Any] | None = None, timeout: float = 60) -> Any:
        if not self._proc or not self._proc.stdin:
            raise McpError("MCP process is not running")
        self._id += 1
        msg_id = self._id
        future: asyncio.Future = asyncio.get_event_loop().create_future()
        self._pending[msg_id] = future
        await self._send({"jsonrpc": "2.0", "id": msg_id, "method": method, "params": params or {}})
        try:
            return await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError as exc:
            raise McpError(f"MCP request {method} timed out after {timeout}s") from exc

    async def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        await self._send({"jsonrpc": "2.0", "method": method, "params": params or {}})

    def add_notification_listener(self, callback) -> None:
        self._notification_listeners.append(callback)

    def add_stderr_listener(self, callback) -> None:
        self._stderr_listeners.append(callback)

    async def _stderr_loop(self) -> None:
        assert self._proc and self._proc.stderr
        try:
            while True:
                line = await self._proc.stderr.readline()
                if not line:
                    break
                if self._stderr_file:
                    try:
                        self._stderr_file.write(line)
                        self._stderr_file.flush()
                    except Exception:
                        pass
                text = line.decode("utf-8", "replace")
                for callback in list(self._stderr_listeners):
                    try:
                        callback(text)
                    except Exception:
                        continue
        except asyncio.CancelledError:
            return
        except Exception:
            return

    async def call_tool(self, name: str, arguments: dict[str, Any] | None = None, timeout: float = 120) -> Any:
        result = await self.request("tools/call", {"name": name, "arguments": arguments or {}}, timeout=timeout)
        if isinstance(result, dict) and result.get("isError"):
            raise McpError(f"tool {name} failed", data=result)
        # MCP tools often return {content:[{type:text,text:...}], structuredContent:...}
        if isinstance(result, dict) and "structuredContent" in result:
            return result["structuredContent"]
        if isinstance(result, dict) and result.get("content"):
            texts = [c.get("text") for c in result["content"] if isinstance(c, dict) and c.get("text")]
            if len(texts) == 1:
                try:
                    return json.loads(texts[0])
                except Exception:
                    return texts[0]
            return texts
        return result

    async def _send(self, message: dict[str, Any]) -> None:
        # Official Pomerado MCP (@modelcontextprotocol/server 2.x) uses NDJSON on stdio.
        raw = (json.dumps(message) + "\n").encode("utf-8")
        assert self._proc and self._proc.stdin
        self._proc.stdin.write(raw)
        await self._proc.stdin.drain()

    async def _read_loop(self) -> None:
        assert self._proc and self._proc.stdout
        reader = self._proc.stdout
        buf = b""
        try:
            while True:
                chunk = await reader.read(4096)
                if not chunk:
                    break
                buf += chunk
                while True:
                    message, buf = _try_extract(buf)
                    if message is None:
                        break
                    self._dispatch(message)
        except asyncio.CancelledError:
            return
        except Exception:
            return

    def _dispatch(self, message: dict[str, Any]) -> None:
        msg_id = message.get("id")
        if msg_id in self._pending:
            fut = self._pending.pop(msg_id)
            if "error" in message:
                err = message["error"]
                fut.set_exception(McpError(str(err), data=err))
            else:
                fut.set_result(message.get("result"))
            return
        if message.get("method"):
            for callback in list(self._notification_listeners):
                try:
                    callback(message)
                except Exception:
                    continue


def _try_extract(buf: bytes) -> tuple[dict[str, Any] | None, bytes]:
    if b"Content-Length:" in buf[:40] or buf.lower().startswith(b"content-length:"):
        sep = b"\r\n\r\n" if b"\r\n\r\n" in buf else b"\n\n"
        if sep not in buf:
            return None, buf
        header, rest = buf.split(sep, 1)
        length = None
        for line in header.split(b"\n"):
            if line.lower().startswith(b"content-length:"):
                length = int(line.split(b":", 1)[1].strip())
        if length is None or len(rest) < length:
            return None, buf
        raw, leftover = rest[:length], rest[length:]
        return json.loads(raw.decode("utf-8")), leftover
    # NDJSON fallback
    if b"\n" not in buf:
        return None, buf
    line, leftover = buf.split(b"\n", 1)
    line = line.strip()
    if not line:
        return None, leftover
    return json.loads(line.decode("utf-8")), leftover
