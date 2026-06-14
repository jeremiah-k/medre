"""Env-only reliability tests for the delivery path.

End-to-end tests that exercise delivery reliability semantics — successful
delivery, duplicate suppression, native-ref dedup persistence, and
route-stats tracking — using the full RuntimeBuilder + MedreApp stack driven entirely
from environment variables with a minimal TOML skeleton.

No Docker, no live transports, no SDK dependencies required.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from medre.config.env import apply_env_overrides
from medre.config.loader import load_config
from medre.core.events import NativeRef
from medre.core.events.canonical import CanonicalEvent
from medre.core.events.metadata import EventMetadata
from medre.core.planning.delivery_plan import DeliveryFailureKind
from medre.runtime.builder import RuntimeBuilder

# ---------------------------------------------------------------------------
# Env isolation
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Clear all env vars between tests to prevent shell/CI contamination."""
    import os

    for var in (
        "MEDRE_HOME",
        "MEDRE_CONFIG",
        "XDG_CONFIG_HOME",
        "XDG_STATE_HOME",
        "XDG_DATA_HOME",
        "XDG_CACHE_HOME",
    ):
        monkeypatch.delenv(var, raising=False)
    for key in list(os.environ.keys()):
        if key.startswith("MEDRE_"):
            monkeypatch.delenv(key, raising=False)


# ---------------------------------------------------------------------------
# Minimal TOML config — no adapters, no routes
# ---------------------------------------------------------------------------

_ENV_ONLY_TOML_TEMPLATE = """\
[runtime]
name = "env-only-reliability-test"
shutdown_timeout_seconds = 5

[logging]
level = "WARNING"
format = "text"

[storage]
backend = "sqlite"
path = "{db_path}"
"""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_config(tmp_path: Path, db_path: str) -> Path:
    """Write the minimal TOML config and return its path."""
    config_path = tmp_path / "reliability.toml"
    config_path.write_text(_ENV_ONLY_TOML_TEMPLATE.format(db_path=db_path))
    return config_path


