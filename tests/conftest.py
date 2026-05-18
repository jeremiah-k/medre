"""Shared pytest fixtures for the medre test suite.

Provides temporary SQLite storage, sample canonical events, routing
fixtures, and adapter context helpers used across all test modules.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncGenerator
from unittest.mock import patch

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
from medre.core.rendering import RenderingPipeline, TextRenderer
from medre.core.routing import Route, Router, RouteSource, RouteTarget
from medre.core.storage.sqlite import SQLiteStorage
from tests.helpers.matrix import build_mock_nio_module

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
async def temp_storage() -> AsyncGenerator[SQLiteStorage, None]:
    """SQLiteStorage backed by a temporary file, cleaned up after test.

    When ``MEDRE_DOCKER_ARTIFACT_RUN_DIR`` is set, the database file is
    placed under that directory (with a unique suffix) and **not** deleted
    after the test so the artifact collector can retrieve it.
    """
    artifact_dir = os.environ.get("MEDRE_DOCKER_ARTIFACT_RUN_DIR")
    if artifact_dir:
        ad = Path(artifact_dir)
        ad.mkdir(parents=True, exist_ok=True)
        import uuid

        db_path = str(ad / f"storage-{uuid.uuid4().hex[:12]}.db")
    else:
        f = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        db_path = f.name
        f.close()

    storage = SQLiteStorage(db_path=db_path)
    await storage.initialize()
    yield storage
    await storage.close()
    if not artifact_dir:
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
        from medre.core.contracts.adapter import AdapterContext

        return AdapterContext(
            adapter_id=adapter_id,
            event_bus=None,
            publish_inbound=inbound_collector,
            logger=logging.getLogger(f"test.{adapter_id}"),
            clock=lambda: datetime.now(timezone.utc),
            shutdown_event=asyncio.Event(),
        )

    return _make


# ---------------------------------------------------------------------------
# CLI config fixtures (shared across test_cli_* and test_replay_* modules)
# ---------------------------------------------------------------------------

CONFIG_FAKE_MULTI = """\
[runtime]
name = "workflow-test"
shutdown_timeout_seconds = 5

[runtime.limits]
max_inflight_deliveries = 50
max_inflight_replay_events = 25
shutdown_drain_timeout_seconds = 3
delivery_acquire_timeout_seconds = 0.5

[logging]
level = "INFO"
format = "text"

[storage]
backend = "memory"

[adapters.matrix.fake_matrix]
enabled = true
adapter_kind = "fake"
homeserver = "https://fake.local"
user_id = "@bot:fake.local"
access_token = "fake_tok"
room_allowlist = ["!room:fake.local"]
encryption_mode = "plaintext"

[adapters.meshtastic.fake_mesh]
enabled = true
adapter_kind = "fake"
connection_type = "fake"
meshnet_name = "TestMesh"

[routes.matrix_to_mesh]
source_adapters = ["fake_matrix"]
dest_adapters = ["fake_mesh"]
directionality = "source_to_dest"
enabled = true
source_room = "!room:fake.local"
dest_channel = "1"

[routes.mesh_to_matrix]
source_adapters = ["fake_mesh"]
dest_adapters = ["fake_matrix"]
directionality = "source_to_dest"
enabled = false

[routes.bidirectional_bridge]
source_adapters = ["fake_matrix"]
dest_adapters = ["fake_mesh"]
directionality = "bidirectional"
enabled = true

[routes.bidirectional_bridge.policy]
allowed_event_types = ["message"]
"""

CONFIG_MINIMAL_MEMORY = """\
[runtime]
name = "minimal-workflow"

[storage]
backend = "memory"
"""

CONFIG_SINGLE_ADAPTER = """\
[runtime]
name = "single-adapter"

[storage]
backend = "memory"

[adapters.matrix.solo]
enabled = true
adapter_kind = "fake"
homeserver = "https://fake.local"
user_id = "@bot:fake.local"
access_token = "tok_single"
room_allowlist = ["!room:fake.local"]
encryption_mode = "plaintext"
"""


@pytest.fixture()
def config_fake_multi(tmp_path: Path) -> Path:
    p = tmp_path / "config.toml"
    p.write_text(CONFIG_FAKE_MULTI)
    return p


@pytest.fixture()
def config_minimal(tmp_path: Path) -> Path:
    p = tmp_path / "config.toml"
    p.write_text(CONFIG_MINIMAL_MEMORY)
    return p


@pytest.fixture()
def config_single(tmp_path: Path) -> Path:
    p = tmp_path / "config.toml"
    p.write_text(CONFIG_SINGLE_ADAPTER)
    return p


@pytest.fixture()
def tmp_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Set MEDRE_HOME to a temp dir and return it."""
    monkeypatch.setenv("MEDRE_HOME", str(tmp_path))
    return tmp_path


# ---------------------------------------------------------------------------
# Docker artifact fixtures (shared across test_docker_artifact_core.py)
# ---------------------------------------------------------------------------


@pytest.fixture
def tmp_base(tmp_path: Path) -> Path:
    """Provide a temporary base directory for artifact runs."""
    return tmp_path / "bridge-runs"


# ---------------------------------------------------------------------------
# Replay engine fixtures (shared across test_replay_* modules)
# ---------------------------------------------------------------------------


@pytest.fixture
def rendering_pipeline() -> RenderingPipeline:
    """RenderingPipeline with TextRenderer registered."""
    pipeline = RenderingPipeline()
    pipeline.register(TextRenderer(), priority=100)
    return pipeline


# ---------------------------------------------------------------------------
# Matrix mock fixtures (shared across test_wrapper_multi_callback.py)
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_nio() -> Any:
    """Inject a mock nio module into sys.modules and patch HAS_NIO."""
    mock = build_mock_nio_module()
    saved_nio = sys.modules.get("nio")
    saved_nio_events = sys.modules.get("nio.events")
    sys.modules["nio"] = mock
    sys.modules["nio.events"] = mock.events
    with patch("medre.adapters.matrix.adapter.HAS_NIO", True):
        yield mock
    if saved_nio is None:
        sys.modules.pop("nio", None)
    else:
        sys.modules["nio"] = saved_nio
    if saved_nio_events is None:
        sys.modules.pop("nio.events", None)
    else:
        sys.modules["nio.events"] = saved_nio_events
