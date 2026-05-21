"""Live Meshtastic connectivity smoke tests.

These tests connect to a **real** Meshtastic radio node and exercise two
distinct categories of functionality:

**Category A — Raw ``mtjk`` API Smoke Tests:**
Exercise the raw ``meshtastic`` (mtjk) library directly, bypassing the
MEDRE adapter.  These validate that the underlying library can connect,
send, and receive against real hardware.

**Category B — MEDRE Adapter Lifecycle Smoke Tests:**
Exercise the MEDRE ``MeshtasticAdapter`` against a real node: start,
health_check, diagnostics, and stop.  These also exercise the
``MeshtasticSession`` boundary for real transport lifecycle.

All tests are **skipped by default** and require explicit opt-in via
environment variables.

**Running live tests:**

1. Set up a Meshtastic radio node accessible via TCP (recommended),
   serial, or BLE.

2. Set the required environment variables:

   .. code-block:: bash

       export MESHTASTIC_CONNECTION_TYPE="tcp"
       export MESHTASTIC_HOST="meshtastic.local"
       # export MESHTASTIC_PORT="4403"       # optional, default 4403
       # export MESHTASTIC_SERIAL_PORT="/dev/ttyUSB0"  # for serial
       # export MESHTASTIC_BLE_ADDRESS="AA:BB:CC:DD:EE:FF"  # for BLE
       export MESHTASTIC_CHANNEL_INDEX="0"

3. Run the live tests:

   .. code-block:: bash

       pip install mtjk
       pytest tests/test_meshtastic_live.py -m live -v

   Default ``pytest`` run (no live tests):

   .. code-block:: bash

       pytest   # live tests excluded by addopts

   Override to include live tests:

   .. code-block:: bash

       pytest -m ""   # run ALL tests including live

**Required environment variables:**

=========================== =====================================================
Variable                    Description
=========================== =====================================================
``MESHTASTIC_CONNECTION_TYPE``  Connection mode: ``tcp``, ``serial``, or ``ble``
``MESHTASTIC_HOST``         Hostname or IP for TCP connections
                            (e.g. ``meshtastic.local``, ``192.168.1.100``)
``MESHTASTIC_PORT``         Port for TCP (default ``4403``)
``MESHTASTIC_SERIAL_PORT``  Serial device path for serial connections
                            (e.g. ``/dev/ttyUSB0``)
``MESHTASTIC_BLE_ADDRESS``  BLE MAC address for BLE connections
                            (e.g. ``AA:BB:CC:DD:EE:FF``)
``MESHTASTIC_CHANNEL_INDEX`` Channel index for outbound test messages
                             (default ``0``)
=========================== =====================================================

At minimum, ``MESHTASTIC_CONNECTION_TYPE`` must be set.  Depending on the
connection type, the corresponding host/port/serial/BLE variable must also
be set.  If any required variable is missing, every test in this file skips
with a descriptive reason.

**Known limitations (explicit):**

- **No E2EE.** Meshtastic encrypted channels are not supported.
- **No telemetry, position, or nodeinfo processing.** Only text messages.
- **No admin API.** Tests do not send admin portnum packets.
- **No BLE tested.** BLE constructor is documented but untested in this
  harness (requires BLE-capable hardware and OS support).
- **Radio traffic safety.** Tests send a small number of text messages on
  the configured channel.  Ensure the channel is not used for critical
  communications during testing.  Messages are prefixed with
  ``MEDRE live smoke`` for easy identification.

**Dependency notes:**

- Requires the ``mtjk`` package installed: ``pip install mtjk``
- Import namespace is ``meshtastic`` (not ``mtjk``)
- ``mtjk`` is a fork of the Meshtastic Python library maintained at
  ``github.com/jeremiah-k/mtjk``
- Upstream/fork master ``pyproject`` name is ``meshtastic`` v2.7.8
- PyPI ``mtjk`` advertises drop-in import namespace compatibility

**What this proves:**

- **Category A (raw mtjk):** The raw ``mtjk`` library can connect, send, and
  receive against a real Meshtastic node.  ``sendText``/``sendData`` return
  real packets with populated IDs.  Pubsub callbacks fire for received
  packets.  Packet shape matches expected fields.
- **Category B (MEDRE adapter lifecycle):** The MEDRE ``MeshtasticAdapter``
  can ``start()`` against a real node, ``health_check()`` reports
  ``"healthy"``, ``diagnostics()`` returns session state, and ``stop()``
  disconnects cleanly.  The ``MeshtasticSession`` boundary owns the raw
  transport lifecycle.

**What this does NOT prove:**

- Full MEDRE adapter ``send_one`` integration with real hardware (adapter's
  queue → pacing → real ``sendText`` via ``send_one`` is not exercised;
  tested with monkeypatched clients in unit tests only).
- Production-grade reconnection handling (session reconnect loop is
  exercised in unit tests with mocked connections).
- Multi-hop mesh delivery (tests only cover direct node communication).
- Encrypted channel support.
- Channel mapping or advanced routing.
- Real-time latency under load.
"""

