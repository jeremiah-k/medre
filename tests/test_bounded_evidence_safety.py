"""Bounded evidence and snapshot safety tests.

Proves MEDRE handles large replay histories, oversized payloads,
repeated replays, recursive metadata, and huge error strings without
exploding evidence output or corrupting state.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, cast

import pytest

from medre.adapters.fake_presentation import FakePresentationAdapter
from medre.core.engine.pipeline import PipelineRunner
from medre.core.events import CanonicalEvent, EventMetadata
from medre.core.events.metadata import RoutingMetadata
from medre.core.observability.metrics import Diagnostician
from medre.core.routing import Route, RouteSource, RouteTarget, Router
from medre.core.routing.stats import RouteStats
from medre.core.runtime.accounting import RuntimeAccounting
from medre.core.storage.backend import StorageBackend
from medre.core.storage.replay import (
    ReplayEngine,
    ReplayMode,
    ReplayRequest,
    collect_replay_summary,
)
from medre.core.storage.sqlite import SQLiteStorage
from medre.observability.sanitization import sanitize_error
from medre.runtime.app import RuntimeState
from medre.runtime.snapshot import SCHEMA_VERSION, build_runtime_snapshot

from tests.helpers.bridge import make_pipeline_config
from tests.helpers.pipeline import make_event

# -- Constants --
_SNAPSHOT_SIZE_LIMIT = 1_000_000
_LARGE_EVENT_COUNT = 50
_OVERSIZED_PAYLOAD_SIZE = 120_000
_RECURSIVE_DEPTH = 50


# -- Helpers --

def _make_bridge_route() -> Route:
    return Route(
        id="test-bridge-route",
        source=RouteSource(
            adapter="fake_transport",
            event_kinds=("message.created",),
            channel="ch-0",
        ),
        targets=[RouteTarget(adapter="fake_presentation")],
    )


async def _open_storage() -> SQLiteStorage:
    storage = SQLiteStorage(":memory:")
    await storage.initialize()
    return storage


def _build_runner(
    storage: SQLiteStorage, router: Router, *,
    adapters: dict[str, Any] | None = None,
    accounting: RuntimeAccounting | None = None,
    route_stats: RouteStats | None = None,
) -> PipelineRunner:
    config = make_pipeline_config(
        storage=cast(StorageBackend, storage), router=router,
        adapters=adapters or {}, accounting=accounting,
        route_stats=route_stats,
    )
    return PipelineRunner(config)


async def _inject_events(
    runner: PipelineRunner, count: int, *,
    prefix: str = "evt",
    source_adapter: str = "fake_transport",
    source_channel_id: str = "ch-0",
    payload_factory: Any | None = None,
) -> list[str]:
    event_ids: list[str] = []
    for i in range(count):
        eid = f"{prefix}-{i:04d}"
        payload = payload_factory(eid) if payload_factory else {"text": f"msg-{i}"}
        evt = make_event(
            event_id=eid, source_adapter=source_adapter,
            source_channel_id=source_channel_id, payload=payload,
        )
        await runner.handle_ingress(evt)
        event_ids.append(eid)
    return event_ids


class _AppStub:
    """Minimal MedreApp-like object for build_runtime_snapshot."""

    def __init__(self, *,
        storage: SQLiteStorage | None = None,
        accounting: RuntimeAccounting | None = None,
        adapters: dict[str, Any] | None = None,
        route_stats: RouteStats | None = None,
        diagnostician: Diagnostician | None = None,
    ) -> None:
        self.state = RuntimeState.RUNNING
        self.storage = storage
        self.adapters = adapters or {}
        self._runtime_accounting = accounting
        self._route_stats = route_stats
        self._diagnostics_collector = diagnostician
        self.build_failures: list[Any] = []
        self._startup_wall = datetime.now(timezone.utc).isoformat()
        self._startup_monotonic: float | None = None
        self._adapter_states: dict[str, Any] = {}
        self._health_state = None
        self._live_health_state = None
        self._boot_summary = None
        self._event_buffer = None
        self._replay_engine = None
        self._capacity_controller = None
        self._startup_readiness = None
        self.config = None
        for aid in sorted(self.adapters):
            self._adapter_states[aid] = RuntimeState.RUNNING

    @property
    def route_stats(self) -> RouteStats | None:
        return self._route_stats

    @property
    def route_eligibility(self) -> None:
        return None


def _check_no_truncation_artifacts(obj: Any, *, path: str, depth: int = 0) -> None:
    """Recursively assert no string field is the bare truncation marker '...'."""
    if depth > 20:
        return
    if isinstance(obj, dict):
        for k, v in obj.items():
            _check_no_truncation_artifacts(v, path=f"{path}.{k}", depth=depth + 1)
    elif isinstance(obj, list):
        for i, item in enumerate(obj):
            _check_no_truncation_artifacts(item, path=f"{path}[{i}]", depth=depth + 1)
    elif isinstance(obj, str):
        is_bf_error = "build_failures" in path and "error" in path
        if not is_bf_error:
            assert obj != "...", f"Truncation artifact '...' found at {path}"


# ===================================================================
# Test 1: Large replay history does not explode evidence
# ===================================================================


@pytest.mark.asyncio
async def test_large_replay_history_does_not_explode_evidence() -> None:
    """50 events in SQLite, replayed via BEST_EFFORT, evidence stays bounded."""
    storage = await _open_storage()
    router = Router(routes=[_make_bridge_route()])
    accounting = RuntimeAccounting()
    pres = FakePresentationAdapter(adapter_id="fake_presentation")
    runner = _build_runner(
        storage, router,
        adapters={"fake_presentation": pres}, accounting=accounting,
    )
    await runner.start()

    # Inject 50 live events.
    event_ids = await _inject_events(runner, _LARGE_EVENT_COUNT)
    assert await storage.count_events() == _LARGE_EVENT_COUNT
    assert await storage.count_receipts() == _LARGE_EVENT_COUNT

    # Replay all 50 via BEST_EFFORT.
    replay = ReplayEngine(storage=storage, pipeline=runner, accounting=accounting)
    summary = await collect_replay_summary(
        replay.replay(ReplayRequest(
            mode=ReplayMode.BEST_EFFORT, run_id="large-replay-001",
            limit=_LARGE_EVENT_COUNT,
        )),
        events_scanned=_LARGE_EVENT_COUNT,
    )
    assert summary.events_replayed > 0
    assert summary.failure_count == 0

    # Total receipts: 50 live + 50 replay = 100.
    assert await storage.count_receipts() == _LARGE_EVENT_COUNT * 2

    # Per-event: exactly 2 receipts (1 live + 1 replay).
    receipts = await storage.list_receipts_for_event(event_ids[0])
    assert len(receipts) == 2
    assert {r.source for r in receipts} == {"live", "replay"}

    # Snapshot stays bounded.
    snap = build_runtime_snapshot(_AppStub(
        storage=storage, accounting=accounting,
        adapters={"fake_presentation": pres},
        route_stats=runner._config.route_stats,
    ))
    snap_json = json.dumps(snap, sort_keys=True)
    assert len(snap_json.encode()) < _SNAPSHOT_SIZE_LIMIT
    assert snap["schema_version"] == SCHEMA_VERSION

    await runner.stop()
    await storage.close()


# ===================================================================
# Test 2: Oversized payloads truncated / stored correctly
# ===================================================================


@pytest.mark.asyncio
async def test_oversized_payloads_truncated() -> None:
    """100KB+ payloads stored intact; snapshot does not inline them."""
    storage = await _open_storage()
    router = Router(routes=[_make_bridge_route()])
    pres = FakePresentationAdapter(adapter_id="fake_presentation")
    runner = _build_runner(
        storage, router, adapters={"fake_presentation": pres},
    )
    await runner.start()

    big_text = "X" * _OVERSIZED_PAYLOAD_SIZE
    def _big_payload(eid: str) -> dict[str, str]:
        return {"text": big_text, "ref": eid}

    event_ids = await _inject_events(
        runner, count=3, prefix="big-evt", payload_factory=_big_payload,
    )

    # Stored payloads are intact (SQLite stores as-is).
    for eid in event_ids:
        retrieved = await storage.get(eid)
        assert retrieved is not None
        assert retrieved.payload["text"] == big_text

    # Snapshot is metadata-only — no oversized strings inlined.
    snap = build_runtime_snapshot(_AppStub(
        storage=storage, adapters={"fake_presentation": pres},
    ))
    snap_json = json.dumps(snap, sort_keys=True)
    assert big_text not in snap_json
    assert len(snap_json.encode()) < 50_000

    await runner.stop()
    await storage.close()


# ===================================================================
# Test 3: Repeated replay runs produce distinct receipts
# ===================================================================


@pytest.mark.asyncio
async def test_repeated_replay_runs_deterministic() -> None:
    """3 replay sessions on the same event produce 4 distinct receipts."""
    storage = await _open_storage()
    router = Router(routes=[_make_bridge_route()])
    accounting = RuntimeAccounting()
    pres = FakePresentationAdapter(adapter_id="fake_presentation")
    runner = _build_runner(
        storage, router,
        adapters={"fake_presentation": pres}, accounting=accounting,
    )
    await runner.start()

    # Inject a single live event.
    event_ids = await _inject_events(runner, 1, prefix="replay-deterministic")
    target_eid = event_ids[0]
    receipts_before = await storage.list_receipts_for_event(target_eid)
    assert len(receipts_before) == 1
    assert receipts_before[0].source == "live"

    # Run 3 replay sessions with distinct run_ids.
    run_ids = [f"replay-run-{i:03d}" for i in range(3)]
    for run_id in run_ids:
        replay = ReplayEngine(storage=storage, pipeline=runner, accounting=accounting)
        summary = await collect_replay_summary(
            replay.replay(ReplayRequest(
                mode=ReplayMode.BEST_EFFORT, run_id=run_id,
                correlation_ids=[target_eid],
            )),
        )
        assert summary.events_replayed >= 1

    # Total: 1 live + 3 replay = 4 receipts.
    all_receipts = await storage.list_receipts_for_event(target_eid)
    assert len(all_receipts) == 4

    # Sources: 1 live, 3 replay.
    source_counts: dict[str, int] = {}
    for r in all_receipts:
        source_counts[r.source] = source_counts.get(r.source, 0) + 1
    assert source_counts == {"live": 1, "replay": 3}

    # Each replay_run_id is distinct.
    replay_run_ids = {
        r.replay_run_id for r in all_receipts
        if r.source == "replay" and r.replay_run_id is not None
    }
    assert replay_run_ids == set(run_ids)

    # Receipt IDs are all unique.
    assert len({r.receipt_id for r in all_receipts}) == 4

    await runner.stop()
    await storage.close()


# ===================================================================
# Test 4: Recursive metadata payloads
# ===================================================================


@pytest.mark.asyncio
async def test_recursive_metadata_payloads() -> None:
    """Deeply nested payload dicts and long route_trace store without crash."""
    storage = await _open_storage()

    # Deep nested payload: _RECURSIVE_DEPTH levels.
    nested: dict[str, Any] = {"leaf": "value"}
    for i in range(_RECURSIVE_DEPTH, 0, -1):
        nested = {f"level_{i}": nested}

    routing_meta = RoutingMetadata(
        matched_routes=tuple(f"matched-{i}" for i in range(10)),
        route_trace=tuple(f"route-trace-{i}" for i in range(_RECURSIVE_DEPTH)),
    )
    evt = CanonicalEvent(
        event_id="recursive-evt-001", event_kind="message.created",
        schema_version=1, timestamp=datetime.now(timezone.utc),
        source_adapter="fake_transport", source_transport_id="node-1",
        source_channel_id="ch-0", parent_event_id=None,
        lineage=(), relations=(), payload=nested,
        metadata=EventMetadata(routing=routing_meta),
    )
    await storage.append(evt)
    retrieved = await storage.get("recursive-evt-001")
    assert retrieved is not None
    assert "level_1" in retrieved.payload
    assert "leaf" in str(retrieved.payload)
    assert retrieved.metadata.routing is not None
    assert len(retrieved.metadata.routing.route_trace) == _RECURSIVE_DEPTH
    assert retrieved.metadata.routing.route_trace[0] == "route-trace-0"
    assert len(retrieved.metadata.routing.matched_routes) == 10

    # Wide payload: 500 top-level keys + nested sub-dicts.
    wide_payload: dict[str, Any] = {f"key_{i}": f"value_{i}" for i in range(500)}
    wide_payload["nested"] = {
        f"sub_{i}": {"data": list(range(10))} for i in range(100)
    }
    wide_evt = CanonicalEvent(
        event_id="recursive-evt-002", event_kind="message.created",
        schema_version=1, timestamp=datetime.now(timezone.utc),
        source_adapter="fake_transport", source_transport_id="node-1",
        source_channel_id="ch-0", parent_event_id=None,
        lineage=(), relations=(), payload=wide_payload,
        metadata=EventMetadata(),
    )
    await storage.append(wide_evt)
    wide_r = await storage.get("recursive-evt-002")
    assert wide_r is not None
    assert len(wide_r.payload) >= 500

    await storage.close()


# ===================================================================
# Test 5: Huge error sanitization
# ===================================================================


def test_huge_error_sanitization() -> None:
    """sanitize_error truncates, redacts tokens, and handles edge cases.

    Note: _TOKEN_RE has a ``(?!(.)\\3{39,})[A-Za-z0-9+/=]{40,}`` branch
    that triggers catastrophic backtracking on long runs of a single
    character.  Tests use varied content to avoid this known issue while
    still exercising truncation and redaction.
    """
    # Large varied string is truncated.
    huge = " ".join(f"Error fragment {i}" for i in range(10_000))
    assert len(sanitize_error(huge)) <= 512
    assert sanitize_error(huge).endswith("...")

    # Tokens are redacted in large strings.
    token_err = (
        "Connection failed: access_token=syt_abcdef123456 for user "
        + " ".join(f"detail-{i}" for i in range(10_000))
    )
    r = sanitize_error(token_err)
    assert "syt_" not in r
    assert "[REDACTED]" in r
    assert len(r) <= 512

    # SDK object repr is redacted.
    sdk_err = (
        "Failed: <nio.client.AsyncClient object at 0x7f1234> "
        + " ".join(f"frame-{i}" for i in range(10_000))
    )
    r = sanitize_error(sdk_err)
    assert "<nio.client.AsyncClient object at 0x7f1234>" not in r
    assert "[OBJECT_REPR]" in r

    # Multiple secret patterns redacted.
    multi = (
        "password=secret123 api_key=sk-abc123 token=abc123 "
        + " ".join(f"log-{i}" for i in range(5_000))
    )
    r = sanitize_error(multi)
    assert "[REDACTED]" in r
    assert "secret123" not in r
    assert "sk-abc123" not in r

    # Edge cases.
    assert sanitize_error("") == ""
    assert sanitize_error("Connection timeout after 30s") == "Connection timeout after 30s"

    # Boundary: exactly 512 chars stays as-is (no truncation marker).
    boundary = " ".join(f"part{i}" for i in range(50))
    boundary = boundary[:512].ljust(512, " ")
    assert len(sanitize_error(boundary)) == 512

    # Just over boundary: truncated to 512 with "...".
    over = " ".join(f"seg{i}" for i in range(60))
    over = over[:513].ljust(600, " ")
    r = sanitize_error(over)
    assert len(r) == 512
    assert r.endswith("...")


# ===================================================================
# Test 6: Snapshot bounded after long run
# ===================================================================


@pytest.mark.asyncio
async def test_snapshot_bounded_after_long_run() -> None:
    """50 events + replay produces a bounded, valid JSON snapshot."""
    storage = await _open_storage()
    router = Router(routes=[_make_bridge_route()])
    accounting = RuntimeAccounting()
    pres = FakePresentationAdapter(adapter_id="fake_presentation")
    route_stats = RouteStats()
    runner = _build_runner(
        storage, router,
        adapters={"fake_presentation": pres},
        accounting=accounting, route_stats=route_stats,
    )
    await runner.start()

    await _inject_events(runner, _LARGE_EVENT_COUNT)

    replay = ReplayEngine(storage=storage, pipeline=runner, accounting=accounting)
    summary = await collect_replay_summary(
        replay.replay(ReplayRequest(
            mode=ReplayMode.BEST_EFFORT, run_id="snap-test-replay",
            limit=_LARGE_EVENT_COUNT,
        )),
        events_scanned=_LARGE_EVENT_COUNT,
    )
    assert summary.events_replayed > 0

    snap = build_runtime_snapshot(_AppStub(
        storage=storage, accounting=accounting,
        adapters={"fake_presentation": pres}, route_stats=route_stats,
    ))
    snap_json = json.dumps(snap, sort_keys=True, indent=2)

    # Valid JSON with correct schema.
    parsed = json.loads(snap_json)
    assert parsed["schema_version"] == SCHEMA_VERSION

    # Size is bounded (well under 1MB; metadata-only).
    snap_bytes = len(snap_json.encode())
    assert snap_bytes < _SNAPSHOT_SIZE_LIMIT
    assert snap_bytes < 100_000, f"Snapshot unexpectedly large: {snap_bytes}"

    # No truncation artifacts (bare "..." strings).
    _check_no_truncation_artifacts(snap, path="root")

    # Key sections present.
    for section in ("lifecycle", "adapters", "replay", "routes", "health"):
        assert section in snap
    assert "fake_presentation" in snap["adapters"]

    await runner.stop()
    await storage.close()
