"""CLI replay-flow tests: dry_run, best_effort, full walkthrough.

Split from the original walkthrough CLI test monolith.
"""

from __future__ import annotations

import asyncio
import io
import json
from contextlib import redirect_stderr, redirect_stdout
from datetime import UTC, datetime
from pathlib import Path

import pytest

from medre.cli import main
from medre.config.model import (
    AdapterConfigSet,
    LoggingConfig,
    MatrixRuntimeConfig,
    MeshtasticRuntimeConfig,
    RuntimeConfig,
    RuntimeOptions,
    StorageConfig,
)
from medre.config.paths import MedrePaths, resolve
from medre.config.routes import RouteConfig, RouteConfigSet
from medre.core.events import CanonicalEvent, EventMetadata
from medre.core.storage.backend import DeliveryOutboxItem
from medre.runtime.builder import RuntimeBuilder
from tests.helpers.fake_runtime import wait_until
from tests.helpers.storage_outbox import make_outbox_item
from tests.helpers.walkthrough import (
    seed_via_smoke_cli,
    smoke_config_path,
    write_replay_config,
    write_sqlite_config_from_example,
)

# ---------------------------------------------------------------------------
# Tests: replay dry_run (config required)
# ---------------------------------------------------------------------------


class TestReplayDryRunCLI:
    """``medre replay --config <cfg> --mode dry_run --event <id> --json``."""

    def test_dry_run_exits_cleanly(self, tmp_path: Path) -> None:
        """DRY_RUN --json exits without error and returns valid JSON."""
        event_id, db_path = seed_via_smoke_cli(tmp_path)
        config_path = write_replay_config(tmp_path, db_path)

        stdout_buf = io.StringIO()
        with redirect_stdout(stdout_buf), redirect_stderr(io.StringIO()):
            main(
                [
                    "replay",
                    "--config",
                    config_path,
                    "--mode",
                    "dry_run",
                    "--event",
                    event_id,
                    "--json",
                ]
            )

        summary = json.loads(stdout_buf.getvalue())
        assert summary["mode"] == "dry_run"
        assert summary["events_scanned"] >= 1
        assert summary["events_replayed"] >= 1

    def test_dry_run_no_side_effects(self, tmp_path: Path) -> None:
        """DRY_RUN does not create replay receipts."""

        from medre.core.storage.sqlite.storage import SQLiteStorage

        event_id, db_path = seed_via_smoke_cli(tmp_path)
        config_path = write_replay_config(tmp_path, db_path)

        stdout_buf = io.StringIO()
        with redirect_stdout(stdout_buf), redirect_stderr(io.StringIO()):
            main(
                [
                    "replay",
                    "--config",
                    config_path,
                    "--mode",
                    "dry_run",
                    "--event",
                    event_id,
                    "--json",
                ]
            )

        async def _check() -> None:
            storage = SQLiteStorage(db_path=str(db_path))
            try:
                await storage.initialize()
                receipts = await storage.list_receipts_for_event(event_id)
                replay_receipts = [r for r in receipts if r.source == "replay"]
                assert len(replay_receipts) == 0, (
                    f"DRY_RUN should not create replay receipts, "
                    f"got {len(replay_receipts)}"
                )
            finally:
                await storage.close()

        asyncio.run(_check())


# ---------------------------------------------------------------------------
# Tests: replay best_effort (config required)
# ---------------------------------------------------------------------------


class TestReplayBestEffortCLI:
    """``medre replay --config <cfg> --mode best_effort --event <id> --json``."""

    def test_best_effort_exits_cleanly(self, tmp_path: Path) -> None:
        """BEST_EFFORT --json exits without error."""
        event_id, db_path = seed_via_smoke_cli(tmp_path)
        config_path = write_replay_config(tmp_path, db_path)

        stdout_buf = io.StringIO()
        with redirect_stdout(stdout_buf), redirect_stderr(io.StringIO()):
            main(
                [
                    "replay",
                    "--config",
                    config_path,
                    "--mode",
                    "best_effort",
                    "--event",
                    event_id,
                    "--json",
                ]
            )

        summary = json.loads(stdout_buf.getvalue())
        assert summary["mode"] == "best_effort"

    def test_best_effort_creates_replay_receipts(self, tmp_path: Path) -> None:
        """BEST_EFFORT replay creates receipts with source='replay'."""

        from medre.core.storage.sqlite.storage import SQLiteStorage

        event_id, db_path = seed_via_smoke_cli(tmp_path)
        config_path = write_replay_config(tmp_path, db_path)

        stdout_buf = io.StringIO()
        with redirect_stdout(stdout_buf), redirect_stderr(io.StringIO()):
            main(
                [
                    "replay",
                    "--config",
                    config_path,
                    "--mode",
                    "best_effort",
                    "--event",
                    event_id,
                    "--json",
                ]
            )

        async def _check() -> None:
            storage = SQLiteStorage(db_path=str(db_path))
            try:
                await storage.initialize()
                receipts = await storage.list_receipts_for_event(event_id)
                replay_receipts = [r for r in receipts if r.source == "replay"]
                assert (
                    len(replay_receipts) >= 1
                ), f"Expected >= 1 replay receipt, got {len(replay_receipts)}"
            finally:
                await storage.close()

        asyncio.run(_check())


