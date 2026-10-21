# medre-test-tier: compatibility
"""Consumer type checks for the plugin lifecycle and stage interfaces."""

import subprocess
import sys
from pathlib import Path


def test_plugin_protocols_accept_callback_names_and_reject_wrong_inputs(
    tmp_path: Path,
) -> None:
    root = Path(__file__).resolve().parents[1]
    client = tmp_path / "plugin_client.py"
    source = """from medre.plugins import (
    Allow, EnrichPlugin, Pass, Plugin, PluginContext, PolicyPlugin, TransformPlugin,
)
from medre.core.events import CanonicalEvent

class Valid:
    async def initialize(self, ctx: PluginContext) -> None:
        pass
    async def shutdown(self) -> None:
        pass
    async def enrich(self, message: CanonicalEvent) -> Pass:
        return Pass()
    async def transform(self, message: CanonicalEvent) -> Pass:
        return Pass()
    async def evaluate(self, message: CanonicalEvent) -> Allow:
        return Allow()

class Invalid:
    async def initialize(self, ctx: int) -> None:
        pass
    async def shutdown(self) -> None:
        pass
    async def enrich(self, message: int) -> Pass:
        return Pass()
    async def transform(self, message: int) -> Pass:
        return Pass()
    async def evaluate(self, message: int) -> Allow:
        return Allow()

lifecycle: Plugin = Valid()
enrich: EnrichPlugin = Valid()
transform: TransformPlugin = Valid()
policy: PolicyPlugin = Valid()
bad_lifecycle: Plugin = Invalid()
bad_enrich: EnrichPlugin = Invalid()
bad_transform: TransformPlugin = Invalid()
bad_policy: PolicyPlugin = Invalid()
"""
    client.write_text(source, encoding="utf-8")
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "mypy",
            "--no-incremental",
            "--config-file",
            str(root / "pyproject.toml"),
            str(client),
        ],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    errors = [line for line in result.stdout.splitlines() if ": error:" in line]
    expected_lines = [
        index
        for index, line in enumerate(source.splitlines(), 1)
        if line.startswith("bad_")
    ]
    assert result.returncode == 1, result.stdout + result.stderr
    assert len(errors) == len(expected_lines), result.stdout + result.stderr
    for line_number, error in zip(expected_lines, errors, strict=True):
        assert f":{line_number}: error:" in error
        assert "[assignment]" in error
