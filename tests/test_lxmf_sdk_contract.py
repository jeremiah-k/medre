"""Executable contract checks for the pinned LXMF and Reticulum SDKs.

These tests are excluded from the default suite and run in the dedicated
``lxmf_sdk`` CI job with ``medre[lxmf]`` installed.  They intentionally touch
real SDK classes so permissive fakes cannot hide constructor or state drift.
"""

from __future__ import annotations

import inspect
from importlib import import_module

import pytest

from tests.helpers.sdk_contract import assert_installed_extra_matches_declared_pins

pytestmark = pytest.mark.lxmf_sdk


def _load_sdks() -> tuple[object, object]:
    """Import the pinned LXMF and RNS modules or fail this contract tier."""
    try:
        return import_module("LXMF"), import_module("RNS")
    except ImportError as exc:  # pragma: no cover - CI dependency contract
        pytest.fail(f"lxmf_sdk tier requires medre[lxmf]: {exc}")


def _destination_stub(rns: object, value: int) -> object:
    """Create a side-effect-free real ``RNS.Destination`` instance shell."""
    destination_type = rns.Destination
    destination = object.__new__(destination_type)
    destination.hash = bytes([value]) * 16
    return destination


def test_installed_lxmf_and_rns_match_declared_extra() -> None:
    """The contract tier executes against MEDRE's current declared SDK pins."""
    assert_installed_extra_matches_declared_pins("lxmf", ("lxmf", "rns"))


def test_lxmessage_requires_rns_destination_source() -> None:
    """LXMessage accepts a real Destination source and rejects a router/object."""
    lxmf, rns = _load_sdks()
    destination = _destination_stub(rns, 0x11)
    source = _destination_stub(rns, 0x22)

    message = lxmf.LXMessage(destination, source, "lxmf-sdk-contract")
    assert message.destination_hash == destination.hash
    assert message.source_hash == source.hash

    with pytest.raises(ValueError, match="invalid source"):
        lxmf.LXMessage(destination, object(), "invalid-source")


def test_lxmessage_constructor_accepts_medre_call_shape() -> None:
    """The constructor accepts the positional/keyword shape MEDRE actually uses."""
    lxmf, _ = _load_sdks()
    signature = inspect.signature(lxmf.LXMessage)
    signature.bind(
        object(),
        object(),
        "content",
        title="title",
        fields={},
        desired_method=object(),
    )


def test_router_identity_and_lookup_surfaces_match_session_usage() -> None:
    """Freeze every LXMF/RNS entry point used by the production session."""
    lxmf, rns = _load_sdks()

    # Full ``bind`` (not ``bind_partial``): the MEDRE call shape must also
    # supply every required parameter, so an SDK that adds a required argument
    # fails this contract instead of production with a ``TypeError``.
    router_signature = inspect.signature(lxmf.LXMRouter)
    router_signature.bind(identity=object(), storagepath="/tmp/medre-sdk-contract")

    for name in (
        "register_delivery_identity",
        "register_delivery_callback",
        "handle_outbound",
        "announce",
        "set_outbound_propagation_node",
        "get_outbound_propagation_node",
        "exit_handler",
    ):
        assert callable(getattr(lxmf.LXMRouter, name, None)), name

    registration_signature = inspect.signature(
        lxmf.LXMRouter.register_delivery_identity
    )
    registration_signature.bind(
        object(), object(), display_name="MEDRE", stamp_cost=None
    )

    for name in ("from_file", "recall", "recall_app_data"):
        assert callable(getattr(rns.Identity, name, None)), name

    assert isinstance(rns.Destination.OUT, int)
    assert isinstance(rns.Destination.SINGLE, int)
    inspect.signature(rns.Destination).bind(
        object(),
        rns.Destination.OUT,
        rns.Destination.SINGLE,
        "lxmf",
        "delivery",
    )
    assert callable(getattr(lxmf, "display_name_from_app_data", None))


def test_lxmessage_delivery_states_support_medre_mapping() -> None:
    """The named states and callbacks MEDRE uses remain available."""
    lxmf, _ = _load_sdks()
    assert callable(getattr(lxmf.LXMessage, "register_delivery_callback", None))
    assert callable(getattr(lxmf.LXMessage, "register_failed_callback", None))
    names = (
        "GENERATING",
        "OUTBOUND",
        "SENDING",
        "SENT",
        "DELIVERED",
        "REJECTED",
        "CANCELLED",
        "FAILED",
    )
    values = [getattr(lxmf.LXMessage, name) for name in names]
    assert all(isinstance(value, int) for value in values)
    assert len(set(values)) == len(values)