# ---------------------------------------------------------------------------
# Tests: best_effort replay must not dispatch unrelated pending/live work
# ---------------------------------------------------------------------------


_SCOPE_YAML = """\
runtime:
  name: alpha-replay-scope
  shutdown_timeout_seconds: 10
logging:
  level: WARNING
  format: text
storage:
  backend: sqlite
  path: '{storage_path}'
retry:
  enabled: true
  interval_seconds: 0.2
adapters:
  matrix:
    fake_matrix:
      enabled: true
      adapter_kind: fake
      homeserver: https://fake.local
      user_id: '@bot:fake.local'
      access_token: fake
      room_allowlist: ['!room:fake.local']
      encryption_mode: plaintext
  meshtastic:
    fake_meshtastic:
      enabled: true
      adapter_kind: fake
      connection_type: fake
      origin_label: operator-workflows
routes:
  mx_to_mesh:
    source_adapters: [fake_matrix]
    dest_adapters: [fake_meshtastic]
    directionality: source_to_dest
    enabled: true
"""


def _write_scope_config(tmp_path: Path, db_path: Path) -> str:
    cfg = tmp_path / "scope_replay_config.yaml"
    cfg.write_text(_SCOPE_YAML.format(storage_path=str(db_path)))
    return str(cfg)


_LIVE_EVENT_ID = "evt-scope-live-pending"


def _nonsel_event(event_id: str) -> CanonicalEvent:
    """A routable, genuinely dispatchable event that replay did NOT select."""
    return CanonicalEvent(
        event_id=event_id,
        event_kind="message.created",
        schema_version=1,
        timestamp=datetime.now(UTC),
        source_adapter="fake_matrix",
        source_transport_id="t-nonsel",
        source_channel_id="!room:fake.local",
        parent_event_id=None,
        lineage=(),
        relations=(),
        payload={"text": "nonselected pending work"},
        metadata=EventMetadata(),
    )


def _scope_runtime_config(db_path: Path) -> RuntimeConfig:
    """Matrix→Meshtastic fake runtime with SQLite storage at *db_path*."""
    return RuntimeConfig(
        runtime=RuntimeOptions(name="replay-scope-test"),
        logging=LoggingConfig(level="WARNING"),
        storage=StorageConfig(backend="sqlite", path=str(db_path)),
        adapters=AdapterConfigSet(
            matrix={
                "fake_matrix": MatrixRuntimeConfig(
                    adapter_id="fake_matrix",
                    enabled=True,
                    adapter_kind="fake",
                ),
            },
            meshtastic={
                "fake_meshtastic": MeshtasticRuntimeConfig(
                    adapter_id="fake_meshtastic",
                    enabled=True,
                    adapter_kind="fake",
                ),
            },
        ),
        routes=RouteConfigSet(
            routes=(
                RouteConfig(
                    route_id="mx-to-mesh",
                    source_adapters=("fake_matrix",),
                    dest_adapters=("fake_meshtastic",),
                ),
            )
        ),
    )


async def _seed_pending_ingress(db_path: Path, event_id: str) -> None:
    """Persist one pending durable-ingress row via the storage authority.

    This is the exact state a crash leaves behind: durably admitted live
    ingress that no worker has routed yet.
    """
    from medre.core.storage.sqlite.storage import SQLiteStorage

    storage = SQLiteStorage(db_path=str(db_path))
    try:
        await storage.initialize()
        result = await storage.admit_ingress(_nonsel_event(event_id), None, "live")
        assert result.created and result.work_status == "pending"
    finally:
        await storage.close()


async def _receipt_count(db_path: Path, event_id: str) -> int:
    from medre.core.storage.sqlite.storage import SQLiteStorage

    storage = SQLiteStorage(db_path=str(db_path))
    try:
        await storage.initialize()
        return len(await storage.list_receipts_for_event(event_id))
    finally:
        await storage.close()


