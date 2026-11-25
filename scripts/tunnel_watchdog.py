#!/usr/bin/env python3
"""Keep a Cloudflare quick tunnel to the local backend alive and publish its URL.

Quick tunnels have no uptime guarantee (no Cloudflare account on this box, so a
named tunnel is not available). This watchdog probes the public URL every ~30s,
restarts cloudflared on failure, updates /etc/hosts, and publishes
/backend.json to the Vercel phone app so Alex never has to paste a new URL.

Does not print secrets. Stdlib only.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOG_DIR = Path(os.environ.get("BP_TUNNEL_LOG_DIR") or "/tmp/browser-platform-logs")
TUNNEL_LOG = LOG_DIR / "tunnel.log"
WATCHDOG_LOG = LOG_DIR / "watchdog.log"
URL_FILE = LOG_DIR / "tunnel_url.txt"
PID_FILE = LOG_DIR / "tunnel.pid"
WATCHDOG_PID = LOG_DIR / "watchdog.pid"
DISCOVERY_LOG = LOG_DIR / "backend.json"
PUBLIC_FILE = ROOT / "web" / "public" / "backend.json"
SITE_FILE = ROOT / "web" / "site" / "backend.json"

DEFAULT_BACKEND = os.environ.get("BP_BACKEND_ORIGIN") or "http://127.0.0.1:8745"
HOSTS_IP = os.environ.get("BP_TUNNEL_HOSTS_IP") or "104.16.230.132"
PROBE_PATH = "/api/recorder"
INTERVAL_S = float(os.environ.get("BP_TUNNEL_INTERVAL_S") or 30)
CLOUDFLARED = os.environ.get("BP_CLOUDFLARED") or shutil.which("cloudflared") or "/home/box/.local/bin/cloudflared"
VERCEL = os.environ.get("BP_VERCEL") or (
    str(Path.home() / ".local" / "bin" / "vercel") if (Path.home() / ".local" / "bin" / "vercel").exists() else shutil.which("vercel") or "vercel"
)
VERCEL_SCOPE = os.environ.get("BP_VERCEL_SCOPE") or "alexs-projects-67685b1a"
# Cloudflare's own precheck on this box preferred http2 (QUIC UDP works, but the
# receive buffer is too small). Named tunnels need a login we don't have.
# localhost.run / similar are also ephemeral and would still need discovery.
# Prefer QUIC (it re-established cleanly on this box). Fall back to http2 if a
# start does not become probeable. Cloudflare account-less quick tunnels only.
PROTOCOL = os.environ.get("BP_TUNNEL_PROTOCOL") or "quic"
FALLBACK_PROTOCOL = os.environ.get("BP_TUNNEL_FALLBACK_PROTOCOL") or "http2"

URL_RE = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com", re.I)

# When set (or falling back to the Oracle VM URL), never publish a tunnel over it.
ORACLE_BACKEND_URL = "https://browser.129-159-156-75.sslip.io"

DEATH_MARKERS = (
    "Tunnel not found",
    "Lost connection",
    "Unauthorized: Tunnel not found",
)
# Hostnames we always keep on the 104.16.230.132 line.
HOSTS_KEEP = ("trycloudflare.com", "api.trycloudflare.com")


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def log(message: str) -> None:
    line = f"{_now()} {message}"
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    with WATCHDOG_LOG.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")
    print(line, flush=True)


def read_text(path: Path) -> str:
    if not path.exists():
        return ""
    raw = path.read_bytes().replace(b"\x00", b"\n")
    return raw.decode("utf-8", "replace")


def current_url() -> str:
    text = URL_FILE.read_text(encoding="utf-8").strip() if URL_FILE.exists() else ""
    if URL_RE.fullmatch(text or ""):
        return text.rstrip("/")
    return ""


def write_url(url: str) -> None:
    url = url.rstrip("/")
    URL_FILE.write_text(url + "\n", encoding="utf-8")


def extract_urls(text: str) -> list[str]:
    seen: list[str] = []
    for match in URL_RE.findall(text or ""):
        url = match.rstrip("/")
        if url not in seen:
            seen.append(url)
    return seen


def latest_url_in_log(after: int = 0) -> str:
    text = read_text(TUNNEL_LOG)
    if after:
        # after is a byte offset; decode approximately from that point
        raw = TUNNEL_LOG.read_bytes() if TUNNEL_LOG.exists() else b""
        text = raw[after:].replace(b"\x00", b"\n").decode("utf-8", "replace")
    urls = extract_urls(text)
    return urls[-1] if urls else ""


def log_offset() -> int:
    try:
        return TUNNEL_LOG.stat().st_size
    except FileNotFoundError:
        return 0


def new_death_markers(after: int) -> list[str]:
    raw = TUNNEL_LOG.read_bytes() if TUNNEL_LOG.exists() else b""
    chunk = raw[after:].replace(b"\x00", b"\n").decode("utf-8", "replace")
    hits = [marker for marker in DEATH_MARKERS if marker.lower() in chunk.lower()]
    return hits


def hostname_of(url: str) -> str:
    return re.sub(r"^https?://", "", url, flags=re.I).split("/")[0].strip().lower()


def update_hosts_contents(hosts_text: str, hostname: str, ip: str = HOSTS_IP) -> str:
    """Append hostname to the existing IPv4 line, preserving other names."""

    hostname = hostname.strip().lower()
    if not hostname:
        return hosts_text
    lines = hosts_text.splitlines()
    if hosts_text.endswith("\n") or not lines:
        trailing = "\n" if hosts_text.endswith("\n") or not hosts_text else ""
    else:
        trailing = "\n"
    updated = False
    out: list[str] = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("#") or not stripped:
            out.append(line)
            continue
        parts = stripped.split()
        if not parts or parts[0] != ip:
            out.append(line)
            continue
        names = parts[1:]
        if hostname not in names:
            names.append(hostname)
        # Keep the line from growing without bound: static names + last few tunnels + new one.
        static = [n for n in names if n in HOSTS_KEEP]
        tunnels = [n for n in names if n not in HOSTS_KEEP]
        # Preserve order, drop oldest extras beyond 8 tunnel names.
        deduped: list[str] = []
        for name in tunnels:
            if name not in deduped:
                deduped.append(name)
        kept_tunnels = deduped[-8:]
        merged: list[str] = []
        for name in static + kept_tunnels:
            if name not in merged:
                merged.append(name)
        out.append(" ".join([ip, *merged]))
        updated = True
    if not updated:
        out.append(f"{ip} {' '.join(HOSTS_KEEP)} {hostname}")
    result = "\n".join(out)
    if trailing and not result.endswith("\n"):
        result += "\n"
    return result


def apply_hosts(hostname: str) -> bool:
    if os.environ.get("BP_SKIP_HOSTS") == "1":
        return False
    hosts_path = Path("/etc/hosts")
    try:
        original = hosts_path.read_text(encoding="utf-8")
    except OSError as exc:
        log(f"could not read /etc/hosts: {exc}")
        return False
    updated = update_hosts_contents(original, hostname)
    if updated == original:
        return False
    tmp = Path("/tmp/browser-platform-hosts.new")
    tmp.write_text(updated, encoding="utf-8")
    try:
        subprocess.run(["sudo", "-n", "cp", str(tmp), str(hosts_path)], check=True, capture_output=True, text=True)
        log(f"updated /etc/hosts with {hostname}")
        return True
    except subprocess.CalledProcessError as exc:
        log(f"failed to update /etc/hosts (sudo): {exc.stderr or exc.stdout or exc}")
        return False
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass


def discovery_payload(url: str) -> dict:
    return {"backendUrl": url.rstrip("/"), "updatedAt": _now()}


def write_discovery(url: str) -> None:
    payload = json.dumps(discovery_payload(url), indent=2) + "\n"
    for path in (DISCOVERY_LOG, PUBLIC_FILE, SITE_FILE):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(payload, encoding="utf-8")


def already_published(url: str) -> bool:
    try:
        data = json.loads(DISCOVERY_LOG.read_text(encoding="utf-8"))
        return str(data.get("backendUrl") or "").rstrip("/") == url.rstrip("/")
    except Exception:
        return False


def publish_via_api(url: str) -> bool:
    """Push backend.json through the site API so we do not need a full deploy."""
    origin = (os.environ.get("BP_MARKET_ORIGIN") or "https://browser-platform-web.vercel.app").rstrip("/")
    token = (
        os.environ.get("BROWSER_PLATFORM_USER_TOKEN")
        or os.environ.get("BROWSER_PLATFORM_ACCESS_TOKEN")
        or ""
    ).strip()
    if not token:
        log("api publish skipped: no access token in env")
        return False
    payload = json.dumps(discovery_payload(url)).encode("utf-8")
    request = urllib.request.Request(
        origin + "/api/backend",
        data=payload,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "X-Browser-Platform-Token": token,
            "User-Agent": "browser-platform-tunnel-watchdog",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            status = int(response.status)
            body = response.read(400).decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        status = int(exc.code)
        try:
            body = exc.read(200).decode("utf-8", "replace")
        except Exception:
            body = str(exc)
        log(f"api publish failed http={status} body={body[:160]}")
        return False
    except Exception as exc:
        log(f"api publish failed: {type(exc).__name__}")
        return False
    if status not in {200, 201}:
        log(f"api publish failed http={status} body={body[:160]}")
        return False
    log(f"published backend.json via API backendUrl={url}")
    return True


def pinned_backend_url() -> str:
    return (os.environ.get("BP_PINNED_BACKEND_URL") or ORACLE_BACKEND_URL or "").strip().rstrip("/")


def publish_discovery(url: str) -> bool:
    pinned = pinned_backend_url()
    proposed = (url or "").strip().rstrip("/")
    if pinned and proposed and proposed != pinned:
        log(f"skip tunnel publish; backend is pinned to {pinned}")
        return True
    write_discovery(url)
    if publish_via_api(url):
        return True
    log("api publish unavailable; falling back to Vercel deploy")
    return publish_vercel(url)


def publish_vercel(url: str) -> bool:
    if os.environ.get("BP_SKIP_VERCEL") == "1":
        log("skip Vercel publish (BP_SKIP_VERCEL=1)")
        return False
    write_discovery(url)
    web = ROOT / "web"
    cmd = [
        VERCEL,
        "deploy",
        "--prod",
        "--non-interactive",
        "--yes",
        f"--scope={VERCEL_SCOPE}",
    ]
    env = {k: v for k, v in os.environ.items() if "KEY" not in k.upper() or k in {"PATH", "HOME"}}
    # Keep PATH/HOME; drop obvious secrets from the child *log*, but vercel needs its login files
    # (already on disk). Do not pass or print API tokens.
    child_env = os.environ.copy()
    try:
        result = subprocess.run(
            cmd,
            cwd=str(web),
            env=child_env,
            capture_output=True,
            text=True,
            timeout=300,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
        log(f"vercel deploy failed: {exc}")
        return False
    # Redact anything that looks like a token just in case.
    combined = (result.stdout or "") + "\n" + (result.stderr or "")
    combined = re.sub(r"\b(sk-|vercel_|eyJ)[A-Za-z0-9._\-]{8,}", "[redacted]", combined)
    if result.returncode != 0:
        log(f"vercel deploy exited {result.returncode}: {combined[-1500:]}")
        return False
    # Last https URL in output is usually the deployment.
    urls = re.findall(r"https://[a-zA-Z0-9.-]+\.vercel\.app", combined)
    log(f"published backend.json backendUrl={url} vercel={urls[-1] if urls else 'ok'}")
    return True


def _probe_once(url: str, timeout: float) -> tuple[bool, str]:
    target = url.rstrip("/") + PROBE_PATH
    request = urllib.request.Request(target, method="GET", headers={"User-Agent": "browser-platform-tunnel-watchdog"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status = int(response.status)
            body = response.read(400).decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        status = int(exc.code)
        try:
            body = exc.read(400).decode("utf-8", "replace")
        except Exception:
            body = str(exc)
    lowered = body.lower()
    if "tunnel not found" in lowered or "lost connection" in lowered:
        return False, f"http={status} body_indicates_dead_tunnel"
    if status in {200, 401}:
        return True, f"http={status}"
    return False, f"http={status}"


def probe(url: str, timeout: float = 15.0) -> tuple[bool, str]:
    if not url:
        return False, "no url"
    # urllib timeouts can be ignored by a stuck TLS/proxy handshake on this box
    # (we have seen a 43-minute hang). Bound the call with a hard deadline.
    deadline = timeout + 3.0
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="probe") as pool:
            future = pool.submit(_probe_once, url, timeout)
            return future.result(timeout=deadline)
    except concurrent.futures.TimeoutError:
        return False, "error:Timeout"
    except Exception as exc:
        return False, f"error:{type(exc).__name__}"


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
        rparen = stat.rfind(")")
        state = stat[rparen + 2 : rparen + 3] if rparen != -1 else ""
        if state in {"Z", "X"}:
            return False
        return True
    except OSError:
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False


def reap_children() -> None:
    try:
        while True:
            pid, _status = os.waitpid(-1, os.WNOHANG)
            if pid == 0:
                break
            log(f"reaped child pid={pid}")
    except ChildProcessError:
        pass


def read_pid() -> int:
    try:
        return int(PID_FILE.read_text(encoding="utf-8").strip())
    except (FileNotFoundError, ValueError):
        return 0


def find_existing_tunnel_pid() -> int:
    stored = read_pid()
    if stored and pid_alive(stored):
        return stored
    try:
        out = subprocess.check_output(["pgrep", "-af", "cloudflared"], text=True, stderr=subprocess.DEVNULL)
    except subprocess.CalledProcessError:
        return 0
    for line in out.splitlines():
        if "--url" in line and "8745" in line and "tunnel" in line:
            try:
                return int(line.split(None, 1)[0])
            except ValueError:
                continue
    return 0


def stop_pid(pid: int) -> None:
    if not pid:
        return
    log(f"stopping cloudflared pid={pid}")
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        pass
    for _ in range(20):
        if not pid_alive(pid):
            break
        time.sleep(0.25)
    if pid_alive(pid):
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass
    reap_children()
    stored = read_pid()
    if stored == pid:
        try:
            PID_FILE.unlink()
        except OSError:
            pass


def stop_tunnel() -> None:
    stop_pid(find_existing_tunnel_pid())


def start_tunnel(protocol: str) -> int:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    # Start a new log segment so we can pick the URL created *this* start.
    handle = TUNNEL_LOG.open("ab")
    cmd = [
        CLOUDFLARED,
        "tunnel",
        "--url",
        DEFAULT_BACKEND,
        "--no-autoupdate",
        "--protocol",
        protocol,
    ]
    proc = subprocess.Popen(
        cmd,
        stdout=handle,
        stderr=handle,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
    )
    handle.close()
    PID_FILE.write_text(str(proc.pid) + "\n", encoding="utf-8")
    log(f"started cloudflared pid={proc.pid} protocol={protocol}")
    return proc.pid


def wait_for_url(offset: int, timeout: float = 45.0) -> str:
    deadline = time.time() + timeout
    while time.time() < deadline:
        url = latest_url_in_log(after=offset)
        if url:
            return url
        time.sleep(0.4)
    return latest_url_in_log(after=offset)


def restart_tunnel(reason: str) -> str:
    log(f"restarting tunnel ({reason})")
    old_pid = find_existing_tunnel_pid()
    # Start a replacement first so the published URL is not left with no process
    # if the new probe is slow. Only stop the old tunnel after the new one is ready,
    # or if we have to give up on this protocol's process.
    for protocol in (PROTOCOL, FALLBACK_PROTOCOL):
        if not protocol:
            continue
        offset = log_offset()
        pid = start_tunnel(protocol)
        url = wait_for_url(offset)
        if url and pid_alive(pid):
            apply_hosts(hostname_of(url))
            ok, detail = False, "not probed"
            for attempt in range(4):
                time.sleep(2)
                log(f"probe attempt {attempt + 1}/4 {url}")
                ok, detail = probe(url)
                if ok:
                    break
                log(f"probe attempt {attempt + 1}/4 {url} {detail}")
            if ok:
                write_url(url)
                log(f"tunnel ready {url} ({detail})")
                if old_pid and old_pid != pid:
                    stop_pid(old_pid)
                return url
            log(f"tunnel started but probe failed {url} {detail}; trying next protocol")
        else:
            log(f"no URL advertised for protocol={protocol}")
        if pid and pid != old_pid:
            stop_pid(pid)
        time.sleep(1)
    log("failed to establish a quick tunnel")
    return ""


def already_running() -> bool:
    try:
        pid = int(WATCHDOG_PID.read_text(encoding="utf-8").strip())
    except (FileNotFoundError, ValueError):
        return False
    if pid == os.getpid():
        return False
    return pid_alive(pid)


def write_watchdog_pid() -> None:
    WATCHDOG_PID.write_text(str(os.getpid()) + "\n", encoding="utf-8")


def loop(once: bool = False) -> int:
    if already_running() and not once:
        log(f"another watchdog is running ({WATCHDOG_PID.read_text().strip()}); exiting")
        return 0
    write_watchdog_pid()
    log("watchdog starting")
    last_published = ""
    last_log_scan = log_offset()
    # Adopt or repair the current tunnel before the periodic loop.
    pid = find_existing_tunnel_pid()
    candidates = []
    for candidate in (latest_url_in_log(), current_url()):
        if candidate and candidate not in candidates:
            candidates.append(candidate)
    url = ""
    for candidate in candidates:
        apply_hosts(hostname_of(candidate))
        ok, detail = probe(candidate)
        log(f"initial probe {candidate} {detail}")
        if ok:
            url = candidate
            write_url(url)
            break
    if not url:
        if pid or candidates:
            url = restart_tunnel("initial probe failed for advertised URL(s)" if candidates else "running cloudflared but no advertised URL")
        else:
            url = restart_tunnel("no cloudflared process")
    if url and url != last_published and not already_published(url):
        publish_discovery(url)
        last_published = url
    elif url:
        last_published = url
        log(f"discovery already has {url}; skip republish")
    if once:
        return 0 if url else 2

    failures = 0
    while True:
        time.sleep(INTERVAL_S)
        reap_children()
        pid = find_existing_tunnel_pid()
        markers = new_death_markers(last_log_scan)
        last_log_scan = log_offset()
        ok, detail = probe(current_url())
        if pid and ok and not markers:
            failures = 0
            log(f"ok {current_url()} {detail}")
            continue
        reason_bits = []
        if not pid:
            reason_bits.append("process dead")
        if not ok:
            reason_bits.append(f"probe {detail}")
        if markers:
            reason_bits.append("log:" + ",".join(markers))
        failures += 1
        # One immediate re-probe to ride out a short blip.
        time.sleep(3)
        if pid_alive(pid) and probe(current_url())[0] and not markers:
            log("probe recovered after brief blip")
            failures = 0
            continue
        url = restart_tunnel("; ".join(reason_bits) or "unknown")
        if url:
            if url != last_published and not already_published(url):
                publish_discovery(url)
                last_published = url
            else:
                last_published = url
            failures = 0
        else:
            backoff = min(60, 15 * failures)
            log(f"restart failed; retry in {backoff}s")
            time.sleep(backoff)


def main() -> int:
    parser = argparse.ArgumentParser(description="Watch and republish the Browser Platform Cloudflare quick tunnel")
    parser.add_argument("--once", action="store_true", help="Adopt/repair once, publish if needed, then exit")
    parser.add_argument("--publish-only", metavar="URL", help="Write discovery + publish this URL, then exit")
    args = parser.parse_args()
    if args.publish_only:
        url = args.publish_only.rstrip("/")
        write_url(url)
        write_discovery(url)
        apply_hosts(hostname_of(url))
        ok = publish_discovery(url)
        return 0 if ok else 2
    return loop(once=args.once)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        log("watchdog interrupted")
        raise SystemExit(0)
