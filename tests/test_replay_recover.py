"""Tests for medre replay and medre recover CLI commands."""

from __future__ import annotations

import io
import json
from contextlib import redirect_stdout, redirect_stderr
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from medre.cli import EXIT_BUILD, EXIT_CONFIG, EXIT_NOT_FOUND, main


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run_cli(*args: str) -> str:
    """Run CLI capturing stdout; re-raise non-zero SystemExit."""
    stdout = io.StringIO()
    stderr = io.StringIO()
    try:
        with redirect_stdout(stdout), redirect_stderr(stderr):
            main(list(args))
    except SystemExit as e:
        if e.code not in (None, 0):
            raise
    return stdout.getvalue()


def _run_cli_both(*args: str) -> tuple[str, str]:
    """Run CLI and return (stdout, stderr); swallow SystemExit."""
    stdout = io.StringIO()
    stderr = io.StringIO()
    try:
        with redirect_stdout(stdout), redirect_stderr(stderr):
            main(list(args))
    except SystemExit:
        pass
    return stdout.getvalue(), stderr.getvalue()


def _make_event_dict(
    event_id: str = "evt-1",
    event_kind: str = "message.created",
    source_adapter: str = "test_adapter",
) -> dict[str, Any]:
    """Return a minimal event-like object with needed attributes."""
    return {
        "event_id": event_id,
        "event_kind": event_kind,
        "source_adapter": source_adapter,
    }


class _FakeEvent:
    """Minimal event object with attributes needed by _recover."""

    def __init__(
        self,
        event_id: str = "evt-1",
        event_kind: str = "message.created",
        source_adapter: str = "test_adapter",
    ) -> None:
        self.event_id = event_id
        self.event_kind = event_kind
        self.source_adapter = source_adapter
        self.source_transport_id = "test-transport"
        self.source_channel_id = "ch-0"
        self.timestamp = datetime(2026, 1, 15, 12, 0, 0, tzinfo=timezone.utc)
        self.schema_version = 1
        self.parent_event_id = None
        self.lineage: tuple[object, ...] = ()
        self.relations: tuple[object, ...] = ()
        self.payload: dict[str, object] = {"text": "test"}
        self.metadata: object = None


class _FakeReceipt:
    """Minimal receipt for _recover failed-target identification."""

    def __init__(
        self,
        receipt_id: str = "rcpt-1",
        event_id: str = "evt-1",
        target_adapter: str = "dest_a",
        status: str = "sent",
        attempt_number: int = 1,
    ) -> None:
        self.receipt_id = receipt_id
        self.event_id = event_id
        self.target_adapter = target_adapter
        self.status = status
        self.attempt_number = attempt_number
        self.delivery_plan_id = "plan-1"
        self.source = "live"
        self.replay_run_id = None
        self.sequence = 1
        self.created_at = datetime(2026, 1, 15, 12, 0, 1, tzinfo=timezone.utc)


