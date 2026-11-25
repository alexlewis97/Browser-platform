"""Register a published Pomerado integration as a platform workflow."""

from __future__ import annotations

import re
from typing import Any

from browser_platform.models.workflow import WorkflowDefinition, WorkflowInput, WorkflowStep
from browser_platform.workflow_inputs import inputs_from_integration

_SLUG_RE = re.compile(r"[^a-z0-9_]+")


def integration_slug(name: str) -> str:
    slug = _SLUG_RE.sub("_", (name or "").strip().lower()).strip("_")
    if not slug or not slug[0].isalpha():
        slug = f"tool_{slug}" if slug else "tool"
    return slug[:80]


def already_registered(store, directory: str) -> Any | None:
    target = str(directory)
    for stored in store.list_workflows():
        artifact = getattr(stored.definition, "pomerado_artifact", None)
        if artifact and str(artifact) == target:
            return stored
    return None


def attach_mint_job_id(
    store,
    *,
    directory: str,
    mint_job_id: str,
    workflow_id: str | None = None,
    name: str | None = None,
) -> None:
    """Stamp mint_job_id on an already-registered workflow so the UI can link the trace."""

    if not store or not mint_job_id:
        return
    stored = None
    if workflow_id:
        try:
            stored = store.get_workflow(workflow_id)
        except Exception:
            stored = None
    if stored is None and directory:
        stored = already_registered(store, directory)
    if stored is None and name:
        for item in store.list_workflows():
            if item.definition.name == name and getattr(item.definition, "pomerado_artifact", None):
                stored = item
                break
    if stored is None:
        return
    if getattr(stored.definition, "mint_job_id", None) == mint_job_id:
        return
    updated = stored.definition.model_copy(update={"mint_job_id": mint_job_id})
    store.update_workflow(stored.id, updated)


def jail_integration_directory(directory: str | None, root: Any) -> str:
    """Resolve directory under the integrations root; raise if it escapes."""

    from pathlib import Path

    if not directory:
        raise ValueError("integration directory is required")
    base = Path(root).resolve()
    candidate = Path(directory)
    if not candidate.is_absolute():
        candidate = (base / candidate).resolve()
    else:
        candidate = candidate.resolve()
    try:
        candidate.relative_to(base)
    except ValueError as exc:
        raise ValueError("integration directory escapes integrations root") from exc
    return str(candidate)


def register_published_tool(
    store,
    *,
    name: str,
    url: str,
    intent: str,
    effect: str = "read",
    directory: str,
    input_values: dict[str, Any] | None = None,
    tool: str | None = None,
    mint_job_id: str | None = None,
    integrations_root: Any | None = None,
) -> dict[str, Any]:
    """Create (or reuse) a workflow so the tool appears in the phone Workflows list."""

    if integrations_root is not None:
        directory = jail_integration_directory(directory, integrations_root)
    existing = already_registered(store, directory)
    if existing is not None:
        if mint_job_id:
            attach_mint_job_id(store, directory=directory, mint_job_id=mint_job_id, workflow_id=existing.id)
            existing = store.get_workflow(existing.id)
        return {"id": existing.id, "path": existing.path, "created": False, "name": existing.definition.name}
    inputs = input_values or {}
    input_schema = inputs_from_integration(directory, inputs)
    definition = WorkflowDefinition(
        name=name,
        description=intent or name,
        version="1",
        pomerado_artifact=str(directory),
        pomerado_tool=tool or name,
        # Site/session config only — user values are declared inputs, not silent defaults.
        pomerado_input={"url": url, "intent": intent, "effect": effect or "read"},
        mint_job_id=mint_job_id,
        input_schema=input_schema,
        steps=[
            WorkflowStep(
                type="extract",
                description="Served by the minted Pomerado tool (0 tokens).",
                extractionGoal=intent or "run the minted tool",
                output="result",
            )
        ],
    )
    stored = store.create_workflow(definition)
    return {"id": stored.id, "path": stored.path, "created": True, "name": stored.definition.name}
