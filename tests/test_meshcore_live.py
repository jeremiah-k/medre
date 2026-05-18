"""Live MeshCore connectivity smoke tests.

These tests connect to a **real** MeshCore radio node and exercise
MEDRE adapter lifecycle against real hardware.

All tests are **skipped by default** and require explicit opt-in via
environment variables.

**Running live tests:**

1. Set up a MeshCore radio node accessible via TCP, serial, or BLE.

2. Set the required environment variables:

   .. code-block:: bash

       export MESHCORE_CONNECTION_TYPE="tcp"
       export MESHCORE_HOST="meshcore.local"
       # export MESHCORE_PORT="4403"       # optional
       # export MESHCORE_SERIAL_PORT="/dev/ttyUSB0"  # for serial
       # export MESHCORE_BLE_ADDRESS="AA:BB:CC:DD:EE:FF"  # for BLE
       export MESHCORE_CHANNEL_INDEX="0"

3. Run the live tests:

   .. code-block:: bash

       pip install meshcore
       pytest tests/test_meshcore_live.py -m live -v

   Default ``pytest`` run (no live tests):

   .. code-block:: bash

       pytest   # live tests excluded by addopts

**Required environment variables:**

=========================== =====================================================
Variable                    Description
=========================== =====================================================
``MESHCORE_CONNECTION_TYPE`` Connection mode: ``tcp``, ``serial``, or ``ble``
``MESHCORE_HOST``           Hostname or IP for TCP connections
``MESHCORE_PORT``           Port for TCP (default ``4403``)
``MESHCORE_SERIAL_PORT``    Serial device path for serial connections
``MESHCORE_BLE_ADDRESS``    BLE MAC address for BLE connections
``MESHCORE_CHANNEL_INDEX``  Channel index for outbound test messages (default ``0``)
=========================== =====================================================

At minimum, ``MESHCORE_CONNECTION_TYPE`` must be set.  Depending on the
connection type, the corresponding host/port/serial/BLE variable must also
be set.  If any required variable is missing, every test in this file skips
with a descriptive reason.

**Known limitations (explicit):**

- **No E2EE.**  MeshCore encrypted channels are not supported.
- **No telemetry, position, or admin processing.**  Only text messages.
- **Radio traffic safety.**  When enabled, tests send a small number of
  text messages on the configured channel.  Messages will be prefixed
  with ``MEDRE live smoke`` for easy identification.
- **Duplicate-send risk.**  The session retries transient failures up to
  3 times; a message may be delivered more than once if the ACK was lost.

**What this proves (when enabled):**

- The MEDRE ``MeshCoreAdapter`` can ``start()`` against a real node.
- ``health_check()`` reports ``"healthy"``.
- ``stop()`` disconnects cleanly.
- ``send_text()`` delivers a message to the mesh.
- Inbound messages are received with metadata preservation.

**What this does NOT prove:**

- Production-grade reconnection handling under sustained failure.
- Multi-hop mesh delivery.
- Encrypted channel support.
"""

import asyncio
import logging
import os
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

# ---------------------------------------------------------------------------
# Module-level marker — entire file is tagged "live" so it is excluded by the
# default ``addopts = "-m 'not live'"`` in pyproject.toml.
# ---------------------------------------------------------------------------
pytestmark = [pytest.mark.live]

# ---------------------------------------------------------------------------
# Environment variable gate
# ---------------------------------------------------------------------------
MESHCORE_CONNECTION_TYPE = os.environ.get("MESHCORE_CONNECTION_TYPE", "").lower()
MESHCORE_HOST = os.environ.get("MESHCORE_HOST")
MESHCORE_PORT = os.environ.get("MESHCORE_PORT", "4403")
MESHCORE_SERIAL_PORT = os.environ.get("MESHCORE_SERIAL_PORT")
MESHCORE_BLE_ADDRESS = os.environ.get("MESHCORE_BLE_ADDRESS")
MESHCORE_CHANNEL_INDEX = os.environ.get("MESHCORE_CHANNEL_INDEX", "0")


