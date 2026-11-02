# medre-test-tier: compatibility
"""Fake-provider routing/evidence invariants under live retention collection.

PR E's fake-provider sweep: the enabled relay-only runtime with the fake
adapter set delivers a mixed workload of ordinary, reply-relating,
attachment-bearing, and bodyless events through the real durable ingress,
dispatch, and terminal-settlement path while the real collection worker
sweeps concurrently. Collection must preserve routing facts, delivery
evidence, native references, and conversation identity exactly, leave
borrowed-quote and attachment owners retained, and project plausible
content-free counters into the diagnostic snapshot and runtime snapshot.

The seven-adapter cross-repo qualification runs automatically in the core
PR's adapter CI matrix (independent adapter packages install their own
fakes); it is CI-covered and deliberately not duplicated here. Native-SDK
and hardware tiers are out of scope for this slice.

Plan invariants: 1 (durable owners), 5 (no identity rewrite), 9
(content-free observability).
"""

from __future__ import annotations

import json
from dataclasses import replace as _dc_replace
from datetime import datetime, timezone
from typing import Any

import msgspec
import pytest

from medre.core.events.metadata import RoutingMetadata

from medre.config.model import AdapterConfigSet
from medre.config.paths import MedrePaths
from medre.core.events import EventRelation
from medre.core.routing.models import Route, RouteSource, RouteTarget
from medre.runtime.app import RuntimeState
from medre.runtime.snapshot import build_runtime_snapshot
from tests.helpers.async_utils import wait_until
from tests.helpers.fake_runtime import (
    build_and_start,
    clean_stop,
    make_multi_adapter_config,
)

_ROUTE_ID = "mx-to-mx-dst"
_QUOTE = "OPERATORS-QUOTE-SECRET"


def _config(db_path: str) -> Any:
    base = make_multi_adapter_config()
    groups = dict(base.adapters.groups())
    matrix = dict(groups["matrix"])
    matrix["fake_matrix_dst"] = type(matrix["fake_matrix"])(
        adapter_id="fake_matrix_dst", enabled=True, adapter_kind="fake"
    )
    groups["matrix"] = matrix
    return _dc_replace(
        base,
        storage=_storage(db_path),
        adapters=AdapterConfigSet(groups),
    )


def _storage(db_path: str) -> Any:
    from medre.config.model import StorageConfig

    return StorageConfig(backend="sqlite", path=db_path)


