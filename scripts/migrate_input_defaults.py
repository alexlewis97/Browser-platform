"""One-off migration (defaults-v1): remove example values masquerading as input defaults.

A default is kept only if it is a runtime token (current month, today, ...) or has a
trusted source. Literal defaults without provenance become examples, and the input is
required (or, for optional inputs used only by action steps, the step is skipped when no
value is given so the page keeps its own state). Idempotent. Dry-run by default.

    python scripts/migrate_input_defaults.py --data /data            # dry run
    python scripts/migrate_input_defaults.py --data /data --apply    # write (new version per changed workflow)

Can be piped into the container: docker exec -i browser-platform python - --data /data < scripts/migrate_input_defaults.py
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from browser_platform.store.file_store import FileStore
from browser_platform.workflow_inputs import MIGRATION_TAG, migrate_defaults


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data", required=True, help="platform data dir (contains workflows/)")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    store = FileStore(Path(args.data))
    report = []
    for stored in store.list_workflows():
        definition = stored.definition
        if getattr(definition, "inputs_migrated", None) == MIGRATION_TAG:
            continue
        migrated, changes = migrate_defaults(definition)
        if not changes:
            continue
        report.append({"id": stored.id, "name": definition.name, "changes": changes})
        if args.apply:
            setattr(migrated, "inputs_migrated", MIGRATION_TAG)
            store.update_workflow(stored.id, migrated)
    print(json.dumps({"applied": args.apply, "workflows": report}, indent=1, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
