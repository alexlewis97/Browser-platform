"""Node-side OpenRouter tool adapter tests (hosted apply_patch -> function tool)."""

from __future__ import annotations

import subprocess
from pathlib import Path

HOST = Path(__file__).resolve().parents[1] / "node_host"


def test_openrouter_tool_and_provider_node_unit_tests():
    result = subprocess.run(
        ["node", "--test", "src/openrouter-tools.test.mjs", "src/openrouter-provider.test.mjs"],
        cwd=HOST,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + "\n" + result.stderr
    assert "fail 0" in (result.stdout + result.stderr) or "passed" in (result.stdout + result.stderr).lower()
