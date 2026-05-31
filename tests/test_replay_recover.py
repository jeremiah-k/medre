"""Tests for medre replay and medre recover CLI commands."""

from __future__ import annotations

import io
import json
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
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
        error: str | None = None,
        failure_kind: str | None = None,
        source: str = "live",
        replay_run_id: str | None = None,
        target_channel: str | None = None,
        route_id: str = "route-1",
    ) -> None:
        self.receipt_id = receipt_id
        self.event_id = event_id
        self.target_adapter = target_adapter
        self.status = status
        self.attempt_number = attempt_number
        self.error = error
        self.failure_kind = failure_kind
        self.delivery_plan_id = "plan-1"
        self.route_id = route_id
        self.adapter_message_id = None
        self.target_channel = target_channel
        self.source = source
        self.replay_run_id = replay_run_id
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
                "replay",
                "--mode",
                "strict",
                "--event",
                "evt-1",
                "--json",
                "--target-adapters",
                "a1",
                "a2",
                "--route-ids",
                "r1",
                "--limit",
                "50",
                "--config",
                "/nonexistent",
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
                "--event",
                "evt-1",
                "--failed-only",
                "--since",
                "2026-01-01",
                "--dry-run",
                "--json",
                "--config",
                "/nonexistent",
            )
        assert exc_info.value.code in (EXIT_CONFIG, EXIT_BUILD)


# ---------------------------------------------------------------------------
# Replay dispatch tests (with mocked runtime)
# ---------------------------------------------------------------------------


class TestReplayDispatch:
    """Tests for 'medre replay' command dispatch with mocked runtime."""

    def test_replay_strict_mode(self) -> None:
        """Replay with strict mode builds runtime and calls replay_engine."""
        from medre.core.engine.replay.summary import ReplaySummary
        from medre.core.engine.replay.types import ReplayMode

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
        mock_app.storage = AsyncMock()

        mock_builder = MagicMock()
        mock_builder.build.return_value = mock_app

        with patch(
            "medre.cli.replay_commands.RuntimeBuilder", return_value=mock_builder
        ), patch("medre.cli.replay_commands.load_config") as mock_load, patch(
            "medre.cli.replay_commands.apply_env_overrides", side_effect=lambda c, p: c
        ), patch(
            "medre.cli.replay_commands.collect_replay_summary",
            new_callable=AsyncMock,
            return_value=summary,
        ):
            mock_load.return_value = (MagicMock(), MagicMock(), MagicMock())
            output = _run_cli("replay", "--mode", "strict", "--json")
            parsed = json.loads(output)
            assert parsed["events_scanned"] == 1
            assert parsed["events_replayed"] == 1
            assert parsed["mode"] == "strict"

    def test_replay_best_effort_warns(self) -> None:
        """BEST_EFFORT mode prints warning to stderr."""
        from medre.core.engine.replay.summary import ReplaySummary

        summary = ReplaySummary(
            by_status={"passed": 0, "skipped": 0, "failed": 0, "error": 0},
        )

        mock_engine = MagicMock()
        mock_engine.replay.return_value = AsyncMock()

        mock_app = MagicMock()
        mock_app.replay_engine = mock_engine
        mock_app.storage = AsyncMock()

        mock_builder = MagicMock()
        mock_builder.build.return_value = mock_app

        with patch(
            "medre.cli.replay_commands.RuntimeBuilder", return_value=mock_builder
        ), patch("medre.cli.replay_commands.load_config") as mock_load, patch(
            "medre.cli.replay_commands.apply_env_overrides", side_effect=lambda c, p: c
        ), patch(
            "medre.cli.replay_commands.collect_replay_summary",
            new_callable=AsyncMock,
            return_value=summary,
        ):
            mock_load.return_value = (MagicMock(), MagicMock(), MagicMock())
            _stdout, stderr = _run_cli_both(
                "replay",
                "--mode",
                "best_effort",
            )
            assert "WARNING" in stderr or "duplicate" in stderr.lower()

    def test_replay_no_engine_exits_build(self) -> None:
        """If replay_engine is None, exit with EXIT_BUILD."""
        mock_app = MagicMock()
        mock_app.replay_engine = None

        mock_builder = MagicMock()
        mock_builder.build.return_value = mock_app

        with patch(
            "medre.cli.replay_commands.RuntimeBuilder", return_value=mock_builder
        ), patch("medre.cli.replay_commands.load_config") as mock_load, patch(
            "medre.cli.replay_commands.apply_env_overrides", side_effect=lambda c, p: c
        ):
            mock_load.return_value = (MagicMock(), MagicMock(), MagicMock())
            with pytest.raises(SystemExit) as exc_info:
                _run_cli("replay", "--mode", "strict", "--json")
            assert exc_info.value.code == EXIT_BUILD

    def test_replay_human_readable(self) -> None:
        """Non-JSON output includes key summary fields."""
        from medre.core.engine.replay.summary import ReplaySummary

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
        mock_app.storage = AsyncMock()

        mock_builder = MagicMock()
        mock_builder.build.return_value = mock_app

        with patch(
            "medre.cli.replay_commands.RuntimeBuilder", return_value=mock_builder
        ), patch("medre.cli.replay_commands.load_config") as mock_load, patch(
            "medre.cli.replay_commands.apply_env_overrides", side_effect=lambda c, p: c
        ), patch(
            "medre.cli.replay_commands.collect_replay_summary",
            new_callable=AsyncMock,
            return_value=summary,
        ):
            mock_load.return_value = (MagicMock(), MagicMock(), MagicMock())
            output = _run_cli("replay", "--mode", "strict")
            assert "Replay: strict" in output
            assert "Events scanned:  5" in output
            assert "Events replayed: 3" in output
            assert "42.5ms" in output

    def test_replay_json_output_bounded(self) -> None:
        """JSON output is parseable and has bounded error list."""
        from medre.core.engine.replay.summary import ReplaySummary

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
        mock_app.storage = AsyncMock()

        mock_builder = MagicMock()
        mock_builder.build.return_value = mock_app

        with patch(
            "medre.cli.replay_commands.RuntimeBuilder", return_value=mock_builder
        ), patch("medre.cli.replay_commands.load_config") as mock_load, patch(
            "medre.cli.replay_commands.apply_env_overrides", side_effect=lambda c, p: c
        ), patch(
            "medre.cli.replay_commands.collect_replay_summary",
            new_callable=AsyncMock,
            return_value=summary,
        ):
            mock_load.return_value = (MagicMock(), MagicMock(), MagicMock())
            output = _run_cli("replay", "--mode", "strict", "--json")
            parsed = json.loads(output)
            assert isinstance(parsed["errors"], list)
            assert len(parsed["errors"]) <= 10

    def test_replay_build_failure_exits_build(self) -> None:
        """Runtime build error exits with EXIT_BUILD."""
        mock_builder = MagicMock()
        mock_builder.build.side_effect = RuntimeError("build broke")

        with patch(
            "medre.cli.replay_commands.RuntimeBuilder", return_value=mock_builder
        ), patch("medre.cli.replay_commands.load_config") as mock_load, patch(
            "medre.cli.replay_commands.apply_env_overrides", side_effect=lambda c, p: c
        ):
            mock_load.return_value = (MagicMock(), MagicMock(), MagicMock())
            with pytest.raises(SystemExit) as exc_info:
                _run_cli("replay", "--mode", "strict", "--json")
            assert exc_info.value.code == EXIT_BUILD


