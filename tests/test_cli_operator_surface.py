"""Focused tests for CLI operator-surface consistency fixes (F-3/F-4/F-5/F-6/F-7/F-8).

Tests cover:
- F-3: Run-session accounting uses authoritative counter name ``inbound_accepted``
- F-4: Suppression reasons visible in recover human output
- F-5: Recover accepts read-only ``--storage-path``
- F-6: Failed targets include ``delivery_plan_id``
- F-7: Replay accepts ``--run-id`` and surfaces it in JSON/human output
- F-8: Inspect native-ref uses ``native_ref_to_report_dict`` helper shape
"""

from __future__ import annotations

import io
import json
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
from pathlib import Path

import pytest

from medre.cli import main
from tests.helpers.cli import _run_cli

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in (
        "MEDRE_HOME",
        "MEDRE_CONFIG",
        "XDG_CONFIG_HOME",
        "XDG_STATE_HOME",
        "XDG_DATA_HOME",
        "XDG_CACHE_HOME",
    ):
        monkeypatch.delenv(var, raising=False)


def _seed_db_with_failed_receipt(
    db_path: str,
    event_id: str = "evt-recover-fail-1",
    source_adapter: str = "test_src",
    target_adapter: str = "test_dest",
    status: str = "failed",
    failure_kind: str = "adapter_permanent",
    error: str | None = None,
    delivery_plan_id: str | None = None,
) -> None:
    """Seed a DB with an event and a failed receipt for recover testing."""
    import asyncio

    from medre.core.events import (
        CanonicalEvent,
        DeliveryReceipt,
        EventMetadata,
    )
    from medre.core.storage.sqlite.storage import SQLiteStorage

    async def _seed() -> None:
        storage = SQLiteStorage(db_path)
        try:
            await storage.initialize()
            event = CanonicalEvent(
                event_id=event_id,
                event_kind="message.created",
                schema_version=1,
                timestamp=datetime(2026, 3, 1, 12, 0, 0, tzinfo=timezone.utc),
                source_adapter=source_adapter,
                source_transport_id="test-transport",
                source_channel_id="ch-test",
                parent_event_id=None,
                lineage=(),
                relations=(),
                payload={"text": "recover test"},
                metadata=EventMetadata(),
            )
            await storage.append(event)

            receipt_kwargs: dict = dict(
                sequence=1,
                receipt_id="rcpt-fail-1",
                event_id=event_id,
                delivery_plan_id=delivery_plan_id or "plan-recover-1",
                target_adapter=target_adapter,
                route_id="route-recover",
                status=status,
                failure_kind=failure_kind,
                error=error,
                created_at=datetime(2026, 3, 1, 12, 0, 1, tzinfo=timezone.utc),
            )
            await storage.append_receipt(DeliveryReceipt(**receipt_kwargs))
        finally:
            await storage.close()

    asyncio.run(_seed())


def _seed_db_with_suppressed_receipt(
    db_path: str,
    event_id: str = "evt-suppressed-1",
) -> None:
    """Seed a DB with an event and a failed receipt with capability_suppressed error."""
    import asyncio

    from medre.core.events import (
        CanonicalEvent,
        DeliveryReceipt,
        EventMetadata,
    )
    from medre.core.storage.sqlite.storage import SQLiteStorage

    async def _seed() -> None:
        storage = SQLiteStorage(db_path)
        try:
            await storage.initialize()
            event = CanonicalEvent(
                event_id=event_id,
                event_kind="message.created",
                schema_version=1,
                timestamp=datetime(2026, 4, 1, 12, 0, 0, tzinfo=timezone.utc),
                source_adapter="test_src",
                source_transport_id="test-transport",
                source_channel_id="ch-test",
                parent_event_id=None,
                lineage=(),
                relations=(),
                payload={"text": "suppressed test"},
                metadata=EventMetadata(),
            )
            await storage.append(event)
            # Use status="failed" (recover processes failed/dead_lettered)
            # with a capability_suppressed error pattern so the suppression
            # reason derivation fires.
            await storage.append_receipt(
                DeliveryReceipt(
                    sequence=1,
                    receipt_id="rcpt-suppressed-1",
                    event_id=event_id,
                    delivery_plan_id="plan-suppressed-1",
                    target_adapter="dest_adapter",
                    route_id="route-suppressed",
                    status="failed",
                    failure_kind="capability_suppressed",
                    error="capability_suppressed: file_relation unsupported",
                    created_at=datetime(2026, 4, 1, 12, 0, 1, tzinfo=timezone.utc),
                )
            )
        finally:
            await storage.close()

    asyncio.run(_seed())