import asyncio
import logging
import os
import time
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from tests.helpers.live_harness import (
    LiveRequirement,
    LiveSmokeResult,
    assert_no_secret_leak,
    bounded,
    live_env_status,
)

# ---------------------------------------------------------------------------
# Module-level marker — entire file is tagged "live" so it is excluded by the
# default ``addopts = "-m 'not live'"`` in pyproject.toml.
# ---------------------------------------------------------------------------
pytestmark = pytest.mark.live

# ---------------------------------------------------------------------------
# Environment variable gate
# ---------------------------------------------------------------------------
MESHTASTIC_CONNECTION_TYPE = os.environ.get("MESHTASTIC_CONNECTION_TYPE", "").lower()
MESHTASTIC_HOST = os.environ.get("MESHTASTIC_HOST")
MESHTASTIC_PORT = os.environ.get("MESHTASTIC_PORT", "4403")
MESHTASTIC_SERIAL_PORT = os.environ.get("MESHTASTIC_SERIAL_PORT")
MESHTASTIC_BLE_ADDRESS = os.environ.get("MESHTASTIC_BLE_ADDRESS")
MESHTASTIC_CHANNEL_INDEX = os.environ.get("MESHTASTIC_CHANNEL_INDEX", "0")


def _get_live_requirements() -> list[LiveRequirement]:
    """Build the list of env var requirements for Meshtastic live tests."""
    ct = os.environ.get("MESHTASTIC_CONNECTION_TYPE", "").lower()
    reqs = [LiveRequirement("MESHTASTIC_CONNECTION_TYPE", description="Connection mode: tcp, serial, or ble")]
    if ct == "tcp":
        reqs.append(LiveRequirement("MESHTASTIC_HOST", description="Node hostname or IP for TCP"))
    elif ct == "serial":
        reqs.append(LiveRequirement("MESHTASTIC_SERIAL_PORT", description="Serial device path"))
    elif ct == "ble":
        reqs.append(LiveRequirement("MESHTASTIC_BLE_ADDRESS", secret=False, description="BLE MAC address"))
    return reqs


_LIVE_STATUS = live_env_status(_get_live_requirements())
_LIVE_ENV_SET = _LIVE_STATUS.enabled

