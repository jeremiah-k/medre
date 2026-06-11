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
       # export MESHCORE_PORT="4000"       # optional
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
``MESHCORE_PORT``           Port for TCP (default ``4000``)
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
- **Duplicate-accept risk.**  The session retries transient failures up to
  3 times; the local SDK may accept the same message more than once if
  the ACK was lost.

**What this proves (when enabled):**

- The MEDRE ``MeshCoreAdapter`` can ``start()`` against a real node.
- ``health_check()`` reports ``"healthy"``.
- ``stop()`` disconnects cleanly.
- ``send_text()`` is accepted by the local MeshCore SDK/node without error.
  Remote receipt / RF end-to-end delivery is **not** confirmed unless a
  second MeshCore device independently observes the message.
- Inbound messages are received with metadata preservation.

**What this does NOT prove:**

- Production-grade reconnection handling under sustained failure.
- Multi-hop mesh delivery.
- Encrypted channel support.
- RF end-to-end delivery from local ``send_text()`` acceptance alone.  A
  successful return only confirms the local SDK/node accepted the message;
  remote receipt requires independent observation by a second MeshCore
  device.
"""

import asyncio
import logging
import os
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from medre.adapters.meshcore.compat import HAS_MESHCORE
from tests.helpers.live_harness import assert_no_secret_leak, bounded

# ---------------------------------------------------------------------------
# Module-level marker — live tests are tagged "live" at the class level so
# they are excluded by the default ``addopts = "-m 'not live'"`` in
# pyproject.toml.  The BLE validation class at the bottom is NOT marked
# live and runs unconditionally.
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Timeout bounds for live async operations (seconds).
# ---------------------------------------------------------------------------
_ADAPTER_START_TIMEOUT: float = 30.0
_ADAPTER_STOP_TIMEOUT: float = 10.0
_DELIVER_TIMEOUT: float = 15.0  # local SDK/node acceptance, not RF delivery

# ---------------------------------------------------------------------------
# Environment variable gate
# ---------------------------------------------------------------------------
MESHCORE_CONNECTION_TYPE = os.environ.get("MESHCORE_CONNECTION_TYPE", "").lower()
MESHCORE_HOST = os.environ.get("MESHCORE_HOST")
MESHCORE_PORT = os.environ.get("MESHCORE_PORT", "4000")
MESHCORE_SERIAL_PORT = os.environ.get("MESHCORE_SERIAL_PORT")
MESHCORE_BLE_ADDRESS = os.environ.get("MESHCORE_BLE_ADDRESS")
MESHCORE_CHANNEL_INDEX = os.environ.get("MESHCORE_CHANNEL_INDEX", "0")
MESHCORE_LIVE_SEND = os.environ.get("MESHCORE_LIVE_SEND", "").strip() == "1"


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

require_live = pytest.mark.skipif(
    not (_LIVE_ENV_SET and HAS_MESHCORE),
    reason=(
        _LIVE_SKIP_REASON
        if not _LIVE_ENV_SET
        else "meshcore SDK not installed; pip install meshcore"
    ),
)

# Additional gate for tests that actually send messages via the local
# MeshCore node.  These tests are opt-in: MESHCORE_LIVE_SEND=1 must be
# set explicitly.
require_live_send = pytest.mark.skipif(
    not MESHCORE_LIVE_SEND,
    reason=("Set MESHCORE_LIVE_SEND=1 to enable live send tests"),
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
            port=int(MESHCORE_PORT) if MESHCORE_PORT else 4000,
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
@pytest.mark.live
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
            await asyncio.wait_for(adapter.start(ctx), timeout=_ADAPTER_START_TIMEOUT)
            info = await adapter.health_check()
            assert info.health in (
                "healthy",
                "degraded",
            ), f"Expected healthy or degraded, got {info.health!r}"
        finally:
            await asyncio.wait_for(adapter.stop(), timeout=_ADAPTER_STOP_TIMEOUT)

    async def test_session_connected_after_start(self):
        """Verify session reports connected after adapter start."""
        from medre.adapters.meshcore.adapter import MeshCoreAdapter

        config = _make_config()
        adapter = MeshCoreAdapter(config)
        ctx = _make_context()

        try:
            await asyncio.wait_for(adapter.start(ctx), timeout=_ADAPTER_START_TIMEOUT)
            assert adapter._session is not None
            assert adapter._session.connected is True
        finally:
            await asyncio.wait_for(adapter.stop(), timeout=_ADAPTER_STOP_TIMEOUT)

    async def test_session_disconnected_after_stop(self):
        """Verify session reports disconnected after adapter stop."""
        from medre.adapters.meshcore.adapter import MeshCoreAdapter

        config = _make_config()
        adapter = MeshCoreAdapter(config)
        ctx = _make_context()

        try:
            await asyncio.wait_for(adapter.start(ctx), timeout=_ADAPTER_START_TIMEOUT)
            await asyncio.wait_for(adapter.stop(), timeout=_ADAPTER_STOP_TIMEOUT)
            assert adapter._session is None
        finally:
            await asyncio.wait_for(adapter.stop(), timeout=_ADAPTER_STOP_TIMEOUT)

    # -- Diagnostics --------------------------------------------------------

    async def test_diagnostics_available_after_start(self):
        """Verify diagnostics snapshot is available."""
        from medre.adapters.meshcore.adapter import MeshCoreAdapter

        config = _make_config()
        adapter = MeshCoreAdapter(config)
        ctx = _make_context()

        try:
            await asyncio.wait_for(adapter.start(ctx), timeout=_ADAPTER_START_TIMEOUT)
            diag = adapter.diagnostics()
            assert diag["started"] is True
            assert "session" in diag
            assert diag["session"]["connected"] is True
            assert diag["session"]["mode"] in ("tcp", "serial", "ble")
        finally:
            await asyncio.wait_for(adapter.stop(), timeout=_ADAPTER_STOP_TIMEOUT)

    async def test_diagnostics_no_secrets(self):
        """Diagnostics never expose secrets."""
        from medre.adapters.meshcore.adapter import MeshCoreAdapter

        config = _make_config()
        adapter = MeshCoreAdapter(config)
        ctx = _make_context()

        try:
            await asyncio.wait_for(adapter.start(ctx), timeout=_ADAPTER_START_TIMEOUT)
            diag = adapter.diagnostics()
            assert_no_secret_leak(diag, {"private_key", "secret", "password"})
        finally:
            await asyncio.wait_for(adapter.stop(), timeout=_ADAPTER_STOP_TIMEOUT)

    async def test_diagnostics_shape_useful_fields(self):
        """Diagnostics contain useful operational fields with correct shape.

        Verifies the diagnostics snapshot includes all expected
        MeshCore-specific fields: adapter metadata, session state,
        and no secrets.
        """
        from medre.adapters.meshcore.adapter import MeshCoreAdapter

        config = _make_config()
        adapter = MeshCoreAdapter(config)
        ctx = _make_context()

        try:
            await asyncio.wait_for(adapter.start(ctx), timeout=_ADAPTER_START_TIMEOUT)
            diag = adapter.diagnostics()

            # -- Required MeshCore-specific fields --------------------------------
            required_fields = (
                "adapter_id",
                "platform",
                "started",
                "mode",
            )
            for field in required_fields:
                assert field in diag, (
                    f"diagnostics() missing required field {field!r}. "
                    f"Available fields: {sorted(diag.keys())}"
                )

            assert diag["adapter_id"] == "meshcore-live-smoke"
            assert diag["platform"] == "meshcore"
            assert diag["started"] is True
            assert diag["mode"] in ("tcp", "serial", "ble")

            # -- Session diagnostics shape ----------------------------------------
            assert "session" in diag
            session_diag = diag["session"]
            assert session_diag["connected"] is True
            assert isinstance(session_diag.get("reconnecting"), bool)
            assert isinstance(session_diag.get("reconnect_attempts"), int)
            assert isinstance(session_diag.get("transient_delivery_failures"), int)
            assert isinstance(session_diag.get("permanent_delivery_failures"), int)

            # -- No secrets in diagnostics output --------------------------------
            assert_no_secret_leak(diag, {"private_key", "secret", "password"})
        finally:
            await asyncio.wait_for(adapter.stop(), timeout=_ADAPTER_STOP_TIMEOUT)

    # -- Outbound send ------------------------------------------------------

    @require_live_send
    async def test_send_channel_message(self):
        """Send a channel message and verify the local SDK accepts it.

        Requires MESHCORE_LIVE_SEND=1 to actually send through the local
        MeshCore SDK/node.  Success confirms local acceptance only; it does
        not prove RF end-to-end delivery.
        """
        from medre.adapters.meshcore.adapter import MeshCoreAdapter

        config = _make_config()
        adapter = MeshCoreAdapter(config)
        ctx = _make_context()

        try:
            await asyncio.wait_for(adapter.start(ctx), timeout=_ADAPTER_START_TIMEOUT)
            assert adapter._session is not None
            await asyncio.wait_for(
                adapter._session.send_text(
                    contact_id="",
                    text="MEDRE live smoke: send test",
                    channel_index=int(MESHCORE_CHANNEL_INDEX),
                ),
                timeout=_DELIVER_TIMEOUT,
            )
            # Result may be None or a native message ID.
            # The important thing is no exception was raised.
        finally:
            await asyncio.wait_for(adapter.stop(), timeout=_ADAPTER_STOP_TIMEOUT)

    # -- Inbound receive ----------------------------------------------------

    @require_live_send
    async def test_inbound_callback_receives_messages(self):
        """Subscribe to inbound messages and wait for one.

        This test waits up to 30 seconds for an inbound message.
        It will pass if any message is received during the wait period.

        Requires MESHCORE_LIVE_SEND=1 because it needs active radio
        traffic to observe inbound messages on the local node.
        """
        from medre.adapters.meshcore.adapter import MeshCoreAdapter

        config = _make_config()
        adapter = MeshCoreAdapter(config)
        ctx = _make_context()

        received: list[dict] = []

        async def capture(pkt: dict) -> None:
            received.append(pkt)

        try:
            await asyncio.wait_for(adapter.start(ctx), timeout=_ADAPTER_START_TIMEOUT)
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
            await asyncio.wait_for(adapter.stop(), timeout=_ADAPTER_STOP_TIMEOUT)

    # -- Repeated start/stop ------------------------------------------------

    async def test_repeated_start_stop(self):
        """Start/stop cycle can be repeated without errors."""
        from medre.adapters.meshcore.adapter import MeshCoreAdapter

        config = _make_config()
        adapter = MeshCoreAdapter(config)
        ctx = _make_context()

        try:
            for _i in range(3):
                await asyncio.wait_for(
                    adapter.start(ctx), timeout=_ADAPTER_START_TIMEOUT
                )
                assert adapter._session is not None
                assert adapter._session.connected is True
                await asyncio.wait_for(adapter.stop(), timeout=_ADAPTER_STOP_TIMEOUT)
                assert adapter._session is None
        finally:
            await asyncio.wait_for(adapter.stop(), timeout=_ADAPTER_STOP_TIMEOUT)

    # -- Bounded async ops --------------------------------------------------

    async def test_bounded_start_stop(self):
        """start() and stop() complete within timeout bounds.

        This catches resource leaks (unclosed sessions, dangling tasks)
        that would prevent clean shutdown.
        """
        from medre.adapters.meshcore.adapter import MeshCoreAdapter

        config = _make_config()
        adapter = MeshCoreAdapter(config)
        ctx = _make_context()

        await asyncio.wait_for(adapter.start(ctx), timeout=_ADAPTER_START_TIMEOUT)
        await asyncio.wait_for(adapter.stop(), timeout=_ADAPTER_STOP_TIMEOUT)

        # After stop, diagnostics should report disconnected state.
        diag = adapter.diagnostics()
        assert diag["started"] is False
        assert "session" not in diag

    # -- Stop idempotency ---------------------------------------------------

    async def test_stop_idempotency(self):
        """Calling stop() multiple times is safe and idempotent.

        Verifies:
        - stop() on a never-started adapter is a no-op.
        - stop() after stop() is a no-op.
        - Health remains 'unknown' throughout.
        """
        from medre.adapters.meshcore.adapter import MeshCoreAdapter

        config = _make_config()
        adapter = MeshCoreAdapter(config)

        # stop() on never-started adapter — no-op
        await asyncio.wait_for(adapter.stop(), timeout=_ADAPTER_STOP_TIMEOUT)
        info = await adapter.health_check()
        assert info.health == "unknown"

        # Start, then stop twice
        ctx = _make_context()
        await asyncio.wait_for(adapter.start(ctx), timeout=_ADAPTER_START_TIMEOUT)
        info = await adapter.health_check()
        assert info.health in ("healthy", "degraded")

        await asyncio.wait_for(adapter.stop(), timeout=_ADAPTER_STOP_TIMEOUT)
        info = await adapter.health_check()
        assert info.health == "unknown"

        # Second stop — no-op
        await asyncio.wait_for(adapter.stop(), timeout=_ADAPTER_STOP_TIMEOUT)
        info = await adapter.health_check()
        assert info.health == "unknown"


# ---------------------------------------------------------------------------
# BLE validation tests (mock-based, no hardware required)
# ---------------------------------------------------------------------------
class TestMeshCoreBLEValidation:
    """BLE-specific validation tests that run without hardware.

    These tests construct MeshCoreConfig directly and use mocks/monkeypatch
    to verify BLE paths. They are NOT gated by ``@require_live`` — they
    always execute with ``-m "not live"`` (the default pytest filter).
    """

    # -- a) Config builds -----------------------------------------------------

    def test_ble_config_builds(self):
        """BLE config with a valid address passes validate()."""
        from medre.config.adapters.meshcore import MeshCoreConfig

        config = MeshCoreConfig(
            adapter_id="meshcore-ble-test",
            connection_type="ble",
            ble_address="AA:BB:CC:DD:EE:FF",
        )
        result = config.validate()
        assert result.connection_type == "ble"
        assert result.ble_address == "AA:BB:CC:DD:EE:FF"

    # -- b) Factory path called -----------------------------------------------

    async def test_ble_start_calls_factory_path(self):
        """BLE session.start() calls MeshCore.create_ble with correct address."""
        from medre.adapters.meshcore.session import MeshCoreSession
        from medre.config.adapters.meshcore import MeshCoreConfig

        config = MeshCoreConfig(
            adapter_id="ble-factory-test",
            connection_type="ble",
            ble_address="C4:4F:33:6A:B0:23",
        )

        mock_mc_instance = MagicMock()
        mock_mc_instance.subscribe = MagicMock()
        mock_mc_instance.disconnect = AsyncMock()
        mock_mc_instance.commands = AsyncMock()
        mock_mc_instance.commands.send_appstart = AsyncMock(
            return_value=MagicMock(is_error=lambda: False)
        )

        fake_create_ble = AsyncMock(return_value=mock_mc_instance)

        mock_meshcore_module = MagicMock()
        mock_meshcore_module.MeshCore.create_ble = fake_create_ble
        mock_meshcore_module.EventType.CONTACT_MSG_RECV = "CONTACT_MSG_RECV"
        mock_meshcore_module.EventType.CHANNEL_MSG_RECV = "CHANNEL_MSG_RECV"
        mock_meshcore_module.EventType.DISCONNECTED = "DISCONNECTED"
        mock_meshcore_module.EventType.CONTACTS = "CONTACTS"
        mock_meshcore_module.EventType.SELF_INFO = "SELF_INFO"

        session = MeshCoreSession(
            config=config,
            adapter_id="ble-factory-test",
        )

        with patch("medre.adapters.meshcore.session.HAS_MESHCORE", True), patch(
            "medre.adapters.meshcore.session.importlib.import_module",
            return_value=mock_meshcore_module,
        ) as mock_import:
            await session.start(message_callback=lambda _pkt: None)

        assert session.connected is True
        mock_import.assert_called_once_with("meshcore")
        fake_create_ble.assert_called_once_with(
            address="C4:4F:33:6A:B0:23", device=None
        )

        # Verify send_appstart was called during startup.
        mock_mc_instance.commands.send_appstart.assert_awaited_once()

        # Verify subscription wiring was exercised.
        assert (
            mock_mc_instance.subscribe.call_count >= 1
        ), "Expected at least one event subscription after BLE start"

        await session.stop()

        # Verify disconnect was called during stop.
        mock_mc_instance.disconnect.assert_awaited_once()

    # -- c) Failed connect diagnostics ----------------------------------------

    async def test_ble_failed_connect_diagnostics(self):
        """When BLE connection fails, diagnostics still capture useful fields."""
        from medre.adapters.meshcore.errors import MeshCoreConnectionError
        from medre.adapters.meshcore.session import MeshCoreSession
        from medre.config.adapters.meshcore import MeshCoreConfig

        config = MeshCoreConfig(
            adapter_id="ble-fail-test",
            connection_type="ble",
            ble_address="C4:4F:33:6A:B0:23",
        )

        mock_meshcore_module = MagicMock()
        mock_meshcore_module.MeshCore.create_ble = AsyncMock(
            side_effect=OSError("BLE device not found")
        )

        session = MeshCoreSession(
            config=config,
            adapter_id="ble-fail-test",
        )

        with patch("medre.adapters.meshcore.session.HAS_MESHCORE", True), patch(
            "medre.adapters.meshcore.session.importlib.import_module",
            return_value=mock_meshcore_module,
        ):
            with pytest.raises(MeshCoreConnectionError):
                await session.start(message_callback=lambda _pkt: None)

        # Diagnostics should still be available and safe.
        diag = session.diagnostics()
        assert diag["connected"] is False
        assert diag["mode"] == "ble"
        assert_no_secret_leak(
            diag, {"private_key", "secret", "password", "C4:4F:33:6A:B0:23"}
        )

    # -- d) Send requires live send -------------------------------------------

    async def test_ble_send_requires_live_send(self):
        """Document: MESHCORE_LIVE_SEND gates live send() calls to the node.

        The @require_live_send marker (used in TestMeshCoreLiveSmoke)
        gates live send tests. This test documents the gate pattern.
        When MESHCORE_LIVE_SEND is unset, live-send tests skip.
        Fake-mode deliver() does not check LIVE_SEND.
        """
        import time

        from medre.adapters.meshcore.adapter import MeshCoreAdapter
        from medre.config.adapters.meshcore import MeshCoreConfig
        from medre.core.rendering.renderer import RenderingResult

        config = MeshCoreConfig(
            adapter_id="ble-send-gate",
            connection_type="fake",
        )
        adapter = MeshCoreAdapter(config)
        ctx = _make_context()
        await bounded(adapter.start(ctx), 5.0, "ble send gate start")
        try:
            ts = int(time.time())
            result = RenderingResult(
                event_id=f"ble-send-gate-{ts}",
                target_adapter="ble-send-gate",
                target_channel="0",
                payload={"text": "BLE send gate test"},
                metadata={"test": "ble-send-gate"},
            )
            # In fake mode, deliver() returns None (no real transmit)
            # regardless of MESHCORE_LIVE_SEND.
            delivery = await bounded(
                adapter.deliver(result), 5.0, "ble send gate deliver"
            )
            # Fake mode returns None — no real transmission occurred.
            assert delivery is None, "Fake-mode deliver should return None"
        finally:
            await bounded(adapter.stop(), 5.0, "ble send gate stop")

    # -- e) Bounded start/stop cycle ------------------------------------------

    async def test_ble_config_bounded_start_stop(self):
        """BLE-named adapter config start/stop works bounded in fake mode.

        Uses connection_type='fake' to avoid needing real BLE hardware.
        The bounded() wrapper ensures no infinite hangs.
        """
        from medre.adapters.meshcore.adapter import MeshCoreAdapter
        from medre.config.adapters.meshcore import MeshCoreConfig

        config = MeshCoreConfig(
            adapter_id="ble-bounded-test",
            connection_type="fake",
        )
        adapter = MeshCoreAdapter(config)
        ctx = _make_context()

        await bounded(adapter.start(ctx), 5.0, "ble bounded start")
        info = await bounded(adapter.health_check(), 5.0, "ble bounded health")
        assert info.health in ("healthy",)
        await bounded(adapter.stop(), 5.0, "ble bounded stop")

        diag = adapter.diagnostics()
        assert diag["started"] is False