def _write_inspect_config(tmp_path: Path, db_path: Path) -> Path:
    """Write a minimal TOML config pointing storage at db_path."""
    cfg = tmp_path / "config.toml"
    cfg.write_text(f"""\
[runtime]
name = "test-operator-surface"

[storage]
backend = "sqlite"
path = {str(db_path)!r}
""")
    return cfg


# ---------------------------------------------------------------------------
# F-3: Run-session accounting uses authoritative counter name
# ---------------------------------------------------------------------------


class TestF3RunSessionAccountingCounter:
    """``_run_session`` human output uses ``inbound_accepted`` (not ``inbound``)."""

    def test_run_session_accounting_key_inbound_accepted(self) -> None:
        """Verify the source code uses inbound_accepted not inbound."""
        import inspect

        from medre.cli.smoke_commands import _run_session

        source = inspect.getsource(_run_session)
        # The authoritative key is 'inbound_accepted', not 'inbound'.
        assert (
            "acc.get('inbound_accepted'" in source
        ), "Expected 'inbound_accepted' key in _run_session accounting"
        # The old buggy key should NOT be present.
        assert (
            "acc.get('inbound'" not in source
        ), "Old buggy 'inbound' key should not appear in _run_session"


# ---------------------------------------------------------------------------
# F-4: Suppression reasons visible in recover human output
# ---------------------------------------------------------------------------


class TestF4SuppressionReasonInRecover:
    """Recover human-readable output surfaces suppression_reason."""

    def test_suppressed_receipt_shows_reason_in_human_output(
        self,
        tmp_path: Path,
    ) -> None:
        db_path = tmp_path / "recover_suppressed.db"
        _seed_db_with_suppressed_receipt(str(db_path))
        cfg = _write_inspect_config(tmp_path, db_path)

        stdout_buf = io.StringIO()
        stderr_buf = io.StringIO()
        with redirect_stdout(stdout_buf), redirect_stderr(stderr_buf):
            main(
                [
                    "recover",
                    "--config",
                    str(cfg),
                    "--event",
                    "evt-suppressed-1",
                ]
            )

        output = stdout_buf.getvalue()
        assert (
            "suppressed:" in output
        ), f"Expected 'suppressed:' in recover output, got:\n{output}"

    def test_suppressed_receipt_includes_suppression_reason_json(
        self,
        tmp_path: Path,
    ) -> None:
        db_path = tmp_path / "recover_suppressed_json.db"
        _seed_db_with_suppressed_receipt(str(db_path))
        cfg = _write_inspect_config(tmp_path, db_path)

        stdout_buf = io.StringIO()
        with redirect_stdout(stdout_buf), redirect_stderr(io.StringIO()):
            main(
                [
                    "recover",
                    "--config",
                    str(cfg),
                    "--event",
                    "evt-suppressed-1",
                    "--json",
                ]
            )

        report = json.loads(stdout_buf.getvalue())
        failed = report["failed_targets"]
        assert len(failed) >= 1
        assert (
            "suppression_reason" in failed[0]
        ), f"Expected 'suppression_reason' in failed target, keys: {sorted(failed[0].keys())}"


# ---------------------------------------------------------------------------
# F-5: Recover accepts read-only --storage-path
# ---------------------------------------------------------------------------


