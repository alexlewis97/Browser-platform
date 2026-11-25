"""Optional wrapper around the workflow-use package (AGPL-3.0).

Official WorkflowDefinitionSchema is loaded from schema/views.py *without*
importing workflow_use/__init__.py, which pulls service.py and fails against
browser-use 1.0.8 (`SystemMessage` / `BrowserProfile` were removed).

Replay of a recorded workflow against the user's already-open Chrome is done
by our Playwright CDP executor, which understands the same step types and
selectorStrategies. Stock run_with_no_ai is not compatible with this stack.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import types
from pathlib import Path
from typing import Any

import yaml

from browser_platform.models.workflow import WorkflowDefinition
from browser_platform.workflow_inputs import ensure_definition_inputs


def workflow_use_package_importable() -> bool:
    """True only if `import workflow_use` succeeds (it does not with browser-use 1.0.8)."""

    try:
        # A previous schema-only load may have planted a namespace module.
        existing = sys.modules.get("workflow_use")
        if existing is not None and getattr(existing, "WorkflowDefinitionSchema", None):
            return True
        # Avoid poisoning later schema loads if this fails part-way.
        import workflow_use  # noqa: F401

        return True
    except Exception:
        return False


def workflow_use_available() -> bool:
    """True when the official schema class can be loaded (package does not need to import)."""

    return official_schema_class() is not None


def official_schema_class():
    """Load workflow_use.schema.views.WorkflowDefinitionSchema without package __init__."""

    cached = getattr(official_schema_class, "_cached", None)
    if cached is not None:
        return cached or None
    try:
        from importlib.metadata import distribution

        dist = distribution("workflow-use")
        views_file = None
        for file in dist.files or []:
            text = str(file).replace("\\", "/")
            if text.endswith("workflow_use/schema/views.py"):
                views_file = Path(dist.locate_file(file))
                break
        if views_file is None or not views_file.is_file():
            official_schema_class._cached = False
            return None

        pkg_root = views_file.parents[1]  # .../workflow_use
        if "workflow_use" not in sys.modules:
            pkg = types.ModuleType("workflow_use")
            pkg.__path__ = [str(pkg_root)]
            pkg.__package__ = "workflow_use"
            pkg.__file__ = str(pkg_root / "__init__.py")
            sys.modules["workflow_use"] = pkg
        elif getattr(sys.modules["workflow_use"], "__path__", None) is None:
            # A failed import may have left a dummy; make it a package.
            sys.modules["workflow_use"].__path__ = [str(pkg_root)]

        schema_pkg_name = "workflow_use.schema"
        if schema_pkg_name not in sys.modules:
            schema_pkg = types.ModuleType(schema_pkg_name)
            schema_pkg.__path__ = [str(views_file.parent)]
            schema_pkg.__package__ = schema_pkg_name
            sys.modules[schema_pkg_name] = schema_pkg

        views_name = "workflow_use.schema.views"
        if views_name in sys.modules and hasattr(sys.modules[views_name], "WorkflowDefinitionSchema"):
            cls = sys.modules[views_name].WorkflowDefinitionSchema
            official_schema_class._cached = cls
            return cls

        spec = importlib.util.spec_from_file_location(views_name, views_file)
        if spec is None or spec.loader is None:
            official_schema_class._cached = False
            return None
        module = importlib.util.module_from_spec(spec)
        sys.modules[views_name] = module
        spec.loader.exec_module(module)
        cls = module.WorkflowDefinitionSchema
        official_schema_class._cached = cls
        return cls
    except Exception:
        official_schema_class._cached = False
        return None


def official_schema_incompatibility() -> str:
    return (
        "workflow-use==0.2.11 imports browser_use.llm.SystemMessage and "
        "browser_use.browser.profile.BrowserProfile, both removed in browser-use 1.0.8. "
        "Official schema ingest works via schema/views.py; stock run_with_no_ai and the "
        "recorder service cannot run in-process alongside browser-use 1.0. Replay of the "
        "same schema is performed by this platform's Playwright CDP executor."
    )


def _read_document(path: str | Path) -> dict[str, Any]:
    p = Path(path)
    text = p.read_text(encoding="utf-8")
    data = yaml.safe_load(text) if p.suffix in {".yaml", ".yml"} else json.loads(text)
    if not isinstance(data, dict):
        raise ValueError(f"workflow document must be an object: {p}")
    return data


def ingest_payload(data: dict[str, Any]) -> WorkflowDefinition:
    """Validate a recorded or hand-written workflow against the official schema when possible."""

    payload = dict(data)
    # Recorder events wrap the definition.
    if "workflow" in payload and isinstance(payload["workflow"], dict):
        payload = payload["workflow"]
    if "definition" in payload and isinstance(payload["definition"], dict) and "steps" in payload["definition"]:
        payload = payload["definition"]
    payload.setdefault("input_schema", [])
    schema_cls = official_schema_class()
    if schema_cls is not None:
        official = schema_cls.model_validate(payload)
        official_dump = official.model_dump()
        # Keep platform extras from the original payload.
        merged = {**payload, **official_dump}
        return ensure_definition_inputs(WorkflowDefinition.model_validate(merged))
    return ensure_definition_inputs(WorkflowDefinition.model_validate(payload))


def load_definition(path: str) -> WorkflowDefinition:
    """Prefer official workflow-use schema validation; otherwise our schema."""

    return ingest_payload(_read_document(path))


async def try_run_with_no_ai(definition: WorkflowDefinition, variables: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """Stock run_with_no_ai is incompatible with browser-use 1.0.8 + user-Chrome CDP."""

    _ = (definition, variables)
    return None


def recorder_help() -> dict[str, str]:
    schema = official_schema_class()
    return {
        "package": "workflow-use",
        "license": "AGPL-3.0",
        "schema": "official" if schema is not None else "platform-fallback",
        "schema_version": "0.2.11",
        "record": "Build the workflow-use Chrome extension, then `python cli.py create-workflow`, or POST a recorded .workflow.json to /api/workflows.",
        "ingest": "POST /api/workflows with the recorded JSON (workflow-use schema). Official WorkflowDefinitionSchema is used when the package is installed.",
        "from_task": "POST /api/workflows/from-task with a natural-language task (and optional variables + llm). browser-use demonstrates it once on the user's Chrome; we save a workflow-use JSON document and immediately replay it with 0 tokens. Use wait:false and GET /api/workflows/from-task/{job_id} to poll.",
        "replay": "Playwright CDP executor (compatible path). Stock run_with_no_ai is not used: " + official_schema_incompatibility(),
    }
