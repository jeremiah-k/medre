"""Env-override round-trip preserves route origin labels.

Regression tests for a bug in :func:`apply_route_overrides`: when an
existing TOML route was overridden via ``MEDRE_ROUTE__<TOKEN>__<FIELD>``
env vars, the override path rebuilt the route through
:meth:`RouteConfig.from_dict` and carried forward complex fields
(``channel_room_map``, ``policy``, ``retry``) but **dropped**
``source_origin_label`` and ``dest_origin_label``.

Origin labels are not settable via env vars (they are not in the
supported field list), so they must survive the override round-trip
exactly as declared in TOML.  Dropping them silently reset relay-prefix
attribution for overridden routes.

These tests construct a base config with a labelled route, override an
unrelated field (``enabled``) via env, and assert both labels survive.
They also cover the partial cases (only one label set) and the sentinel
states (``None`` stays ``None``; ``""`` stays ``""``).
"""

from __future__ import annotations

import os

import pytest

from medre.config.env import apply_env_overrides
from medre.config.model import (
    AdapterConfigSet,
    LoggingConfig,
    RuntimeConfig,
    RuntimeOptions,
    StorageConfig,
)
from medre.config.routes import (
    RouteConfig,
    RouteConfigSet,
    RouteDirectionality,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove all MEDRE_* env vars between tests."""
    for key in list(os.environ.keys()):
        if key.startswith("MEDRE_"):
            monkeypatch.delenv(key, raising=False)


def _config_with_route(
    *,
    source_origin_label: str | None,
    dest_origin_label: str | None,
) -> RuntimeConfig:
    """RuntimeConfig with one TOML route carrying the given origin labels."""
    route = RouteConfig(
        route_id="toml-route",
        source_adapters=("adapter-a",),
        dest_adapters=("adapter-b",),
        directionality=RouteDirectionality.SOURCE_TO_DEST,
        enabled=True,
        source_origin_label=source_origin_label,
        dest_origin_label=dest_origin_label,
    )
    return RuntimeConfig(
        runtime=RuntimeOptions(name="test"),
        logging=LoggingConfig(level="INFO"),
        storage=StorageConfig(backend="sqlite", path="/tmp/test.db"),
        adapters=AdapterConfigSet(),
        routes=RouteConfigSet(routes=(route,)),
    )


def _override_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """Set a benign env override that triggers the override round-trip path."""
    monkeypatch.setenv("MEDRE_ROUTE__TOML_ROUTE__ENABLED", "false")


# ===========================================================================
# Regression: overriding an existing route must preserve origin labels
# ===========================================================================


class TestEnvOverridePreservesOriginLabels:
    """Overriding an existing TOML route via env must not drop its
    source_origin_label / dest_origin_label."""

    def test_both_labels_preserved_on_override(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _override_enabled(monkeypatch)
        base = _config_with_route(
            source_origin_label="East Net",
            dest_origin_label="West Net",
        )
        result = apply_env_overrides(base)

        assert len(result.routes.routes) == 1
        route = result.routes.routes[0]
        assert route.enabled is False
        assert route.source_origin_label == "East Net"
        assert route.dest_origin_label == "West Net"

    def test_only_source_label_preserved(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _override_enabled(monkeypatch)
        base = _config_with_route(
            source_origin_label="East Net",
            dest_origin_label=None,
        )
        result = apply_env_overrides(base)

        route = result.routes.routes[0]
        assert route.source_origin_label == "East Net"
        assert route.dest_origin_label is None

    def test_only_dest_label_preserved(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _override_enabled(monkeypatch)
        base = _config_with_route(
            source_origin_label=None,
            dest_origin_label="West Net",
        )
        result = apply_env_overrides(base)

        route = result.routes.routes[0]
        assert route.source_origin_label is None
        assert route.dest_origin_label == "West Net"

    def test_empty_string_labels_preserved(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Explicit '' labels (suppress adapter fallback) must survive."""
        _override_enabled(monkeypatch)
        base = _config_with_route(
            source_origin_label="",
            dest_origin_label="",
        )
        result = apply_env_overrides(base)

        route = result.routes.routes[0]
        # Empty string must stay empty string — not None.
        assert route.source_origin_label == ""
        assert route.dest_origin_label == ""

    def test_no_labels_stay_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Routes without labels keep None after override."""
        _override_enabled(monkeypatch)
        base = _config_with_route(
            source_origin_label=None,
            dest_origin_label=None,
        )
        result = apply_env_overrides(base)

        route = result.routes.routes[0]
        assert route.source_origin_label is None
        assert route.dest_origin_label is None


class TestEnvOverridePreservesOriginLabelsWithComplexFields:
    """Origin labels survive the override round-trip alongside other complex
    fields (channel_room_map, policy, retry) that were already preserved."""

    def test_labels_and_complex_fields_all_preserved(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from medre.config.routes import BridgePolicy, RouteRetryConfig

        route = RouteConfig(
            route_id="toml-route",
            source_adapters=("adapter-a",),
            dest_adapters=("adapter-b",),
            directionality=RouteDirectionality.SOURCE_TO_DEST,
            enabled=True,
            channel_room_map={"0": "!room0:matrix.org"},
            policy=BridgePolicy(allowed_event_types=("message",)),
            retry=RouteRetryConfig(enabled=True, max_attempts=5),
            source_origin_label="Source",
            dest_origin_label="Dest",
        )
        base = RuntimeConfig(
            runtime=RuntimeOptions(name="test"),
            logging=LoggingConfig(level="INFO"),
            storage=StorageConfig(backend="sqlite", path="/tmp/test.db"),
            adapters=AdapterConfigSet(),
            routes=RouteConfigSet(routes=(route,)),
        )

        _override_enabled(monkeypatch)
        result = apply_env_overrides(base)

        route = result.routes.routes[0]
        assert route.enabled is False
        # All preserved complex fields.
        assert route.channel_room_map == {"0": "!room0:matrix.org"}
        assert route.policy is not None
        assert route.policy.allowed_event_types == ("message",)
        assert route.retry is not None
        assert route.retry.max_attempts == 5
        # And the origin labels.
        assert route.source_origin_label == "Source"
        assert route.dest_origin_label == "Dest"