class TestF5RecoverStoragePath:
    """``medre recover --storage-path`` works without --config."""

    def test_recover_storage_path_json(
        self,
        tmp_path: Path,
    ) -> None:
        db_path = tmp_path / "recover_sp.db"
        _seed_db_with_failed_receipt(str(db_path))

        stdout_buf = io.StringIO()
        with redirect_stdout(stdout_buf), redirect_stderr(io.StringIO()):
            main(
                [
                    "recover",
                    "--storage-path",
                    str(db_path),
                    "--event",
                    "evt-recover-fail-1",
                    "--json",
                ]
            )

        report = json.loads(stdout_buf.getvalue())
        assert report["scope"] == "event"
        assert report["event_id"] == "evt-recover-fail-1"

    def test_recover_storage_path_human(
        self,
        tmp_path: Path,
    ) -> None:
        db_path = tmp_path / "recover_sp_human.db"
        _seed_db_with_failed_receipt(str(db_path))

        stdout_buf = io.StringIO()
        with redirect_stdout(stdout_buf), redirect_stderr(io.StringIO()):
            main(
                [
                    "recover",
                    "--storage-path",
                    str(db_path),
                    "--event",
                    "evt-recover-fail-1",
                ]
            )

        output = stdout_buf.getvalue()
        assert "Recovery runbook:" in output

    def test_recover_config_and_storage_path_exclusive(self) -> None:
        """--config and --storage-path are mutually exclusive."""
        stderr_buf = io.StringIO()
        with redirect_stderr(stderr_buf):
            with pytest.raises(SystemExit) as exc_info:
                main(
                    [
                        "recover",
                        "--config",
                        "/dev/null",
                        "--storage-path",
                        "/tmp/test.db",
                        "--event",
                        "evt-1",
                    ]
                )
        assert exc_info.value.code == 2


# ---------------------------------------------------------------------------
# F-6: Failed targets include delivery_plan_id
# ---------------------------------------------------------------------------


class TestF6FailedTargetsDeliveryPlanId:
    """Recover failed_targets entries include ``delivery_plan_id``."""

    def test_failed_target_has_delivery_plan_id_json(
        self,
        tmp_path: Path,
    ) -> None:
        db_path = tmp_path / "recover_planid.db"
        _seed_db_with_failed_receipt(
            str(db_path),
            delivery_plan_id="plan-op-surface-1",
        )
        cfg = _write_inspect_config(tmp_path, db_path)

        stdout_buf = io.StringIO()
        with redirect_stdout(stdout_buf), redirect_stderr(io.StringIO()):
            main(
                [
                    "recover",
                    "--config",
                    str(cfg),
                    "--event",
                    "evt-recover-fail-1",
                    "--json",
                ]
            )

        report = json.loads(stdout_buf.getvalue())
        failed = report["failed_targets"]
        assert len(failed) >= 1
        assert (
            failed[0]["delivery_plan_id"] == "plan-op-surface-1"
        ), f"Expected delivery_plan_id='plan-op-surface-1', got {failed[0].get('delivery_plan_id')}"


# ---------------------------------------------------------------------------
# F-7: Replay --run-id accepted and surfaced
# ---------------------------------------------------------------------------


_REPLAY_TOML = """\
[runtime]
name = "cli-replay-run-id"

[logging]
level = "WARNING"
format = "text"

[storage]
backend = "sqlite"
path = {storage_path!r}

[adapters.matrix.fake_matrix]
enabled = true
adapter_kind = "fake"
homeserver = "https://fake.local"
user_id = "@bot:fake.local"
access_token = "fake"
room_allowlist = ["!room:fake.local"]
encryption_mode = "plaintext"

[adapters.meshtastic.fake_meshtastic]
enabled = true
adapter_kind = "fake"
connection_type = "fake"
meshnet_name = "replay-run-id"

[routes.mx_to_mesh]
source_adapters = ["fake_matrix"]
dest_adapters = ["fake_meshtastic"]
directionality = "source_to_dest"
enabled = true
"""


def _seed_replay_db(tmp_path: Path) -> tuple[str, Path]:
    """Seed a DB via smoke and return (event_id, db_path)."""
    db_path = tmp_path / "replay_runid.db"
    cfg = tmp_path / "replay.toml"
    cfg.write_text(_REPLAY_TOML.format(storage_path=str(db_path)))

    stdout_buf = io.StringIO()
    stderr_buf = io.StringIO()
    with redirect_stdout(stdout_buf), redirect_stderr(stderr_buf):
        with pytest.raises(SystemExit) as exc_info:
            main(
                [
                    "smoke",
                    "--config",
                    str(cfg),
                    "--storage-path",
                    str(db_path),
                    "--json",
                ]
            )
    assert exc_info.value.code == 0
    report = json.loads(stdout_buf.getvalue())
    return report["event_id"], db_path