async def _event_present(db_path: Path, event_id: str) -> bool:
    from medre.core.storage.sqlite.storage import SQLiteStorage

    storage = SQLiteStorage(db_path=str(db_path))
    try:
        await storage.initialize()
        return await storage.get(event_id) is not None
    finally:
        await storage.close()


@pytest.fixture()
def tmp_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> MedrePaths:
    monkeypatch.setenv("MEDRE_HOME", str(tmp_path / "medre_home"))
    return resolve()


class TestReplayBestEffortScopeIsolation:
    """best_effort replay executes only the selected replay.

    The replay CLI starts the full runtime lifecycle for side-effect modes.
    That lifecycle must not, as a side effect of starting, dispatch
    unrelated work: due ``pending``/``retry_wait`` outbox rows (retry
    worker) or pending durable ingress rows (ingress worker).  Those rows
    belong to the live runtime's authority, not to the replay execution
    scope.
    """

    def test_best_effort_leaves_unrelated_pending_outbox_undispatched(
        self, tmp_path: Path
    ) -> None:
        """A due pending outbox row for a NONSELECTED event stays pending.

        Consumer-visible contract: after ``medre replay --mode best_effort
        --event <selected>``, the selected event carries replay receipts
        while the unrelated dispatchable row is untouched — no claim, no
        attempt bump, no receipts.
        """

        from medre.core.storage.sqlite.storage import SQLiteStorage

        selected_id, db_path = seed_via_smoke_cli(tmp_path)
        config_path = _write_scope_config(tmp_path, db_path)
        nonsel_id = "evt-nonselected-pending"

        async def _seed() -> str:
            storage = SQLiteStorage(db_path=str(db_path))
            try:
                await storage.initialize()
                await storage.append(_nonsel_event(nonsel_id))
                item = make_outbox_item(
                    delivery_plan_id="plan-nonsel",
                    target_adapter="fake_meshtastic",
                    target_channel=None,
                    status="pending",
                    event_id=nonsel_id,
                )
                created = await storage.create_outbox_item(item)
                return created.outbox_id
            finally:
                await storage.close()

        outbox_id = asyncio.run(_seed())

        stdout_buf = io.StringIO()
        with redirect_stdout(stdout_buf), redirect_stderr(io.StringIO()):
            main(
                [
                    "replay",
                    "--config",
                    config_path,
                    "--mode",
                    "best_effort",
                    "--event",
                    selected_id,
                    "--json",
                ]
            )

        async def _check() -> tuple[DeliveryOutboxItem | None, int]:
            storage = SQLiteStorage(db_path=str(db_path))
            try:
                await storage.initialize()
                row = await storage.get_outbox_item(outbox_id)
                receipts = await storage.list_receipts_for_event(nonsel_id)
                return row, len(receipts)
            finally:
                await storage.close()

        row, receipt_count = asyncio.run(_check())
        assert row is not None, "nonselected outbox row vanished"
        assert row.status == "pending", (
            f"replay dispatched unrelated pending work: status={row.status!r} "
            f"attempt={row.attempt_number}"
        )
        assert row.attempt_number == 1
        assert (
            receipt_count == 0
        ), f"replay appended {receipt_count} receipt(s) to the nonselected event"

        async def _selected_check() -> int:
            storage = SQLiteStorage(db_path=str(db_path))
            try:
                await storage.initialize()
                receipts = await storage.list_receipts_for_event(selected_id)
                return len([r for r in receipts if r.source == "replay"])
            finally:
                await storage.close()

        asyncio.run(_selected_check())

    def test_scoped_start_defers_unrelated_work_without_loss(
        self,
        tmp_paths: MedrePaths,
        tmp_path: Path,
    ) -> None:
        """StartupScope contract at the runtime lifecycle seam.

        LIVE start processes pending durable ingress (pre-existing
        behaviour, asserted here as the live authority).  REPLAY scope
        starts storage/pipeline/adapters for the replay delivery but does
        NOT claim pending durable work: unrelated rows stay pending while
        scoped, live ingress admitted during the scope crosses the durable
        admission boundary (never silently lost), and a normal LIVE start
        afterwards processes both.
        """

        from medre.adapters.fakes.matrix import FakeMatrixAdapter
        from medre.adapters.fakes.meshtastic import FakeMeshtasticAdapter
        from medre.runtime.app import RuntimeState, StartupScope

        async def _scenario() -> None:
            # -- LIVE authority: a normal start processes pending ingress.
            db = tmp_path / "scope_defer.db"
            config = _scope_runtime_config(db)
            app = RuntimeBuilder(config, tmp_paths).build()
            await _seed_pending_ingress(db, _LIVE_EVENT_ID)
            await app.start()
            try:
                assert app._ingress_worker is not None
                beta = app.adapters["fake_meshtastic"]
                assert isinstance(beta, FakeMeshtasticAdapter)
                await wait_until(lambda: len(beta.delivered_payloads) >= 1, timeout=5.0)
            finally:
                await app.stop()
                assert app.state is RuntimeState.STOPPED

            # -- REPLAY scope: unrelated work deferred, nothing lost.
            db2 = tmp_path / "scope_defer2.db"
            config2 = _scope_runtime_config(db2)
            app2 = RuntimeBuilder(config2, tmp_paths).build()
            await _seed_pending_ingress(db2, _LIVE_EVENT_ID)
            await app2.start(scope=StartupScope.REPLAY)
            try:
                assert app2.state is RuntimeState.RUNNING
                assert app2._ingress_worker is None
                alpha = app2.adapters["fake_matrix"]
                assert isinstance(alpha, FakeMatrixAdapter)
                # Live ingress while scoped crosses the durable admission
                # boundary (adapters keep admitting; processing deferred).
                await alpha.simulate_inbound(alpha.make_event("scoped live ingress"))
                await asyncio.sleep(1.5)
                beta2 = app2.adapters["fake_meshtastic"]
                assert isinstance(beta2, FakeMeshtasticAdapter)
                assert (
                    beta2.delivered_payloads == []
                ), "REPLAY scope dispatched unrelated pending work"
                assert (await _receipt_count(db2, _LIVE_EVENT_ID)) == 0
                # The simulated event must be durably admitted (not dropped).
                assert await _event_present(db2, alpha.inbound_events[0].event_id)
            finally:
                await app2.stop()
                assert app2.state is RuntimeState.STOPPED

            # -- A normal LIVE start afterwards processes the deferred work.
            config3 = _scope_runtime_config(db2)
            app3 = RuntimeBuilder(config3, tmp_paths).build()
            await app3.start()
            try:
                beta3 = app3.adapters["fake_meshtastic"]
                assert isinstance(beta3, FakeMeshtasticAdapter)
                await wait_until(
                    lambda: len(beta3.delivered_payloads) >= 2, timeout=5.0
                )
            finally:
                await app3.stop()

        asyncio.run(_scenario())


