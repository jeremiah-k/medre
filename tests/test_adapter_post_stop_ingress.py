"""Post-stop ingress guard tests for adapter callback paths.

These tests cover the lifecycle boundary where an adapter retains ``ctx`` after
``stop()`` but must not publish late inbound events.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

import pytest

from medre.adapters.fakes.lxmf import FakeLxmfAdapter
from medre.adapters.fakes.matrix import FakeMatrixAdapter
from medre.adapters.fakes.meshcore import FakeMeshCoreAdapter
from medre.adapters.fakes.meshtastic import FakeMeshtasticAdapter
from medre.adapters.lxmf.adapter import LxmfAdapter
from medre.adapters.matrix.adapter import MatrixAdapter
from medre.adapters.meshcore.adapter import MeshCoreAdapter
from medre.adapters.meshtastic.adapter import MeshtasticAdapter
from medre.config.adapters.lxmf import LxmfConfig
from medre.config.adapters.matrix import MatrixConfig
from medre.config.adapters.meshcore import MeshCoreConfig
from medre.core.events.canonical import CanonicalEvent
from tests.helpers.bridge import make_meshcore_packet
from tests.helpers.meshtastic import (
    make_meshtastic_config,
    make_meshtastic_text_packet,
)


def _lxmf_packet(content: str = "hello", message_id: str = "cd" * 32) -> dict[str, Any]:
    return {
        "source_hash": "ab" * 16,
        "destination_hash": "00" * 16,
        "message_id": message_id,
        "timestamp": 1700000000.0,
        "title": "",
        "content": content,
        "fields": {},
        "signature_validated": True,
        "has_fields": False,
    }


def _matrix_event(adapter: FakeMatrixAdapter) -> CanonicalEvent:
    return adapter.make_event(text="late matrix")


def _matrix_room_event() -> dict[str, Any]:
    return {
        "room_id": "!room:example.com",
        "sender": "@alice:example.com",
        "body": "late matrix",
        "event_id": "$late",
        "source": {"content": {"msgtype": "m.text", "body": "late matrix"}},
        "msgtype": "m.text",
        "server_timestamp": 1700000000000,
        "sender_display_name": "Alice",
    }


@pytest.mark.parametrize(
    ("name", "adapter_factory", "payload_factory"),
    [
        (
            "matrix",
            lambda: FakeMatrixAdapter("fake-matrix-post-stop"),
            _matrix_event,
        ),
        (
            "meshtastic",
            lambda: FakeMeshtasticAdapter(adapter_id="fake-mesh-post-stop"),
            lambda _adapter: make_meshtastic_text_packet(
                text="late mesh", packet_id=42
            ),
        ),
        (
            "meshcore",
            lambda: FakeMeshCoreAdapter(adapter_id="fake-meshcore-post-stop"),
            lambda _adapter: make_meshcore_packet(text="late meshcore", packet_id=43),
        ),
        (
            "lxmf",
            lambda: FakeLxmfAdapter(adapter_id="fake-lxmf-post-stop"),
            lambda _adapter: _lxmf_packet(content="late lxmf"),
        ),
    ],
    ids=lambda value: value if isinstance(value, str) else None,
)
async def test_fake_adapters_drop_simulate_inbound_after_stop(
    name: str,
    adapter_factory: Callable[[], Any],
    payload_factory: Callable[[Any], Any],
    make_adapter_context,
    inbound_collector,
) -> None:
    adapter = adapter_factory()
    ctx = make_adapter_context(f"{name}-post-stop")
    await adapter.start(ctx)

    await adapter.stop()
    assert adapter.ctx is not None

    await adapter.simulate_inbound(payload_factory(adapter))

    assert inbound_collector.events == []
    assert getattr(adapter, "inbound_events", []) == []


async def test_meshtastic_adapter_drops_simulate_inbound_after_stop(
    make_adapter_context,
    inbound_collector,
) -> None:
    adapter = MeshtasticAdapter(
        make_meshtastic_config(adapter_id="real-mesh-post-stop", connection_type="fake")
    )
    await adapter.start(make_adapter_context("real-mesh-post-stop"))
    await adapter.stop()

    await adapter.simulate_inbound(
        make_meshtastic_text_packet(text="late", packet_id=7)
    )

    assert inbound_collector.events == []
    assert adapter.diagnostics()["inbound_published"] == 0


async def test_matrix_adapter_drops_room_callback_after_stop(
    make_adapter_context,
    inbound_collector,
) -> None:
    adapter = MatrixAdapter(
        MatrixConfig(
            adapter_id="matrix-post-stop",
            homeserver="https://matrix.example.com",
            user_id="@bot:example.com",
            access_token="token",
            encryption_mode="plaintext",
        )
    )
    adapter.ctx = make_adapter_context("matrix-post-stop")
    adapter._started = True
    await adapter.stop()

    await adapter._on_room_message(_matrix_room_event())

    assert inbound_collector.events == []
    assert adapter.diagnostics()["inbound_published"] == 0


async def test_lxmf_adapter_drops_delivery_state_callback_after_stop(
    make_adapter_context,
    caplog: pytest.LogCaptureFixture,
) -> None:
    adapter = LxmfAdapter(
        LxmfConfig(adapter_id="lxmf-delivery-post-stop", connection_type="fake")
    )
    await adapter.start(make_adapter_context("lxmf-delivery-post-stop"))
    await adapter.stop()

    assert adapter.ctx is not None
    assert adapter.diagnostics()["started"] is False

    caplog.clear()
    with caplog.at_level(logging.INFO):
        adapter._on_delivery_state("ab" * 16, "delivered")

    assert all(
        "delivery" not in record.getMessage().lower() for record in caplog.records
    )


async def test_lxmf_adapter_drops_simulate_inbound_after_stop(
    make_adapter_context,
    inbound_collector,
) -> None:
    """Real LxmfAdapter simulate_inbound drops events after stop()."""
    config = LxmfConfig(adapter_id="lxmf-sim-post-stop", connection_type="fake")
    adapter = LxmfAdapter(config)
    ctx = make_adapter_context("lxmf-sim-post-stop")
    await adapter.start(ctx)
    assert adapter._started
    await adapter.stop()
    assert not adapter._started
    # ctx is retained but _started gates simulate_inbound
    assert adapter.ctx is not None

    packet = _lxmf_packet("post-stop-lxmf", "should be dropped")
    await adapter.simulate_inbound(packet)

    assert inbound_collector.events == []
    assert adapter.diagnostics()["inbound_published"] == 0


async def test_meshcore_adapter_drops_simulate_inbound_after_stop(
    make_adapter_context,
    inbound_collector,
) -> None:
    """Real MeshCoreAdapter simulate_inbound drops events after stop()."""
    config = MeshCoreConfig(adapter_id="meshcore-sim-post-stop", connection_type="fake")
    adapter = MeshCoreAdapter(config)
    ctx = make_adapter_context("meshcore-sim-post-stop")
    await adapter.start(ctx)
    assert adapter._started
    await adapter.stop()
    assert not adapter._started
    assert adapter.ctx is not None

    packet = make_meshcore_packet(
        text="should be dropped",
        sender="test-sender",
        packet_id=99,
    )
    await adapter.simulate_inbound(packet)

    assert inbound_collector.events == []
    assert adapter.diagnostics()["inbound_published"] == 0
