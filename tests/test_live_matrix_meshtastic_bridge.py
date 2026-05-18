"""Live Matrix↔Meshtastic bridge smoke tests.

These tests exercise the **adapter boundary** (start, health, diagnostics,
outbound delivery) against real Matrix homeservers and Meshtastic radio
nodes.  They are **not** full end-to-end bridge tests — that would require
automated Meshtastic→Matrix inbound verification which is not yet reliable.

**Skipped by default** — all tests require explicit opt-in via environment
variables.  See :mod:`tests.helpers.live_config` for the full list.

Running
-------

1. Set all ``MATRIX_*`` and ``MESHTASTIC_*`` environment variables
   (see :mod:`tests.helpers.live_config`).

2. Run the live bridge tests::

       pytest tests/test_live_matrix_meshtastic_bridge.py -m live -v

Test classes
------------

``TestLiveBridgeConfig``
    Config construction and TOML round-trip (requires all env vars).
``TestLiveBridgeDiagnostics``
    Adapter health and diagnostics (requires all env vars).
``TestMatrixToMeshtasticSmoke``
    Matrix outbound delivery smoke test (requires all env vars).
``TestMeshtasticToMatrix``
    Placeholder for Meshtastic→Matrix inbound (explicitly skipped).
"""

import asyncio
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from tests.helpers.live_config import (
    all_live_env_set,
    matrix_env_set,
    meshtastic_env_set,
    build_live_bridge_runtime_config,
    write_live_bridge_toml,
)

# ---------------------------------------------------------------------------
# Module-level marker — entire file tagged "live" so it is excluded by the
# default ``addopts = "-m 'not live'"`` in pyproject.toml.
# ---------------------------------------------------------------------------
pytestmark = pytest.mark.live

# ---------------------------------------------------------------------------
# Skip guards — mirrors the pattern in test_matrix_live.py and
# test_meshtastic_live.py.
# ---------------------------------------------------------------------------
require_live = pytest.mark.skipif(
    not all_live_env_set(),
    reason=(
        "Set all MATRIX_* and MESHTASTIC_* env vars to run live bridge tests"
    ),
)

require_matrix = pytest.mark.skipif(
    not matrix_env_set(),
    reason=(
        "Set MATRIX_HOMESERVER, MATRIX_USER_ID, MATRIX_ACCESS_TOKEN, "
        "and MATRIX_ROOM_ID env vars to run live Matrix tests"
    ),
)