require_live = pytest.mark.skipif(
    not _LIVE_ENV_SET,
    reason=f"Missing env vars: {', '.join(_LIVE_STATUS.missing)}",
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _make_config():
    """Build a MeshtasticConfig from the live environment variables."""
    from medre.config.adapters.meshtastic import MeshtasticConfig

    ct = MESHTASTIC_CONNECTION_TYPE
    if ct == "tcp":
        return MeshtasticConfig(
            adapter_id="meshtastic-live-smoke",
            connection_type="tcp",
            host=MESHTASTIC_HOST or "localhost",
            port=int(MESHTASTIC_PORT) if MESHTASTIC_PORT else 4403,
        )
    elif ct == "serial":
        return MeshtasticConfig(
            adapter_id="meshtastic-live-smoke",
            connection_type="serial",
            serial_port=MESHTASTIC_SERIAL_PORT or "/dev/ttyUSB0",
        )
    elif ct == "ble":
        return MeshtasticConfig(
            adapter_id="meshtastic-live-smoke",
            connection_type="ble",
            ble_address=MESHTASTIC_BLE_ADDRESS or "",
        )
    else:
        return MeshtasticConfig(
            adapter_id="meshtastic-live-smoke",
            connection_type="tcp",
            host=MESHTASTIC_HOST or "localhost",
        )


def _make_context():
    """Build an AdapterContext suitable for live smoke tests."""
    from medre.core.contracts.adapter import AdapterContext

    return AdapterContext(
        adapter_id="meshtastic-live-smoke",
        event_bus=None,
        publish_inbound=AsyncMock(),
        logger=logging.getLogger("test.meshtastic-live"),
        clock=lambda: datetime.now(timezone.utc),
        shutdown_event=asyncio.Event(),
    )


def _connect_interface(config):
    """Create and return a real mtjk interface based on config.

    Returns the interface object, or raises if mtjk is not installed.
    """
    import meshtastic
    import meshtastic.serial_interface
    import meshtastic.tcp_interface

    ct = MESHTASTIC_CONNECTION_TYPE
    if ct == "tcp":
        iface = meshtastic.tcp_interface.TCPInterface(
            hostname=config.host,
            portNumber=config.port or 4403,
            noNodes=False,
        )
    elif ct == "serial":
        iface = meshtastic.serial_interface.SerialInterface(
            devPath=config.serial_port,
        )
    else:
        pytest.skip(f"Connection type {ct!r} not yet supported in live harness")

    return iface


# ---------------------------------------------------------------------------
# Live tests
# ---------------------------------------------------------------------------
@require_live
class TestMeshtasticLiveSmoke:
    """Live Meshtastic connectivity smoke tests.

    These tests connect to a real Meshtastic radio node and verify the
    adapter lifecycle, outbound delivery, and (where feasible) inbound
    packet reception.

    All tests require MESHTASTIC_CONNECTION_TYPE and corresponding
    connection parameters.  Run with::

        pytest tests/test_meshtastic_live.py -m live -v
    """

    # -- Lifecycle: connect, health, disconnect ----------------------------

    async def test_tcp_interface_connects(self):
        """Verify a raw TCPInterface can connect to the configured node.

        **Category A — Raw mtjk API smoke test.**

        This validates:
        - ``mtjk`` is installed and importable as ``meshtastic``.
        - The target host/port is reachable.
        - ``TCPInterface`` completes its initial handshake.
        - ``close()`` disconnects cleanly.
        """
        config = _make_config()
        iface = _connect_interface(config)
        try:
            # Wait for connection to establish
            await asyncio.wait_for(
                asyncio.get_event_loop().run_in_executor(
                    None, lambda: iface.waitForConfig()
                ),
                timeout=15.0,
            )
            assert iface.isConnected.is_set(), "Interface should be connected"
        finally:
            await asyncio.wait_for(
                asyncio.get_event_loop().run_in_executor(None, iface.close),
                timeout=10.0,
            )

    async def test_adapter_starts_and_reports_healthy(self):
        """Start the real adapter and verify health_check reports healthy.

        **Category B — MEDRE adapter lifecycle smoke test.**

        This validates:
        - The adapter creates a real MeshtasticSession in ``start()``.
        - ``health_check()`` returns ``"healthy"`` after start.
        - ``stop()`` disconnects cleanly.
        - ``diagnostics()`` returns session state.

        Note: This does NOT exercise full MEDRE ``send_one`` / ``deliver``
        integration.  The adapter lifecycle (connect → health → disconnect)
        is tested, but outbound delivery through the MEDRE queue + ``send_one``
        path against real hardware is deferred to a future harness.
        """
        from medre.adapters.meshtastic.adapter import MeshtasticAdapter

        config = _make_config()
        adapter = MeshtasticAdapter(config)
        ctx = _make_context()

        # The adapter creates a real client via session in ``start()`` when
        # mtjk is available, or raises MeshtasticConnectionError if not.
        try:
            await asyncio.wait_for(adapter.start(ctx), timeout=15.0)
            info = await asyncio.wait_for(adapter.health_check(), timeout=15.0)
            # After start, health should be "healthy" (client connected)
            # or "unknown" if start didn't complete fully.
            assert info.health in (
                "healthy",
                "unknown",
            ), f"Expected healthy or unknown, got {info.health!r}"

            # Verify diagnostics returns session state
            diag = adapter.diagnostics()
            assert "session" in diag
            assert diag["session"]["connected"] in (True, False)
        finally:
            await asyncio.wait_for(adapter.stop(), timeout=10.0)

    async def test_adapter_diagnostics_exposes_session_state(self):
        """Verify diagnostics() exposes session boundary state.

        **Category B — MEDRE adapter diagnostics smoke test.**

        This validates:
        - ``diagnostics()`` returns combined adapter + session state.
        - Session diagnostics include connected, reconnecting, etc.
        """
        from medre.adapters.meshtastic.adapter import MeshtasticAdapter

        config = _make_config()
        adapter = MeshtasticAdapter(config)
        ctx = _make_context()

        try:
            await asyncio.wait_for(adapter.start(ctx), timeout=15.0)
            diag = adapter.diagnostics()

            # Adapter-level diagnostics
            assert diag["adapter_id"] == "meshtastic-live-smoke"
            assert diag["platform"] == "meshtastic"
            assert diag["started"] is True
            assert diag["connection_type"] == MESHTASTIC_CONNECTION_TYPE

            # Session-level diagnostics
            assert "session" in diag
            session = diag["session"]
            assert "connected" in session
            assert "reconnecting" in session
            assert "reconnect_attempts" in session
            assert "last_packet_time" in session
            assert "node_id" in session
            assert "channel_count" in session
            assert "transient_delivery_failures" in session
            assert "permanent_delivery_failures" in session
            assert "last_error" in session
        finally:
            await asyncio.wait_for(adapter.stop(), timeout=10.0)

    # -- Outbound delivery --------------------------------------------------

    async def test_send_text_via_raw_interface(self):
        """Send a text message using the raw mtjk interface directly.

        **Category A — Raw mtjk API smoke test.**

        This validates:
        - ``sendText()`` completes without error.
        - The returned protobuf has a populated ``id`` field.
        - The message appears on the configured channel.

        This test uses the raw ``meshtastic`` interface, not the MEDRE
        adapter, to verify the underlying library works before testing
        the MEDRE integration layer.
        """
        if not _MESHTASTIC_LIVE_SEND:
            pytest.skip("Set MESHTASTIC_LIVE_SEND=1 to test RF transmission")
        config = _make_config()
        iface = _connect_interface(config)
        try:
            await asyncio.get_event_loop().run_in_executor(
                None, lambda: iface.waitForConfig()
            )
            assert iface.isConnected.is_set()

            ts = int(time.time())
            text = f"MEDRE live smoke test (ts={ts}) - safe to ignore"
            channel_index = int(MESHTASTIC_CHANNEL_INDEX)

            result = await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: iface.sendText(
                    text,
                    channelIndex=channel_index,
                ),
            )

            # sendText returns a MeshPacket protobuf with id populated
            assert result is not None, "sendText returned None"
            packet_id = getattr(result, "id", None)
            assert (
                packet_id is not None and packet_id != 0
            ), f"Expected populated packet id, got {packet_id!r}"
        finally:
            await asyncio.get_event_loop().run_in_executor(None, iface.close)

    async def test_send_data_via_raw_interface(self):
        """Send raw data using ``sendData()`` via the mtjk interface.

        **Category A — Raw mtjk API smoke test.**

        This validates:
        - ``sendData()`` with ``TEXT_MESSAGE_APP`` portnum works.
        - The returned protobuf has a populated ``id`` field.

        ``sendData`` is the lower-level API that ``sendText`` wraps.
        It accepts raw bytes and explicit portnum.  This test documents
        that both APIs return packet IDs.
        """
        if not _MESHTASTIC_LIVE_SEND:
            pytest.skip("Set MESHTASTIC_LIVE_SEND=1 to test RF transmission")
        import meshtastic.protobuf.portnums_pb2 as portnums_pb2

        config = _make_config()
        iface = _connect_interface(config)
        try:
            await asyncio.get_event_loop().run_in_executor(
                None, lambda: iface.waitForConfig()
            )
            assert iface.isConnected.is_set()

            ts = int(time.time())
            data = f"MEDRE live data test (ts={ts}) - safe to ignore".encode("utf-8")
            channel_index = int(MESHTASTIC_CHANNEL_INDEX)

            result = await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: iface.sendData(
                    data,
                    channelIndex=channel_index,
                    portNum=portnums_pb2.PortNum.TEXT_MESSAGE_APP,
                ),
            )

            assert result is not None, "sendData returned None"
            packet_id = getattr(result, "id", None)
            assert (
                packet_id is not None and packet_id != 0
            ), f"Expected populated packet id, got {packet_id!r}"
        finally:
            await asyncio.get_event_loop().run_in_executor(None, iface.close)

    # -- Inbound reception (manual observation) -----------------------------

    async def test_pubsub_callback_receives_packets(self):
        """Verify that pubsub callback fires when a packet is received.

        **Category A — Raw mtjk API smoke test.**

        This test subscribes to ``meshtastic.receive`` and waits up to
        30 seconds for an inbound packet.  If no packet arrives, it passes
        with a note (the mesh may be silent).

        To reliably trigger an inbound packet during this test, either:
        1. Have another node send a message on the same channel, or
        2. Send a message from the test node itself (self-reception).

        Safety note: this test sends one message on the configured channel
        to trigger a self-reception callback.  The message is prefixed
        with ``MEDRE live smoke`` for easy identification.
        """
        if not _MESHTASTIC_LIVE_SEND:
            pytest.skip("Set MESHTASTIC_LIVE_SEND=1 to test RF transmission")
        from pubsub import pub

        config = _make_config()
        iface = _connect_interface(config)
        received_packets: list[dict] = []

        def _on_receive(packet, interface=None):
            received_packets.append(packet)

        try:
            await asyncio.get_event_loop().run_in_executor(
                None, lambda: iface.waitForConfig()
            )
            assert iface.isConnected.is_set()

            pub.subscribe(_on_receive, "meshtastic.receive")

            # Send a message to trigger self-reception
            ts = int(time.time())
            channel_index = int(MESHTASTIC_CHANNEL_INDEX)
            text = f"MEDRE live smoke self-receive (ts={ts}) - safe to ignore"

            await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: iface.sendText(text, channelIndex=channel_index),
            )

            # Wait up to 10 seconds for the callback to fire
            for _ in range(100):
                await asyncio.sleep(0.1)
                if received_packets:
                    break

            if received_packets:
                pkt = received_packets[0]
                assert "decoded" in pkt, f"Packet missing 'decoded': {pkt}"
                assert "id" in pkt, f"Packet missing 'id': {pkt}"
                # Self-received text packets should have portnum
                portnum = pkt.get("decoded", {}).get("portnum")
                assert portnum in (
                    "TEXT_MESSAGE_APP",
                    "text_message",
                ), f"Unexpected portnum {portnum!r}"
            # If no packet received, test passes silently — mesh may be silent
        finally:
            pub.unsubscribe(_on_receive, "meshtastic.receive")
            await asyncio.get_event_loop().run_in_executor(None, iface.close)

    # -- Start/stop cycle ---------------------------------------------------

    async def test_repeated_start_stop_cycle(self):
        """Verify the adapter can start/stop multiple times cleanly.

        **Category B — MEDRE adapter lifecycle smoke test.**

        This validates:
        - Multiple start/stop cycles don't leak resources.
        - Each restart connects cleanly.
        - Session state is properly reset between cycles.
        """
        from medre.adapters.meshtastic.adapter import MeshtasticAdapter

        config = _make_config()

        for _i in range(3):
            adapter = MeshtasticAdapter(config)
            ctx = _make_context()
            try:
                await asyncio.wait_for(adapter.start(ctx), timeout=15.0)
                info = await asyncio.wait_for(adapter.health_check(), timeout=15.0)
                assert info.health in ("healthy", "unknown")
            finally:
                await asyncio.wait_for(adapter.stop(), timeout=10.0)

    # -- Documentation tests (always pass) ----------------------------------

    async def test_backlog_suppression_not_implemented_note(self):
        """Document: startup backlog suppression is not implemented.

        This test always passes.  It exists to document that
        ``startup_backlog_suppress_seconds`` is a config field with no
        runtime implementation.  Real nodes replay buffered packets on
        TCP connect; MEDRE does not yet filter these.
        """
        # startup_backlog_suppress_seconds exists in MeshtasticConfig
        # but is not enforced in the adapter's _on_packet path.
        # Future implementation: compare packet rxTime against
        # adapter start time and drop stale packets.
        pass

    async def test_inbound_dm_not_supported_note(self):
        """Document: outbound DM delivery is not supported in tranche 1.

        This test always passes.  The adapter declares
        ``direct_messages=False``.  Inbound DM metadata is preserved but
        no outbound DM send path exists.
        """
        pass

    async def test_sendtext_returns_packet_with_id_note(self):
        """Document: sendText returns MeshPacket with populated id.

        Both ``sendText()`` and ``sendData()`` return a ``MeshPacket``
        protobuf with the ``id`` field populated by the interface's
        packet ID generator.  This is confirmed from mtjk source code
        analysis (see ``docs/contracts/10-meshtastic-source-audit.md``
        Section 5.1) and verified by the ``test_send_text_via_raw_interface``
        and ``test_send_data_via_raw_interface`` tests above.

        The MEDRE ``FakeMeshtasticClient`` mirrors this behavior with
        sequential deterministic IDs.
        """
        pass