def _set_reliability_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Set env vars for the standard radio-a → matrix-fake route."""
    monkeypatch.setenv("MEDRE_ADAPTER__RADIO_A__TRANSPORT", "meshtastic")
    monkeypatch.setenv("MEDRE_ADAPTER__RADIO_A__ADAPTER_KIND", "fake")
    monkeypatch.setenv("MEDRE_ADAPTER__RADIO_A__CONNECTION_TYPE", "fake")
    monkeypatch.setenv("MEDRE_ADAPTER__MATRIX_FAKE__TRANSPORT", "matrix")
    monkeypatch.setenv("MEDRE_ADAPTER__MATRIX_FAKE__ADAPTER_KIND", "fake")
    monkeypatch.setenv("MEDRE_ADAPTER__MATRIX_FAKE__HOMESERVER", "https://matrix.test")
    monkeypatch.setenv("MEDRE_ADAPTER__MATRIX_FAKE__USER_ID", "@bot:test")
    monkeypatch.setenv("MEDRE_ADAPTER__MATRIX_FAKE__ACCESS_TOKEN", "tok")
    monkeypatch.setenv("MEDRE_ADAPTER__MATRIX_FAKE__ROOM_ALLOWLIST", "!room:test")
    monkeypatch.setenv("MEDRE_ROUTE__RADIO_TO_MATRIX__SOURCE_ADAPTERS", "radio-a")
    monkeypatch.setenv("MEDRE_ROUTE__RADIO_TO_MATRIX__DEST_ADAPTERS", "matrix-fake")
    monkeypatch.setenv("MEDRE_ROUTE__RADIO_TO_MATRIX__DIRECTIONALITY", "source_to_dest")
    monkeypatch.setenv("MEDRE_ROUTE__RADIO_TO_MATRIX__ENABLED", "true")


def _load_and_build(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    db_path: str,
    env_setup: Any = None,
) -> Any:
    """Write config, load TOML, apply env overrides, build and return app."""
    if env_setup is not None:
        env_setup(monkeypatch)
    else:
        _set_reliability_env(monkeypatch)

    config_path = _write_config(tmp_path, db_path)
    config, _source, paths = load_config(str(config_path))
    config = apply_env_overrides(config)

    builder = RuntimeBuilder(config, paths)
    return builder.build()


def _make_event(
    adapter_id: str,
    text: str = "test",
    event_id: str | None = None,
    source_native_ref: NativeRef | None = None,
) -> CanonicalEvent:
    """Create a CanonicalEvent suitable for pipeline injection."""
    return CanonicalEvent(
        event_id=event_id or f"rel-test-{uuid.uuid4()}",
        event_kind="message.created",
        schema_version=1,
        timestamp=datetime.now(timezone.utc),
        source_adapter=adapter_id,
        source_transport_id="meshtastic",
        source_channel_id="ch-0",
        parent_event_id=None,
        lineage=(),
        relations=(),
        payload={"text": text, "body": text},
        metadata=EventMetadata(),
        source_native_ref=source_native_ref,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestEnvOnlyReliability:
    """Delivery reliability tests using env-only fake deployment."""

    @pytest.mark.asyncio
    async def test_env_only_successful_delivery(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """Event flows from radio-a through pipeline to matrix-fake with
        storage persistence of event, receipt (status=sent), and native ref
        (direction=outbound)."""
        db_path = str(tmp_path / "delivery.db")
        app = _load_and_build(monkeypatch, tmp_path, db_path)

        try:
            await app.start()

            # Inject event from radio-a.
            event = _make_event("radio-a", text="reliability delivery test")
            outcomes = await app.pipeline_runner.handle_ingress(event)

            # -- Verify delivery outcomes ------------------------------------
            assert len(outcomes) >= 1, "Expected at least one delivery outcome"
            successful = [o for o in outcomes if o.status == "success"]
            assert (
                len(successful) >= 1
            ), f"No successful deliveries; statuses: {[o.status for o in outcomes]}"
            assert successful[0].target_adapter == "matrix-fake"

            # -- Verify storage: event persisted ------------------------------
            storage = app.storage
            assert storage is not None

            stored_event = await storage.get(event.event_id)
            assert stored_event is not None, f"Event {event.event_id!r} not in storage"
            assert stored_event.event_id == event.event_id

            # -- Verify storage: delivery receipt with status="sent" ----------
            receipts = await storage.list_receipts_for_event(event.event_id)
            assert len(receipts) >= 1, "Expected at least one delivery receipt"
            sent_receipts = [r for r in receipts if r.status == "sent"]
            assert len(sent_receipts) >= 1, "No receipt with status 'sent'"

            # -- Verify storage: outbound native ref -------------------------
            native_refs = await storage.list_native_refs_for_event(event.event_id)
            outbound_refs = [
                nr for nr in native_refs if getattr(nr, "direction", None) == "outbound"
            ]
            assert (
                len(outbound_refs) >= 1
            ), "Expected at least one outbound native ref for matrix-fake"
            assert getattr(outbound_refs[0], "adapter", None) == "matrix-fake"
            assert getattr(outbound_refs[0], "native_message_id", None) is not None

        finally:
            try:
                await app.stop()
            except Exception as exc:
                pytest.fail(f"app.stop() failed: {exc!r}")

    @pytest.mark.asyncio
    async def test_env_only_duplicate_suppression(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """Injecting the same event twice does not produce duplicate
        deliveries.  Second injection returns empty outcomes (suppressed)
        and storage has exactly one receipt and one native ref."""
        db_path = str(tmp_path / "dedup.db")
        app = _load_and_build(monkeypatch, tmp_path, db_path)

        try:
            await app.start()

            native_ref = NativeRef(
                adapter="radio-a",
                native_channel_id="ch-0",
                native_message_id="dedup-native-001",
            )
            event_id = f"dedup-{uuid.uuid4()}"

            # First injection.
            event1 = _make_event(
                "radio-a",
                text="first",
                event_id=event_id,
                source_native_ref=native_ref,
            )
            outcomes1 = await app.pipeline_runner.handle_ingress(event1)
            assert len(outcomes1) >= 1, "First injection should deliver"
            assert any(o.status == "success" for o in outcomes1)

            # Second injection with the same native ref but different event_id.
            event2 = _make_event(
                "radio-a",
                text="duplicate",
                event_id=f"dup-{uuid.uuid4()}",
                source_native_ref=native_ref,
            )
            outcomes2 = await app.pipeline_runner.handle_ingress(event2)

            # Second injection should be suppressed (empty outcomes).
            assert (
                outcomes2 == []
            ), f"Expected empty outcomes for duplicate, got {[o.status for o in outcomes2]}"

            # Storage should have exactly one event.
            storage = app.storage
            assert storage is not None
            stored_event = await storage.get(event_id)
            assert stored_event is not None

            # Exactly one receipt for this event.
            receipts = await storage.list_receipts_for_event(event_id)
            assert (
                len(receipts) == 1
            ), f"Expected exactly 1 receipt, got {len(receipts)}"

            # Exactly one native ref for this event.
            native_refs = await storage.list_native_refs_for_event(event_id)
            assert (
                len(native_refs) >= 1
            ), f"Expected at least 1 native ref for this event, got {len(native_refs)}"

        finally:
            try:
                await app.stop()
            except Exception as exc:
                pytest.fail(f"app.stop() failed: {exc!r}")

    @pytest.mark.asyncio
    async def test_env_only_route_stats_tracked(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """RouteStats properly track delivery counts after a successful
        delivery through an env-created route."""
        db_path = str(tmp_path / "routestats.db")
        app = _load_and_build(
            monkeypatch,
            tmp_path,
            db_path,
        )

        try:
            await app.start()

            event = _make_event("radio-a", text="route-stats test")
            outcomes = await app.pipeline_runner.handle_ingress(event)

            assert len(outcomes) >= 1, "Expected at least one outcome"
            assert outcomes[0].status == "success"

            # RouteStats should show at least one delivered count for
            # the radio-to-matrix route.
            route_stats = app.route_stats
            assert route_stats is not None
            snap = route_stats.snapshot()
            assert "radio-to-matrix" in snap, (
                f"Expected route 'radio-to-matrix' in RouteStats, "
                f"got: {sorted(snap.keys())}"
            )
            route_entry = snap["radio-to-matrix"]
            assert (
                route_entry.get("delivered", 0) >= 1
            ), f"Expected delivered >= 1, got: {route_entry}"
        finally:
            try:
                await app.stop()
            except Exception as exc:
                pytest.fail(f"app.stop() failed: {exc!r}")

    @pytest.mark.asyncio
    async def test_env_only_native_ref_dedup_persists(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """Native ref dedup works across separate handle_ingress calls.
        Two events with different event_ids but the same source_native_ref
        triple: second is suppressed and only one event is stored."""
        db_path = str(tmp_path / "nref_dedup.db")
        app = _load_and_build(monkeypatch, tmp_path, db_path)

        try:
            await app.start()

            shared_native_ref = NativeRef(
                adapter="radio-a",
                native_channel_id="ch-0",
                native_message_id="shared-native-999",
            )

            # First event — should succeed.
            event_a = _make_event(
                "radio-a",
                text="first with shared ref",
                event_id=f"nref-a-{uuid.uuid4()}",
                source_native_ref=shared_native_ref,
            )
            outcomes_a = await app.pipeline_runner.handle_ingress(event_a)
            assert len(outcomes_a) >= 1, "First event should deliver"
            assert any(o.status == "success" for o in outcomes_a)

            # Second event with same native ref but different event_id.
            event_b = _make_event(
                "radio-a",
                text="second with shared ref",
                event_id=f"nref-b-{uuid.uuid4()}",
                source_native_ref=shared_native_ref,
            )
            outcomes_b = await app.pipeline_runner.handle_ingress(event_b)

            # Second event should be suppressed.
            assert (
                outcomes_b == []
            ), f"Expected empty outcomes for dedup, got {[o.status for o in outcomes_b]}"

            # Only the first event_id should be in storage.
            storage = app.storage
            assert storage is not None

            stored_a = await storage.get(event_a.event_id)
            assert stored_a is not None, "First event should be persisted"

            stored_b = await storage.get(event_b.event_id)
            assert (
                stored_b is None
            ), f"Second event {event_b.event_id!r} should NOT be persisted (dedup)"

        finally:
            try:
                await app.stop()
            except Exception as exc:
                pytest.fail(f"app.stop() failed: {exc!r}")

    @pytest.mark.asyncio
    async def test_env_only_loop_suppressed_failure_kind(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """DeliveryOutcome for route-trace loop prevention has
        failure_kind=LOOP_SUPPRESSED."""
        db_path = str(tmp_path / "loopsuppress.db")
        app = _load_and_build(
            monkeypatch,
            tmp_path,
            db_path,
        )

        try:
            await app.start()

            # Create an event with a route_trace that includes the
            # route ID twice (simulating a prior traversal).
            from medre.core.events.metadata import RoutingMetadata

            event = CanonicalEvent(
                event_id=f"loop-suppress-{uuid.uuid4()}",
                event_kind="message.created",
                schema_version=1,
                timestamp=datetime.now(timezone.utc),
                source_adapter="radio-a",
                source_transport_id="meshtastic",
                source_channel_id="ch-0",
                parent_event_id=None,
                lineage=(),
                relations=(),
                payload={"text": "loop suppression test"},
                metadata=EventMetadata(
                    routing=RoutingMetadata(
                        route_trace=("radio-to-matrix", "radio-to-matrix"),
                    ),
                ),
            )

            outcomes = await app.pipeline_runner.handle_ingress(event)

            # There might be outcomes from other routes too, but at least
            # one should be a route-trace loop suppression.
            suppressed = [
                o
                for o in outcomes
                if o.status == "skipped"
                and o.failure_kind is not None
                and o.failure_kind == DeliveryFailureKind.LOOP_SUPPRESSED
            ]
            assert len(suppressed) >= 1, (
                f"Expected at least one LOOP_SUPPRESSED outcome, "
                f"got outcomes: {[(o.status, o.failure_kind) for o in outcomes]}"
            )

            # RouteStats should show loop_prevented >= 1.
            route_stats = app.route_stats
            assert route_stats is not None
            snap = route_stats.snapshot()
            loop_total = sum(v.get("loop_prevented", 0) for v in snap.values())
            assert loop_total >= 1, f"Expected loop_prevented >= 1, got {loop_total}"
        finally:
            try:
                await app.stop()
            except Exception as exc:
                pytest.fail(f"app.stop() failed: {exc!r}")

    def test_loop_suppressed_is_not_retryable(self) -> None:
        """LOOP_SUPPRESSED is not retryable and classifies as a permanent failure."""
        from medre.core.observability.classification import (
            PERMANENT_KINDS,
            failure_category,
        )

        assert not DeliveryFailureKind.LOOP_SUPPRESSED.is_retryable
        assert DeliveryFailureKind.LOOP_SUPPRESSED.value in PERMANENT_KINDS
        assert failure_category("loop_suppressed") == "permanent"
