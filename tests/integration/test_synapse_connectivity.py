"""Synapse connectivity integration tests for the MEDRE Matrix adapter.

These tests start a real Synapse homeserver in Docker, register a bot
user, create a test room, and exercise the MEDRE MatrixAdapter lifecycle
against it.  They are tagged ``pytest.mark.docker`` and excluded from
default runs.

Running locally::

    # Prerequisites: Docker running, MEDRE matrix extras installed
    pip install -e ".[matrix]"
    pytest tests/integration/test_synapse_connectivity.py -m docker -v

To include in a broader integration run::

    pytest tests/integration/ -m docker -v
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from medre.adapters.matrix.adapter import MatrixAdapter
from medre.adapters.matrix.compat import HAS_NIO
from medre.config.adapters.matrix import MatrixConfig
from medre.core.contracts.adapter import AdapterContext

from .conftest import SynapseEnvironment

logger = logging.getLogger(__name__)

# Re-apply module-level skip in case conftest skips don't cascade.
pytestmark = pytest.mark.docker

if not HAS_NIO:
    pytestmark = [
        pytest.mark.docker,
        pytest.mark.skip(
            reason="mindroom-nio not installed; run: pip install '.[matrix]'"
        ),
    ]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_matrix_config(env: SynapseEnvironment) -> MatrixConfig:
    """Build a MatrixConfig pointing at the Docker Synapse."""
    return MatrixConfig(
        adapter_id="synapse-integration",
        homeserver=env.base_url,
        user_id=env.bot_user_id,
        access_token=env.bot_access_token,
        room_allowlist={env.test_room_id},
        encryption_mode="plaintext",
    ).validate()


def _make_context(adapter_id: str = "synapse-integration") -> AdapterContext:
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


class TestSynapseConnectivity:
    """Connect MEDRE's MatrixAdapter to a Docker Synapse homeserver."""

    @pytest.mark.asyncio
    async def test_adapter_starts_against_synapse(
        self, synapse_env: SynapseEnvironment
    ) -> None:
        """The adapter can start and connect to a real Synapse."""
        config = _make_matrix_config(synapse_env)
        ctx = _make_context()
        adapter = MatrixAdapter(config)

        await adapter.start(ctx)
        try:
            info = await adapter.health_check()
            assert (
                info.health == "healthy"
            ), f"Expected healthy after start, got {info.health!r}"
            assert info.platform == "matrix"
        finally:
            await adapter.stop()

    @pytest.mark.asyncio
    async def test_health_check_reports_healthy(
        self, synapse_env: SynapseEnvironment
    ) -> None:
        """health_check() returns healthy when connected to Synapse."""
        config = _make_matrix_config(synapse_env)
        ctx = _make_context()
        adapter = MatrixAdapter(config)

        await adapter.start(ctx)
        try:
            info = await adapter.health_check()
            assert info.health == "healthy", f"Expected healthy, got {info.health!r}"
        finally:
            await adapter.stop()

    @pytest.mark.asyncio
    async def test_health_check_before_start_is_unknown(
        self, synapse_env: SynapseEnvironment
    ) -> None:
        """health_check() returns unknown before the adapter is started."""
        config = _make_matrix_config(synapse_env)
        adapter = MatrixAdapter(config)

        info = await adapter.health_check()
        assert (
            info.health == "unknown"
        ), f"Expected unknown before start, got {info.health!r}"

    @pytest.mark.asyncio
    async def test_send_text_message_to_synapse_room(
        self, synapse_env: SynapseEnvironment
    ) -> None:
        """Send a text message through the adapter to the test room."""
        from medre.core.rendering.renderer import RenderingResult

        config = _make_matrix_config(synapse_env)
        ctx = _make_context()
        adapter = MatrixAdapter(config)

        await adapter.start(ctx)
        try:
            ts = int(time.time())
            result = RenderingResult(
                event_id=f"int-smoke-{ts}",
                target_adapter="synapse-integration",
                target_channel=synapse_env.test_room_id,
                payload={
                    "msgtype": "m.text",
                    "body": f"MEDRE integration smoke test (ts={ts}) — safe to ignore",
                },
                metadata={"renderer": "matrix", "test": "docker-integration"},
            )
            delivery = await adapter.deliver(result)
            assert delivery is not None, "deliver() returned None"
            assert (
                delivery.native_message_id is not None
            ), "native_message_id is None — homeserver did not return event_id"
            assert delivery.native_message_id.startswith("$"), (
                f"Matrix event_id should start with '$', "
                f"got {delivery.native_message_id!r}"
            )
        finally:
            await adapter.stop()

    @pytest.mark.asyncio
    async def test_start_stop_idempotent(self, synapse_env: SynapseEnvironment) -> None:
        """start/stop can be called multiple times without error."""
        config = _make_matrix_config(synapse_env)
        ctx = _make_context()
        adapter = MatrixAdapter(config)

        await adapter.start(ctx)
        # Second start should be idempotent.
        await adapter.start(ctx)
        info = await adapter.health_check()
        assert info.health == "healthy"

        await adapter.stop()
        # Second stop should be idempotent.
        await adapter.stop()
        info = await adapter.health_check()
        assert info.health == "unknown"