class TestFullWalkthroughCLI:
    """Full operator workflow: smoke → inspect → inspect flags → replay via main()."""

    def test_full_walkthrough_sequence(self, tmp_path: Path) -> None:
        """Prove the documented operator walkthrough sequence works via main().

        Operator walkthrough (as documented in operator-workflows.md):
        medre smoke --config <sqlite-config> --json  → event_id
        medre inspect receipts --event <id> --storage-path <db>  (inspect-first)
        medre inspect event <id> --timeline --storage-path <db>  (deeper investigation)
                 medre inspect event <id> --evidence --storage-path <db>
        medre replay --config <path> --mode dry_run --event <id> --json    (lower-level)
                 medre replay --config <path> --mode best_effort --event <id> --json
        """
        config_path = smoke_config_path()

        # Optional local smoke seeds persistent DB
        db_path = tmp_path / "full_walkthrough.db"
        config_path = write_sqlite_config_from_example(tmp_path, db_path)
        stdout_buf = io.StringIO()
        with redirect_stdout(stdout_buf), redirect_stderr(io.StringIO()):
            with pytest.raises(SystemExit) as exc_info:
                main(
                    [
                        "smoke",
                        "--config",
                        config_path,
                        "--json",
                    ]
                )
        assert exc_info.value.code == 0
        report = json.loads(stdout_buf.getvalue())
        assert report["status"] == "passed"
        event_id = report["event_id"]

        # Inspect-first — check delivery receipts
        stdout_buf = io.StringIO()
        with redirect_stdout(stdout_buf), redirect_stderr(io.StringIO()):
            main(
                [
                    "inspect",
                    "receipts",
                    "--event",
                    event_id,
                    "--storage-path",
                    str(db_path),
                ]
            )
        assert "sent" in stdout_buf.getvalue()

        # Deeper investigation — inspect event --timeline
        stdout_buf = io.StringIO()
        with redirect_stdout(stdout_buf), redirect_stderr(io.StringIO()):
            main(
                [
                    "inspect",
                    "event",
                    event_id,
                    "--timeline",
                    "--storage-path",
                    str(db_path),
                ]
            )
        result = json.loads(stdout_buf.getvalue())
        assert "event" in result
        assert "timeline" in result
        assert len(result["timeline"]) >= 1
        entry_types = [e.get("entry_type") for e in result["timeline"]]
        assert "receipt" in entry_types

        # Deeper investigation — inspect event --evidence
        stdout_buf = io.StringIO()
        with redirect_stdout(stdout_buf), redirect_stderr(io.StringIO()):
            main(
                [
                    "inspect",
                    "event",
                    event_id,
                    "--evidence",
                    "--storage-path",
                    str(db_path),
                ]
            )
        result = json.loads(stdout_buf.getvalue())
        assert result["evidence"]["status"] in ("partial", "passed")

        # Replay uses config with SQLite pointing at the same DB
        replay_config = write_replay_config(tmp_path, db_path)

        # Replay dry_run (lower-level, specialized)
        stdout_buf = io.StringIO()
        with redirect_stdout(stdout_buf), redirect_stderr(io.StringIO()):
            main(
                [
                    "replay",
                    "--config",
                    replay_config,
                    "--mode",
                    "dry_run",
                    "--event",
                    event_id,
                    "--json",
                ]
            )
        dry_summary = json.loads(stdout_buf.getvalue())
        assert dry_summary["mode"] == "dry_run"
        assert dry_summary["events_scanned"] >= 1

        # Replay best_effort (lower-level, specialized)
        stdout_buf = io.StringIO()
        with redirect_stdout(stdout_buf), redirect_stderr(io.StringIO()):
            main(
                [
                    "replay",
                    "--config",
                    replay_config,
                    "--mode",
                    "best_effort",
                    "--event",
                    event_id,
                    "--json",
                ]
            )
        be_summary = json.loads(stdout_buf.getvalue())
        assert be_summary["mode"] == "best_effort"

    def test_event_id_flows_through_all_commands(self, tmp_path: Path) -> None:
        """Verify the exact event_id from smoke appears in every downstream command (inspect-first path)."""

        # Seed via optional local smoke
        db_path = tmp_path / "event_flow.db"
        config_path = write_sqlite_config_from_example(tmp_path, db_path)
        stdout_buf = io.StringIO()
        with redirect_stdout(stdout_buf), redirect_stderr(io.StringIO()):
            with pytest.raises(SystemExit) as exc_info:
                main(
                    [
                        "smoke",
                        "--config",
                        config_path,
                        "--json",
                    ]
                )
        assert exc_info.value.code == 0
        event_id = json.loads(stdout_buf.getvalue())["event_id"]

        # Inspect-first — receipts
        stdout_buf = io.StringIO()
        with redirect_stdout(stdout_buf), redirect_stderr(io.StringIO()):
            main(
                [
                    "inspect",
                    "receipts",
                    "--event",
                    event_id,
                    "--storage-path",
                    str(db_path),
                ]
            )
        assert event_id in stdout_buf.getvalue()

        # Deeper investigation — inspect event --timeline
        stdout_buf = io.StringIO()
        with redirect_stdout(stdout_buf), redirect_stderr(io.StringIO()):
            main(
                [
                    "inspect",
                    "event",
                    event_id,
                    "--timeline",
                    "--storage-path",
                    str(db_path),
                ]
            )
        result = json.loads(stdout_buf.getvalue())
        assert result["event"]["event_id"] == event_id
        assert len(result["timeline"]) >= 1

        # Deeper investigation — inspect event --evidence
        stdout_buf = io.StringIO()
        with redirect_stdout(stdout_buf), redirect_stderr(io.StringIO()):
            main(
                [
                    "inspect",
                    "event",
                    event_id,
                    "--evidence",
                    "--storage-path",
                    str(db_path),
                ]
            )
        result = json.loads(stdout_buf.getvalue())
        assert (
            result["evidence"]["sections"]["storage"]["data"]["event"]["event_id"]
            == event_id
        )

        # Replay dry_run (lower-level, specialized)
        replay_config = write_replay_config(tmp_path, db_path)
        stdout_buf = io.StringIO()
        with redirect_stdout(stdout_buf), redirect_stderr(io.StringIO()):
            main(
                [
                    "replay",
                    "--config",
                    replay_config,
                    "--mode",
                    "dry_run",
                    "--event",
                    event_id,
                    "--json",
                ]
            )
        dry_summary = json.loads(stdout_buf.getvalue())
        assert dry_summary["events_replayed"] >= 1