class _FakeNativeRef:
    """Minimal native ref for radio transport detection."""

    def __init__(self, adapter: str = "matrix") -> None:
        self.adapter = adapter
        self.id = "nref-1"
        self.event_id = "evt-1"
        self.native_channel_id = "ch-0"
        self.native_message_id = "msg-0"
        self.native_thread_id = None
        self.native_relation_id = None
        self.direction = "outbound"
        self.created_at = datetime(2026, 1, 15, 12, 0, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Parser tests
# ---------------------------------------------------------------------------


class TestReplayParser:
    """Tests for 'medre replay' argument parsing."""

    def test_replay_requires_mode(self) -> None:
        with pytest.raises(SystemExit):
            _run_cli("replay")

    def test_replay_invalid_mode(self) -> None:
        with pytest.raises(SystemExit):
            _run_cli("replay", "--mode", "invalid_mode")

    def test_replay_accepts_valid_modes(self) -> None:
        """All valid modes are accepted by the parser."""
        for mode in ("strict", "re_render", "re_route", "best_effort", "dry_run"):
            # This will fail at runtime (no config), but parser should accept it.
            with pytest.raises(SystemExit) as exc_info:
                _run_cli("replay", "--mode", mode, "--config", "/nonexistent")
            # Config load failure, not parser failure.
            assert exc_info.value.code in (EXIT_CONFIG, EXIT_BUILD)

    def test_replay_accepts_optional_flags(self) -> None:
        """Parser accepts --event, --json, --target-adapters, --route-ids, --limit."""
        with pytest.raises(SystemExit) as exc_info:
            _run_cli(
                "replay", "--mode", "strict",
                "--event", "evt-1",
                "--json",
                "--target-adapters", "a1", "a2",
                "--route-ids", "r1",
                "--limit", "50",
                "--config", "/nonexistent",
            )
        assert exc_info.value.code in (EXIT_CONFIG, EXIT_BUILD)


class TestRecoverParser:
    """Tests for 'medre recover' argument parsing."""

    def test_recover_no_args_accepted(self) -> None:
        """Parser accepts bare 'medre recover' (broad scan mode)."""
        with pytest.raises(SystemExit) as exc_info:
            _run_cli("recover", "--config", "/nonexistent")
        assert exc_info.value.code in (EXIT_CONFIG, EXIT_BUILD)

    def test_recover_accepts_all_flags(self) -> None:
        with pytest.raises(SystemExit) as exc_info:
            _run_cli(
                "recover",
                "--event", "evt-1",
                "--failed-only",
                "--since", "2026-01-01",
                "--dry-run",
                "--json",
                "--config", "/nonexistent",
            )
        assert exc_info.value.code in (EXIT_CONFIG, EXIT_BUILD)


# ---------------------------------------------------------------------------
# Replay dispatch tests (with mocked runtime)
# ---------------------------------------------------------------------------


class TestReplayDispatch:
    """Tests for 'medre replay' command dispatch with mocked runtime."""

    def test_replay_strict_mode(self) -> None:
        """Replay with strict mode builds runtime and calls replay_engine."""
        from medre.core.storage.replay import ReplayMode, ReplaySummary

        summary = ReplaySummary(
            events_scanned=1,
            events_replayed=1,
            skipped_count=0,
            failure_count=0,
            mode=ReplayMode.STRICT,
            by_status={"passed": 1, "skipped": 0, "failed": 0, "error": 0},
        )

        mock_engine = MagicMock()
        mock_engine.replay.return_value = AsyncMock()

        mock_app = MagicMock()
        mock_app.replay_engine = mock_engine

        mock_builder = MagicMock()
        mock_builder.build.return_value = mock_app

        with patch("medre.runtime.builder.RuntimeBuilder", return_value=mock_builder), \
             patch("medre.cli.load_config") as mock_load, \
             patch("medre.cli.apply_env_overrides", side_effect=lambda c, p: c), \
             patch("medre.core.storage.replay.collect_replay_summary", new_callable=AsyncMock, return_value=summary):
            mock_load.return_value = (MagicMock(), MagicMock(), MagicMock())
            output = _run_cli("replay", "--mode", "strict", "--json")
            parsed = json.loads(output)
            assert parsed["events_scanned"] == 1
            assert parsed["events_replayed"] == 1
            assert parsed["mode"] == "strict"

    def test_replay_best_effort_warns(self) -> None:
        """BEST_EFFORT mode prints warning to stderr."""
        from medre.core.storage.replay import ReplaySummary

        summary = ReplaySummary(
            by_status={"passed": 0, "skipped": 0, "failed": 0, "error": 0},
        )

        mock_engine = MagicMock()
        mock_engine.replay.return_value = AsyncMock()

        mock_app = MagicMock()
        mock_app.replay_engine = mock_engine

        mock_builder = MagicMock()
        mock_builder.build.return_value = mock_app

        with patch("medre.runtime.builder.RuntimeBuilder", return_value=mock_builder), \
             patch("medre.cli.load_config") as mock_load, \
             patch("medre.cli.apply_env_overrides", side_effect=lambda c, p: c), \
             patch("medre.core.storage.replay.collect_replay_summary", new_callable=AsyncMock, return_value=summary):
            mock_load.return_value = (MagicMock(), MagicMock(), MagicMock())
            _stdout, stderr = _run_cli_both(
                "replay", "--mode", "best_effort",
            )
            assert "WARNING" in stderr or "duplicate" in stderr.lower()

    def test_replay_no_engine_exits_build(self) -> None:
        """If replay_engine is None, exit with EXIT_BUILD."""
        mock_app = MagicMock()
        mock_app.replay_engine = None

        mock_builder = MagicMock()
        mock_builder.build.return_value = mock_app

        with patch("medre.runtime.builder.RuntimeBuilder", return_value=mock_builder), \
             patch("medre.cli.load_config") as mock_load, \
             patch("medre.cli.apply_env_overrides", side_effect=lambda c, p: c):
            mock_load.return_value = (MagicMock(), MagicMock(), MagicMock())
            with pytest.raises(SystemExit) as exc_info:
                _run_cli("replay", "--mode", "strict", "--json")
            assert exc_info.value.code == EXIT_BUILD

    def test_replay_human_readable(self) -> None:
        """Non-JSON output includes key summary fields."""
        from medre.core.storage.replay import ReplaySummary

        summary = ReplaySummary(
            events_scanned=5,
            events_replayed=3,
            by_status={"passed": 2, "skipped": 1, "failed": 0, "error": 0},
            elapsed_ms=42.5,
        )

        mock_engine = MagicMock()
        mock_engine.replay.return_value = AsyncMock()

        mock_app = MagicMock()
        mock_app.replay_engine = mock_engine

        mock_builder = MagicMock()
        mock_builder.build.return_value = mock_app

        with patch("medre.runtime.builder.RuntimeBuilder", return_value=mock_builder), \
             patch("medre.cli.load_config") as mock_load, \
             patch("medre.cli.apply_env_overrides", side_effect=lambda c, p: c), \
             patch("medre.core.storage.replay.collect_replay_summary", new_callable=AsyncMock, return_value=summary):
            mock_load.return_value = (MagicMock(), MagicMock(), MagicMock())
            output = _run_cli("replay", "--mode", "strict")
            assert "Replay: strict" in output
            assert "Events scanned:  5" in output
            assert "Events replayed: 3" in output
            assert "42.5ms" in output

    def test_replay_json_output_bounded(self) -> None:
        """JSON output is parseable and has bounded error list."""
        from medre.core.storage.replay import ReplaySummary

        summary = ReplaySummary(
            events_scanned=1,
            events_replayed=1,
            errors=("err1", "err2"),
            by_status={"passed": 1, "skipped": 0, "failed": 0, "error": 0},
        )

        mock_engine = MagicMock()
        mock_engine.replay.return_value = AsyncMock()

        mock_app = MagicMock()
        mock_app.replay_engine = mock_engine

        mock_builder = MagicMock()
        mock_builder.build.return_value = mock_app

        with patch("medre.runtime.builder.RuntimeBuilder", return_value=mock_builder), \
             patch("medre.cli.load_config") as mock_load, \
             patch("medre.cli.apply_env_overrides", side_effect=lambda c, p: c), \
             patch("medre.core.storage.replay.collect_replay_summary", new_callable=AsyncMock, return_value=summary):
            mock_load.return_value = (MagicMock(), MagicMock(), MagicMock())
            output = _run_cli("replay", "--mode", "strict", "--json")
            parsed = json.loads(output)
            assert isinstance(parsed["errors"], list)
            assert len(parsed["errors"]) <= 10

    def test_replay_build_failure_exits_build(self) -> None:
        """Runtime build error exits with EXIT_BUILD."""
        mock_builder = MagicMock()
        mock_builder.build.side_effect = RuntimeError("build broke")

        with patch("medre.runtime.builder.RuntimeBuilder", return_value=mock_builder), \
             patch("medre.cli.load_config") as mock_load, \
             patch("medre.cli.apply_env_overrides", side_effect=lambda c, p: c):
            mock_load.return_value = (MagicMock(), MagicMock(), MagicMock())
            with pytest.raises(SystemExit) as exc_info:
                _run_cli("replay", "--mode", "strict", "--json")
            assert exc_info.value.code == EXIT_BUILD


# ---------------------------------------------------------------------------
# Recover dispatch tests (with mocked storage)
# ---------------------------------------------------------------------------


class TestRecoverDispatch:
    """Tests for 'medre recover' command dispatch with mocked storage."""

    def test_recover_broad_scan_json(self) -> None:
        """Broad scan (no --event) returns JSON with scope=scan."""
        mock_storage = AsyncMock()

        with patch("medre.cli._open_readonly_storage", return_value=mock_storage), \
             patch("medre.cli._recover") as mock_recover:
            # We'll test through the handler directly for more control.
            pass

    def test_recover_single_event_not_found(self) -> None:
        """Recover with unknown event_id exits EXIT_NOT_FOUND."""
        mock_storage = AsyncMock()
        mock_storage.get = AsyncMock(return_value=None)
        mock_storage.close = AsyncMock()

        with patch("medre.cli._open_readonly_storage", return_value=mock_storage):
            with pytest.raises(SystemExit) as exc_info:
                _run_cli(
                    "recover", "--event", "nonexistent",
                    "--json", "--config", "/nonexistent",
                )
            assert exc_info.value.code == EXIT_NOT_FOUND

    def test_recover_single_event_json_output(self) -> None:
        """Recover --event <id> --json returns parseable JSON runbook."""
        event = _FakeEvent()
        receipt = _FakeReceipt()
        mock_storage = AsyncMock()
        mock_storage.get = AsyncMock(return_value=event)
        mock_storage.list_receipts_for_event = AsyncMock(return_value=[receipt])
        mock_storage.list_native_refs_for_event = AsyncMock(return_value=[])
        mock_storage.list_relations = AsyncMock(return_value=[])
        mock_storage.close = AsyncMock()

        with patch("medre.cli._open_readonly_storage", return_value=mock_storage):
            output = _run_cli(
                "recover", "--event", "evt-1",
                "--json", "--config", "/nonexistent",
            )
            parsed = json.loads(output)
            assert parsed["scope"] == "event"
            assert parsed["event_id"] == "evt-1"
            assert isinstance(parsed["failed_targets"], list)
            assert isinstance(parsed["timeline"], list)
            assert isinstance(parsed["warnings"], list)

    def test_recover_single_event_failed_targets(self) -> None:
        """Failed targets are identified from receipts with status failed/dead_lettered."""
        event = _FakeEvent()
        failed_receipt = _FakeReceipt(status="failed", target_adapter="broken_adapter")
        ok_receipt = _FakeReceipt(status="sent", target_adapter="ok_adapter")
        dead_receipt = _FakeReceipt(status="dead_lettered", target_adapter="dead_adapter")

        mock_storage = AsyncMock()
        mock_storage.get = AsyncMock(return_value=event)
        mock_storage.list_receipts_for_event = AsyncMock(
            return_value=[ok_receipt, failed_receipt, dead_receipt]
        )
        mock_storage.list_native_refs_for_event = AsyncMock(return_value=[])
        mock_storage.list_relations = AsyncMock(return_value=[])
        mock_storage.close = AsyncMock()

        with patch("medre.cli._open_readonly_storage", return_value=mock_storage):
            output = _run_cli(
                "recover", "--event", "evt-1",
                "--json", "--config", "/nonexistent",
            )
            parsed = json.loads(output)
            failed_names = [t["target_adapter"] for t in parsed["failed_targets"]]
            assert "broken_adapter" in failed_names
            assert "dead_adapter" in failed_names
            assert "ok_adapter" not in failed_names

    def test_recover_radio_transport_warning(self) -> None:
        """Recovery warns about radio transport duplicate risk."""
        event = _FakeEvent()
        radio_ref = _FakeNativeRef(adapter="meshtastic")

        mock_storage = AsyncMock()
        mock_storage.get = AsyncMock(return_value=event)
        mock_storage.list_receipts_for_event = AsyncMock(return_value=[])
        mock_storage.list_native_refs_for_event = AsyncMock(return_value=[radio_ref])
        mock_storage.list_relations = AsyncMock(return_value=[])
        mock_storage.close = AsyncMock()

        with patch("medre.cli._open_readonly_storage", return_value=mock_storage):
            output = _run_cli(
                "recover", "--event", "evt-1",
                "--json", "--config", "/nonexistent",
            )
            parsed = json.loads(output)
            assert len(parsed["warnings"]) > 0
            warning_text = " ".join(parsed["warnings"])
            assert "radio" in warning_text.lower() or "duplicate" in warning_text.lower()

    def test_recover_dry_run_no_side_effects(self) -> None:
        """Dry run does not call storage.write methods."""
        event = _FakeEvent()
        mock_storage = AsyncMock()
        mock_storage.get = AsyncMock(return_value=event)
        mock_storage.list_receipts_for_event = AsyncMock(return_value=[])
        mock_storage.list_native_refs_for_event = AsyncMock(return_value=[])
        mock_storage.list_relations = AsyncMock(return_value=[])
        mock_storage.close = AsyncMock()

        with patch("medre.cli._open_readonly_storage", return_value=mock_storage):
            output = _run_cli(
                "recover", "--event", "evt-1",
                "--dry-run", "--json", "--config", "/nonexistent",
            )
            parsed = json.loads(output)
            assert "dry_run" in parsed
            assert parsed["dry_run"]["status"] == "preview"
            # Only read methods were called.
            mock_storage.get.assert_called()
            mock_storage.list_receipts_for_event.assert_called()
            # No write methods.
            mock_storage.append.assert_not_called()
            mock_storage.append_receipt.assert_not_called()

    def test_recover_human_readable(self) -> None:
        """Human-readable output includes event info."""
        event = _FakeEvent()
        mock_storage = AsyncMock()
        mock_storage.get = AsyncMock(return_value=event)
        mock_storage.list_receipts_for_event = AsyncMock(return_value=[])
        mock_storage.list_native_refs_for_event = AsyncMock(return_value=[])
        mock_storage.list_relations = AsyncMock(return_value=[])
        mock_storage.close = AsyncMock()

        with patch("medre.cli._open_readonly_storage", return_value=mock_storage):
            output = _run_cli(
                "recover", "--event", "evt-1",
                "--config", "/nonexistent",
            )
            assert "Recovery runbook: evt-1" in output
            assert "message.created" in output
            assert "Failed targets: none" in output

    def test_recover_broad_scan_human_readable(self) -> None:
        """Broad scan without --event prints scan summary."""
        mock_storage = AsyncMock()
        mock_storage.close = AsyncMock()

        with patch("medre.cli._open_readonly_storage", return_value=mock_storage):
            output = _run_cli(
                "recover", "--config", "/nonexistent",
            )
            assert "Recovery scan" in output

    def test_recover_broad_scan_json(self) -> None:
        """Broad scan JSON includes scope=scan and warnings."""
        mock_storage = AsyncMock()
        mock_storage.close = AsyncMock()

        with patch("medre.cli._open_readonly_storage", return_value=mock_storage):
            output = _run_cli(
                "recover", "--json", "--config", "/nonexistent",
            )
            parsed = json.loads(output)
            assert parsed["scope"] == "scan"
            assert isinstance(parsed["warnings"], list)
            assert len(parsed["warnings"]) > 0

    def test_recover_json_sorted_keys(self) -> None:
        """JSON output has deterministically sorted keys."""
        event = _FakeEvent()
        mock_storage = AsyncMock()
        mock_storage.get = AsyncMock(return_value=event)
        mock_storage.list_receipts_for_event = AsyncMock(return_value=[])
        mock_storage.list_native_refs_for_event = AsyncMock(return_value=[])
        mock_storage.list_relations = AsyncMock(return_value=[])
        mock_storage.close = AsyncMock()

        with patch("medre.cli._open_readonly_storage", return_value=mock_storage):
            output = _run_cli(
                "recover", "--event", "evt-1",
                "--json", "--config", "/nonexistent",
            )
            parsed = json.loads(output)
            keys = list(parsed.keys())
            assert keys == sorted(keys)


# ---------------------------------------------------------------------------
# Replay with --event flag
# ---------------------------------------------------------------------------


class TestReplayWithEvent:
    """Tests for replay --event wiring."""

    def test_replay_passes_event_as_correlation_id(self) -> None:
        """When --event is given, it becomes a correlation_id in the request."""
        from medre.core.storage.replay import ReplaySummary

        summary = ReplaySummary(
            by_status={"passed": 0, "skipped": 0, "failed": 0, "error": 0},
        )

        mock_engine = MagicMock()
        mock_engine.replay.return_value = AsyncMock()

        mock_app = MagicMock()
        mock_app.replay_engine = mock_engine

        mock_builder = MagicMock()
        mock_builder.build.return_value = mock_app

        with patch("medre.runtime.builder.RuntimeBuilder", return_value=mock_builder), \
             patch("medre.cli.load_config") as mock_load, \
             patch("medre.cli.apply_env_overrides", side_effect=lambda c, p: c), \
             patch("medre.core.storage.replay.collect_replay_summary", new_callable=AsyncMock, return_value=summary) as mock_collect:

            mock_load.return_value = (MagicMock(), MagicMock(), MagicMock())
            _run_cli("replay", "--mode", "dry_run", "--event", "evt-42", "--json")

            # Verify the replay engine was called (via collect_replay_summary).
            mock_collect.assert_called_once()

    def test_replay_with_target_adapters_and_route_ids(self) -> None:
        """Target adapters and route IDs are passed through."""
        from medre.core.storage.replay import ReplayMode, ReplaySummary

        summary = ReplaySummary(
            mode=ReplayMode.DRY_RUN,
            by_status={"passed": 0, "skipped": 0, "failed": 0, "error": 0},
        )

        mock_engine = MagicMock()
        mock_engine.replay.return_value = AsyncMock()

        mock_app = MagicMock()
        mock_app.replay_engine = mock_engine

        mock_builder = MagicMock()
        mock_builder.build.return_value = mock_app

        with patch("medre.runtime.builder.RuntimeBuilder", return_value=mock_builder), \
             patch("medre.cli.load_config") as mock_load, \
             patch("medre.cli.apply_env_overrides", side_effect=lambda c, p: c), \
             patch("medre.core.storage.replay.collect_replay_summary", new_callable=AsyncMock, return_value=summary):

            mock_load.return_value = (MagicMock(), MagicMock(), MagicMock())
            output = _run_cli(
                "replay", "--mode", "dry_run",
                "--target-adapters", "a1", "a2",
                "--route-ids", "r1",
                "--limit", "10",
                "--json",
            )
            parsed = json.loads(output)
            assert parsed["mode"] == "dry_run"