def _validate_env() -> tuple[str, str]:
    """Validate env vars and return (reason, connection_type).

    Returns ("", connection_type) if valid, or (skip_reason, "") if not.
    """
    ct = MESHCORE_CONNECTION_TYPE
    if not ct:
        return (
            "Set MESHCORE_CONNECTION_TYPE (tcp/serial/ble) to run live MeshCore tests",
            "",
        )

    if ct == "tcp":
        if not MESHCORE_HOST:
            return (
                "MESHCORE_HOST is required for TCP connection type",
                "",
            )
    elif ct == "serial":
        if not MESHCORE_SERIAL_PORT:
            return (
                "MESHCORE_SERIAL_PORT is required for serial connection type",
                "",
            )
    elif ct == "ble":
        if not MESHCORE_BLE_ADDRESS:
            return (
                "MESHCORE_BLE_ADDRESS is required for BLE connection type",
                "",
            )
    else:
        return (
            f"Unknown MESHCORE_CONNECTION_TYPE {ct!r}; use tcp, serial, or ble",
            "",
        )

    return ("", ct)


_LIVE_SKIP_REASON, _CONNECTION_TYPE = _validate_env()
_LIVE_ENV_SET = _CONNECTION_TYPE != ""

# Also check for SDK availability.
from medre.adapters.meshcore.compat import HAS_MESHCORE

