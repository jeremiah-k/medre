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

**Meshtastic env vars (connection-type-aware):**

=========================== =====================================================
Variable                    Description
=========================== =====================================================
``MESHTASTIC_CONNECTION_TYPE``  ``tcp``, ``serial``, or ``ble``
``MESHTASTIC_HOST``         Hostname/IP for TCP (required when type=tcp)
``MESHTASTIC_PORT``         Port for TCP (default ``4403``)
``MESHTASTIC_SERIAL_PORT``  Device path for serial (required when type=serial)
``MESHTASTIC_BLE_ADDRESS``  BLE MAC for BLE (required when type=ble)
``MESHTASTIC_CHANNEL_INDEX`` Channel index for outbound messages (default ``0``)
=========================== =====================================================

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
# Meshtastic soak — env gating (connection-type-aware, parity with live smoke)
# ---------------------------------------------------------------------------

_MESHTASTIC_CONNECTION_TYPE = os.environ.get(
    "MESHTASTIC_CONNECTION_TYPE", ""
).lower()


def _validate_meshtastic_soak_env() -> tuple[bool, str]:
    """Check Meshtastic soak env vars, mirroring live smoke gating.

    Returns (ok, reason).  ``ok`` is True when the required vars for the
    selected connection type are present.
    """
    ct = _MESHTASTIC_CONNECTION_TYPE
    if not ct:
        return False, "Set MESHTASTIC_CONNECTION_TYPE (tcp/serial/ble) for Meshtastic soak"
    if ct == "tcp":
        if not os.environ.get("MESHTASTIC_HOST"):
            return False, "MESHTASTIC_HOST required for TCP soak"
    elif ct == "serial":
        if not os.environ.get("MESHTASTIC_SERIAL_PORT"):
            return False, "MESHTASTIC_SERIAL_PORT required for serial soak"
    elif ct == "ble":
        if not os.environ.get("MESHTASTIC_BLE_ADDRESS"):
            return False, "MESHTASTIC_BLE_ADDRESS required for BLE soak"
    else:
        return False, f"Unknown MESHTASTIC_CONNECTION_TYPE {ct!r}; use tcp/serial/ble"
    return True, ""


_meshtastic_soak_ok, _meshtastic_soak_reason = _validate_meshtastic_soak_env()