def _write_replay_config(tmp_path: Path, db_path: Path) -> Path:
    cfg = tmp_path / "replay.toml"
    cfg.write_text(_REPLAY_TOML.format(storage_path=str(db_path)))
    return cfg


class TestF7ReplayRunId:
    """``medre replay --run-id`` is accepted and surfaced in output."""

    def test_run_id_in_json_output(
        self,
        tmp_path: Path,
    ) -> None:
        event_id, db_path = _seed_replay_db(tmp_path)
        config_path = _write_replay_config(tmp_path, db_path)

        stdout_buf = io.StringIO()
        with redirect_stdout(stdout_buf), redirect_stderr(io.StringIO()):
            main(
                [
                    "replay",
                    "--config",
                    str(config_path),
                    "--mode",
                    "dry_run",
                    "--event",
                    event_id,
                    "--run-id",
                    "run-op-surface-42",
                    "--json",
                ]
            )

        summary = json.loads(stdout_buf.getvalue())
        assert (
            summary["run_id"] == "run-op-surface-42"
        ), f"Expected run_id='run-op-surface-42', got {summary.get('run_id')!r}"

    def test_run_id_in_human_output(
        self,
        tmp_path: Path,
    ) -> None:
        event_id, db_path = _seed_replay_db(tmp_path)
        config_path = _write_replay_config(tmp_path, db_path)

        stdout_buf = io.StringIO()
        stderr_buf = io.StringIO()
        with redirect_stdout(stdout_buf), redirect_stderr(stderr_buf):
            main(
                [
                    "replay",
                    "--config",
                    str(config_path),
                    "--mode",
                    "dry_run",
                    "--event",
                    event_id,
                    "--run-id",
                    "run-human-99",
                ]
            )

        output = stdout_buf.getvalue()
        assert "Run ID:" in output, f"Expected 'Run ID:' in output, got:\n{output}"
        assert "run-human-99" in output

    def test_run_id_default_empty_string(
        self,
        tmp_path: Path,
    ) -> None:
        event_id, db_path = _seed_replay_db(tmp_path)
        config_path = _write_replay_config(tmp_path, db_path)

        stdout_buf = io.StringIO()
        with redirect_stdout(stdout_buf), redirect_stderr(io.StringIO()):
            main(
                [
                    "replay",
                    "--config",
                    str(config_path),
                    "--mode",
                    "dry_run",
                    "--event",
                    event_id,
                    "--json",
                ]
            )

        summary = json.loads(stdout_buf.getvalue())
        assert summary["run_id"] == ""

    def test_run_id_in_best_effort_receipts(
        self,
        tmp_path: Path,
    ) -> None:
        import asyncio

        from medre.core.storage.sqlite.storage import SQLiteStorage

        event_id, db_path = _seed_replay_db(tmp_path)
        config_path = _write_replay_config(tmp_path, db_path)

        stdout_buf = io.StringIO()
        with redirect_stdout(stdout_buf), redirect_stderr(io.StringIO()):
            main(
                [
                    "replay",
                    "--config",
                    str(config_path),
                    "--mode",
                    "best_effort",
                    "--event",
                    event_id,
                    "--run-id",
                    "run-receipt-check",
                    "--json",
                ]
            )

        async def _check() -> None:
            storage = SQLiteStorage(db_path=str(db_path))
            try:
                await storage.initialize()
                receipts = await storage.list_receipts_for_event(event_id)
                replay_receipts = [r for r in receipts if r.source == "replay"]
                assert len(replay_receipts) >= 1
                for r in replay_receipts:
                    assert (
                        r.replay_run_id == "run-receipt-check"
                    ), f"Expected replay_run_id='run-receipt-check', got {r.replay_run_id!r}"
            finally:
                await storage.close()

        asyncio.run(_check())


# ---------------------------------------------------------------------------
# F-8: Inspect native-ref uses reporting helper shape
# ---------------------------------------------------------------------------