require_meshtastic = pytest.mark.skipif(
    not meshtastic_env_set(),
    reason=(
        "Set MESHTASTIC_CONNECTION_TYPE (and type-specific vars) to run "
        "live Meshtastic tests"
    ),
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_matrix_context():
    """Build an AdapterContext for the Matrix side of the live bridge."""
    from medre.core.contracts.adapter import AdapterContext

    return AdapterContext(
        adapter_id="matrix",
        event_bus=None,
        publish_inbound=AsyncMock(),
        logger=logging.getLogger("test.live-bridge.matrix"),
        clock=lambda: datetime.now(timezone.utc),
        shutdown_event=asyncio.Event(),
    )


def _make_meshtastic_context():
    """Build an AdapterContext for the Meshtastic side of the live bridge."""
    from medre.core.contracts.adapter import AdapterContext

    return AdapterContext(
        adapter_id="radio",
        event_bus=None,
        publish_inbound=AsyncMock(),
        logger=logging.getLogger("test.live-bridge.meshtastic"),
        clock=lambda: datetime.now(timezone.utc),
        shutdown_event=asyncio.Event(),
    )


# ===========================================================================
# 1. Config construction and TOML round-trip
# ===========================================================================


@require_live
class TestLiveBridgeConfig:
    """Config construction and TOML round-trip tests.

    These tests verify that the live bridge config can be built from
    environment variables, written as TOML, and re-loaded successfully.
    """

    def test_config_builds_from_env(self, tmp_path: Path) -> None:
        """build_live_bridge_runtime_config succeeds with all env vars set."""
        config = build_live_bridge_runtime_config(tmp_path)
        assert config.runtime.name == "live-bridge-test"
        assert config.logging.level == "DEBUG"
        assert config.storage.backend == "sqlite"
        assert "matrix" in config.adapters.matrix
        assert "radio" in config.adapters.meshtastic
        assert len(config.routes.routes) == 2

    def test_config_toml_round_trip(self, tmp_path: Path) -> None:
        """Write TOML via write_live_bridge_toml → load_config → valid RuntimeConfig."""
        import tomllib
        from medre.config.loader import load_config

        toml_path = write_live_bridge_toml(tmp_path)

        # Verify the TOML is parseable.
        raw = toml_path.read_text(encoding="utf-8")
        data = tomllib.loads(raw)
        assert isinstance(data, dict)
        assert "adapters" in data
        assert "routes" in data

        # Load via the config loader.
        config, _source, _paths = load_config(str(toml_path))
        assert config.runtime.name == "live-bridge-test"
        assert config.adapters.matrix["matrix"].adapter_kind == "real"
        assert config.adapters.meshtastic["radio"].adapter_kind == "real"
        route_ids = [r.route_id for r in config.routes.routes]
        assert "matrix_to_radio" in route_ids
        assert "radio_to_matrix" in route_ids

    def test_example_live_config_has_required_routes(self) -> None:
        """Parse examples/configs/live-matrix-meshtastic.toml and verify routes exist."""
        import tomllib

        examples_path = Path(__file__).resolve().parent.parent / "examples" / "configs" / "live-matrix-meshtastic.toml"
        raw = examples_path.read_text(encoding="utf-8")
        data = tomllib.loads(raw)

        assert "routes" in data, "live-matrix-meshtastic.toml must have a [routes] section"
        routes = data["routes"]
        assert len(routes) >= 1, "Expected at least one route"

        # Verify each route has required fields.
        for route_id, route_table in routes.items():
            assert "source_adapters" in route_table, (
                f"Route {route_id!r} missing source_adapters"
            )
            assert "dest_adapters" in route_table, (
                f"Route {route_id!r} missing dest_adapters"
            )

        # Verify targeting fields on specific routes.
        assert "matrix_to_radio" in routes, "Expected 'matrix_to_radio' route"
        m2r = routes["matrix_to_radio"]
        assert "source_room" in m2r, (
            "Route 'matrix_to_radio' missing source_room targeting field"
        )
        assert "dest_channel" in m2r, (
            "Route 'matrix_to_radio' missing dest_channel targeting field"
        )

        assert "radio_to_matrix" in routes, "Expected 'radio_to_matrix' route"
        r2m = routes["radio_to_matrix"]
        assert "source_channel" in r2m, (
            "Route 'radio_to_matrix' missing source_channel targeting field"
        )
        assert "dest_room" in r2m, (
            "Route 'radio_to_matrix' missing dest_room targeting field"
        )

        # Verify Matrix adapter has room_allowlist.
        adapters = data.get("adapters", {})
        matrix_adapters = adapters.get("matrix", {})
        assert "matrix" in matrix_adapters, "Expected [adapters.matrix.matrix] section"
        matrix_cfg = matrix_adapters["matrix"]
        assert "room_allowlist" in matrix_cfg, (
            "Matrix adapter config missing room_allowlist"
        )


# ===========================================================================
# 2. Adapter health and diagnostics
# ===========================================================================


@require_live
class TestLiveBridgeDiagnostics:
    """Start real adapters and verify health + diagnostics.

    These tests exercise the adapter lifecycle boundary (start, health,
    diagnostics, stop) for both Matrix and Meshtastic adapters built from
    the live bridge config.
    """

    async def test_matrix_adapter_healthy(self, tmp_path: Path) -> None:
        """Start MatrixAdapter from live config, verify health=='healthy', stop."""
        from medre.adapters.matrix.adapter import MatrixAdapter

        config = build_live_bridge_runtime_config(tmp_path)
        matrix_rtc = config.adapters.matrix["matrix"]
        assert matrix_rtc.config is not None

        adapter = MatrixAdapter(matrix_rtc.config)
        ctx = _make_matrix_context()
        await adapter.start(ctx)
        try:
            info = await adapter.health_check()
            assert info.health == "healthy", (
                f"Expected healthy, got {info.health!r}"
            )
            assert info.platform == "matrix"
        finally:
            await adapter.stop()

    async def test_meshtastic_adapter_healthy(self, tmp_path: Path) -> None:
        """Start MeshtasticAdapter from live config, verify health, stop."""
        from medre.adapters.meshtastic.adapter import MeshtasticAdapter

        config = build_live_bridge_runtime_config(tmp_path)
        meshtastic_rtc = config.adapters.meshtastic["radio"]
        assert meshtastic_rtc.config is not None

        adapter = MeshtasticAdapter(meshtastic_rtc.config)
        ctx = _make_meshtastic_context()
        await adapter.start(ctx)
        try:
            info = await adapter.health_check()
            # Meshtastic may report "healthy" or "unknown" depending on
            # connection establishment timing.
            assert info.health in ("healthy", "unknown"), (
                f"Expected healthy or unknown, got {info.health!r}"
            )
            assert info.platform == "meshtastic"
        finally:
            await adapter.stop()

    async def test_diagnostics_report_adapter_state(self, tmp_path: Path) -> None:
        """Start both adapters and check diagnostics() returns expected keys."""
        from medre.adapters.matrix.adapter import MatrixAdapter
        from medre.adapters.meshtastic.adapter import MeshtasticAdapter

        config = build_live_bridge_runtime_config(tmp_path)

        matrix_rtc = config.adapters.matrix["matrix"]
        meshtastic_rtc = config.adapters.meshtastic["radio"]
        assert matrix_rtc.config is not None
        assert meshtastic_rtc.config is not None

        mx_adapter = MatrixAdapter(matrix_rtc.config)
        mesh_adapter = MeshtasticAdapter(meshtastic_rtc.config)

        mx_ctx = _make_matrix_context()
        mesh_ctx = _make_meshtastic_context()

        await mx_adapter.start(mx_ctx)
        await mesh_adapter.start(mesh_ctx)
        try:
            mx_diag = mx_adapter.diagnostics()
            assert "adapter_id" in mx_diag
            assert "platform" in mx_diag
            assert mx_diag["platform"] == "matrix"

            mesh_diag = mesh_adapter.diagnostics()
            assert "adapter_id" in mesh_diag
            assert "platform" in mesh_diag
            assert mesh_diag["platform"] == "meshtastic"
        finally:
            await mesh_adapter.stop()
            await mx_adapter.stop()


# ===========================================================================
# 3. Matrix→Meshtastic outbound smoke
# ===========================================================================


@require_live
class TestMatrixToMeshtasticSmoke:
    """Matrix outbound delivery smoke test.

    These tests exercise the Matrix adapter's ``deliver()`` method against
    a real homeserver.  Radio reception requires manual verification.
    """

    async def test_matrix_send_returns_delivery_result(self, tmp_path: Path) -> None:
        """Start Matrix adapter, send message, verify AdapterDeliveryResult returned.

        Tests Matrix outbound delivery only.  Radio reception requires
        manual verification.
        """
        from medre.adapters.matrix.adapter import MatrixAdapter
        from medre.core.rendering.renderer import RenderingResult

        config = build_live_bridge_runtime_config(tmp_path)
        matrix_rtc = config.adapters.matrix["matrix"]
        assert matrix_rtc.config is not None

        adapter = MatrixAdapter(matrix_rtc.config)
        ctx = _make_matrix_context()
        await adapter.start(ctx)
        try:
            ts = int(time.time())
            room_id = os.environ.get("MATRIX_ROOM_ID", "")
            result = RenderingResult(
                event_id=f"live-bridge-{ts}",
                target_adapter="matrix",
                target_channel=room_id,
                payload={
                    "msgtype": "m.text",
                    "body": (
                        f"MEDRE live bridge smoke test (ts={ts}) "
                        f"— safe to ignore"
                    ),
                },
                metadata={
                    "renderer": "matrix",
                    "test": "live-bridge-smoke",
                },
            )
            delivery = await adapter.deliver(result)
            assert delivery is not None, "deliver() returned None"
            assert delivery.native_message_id is not None, (
                "native_message_id is None — homeserver did not return event_id"
            )
            assert delivery.native_message_id.startswith("$"), (
                f"Matrix event_id should start with '$', "
                f"got {delivery.native_message_id!r}"
            )
        finally:
            await adapter.stop()


# ===========================================================================
# 4. Meshtastic→Matrix (placeholder, explicitly skipped)
# ===========================================================================


@pytest.mark.skip(
    reason="Meshtastic → Matrix automated inbound not yet reliable"
)
class TestMeshtasticToMatrix:
    """Placeholder for Meshtastic→Matrix inbound tests.

    Automated inbound testing from Meshtastic to Matrix is not yet
    reliable enough for CI.  Manual testing via the operator runbook
    is recommended.

    See ``docs/runbooks/operational-evidence.md`` for manual test
    procedures.
    """

    def test_meshtastic_inbound_to_matrix(self) -> None:
        """Manual test placeholder — see runbook for manual procedures.

        To test Meshtastic→Matrix inbound manually:

        1. Start the MEDRE runtime with a live bridge config.
        2. Send a text message from another Meshtastic node on the
           configured channel.
        3. Verify the message appears in the target Matrix room.

        This test is a ``pass`` statement because the automated path
        is not yet reliable.
        """
        # Meshtastic → Matrix inbound delivery requires a second radio
        # node sending a message on the configured channel and the
        # runtime pipeline forwarding it.  This is exercised manually
        # via the operator runbook.
        pass
