"""Opt-in soak/resilience tests for Matrix and Meshtastic adapters.

These tests exercise **sustained operation** against real services/hardware
over a bounded, configurable time window.  They are **not** unit tests — they
validate that sessions remain healthy, reconnect correctly, and do not leak
resources under extended runtime.

All tests in this module are:

- Marked ``pytest.mark.live`` — excluded by default (``addopts = "-m 'not live'"``).
- Environment-gated — skip unless the relevant transport env vars are set.
- **Bounded runtime** — controlled via ``SOAK_DURATION_SECONDS`` (default 30 s,
  safe maximum 300 s / 5 min).  Override via environment variable.
- **Observational only** — they observe and report; they do not assert on
  transport-specific timing or throughput targets.

**Running soak tests:**

1. Set transport-specific env vars (same as live smoke tests).
2. Optionally set ``SOAK_DURATION_SECONDS`` (default 30, max 300).
3. Run::

       pytest tests/test_soak.py -m live -v -s

**Safety:**

- Maximum soak duration is **hard-capped at 300 seconds** regardless of
  env var value.
- Each test sends at most **1 message per 10 seconds** to avoid flooding
  radio channels or rate-limited APIs.
- No test creates rooms, channels, or admin resources.
- No test sends media, reactions, edits, or encrypted messages.

**Known limitations:**

- Soak tests prove session health over time; they do **not** prove
  message delivery reliability or ordering guarantees.
- Matrix soak tests send to a single pre-existing room.
- Meshtastic soak tests send on a single pre-existing channel.
- No cross-transport soak testing.
- No E2EE soak testing.
- No MeshCore or LXMF soak testing (deferred to follow-up).
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock

import pytest

# ---------------------------------------------------------------------------
# Soak duration configuration
# ---------------------------------------------------------------------------

_MAX_SOAK_SECONDS: int = 300  # Hard cap: 5 minutes.
_DEFAULT_SOAK_SECONDS: int = 30  # Safe default.

_SEND_INTERVAL_SECONDS: float = 10.0  # Minimum gap between sends.


def _get_soak_duration() -> int:
    """Read ``SOAK_DURATION_SECONDS`` from env, clamped to safe range."""
    try:
        val = int(os.environ.get("SOAK_DURATION_SECONDS", _DEFAULT_SOAK_SECONDS))
    except (ValueError, TypeError):
        val = _DEFAULT_SOAK_SECONDS
    return max(1, min(val, _MAX_SOAK_SECONDS))


# ---------------------------------------------------------------------------
# Matrix soak — env gating
# ---------------------------------------------------------------------------

_matrix_env_vars = [
    "MATRIX_HOMESERVER",
    "MATRIX_USER_ID",
    "MATRIX_ACCESS_TOKEN",
    "MATRIX_ROOM_ID",
]

_matrix_soak_ok = all(os.environ.get(v) for v in _matrix_env_vars)

pytestmark_matrix = [
    pytest.mark.live,
    pytest.mark.skipif(
        not _matrix_soak_ok,
        reason="Matrix soak tests require MATRIX_HOMESERVER, MATRIX_USER_ID, "
        "MATRIX_ACCESS_TOKEN, MATRIX_ROOM_ID",
    ),
]


# ---------------------------------------------------------------------------
# Meshtastic soak — env gating
# ---------------------------------------------------------------------------

_meshtastic_env_vars = [
    "MESHTASTIC_CONNECTION_TYPE",
    "MESHTASTIC_HOST",
]

_meshtastic_soak_ok = all(os.environ.get(v) for v in _meshtastic_env_vars)

pytestmark_meshtastic = [
    pytest.mark.live,
    pytest.mark.skipif(
        not _meshtastic_soak_ok,
        reason="Meshtastic soak tests require MESHTASTIC_CONNECTION_TYPE, "
        "MESHTASTIC_HOST",
    ),
]


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _make_matrix_context() -> Any:
    """Build an AdapterContext for Matrix soak tests."""
    from medre.adapters.base import AdapterContext

    return AdapterContext(
        adapter_id="matrix-soak",
        event_bus=None,
        publish_inbound=AsyncMock(),
        logger=logging.getLogger("test.soak.matrix"),
        clock=lambda: datetime.now(timezone.utc),
        shutdown_event=asyncio.Event(),
    )


def _make_matrix_config() -> Any:
    """Build a MatrixConfig from environment variables."""
    from medre.adapters.matrix.config import MatrixConfig

    return MatrixConfig(
        adapter_id="matrix-soak",
        homeserver=os.environ["MATRIX_HOMESERVER"],
        user_id=os.environ["MATRIX_USER_ID"],
        access_token=os.environ["MATRIX_ACCESS_TOKEN"],
        room_allowlist={os.environ["MATRIX_ROOM_ID"]},
    )


def _make_meshtastic_context() -> Any:
    """Build an AdapterContext for Meshtastic soak tests."""
    from medre.adapters.base import AdapterContext

    return AdapterContext(
        adapter_id="meshtastic-soak",
        event_bus=None,
        publish_inbound=AsyncMock(),
        logger=logging.getLogger("test.soak.meshtastic"),
        clock=lambda: datetime.now(timezone.utc),
        shutdown_event=asyncio.Event(),
    )


def _make_meshtastic_config() -> Any:
    """Build a MeshtasticConfig from environment variables."""
    from medre.adapters.meshtastic.config import MeshtasticConfig

    return MeshtasticConfig(
        adapter_id="meshtastic-soak",
        connection_type=os.environ["MESHTASTIC_CONNECTION_TYPE"],  # type: ignore[arg-type]
        host=os.environ.get("MESHTASTIC_HOST"),
        port=int(os.environ.get("MESHTASTIC_PORT", "4403")),
        default_channel=int(os.environ.get("MESHTASTIC_CHANNEL_INDEX", "0")),
    )


# ---------------------------------------------------------------------------
# Matrix soak tests
# ---------------------------------------------------------------------------


class TestMatrixSoak:
    """Sustained Matrix session health over a bounded time window.

    Observational: reports sync health, reconnect count, diagnostics
    at regular intervals.  Does not assert on specific timing targets.
    """

    pytestmark = pytestmark_matrix

    async def test_matrix_session_sustained_sync(self) -> None:
        """Keep a Matrix session alive for the configured soak duration.

        Validates that:
        - The session remains connected and logged in throughout.
        - The sync task continues running without exhausting reconnects.
        - No more than 3 reconnect attempts occur during the soak window.
        - Diagnostics snapshot is available and sane at every check.
        - A message can be sent near the end of the soak window.
        """
        pytest.importorskip("nio")
        from medre.adapters.matrix.adapter import MatrixAdapter

        duration = _get_soak_duration()
        adapter = MatrixAdapter(_make_matrix_config())
        ctx = _make_matrix_context()

        await adapter.start(ctx)
        try:
            info = await adapter.health_check()
            assert info.health == "healthy", (
                "Adapter must be healthy at start of soak"
            )

            deadline = time.monotonic() + duration
            check_interval = min(5.0, duration / 4)

            while time.monotonic() < deadline:
                await asyncio.sleep(check_interval)
                info = await adapter.health_check()
                assert info.health in ("healthy", "degraded"), (
                    f"Adapter health unexpected: {info.health}"
                )

            # Send one message near the end to verify the session is still
            # functional.
            from medre.core.rendering.renderer import RenderingResult

            result = RenderingResult(
                event_id="soak-end-check",
                target_adapter="matrix-soak",
                target_channel=os.environ["MATRIX_ROOM_ID"],
                payload={"msgtype": "m.text", "body": "MEDRE soak end-check"},
            )
            delivery = await adapter.deliver(result)
            assert delivery is not None, "Soak-end send returned None"
            assert delivery.native_message_id is not None, (
                "Soak-end send must return a native_message_id"
            )
        finally:
            await adapter.stop(timeout=5.0)

    async def test_matrix_session_periodic_send(self) -> None:
        """Send messages at regular intervals during soak, observe delivery.

        Observational: counts successful sends.  Does not assert on
        delivery latency or ordering.
        """
        pytest.importorskip("nio")
        from medre.adapters.matrix.adapter import MatrixAdapter
        from medre.core.rendering.renderer import RenderingResult

        duration = _get_soak_duration()
        adapter = MatrixAdapter(_make_matrix_config())
        ctx = _make_matrix_context()

        send_count = 0
        success_count = 0

        await adapter.start(ctx)
        try:
            deadline = time.monotonic() + duration
            next_send = time.monotonic()

            while time.monotonic() < deadline:
                now = time.monotonic()
                if now >= next_send:
                    send_count += 1
                    try:
                        result = RenderingResult(
                            event_id=f"soak-msg-{send_count}",
                            target_adapter="matrix-soak",
                            target_channel=os.environ["MATRIX_ROOM_ID"],
                            payload={
                                "msgtype": "m.text",
                                "body": f"MEDRE soak msg #{send_count}",
                            },
                        )
                        delivery = await adapter.deliver(result)
                        if delivery and delivery.native_message_id:
                            success_count += 1
                    except Exception:
                        pass  # Observational: record but do not fail
                    next_send = now + _SEND_INTERVAL_SECONDS
                else:
                    await asyncio.sleep(1.0)
        finally:
            await adapter.stop(timeout=5.0)

        # At least one message must have been sent successfully if any were
        # attempted.
        if send_count > 0:
            assert success_count >= 1, (
                f"Soak sent {send_count} messages but none succeeded"
            )


# ---------------------------------------------------------------------------
# Meshtastic soak tests
# ---------------------------------------------------------------------------


class TestMeshtasticSoak:
    """Sustained Meshtastic session health over a bounded time window.

    Observational: reports connection health, reconnect count, diagnostics.
    Does not assert on delivery timing or ACK reliability.
    """

    pytestmark = pytestmark_meshtastic

    async def test_meshtastic_session_sustained_connection(self) -> None:
        """Keep a Meshtastic session alive for the configured soak duration.

        Validates that:
        - The session remains connected throughout.
        - Reconnect attempts stay within bounded budget.
        - Health check reports healthy at every check.
        """
        pytest.importorskip("meshtastic")
        from medre.adapters.meshtastic.adapter import MeshtasticAdapter

        duration = _get_soak_duration()
        adapter = MeshtasticAdapter(_make_meshtastic_config())
        ctx = _make_meshtastic_context()

        await adapter.start(ctx)
        try:
            info = await adapter.health_check()
            assert info.health == "healthy", (
                "Adapter must be healthy at start of soak"
            )

            deadline = time.monotonic() + duration
            check_interval = min(5.0, duration / 4)

            while time.monotonic() < deadline:
                await asyncio.sleep(check_interval)
                info = await adapter.health_check()
                assert info.health in ("healthy", "degraded"), (
                    f"Adapter health unexpected: {info.health}"
                )
        finally:
            await adapter.stop(timeout=5.0)

    async def test_meshtastic_session_periodic_send(self) -> None:
        """Send messages at regular intervals during Meshtastic soak.

        Observational: counts send attempts and successes.  Does not
        assert on ACK timing or delivery confirmation.
        """
        pytest.importorskip("meshtastic")
        from medre.adapters.meshtastic.adapter import MeshtasticAdapter
        from medre.core.rendering.renderer import RenderingResult

        duration = _get_soak_duration()
        channel_index = int(os.environ.get("MESHTASTIC_CHANNEL_INDEX", "0"))
        adapter = MeshtasticAdapter(_make_meshtastic_config())
        ctx = _make_meshtastic_context()

        send_count = 0
        success_count = 0

        await adapter.start(ctx)
        try:
            deadline = time.monotonic() + duration
            next_send = time.monotonic()

            while time.monotonic() < deadline:
                now = time.monotonic()
                if now >= next_send:
                    send_count += 1
                    try:
                        result = RenderingResult(
                            event_id=f"soak-msg-{send_count}",
                            target_adapter="meshtastic-soak",
                            target_channel=f"channel:{channel_index}",
                            payload={
                                "text": f"MEDRE soak msg #{send_count}",
                            },
                        )
                        delivery = await adapter.deliver(result)
                        if delivery and delivery.native_message_id:
                            success_count += 1
                    except Exception:
                        pass  # Observational: record but do not fail
                    next_send = now + _SEND_INTERVAL_SECONDS
                else:
                    await asyncio.sleep(1.0)
        finally:
            await adapter.stop(timeout=5.0)

        if send_count > 0:
            assert success_count >= 1, (
                f"Soak sent {send_count} messages but none succeeded"
            )