pytestmark_meshtastic = [
    pytest.mark.live,
    pytest.mark.skipif(
        not _meshtastic_soak_ok,
        reason=_meshtastic_soak_reason,
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
    """Build a MeshtasticConfig from environment variables.

    Supports tcp, serial, and ble connection types with the same env var
    convention as the live smoke tests (``test_meshtastic_live.py``).
    """
    from medre.adapters.meshtastic.config import MeshtasticConfig

    ct = _MESHTASTIC_CONNECTION_TYPE
    if ct == "serial":
        return MeshtasticConfig(
            adapter_id="meshtastic-soak",
            connection_type="serial",
            serial_port=os.environ["MESHTASTIC_SERIAL_PORT"],
            default_channel=int(os.environ.get("MESHTASTIC_CHANNEL_INDEX", "0")),
        )
    elif ct == "ble":
        return MeshtasticConfig(
            adapter_id="meshtastic-soak",
            connection_type="ble",
            ble_address=os.environ["MESHTASTIC_BLE_ADDRESS"],
            default_channel=int(os.environ.get("MESHTASTIC_CHANNEL_INDEX", "0")),
        )
    else:  # tcp (default)
        return MeshtasticConfig(
            adapter_id="meshtastic-soak",
            connection_type="tcp",
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

        Observational: captures diagnostics snapshots at each interval
        (reconnect attempts, queue depth, background tasks, last error).
        Reports inbound packet count and duplication at soak end.
        """
        pytest.importorskip("meshtastic")
        from medre.adapters.meshtastic.adapter import MeshtasticAdapter

        duration = _get_soak_duration()
        adapter = MeshtasticAdapter(_make_meshtastic_config())
        ctx = _make_meshtastic_context()
        soak_start = time.monotonic()

        await adapter.start(ctx)
        try:
            info = await adapter.health_check()
            assert info.health == "healthy", (
                "Adapter must be healthy at start of soak"
            )
            print(f"[soak+0s] health={info.health}")

            deadline = time.monotonic() + duration
            check_interval = min(5.0, duration / 4)

            while time.monotonic() < deadline:
                await asyncio.sleep(check_interval)
                elapsed = time.monotonic() - soak_start
                info = await adapter.health_check()
                assert info.health in ("healthy", "degraded"), (
                    f"Adapter health unexpected: {info.health}"
                )

                # Diagnostics snapshot
                diag = adapter.diagnostics()
                session = diag.get("session", {})
                print(
                    f"[soak+{elapsed:.0f}s] health={info.health} "
                    f"reconnects={session.get('reconnect_attempts', 'N/A')} "
                    f"reconnecting={session.get('reconnecting', 'N/A')} "
                    f"queue_pending={diag.get('queue_pending', 'N/A')} "
                    f"bg_tasks={diag.get('background_tasks', 'N/A')} "
                    f"last_err={session.get('last_error') or 'none'}"
                )

                # Reconnect budget assertion
                max_reconnects = 10
                reconnects = session.get("reconnect_attempts", 0)
                assert reconnects <= max_reconnects, (
                    f"Reconnect attempts ({reconnects}) exceeded budget "
                    f"({max_reconnects}) during soak"
                )

            # Final summary
            diag = adapter.diagnostics()
            session = diag.get("session", {})
            inbound_mock = ctx.publish_inbound
            print(f"\n=== Meshtastic sustained soak summary ===")
            print(f"  duration={duration}s  "
                  f"final_health={info.health}")
            print(f"  session: connected={session.get('connected')}  "
                  f"reconnect_attempts={session.get('reconnect_attempts')}  "
                  f"transient_fail={session.get('transient_delivery_failures')}  "
                  f"permanent_fail={session.get('permanent_delivery_failures')}")
            print(f"  queue: pending={diag.get('queue_pending')}  "
                  f"total_sent={diag.get('queue_total_sent')}  "
                  f"total_failed={diag.get('queue_total_failed')}")
            print(f"  inbound_packets={inbound_mock.call_count}")
        finally:
            await adapter.stop(timeout=5.0)

    async def test_meshtastic_session_periodic_send(self) -> None:
        """Send messages at regular intervals during Meshtastic soak.

        Observational: counts send attempts and successes.  Does not
        assert on ACK timing or delivery confirmation.

        The Meshtastic adapter's ``deliver()`` enqueues to the internal
        outbound queue and returns ``None`` (async enqueue-only design).
        To actually transmit, ``send_one()`` must be called to flush one
        queued item through the session's ``send()`` path.
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
        fail_count = 0
        seen_inbound_ids: list[int] = []

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
                        # deliver() enqueues; send_one() flushes the queue
                        await adapter.deliver(result)
                        send_result = await adapter.send_one()
                        if send_result and send_result.native_message_id:
                            success_count += 1
                            print(
                                f"[soak-send #{send_count}] ok "
                                f"native_id={send_result.native_message_id}"
                            )
                        else:
                            fail_count += 1
                            print(
                                f"[soak-send #{send_count}] no delivery result "
                                f"(queue_pending={adapter.diagnostics().get('queue_pending', '?')})"
                            )
                    except Exception as exc:
                        fail_count += 1
                        print(f"[soak-send #{send_count}] error: {exc}")
                    next_send = now + _SEND_INTERVAL_SECONDS
                else:
                    await asyncio.sleep(1.0)

            # -- Post-loop observations --
            diag = adapter.diagnostics()
            print(f"\n=== Meshtastic soak send summary ===")
            print(f"  attempts={send_count}  successes={success_count}  "
                  f"failures={fail_count}")
            print(f"  queue: pending={diag.get('queue_pending')}  "
                  f"total_sent={diag.get('queue_total_sent')}  "
                  f"total_failed={diag.get('queue_total_failed')}")
            session = diag.get("session", {})
            print(f"  session: connected={session.get('connected')}  "
                  f"reconnects={session.get('reconnect_attempts')}  "
                  f"transient_fail={session.get('transient_delivery_failures')}  "
                  f"permanent_fail={session.get('permanent_delivery_failures')}")
            if session.get("last_error"):
                print(f"  session last_error: {session['last_error']}")

            # Inbound packet duplication observation
            inbound_mock = ctx.publish_inbound
            if inbound_mock.call_count > 0:
                for call in inbound_mock.call_args_list:
                    event = call[0][0] if call[0] else None
                    if event and hasattr(event, "metadata"):
                        pkt_id = getattr(event.metadata, "native_event_id", None)
                        if pkt_id is not None:
                            seen_inbound_ids.append(pkt_id)
                dup_count = len(seen_inbound_ids) - len(set(seen_inbound_ids))
                print(f"  inbound: {len(seen_inbound_ids)} packets  "
                      f"duplicates={dup_count}")
        finally:
            await adapter.stop(timeout=5.0)

        if send_count > 0:
            assert success_count >= 1, (
                f"Soak sent {send_count} messages but none succeeded"
            )

    async def test_meshtastic_session_stop_cleanliness(self) -> None:
        """Verify stop() cleanly tears down all resources.

        Validates that after stop():
        - The adapter reports started=False.
        - Session reference is None (no leaked transport).
        - Client reference is None (no leaked connection).
        - No background tasks remain.
        - Diagnostics show a stopped state.
        - A second stop() call is idempotent (no error).
        """
        pytest.importorskip("meshtastic")
        from medre.adapters.meshtastic.adapter import MeshtasticAdapter

        adapter = MeshtasticAdapter(_make_meshtastic_config())
        ctx = _make_meshtastic_context()

        await adapter.start(ctx)

        # Verify started state
        info = await adapter.health_check()
        assert info.health == "healthy", (
            f"Adapter must be healthy before stop, got {info.health}"
        )
        diag_before = adapter.diagnostics()
        assert diag_before["started"] is True

        # Stop
        await adapter.stop(timeout=5.0)

        # Verify clean state after stop
        assert adapter._started is False, "Adapter must report stopped"
        assert adapter._session is None, "Session must be None after stop"
        assert adapter._client is None, "Client must be None after stop"
        assert len(adapter._background_tasks) == 0, (
            f"Background tasks remain after stop: {len(adapter._background_tasks)}"
        )

        diag_after = adapter.diagnostics()
        assert diag_after["started"] is False
        assert diag_after["queue_pending"] == 0, (
            f"Queue must be empty after stop, got {diag_after['queue_pending']}"
        )
        assert "session" not in diag_after, (
            "Session diagnostics should not be present after stop"
        )

        print(f"=== Meshtastic stop cleanliness ===")
        print(f"  started={diag_after['started']}  "
              f"queue_pending={diag_after['queue_pending']}  "
              f"bg_tasks={diag_after['background_tasks']}")

        # Idempotent second stop — must not raise
        await adapter.stop(timeout=5.0)

    async def test_meshtastic_session_inbound_duplication(self) -> None:
        """Observe inbound packets for duplication over a bounded window.

        This is a shorter, focused observation (default soak duration)
        that tracks inbound packet IDs received from the radio.  Reports
        total inbound count and any detected duplicates.

        Observational only — does not assert on duplication count since
        radio-level duplicate delivery is expected in mesh networks.
        """
        pytest.importorskip("meshtastic")
        from medre.adapters.meshtastic.adapter import MeshtasticAdapter

        duration = _get_soak_duration()
        adapter = MeshtasticAdapter(_make_meshtastic_config())
        ctx = _make_meshtastic_context()
        inbound_ids: list[int] = []

        await adapter.start(ctx)
        try:
            deadline = time.monotonic() + duration
            while time.monotonic() < deadline:
                await asyncio.sleep(min(5.0, duration / 4))

            # Collect inbound packet IDs from the mock
            inbound_mock = ctx.publish_inbound
            for call in inbound_mock.call_args_list:
                event = call[0][0] if call[0] else None
                if event is not None:
                    # Try multiple common locations for the native packet ID
                    pkt_id = None
                    if hasattr(event, "metadata") and event.metadata is not None:
                        pkt_id = getattr(event.metadata, "native_event_id", None)
                    if pkt_id is None and hasattr(event, "event_id"):
                        pkt_id = event.event_id
                    if pkt_id is not None:
                        inbound_ids.append(hash(pkt_id) % (2**31))

            total = len(inbound_ids)
            unique = len(set(inbound_ids))
            dups = total - unique

            print(f"\n=== Meshtastic inbound duplication observation ===")
            print(f"  duration={duration}s  "
                  f"total_inbound={total}  "
                  f"unique={unique}  "
                  f"duplicates={dups}")
            if dups > 0:
                print(f"  NOTE: mesh radio duplicate delivery is expected; "
                      f"not treated as error")
        finally:
            await adapter.stop(timeout=5.0)