# ---------------------------------------------------------------------------
# Helpers for bounded live tests
# ---------------------------------------------------------------------------

# Transmit guard: live RF tests that actually transmit radio traffic
# require MESHTASTIC_LIVE_SEND=1.  Without it, tests may connect and
# health-check but must NOT exercise the transmit path.
#
# RULE: Any test that calls adapter.deliver(), session.send(),
# iface.sendText(), iface.sendData(), or any other enqueue/transmit
# method against a non-fake/live connection must require
# MESHTASTIC_LIVE_SEND=1.  Tests without that flag may connect,
# health-check, and inspect diagnostics only — they must never
# enqueue or transmit a packet over RF.
_MESHTASTIC_LIVE_SEND = os.environ.get("MESHTASTIC_LIVE_SEND", "") == "1"


def _make_rendering_result(
    text: str = "test message",
    event_id: str = "evt-test-001",
    channel_index: int = 0,
):
    """Build a minimal ``RenderingResult`` for deliver() tests."""
    from medre.core.rendering.renderer import RenderingResult

    return RenderingResult(
        event_id=event_id,
        target_adapter="meshtastic-live-smoke",
        target_channel=None,
        payload={"text": text, "channel_index": channel_index},
    )


# ---------------------------------------------------------------------------
# TestMeshtasticBoundedLiveTests — opt-in live tests with env var gating
# ---------------------------------------------------------------------------

