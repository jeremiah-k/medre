"""Smoke injection follows configured routes without a preferred platform."""

from types import SimpleNamespace

from medre.core.routing import Route, RouteSource, RouteTarget
from medre.runtime.smoke import _pick_source_adapter


def test_source_selection_uses_route_instead_of_platform() -> None:
    destination = SimpleNamespace(platform="matrix")
    source = SimpleNamespace(platform="community_private_chat")
    route = Route(
        id="private-to-destination",
        source=RouteSource(adapter="z-source", event_kinds=(), channel=None),
        targets=[RouteTarget(adapter="a-destination")],
    )
    app = SimpleNamespace(
        adapters={"a-destination": destination, "z-source": source},
        _registered_routes=(route,),
    )
    assert _pick_source_adapter(app) == ("z-source", source)
