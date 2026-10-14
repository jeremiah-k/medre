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


def test_explicit_source_selection_is_route_aware() -> None:
    from medre.runtime.smoke import _eligible_smoke_sources

    app = SimpleNamespace(
        adapters={
            "matrix": SimpleNamespace(),
            "lxmf": SimpleNamespace(),
            "meshcore": SimpleNamespace(),
        },
        _registered_routes=(
            Route(
                id="matrix-to-lxmf",
                source=RouteSource(adapter="matrix", event_kinds=(), channel=None),
                targets=[RouteTarget(adapter="lxmf")],
            ),
            Route(
                id="meshcore-to-matrix",
                source=RouteSource(adapter="meshcore", event_kinds=(), channel=None),
                targets=[RouteTarget(adapter="matrix")],
            ),
        ),
    )
    assert _eligible_smoke_sources(app) == ["matrix", "meshcore"]
    assert _pick_source_adapter(app)[0] == "matrix"
    assert _pick_source_adapter(app, "meshcore")[0] == "meshcore"
    import pytest

    with pytest.raises(ValueError, match="not an enabled route source"):
        _pick_source_adapter(app, "lxmf")
