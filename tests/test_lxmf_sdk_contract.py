"""Executable contract checks for the pinned LXMF and Reticulum SDKs.

These tests are excluded from the default suite and run in the dedicated
``lxmf_sdk`` CI job with ``medre[lxmf]`` installed.  They intentionally touch
real SDK classes so permissive fakes cannot hide constructor or state drift.
"""

from __future__ import annotations

import inspect
from importlib import import_module, metadata

import pytest

pytestmark = pytest.mark.lxmf_sdk


def _load_sdks() -> tuple[object, object]:
    """Import the pinned LXMF and RNS modules or fail this contract tier."""
    try:
        return import_module("LXMF"), import_module("RNS")
    except ImportError as exc:  # pragma: no cover - CI dependency contract
        pytest.fail(f"lxmf_sdk tier requires medre[lxmf]: {exc}")


def _destination_stub(rns: object, value: int) -> object:
    """Create a side-effect-free real ``RNS.Destination`` instance shell."""
    destination_type = getattr(rns, "Destination")
    destination = object.__new__(destination_type)
    destination.hash = bytes([value]) * 16
    return destination


def test_pinned_lxmf_and_rns_versions_are_exact() -> None:
    """The executable contract tier must test the versions MEDRE pins."""
    assert metadata.version("lxmf") == "1.1.1"
    assert metadata.version("rns") == "1.4.2"


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


def test_lxmessage_constructor_shape_is_frozen() -> None:
    """Pin the positional/keyword surface MEDRE uses for outbound messages."""
    lxmf, _ = _load_sdks()
    parameters = inspect.signature(lxmf.LXMessage).parameters
    assert tuple(parameters)[:6] == (
        "destination",
        "source",
        "content",
        "title",
        "fields",
        "desired_method",
    )


def test_router_identity_and_lookup_surfaces_match_session_usage() -> None:
    """Freeze every LXMF/RNS entry point used by the production session."""
    lxmf, rns = _load_sdks()

    router_params = inspect.signature(lxmf.LXMRouter).parameters
    assert "identity" in router_params
    assert "storagepath" in router_params

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

    registration_params = inspect.signature(
        lxmf.LXMRouter.register_delivery_identity
    ).parameters
    assert tuple(registration_params)[:4] == (
        "self",
        "identity",
        "display_name",
        "stamp_cost",
    )

    for name in ("from_file", "recall", "recall_app_data"):
        assert callable(getattr(rns.Identity, name, None)), name

    destination_params = inspect.signature(rns.Destination).parameters
    assert tuple(destination_params)[:4] == (
        "identity",
        "direction",
        "type",
        "app_name",
    )
    assert isinstance(rns.Destination.OUT, int)
    assert isinstance(rns.Destination.SINGLE, int)
    assert callable(getattr(lxmf, "display_name_from_app_data", None))


def test_lxmessage_delivery_states_match_medre_mapping() -> None:
    """Freeze the eight delivery states consumed by MEDRE diagnostics."""
    lxmf, _ = _load_sdks()
    assert {
        "GENERATING": lxmf.LXMessage.GENERATING,
        "OUTBOUND": lxmf.LXMessage.OUTBOUND,
        "SENDING": lxmf.LXMessage.SENDING,
        "SENT": lxmf.LXMessage.SENT,
        "DELIVERED": lxmf.LXMessage.DELIVERED,
        "REJECTED": lxmf.LXMessage.REJECTED,
        "CANCELLED": lxmf.LXMessage.CANCELLED,
        "FAILED": lxmf.LXMessage.FAILED,
    } == {
        "GENERATING": 0x00,
        "OUTBOUND": 0x01,
        "SENDING": 0x02,
        "SENT": 0x04,
        "DELIVERED": 0x08,
        "REJECTED": 0xFD,
        "CANCELLED": 0xFE,
        "FAILED": 0xFF,
    }


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

    registration_source = inspect.getsource(router.register_delivery_identity)
    assert "RNS.Destination" in registration_source
    assert "return delivery_destination" in registration_source
    announce_source = inspect.getsource(router.announce)
    assert "delivery_destinations" in announce_source
    init_source = inspect.getsource(router.__init__)
    assert "atexit.register(self.exit_handler)" in init_source
    assert "target=self.jobloop" in init_source
    exit_source = inspect.getsource(router.exit_handler)
    assert "exit_handler_running = True" in exit_source

    reticulum = rns.Reticulum
    assert callable(getattr(reticulum, "get_instance", None))
    assert callable(getattr(reticulum, "exit_handler", None))
    assert callable(getattr(reticulum, "sigint_handler", None))
    assert callable(getattr(reticulum, "sigterm_handler", None))
    # MEDRE deliberately does not call global Reticulum shutdown per session.
    assert not hasattr(reticulum, "stop")
