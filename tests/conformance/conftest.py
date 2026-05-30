"""Shared fixtures and builders for conformance tests.

Provides codec instances, renderer instances, capability sets, and
canonical-event builders used across the conformance test modules.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest

from medre.adapters.matrix.codec import MatrixCodec
from medre.adapters.matrix.renderer import MatrixRenderer
from medre.adapters.meshtastic.codec import MeshtasticCodec
from medre.adapters.meshtastic.renderer import MeshtasticRenderer
from medre.config.adapters.matrix import MatrixConfig
from medre.config.adapters.meshtastic import MeshtasticConfig
from medre.core.contracts.adapter import AdapterCapabilities
from medre.core.events.canonical import (
    CanonicalEvent,
    EventRelation,
    NativeRef,
)
from medre.core.events.kinds import EventKind
from medre.core.events.metadata import EventMetadata, NativeMetadata

# ---------------------------------------------------------------------------
# Adapter IDs used across conformance tests
# ---------------------------------------------------------------------------

MATRIX_ADAPTER_ID = "matrix_conf"
MESHTASTIC_ADAPTER_ID = "mesh_conf"


# ---------------------------------------------------------------------------
# Configs
# ---------------------------------------------------------------------------


@pytest.fixture()
def matrix_config() -> MatrixConfig:
    """A minimal MatrixConfig for conformance tests."""
    return MatrixConfig(
        adapter_id=MATRIX_ADAPTER_ID,
        homeserver="https://example.com",
        user_id="@bot:example.com",
    )


@pytest.fixture()
def meshtastic_config() -> MeshtasticConfig:
    """A minimal MeshtasticConfig for conformance tests."""
    return MeshtasticConfig(
        adapter_id=MESHTASTIC_ADAPTER_ID,
        max_text_bytes=227,
    )


# ---------------------------------------------------------------------------
# Codecs
# ---------------------------------------------------------------------------


@pytest.fixture()
def matrix_codec(matrix_config: MatrixConfig) -> MatrixCodec:
    """MatrixCodec wired to the conformance adapter ID."""
    return MatrixCodec(MATRIX_ADAPTER_ID, matrix_config)


@pytest.fixture()
def meshtastic_codec(meshtastic_config: MeshtasticConfig) -> MeshtasticCodec:
    """MeshtasticCodec with a deterministic clock."""
    fixed_time = datetime(2025, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
    return MeshtasticCodec(
        MESHTASTIC_ADAPTER_ID,
        meshtastic_config,
        clock=lambda: fixed_time,
    )


# ---------------------------------------------------------------------------
# Renderers
# ---------------------------------------------------------------------------


@pytest.fixture()
def matrix_renderer() -> MatrixRenderer:
    """MatrixRenderer with no source configs."""
    return MatrixRenderer()


@pytest.fixture()
def meshtastic_renderer(meshtastic_config: MeshtasticConfig) -> MeshtasticRenderer:
    """MeshtasticRenderer wired to the conformance config."""
    return MeshtasticRenderer(
        configs={MESHTASTIC_ADAPTER_ID: meshtastic_config},
    )


# ---------------------------------------------------------------------------
# Adapter capabilities
# ---------------------------------------------------------------------------


@pytest.fixture()
def matrix_capabilities() -> AdapterCapabilities:
    """Capabilities matching docs/spec/transport-profiles/matrix-capabilities.json."""
    return AdapterCapabilities(
        text=True,
        title=False,
        replies="native",
        reactions="native",
        edits="unsupported",
        deletes="unsupported",
        attachments=False,
        metadata_fields=False,
        delivery_receipts=True,
        store_and_forward=False,
        direct_messages=True,
        channels=True,
        ack_tracking=False,
        async_delivery=True,
        identity_encryption=False,
        presence=False,
        topic_rooms=True,
        mesh_routing=False,
        priority_delivery=False,
        max_text_bytes=None,
        max_text_chars=None,
    )


@pytest.fixture()
def meshtastic_capabilities() -> AdapterCapabilities:
    """Capabilities matching docs/spec/transport-profiles/meshtastic-capabilities.json."""
    return AdapterCapabilities(
        text=True,
        title=False,
        replies="native",
        reactions="native",
        edits="unsupported",
        deletes="unsupported",
        attachments=False,
        metadata_fields=True,
        delivery_receipts=False,
        store_and_forward=False,
        direct_messages=False,
        channels=True,
        ack_tracking=False,
        async_delivery=True,
        identity_encryption=False,
        presence=False,
        topic_rooms=False,
        mesh_routing=True,
        priority_delivery=False,
        max_text_bytes=227,
        max_text_chars=None,
    )


# ---------------------------------------------------------------------------
# Canonical-event builder
# ---------------------------------------------------------------------------


def make_text_event(
    *,
    source_adapter: str = "matrix_conf",
    source_channel_id: str | None = "!room:example.com",
    body: str = "Hello",
    relations: tuple[EventRelation, ...] = (),
) -> CanonicalEvent:
    """Build a minimal text CanonicalEvent for conformance assertions."""
    return CanonicalEvent(
        event_id=str(uuid.uuid4()),
        event_kind=EventKind.MESSAGE_CREATED,
        schema_version=1,
        timestamp=datetime(2025, 1, 1, 0, 0, 0, tzinfo=timezone.utc),
        source_adapter=source_adapter,
        source_transport_id="user_conf",
        source_channel_id=source_channel_id,
        parent_event_id=None,
        lineage=(),
        relations=relations,
        payload={"body": body, "text": body},
        metadata=EventMetadata(native=NativeMetadata(data={})),
    )


def make_reply_event(
    *,
    source_adapter: str = "matrix_conf",
    target_adapter: str = "matrix_conf",
    target_channel: str = "!room:example.com",
    target_message_id: str = "$original_001",
    body: str = "Reply text",
) -> CanonicalEvent:
    """Build a text CanonicalEvent with a reply relation."""
    rel = EventRelation(
        relation_type="reply",
        target_event_id=None,
        target_native_ref=NativeRef(
            adapter=target_adapter,
            native_channel_id=target_channel,
            native_message_id=target_message_id,
        ),
        key=None,
        fallback_text=None,
    )
    return make_text_event(
        source_adapter=source_adapter,
        source_channel_id=target_channel,
        body=body,
        relations=(rel,),
    )


def make_reaction_event(
    *,
    source_adapter: str = "mesh_conf",
    target_adapter: str = "mesh_conf",
    target_channel: str = "0",
    target_message_id: str = "42",
    emoji: str = "\U0001f44d",
) -> CanonicalEvent:
    """Build a reaction CanonicalEvent."""
    rel = EventRelation(
        relation_type="reaction",
        target_event_id=None,
        target_native_ref=NativeRef(
            adapter=target_adapter,
            native_channel_id=target_channel,
            native_message_id=target_message_id,
        ),
        key=emoji,
        fallback_text=None,
    )
    return CanonicalEvent(
        event_id=str(uuid.uuid4()),
        event_kind=EventKind.MESSAGE_REACTED,
        schema_version=1,
        timestamp=datetime(2025, 1, 1, 0, 0, 0, tzinfo=timezone.utc),
        source_adapter=source_adapter,
        source_transport_id="!sender",
        source_channel_id=target_channel,
        parent_event_id=None,
        lineage=(),
        relations=(rel,),
        payload={"body": emoji, "key": emoji},
        metadata=EventMetadata(native=NativeMetadata(data={})),
    )
