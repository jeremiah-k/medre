"""Targeted tests for fake-adapter fidelity knobs (D9).

Each test pins one knob on a fake (LXMF signature_validated, MeshCore
``type: PRIV`` direct-event helper, Meshtastic connection_generation
passthrough) to a behavior the real SDK can produce. Defaults preserve
the historical happy-path behavior.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from unittest.mock import patch

from medre.adapters.fakes.lxmf import FakeLxmfAdapter
from medre.adapters.fakes.meshcore import FakeMeshCoreAdapter
from medre.adapters.fakes.meshtastic import FakeMeshtasticAdapter
from medre.config.adapters.lxmf import LxmfConfig
from medre.config.adapters.meshcore import MeshCoreConfig
from medre.config.adapters.meshtastic import MeshtasticConfig
from medre.core.contracts.adapter import AdapterContext


def _make_context(adapter_id: str) -> AdapterContext:
    return AdapterContext(
        adapter_id=adapter_id,
        event_bus=None,
        publish_inbound=lambda _e: asyncio.sleep(0),
        logger=logging.getLogger(f"test.{adapter_id}"),
        clock=lambda: datetime.now(UTC),
        shutdown_event=asyncio.Event(),
    )


def _seeded_lxmf() -> FakeLxmfAdapter:
    return FakeLxmfAdapter(
        LxmfConfig(adapter_id="lxmf-fidelity", connection_type="fake").validate()
    )


def _seeded_meshcore() -> FakeMeshCoreAdapter:
    return FakeMeshCoreAdapter(
        MeshCoreConfig(
            adapter_id="meshcore-fidelity", connection_type="fake"
        ).validate()
    )


def _seeded_meshtastic() -> FakeMeshtasticAdapter:
    return FakeMeshtasticAdapter(
        MeshtasticConfig(
            adapter_id="meshtastic-fidelity", connection_type="fake"
        ).validate()
    )


def test_fake_lxmf_default_signature_is_validated() -> None:
    adapter = _seeded_lxmf()

    event = adapter.make_text_event(body="hello", source_name="alice")

    assert event.payload["body"] == "hello"


def test_fake_lxmf_unvalidated_signature_reaches_codec_input() -> None:
    """The fidelity knob is preserved on the packet consumed by the codec."""
    adapter = _seeded_lxmf()
    validated = adapter.make_text_event(
        body="hi", source_name="alice", signature_validated=True
    )

    with patch.object(adapter._codec, "decode", wraps=adapter._codec.decode) as decode:
        unvalidated = adapter.make_text_event(
            body="hi", source_name="alice", signature_validated=False
        )

    packet = decode.call_args.args[0]
    assert packet["signature_validated"] is False
    assert validated.payload["body"] == unvalidated.payload["body"]
    assert validated.event_kind == unvalidated.event_kind
    assert validated.source_adapter == unvalidated.source_adapter
    assert (
        validated.metadata.native.data["lxmf"]["source_hash"]
        == unvalidated.metadata.native.data["lxmf"]["source_hash"]
    )


def test_fake_meshcore_direct_event_marks_direct_message() -> None:
    adapter = _seeded_meshcore()

    event = adapter.make_direct_event(body="hi", sender="abc123")

    meshcore_meta = event.metadata.native.data["meshcore"]
    assert meshcore_meta["is_direct_message"] is True
    assert meshcore_meta["channel"] is None


def test_fake_meshcore_direct_event_has_no_channel_index() -> None:
    """Real contact-message receive events are not scoped to a channel."""
    adapter = _seeded_meshcore()

    event = adapter.make_direct_event(body="hi")

    meshcore_meta = event.metadata.native.data["meshcore"]
    assert meshcore_meta["is_direct_message"] is True
    assert meshcore_meta["channel"] is None


async def test_fake_meshtastic_default_omits_connection_generation() -> None:
    adapter = _seeded_meshtastic()
    await adapter.start(_make_context("meshtastic-fidelity"))

    packet = {
        "fromId": "!sender",
        "toId": "^all",
        "channel": 0,
        "decoded": {"portnum": "text_message", "text": "hello"},
        "id": 12345,
    }
    await adapter.simulate_inbound(packet)

    assert "_connection_generation" not in packet


async def test_fake_meshtastic_records_connection_generation() -> None:
    adapter = _seeded_meshtastic()
    await adapter.start(_make_context("meshtastic-fidelity"))

    packet = {
        "fromId": "!sender",
        "toId": "^all",
        "channel": 0,
        "decoded": {"portnum": "text_message", "text": "hello"},
        "id": 12345,
    }
    await adapter.simulate_inbound(packet, connection_generation=7)

    assert packet["_connection_generation"] == 7


async def test_fake_meshtastic_generation_does_not_change_classification() -> None:
    """The generation token is metadata-only for the inbound classifier."""
    adapter = _seeded_meshtastic()
    await adapter.start(_make_context("meshtastic-fidelity"))

    packet = {
        "fromId": "!sender",
        "toId": "^all",
        "channel": 0,
        "decoded": {"portnum": "text_message", "text": "hello"},
        "id": 12345,
    }
    await adapter.simulate_inbound(dict(packet))
    await adapter.simulate_inbound(dict(packet), connection_generation=99)

    assert len(adapter.inbound_events) == 2
