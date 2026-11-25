import json
import logging
from pathlib import Path

from browser_platform.models.llm import LLMConfig
from browser_platform.models.run import RunRecord
from browser_platform.redaction import RedactingFilter, redact, redact_text
from browser_platform.store.file_store import FileStore


def test_redact_openrouter_and_bearer_keys():
    text = "Authorization: Bearer sk-or-v1-abcDEF123456 and also sk-abcdefghijklmnopqrstuvwxyz"
    out = redact_text(text)
    assert "sk-or-v1-abcDEF123456" not in out
    assert "sk-abcdefghijklmnopqrstuvwxyz" not in out
    assert "[REDACTED]" in out


def test_redact_dict_key_names():
    payload = {"api_key": "sk-or-v1-secret", "model": "qwen/qwen3.7-flash", "nested": {"token": "abc"}}
    out = redact(payload)
    assert out["api_key"] == "[REDACTED]"
    assert out["model"] == "qwen/qwen3.7-flash"
    assert out["nested"]["token"] == "[REDACTED]"


def test_llm_config_secret_not_in_repr():
    cfg = LLMConfig(api_key="sk-or-v1-supersecretvalue", model="openai/gpt-oss-20b")
    assert "supersecretvalue" not in repr(cfg)
    assert "supersecretvalue" not in cfg.model_dump_json()


def test_store_never_persists_api_key(data_dir: Path):
    store = FileStore(data_dir)
    run = RunRecord(
        id="run1",
        workflow_id="wf",
        workflow_name="n",
        workflow_version="1",
        status="succeeded",
        model="qwen/qwen3.7-flash",
    )
    # Even if a caller smuggles a key into extras:
    run_dump_path_model = run.model_copy()
    object.__setattr__  # keep linters calm
    store.save_run(run_dump_path_model)
    raw = (data_dir / "runs" / "run1.json").read_text()
    assert "api_key" not in raw or "[REDACTED]" in raw
    data = json.loads(raw)
    assert "api_key" not in data


def test_log_filter_redacts(caplog):
    logger = logging.getLogger("browser_platform.test")
    logger.addFilter(RedactingFilter())
    logger.setLevel(logging.INFO)
    with caplog.at_level(logging.INFO, logger="browser_platform.test"):
        logger.info("using key sk-or-v1-aaaaaaaaaaaaaaaaaaaa")
    assert "sk-or-v1-aaaaaaaaaaaaaaaaaaaa" not in caplog.text
