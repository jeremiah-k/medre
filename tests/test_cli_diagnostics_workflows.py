"""Diagnostics CLI workflows — JSON validation, schema, and secret redaction.

Operators run ``medre diagnostics`` for pre-flight snapshots and verify the
output structure, determinism, and absence of secrets.
"""

from __future__ import annotations

import json
from pathlib import Path

from tests.helpers.cli import (
    _run_cli,
    _run_cli_raw,
)


class TestDiagnosticsWorkflow:
    """Operators run 'medre diagnostics' for pre-flight snapshots."""

    def test_diagnostics_produces_valid_json(self, config_fake_multi: Path) -> None:
        output = _run_cli("diagnostics", "--config", str(config_fake_multi))
        parsed = json.loads(output)
        assert isinstance(parsed, dict)

    def test_diagnostics_has_snapshot_scope_build(self, config_fake_multi: Path) -> None:
        """Plain diagnostics emits snapshot_scope='build'."""
        output = _run_cli("diagnostics", "--config", str(config_fake_multi))
        parsed = json.loads(output)
        assert parsed["snapshot_scope"] == "build"

    def test_diagnostics_has_schema_version(self, config_fake_multi: Path) -> None:
        output = _run_cli("diagnostics", "--config", str(config_fake_multi))
        parsed = json.loads(output)
        assert "schema_version" in parsed
        assert isinstance(parsed["schema_version"], int)

    def test_diagnostics_has_runtime_state(self, config_fake_multi: Path) -> None:
        output = _run_cli("diagnostics", "--config", str(config_fake_multi))
        parsed = json.loads(output)
        assert "lifecycle" in parsed
        assert "runtime_state" in parsed["lifecycle"]
        assert isinstance(parsed["lifecycle"]["runtime_state"], str)

    def test_diagnostics_has_adapters(self, config_fake_multi: Path) -> None:
        output = _run_cli("diagnostics", "--config", str(config_fake_multi))
        parsed = json.loads(output)
        assert "adapters" in parsed
        assert isinstance(parsed["adapters"], dict)

    def test_diagnostics_has_routes(self, config_fake_multi: Path) -> None:
        output = _run_cli("diagnostics", "--config", str(config_fake_multi))
        parsed = json.loads(output)
        assert "routes" in parsed

    def test_diagnostics_has_limits(self, config_fake_multi: Path) -> None:
        output = _run_cli("diagnostics", "--config", str(config_fake_multi))
        parsed = json.loads(output)
        assert "limits" in parsed

    def test_diagnostics_deterministic_keys_sorted(
        self, config_fake_multi: Path
    ) -> None:
        """JSON keys are sorted for stable output."""
        output = _run_cli("diagnostics", "--config", str(config_fake_multi))
        parsed = json.loads(output)
        top_keys = list(parsed.keys())
        assert top_keys == sorted(top_keys), f"keys not sorted: {top_keys}"

    def test_diagnostics_no_secrets(self, config_fake_multi: Path) -> None:
        output = _run_cli("diagnostics", "--config", str(config_fake_multi))
        assert "fake_tok" not in output
        assert "access_token" not in output

    def test_diagnostics_missing_config_clean(self, tmp_path: Path) -> None:
        _, stderr, code = _run_cli_raw(
            "diagnostics", "--config", str(tmp_path / "missing.toml")
        )
        assert code != 0
        assert "Traceback" not in stderr
        assert "Config error:" in stderr

    def test_diagnostics_with_single_adapter(self, config_single: Path) -> None:
        output = _run_cli("diagnostics", "--config", str(config_single))
        parsed = json.loads(output)
        assert "adapters" in parsed
        assert len(parsed["adapters"]) >= 1