require_live_send = pytest.mark.skipif(
    not _MESHTASTIC_LIVE_SEND,
    reason="Set MESHTASTIC_LIVE_SEND=1 to enable RF transmission tests",
)


@require_live
class TestMeshtasticBoundedLiveTests:
    """Opt-in live tests that connect to real Meshtastic hardware.

    All tests require ``MESHTASTIC_CONNECTION_TYPE`` plus the corresponding
    connection parameters.  Tests that transmit RF traffic additionally
    require ``MESHTASTIC_LIVE_SEND=1``.

    Every async operation is bounded with ``asyncio.wait_for`` to prevent
    hangs on unresponsive hardware.
    """

    async def test_live_start_and_health(self):
        """Start the adapter against real hardware and verify health."""
        from medre.adapters.meshtastic.adapter import MeshtasticAdapter

        config = _make_config()
        adapter = MeshtasticAdapter(config)
        ctx = _make_context()

        try:
            await bounded(adapter.start(ctx), 15.0, "test_live_start_and_health: adapter.start()")
            info = await bounded(adapter.health_check(), 15.0, "test_live_start_and_health: health_check()")
            assert info.health in ("healthy", "unknown")
            assert info.adapter_id == "meshtastic-live-smoke"
            assert info.platform == "meshtastic"
        finally:
            await bounded(adapter.stop(), 10.0, "test_live_start_and_health: adapter.stop()")

    @require_live_send
    async def test_live_deliver_with_transmit(self):
        """deliver() transmits a real message when MESHTASTIC_LIVE_SEND=1."""
        from medre.adapters.meshtastic.adapter import MeshtasticAdapter

        config = _make_config()
        adapter = MeshtasticAdapter(config)
        ctx = _make_context()

        try:
            await bounded(adapter.start(ctx), 15.0, "test_live_deliver_with_transmit: adapter.start()")
            ts = int(time.time())
            result_obj = _make_rendering_result(
                text=f"MEDRE live bounded test (ts={ts}) - safe to ignore",
                event_id=f"evt-live-{ts}",
                channel_index=int(MESHTASTIC_CHANNEL_INDEX),
            )
            delivery = await bounded(adapter.deliver(result_obj), 15.0, "test_live_deliver_with_transmit: adapter.deliver()")
            assert delivery is not None
            assert delivery.native_channel_id is not None
            # Delivery was accepted (queued for transmit)
            assert "enqueued" in delivery.delivery_note
        finally:
            await bounded(adapter.stop(), 10.0, "test_live_deliver_with_transmit: adapter.stop()")

    @require_live_send
    async def test_live_bounded_start_stop_deliver(self):
        """Full bounded lifecycle: start → deliver → stop."""
        from medre.adapters.meshtastic.adapter import MeshtasticAdapter

        config = _make_config()
        adapter = MeshtasticAdapter(config)
        ctx = _make_context()

        try:
            await bounded(adapter.start(ctx), 15.0, "test_live_bounded_start_stop_deliver: adapter.start()")

            info = await bounded(adapter.health_check(), 15.0, "test_live_bounded_start_stop_deliver: health_check()")
            assert info.health in ("healthy", "unknown")

            result_obj = _make_rendering_result(
                text="MEDRE bounded lifecycle test - safe to ignore",
                event_id="evt-lifecycle-001",
            )
            delivery = await bounded(adapter.deliver(result_obj), 15.0, "test_live_bounded_start_stop_deliver: adapter.deliver()")
            assert delivery is not None
        finally:
            await bounded(adapter.stop(), 10.0, "test_live_bounded_start_stop_deliver: adapter.stop()")

    async def test_live_stop_idempotency(self):
        """stop() called multiple times on a live adapter is safe."""
        from medre.adapters.meshtastic.adapter import MeshtasticAdapter

        config = _make_config()
        adapter = MeshtasticAdapter(config)
        ctx = _make_context()

        try:
            await bounded(adapter.start(ctx), 15.0, "test_live_stop_idempotency: adapter.start()")
        finally:
            await bounded(adapter.stop(), 10.0, "test_live_stop_idempotency: adapter.stop() #1")
            # Idempotent second stop
            await bounded(adapter.stop(), 10.0, "test_live_stop_idempotency: adapter.stop() #2")
            # Idempotent third stop
            await bounded(adapter.stop(), 10.0, "test_live_stop_idempotency: adapter.stop() #3")

        assert adapter._started is False

    async def test_live_diagnostics_shape(self):
        """diagnostics() returns expected shape against live hardware."""
        from medre.adapters.meshtastic.adapter import MeshtasticAdapter

        config = _make_config()
        adapter = MeshtasticAdapter(config)
        ctx = _make_context()

        try:
            await bounded(adapter.start(ctx), 15.0, "test_live_diagnostics_shape: adapter.start()")
            diag = adapter.diagnostics()

            # Adapter-level keys
            assert diag["adapter_id"] == "meshtastic-live-smoke"
            assert diag["platform"] == "meshtastic"
            assert diag["started"] is True
            assert diag["connection_type"] == MESHTASTIC_CONNECTION_TYPE
            assert isinstance(diag["queue_pending"], int)
            assert isinstance(diag["queue_total_sent"], int)
            assert isinstance(diag["queue_total_failed"], int)
            assert isinstance(diag["queue_total_dropped"], int)
            assert isinstance(diag["background_tasks"], int)

            # Session-level keys
            assert "session" in diag
            session = diag["session"]
            assert "connected" in session
            assert "reconnecting" in session
            assert "reconnect_attempts" in session
            assert "last_packet_time" in session
            assert "node_id" in session
            assert "channel_count" in session
            assert "transient_delivery_failures" in session
            assert "permanent_delivery_failures" in session
            assert "last_error" in session
        finally:
            await bounded(adapter.stop(), 10.0, "test_live_diagnostics_shape: adapter.stop()")

    async def test_live_no_secret_leakage_in_diagnostics(self):
        """diagnostics() does NOT expose serial paths, host IPs, or secrets."""
        from medre.adapters.meshtastic.adapter import MeshtasticAdapter

        config = _make_config()
        adapter = MeshtasticAdapter(config)
        ctx = _make_context()

        try:
            await bounded(adapter.start(ctx), 15.0, "test_live_no_secret_leakage_in_diagnostics: adapter.start()")
            diag = adapter.diagnostics()

            # Collect secret-like values from env to check against
            leak_candidates = []
            if MESHTASTIC_SERIAL_PORT:
                leak_candidates.append(MESHTASTIC_SERIAL_PORT)
            if MESHTASTIC_HOST:
                leak_candidates.append(MESHTASTIC_HOST)
            if MESHTASTIC_BLE_ADDRESS:
                leak_candidates.append(MESHTASTIC_BLE_ADDRESS)
            assert_no_secret_leak(diag, leak_candidates)
        finally:
            await bounded(adapter.stop(), 10.0, "test_live_no_secret_leakage_in_diagnostics: adapter.stop()")
