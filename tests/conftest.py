"""Shared pytest fixtures for the medre test suite.

Provides temporary SQLite storage, sample canonical events, routing
fixtures, and adapter context helpers used across all test modules.
"""

from __future__ import annotations

import asyncio
import logging
import os
import tempfile
from datetime import datetime, timezone
from typing import Any

import pytest

from medre.core.events import (
    CanonicalEvent,
    EventMetadata,
    EventRelation,
    NativeMessageRef,
    NativeRef,
    RadioMetadata,
    RoutingMetadata,
    TransportMetadata,
)
from medre.core.routing import Route, RouteSource, RouteTarget, Router
from medre.core.storage.sqlite import SQLiteStorage


# ---------------------------------------------------------------------------
# Event fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def sample_event() -> CanonicalEvent:
    """Basic canonical event fixture."""
    return CanonicalEvent(
        event_id="test-001",
        event_kind="message.created",
        schema_version=1,
        timestamp=datetime.now(timezone.utc),
        source_adapter="fake_transport",
        source_transport_id="node-123",
        source_channel_id="ch-0",
        parent_event_id=None,
        lineage=(),
        relations=(),
        payload={"text": "hello world"},
        metadata=EventMetadata(),
    )


@pytest.fixture
def sample_event_with_relations() -> CanonicalEvent:
    """Canonical event that carries a reply relation."""
    native_ref = NativeRef(
        adapter="fake_presentation",
        native_channel_id="ch-0",
        native_message_id="native-msg-001",
    )
    relation = EventRelation(
        relation_type="reply",
        target_event_id="test-000",
        target_native_ref=native_ref,
        key=None,
        fallback_text="original message",
    )
    return CanonicalEvent(
        event_id="test-002",
        event_kind="message.created",
        schema_version=1,
        timestamp=datetime.now(timezone.utc),
        source_adapter="fake_transport",
        source_transport_id="node-123",
        source_channel_id="ch-0",
        parent_event_id=None,
        lineage=(),
        relations=(relation,),
        payload={"text": "a reply"},
        metadata=EventMetadata(
            transport=TransportMetadata(protocol="test", delivery_confirmed=True),
            routing=RoutingMetadata(matched_routes=("test-route",)),
            radio=RadioMetadata(snr=7.5, rssi=-90.0, frequency=868.0),
        ),
    )


@pytest.fixture
def sample_native_ref() -> NativeRef:
    """A native-space message reference."""
    return NativeRef(
        adapter="fake_transport",
        native_channel_id="ch-0",
        native_message_id="native-msg-42",
    )


@pytest.fixture
def sample_native_message_ref() -> NativeMessageRef:
    """A persisted native-to-canonical mapping."""
    return NativeMessageRef(
        id="nref-001",
        event_id="test-001",
        adapter="fake_transport",
        native_channel_id="ch-0",
        native_message_id="native-msg-42",
        native_thread_id=None,
        native_relation_id=None,
        direction="inbound",
        metadata={},
        created_at=datetime.now(timezone.utc),
    )


# ---------------------------------------------------------------------------
# Storage fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
async def temp_storage() -> SQLiteStorage:
    """SQLiteStorage backed by a temporary file, cleaned up after test."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    storage = SQLiteStorage(db_path=db_path)
    await storage.initialize()
    yield storage
    await storage.close()
    os.unlink(db_path)


# ---------------------------------------------------------------------------
# Routing fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def sample_route() -> Route:
    """Basic route: fake_transport -> fake_presentation."""
    return Route(
        id="test-route",
        source=RouteSource(
            adapter="fake_transport",
            event_kinds=("message.created",),
            channel="ch-0",
        ),
        targets=[RouteTarget(adapter="fake_presentation")],
    )


@pytest.fixture
def router_with_routes(sample_route: Route) -> Router:
    """Router pre-loaded with sample routes."""
    return Router(routes=[sample_route])


# ---------------------------------------------------------------------------
# Adapter context helpers
# ---------------------------------------------------------------------------


class _InboundCollector:
    """Callable that collects published inbound events into a list."""

    def __init__(self) -> None:
        self.events: list[CanonicalEvent] = []

    async def __call__(self, event: CanonicalEvent) -> None:
        self.events.append(event)


@pytest.fixture
def inbound_collector() -> _InboundCollector:
    """Callable that records every event passed to publish_inbound."""
    return _InboundCollector()


@pytest.fixture
def make_adapter_context(inbound_collector: _InboundCollector):
    """Factory that creates an AdapterContext wired to the inbound collector."""

    def _make(adapter_id: str = "test_adapter") -> Any:
        from medre.adapters.base import AdapterContext

        return AdapterContext(
            adapter_id=adapter_id,
            event_bus=None,
            publish_inbound=inbound_collector,
            logger=logging.getLogger(f"test.{adapter_id}"),
            clock=lambda: datetime.now(timezone.utc),
            shutdown_event=asyncio.Event(),
        )

    return _make
