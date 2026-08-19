"""Executable contract checks for the pinned mtjk Meshtastic SDK."""

from __future__ import annotations

import inspect
from importlib import import_module, metadata

import pytest

from medre.config.adapters.meshtastic import MeshtasticConfig

pytestmark = pytest.mark.meshtastic_sdk


def _load_sdk() -> tuple[object, object, object, object, object]:
    """Import the pinned Meshtastic modules or fail this contract tier."""
    try:
        mesh_interface = import_module("meshtastic.mesh_interface")
        mesh_pb2 = import_module("meshtastic.protobuf.mesh_pb2")
        portnums_pb2 = import_module("meshtastic.protobuf.portnums_pb2")
        receive_pipeline = import_module(
            "meshtastic.mesh_interface_runtime.receive_pipeline"
        )
        pub = import_module("pubsub.pub")
    except ImportError as exc:  # pragma: no cover - CI dependency contract
        pytest.fail(f"meshtastic_sdk tier requires medre[meshtastic]: {exc}")
    return mesh_interface, mesh_pb2, portnums_pb2, receive_pipeline, pub


def test_pinned_mtjk_version_is_exact() -> None:
    """The contract tier must execute against MEDRE's exact mtjk pin."""
    assert metadata.version("mtjk") == "2.7.11.post5"
    assert metadata.version("PyPubSub") == "4.0.7"


def test_private_send_surfaces_and_sync_semantics_are_frozen() -> None:
    """Pin the synchronous private/public methods MEDRE wraps in threads."""
    mesh_interface, _, _, _, _ = _load_sdk()
    interface_type = mesh_interface.MeshInterface

    for name in ("sendText", "_sendPacket", "_generatePacketId", "close"):
        method = getattr(interface_type, name, None)
        assert callable(method), f"MeshInterface.{name} is required by MEDRE"
        assert not inspect.iscoroutinefunction(method)

    send_packet = inspect.signature(interface_type._sendPacket).parameters
    assert tuple(send_packet)[:4] == (
        "self",
        "meshPacket",
        "destinationId",
        "wantAck",
    )
    assert send_packet["wantAck"].default is False


def test_generated_packet_ids_are_real_uint32_ids() -> None:
    """Exercise the SDK's packet-id generator without opening a transport."""
    mesh_interface, _, _, _, _ = _load_sdk()
    interface = mesh_interface.MeshInterface(noProto=True)
    try:
        first = interface._generatePacketId()
        second = interface._generatePacketId()
    finally:
        interface.close()

    assert isinstance(first, int)
    assert 0 <= first <= 0xFFFFFFFF
    assert isinstance(second, int)
    assert 0 <= second <= 0xFFFFFFFF
    assert first != second


def test_reply_reaction_protobuf_fields_and_text_port_are_present() -> None:
    """Pin Data.reply_id/Data.emoji and TEXT_MESSAGE_APP used by structured sends."""
    _, mesh_pb2, portnums_pb2, _, _ = _load_sdk()
    fields = mesh_pb2.Data.DESCRIPTOR.fields_by_name
    assert {"portnum", "payload", "reply_id", "emoji"}.issubset(fields)
    assert int(portnums_pb2.PortNum.TEXT_MESSAGE_APP) > 0


def test_text_byte_budget_stays_below_sdk_payload_limit() -> None:
    """MEDRE's final UTF-8 budget must fit the SDK's decoded-data payload cap."""
    _, mesh_pb2, _, _, _ = _load_sdk()
    sdk_payload_limit = int(mesh_pb2.Constants.DATA_PAYLOAD_LEN)
    assert sdk_payload_limit == 233
    config = MeshtasticConfig(adapter_id="sdk-contract")
    assert config.max_text_bytes == 227
    assert config.max_text_bytes <= sdk_payload_limit


def test_pubsub_topics_and_close_contract_remain_visible_in_sdk_source() -> None:
    """Freeze the background callback topics MEDRE subscribes/unsubscribes."""
    mesh_interface, _, _, receive_pipeline, pub = _load_sdk()
    interface_source = inspect.getsource(mesh_interface.MeshInterface)
    receive_source = inspect.getsource(receive_pipeline.ReceivePipeline)
    assert '"meshtastic.receive"' in receive_source
    assert '"meshtastic.connection.lost"' in interface_source
    assert callable(pub.subscribe)
    assert callable(pub.unsubscribe)
    assert not inspect.iscoroutinefunction(pub.subscribe)
    assert not inspect.iscoroutinefunction(pub.unsubscribe)
