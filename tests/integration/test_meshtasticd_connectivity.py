"""Meshtasticd connectivity integration tests for the MEDRE Meshtastic adapter.

These tests start a real meshtasticd instance in Docker and exercise the
MEDRE MeshtasticAdapter lifecycle against it via TCP.  They are tagged
``pytest.mark.docker`` and excluded from default runs.

Running locally::

    # Prerequisites: Docker running, MEDRE meshtastic extras installed
    pip install -e ".[meshtastic]"
    pytest tests/integration/test_meshtasticd_connectivity.py -m docker -v

To include in a broader integration run::

    pytest tests/integration/ -m docker -v
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from medre.core.contracts.adapter import AdapterContext
from medre.adapters.meshtastic.adapter import MeshtasticAdapter
from medre.config.adapters.meshtastic import MeshtasticConfig
from medre.adapters.meshtastic.compat import HAS_MESHTASTIC

from .conftest import MeshtasticdEnvironment

logger = logging.getLogger(__name__)

# Re-apply module-level skip in case conftest skips don't cascade.
pytestmark = pytest.mark.docker

if not HAS_MESHTASTIC:
    pytestmark = [
        pytest.mark.docker,
        pytest.mark.skip(reason="mtjk not installed; run: pip install '.[meshtastic]'"),
    ]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_meshtastic_config(env: MeshtasticdEnvironment) -> MeshtasticConfig:
    """Build a MeshtasticConfig pointing at the Docker meshtasticd."""
    return MeshtasticConfig(
        adapter_id="meshtasticd-integration",
        connection_type="tcp",
        host=env.host,
        port=env.port,
        meshnet_name="MEDRE CI Mesh",
    ).validate()


def _make_context(adapter_id: str = "meshtasticd-integration") -> AdapterContext:
    """Build an AdapterContext wired to a mock publish_inbound."""
    return AdapterContext(
        adapter_id=adapter_id,
        event_bus=None,
        publish_inbound=AsyncMock(),
        logger=logging.getLogger(f"test.{adapter_id}"),
        clock=lambda: datetime.now(timezone.utc),
        shutdown_event=asyncio.Event(),
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestMeshtasticdConnectivity:
    """Connect MEDRE's MeshtasticAdapter to a Docker meshtasticd instance."""

    @pytest.mark.asyncio
    async def test_raw_tcp_interface_connects(
        self, meshtasticd_env: MeshtasticdEnvironment
    ) -> None:
        """Verify a raw TCPInterface can connect to the meshtasticd container.

        **Category A — Raw mtjk API smoke test.**

        Validates that ``mtjk`` is installed and can establish a TCP
        connection to the container.
        """
        import meshtastic
        import meshtastic.tcp_interface

        iface = meshtastic.tcp_interface.TCPInterface(
            hostname=meshtasticd_env.host,
            portNumber=meshtasticd_env.port,
        )
        try:
            await asyncio.get_event_loop().run_in_executor(
                None, lambda: iface.waitForConfig()
            )
            assert iface.isConnected.is_set(), "Interface should be connected"
        finally:
            await asyncio.get_event_loop().run_in_executor(None, iface.close)

    @pytest.mark.asyncio
    async def test_adapter_starts_and_reports_healthy(
        self, meshtasticd_env: MeshtasticdEnvironment
    ) -> None:
        """Start the adapter against meshtasticd and verify health_check.

        **Category B — MEDRE adapter lifecycle smoke test.**
        """
        config = _make_meshtastic_config(meshtasticd_env)
        adapter = MeshtasticAdapter(config)
        ctx = _make_context()

        await adapter.start(ctx)
        try:
            info = await adapter.health_check()
            assert info.health in ("healthy", "degraded", "unknown"), (
                f"Expected healthy/degraded/unknown, got: {info.health!r}"
            )
        finally:
            await adapter.stop()

    @pytest.mark.asyncio
    async def test_adapter_start_stop_idempotent(
        self, meshtasticd_env: MeshtasticdEnvironment
    ) -> None:
        """start/stop can be called multiple times without error."""
        config = _make_meshtastic_config(meshtasticd_env)
        adapter = MeshtasticAdapter(config)
        ctx = _make_context()

        await adapter.start(ctx)
        # Second start should be idempotent.
        await adapter.start(ctx)
        info = await adapter.health_check()
        assert info.health in ("healthy", "degraded", "unknown")

        await adapter.stop()
        # Second stop should be idempotent.
        await adapter.stop()
        info = await adapter.health_check()
        assert info.health == "unknown"

    @pytest.mark.asyncio
    async def test_adapter_diagnostics_exposes_session_state(
        self, meshtasticd_env: MeshtasticdEnvironment
    ) -> None:
        """diagnostics() returns meaningful session metadata when connected."""
        config = _make_meshtastic_config(meshtasticd_env)
        adapter = MeshtasticAdapter(config)
        ctx = _make_context()

        await adapter.start(ctx)
        try:
            diag = adapter.diagnostics()
            assert isinstance(diag, dict)
            # At minimum, the adapter should report its connection state.
            assert len(diag) > 0, "diagnostics() returned empty dict"
        finally:
            await adapter.stop()
