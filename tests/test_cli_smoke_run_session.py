"""Operator bridge session lifecycle tests (run_fake_bridge_smoke).

Full operator lifecycle: start -> inject -> stop -> snapshot -> inspect ->
trace -> evidence.  Every test uses ``run_fake_bridge_smoke`` which exercises
the complete runtime pipeline with fake adapters.  No Docker, no network, no
SDKs.
"""

from __future__ import annotations

import io
import json
import shlex
from contextlib import redirect_stdout
from pathlib import Path

import pytest


def _smoke_config_path() -> str:
    """Return path to the shipped fake-bridge-smoke.toml."""
    from medre.runtime.smoke import _default_smoke_config_path

    path = _default_smoke_config_path()
    assert path is not None, "examples/configs/fake-bridge-smoke.toml not found"
    return path


class TestOperatorBridgeSession:
    """Full operator lifecycle: start -> inject -> stop -> snapshot -> inspect -> trace -> evidence."""

    @pytest.mark.asyncio
    async def test_run_session_full_lifecycle(self, tmp_path: Path) -> None:
        """Full bridge session with persistent storage produces PASS with all evidence."""
        from medre.runtime.smoke import run_fake_bridge_smoke

        db_path = str(tmp_path / "session.db")
        report = await run_fake_bridge_smoke(
            _smoke_config_path(),
            storage_path=db_path,
        )

        # -- Status --
        assert report["status"] == "passed", (
            f"Expected PASS, got {report['status']}: "
            f"{report.get('fail_reasons', [])}"
        )

        # -- Storage path present --
        assert report["storage_path"] == db_path

        # -- Event ID present --
        assert isinstance(report["event_id"], str)
        assert len(report["event_id"]) > 0

        # -- Receipts found --
        receipts = report["delivery_receipts"]
        assert isinstance(receipts, list)
        assert len(receipts) >= 1
        for r in receipts:
            assert r["status"] == "sent"

        # -- Native refs found --
        native_refs = report["native_refs"]
        assert isinstance(native_refs, list)
        assert len(native_refs) >= 1
        for ref in native_refs:
            assert ref["resolves_to"] == report["event_id"]

        # -- Accounting counters present --
        acc = report["accounting"]
        assert isinstance(acc, dict)
        assert acc["outbound_delivered"] >= 1

        # -- Limits present in snapshot --
        snap = report["snapshot"]
        assert "accounting" in snap
        assert snap["accounting"] is not None

    @pytest.mark.asyncio
    async def test_run_session_report_cross_links(self, tmp_path: Path) -> None:
        """Report commands_text/commands_argv reflect real --storage-path shape."""
        from medre.runtime.run_session.orchestration import run_bridge_session

        config_path = _smoke_config_path()
        db_path = str(tmp_path / "crosslink.db")
        report = await run_bridge_session(
            config_path=config_path,
            storage_path=db_path,
        )
        assert report["status"] == "passed"
        assert report["storage_path"] == db_path

        event_id = report["event_id"]
        cmd_text = report["commands"]["commands_text"]
        cmd_argv = report["commands"]["commands_argv"]

        # -- Primary keys: inspect-first commands using --storage-path --
        primary_keys = [
            "inspect_event",
            "inspect_timeline",
            "inspect_receipts",
            "inspect_evidence",
            "inspect_recovery",
        ]
        for key in primary_keys:
            assert key in cmd_text["primary"], f"Missing primary key: {key}"
            assert key in cmd_argv["primary"], f"Missing primary argv key: {key}"

            txt = cmd_text["primary"][key]
            argv = cmd_argv["primary"][key]

            # Contains event_id and --storage-path with the actual DB path
            assert event_id in txt, f"primary.{key} text missing event_id"
            assert (
                "--storage-path" in txt
            ), f"primary.{key} text missing --storage-path: {txt}"
            assert db_path in txt, f"primary.{key} text missing db_path: {txt}"
            # No stale --config in primary read-only commands
            assert "--config" not in txt, f"primary.{key} has stale --config: {txt}"

            # argv mirrors text via shlex.split
            assert (
                shlex.split(txt) == argv
            ), f"primary.{key} argv mismatch: {argv!r} vs shlex.split({txt!r})"

        # -- Specialized keys: trace/evidence (storage-path), recover (config) --
        specialized_keys = ["trace_event", "evidence_bundle", "recover_event"]
        for key in specialized_keys:
            assert key in cmd_text["specialized"], f"Missing specialized key: {key}"
            assert (
                key in cmd_argv["specialized"]
            ), f"Missing specialized argv key: {key}"

            txt = cmd_text["specialized"][key]
            argv = cmd_argv["specialized"][key]

            assert event_id in txt, f"specialized.{key} text missing event_id"

            # Read-only specialized commands use --storage-path
            if key in ("trace_event", "evidence_bundle"):
                assert (
                    "--storage-path" in txt
                ), f"specialized.{key} missing --storage-path: {txt}"
                assert db_path in txt, f"specialized.{key} missing db_path: {txt}"

            # recover_event is config-required
            if key == "recover_event":
                assert "--config" in txt, f"specialized.{key} missing --config: {txt}"

            # argv mirrors text
            assert (
                shlex.split(txt) == argv
            ), f"specialized.{key} argv mismatch: {argv!r} vs shlex.split({txt!r})"

        # -- No stale primary keys for trace/evidence --
        for stale_key in ("trace", "evidence"):
            assert (
                stale_key not in cmd_text["primary"]
            ), f"Stale primary key '{stale_key}' should be under specialized"
            assert (
                stale_key not in cmd_argv["primary"]
            ), f"Stale primary argv key '{stale_key}' should be under specialized"

        # -- Native refs still present and inspectable --
        for ref in report["native_refs"]:
            assert "adapter" in ref
            assert "native_id" in ref

    @pytest.mark.asyncio
    async def test_run_session_persistent_storage(self, tmp_path: Path) -> None:
        """SQLite file is created at storage_path and event is inspectable via CLI."""
        from medre.runtime.smoke import run_fake_bridge_smoke

        db_path = str(tmp_path / "persist.db")
        report = await run_fake_bridge_smoke(
            _smoke_config_path(),
            storage_path=db_path,
        )
        assert report["status"] == "passed"

        # -- SQLite file exists --
        assert Path(db_path).is_file(), f"SQLite DB not created at {db_path}"

        # -- Write a config that points to the same DB for CLI inspection --
        inspect_config = tmp_path / "inspect_config.toml"
        inspect_config.write_text(
            f'[runtime]\nname = "inspect-test"\n\n[storage]\n'
            f'backend = "sqlite"\npath = "{db_path}"\n'
        )

        # -- Inspect event directly (async, not via CLI to avoid nested event loop) --
        event_id = report["event_id"]
        from medre.cli.inspect_commands import _inspect_event
        from medre.cli.trace_commands import _trace_event

        # Inspect event
        evt_stdout = io.StringIO()
        with redirect_stdout(evt_stdout):
            await _inspect_event(str(inspect_config), event_id)
        assert event_id in evt_stdout.getvalue()

        # Inspect receipts
        from medre.cli.inspect_commands import _inspect_receipts

        rcpt_stdout = io.StringIO()
        with redirect_stdout(rcpt_stdout):
            await _inspect_receipts(
                str(inspect_config), event_id=event_id, replay_run_id=None
            )
        assert len(rcpt_stdout.getvalue().strip()) > 0

        # Trace event
        trace_stdout = io.StringIO()
        with redirect_stdout(trace_stdout):
            await _trace_event(str(inspect_config), event_id, json_output=False)
        assert event_id in trace_stdout.getvalue()

    @pytest.mark.asyncio
    async def test_run_session_snapshot_schema(self, tmp_path: Path) -> None:
        """Final snapshot has schema_version=1, lifecycle section, and accounting section."""
        from medre.runtime.smoke import run_fake_bridge_smoke

        db_path = str(tmp_path / "schema.db")
        report = await run_fake_bridge_smoke(
            _smoke_config_path(),
            storage_path=db_path,
        )
        assert report["status"] == "passed"

        snap = report["snapshot"]

        assert snap["schema_version"] == 1
        assert "lifecycle" in snap
        assert "runtime_state" in snap["lifecycle"]
        assert "accounting" in snap
        assert snap["accounting"] is not None
        assert "routes" in snap

    @pytest.mark.asyncio
    async def test_run_session_accounting_consistency(self, tmp_path: Path) -> None:
        """Accounting field names match run_commands.py output; no stale fields."""
        from medre.runtime.smoke import run_fake_bridge_smoke

        db_path = str(tmp_path / "accounting.db")
        report = await run_fake_bridge_smoke(
            _smoke_config_path(),
            storage_path=db_path,
        )
        assert report["status"] == "passed"

        acc = report["accounting"]
        assert isinstance(acc, dict)

        required_fields = [
            "inbound_accepted",
            "outbound_delivered",
            "outbound_failed",
            "loop_prevented",
            "capacity_rejections",
        ]
        for field in required_fields:
            assert field in acc, f"Missing required accounting field: {field}"

        for field in required_fields:
            assert isinstance(
                acc[field], int
            ), f"Accounting field {field} is not int: {type(acc[field])}"
            assert acc[field] >= 0, f"Accounting field {field} is negative"

        assert acc["outbound_delivered"] >= 1
        assert acc["inbound_accepted"] >= 1

        stale_fields = [
            "delivery_timeouts",
            "retry_exhausted",
            "legacy_delivered",
        ]
        for field in stale_fields:
            assert field not in acc, f"Stale accounting field present: {field}"

    @pytest.mark.asyncio
    async def test_run_session_json_safe(self, tmp_path: Path) -> None:
        """Report JSON is fully parseable and all command strings are valid."""
        from medre.runtime.smoke import run_fake_bridge_smoke

        db_path = str(tmp_path / "json_safe.db")
        report = await run_fake_bridge_smoke(
            _smoke_config_path(),
            storage_path=db_path,
        )
        assert report["status"] == "passed"

        serialized = json.dumps(report, sort_keys=True)
        assert isinstance(serialized, str)
        parsed = json.loads(serialized)
        assert parsed["status"] == "passed"
        assert parsed["event_id"] == report["event_id"]

        assert isinstance(parsed["event_id"], str)
        assert len(parsed["event_id"]) > 0
        assert isinstance(parsed["evidence_level"], str)
        assert parsed["evidence_level"] == "fake_bridge"

        assert isinstance(parsed["snapshot"], dict)
        assert isinstance(parsed["accounting"], dict)
        assert isinstance(parsed["delivery_receipts"], list)
        assert isinstance(parsed["native_refs"], list)

    @pytest.mark.asyncio
    async def test_run_session_ephemeral_fallback(self) -> None:
        """Without storage_path, run_session uses temporary SQLite storage and report notes this."""
        from medre.runtime.smoke import run_fake_bridge_smoke

        report = await run_fake_bridge_smoke(_smoke_config_path())

        assert report["status"] == "passed"

        assert "storage_path" not in report
        assert report["storage_backend"] == "memory"

        assert isinstance(report["event_id"], str)
        assert len(report["event_id"]) > 0
        receipts = report["delivery_receipts"]
        assert len(receipts) >= 1
        assert report["accounting"]["outbound_delivered"] >= 1
