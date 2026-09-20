"""Structured LXMF destination: config → route target → render → delivery.

Proves the one documented addressing rule end-to-end: a route target may
carry a structured ``RouteDestination`` (the normative identity/hash
addressing form, routing-delivery §2.3/§2.4) configured through
``routes.<id>.dest_destination``; the LXMF renderer addresses the payload
from that destination, falling back to the transport-defined
``dest_channel`` selector when no structured destination is set.  An
empty destination is never accepted as success: the delivery boundary
fails permanently and actionably.
"""

from __future__ import annotations

import pytest

from medre.adapters.lxmf.renderer import LxmfRenderer
from medre.config.errors import ConfigValidationError
from medre.config.routes import RouteConfig, RouteDestinationConfig
from medre.core.routing.models import RouteDestination
from tests.helpers.rendering_evidence import make_context
from tests.helpers.rendering_evidence import make_event as _make_event

_VALID_HASH = "e5f6a7b8c9d0e1f2a1b2c3d4e5f6a7b8"


def _route_dict(**overrides: object) -> dict[str, object]:
    data: dict[str, object] = {
        "source_adapters": ["matrix-home"],
        "dest_adapters": ["lxmf-node-a"],
        "dest_destination": {
            "kind": "lxmf_destination",
            "destination_hash": _VALID_HASH,
            "destination_name": "mobile-peer-1",
        },
    }
    data.update(overrides)
    return data


class TestRouteDestinationConfigParsing:
    """``routes.<id>.dest_destination`` parses and validates."""

    def test_structured_destination_parses(self) -> None:
        rc = RouteConfig.from_dict("matrix-to-lxmf", _route_dict())
        assert rc.dest_destination == RouteDestinationConfig(
            kind="lxmf_destination",
            destination_hash=_VALID_HASH,
            destination_name="mobile-peer-1",
        )

    def test_unknown_kind_rejected(self) -> None:
        data = _route_dict(
            dest_destination={"kind": "smoke_signal", "destination_hash": _VALID_HASH}
        )
        with pytest.raises(ConfigValidationError, match="kind"):
            RouteConfig.from_dict("matrix-to-lxmf", data)

    def test_lxmf_destination_requires_32_hex_hash(self) -> None:
        data = _route_dict(
            dest_destination={"kind": "lxmf_destination", "destination_hash": "zz"}
        )
        with pytest.raises(ConfigValidationError, match="destination_hash"):
            RouteConfig.from_dict("matrix-to-lxmf", data)

    def test_lxmf_destination_hash_must_be_hex(self) -> None:
        data = _route_dict(
            dest_destination={
                "kind": "lxmf_destination",
                "destination_hash": "z" * 32,
            }
        )
        with pytest.raises(ConfigValidationError, match="destination_hash"):
            RouteConfig.from_dict("matrix-to-lxmf", data)

    def test_destination_conflicts_with_dest_channel(self) -> None:
        data = _route_dict(dest_channel="general")
        with pytest.raises(ConfigValidationError, match="dest_channel"):
            RouteConfig.from_dict("matrix-to-lxmf", data)

    def test_destination_conflicts_with_dest_room(self) -> None:
        data = _route_dict(dest_room="general")
        with pytest.raises(ConfigValidationError, match="dest_room"):
            RouteConfig.from_dict("matrix-to-lxmf", data)

    def test_destination_conflicts_with_channel_room_map(self) -> None:
        data = _route_dict(
            source_adapters=["radio"],
            channel_room_map={"0": {"room": "!abc:example.org"}},
        )
        with pytest.raises(ConfigValidationError, match="channel_room_map"):
            RouteConfig.from_dict("matrix-to-lxmf", data)

    def test_channel_kind_requires_name_and_rejects_hash(self) -> None:
        with pytest.raises(ConfigValidationError, match="destination_name"):
            RouteConfig.from_dict(
                "matrix-to-lxmf",
                _route_dict(dest_destination={"kind": "channel"}),
            )
        with pytest.raises(ConfigValidationError, match="destination_hash"):
            RouteConfig.from_dict(
                "matrix-to-lxmf",
                _route_dict(
                    dest_destination={
                        "kind": "channel",
                        "destination_name": "general",
                        "destination_hash": _VALID_HASH,
                    }
                ),
            )

    def test_meshcore_contact_requires_hash_or_name(self) -> None:
        with pytest.raises(ConfigValidationError, match="meshcore_contact"):
            RouteConfig.from_dict(
                "matrix-to-lxmf",
                _route_dict(dest_destination={"kind": "meshcore_contact"}),
            )

    def test_dest_channel_hash_selector_still_supported(self) -> None:
        """The simple transport-defined selector keeps parsing unchanged."""
        rc = RouteConfig.from_dict(
            "matrix-to-lxmf",
            {
                "source_adapters": ["matrix-home"],
                "dest_adapters": ["lxmf-node-a"],
                "dest_channel": _VALID_HASH,
            },
        )
        assert rc.dest_channel == _VALID_HASH
        assert rc.dest_destination is None


