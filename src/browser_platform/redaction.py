"""Never persist or log secrets (OpenRouter keys, Bearer tokens, etc.)."""

from __future__ import annotations

import logging
import re
from typing import Any

_KEY_NAMES = frozenset({
    "api_key", "apikey", "api-key", "openrouter_api_key", "openai_api_key",
    "authorization", "password", "secret", "token", "access_token",
})

_SECRET_RE = re.compile(
    r"("
    r"sk-or-v1-[A-Za-z0-9_-]+|"
    r"sk-proj-[A-Za-z0-9_-]+|"
    r"sk-admin-[A-Za-z0-9_-]+|"
    r"sk-svcacct-[A-Za-z0-9_-]+|"
    r"sk-[A-Za-z0-9_-]{16,}|"
    r"Bearer\s+[A-Za-z0-9._\-]+|"
    r"(?:access_token|token)=([^\s&#]+)"
    r")",
    re.IGNORECASE,
)


def redact_text(value: str) -> str:
    def _sub(match: re.Match[str]) -> str:
        text = match.group(0)
        if "=" in text and text.lower().startswith(("access_token=", "token=")):
            key = text.split("=", 1)[0]
            return f"{key}=[REDACTED]"
        return "[REDACTED]"

    return _SECRET_RE.sub(_sub, value)


def redact(value: Any) -> Any:
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            if str(k).lower() in _KEY_NAMES:
                out[k] = "[REDACTED]"
            else:
                out[k] = redact(v)
        return out
    if isinstance(value, (list, tuple)):
        return type(value)(redact(v) for v in value)
    return value


class RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = redact_text(record.msg)
        if record.args:
            if isinstance(record.args, dict):
                record.args = redact(record.args)
            else:
                record.args = tuple(redact(a) for a in record.args)
        # Tracebacks / exc_text can carry secrets from str(exc).
        if getattr(record, "exc_text", None):
            record.exc_text = redact_text(record.exc_text)
        return True


_INSTALLED = False


def install_logging_redaction() -> None:
    global _INSTALLED
    if _INSTALLED:
        return
    filt = RedactingFilter()
    logging.getLogger().addFilter(filt)
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access", "browser_platform"):
        logging.getLogger(name).addFilter(filt)
    _INSTALLED = True
