#!/usr/bin/env python3
"""Probe OpenRouter with the apply_patch function schema. Never prints the key."""

from __future__ import annotations

import json
import os
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REPORT = ROOT / "data" / "flash_400_diagnosis.json"
MODEL = os.environ.get("FLASH_DIAG_MODEL") or "qwen/qwen3.7-flash"
# Keep this tiny.
MAX_TOKENS = 16

APPLY_PATCH_PARAMETERS = {
    "type": "object",
    "additionalProperties": True,
    "properties": {
        "type": {"type": "string", "enum": ["create_file", "update_file", "delete_file"]},
        "path": {"type": "string"},
        "diff": {"type": "string"},
    },
}

STRICT_PARAMETERS = {
    "type": "object",
    "additionalProperties": False,
    "required": ["type", "path"],
    "properties": {
        "type": {"type": "string", "enum": ["create_file", "update_file", "delete_file"]},
        "path": {"type": "string"},
        "diff": {"type": "string"},
    },
}


def _post(url: str, key: str, payload: dict) -> dict:
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://browser-platform.local",
            "X-Title": "browser-platform-flash-diag",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=45) as resp:
            body = json.loads(resp.read().decode("utf-8"))
            usage = body.get("usage") or {}
            return {
                "ok": True,
                "status": resp.status,
                "id": body.get("id"),
                "error": None,
                "usage": {
                    "prompt_tokens": usage.get("prompt_tokens") or usage.get("input_tokens"),
                    "completion_tokens": usage.get("completion_tokens") or usage.get("output_tokens"),
                    "cost": usage.get("cost") or usage.get("total_cost"),
                },
                "choice_finish": ((body.get("choices") or [{}])[0]).get("finish_reason")
                or (body.get("status")),
            }
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            parsed = json.loads(raw)
        except Exception:
            parsed = raw[:2000]
        return {"ok": False, "status": exc.code, "error": parsed, "usage": {}}
    except Exception as exc:
        return {"ok": False, "status": None, "error": str(exc), "usage": {}}


def _redact(obj, key: str):
    blob = json.dumps(obj, default=str)
    if key:
        blob = blob.replace(key, "[redacted]")
    return json.loads(blob)


def main() -> int:
    key = os.environ.get("OPENROUTER_API_KEY") or ""
    if not key:
        REPORT.parent.mkdir(parents=True, exist_ok=True)
        REPORT.write_text(json.dumps({"skipped": True, "reason": "no key"}) + "\n")
        print(json.dumps({"skipped": True}))
        return 1
    tools_chat = [
        {
            "type": "function",
            "function": {
                "name": "apply_patch",
                "description": "Apply a workspace file edit.",
                "parameters": APPLY_PATCH_PARAMETERS,
            },
        }
    ]
    tools_resp = [
        {
            "type": "function",
            "name": "apply_patch",
            "description": "Apply a workspace file edit.",
            "parameters": APPLY_PATCH_PARAMETERS,
            "strict": False,
        }
    ]
    tools_strict = [
        {
            "type": "function",
            "function": {
                "name": "apply_patch",
                "description": "Apply a workspace file edit.",
                "parameters": STRICT_PARAMETERS,
            },
        }
    ]
    message = {"role": "user", "content": "Say hi in one word. Do not call tools."}
    cases = {
        "chat_loose_schema": {
            "url": "https://openrouter.ai/api/v1/chat/completions",
            "payload": {
                "model": MODEL,
                "messages": [message],
                "tools": tools_chat,
                "max_tokens": MAX_TOKENS,
            },
        },
        "chat_strict_schema": {
            "url": "https://openrouter.ai/api/v1/chat/completions",
            "payload": {
                "model": MODEL,
                "messages": [message],
                "tools": tools_strict,
                "max_tokens": MAX_TOKENS,
            },
        },
        "chat_no_tools": {
            "url": "https://openrouter.ai/api/v1/chat/completions",
            "payload": {"model": MODEL, "messages": [message], "max_tokens": MAX_TOKENS},
        },
        "chat_parallel_false": {
            "url": "https://openrouter.ai/api/v1/chat/completions",
            "payload": {
                "model": MODEL,
                "messages": [message],
                "tools": tools_chat,
                "parallel_tool_calls": False,
                "max_tokens": MAX_TOKENS,
            },
        },
        "chat_reasoning_effort": {
            "url": "https://openrouter.ai/api/v1/chat/completions",
            "payload": {
                "model": MODEL,
                "messages": [message],
                "tools": tools_chat,
                "reasoning": {"effort": "low"},
                "max_tokens": MAX_TOKENS,
            },
        },
        "responses_function_tool": {
            "url": "https://openrouter.ai/api/v1/responses",
            "payload": {
                "model": MODEL,
                "input": "Say hi in one word. Do not call tools.",
                "tools": tools_resp,
                "max_output_tokens": MAX_TOKENS,
            },
        },
    }
    results = {}
    spend = 0.0
    for name, case in cases.items():
        result = _post(case["url"], key, case["payload"])
        results[name] = result
        cost = (result.get("usage") or {}).get("cost") or 0
        try:
            spend += float(cost or 0)
        except (TypeError, ValueError):
            pass
    report = _redact(
        {"model": MODEL, "spend_usd_observed": spend, "results": results},
        key,
    )
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({"report": str(REPORT), "model": MODEL, "results": {
        name: {"ok": r.get("ok"), "status": r.get("status"), "error": r.get("error"), "usage": r.get("usage")}
        for name, r in results.items()
    }}, default=str, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