class TestRouteExpansionCarriesDestination:
    """Config destination reaches the canonical ``RouteTarget.destination``."""

    def test_forward_leg_targets_carry_destination(self) -> None:
        from medre.runtime.route_engine import _expand_route_config

        rc = RouteConfig.from_dict("matrix-to-lxmf", _route_dict())
        routes = _expand_route_config(rc)
        assert len(routes) == 1
        (target,) = routes[0].targets
        assert target.destination == RouteDestination(
            kind="lxmf_destination",
            destination_hash=_VALID_HASH,
            destination_name="mobile-peer-1",
        )
        assert target.channel is None

    def test_reverse_leg_targets_carry_no_destination(self) -> None:
        from medre.runtime.route_engine import _expand_route_config

        rc = RouteConfig.from_dict(
            "matrix-to-lxmf",
            _route_dict(directionality="bidirectional"),
        )
        forward, reverse = (
            _expand_route_config(rc, swap_direction=False)[0],
            _expand_route_config(rc, swap_direction=True)[0],
        )
        assert forward.targets[0].destination is not None
        assert reverse.targets[0].destination is None


class TestRendererDestinationPrecedence:
    """One authority: structured destination, else channel selector, else ''."""

    def _render_destination_hash(self, ctx_kwargs: dict) -> str:
        import asyncio

        ctx = make_context(target_platform="lxmf", **ctx_kwargs)
        result = asyncio.run(LxmfRenderer().render(_make_event(), ctx))
        return str(result.payload["destination_hash"])

    def test_structured_destination_wins(self) -> None:
        destination_hash = self._render_destination_hash(
            {
                "target_adapter": "lxmf-node-a",
                "target_channel": "not-a-hash",
                "target_destination": RouteDestination(
                    kind="lxmf_destination",
                    destination_hash=_VALID_HASH,
                    destination_name=None,
                ),
            }
        )
        assert destination_hash == _VALID_HASH

    def test_channel_selector_used_without_destination(self) -> None:
        destination_hash = self._render_destination_hash(
            {
                "target_adapter": "lxmf-node-a",
                "target_channel": _VALID_HASH,
            }
        )
        assert destination_hash == _VALID_HASH

    def test_neither_destination_nor_channel_is_empty(self) -> None:
        destination_hash = self._render_destination_hash(
            {
                "target_adapter": "lxmf-node-a",
            }
        )
        assert destination_hash == ""

    def test_channel_destination_kind_falls_back_to_channel(self) -> None:
        """A ``channel``-kind destination carries no hash for LXMF."""
        destination_hash = self._render_destination_hash(
            {
                "target_adapter": "lxmf-node-a",
                "target_channel": "general",
                "target_destination": RouteDestination(
                    kind="channel",
                    destination_hash=None,
                    destination_name="general",
                ),
            }
        )
        assert destination_hash == "general"


class TestPipelineThreadsDestinationToRenderer:
    """The delivery pipeline hands ``target.destination`` to renderers."""

    def test_render_passes_destination_into_context(self) -> None:
        import asyncio

        from medre.core.rendering.renderer import RenderingPipeline

        seen: dict = {}

        class RecordingRenderer(LxmfRenderer):
            async def render(self, event, ctx):  # type: ignore[override]
                seen["destination"] = ctx.target_destination
                seen["channel"] = ctx.target_channel
                return await super().render(event, ctx)

        pipeline = RenderingPipeline()
        pipeline.register(RecordingRenderer())
        pipeline.register_adapter_platform("lxmf-node-a", "lxmf")
        destination = RouteDestination(
            kind="lxmf_destination",
            destination_hash=_VALID_HASH,
            destination_name=None,
        )
        result = asyncio.run(
            pipeline.render(
                _make_event(),
                "lxmf-node-a",
                None,
                target_platform="lxmf",
                target_destination=destination,
            )
        )
        assert seen["destination"] is destination
        assert result.payload["destination_hash"] == _VALID_HASH


class TestDeliveryBoundaryOnStructuredDestination:
    """The fake-transport delivery boundary consumes the rendered hash."""

    async def test_deliver_uses_structured_destination_hash(self) -> None:
        from medre.adapters.fakes.lxmf import FakeLxmfAdapter

        adapter = FakeLxmfAdapter()
        ctx = make_context(
            target_adapter="lxmf-node-a",
            target_platform="lxmf",
            target_destination=RouteDestination(
                kind="lxmf_destination",
                destination_hash=_VALID_HASH,
                destination_name=None,
            ),
        )
        result = await LxmfRenderer().render(_make_event("evt-dest-1"), ctx)
        delivery = await adapter.deliver(result)
        assert delivery is not None
        # The adapter records the pre-rendered payload it accepted; the
        # recipient hash inside it is what the transport client sent to.
        (accepted,) = adapter.delivered_payloads
        assert accepted.payload["destination_hash"] == _VALID_HASH
        assert adapter._fake_client.sent_messages[-1]["destination_hash"] == (
            _VALID_HASH
        )