def _seed_db_with_native_ref(
    db_path: str,
    event_id: str = "evt-nref-shape-1",
    native_adapter: str = "matrix",
    native_channel_id: str = "!room:shape.test",
    native_message_id: str = "$msg-shape-1",
    direction: str = "outbound",
) -> None:
    """Seed a DB with an event and native ref for shape testing."""
    import asyncio

    from medre.core.events import (
        CanonicalEvent,
        EventMetadata,
        NativeMessageRef,
    )
    from medre.core.storage.sqlite.storage import SQLiteStorage

    async def _seed() -> None:
        storage = SQLiteStorage(db_path)
        try:
            await storage.initialize()
            event = CanonicalEvent(
                event_id=event_id,
                event_kind="message.created",
                schema_version=1,
                timestamp=datetime(2026, 5, 1, 12, 0, 0, tzinfo=timezone.utc),
                source_adapter="test_src",
                source_transport_id="test-transport",
                source_channel_id="ch-test",
                parent_event_id=None,
                lineage=(),
                relations=(),
                payload={"text": "native ref shape test"},
                metadata=EventMetadata(),
            )
            await storage.append(event)
            await storage.store_native_ref(
                NativeMessageRef(
                    id="nref-shape-1",
                    event_id=event_id,
                    adapter=native_adapter,
                    native_channel_id=native_channel_id,
                    native_message_id=native_message_id,
                    native_thread_id=None,
                    native_relation_id=None,
                    direction=direction,
                    created_at=datetime(2026, 5, 1, 12, 0, 0, tzinfo=timezone.utc),
                )
            )
        finally:
            await storage.close()

    asyncio.run(_seed())


class TestF8InspectNativeRefReportingShape:
    """``inspect native-ref`` output uses ``native_ref_to_report_dict`` keys."""

    def test_output_has_reporting_helper_keys(
        self,
        tmp_path: Path,
    ) -> None:
        db_path = tmp_path / "nref_shape.db"
        _seed_db_with_native_ref(str(db_path))
        cfg = _write_inspect_config(tmp_path, db_path)

        output = _run_cli(
            "inspect",
            "native-ref",
            "--adapter",
            "matrix",
            "--channel",
            "!room:shape.test",
            "--message",
            "$msg-shape-1",
            "--config",
            str(cfg),
        )

        parsed = json.loads(output)
        # Keys produced by native_ref_to_report_dict
        assert "adapter" in parsed
        assert "native_channel_id" in parsed
        assert "native_message_id" in parsed
        assert "resolves_to" in parsed
        assert "channel" in parsed
        assert "native_id" in parsed
        # Full event should still be present
        assert "event" in parsed
        assert parsed["event"]["event_id"] == "evt-nref-shape-1"

    def test_resolves_to_equals_event_id(
        self,
        tmp_path: Path,
    ) -> None:
        db_path = tmp_path / "nref_resolve.db"
        _seed_db_with_native_ref(str(db_path))
        cfg = _write_inspect_config(tmp_path, db_path)

        output = _run_cli(
            "inspect",
            "native-ref",
            "--adapter",
            "matrix",
            "--channel",
            "!room:shape.test",
            "--message",
            "$msg-shape-1",
            "--config",
            str(cfg),
        )

        parsed = json.loads(output)
        assert parsed["resolves_to"] == "evt-nref-shape-1"

    def test_output_is_deterministic(
        self,
        tmp_path: Path,
    ) -> None:
        db_path = tmp_path / "nref_det.db"
        _seed_db_with_native_ref(str(db_path))
        cfg = _write_inspect_config(tmp_path, db_path)

        output = _run_cli(
            "inspect",
            "native-ref",
            "--adapter",
            "matrix",
            "--channel",
            "!room:shape.test",
            "--message",
            "$msg-shape-1",
            "--config",
            str(cfg),
        )

        parsed = json.loads(output)
        keys = list(parsed.keys())
        assert keys == sorted(keys), f"Keys not sorted: {keys}"

    def test_native_ref_with_storage_path(
        self,
        tmp_path: Path,
    ) -> None:
        db_path = tmp_path / "nref_sp.db"
        _seed_db_with_native_ref(str(db_path))

        output = _run_cli(
            "inspect",
            "native-ref",
            "--adapter",
            "matrix",
            "--channel",
            "!room:shape.test",
            "--message",
            "$msg-shape-1",
            "--storage-path",
            str(db_path),
        )

        parsed = json.loads(output)
        assert parsed["resolves_to"] == "evt-nref-shape-1"
        assert "adapter" in parsed