def test_router_and_reticulum_lifecycle_surfaces_exist() -> None:
    """Pin identity registration, announce, propagation, and shutdown surfaces."""
    lxmf, rns = _load_sdks()
    router = lxmf.LXMRouter
    assert callable(getattr(router, "register_delivery_identity", None))
    assert callable(getattr(router, "register_delivery_callback", None))
    assert callable(getattr(router, "announce", None))
    assert callable(getattr(router, "set_outbound_propagation_node", None))
    assert callable(getattr(router, "get_outbound_propagation_node", None))
    assert callable(getattr(router, "exit_handler", None))

    transport = rns.Transport
    assert callable(getattr(transport, "deregister_destination", None))
    assert callable(getattr(transport, "deregister_announce_handler", None))
    assert isinstance(getattr(transport, "announce_handlers", None), list)

    handlers = import_module("LXMF.Handlers")
    router_owner = object()
    delivery_handler = handlers.LXMFDeliveryAnnounceHandler(router_owner)
    propagation_handler = handlers.LXMFPropagationAnnounceHandler(router_owner)
    assert delivery_handler.lxmrouter is router_owner
    assert propagation_handler.lxmrouter is router_owner

    reticulum = rns.Reticulum
    assert callable(getattr(reticulum, "get_instance", None))
    assert callable(getattr(reticulum, "exit_handler", None))
    assert callable(getattr(reticulum, "sigint_handler", None))
    assert callable(getattr(reticulum, "sigterm_handler", None))
    # MEDRE deliberately does not call global Reticulum shutdown per session.
    assert not hasattr(reticulum, "stop")


@pytest.mark.parametrize(
    ("size", "method"),
    [(286, "OPPORTUNISTIC"), (287, "OPPORTUNISTIC"), (288, "DIRECT"), (295, "DIRECT")],
)
def test_opportunistic_pack_respects_the_encrypted_packet_boundary(
    size: int, method: str,
) -> None:
    """Content above the encrypted packet budget falls back to link delivery."""
    lxmf, rns = _load_sdks()
    destination = _destination_stub(rns, 0x11)
    destination.type = rns.Destination.SINGLE
    source = _destination_stub(rns, 0x22)
    source.sign = rns.Identity().sign
    message = lxmf.LXMessage(
        destination,
        source,
        "x" * size,
        desired_method=lxmf.LXMessage.OPPORTUNISTIC,
    )
    message.pack()
    assert message.method == getattr(lxmf.LXMessage, method)
    assert message.content == b"x" * size


async def test_rendered_title_and_envelope_count_toward_opportunistic_fallback(
    sample_event,
) -> None:
    """MEDRE's metadata overhead changes delivery method without losing payload."""
    from msgspec.structs import replace

    from medre.adapters.lxmf.fields import FIELD_MEDRE_ENVELOPE, LXMF_NAMESPACE
    from medre.adapters.lxmf.renderer import LxmfRenderer
    from medre.core.rendering import RenderingContext

    lxmf, rns = _load_sdks()
    event = replace(sample_event, payload={"body": "x" * 200, "title": "reply title"})
    rendered = await LxmfRenderer(metadata_embedding=True).render(
        event,
        RenderingContext(
            target_adapter="lxmf-contract",
            target_platform="lxmf",
            delivery_strategy="direct",
            max_text_chars=16384,
        ),
    )
    destination = _destination_stub(rns, 0x11)
    destination.type = rns.Destination.SINGLE
    source = _destination_stub(rns, 0x22)
    source.sign = rns.Identity().sign

    def pack(fields):
        message = lxmf.LXMessage(
            destination,
            source,
            rendered.payload["content"],
            title=rendered.payload["title"],
            fields=fields,
            desired_method=lxmf.LXMessage.OPPORTUNISTIC,
        )
        message.pack()
        return message

    plain = pack({})
    embedded = pack(rendered.payload["fields"])
    assert plain.method == lxmf.LXMessage.OPPORTUNISTIC
    assert embedded.method == lxmf.LXMessage.DIRECT
    assert embedded.content == plain.content == b"x" * 200
    assert embedded.title == plain.title == b"reply title"
    envelope = embedded.fields[FIELD_MEDRE_ENVELOPE][LXMF_NAMESPACE]
    assert envelope["event_id"] == event.event_id
    # Inspect the actual wire payload, not only the pre-pack fields dict.
    msgpack = import_module("RNS.vendor.umsgpack")
    header_length = (
        lxmf.LXMessage.DESTINATION_LENGTH * 2 + lxmf.LXMessage.SIGNATURE_LENGTH
    )
    payload = msgpack.unpackb(embedded.packed[header_length:])
    assert payload[1:] == [embedded.title, embedded.content, embedded.fields]
