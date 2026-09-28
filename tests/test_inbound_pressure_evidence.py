"""Durable pre-admission pressure evidence: storage, gate outcomes, surfacing.

Covers the aggregate table contract (windowing, retention, restart
survival, no canonical side effects), the gate's outcome mapping
(``rejected`` / ``timed_out`` / ``deferred``), and the operator surfaces
(``medre inspect pressure`` and the evidence bundle's storage section).
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from medre.core.storage.sqlite._pressure import (
    PRESSURE_WINDOW_SECONDS,
    pressure_window_start,
)
from medre.core.storage.sqlite.storage import SQLiteStorage
from medre.core.supervision.accounting import RuntimeAccounting
from medre.core.supervision.capacity import (
    CapacityController,
    InboundAdmissionRejected,
)
from tests.helpers.async_utils import wait_until
from tests.helpers.inbound_gate import FakeAdmitRunner as _FakeRunner
from tests.helpers.inbound_gate import GateAppDouble as _GateApp
from tests.helpers.inbound_gate import LimitsDouble as _Limits


async def _await_pressure_flush(app: _GateApp) -> None:
    """Deterministically drain the single-flight pressure flush task."""
    task = getattr(app, "_pressure_flush_task", None)
    if task is not None:
        await task


pytestmark = pytest.mark.usefixtures("isolated_config_env")


async def _open_storage(tmp_path: Path) -> SQLiteStorage:
    storage = SQLiteStorage(str(tmp_path / "pressure.db"))
    await storage.initialize()
    return storage


class TestPressureStorageContract:
    """Aggregate upsert, windowing, retention, restart survival."""

    async def test_same_window_aggregates(self, tmp_path: Path) -> None:
        storage = await _open_storage(tmp_path)
        try:
            base = 1_800_000_000  # fixed epoch base
            await storage.record_inbound_pressure(
                "radio",
                "rejected",
                unix_seconds=base,
                iso_timestamp="2026-01-01T00:00:00+00:00",
            )
            await storage.record_inbound_pressure(
                "radio",
                "rejected",
                unix_seconds=base + 30,
                iso_timestamp="2026-01-01T00:00:30+00:00",
            )
            rows = await storage.list_inbound_pressure_observations()
            assert len(rows) == 1
            row = rows[0]
            assert row["window_start"] == pressure_window_start(base)
            assert row["count"] == 2
            assert row["first_seen_at"] == "2026-01-01T00:00:00+00:00"
            assert row["last_seen_at"] == "2026-01-01T00:00:30+00:00"
        finally:
            await storage.close()

    async def test_out_of_order_upserts_preserve_observation_bounds(
        self, tmp_path: Path
    ) -> None:
        storage = await _open_storage(tmp_path)
        try:
            base = 1_800_000_000
            await storage.record_inbound_pressure(
                "radio",
                "rejected",
                unix_seconds=base + 30,
            )
            await storage.record_inbound_pressure(
                "radio",
                "rejected",
                unix_seconds=base + 5,
            )
            await storage.record_inbound_pressure(
                "radio",
                "rejected",
                unix_seconds=base + 45,
            )
            rows = await storage.list_inbound_pressure_observations()
            row = rows[0]
            from datetime import datetime, timezone

            assert (
                row["first_seen_at"]
                == datetime.fromtimestamp(base + 5, tz=timezone.utc).isoformat()
            )
            assert (
                row["last_seen_at"]
                == datetime.fromtimestamp(base + 45, tz=timezone.utc).isoformat()
            )
        finally:
            await storage.close()

    async def test_separate_windows_are_separate_rows(self, tmp_path: Path) -> None:
        storage = await _open_storage(tmp_path)
        try:
            base = 1_800_000_000
            await storage.record_inbound_pressure("a", "rejected", unix_seconds=base)
            await storage.record_inbound_pressure(
                "a", "rejected", unix_seconds=base + PRESSURE_WINDOW_SECONDS
            )
            await storage.record_inbound_pressure("a", "deferred", unix_seconds=base)
            rows = await storage.list_inbound_pressure_observations()
            assert len(rows) == 3
            keys = {(r["window_start"], r["source"], r["outcome"]) for r in rows}
            assert keys == {
                (pressure_window_start(base), "a", "rejected"),
                (
                    pressure_window_start(base + PRESSURE_WINDOW_SECONDS),
                    "a",
                    "rejected",
                ),
                (pressure_window_start(base), "a", "deferred"),
            }
        finally:
            await storage.close()

    async def test_source_filter(self, tmp_path: Path) -> None:
        storage = await _open_storage(tmp_path)
        try:
            await storage.record_inbound_pressure("radio", "rejected")
            await storage.record_inbound_pressure("matrix", "deferred")
            rows = await storage.list_inbound_pressure_observations(source="radio")
            assert len(rows) == 1
            assert rows[0]["source"] == "radio"
        finally:
            await storage.close()

    async def test_observations_are_append_only(self, tmp_path: Path) -> None:
        storage = await _open_storage(tmp_path)
        try:
            now = int(time.time())
            ancient = now - 90 * 24 * 3600
            recent = now - 2 * PRESSURE_WINDOW_SECONDS
            # The append-only invariant forbids deletes: even ancient
            # windows persist once written (growth is rate-bounded by
            # windowing, not pruned).
            await storage.record_inbound_pressure("a", "rejected", unix_seconds=ancient)
            await storage.record_inbound_pressure("a", "rejected", unix_seconds=recent)
            rows = await storage.list_inbound_pressure_observations()
            assert [r["window_start"] for r in rows] == sorted(
                [pressure_window_start(ancient), pressure_window_start(recent)]
            )
        finally:
            await storage.close()

    async def test_observations_survive_restart(self, tmp_path: Path) -> None:
        storage = await _open_storage(tmp_path)
        db = str(tmp_path / "pressure.db")
        try:
            await storage.record_inbound_pressure("radio", "timed_out")
            await storage.record_inbound_pressure("radio", "timed_out")
        finally:
            await storage.close()

        reopened = SQLiteStorage(db)
        await reopened.initialize()
        try:
            rows = await reopened.list_inbound_pressure_observations()
            assert len(rows) == 1
            assert rows[0]["count"] == 2
            assert rows[0]["outcome"] == "timed_out"
        finally:
            await reopened.close()

    async def test_no_canonical_side_effects(self, tmp_path: Path) -> None:
        storage = await _open_storage(tmp_path)
        try:
            await storage.record_inbound_pressure("radio", "rejected")
            await storage.record_inbound_pressure("radio", "deferred")
            assert await storage.count_events() == 0
            assert await storage.count_receipts() == 0
        finally:
            await storage.close()

    async def test_invalid_outcome_rejected(self, tmp_path: Path) -> None:
        storage = await _open_storage(tmp_path)
        try:
            with pytest.raises(ValueError, match="invalid pressure outcome"):
                await storage.record_inbound_pressure("radio", "delivered")
        finally:
            await storage.close()


class _RecordingRunner(_FakeRunner):
    """Fake runner whose admissions hold a slot for controlled spans."""


class TestGateOutcomeRecording:
    """The runtime gate maps refusal reasons to durable outcomes."""

    async def _gate_app(
        self, tmp_path: Path, **limits: float
    ) -> tuple[_GateApp, SQLiteStorage]:
        storage = await _open_storage(tmp_path)
        controller = CapacityController(_Limits(**limits))  # type: ignore[arg-type]
        app = _GateApp()
        app.pipeline_runner = _RecordingRunner(delay=0.2)
        app.storage = storage
        app._capacity_controller = controller
        app._runtime_accounting = RuntimeAccounting()
        return app, storage

    async def test_timeout_refusal_records_timed_out(self, tmp_path: Path) -> None:
        app, storage = await self._gate_app(
            tmp_path,
            max_inflight_inbound_admissions=1,
            inbound_admission_timeout_seconds=0.02,
        )
        publish = app._make_publish_inbound("radio")
        try:
            first = asyncio.create_task(publish("event-1"))
            assert await wait_until(
                lambda: app._capacity_controller.inbound_current == 1
            )
            with pytest.raises(InboundAdmissionRejected):
                await publish("event-2")
            await first
            await _await_pressure_flush(app)

            rows = await storage.list_inbound_pressure_observations(source="radio")
            assert len(rows) == 1
            assert rows[0]["outcome"] == "timed_out"
            assert rows[0]["count"] == 1
            assert app._runtime_accounting.snapshot()["capacity_rejections"] == 1
        finally:
            await storage.close()

    async def test_queue_full_refusal_records_rejected(self, tmp_path: Path) -> None:
        app, storage = await self._gate_app(
            tmp_path,
            max_inflight_inbound_admissions=1,
            inbound_admission_timeout_seconds=5.0,
        )
        publish = app._make_publish_inbound("radio")
        try:
            first = asyncio.create_task(publish("event-1"))
            assert await wait_until(
                lambda: app._capacity_controller.inbound_current == 1
            )
            queued = asyncio.create_task(publish("event-2"))
            assert await wait_until(
                lambda: app._capacity_controller.snapshot()["inbound_admission_waiting"]
                == 1
            )
            with pytest.raises(InboundAdmissionRejected):
                await publish("event-3")
            await _await_pressure_flush(app)
            queued.cancel()
            with pytest.raises(asyncio.CancelledError):
                await queued
            await first

            rows = await storage.list_inbound_pressure_observations(source="radio")
            assert [(r["outcome"], r["count"]) for r in rows] == [("rejected", 1)]
        finally:
            await storage.close()

    async def test_cursor_aware_refusal_records_deferred(self, tmp_path: Path) -> None:
        from medre.core.ingress.types import DurableIngressDeferredError

        app, storage = await self._gate_app(
            tmp_path,
            max_inflight_inbound_admissions=1,
            inbound_admission_timeout_seconds=0.02,
        )
        admit = app._make_admit_inbound("matrix")
        try:
            first = asyncio.create_task(admit({"event_id": "ev-1"}, "live"))
            assert await wait_until(
                lambda: app._capacity_controller.inbound_current == 1
            )
            with pytest.raises(DurableIngressDeferredError) as exc_info:
                await admit({"event_id": "ev-2"}, "live")
            await first
            await _await_pressure_flush(app)

            assert exc_info.value.reasons == ("inbound_admission_capacity",)
            rows = await storage.list_inbound_pressure_observations(source="matrix")
            assert [(r["outcome"], r["count"]) for r in rows] == [("deferred", 1)]
        finally:
            await storage.close()

    async def test_recording_failure_does_not_mask_refusal(
        self, tmp_path: Path
    ) -> None:
        app, storage = await self._gate_app(
            tmp_path,
            max_inflight_inbound_admissions=1,
            inbound_admission_timeout_seconds=0.02,
        )

        class _FailingStorage:
            async def record_inbound_pressure(self, *a: object, **k: object) -> None:
                raise RuntimeError("disk full")

        app.storage = _FailingStorage()
        publish = app._make_publish_inbound("radio")
        try:
            first = asyncio.create_task(publish("event-1"))
            assert await wait_until(
                lambda: app._capacity_controller.inbound_current == 1
            )
            # The typed refusal still surfaces; the recording failure is
            # logged, never raised.
            with pytest.raises(InboundAdmissionRejected):
                await publish("event-2")
            await first
            await _await_pressure_flush(app)
        finally:
            await storage.close()


class TestPressureSurfacing:
    """Operator surfaces: CLI inspect and the evidence storage section."""

    def test_inspect_pressure_cli(self, tmp_path: Path) -> None:
        from tests.helpers.cli import _run_cli

        async def _seed() -> None:
            storage = await _open_storage(tmp_path)
            try:
                await storage.record_inbound_pressure("radio", "rejected")
                await storage.record_inbound_pressure("radio", "rejected")
                await storage.record_inbound_pressure("matrix", "deferred")
            finally:
                await storage.close()

        asyncio.run(_seed())
        out = _run_cli(
            "inspect",
            "pressure",
            "--storage-path",
            str(tmp_path / "pressure.db"),
        )
        payload = json.loads(out)
        assert payload["count"] == 2
        by_outcome = {
            (r["source"], r["outcome"]): r["count"] for r in payload["observations"]
        }
        assert by_outcome == {("radio", "rejected"): 2, ("matrix", "deferred"): 1}

    async def test_evidence_storage_section_carries_pressure(
        self, tmp_path: Path
    ) -> None:
        from medre.runtime.evidence._storage_sections import (
            _collect_storage_data_from_backend,
        )

        storage = await _open_storage(tmp_path)
        try:
            await storage.record_inbound_pressure("radio", "timed_out")
            section = await _collect_storage_data_from_backend(
                storage, str(tmp_path / "pressure.db"), None, None
            )
            pressure = section["data"]["inbound_pressure"]
            assert isinstance(pressure, list)
            assert len(pressure) == 1
            assert pressure[0]["outcome"] == "timed_out"
            assert pressure[0]["source"] == "radio"
            assert pressure[0]["count"] == 1
        finally:
            await storage.close()


class TestReviewHardening:
    """Regression coverage for the review-round hardening fixes."""

    async def test_iso_timestamp_derives_from_effective_epoch(
        self, tmp_path: Path
    ) -> None:
        from datetime import datetime, timezone

        storage = await _open_storage(tmp_path)
        try:
            epoch = 1_700_000_013  # mid-window
            await storage.record_inbound_pressure(
                "radio", "rejected", unix_seconds=epoch
            )
            rows = await storage.list_inbound_pressure_observations()
            assert (
                rows[0]["first_seen_at"]
                == datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat()
            )
        finally:
            await storage.close()

    async def test_refusal_window_captured_at_refusal_time(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        storage = await _open_storage(tmp_path)
        captured: list[tuple[int, int]] = []
        observed_at = 1_800_000_013
        monkeypatch.setattr("medre.runtime.app._time.time", lambda: observed_at)

        async def _record(source, outcome, *, count=1, **kwargs):
            captured.append(
                (
                    kwargs.get("unix_seconds", -1),
                    kwargs.get("last_unix_seconds", -1),
                )
            )

        app = _GateApp()
        app.pipeline_runner = _FakeRunner(delay=0.2)
        app.storage = storage
        app._capacity_controller = CapacityController(
            _Limits(
                max_inflight_inbound_admissions=1,
                inbound_admission_timeout_seconds=0.02,
            )
        )
        # Route recording through a capturing stub while storage stays
        # open for lifecycle symmetry.
        original = app.storage
        app.storage = SimpleNamespace(record_inbound_pressure=_record)
        publish = app._make_publish_inbound("radio")
        try:
            first = asyncio.create_task(publish("event-1"))
            assert await wait_until(
                lambda: app._capacity_controller.inbound_current == 1
            )
            with pytest.raises(InboundAdmissionRejected):
                await publish("event-2")
            await first
            await _await_pressure_flush(app)
            assert len(captured) == 1
            assert captured[0] == (observed_at, observed_at)
        finally:
            app.storage = original
            await storage.close()

    async def test_batched_runtime_counts_preserve_first_and_last_seen(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        storage = await _open_storage(tmp_path)
        app = _GateApp()
        app.storage = storage
        observed = iter((1_800_000_005, 1_800_000_042))
        monkeypatch.setattr("medre.runtime.app._time.time", lambda: next(observed))
        refused = SimpleNamespace(reason="queue_full")
        try:
            app._record_inbound_pressure_loss("radio", refused, cursor_aware=False)
            app._record_inbound_pressure_loss("radio", refused, cursor_aware=False)
            await _await_pressure_flush(app)

            rows = await storage.list_inbound_pressure_observations(source="radio")
            assert len(rows) == 1
            row = rows[0]
            from datetime import datetime, timezone

            assert row["count"] == 2
            assert (
                row["first_seen_at"]
                == datetime.fromtimestamp(1_800_000_005, tz=timezone.utc).isoformat()
            )
            assert (
                row["last_seen_at"]
                == datetime.fromtimestamp(1_800_000_042, tz=timezone.utc).isoformat()
            )
        finally:
            await storage.close()

    async def test_flush_drains_counts_arriving_mid_flight(
        self, tmp_path: Path
    ) -> None:
        storage = await _open_storage(tmp_path)
        calls: list[tuple[str, str, int]] = []

        async def _record(source, outcome, *, count=1, **kwargs):
            calls.append((source, outcome, count))
            if len(calls) == 1:
                # A refusal lands while this write is awaited: it must be
                # drained by the SAME single-flight task, not stranded.
                app._pressure_pending[("radio", "rejected", 42)] = (1, 42, 42)

        app = _GateApp()
        app.storage = SimpleNamespace(record_inbound_pressure=_record)
        app._pressure_pending = {("radio", "rejected", 41): (1, 41, 41)}
        await app._flush_inbound_pressure(_record)
        assert sorted(calls) == [
            ("radio", "rejected", 1),
            ("radio", "rejected", 1),
        ]
        await storage.close()

    async def test_failed_flush_retains_batch_for_later_retry(
        self, tmp_path: Path
    ) -> None:
        storage = await _open_storage(tmp_path)
        calls = 0

        async def _record(source, outcome, *, count=1, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError("temporary storage failure")
            await storage.record_inbound_pressure(
                source, outcome, count=count, **kwargs
            )

        app = _GateApp()
        app.storage = SimpleNamespace(record_inbound_pressure=_record)
        app._pressure_pending = {
            ("radio", "rejected", pressure_window_start(1_800_000_005)): (
                2,
                1_800_000_005,
                1_800_000_042,
            )
        }
        try:
            await app._flush_inbound_pressure(_record)
            assert app._pressure_pending

            await app._drain_inbound_pressure_flush()
            assert app._pressure_pending == {}
            rows = await storage.list_inbound_pressure_observations(source="radio")
            assert rows[0]["count"] == 2
        finally:
            await storage.close()

    async def test_cancelled_flush_requeues_unwritten_batch(
        self, tmp_path: Path
    ) -> None:
        storage = await _open_storage(tmp_path)
        started = asyncio.Event()

        async def _blocked_record(source, outcome, *, count=1, **kwargs):
            started.set()
            await asyncio.Future()

        app = _GateApp()
        app.storage = SimpleNamespace(record_inbound_pressure=_blocked_record)
        key = ("radio", "rejected", pressure_window_start(1_800_000_005))
        aggregate = (2, 1_800_000_005, 1_800_000_042)
        app._pressure_pending = {key: aggregate}
        task = asyncio.create_task(app._flush_inbound_pressure(_blocked_record))
        try:
            await started.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert app._pressure_pending == {key: aggregate}
        finally:
            if not task.done():
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            await storage.close()

    async def test_readonly_db_without_table_returns_empty_history(
        self, tmp_path: Path
    ) -> None:
        import sqlite3

        from medre.core.storage.sqlite.storage import SQLiteStorage as _S

        db_path = tmp_path / "legacy.db"
        # Simulate a schema-version-1 database: full current schema with
        # the additive pressure table absent (test-local DROP; src never
        # deletes).
        seed = SQLiteStorage(str(db_path))
        await seed.initialize()
        await seed.close()
        conn = sqlite3.connect(db_path)
        conn.execute("DROP TABLE inbound_pressure_observations")
        conn.commit()
        conn.close()

        legacy = await _S.open_readonly(str(db_path))
        try:
            rows = await legacy.list_inbound_pressure_observations()
            assert rows == []
        finally:
            await legacy.close()

    def test_cli_limit_rejects_zero(self, tmp_path: Path) -> None:
        from tests.helpers.cli import _run_cli

        with pytest.raises(SystemExit):
            _run_cli(
                "inspect",
                "pressure",
                "--storage-path",
                str(tmp_path / "none.db"),
                "--limit",
                "0",
            )