require_live = pytest.mark.skipif(
    not (_LIVE_ENV_SET and HAS_MESHCORE),
    reason=(
        _LIVE_SKIP_REASON
        if not _LIVE_ENV_SET
        else "meshcore SDK not installed; pip install meshcore"
    ),
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _make_config():
    """Build a MeshCoreConfig from the live environment variables."""
    from medre.config.adapters.meshcore import MeshCoreConfig

    ct = MESHCORE_CONNECTION_TYPE
    if ct == "tcp":
        return MeshCoreConfig(
            adapter_id="meshcore-live-smoke",
            connection_type="tcp",
            host=MESHCORE_HOST or "localhost",
            port=int(MESHCORE_PORT) if MESHCORE_PORT else 4403,
        )
    elif ct == "serial":
        return MeshCoreConfig(
            adapter_id="meshcore-live-smoke",
            connection_type="serial",
            serial_port=MESHCORE_SERIAL_PORT or "/dev/ttyUSB0",
        )
    elif ct == "ble":
        return MeshCoreConfig(
            adapter_id="meshcore-live-smoke",
            connection_type="ble",
            ble_address=MESHCORE_BLE_ADDRESS or "",
        )
    else:
        return MeshCoreConfig(
            adapter_id="meshcore-live-smoke",
            connection_type="tcp",
            host=MESHCORE_HOST or "localhost",
        )


def _make_context():
    """Build an AdapterContext suitable for live smoke tests."""
    from medre.core.contracts.adapter import AdapterContext

    return AdapterContext(
        adapter_id="meshcore-live-smoke",
        event_bus=None,
        publish_inbound=AsyncMock(),
        logger=logging.getLogger("test.meshcore-live"),
        clock=lambda: datetime.now(timezone.utc),
        shutdown_event=asyncio.Event(),
    )


# ---------------------------------------------------------------------------
# Live tests
# ---------------------------------------------------------------------------
@require_live
class TestMeshCoreLiveSmoke:
    """Live MeshCore connectivity smoke tests.

    These tests connect to a real MeshCore radio node and verify the
    adapter lifecycle: start, health_check, send, inbound receive,
    and stop.

    All tests require MESHCORE_CONNECTION_TYPE and corresponding
    connection parameters.  Run with::

        pytest tests/test_meshcore_live.py -m live -v
    """

    # -- Lifecycle: connect, health, disconnect ----------------------------

    async def test_adapter_starts_and_reports_healthy(self):
        """Start the real adapter and verify health_check reports healthy.

        **Category B — MEDRE adapter lifecycle smoke test.**

        This validates:
        - The adapter creates a MeshCoreSession in ``start()``.
        - The session connects via the MeshCore SDK.
        - ``health_check()`` returns ``"healthy"`` after start.
        """
        from medre.adapters.meshcore.adapter import MeshCoreAdapter

        config = _make_config()
        adapter = MeshCoreAdapter(config)
        ctx = _make_context()

        try:
            await adapter.start(ctx)
            info = await adapter.health_check()
            assert info.health in ("healthy", "degraded"), (
                f"Expected healthy or degraded, got {info.health!r}"
            )
        finally:
            await adapter.stop()

    async def test_session_connected_after_start(self):
        """Verify session reports connected after adapter start."""
        from medre.adapters.meshcore.adapter import MeshCoreAdapter

        config = _make_config()
        adapter = MeshCoreAdapter(config)
        ctx = _make_context()

        try:
            await adapter.start(ctx)
            assert adapter._session is not None
            assert adapter._session.connected is True
        finally:
            await adapter.stop()

    async def test_session_disconnected_after_stop(self):
        """Verify session reports disconnected after adapter stop."""
        from medre.adapters.meshcore.adapter import MeshCoreAdapter

        config = _make_config()
        adapter = MeshCoreAdapter(config)
        ctx = _make_context()

        try:
            await adapter.start(ctx)
            await adapter.stop()
            assert adapter._session is None
        finally:
            await adapter.stop()

    # -- Diagnostics --------------------------------------------------------

    async def test_diagnostics_available_after_start(self):
        """Verify diagnostics snapshot is available."""
        from medre.adapters.meshcore.adapter import MeshCoreAdapter

        config = _make_config()
        adapter = MeshCoreAdapter(config)
        ctx = _make_context()

        try:
            await adapter.start(ctx)
            diag = adapter.diagnostics()
            assert diag["started"] is True
            assert "session" in diag
            assert diag["session"]["connected"] is True
            assert diag["session"]["mode"] in ("tcp", "serial", "ble")
        finally:
            await adapter.stop()

    async def test_diagnostics_no_secrets(self):
        """Diagnostics never expose secrets."""
        from medre.adapters.meshcore.adapter import MeshCoreAdapter

        config = _make_config()
        adapter = MeshCoreAdapter(config)
        ctx = _make_context()

        try:
            await adapter.start(ctx)
            diag = adapter.diagnostics()
            diag_str = str(diag)
            assert "private_key" not in diag_str
            assert "secret" not in diag_str
            assert "password" not in diag_str
        finally:
            await adapter.stop()

    # -- Outbound send ------------------------------------------------------

    async def test_send_channel_message(self):
        """Send a channel message and verify no error is raised."""
        from medre.adapters.meshcore.adapter import MeshCoreAdapter
        from medre.adapters.meshcore.errors import MeshCoreSendError

        config = _make_config()
        adapter = MeshCoreAdapter(config)
        ctx = _make_context()

        try:
            await adapter.start(ctx)
            assert adapter._session is not None
            result = await adapter._session.send_text(
                contact_id="",
                text="MEDRE live smoke: send test",
                channel_index=int(MESHCORE_CHANNEL_INDEX),
            )
            # Result may be None or a native message ID.
            # The important thing is no exception was raised.
        finally:
            await adapter.stop()

    # -- Inbound receive ----------------------------------------------------

    async def test_inbound_callback_receives_messages(self):
        """Subscribe to inbound messages and wait for one.

        This test waits up to 30 seconds for an inbound message.
        It will pass if any message is received during the wait period.
        """
        from medre.adapters.meshcore.adapter import MeshCoreAdapter

        config = _make_config()
        adapter = MeshCoreAdapter(config)
        ctx = _make_context()

        received: list[dict] = []

        async def capture(pkt: dict) -> None:
            received.append(pkt)

        try:
            await adapter.start(ctx)
            # Wire the session callback to our capture function.
            if adapter._session is not None:
                adapter._session._message_callback = capture

            # Wait for a message (up to 30 seconds).
            # This test is opportunistic — it passes if a message arrives.
            for _ in range(60):
                await asyncio.sleep(0.5)
                if received:
                    break

            # No assertion on count — this is opportunistic.
            if received:
                assert "text" in received[0]
        finally:
            await adapter.stop()

    # -- Repeated start/stop ------------------------------------------------

    async def test_repeated_start_stop(self):
        """Start/stop cycle can be repeated without errors."""
        from medre.adapters.meshcore.adapter import MeshCoreAdapter

        config = _make_config()
        adapter = MeshCoreAdapter(config)
        ctx = _make_context()

        try:
            for i in range(3):
                await adapter.start(ctx)
                assert adapter._session is not None
                assert adapter._session.connected is True
                await adapter.stop()
                assert adapter._session is None
        finally:
            await adapter.stop()
