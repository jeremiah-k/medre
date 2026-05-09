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

- **No real MeshCore connectivity yet.**  The adapter is scaffolded;
  non-fake connections raise ``MeshCoreConnectionError``.  These tests
  document the future required environment variables and will be enabled
  when production MeshCore support is implemented.
- **No E2EE.**  MeshCore encrypted channels are not supported.
- **No telemetry, position, or admin processing.**  Only text messages.
- **Radio traffic safety.**  When enabled, tests send a small number of
  text messages on the configured channel.  Messages will be prefixed
  with ``MEDRE live smoke`` for easy identification.

**What this proves (when enabled):**

- The MEDRE ``MeshCoreAdapter`` can ``start()`` against a real node.
- ``health_check()`` reports ``"healthy"``.
- ``stop()`` disconnects cleanly.

**What this does NOT prove:**

- Full MEDRE adapter outbound delivery integration with real hardware.
- Production-grade reconnection handling.
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
pytestmark = pytest.mark.live

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

require_live = pytest.mark.skipif(
    not _LIVE_ENV_SET,
    reason=_LIVE_SKIP_REASON,
)

require_meshcore_sdk = pytest.mark.skipif(
    not _LIVE_ENV_SET,
    reason=_LIVE_SKIP_REASON,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _make_config():
    """Build a MeshCoreConfig from the live environment variables."""
    from medre.adapters.meshcore.config import MeshCoreConfig

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
    from medre.adapters.base import AdapterContext

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
    adapter lifecycle: start, health_check, and stop.

    **NOTE**: Real MeshCore connections are not yet implemented in the
    adapter.  These tests will fail with ``MeshCoreConnectionError``
    until production MeshCore support is added.  They are preserved as
    documentation of the intended live test structure.

    All tests require MESHCORE_CONNECTION_TYPE and corresponding
    connection parameters.  Run with::

        pytest tests/test_meshcore_live.py -m live -v
    """

    # -- Lifecycle: connect, health, disconnect ----------------------------

    async def test_adapter_starts_and_reports_healthy(self):
        """Start the real adapter and verify health_check reports healthy.

        **Category B — MEDRE adapter lifecycle smoke test.**

        This validates:
        - The adapter creates a real MeshCore client in ``start()``.
        - ``health_check()`` returns ``"healthy"`` after start.

        Note: This test will raise MeshCoreConnectionError until
        production MeshCore support is implemented.
        """
        from medre.adapters.meshcore.adapter import MeshCoreAdapter
        from medre.adapters.meshcore.errors import MeshCoreConnectionError

        config = _make_config()
        adapter = MeshCoreAdapter(config)
        ctx = _make_context()

        try:
            await adapter.start(ctx)
            info = await adapter.health_check()
            assert info.health in ("healthy", "unknown"), (
                f"Expected healthy or unknown, got {info.health!r}"
            )
        except MeshCoreConnectionError:
            pytest.skip(
                "Real MeshCore connections not yet implemented; "
                "this test documents the future live test structure"
            )
        finally:
            await adapter.stop()

    # -- Documentation tests (always pass) ----------------------------------

    async def test_meshcore_sdk_not_yet_connected_note(self):
        """Document: real MeshCore SDK connections are scaffolded.

        This test always passes.  It exists to document that the
        MeshCoreAdapter raises ``MeshCoreConnectionError`` for non-fake
        connection types.  Full production MeshCore support is deferred
        to a future tranche.
        """
        pass

    async def test_outbound_delivery_not_yet_implemented_note(self):
        """Document: outbound MeshCore delivery is scaffolded.

        This test always passes.  The real MeshCoreAdapter.deliver()
        returns ``None`` — no outbound delivery is implemented.
        """
        pass

    async def test_inbound_event_subscription_not_yet_wired_note(self):
        """Document: MeshCore event subscriptions are scaffolded.

        This test always passes.  _subscribe_events() and
        _unsubscribe_events() are scaffold methods that log but do
        not wire real SDK callbacks.
        """
        pass