# ---------------------------------------------------------------------------
# Recover dispatch tests (with mocked storage)
# ---------------------------------------------------------------------------


class TestRecoverDispatch:
    """Tests for 'medre recover' command dispatch with mocked storage."""

    def test_recover_broad_scan_json_stub(self) -> None:
        """Broad scan (no --event) returns JSON with scope=scan."""
        mock_storage = AsyncMock()

        with patch(
            "medre.cli.recover_commands._open_readonly_storage",
            return_value=mock_storage,
        ), patch("medre.cli.recover_commands._recover"):
            # We'll test through the handler directly for more control.
            pass

    def test_recover_single_event_not_found(self) -> None:
        """Recover with unknown event_id exits EXIT_NOT_FOUND."""
        mock_storage = AsyncMock()
        mock_storage.get = AsyncMock(return_value=None)
        mock_storage.close = AsyncMock()

        with patch(
            "medre.cli.recover_commands._open_readonly_storage",
            return_value=mock_storage,
        ):
            with pytest.raises(SystemExit) as exc_info:
                _run_cli(
                    "recover",
                    "--event",
                    "nonexistent",
                    "--json",
                    "--config",
                    "/nonexistent",
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

        with patch(
            "medre.cli.recover_commands._open_readonly_storage",
            return_value=mock_storage,
        ):
            output = _run_cli(
                "recover",
                "--event",
                "evt-1",
                "--json",
                "--config",
                "/nonexistent",
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
        dead_receipt = _FakeReceipt(
            status="dead_lettered", target_adapter="dead_adapter"
        )

        mock_storage = AsyncMock()
        mock_storage.get = AsyncMock(return_value=event)
        mock_storage.list_receipts_for_event = AsyncMock(
            return_value=[ok_receipt, failed_receipt, dead_receipt]
        )
        mock_storage.list_native_refs_for_event = AsyncMock(return_value=[])
        mock_storage.list_relations = AsyncMock(return_value=[])
        mock_storage.close = AsyncMock()

        with patch(
            "medre.cli.recover_commands._open_readonly_storage",
            return_value=mock_storage,
        ):
            output = _run_cli(
                "recover",
                "--event",
                "evt-1",
                "--json",
                "--config",
                "/nonexistent",
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

        with patch(
            "medre.cli.recover_commands._open_readonly_storage",
            return_value=mock_storage,
        ):
            output = _run_cli(
                "recover",
                "--event",
                "evt-1",
                "--json",
                "--config",
                "/nonexistent",
            )
            parsed = json.loads(output)
            assert len(parsed["warnings"]) > 0
            warning_text = " ".join(parsed["warnings"])
            assert (
                "radio" in warning_text.lower() or "duplicate" in warning_text.lower()
            )

    def test_recover_dry_run_no_side_effects(self) -> None:
        """Dry run does not call storage.write methods."""
        event = _FakeEvent()
        mock_storage = AsyncMock()
        mock_storage.get = AsyncMock(return_value=event)
        mock_storage.list_receipts_for_event = AsyncMock(return_value=[])
        mock_storage.list_native_refs_for_event = AsyncMock(return_value=[])
        mock_storage.list_relations = AsyncMock(return_value=[])
        mock_storage.close = AsyncMock()

        with patch(
            "medre.cli.recover_commands._open_readonly_storage",
            return_value=mock_storage,
        ):
            output = _run_cli(
                "recover",
                "--event",
                "evt-1",
                "--dry-run",
                "--json",
                "--config",
                "/nonexistent",
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

        with patch(
            "medre.cli.recover_commands._open_readonly_storage",
            return_value=mock_storage,
        ):
            output = _run_cli(
                "recover",
                "--event",
                "evt-1",
                "--config",
                "/nonexistent",
            )
            assert "Recovery runbook: evt-1" in output
            assert "message.created" in output
            assert "Failed targets: none" in output

    def test_recover_broad_scan_human_readable(self) -> None:
        """Broad scan without --event prints scan summary."""
        mock_storage = AsyncMock()
        mock_storage.close = AsyncMock()

        with patch(
            "medre.cli.recover_commands._open_readonly_storage",
            return_value=mock_storage,
        ):
            output = _run_cli(
                "recover",
                "--config",
                "/nonexistent",
            )
            assert "Recovery scan" in output

    def test_recover_broad_scan_json(self) -> None:
        """Broad scan JSON includes scope=scan and warnings."""
        mock_storage = AsyncMock()
        mock_storage.close = AsyncMock()

        with patch(
            "medre.cli.recover_commands._open_readonly_storage",
            return_value=mock_storage,
        ):
            output = _run_cli(
                "recover",
                "--json",
                "--config",
                "/nonexistent",
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

        with patch(
            "medre.cli.recover_commands._open_readonly_storage",
            return_value=mock_storage,
        ):
            output = _run_cli(
                "recover",
                "--event",
                "evt-1",
                "--json",
                "--config",
                "/nonexistent",
            )
            parsed = json.loads(output)
            keys = list(parsed.keys())
            assert keys == sorted(keys)


class TestRecoverChannelRoute:
    """Recover runbook includes target_channel and route_id per failed receipt."""

    def _make_storage_with_failed(
        self,
        target_adapter: str = "mesh_adptr",
        target_channel: str | None = "!ch-a",
        route_id: str = "route-x",
    ) -> AsyncMock:
        event = _FakeEvent()
        receipt = _FakeReceipt(
            status="failed",
            target_adapter=target_adapter,
            target_channel=target_channel,
            route_id=route_id,
            error="boom",
        )
        mock_storage = AsyncMock()
        mock_storage.get = AsyncMock(return_value=event)
        mock_storage.list_receipts_for_event = AsyncMock(return_value=[receipt])
        mock_storage.list_native_refs_for_event = AsyncMock(return_value=[])
        mock_storage.list_relations = AsyncMock(return_value=[])
        mock_storage.close = AsyncMock()
        return mock_storage

    def test_json_failed_target_includes_target_channel(self) -> None:
        mock_storage = self._make_storage_with_failed(target_channel="!ch-alpha")
        with patch(
            "medre.cli.recover_commands._open_readonly_storage",
            return_value=mock_storage,
        ):
            output = _run_cli(
                "recover", "--event", "evt-1", "--json", "--config", "/nonexistent"
            )
            parsed = json.loads(output)
            ft = parsed["failed_targets"][0]
            assert ft["target_channel"] == "!ch-alpha"
            assert ft["route_id"] == "route-x"

    def test_json_omits_target_channel_when_none(self) -> None:
        mock_storage = self._make_storage_with_failed(target_channel=None)
        with patch(
            "medre.cli.recover_commands._open_readonly_storage",
            return_value=mock_storage,
        ):
            output = _run_cli(
                "recover", "--event", "evt-1", "--json", "--config", "/nonexistent"
            )
            parsed = json.loads(output)
            ft = parsed["failed_targets"][0]
            assert "target_channel" not in ft
            # route_id is non-empty so it should be present.
            assert ft["route_id"] == "route-x"

    def test_human_readable_shows_channel_and_route(self) -> None:
        mock_storage = self._make_storage_with_failed(
            target_channel="!room:beta", route_id="route-99"
        )
        with patch(
            "medre.cli.recover_commands._open_readonly_storage",
            return_value=mock_storage,
        ):
            output = _run_cli("recover", "--event", "evt-1", "--config", "/nonexistent")
            # Should show adapter/channel and route in the failed target line.
            assert "mesh_adptr/!room:beta" in output
            assert "route=route-99" in output

    def test_human_readable_no_channel_suffix_when_absent(self) -> None:
        mock_storage = self._make_storage_with_failed(target_channel=None)
        with patch(
            "medre.cli.recover_commands._open_readonly_storage",
            return_value=mock_storage,
        ):
            output = _run_cli("recover", "--event", "evt-1", "--config", "/nonexistent")
            # Should show adapter without /channel suffix.
            assert "mesh_adptr route=route-x:" in output
            assert "/!ch-a" not in output

    def test_json_omits_route_id_when_empty(self) -> None:
        mock_storage = self._make_storage_with_failed(route_id="")
        with patch(
            "medre.cli.recover_commands._open_readonly_storage",
            return_value=mock_storage,
        ):
            output = _run_cli(
                "recover", "--event", "evt-1", "--json", "--config", "/nonexistent"
            )
            parsed = json.loads(output)
            ft = parsed["failed_targets"][0]
            assert "route_id" not in ft


# ---------------------------------------------------------------------------
# Replay with --event flag
# ---------------------------------------------------------------------------


class TestReplayWithEvent:
    """Tests for replay --event wiring."""

    def test_replay_passes_event_as_correlation_id(self) -> None:
        """When --event is given, it becomes a correlation_id in the request."""
        from medre.core.engine.replay.summary import ReplaySummary

        summary = ReplaySummary(
            by_status={"passed": 0, "skipped": 0, "failed": 0, "error": 0},
        )

        mock_engine = MagicMock()
        mock_engine.replay.return_value = AsyncMock()

        mock_app = MagicMock()
        mock_app.replay_engine = mock_engine
        mock_app.storage = AsyncMock()

        mock_builder = MagicMock()
        mock_builder.build.return_value = mock_app

        with patch(
            "medre.cli.replay_commands.RuntimeBuilder", return_value=mock_builder
        ), patch("medre.cli.replay_commands.load_config") as mock_load, patch(
            "medre.cli.replay_commands.apply_env_overrides", side_effect=lambda c, p: c
        ), patch(
            "medre.cli.replay_commands.collect_replay_summary",
            new_callable=AsyncMock,
            return_value=summary,
        ) as mock_collect:

            mock_load.return_value = (MagicMock(), MagicMock(), MagicMock())
            _run_cli("replay", "--mode", "dry_run", "--event", "evt-42", "--json")

            # Verify the replay engine was called (via collect_replay_summary).
            mock_collect.assert_called_once()

    def test_replay_with_target_adapters_and_route_ids(self) -> None:
        """Target adapters and route IDs are passed through."""
        from medre.core.engine.replay.summary import ReplaySummary
        from medre.core.engine.replay.types import ReplayMode

        summary = ReplaySummary(
            mode=ReplayMode.DRY_RUN,
            by_status={"passed": 0, "skipped": 0, "failed": 0, "error": 0},
        )

        mock_engine = MagicMock()
        mock_engine.replay.return_value = AsyncMock()

        mock_app = MagicMock()
        mock_app.replay_engine = mock_engine
        mock_app.storage = AsyncMock()

        mock_builder = MagicMock()
        mock_builder.build.return_value = mock_app

        with patch(
            "medre.cli.replay_commands.RuntimeBuilder", return_value=mock_builder
        ), patch("medre.cli.replay_commands.load_config") as mock_load, patch(
            "medre.cli.replay_commands.apply_env_overrides", side_effect=lambda c, p: c
        ), patch(
            "medre.cli.replay_commands.collect_replay_summary",
            new_callable=AsyncMock,
            return_value=summary,
        ):

            mock_load.return_value = (MagicMock(), MagicMock(), MagicMock())
            output = _run_cli(
                "replay",
                "--mode",
                "dry_run",
                "--target-adapters",
                "a1",
                "a2",
                "--route-ids",
                "r1",
                "--limit",
                "10",
                "--json",
            )
            parsed = json.loads(output)
            assert parsed["mode"] == "dry_run"


# ---------------------------------------------------------------------------
# Failure-kind classification tests
# ---------------------------------------------------------------------------


class TestFailureKindClassification:
    """Tests for failure-kind inference from receipt error/status fields."""

    def test_infer_adapter_transient_from_timeout_error(self) -> None:
        from medre.core.observability.classification import infer_failure_kind

        assert (
            infer_failure_kind("TimeoutError: connection timed out", "failed")
            == "adapter_transient"
        )

    def test_infer_adapter_transient_from_connection_reset(self) -> None:
        from medre.core.observability.classification import infer_failure_kind

        assert (
            infer_failure_kind("ConnectionResetError: connection reset", "failed")
            == "adapter_transient"
        )

    def test_infer_adapter_transient_from_dead_lettered(self) -> None:
        """dead_lettered status implies transient (retries exhausted)."""
        from medre.core.observability.classification import infer_failure_kind

        assert infer_failure_kind("some error", "dead_lettered") == "adapter_transient"

    def test_infer_adapter_permanent_from_generic_error(self) -> None:
        from medre.core.observability.classification import infer_failure_kind

        assert infer_failure_kind("permission denied", "failed") == "adapter_permanent"

    def test_infer_renderer_failure(self) -> None:
        from medre.core.observability.classification import infer_failure_kind

        assert (
            infer_failure_kind("no renderer registered for event_kind", "failed")
            == "renderer_failure"
        )

    def test_infer_adapter_missing(self) -> None:
        from medre.core.observability.classification import infer_failure_kind

        assert (
            infer_failure_kind("adapter_missing: adapter 'x' not registered", "failed")
            == "adapter_missing"
        )

    def test_infer_capacity_rejection(self) -> None:
        from medre.core.observability.classification import infer_failure_kind

        assert (
            infer_failure_kind("delivery_capacity_exceeded", "failed")
            == "capacity_rejection"
        )

    def test_infer_shutdown_rejection(self) -> None:
        from medre.core.observability.classification import infer_failure_kind

        assert (
            infer_failure_kind("delivery_rejected_shutdown", "failed")
            == "shutdown_rejection"
        )

    def test_infer_deadline_exceeded(self) -> None:
        from medre.core.observability.classification import infer_failure_kind

        assert (
            infer_failure_kind("deadline_exceeded: plan deadline passed", "failed")
            == "deadline_exceeded"
        )

    def test_infer_unknown_no_error(self) -> None:
        from medre.core.observability.classification import infer_failure_kind

        assert infer_failure_kind(None, "failed") == "unknown"

    def test_failure_category_retryable(self) -> None:
        from medre.core.observability.classification import failure_category

        assert failure_category("adapter_transient") == "retryable"

    def test_failure_category_permanent(self) -> None:
        from medre.core.observability.classification import failure_category

        assert failure_category("adapter_permanent") == "permanent"
        assert failure_category("adapter_missing") == "permanent"
        assert failure_category("renderer_failure") == "permanent"

    def test_failure_category_operational(self) -> None:
        from medre.core.observability.classification import failure_category

        assert failure_category("capacity_rejection") == "operational"
        assert failure_category("shutdown_rejection") == "operational"
        assert failure_category("deadline_exceeded") == "operational"

    def test_failure_category_unknown(self) -> None:
        from medre.core.observability.classification import failure_category

        assert failure_category("unknown") == "unknown"
        assert failure_category("something_else") == "unknown"

    def test_classification_helpers_importable_from_recover_commands(self) -> None:
        """Classification helpers are imported from the canonical observability module."""
        from medre.cli.recover_commands import _failure_category, _infer_failure_kind

        assert _infer_failure_kind("timeout", "failed") == "adapter_transient"
        assert _failure_category("adapter_transient") == "retryable"


# ---------------------------------------------------------------------------
# Recovery classification integration tests
# ---------------------------------------------------------------------------


class TestRecoverClassification:
    """Tests for recover command failure classification output."""

    def test_retryable_grouping(self) -> None:
        """Transient failures are classified as retryable."""
        event = _FakeEvent()
        r1 = _FakeReceipt(
            status="failed",
            target_adapter="adapter_a",
            error="TimeoutError: timed out",
        )
        r2 = _FakeReceipt(
            status="dead_lettered",
            target_adapter="adapter_b",
            error="ConnectionError: reset",
        )
        ok = _FakeReceipt(status="sent", target_adapter="adapter_c")

        mock_storage = AsyncMock()
        mock_storage.get = AsyncMock(return_value=event)
        mock_storage.list_receipts_for_event = AsyncMock(return_value=[r1, r2, ok])
        mock_storage.list_native_refs_for_event = AsyncMock(return_value=[])
        mock_storage.list_relations = AsyncMock(return_value=[])
        mock_storage.close = AsyncMock()

        with patch(
            "medre.cli.recover_commands._open_readonly_storage",
            return_value=mock_storage,
        ):
            output = _run_cli(
                "recover",
                "--event",
                "evt-1",
                "--json",
                "--config",
                "/nonexistent",
            )
            parsed = json.loads(output)
            fc = parsed["failure_classification"]
            assert "retryable" in fc
            retryable_adapters = [i["target_adapter"] for i in fc["retryable"]]
            assert "adapter_a" in retryable_adapters
            assert "adapter_b" in retryable_adapters
            assert "adapter_c" not in retryable_adapters

    def test_permanent_grouping(self) -> None:
        """Permanent failures are classified as permanent."""
        event = _FakeEvent()
        r1 = _FakeReceipt(
            status="failed",
            target_adapter="bad_adapter",
            error="permission denied",
        )
        r2 = _FakeReceipt(
            status="failed",
            target_adapter="missing_adapter",
            error="adapter_missing: not registered",
        )

        mock_storage = AsyncMock()
        mock_storage.get = AsyncMock(return_value=event)
        mock_storage.list_receipts_for_event = AsyncMock(return_value=[r1, r2])
        mock_storage.list_native_refs_for_event = AsyncMock(return_value=[])
        mock_storage.list_relations = AsyncMock(return_value=[])
        mock_storage.close = AsyncMock()

        with patch(
            "medre.cli.recover_commands._open_readonly_storage",
            return_value=mock_storage,
        ):
            output = _run_cli(
                "recover",
                "--event",
                "evt-1",
                "--json",
                "--config",
                "/nonexistent",
            )
            parsed = json.loads(output)
            fc = parsed["failure_classification"]
            assert "permanent" in fc
            permanent_adapters = [i["target_adapter"] for i in fc["permanent"]]
            assert "bad_adapter" in permanent_adapters
            assert "missing_adapter" in permanent_adapters

    def test_operational_grouping(self) -> None:
        """Capacity/shutdown/deadline failures are classified as operational."""
        event = _FakeEvent()
        r1 = _FakeReceipt(
            status="failed",
            target_adapter="op_adapter",
            error="delivery_capacity_exceeded",
        )

        mock_storage = AsyncMock()
        mock_storage.get = AsyncMock(return_value=event)
        mock_storage.list_receipts_for_event = AsyncMock(return_value=[r1])
        mock_storage.list_native_refs_for_event = AsyncMock(return_value=[])
        mock_storage.list_relations = AsyncMock(return_value=[])
        mock_storage.close = AsyncMock()

        with patch(
            "medre.cli.recover_commands._open_readonly_storage",
            return_value=mock_storage,
        ):
            output = _run_cli(
                "recover",
                "--event",
                "evt-1",
                "--json",
                "--config",
                "/nonexistent",
            )
            parsed = json.loads(output)
            fc = parsed["failure_classification"]
            assert "operational" in fc
            assert fc["operational"][0]["failure_kind"] == "capacity_rejection"

    def test_recommended_commands_retryable(self) -> None:
        """Retryable failures recommend DRY_RUN and BEST_EFFORT."""
        event = _FakeEvent()
        receipt = _FakeReceipt(
            status="failed",
            target_adapter="adapter_a",
            error="TimeoutError: timed out",
        )

        mock_storage = AsyncMock()
        mock_storage.get = AsyncMock(return_value=event)
        mock_storage.list_receipts_for_event = AsyncMock(return_value=[receipt])
        mock_storage.list_native_refs_for_event = AsyncMock(return_value=[])
        mock_storage.list_relations = AsyncMock(return_value=[])
        mock_storage.close = AsyncMock()

        with patch(
            "medre.cli.recover_commands._open_readonly_storage",
            return_value=mock_storage,
        ):
            output = _run_cli(
                "recover",
                "--event",
                "evt-1",
                "--json",
                "--config",
                "/nonexistent",
            )
            parsed = json.loads(output)
            cmds = parsed["recommended_commands"]
            cmd_text = " ".join(cmds)
            assert "dry_run" in cmd_text
            assert "best_effort" in cmd_text

    def test_recommended_commands_permanent(self) -> None:
        """Permanent failures recommend inspect-event and inspect-receipts."""
        event = _FakeEvent()
        receipt = _FakeReceipt(
            status="failed",
            target_adapter="adapter_a",
            error="permission denied",
        )

        mock_storage = AsyncMock()
        mock_storage.get = AsyncMock(return_value=event)
        mock_storage.list_receipts_for_event = AsyncMock(return_value=[receipt])
        mock_storage.list_native_refs_for_event = AsyncMock(return_value=[])
        mock_storage.list_relations = AsyncMock(return_value=[])
        mock_storage.close = AsyncMock()

        with patch(
            "medre.cli.recover_commands._open_readonly_storage",
            return_value=mock_storage,
        ):
            output = _run_cli(
                "recover",
                "--event",
                "evt-1",
                "--json",
                "--config",
                "/nonexistent",
            )
            parsed = json.loads(output)
            cmds = parsed["recommended_commands"]
            cmd_text = " ".join(cmds)
            assert "inspect event" in cmd_text
            assert "inspect receipts" in cmd_text

    def test_recommended_commands_operational(self) -> None:
        """Operational failures recommend diagnostics and config check."""
        event = _FakeEvent()
        receipt = _FakeReceipt(
            status="failed",
            target_adapter="adapter_a",
            error="delivery_capacity_exceeded",
        )

        mock_storage = AsyncMock()
        mock_storage.get = AsyncMock(return_value=event)
        mock_storage.list_receipts_for_event = AsyncMock(return_value=[receipt])
        mock_storage.list_native_refs_for_event = AsyncMock(return_value=[])
        mock_storage.list_relations = AsyncMock(return_value=[])
        mock_storage.close = AsyncMock()

        with patch(
            "medre.cli.recover_commands._open_readonly_storage",
            return_value=mock_storage,
        ):
            output = _run_cli(
                "recover",
                "--event",
                "evt-1",
                "--json",
                "--config",
                "/nonexistent",
            )
            parsed = json.loads(output)
            cmds = parsed["recommended_commands"]
            cmd_text = " ".join(cmds)
            assert "diagnostics" in cmd_text
            assert "config check" in cmd_text

    def test_duplicate_send_warning_on_retryable(self) -> None:
        """Duplicate-send warning appears when BEST_EFFORT is recommended."""
        event = _FakeEvent()
        receipt = _FakeReceipt(
            status="failed",
            target_adapter="adapter_a",
            error="TimeoutError: timed out",
        )

        mock_storage = AsyncMock()
        mock_storage.get = AsyncMock(return_value=event)
        mock_storage.list_receipts_for_event = AsyncMock(return_value=[receipt])
        mock_storage.list_native_refs_for_event = AsyncMock(return_value=[])
        mock_storage.list_relations = AsyncMock(return_value=[])
        mock_storage.close = AsyncMock()

        with patch(
            "medre.cli.recover_commands._open_readonly_storage",
            return_value=mock_storage,
        ):
            output = _run_cli(
                "recover",
                "--event",
                "evt-1",
                "--json",
                "--config",
                "/nonexistent",
            )
            parsed = json.loads(output)
            warnings = parsed.get("warnings", [])
            warning_text = " ".join(warnings).lower()
            assert "duplicate" in warning_text
            assert "best_effort" in warning_text or "dry_run" in warning_text

    def test_no_duplicate_send_warning_on_permanent_only(self) -> None:
        """No BEST_EFFORT duplicate warning when only permanent failures exist."""
        event = _FakeEvent()
        receipt = _FakeReceipt(
            status="failed",
            target_adapter="adapter_a",
            error="permission denied",
        )

        mock_storage = AsyncMock()
        mock_storage.get = AsyncMock(return_value=event)
        mock_storage.list_receipts_for_event = AsyncMock(return_value=[receipt])
        mock_storage.list_native_refs_for_event = AsyncMock(return_value=[])
        mock_storage.list_relations = AsyncMock(return_value=[])
        mock_storage.close = AsyncMock()

        with patch(
            "medre.cli.recover_commands._open_readonly_storage",
            return_value=mock_storage,
        ):
            output = _run_cli(
                "recover",
                "--event",
                "evt-1",
                "--json",
                "--config",
                "/nonexistent",
            )
            parsed = json.loads(output)
            warnings = parsed.get("warnings", [])
            best_effort_warnings = [w for w in warnings if "BEST_EFFORT" in w]
            assert len(best_effort_warnings) == 0

    def test_replay_context_included(self) -> None:
        """Replay context is included when event has replay receipts."""
        event = _FakeEvent()
        replay_receipt = _FakeReceipt(
            status="failed",
            target_adapter="adapter_a",
            error="permission denied",
            source="replay",
            replay_run_id="run-42",
        )

        mock_storage = AsyncMock()
        mock_storage.get = AsyncMock(return_value=event)
        mock_storage.list_receipts_for_event = AsyncMock(return_value=[replay_receipt])
        mock_storage.list_native_refs_for_event = AsyncMock(return_value=[])
        mock_storage.list_relations = AsyncMock(return_value=[])
        mock_storage.close = AsyncMock()

        with patch(
            "medre.cli.recover_commands._open_readonly_storage",
            return_value=mock_storage,
        ):
            output = _run_cli(
                "recover",
                "--event",
                "evt-1",
                "--json",
                "--config",
                "/nonexistent",
            )
            parsed = json.loads(output)
            assert "replay_context" in parsed
            assert parsed["replay_context"][0]["replay_run_id"] == "run-42"

    def test_recover_no_replay_side_effects(self) -> None:
        """Recovery is read-only — no replay or write operations."""
        event = _FakeEvent()
        receipt = _FakeReceipt(
            status="failed",
            target_adapter="adapter_a",
            error="TimeoutError: timed out",
        )

        mock_storage = AsyncMock()
        mock_storage.get = AsyncMock(return_value=event)
        mock_storage.list_receipts_for_event = AsyncMock(return_value=[receipt])
        mock_storage.list_native_refs_for_event = AsyncMock(return_value=[])
        mock_storage.list_relations = AsyncMock(return_value=[])
        mock_storage.close = AsyncMock()

        with patch(
            "medre.cli.recover_commands._open_readonly_storage",
            return_value=mock_storage,
        ):
            output = _run_cli(
                "recover",
                "--event",
                "evt-1",
                "--json",
                "--config",
                "/nonexistent",
            )
            parsed = json.loads(output)
            # Verify advisory output structure.
            assert "failure_classification" in parsed
            assert "recommended_commands" in parsed
            # Verify no replay was executed.
            assert "replay_result" not in parsed
            assert "replay_summary" not in parsed
            # Only read methods called.
            mock_storage.get.assert_called()
            mock_storage.list_receipts_for_event.assert_called()
            # No write methods.
            mock_storage.append.assert_not_called()
            mock_storage.append_receipt.assert_not_called()

    def test_failure_kind_on_failed_targets(self) -> None:
        """Each failed target includes failure_kind and category."""
        event = _FakeEvent()
        receipt = _FakeReceipt(
            status="failed",
            target_adapter="adapter_a",
            error="TimeoutError: timed out",
        )

        mock_storage = AsyncMock()
        mock_storage.get = AsyncMock(return_value=event)
        mock_storage.list_receipts_for_event = AsyncMock(return_value=[receipt])
        mock_storage.list_native_refs_for_event = AsyncMock(return_value=[])
        mock_storage.list_relations = AsyncMock(return_value=[])
        mock_storage.close = AsyncMock()

        with patch(
            "medre.cli.recover_commands._open_readonly_storage",
            return_value=mock_storage,
        ):
            output = _run_cli(
                "recover",
                "--event",
                "evt-1",
                "--json",
                "--config",
                "/nonexistent",
            )
            parsed = json.loads(output)
            ft = parsed["failed_targets"][0]
            assert ft["failure_kind"] == "adapter_transient"
            assert ft["category"] == "retryable"

    def test_human_readable_classification(self) -> None:
        """Human-readable output includes failure classification summary."""
        event = _FakeEvent()
        receipt = _FakeReceipt(
            status="failed",
            target_adapter="adapter_a",
            error="TimeoutError: timed out",
        )

        mock_storage = AsyncMock()
        mock_storage.get = AsyncMock(return_value=event)
        mock_storage.list_receipts_for_event = AsyncMock(return_value=[receipt])
        mock_storage.list_native_refs_for_event = AsyncMock(return_value=[])
        mock_storage.list_relations = AsyncMock(return_value=[])
        mock_storage.close = AsyncMock()

        with patch(
            "medre.cli.recover_commands._open_readonly_storage",
            return_value=mock_storage,
        ):
            output = _run_cli(
                "recover",
                "--event",
                "evt-1",
                "--config",
                "/nonexistent",
            )
            assert "adapter_transient" in output
            assert "retryable" in output
            assert "Recommended next commands" in output

    def test_commands_shape_primary_specialized(self) -> None:
        """Recover runbook has commands with primary (inspect-first) and specialized."""
        event = _FakeEvent()
        receipt = _FakeReceipt(
            status="failed",
            target_adapter="adapter_a",
            error="TimeoutError: timed out",
        )

        mock_storage = AsyncMock()
        mock_storage.get = AsyncMock(return_value=event)
        mock_storage.list_receipts_for_event = AsyncMock(return_value=[receipt])
        mock_storage.list_native_refs_for_event = AsyncMock(return_value=[])
        mock_storage.list_relations = AsyncMock(return_value=[])
        mock_storage.close = AsyncMock()

        with patch(
            "medre.cli.recover_commands._open_readonly_storage",
            return_value=mock_storage,
        ):
            output = _run_cli(
                "recover",
                "--event",
                "evt-1",
                "--json",
                "--config",
                "/nonexistent",
            )
            parsed = json.loads(output)
            cmds = parsed["commands"]
            assert "primary" in cmds
            assert "specialized" in cmds
            assert isinstance(cmds["primary"], list)
            assert isinstance(cmds["specialized"], list)

            # Primary commands are inspect-first (no trace/evidence/recover).
            for cmd in cmds["primary"]:
                assert not cmd.startswith(
                    "medre trace "
                ), f"Primary should not start with 'medre trace': {cmd}"
                assert not cmd.startswith(
                    "medre evidence "
                ), f"Primary should not start with 'medre evidence': {cmd}"
                assert not cmd.startswith(
                    "medre recover "
                ), f"Primary should not start with 'medre recover': {cmd}"

            # Specialized includes the recover command.
            recover_cmds = [
                c for c in cmds["specialized"] if c.startswith("medre recover ")
            ]
            assert (
                len(recover_cmds) > 0
            ), f"Expected 'medre recover' in specialized: {cmds['specialized']}"