@pytest.fixture()
def tmp_paths(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> MedrePaths:
    from medre.config.paths import resolve

    for var in ("MEDRE_HOME", "XDG_CONFIG_HOME", "XDG_STATE_HOME", "XDG_DATA_HOME"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("MEDRE_HOME", str(tmp_path))
    return resolve()


async def test_mixed_workload_collection_preserves_routing_and_evidence(
    tmp_paths: MedrePaths,
) -> None:
    """The relay-only runtime collects the collectible share of a mixed fake
    workload and preserves every immutable fact of the collected events."""
    import asyncio

    from medre.config.model import ContentRetentionConfig, RuntimeOptions

    db_path = str(tmp_paths.state_dir / "operators.sqlite")
    config = _dc_replace(
        _config(db_path),
        content_retention=ContentRetentionConfig(
            enabled=True, mode="relay-only", interval_seconds=1, batch_limit=10
        ),
        runtime=RuntimeOptions(name="operators-sweep", shutdown_timeout_seconds=5),
    )
    app = await build_and_start(config, tmp_paths)
    collect_gate = asyncio.Event()
    try:
        assert app.storage is not None
        mx = app.adapters["fake_matrix"]
        dst = app.adapters["fake_matrix_dst"]
        app.router.add_route(
            Route(
                id=_ROUTE_ID,
                source=RouteSource("fake_matrix", ("message.created",), None),
                targets=[RouteTarget("fake_matrix_dst")],
            )
        )

        # Hold collection until the workload exists and its immutable facts
        # are captured, so the before/after comparison is deterministic.
        original = app.storage.collect_event_content

        async def gated(
            event_id: str,
            *,
            dry_run: bool = True,
            retention_epoch: int | None = None,
        ) -> dict[str, Any]:
            await collect_gate.wait()
            return await original(
                event_id, dry_run=dry_run, retention_epoch=retention_epoch
            )

        app.storage.collect_event_content = gated  # type: ignore[method-assign]

        def _routed(event: Any) -> Any:
            """Pin the admission-time routing fact the collector must keep."""
            return msgspec.structs.replace(
                event,
                metadata=msgspec.structs.replace(
                    event.metadata,
                    routing=RoutingMetadata(matched_routes=(_ROUTE_ID,)),
                ),
            )

        async def _deliver(event: Any) -> None:
            await mx.simulate_inbound(event)
            assert await wait_until(
                lambda: any(
                    payload.event_id == event.event_id
                    for payload in dst.delivered_payloads
                ),
                timeout=10.0,
            )

        # Mixed workload: plain text, a reply quoting plain-a, a bodyless
        # event, and an attachment-bearing event introduced at the storage
        # boundary (the attachment family blocks wholesale).
        plain_a = _routed(mx.make_event(text="plain-a body"))
        plain_b = _routed(mx.make_event(text="plain-b body"))
        bodyless = _routed(
            msgspec.structs.replace(mx.make_event(text="ignored"), payload={})
        )
        for event in (plain_a, plain_b, bodyless):
            await _deliver(event)
        reply = _routed(mx.make_event(text="reply body"))
        relation = EventRelation("reply", plain_a.event_id, None, None, _QUOTE)
        reply = msgspec.structs.replace(reply, relations=(relation,))
        await _deliver(reply)
        attach = _routed(mx.make_event(text="attachment body"))
        await _deliver(attach)
        shared_ref = "sha256:" + "d" * 64
        await app.storage._write(
            "INSERT INTO attachment_blobs "
            "(content_ref,size_bytes,media_kind,mime_type,created_at,data) "
            "VALUES (?, 4, NULL, NULL, '2026-10-31T00:00:00+00:00', ?)",
            (shared_ref, b"blob"),
        )
        await app.storage._write(
            "INSERT INTO event_attachment_associations "
            "(event_id,content_ref,created_at) VALUES (?, ?, "
            "'2026-10-31T00:00:00+00:00')",
            (attach.event_id, shared_ref),
        )

        worker = app._collection_worker
        assert worker is not None

        # Capture the immutable facts of the collectible events before any
        # collection decision runs.
        collected_share = (plain_b, reply)
        facts_before: dict[str, Any] = {}
        evidence_before: dict[str, Any] = {}
        for event in collected_share:
            facts_before[event.event_id] = await app.storage._read_one(
                "SELECT matched_routes, fanout_group FROM event_routing_facts "
                "WHERE event_id=?",
                (event.event_id,),
            )
            evidence_before[event.event_id] = {
                "rows": await app.storage.list_outbox_items_for_event(event.event_id),
                "receipts": await app.storage.list_receipts_for_event(event.event_id),
                "refs": await app.storage._read_all(
                    "SELECT adapter, native_channel_id, native_message_id FROM "
                    "native_message_refs WHERE event_id=?",
                    (event.event_id,),
                ),
                "conversation": (
                    await app.storage._read_one(
                        "SELECT conversation_id FROM canonical_events "
                        "WHERE event_id=?",
                        (event.event_id,),
                    )
                )["conversation_id"],
                "routing_metadata": await app.storage.get_metadata(event.event_id),
            }
            assert facts_before[event.event_id] is not None
            assert evidence_before[event.event_id]["refs"]

        # Release collection: the collectible share collects; the
        # borrowed-quote owner and the attachment owner stay retained.
        collect_gate.set()

        async def _settled_outcomes() -> bool:
            return (
                not await _body(app.storage, plain_b.event_id)
                and not await _body(app.storage, reply.event_id)
                and worker.state.blocked >= 2
                and worker.state.empty >= 1
            )

        assert await wait_until(_settled_outcomes, timeout=15.0)
        assert await _body(
            app.storage, plain_a.event_id
        ), "the quoted event stays retained while the excerpt is held"
        assert await _body(
            app.storage, attach.event_id
        ), "the attachment owner stays retained"
        assert worker.state.collected >= 2
        assert worker.state.logical_bytes_removed > 0

        # Routing facts, delivery evidence, native references, and
        # conversation identity of the collected events are byte-identical.
        for event in collected_share:
            event_id = event.event_id
            facts_after = await app.storage._read_one(
                "SELECT matched_routes, fanout_group FROM event_routing_facts "
                "WHERE event_id=?",
                (event_id,),
            )
            assert (
                facts_after == facts_before[event_id]
            ), "typed routing facts survive collection exactly"
            after = evidence_before[event_id]
            assert (
                await app.storage.list_outbox_items_for_event(event_id) == after["rows"]
            )
            assert (
                await app.storage.list_receipts_for_event(event_id) == after["receipts"]
            )
            assert (
                await app.storage._read_all(
                    "SELECT adapter, native_channel_id, native_message_id FROM "
                    "native_message_refs WHERE event_id=?",
                    (event_id,),
                )
                == after["refs"]
            )
            for ref in after["refs"]:
                resolved = await app.storage.resolve_native_ref(
                    ref["adapter"], ref["native_channel_id"], ref["native_message_id"]
                )
                assert resolved == event_id
            row = await app.storage._read_one(
                "SELECT conversation_id FROM canonical_events WHERE event_id=?",
                (event_id,),
            )
            assert row is not None
            assert row["conversation_id"] == after["conversation"]
            metadata_after = await app.storage.get_metadata(event_id)
            assert metadata_after is not None
            assert metadata_after.metadata.routing is not None
            assert metadata_after.metadata.routing.matched_routes == (_ROUTE_ID,)
            assert msgspec.to_builtins(metadata_after) == msgspec.to_builtins(
                after["routing_metadata"]
            )

        # The content-free observability projections carry plausible
        # counters and no content values.
        block = app.diagnostic_snapshot()["content_collection"]
        assert block["worker_running"] is True
        assert block["enabled"] is True
        assert block["collected"] >= 2
        assert block["blocked"] >= 2
        assert block["empty"] >= 1
        assert block["epoch"] == worker.state.epoch
        dumped_block = json.dumps(block)
        assert "plain-" not in dumped_block and _QUOTE not in dumped_block
        snapshot = build_runtime_snapshot(
            app,
            now_fn=lambda: datetime(2026, 11, 1, tzinfo=timezone.utc),
            monotonic_fn=lambda: 0.0,
        )
        snapshot_block = snapshot["content_collection"]
        assert snapshot_block["collected"] == block["collected"]
        assert snapshot_block["epoch"] == block["epoch"]
        assert _QUOTE not in json.dumps(snapshot_block)
    finally:
        collect_gate.set()
        await clean_stop(app)
    assert app.state is RuntimeState.STOPPED


async def _body(store: Any, event_id: str) -> bool:
    row = await store._read_one(
        "SELECT body FROM event_body_content WHERE event_id=?", (event_id,)
    )
    return row is not None
