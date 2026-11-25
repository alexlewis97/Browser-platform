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
    r"(sk-or-v1-[A-Za-z0-9_-]+|sk-[A-Za-z0-9]{16,}|Bearer\s+[A-Za-z0-9._\-]+)",
    re.IGNORECASE,
)


def redact_text(value: str) -> str:
    return _SECRET_RE.sub("[REDACTED]", value)


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
        return True


def install_logging_redaction() -> None:
    filt = RedactingFilter()
    logging.getLogger().addFilter(filt)
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access", "browser_platform"):
        logging.getLogger(name).addFilter(filt)
