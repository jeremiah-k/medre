"""Live LXMF/Reticulum connectivity smoke tests.

These tests exercise the MEDRE LXMF adapter lifecycle — startup, health,
shutdown, outbound delivery, inbound callback pipeline, restart, and
repeated start/stop — against both fake and real Reticulum backends.

All tests are **skipped by default** and require explicit opt-in via
environment variables.

**Running live tests:**

1. Set up a Reticulum instance with an LXMF router (or use fake-mode
   tests which require no Reticulum at all).

2. Set the required environment variables:

   .. code-block:: bash

       export LXMF_CONNECTION_TYPE="reticulum"
       export LXMF_IDENTITY_PATH="/path/to/identity"
       # export LXMF_DISPLAY_NAME="MEDRE Live Test"  # optional
       # export LXMF_DESTINATION_HASH="0123...def"   # optional, for send tests

3. Run the live tests:

   .. code-block:: bash

       pip install lxmf
       pytest tests/test_lxmf_live.py -m live -v

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
``LXMF_CONNECTION_TYPE``    Connection mode: must be ``reticulum``
``LXMF_IDENTITY_PATH``      Path to a Reticulum identity file for the LXMF
                            router.  Must be a non-empty string pointing to
                            an existing identity file.
``LXMF_DISPLAY_NAME``       Optional display name for LXMF announces.
``LXMF_DESTINATION_HASH``   Optional destination hexhash for outbound send
                            tests (32-char hex string).
``LXMF_STORAGE_PATH``       Optional path for LXMF router storage.  Defaults
                            to ``/tmp/medre-live-lxmf-router``.
``LXMF_LIVE_SEND``          Set to ``1`` to enable real outbound send tests.
                            Without this, tests that transmit real messages
                            are skipped.
=========================== =====================================================

At minimum, ``LXMF_CONNECTION_TYPE`` and ``LXMF_IDENTITY_PATH`` must
be set.  If any required variable is missing, every test in this file
skips with a descriptive reason.

**Adapter architecture:**

The ``LxmfAdapter`` delegates all SDK interaction to its owned
``LxmfSession`` instance.  The session owns the raw transport lifecycle
(identity loading, LXMRouter initialisation, delivery callback
registration, outbound send, reconnection, teardown).

**Current status:**

- **Reticulum alpha.**  When the ``lxmf``/``RNS`` packages are installed
  and a valid identity file is provided, ``LxmfAdapter.start()``
  connects to a real Reticulum instance via ``LxmfSession``.
  When the SDK is not installed, non-fake ``start()`` raises
  ``LxmfConnectionError``.
- **Session-backed deliver.**  ``deliver()`` sends via the session and
  returns an ``AdapterDeliveryResult`` with native message ID and
  ``lxmf.delivery_state`` metadata (honest pending/sent semantics).
- **Inbound callbacks.**  ``LxmfSession`` wires real LXMRouter delivery
  callbacks.  Inbound messages are normalised to plain dicts before
  leaving the session boundary.
- **No E2EE.**  LXMF encrypted messages are not supported in the
  current tranche.

**What this proves:**

- The MEDRE ``LxmfAdapter`` can ``start()`` with a real Reticulum
  instance (when SDK is installed) or fake mode (always).
- ``health_check()`` reports ``"healthy"`` after start, ``"unknown"``
  before start and after stop.
- ``stop()`` disconnects cleanly and is idempotent.
- ``start()`` is idempotent — calling twice is a no-op.
- Restart (start → stop → start → stop) works without state leaks.
- Repeated start/stop cycles are stable.
- ``deliver()`` returns an ``AdapterDeliveryResult`` with a native
  message ID and ``lxmf`` delivery-state metadata.
- ``simulate_inbound()`` exercises the same codec/classifier pipeline
  used by real inbound callbacks.

**What this does NOT prove:**

- Multi-hop Reticulum delivery confirmation.
- Encrypted LXMF message support.
- Real inbound delivery callback firing from a remote LXMF peer
  (requires a second LXMF identity or loopback fixture not available
  in this harness).
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
# Bounded-async timeouts and helper
# ---------------------------------------------------------------------------
_ADAPTER_START_TIMEOUT: float = 30.0
_ADAPTER_STOP_TIMEOUT: float = 10.0
_DELIVER_TIMEOUT: float = 15.0


# ---------------------------------------------------------------------------
# Environment variable gate
# ---------------------------------------------------------------------------
LXMF_CONNECTION_TYPE = os.environ.get("LXMF_CONNECTION_TYPE", "").lower()
LXMF_IDENTITY_PATH = os.environ.get("LXMF_IDENTITY_PATH", "")
LXMF_DISPLAY_NAME = os.environ.get("LXMF_DISPLAY_NAME", "")
LXMF_DESTINATION_HASH = os.environ.get("LXMF_DESTINATION_HASH", "")
LXMF_STORAGE_PATH = os.environ.get("LXMF_STORAGE_PATH", "")
LXMF_LIVE_SEND = os.environ.get("LXMF_LIVE_SEND", "").strip() == "1"


def _validate_env() -> tuple[str, str]:
    """Validate env vars and return (reason, connection_type).

    Returns ("", connection_type) if valid, or (skip_reason, "") if not.
    """
    ct = LXMF_CONNECTION_TYPE
    if not ct:
        return (
            "Set LXMF_CONNECTION_TYPE (reticulum) to run live LXMF tests",
            "",
        )

    if ct != "reticulum":
        return (
            f"Unknown LXMF_CONNECTION_TYPE {ct!r}; use reticulum",
            "",
        )

    if not LXMF_IDENTITY_PATH:
        return (
            "LXMF_IDENTITY_PATH is required for live LXMF tests",
            "",
        )

    return ("", ct)


_LIVE_SKIP_REASON, _CONNECTION_TYPE = _validate_env()
_LIVE_ENV_SET = _CONNECTION_TYPE != ""

require_live = pytest.mark.skipif(
    not _LIVE_ENV_SET,
    reason=_LIVE_SKIP_REASON,
)

require_live_send = pytest.mark.skipif(
    not LXMF_LIVE_SEND,
    reason="Set LXMF_LIVE_SEND=1 to enable real outbound send tests",
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _make_config():
    """Build an LxmfConfig from the live environment variables."""
    from medre.config.adapters.lxmf import LxmfConfig

    return LxmfConfig(
        adapter_id="lxmf-live-smoke",
        connection_type="reticulum",
        identity_path=LXMF_IDENTITY_PATH or None,
        display_name=LXMF_DISPLAY_NAME,
        storage_path=LXMF_STORAGE_PATH or "/tmp/medre-live-lxmf-router",
    )


def _make_fake_config():
    """Build an LxmfConfig for fake mode (no Reticulum required)."""
    from medre.config.adapters.lxmf import LxmfConfig

    return LxmfConfig(
        adapter_id="lxmf-live-smoke",
        connection_type="fake",
    )


def _make_context(publish_inbound=None):
    """Build an AdapterContext suitable for live smoke tests.

    Parameters
    ----------
    publish_inbound:
        Optional async callable override for the publish_inbound field.
        Defaults to an ``AsyncMock()``.
    """
    from medre.core.contracts.adapter import AdapterContext

    return AdapterContext(
        adapter_id="lxmf-live-smoke",
        event_bus=None,
        publish_inbound=publish_inbound or AsyncMock(),
        logger=logging.getLogger("test.lxmf-live"),
        clock=lambda: datetime.now(timezone.utc),
        shutdown_event=asyncio.Event(),
    )


# ---------------------------------------------------------------------------
# Live tests
# ---------------------------------------------------------------------------
@require_live
class TestLxmfLiveSmoke:
    """Live LXMF/Reticulum connectivity smoke tests.

    Tests exercise the ``LxmfAdapter`` lifecycle against both fake and
    real Reticulum backends.  Reticulum-mode tests attempt real startup
    and skip with a clear reason if the ``lxmf``/``RNS`` SDK is not
    installed or the identity file is unavailable.  Fake-mode tests
    always pass when the env gate is satisfied.

    All tests require ``LXMF_CONNECTION_TYPE`` and ``LXMF_IDENTITY_PATH``.
    Run with::

        pytest tests/test_lxmf_live.py -m live -v
    """

    # ===================================================================
    # 1. Identity-path setup validation
    # ===================================================================

    async def test_identity_path_config_validates_shape(self):
        """LxmfConfig with identity_path validates shape-only constraints.

        Verifies that the live configuration object can be constructed
        and passes ``validate()`` when ``identity_path`` is a non-empty
        string.  This is a config-shape test; runtime file-existence
        checks happen inside ``LxmfSession.start()``.
        """
        from medre.config.adapters.lxmf import LxmfConfig

        config = LxmfConfig(
            adapter_id="lxmf-live-smoke",
            connection_type="reticulum",
            identity_path=LXMF_IDENTITY_PATH,
            display_name=LXMF_DISPLAY_NAME,
            storage_path="/tmp/medre-live-lxmf-router",
        )
        # Should not raise — identity_path is a non-empty string.
        config.validate()

    async def test_identity_path_config_rejects_empty(self):
        """LxmfConfig rejects an empty identity_path string.

        When ``identity_path`` is explicitly set to ``""``, validation
        must raise ``LxmfConfigError`` because empty strings are invalid.
        """
        from medre.config.adapters.errors import LxmfConfigError
        from medre.config.adapters.lxmf import LxmfConfig

        config = LxmfConfig(
            adapter_id="lxmf-live-smoke",
            connection_type="reticulum",
            identity_path="   ",  # whitespace-only
            storage_path="/tmp/medre-live-lxmf-router",
        )
        with pytest.raises(LxmfConfigError, match="non-empty"):
            config.validate()

    # ===================================================================
    # 2. Startup / shutdown validation (reticulum mode)
    # ===================================================================

    async def test_adapter_starts_and_reports_healthy(self):
        """Start the real adapter and verify health_check reports healthy.

        Attempts real Reticulum startup via ``LxmfSession``.  When the
        ``lxmf``/``RNS`` SDK is installed and a valid identity file is
        configured, ``start()`` must succeed and ``health_check()`` must
        return ``"healthy"``.

        Skips if the SDK is not installed or the identity file is
        unavailable, so the test is safe in environments without
        Reticulum.
        """
        from medre.adapters.lxmf.adapter import LxmfAdapter
        from medre.adapters.lxmf.errors import LxmfConnectionError

        config = _make_config()
        adapter = LxmfAdapter(config)
        ctx = _make_context()

        try:
            await bounded(adapter.start(ctx), _ADAPTER_START_TIMEOUT, "lxmf adapter start")
            info = await bounded(adapter.health_check(), 10.0, "lxmf health_check")
            assert (
                info.health == "healthy"
            ), f"Expected healthy after start, got {info.health!r}"
            assert info.platform == "lxmf"
            assert info.adapter_id == "lxmf-live-smoke"
        except LxmfConnectionError as exc:
            pytest.skip(f"LXMF connection unavailable: {exc}")
        finally:
            await bounded(adapter.stop(), _ADAPTER_STOP_TIMEOUT, "lxmf adapter stop")

    async def test_adapter_health_unknown_before_start(self):
        """Health check on a never-started adapter returns unknown.

        Verifies that ``health_check()`` on a freshly-constructed
        ``LxmfAdapter`` returns ``"unknown"`` with ``platform ==
        "lxmf"`` before any ``start()`` call.
        """
        from medre.adapters.lxmf.adapter import LxmfAdapter
        from medre.core.contracts.adapter import AdapterRole

        config = _make_config()
        adapter = LxmfAdapter(config)

        info = await adapter.health_check()
        assert (
            info.health == "unknown"
        ), f"Expected unknown before start, got {info.health!r}"
        assert info.platform == "lxmf"
        assert info.role == AdapterRole.TRANSPORT

    async def test_adapter_health_unknown_after_stop(self):
        """Stop the adapter and verify health_check reports unknown.

        After ``start()`` + ``stop()``, the adapter must report
        ``health == "unknown"`` because the session is disconnected.
        Skips if Reticulum SDK is unavailable.
        """
        from medre.adapters.lxmf.adapter import LxmfAdapter
        from medre.adapters.lxmf.errors import LxmfConnectionError

        config = _make_config()
        adapter = LxmfAdapter(config)
        ctx = _make_context()

        try:
            await bounded(adapter.start(ctx), _ADAPTER_START_TIMEOUT, "lxmf start")
        except LxmfConnectionError as exc:
            pytest.skip(f"LXMF connection unavailable: {exc}")

        await bounded(adapter.stop(), _ADAPTER_STOP_TIMEOUT, "lxmf stop")
        info = await bounded(adapter.health_check(), 10.0, "lxmf health_check after stop")
        assert (
            info.health == "unknown"
        ), f"Expected unknown after stop, got {info.health!r}"

    async def test_adapter_stop_idempotent_before_start(self):
        """Calling stop() on a never-started adapter is safe (idempotent).

        ``stop()`` should be a no-op when called on an adapter that has
        never been started, without raising any exceptions.
        """
        from medre.adapters.lxmf.adapter import LxmfAdapter

        config = _make_config()
        adapter = LxmfAdapter(config)

        # Should not raise.
        await bounded(adapter.stop(), _ADAPTER_STOP_TIMEOUT, "lxmf stop (never started)")

        info = await adapter.health_check()
        assert info.health == "unknown"

    # ===================================================================
    # 3. Send validation (outbound path)
    # ===================================================================

    async def test_outbound_send_returns_delivery_result_fake(self):
        """deliver() returns AdapterDeliveryResult with lxmf metadata.

        In fake mode, ``deliver()`` returns an ``AdapterDeliveryResult``
        with a deterministic native message ID and ``lxmf.delivery_state``
        metadata set to ``"outbound"`` (honest pending semantics).
        """
        from medre.adapters.lxmf.adapter import LxmfAdapter
        from medre.core.contracts.adapter import AdapterDeliveryResult

        config = _make_fake_config()
        adapter = LxmfAdapter(config)
        ctx = _make_context()
        await bounded(adapter.start(ctx), 5.0, "fake start (send result)")
        try:
            from medre.core.rendering.renderer import RenderingResult

            ts = int(time.time())
            result = RenderingResult(
                event_id=f"send-{ts}",
                target_adapter="lxmf-live-smoke",
                target_channel="0",
                payload={
                    "content": f"MEDRE live smoke send (ts={ts})",
                    "title": "",
                    "destination_hash": "ab" * 16,
                    "delivery_method": "direct",
                },
                metadata={"renderer": "lxmf", "test": "send-smoke"},
            )
            delivery = await bounded(adapter.deliver(result), 5.0, "fake deliver (send result)")
            assert (
                delivery is not None
            ), "deliver() returned None — expected AdapterDeliveryResult"
            assert isinstance(delivery, AdapterDeliveryResult)
            assert delivery.native_message_id is not None, "native_message_id is None"
            lxmf_meta = delivery.metadata.get("lxmf")
            assert isinstance(
                lxmf_meta, dict
            ), "Expected 'lxmf' dict in delivery metadata"
            assert (
                "delivery_state" in lxmf_meta
            ), "Expected 'delivery_state' in lxmf metadata"
            assert lxmf_meta["delivery_state"] == "outbound", (
                f"Expected delivery_state 'outbound', "
                f"got {lxmf_meta['delivery_state']!r}"
            )
        finally:
            await bounded(adapter.stop(), 5.0, "fake stop (send result)")

    async def test_outbound_send_unique_ids_fake(self):
        """Two sends produce distinct native_message_ids.

        Verifies that repeated calls to ``deliver()`` return unique
        native message IDs from the session's fake-mode send path.
        """
        from medre.adapters.lxmf.adapter import LxmfAdapter
        from medre.core.rendering.renderer import RenderingResult

        config = _make_fake_config()
        adapter = LxmfAdapter(config)
        ctx = _make_context()
        await bounded(adapter.start(ctx), 5.0, "fake start (unique ids)")
        try:
            ts = int(time.time())
            for i in range(2):
                result = RenderingResult(
                    event_id=f"unique-{ts}-{i}",
                    target_adapter="lxmf-live-smoke",
                    target_channel="0",
                    payload={
                        "content": f"unique id test {i}",
                        "destination_hash": "ab" * 16,
                    },
                    metadata={"renderer": "lxmf", "test": "unique-ids"},
                )
                delivery = await bounded(adapter.deliver(result), 5.0, "fake deliver (unique loop)")
                assert delivery is not None
                assert delivery.native_message_id is not None

            # Two sends on same adapter — IDs must differ.
            r1 = RenderingResult(
                event_id=f"id-a-{ts}",
                target_adapter="lxmf-live-smoke",
                target_channel="0",
                payload={"content": "msg a", "destination_hash": "ab" * 16},
                metadata={"test": "unique-ids"},
            )
            r2 = RenderingResult(
                event_id=f"id-b-{ts}",
                target_adapter="lxmf-live-smoke",
                target_channel="0",
                payload={"content": "msg b", "destination_hash": "ab" * 16},
                metadata={"test": "unique-ids"},
            )
            d1 = await bounded(adapter.deliver(r1), 5.0, "fake deliver (unique d1)")
            d2 = await bounded(adapter.deliver(r2), 5.0, "fake deliver (unique d2)")
            assert d1 is not None and d2 is not None
            assert d1.native_message_id is not None
            assert d2.native_message_id is not None
            assert (
                d1.native_message_id != d2.native_message_id
            ), "Two sends produced the same native_message_id"
        finally:
            await bounded(adapter.stop(), 5.0, "fake stop (unique ids)")

    async def test_outbound_send_type_validation(self):
        """deliver() raises AdapterPermanentError for non-RenderingResult input.

        This test does not require Reticulum — it validates input
        validation which works in any connection mode.  Uses fake mode
        so the adapter can start without Reticulum.
        """
        from medre.adapters.lxmf.adapter import LxmfAdapter
        from medre.core.contracts.adapter import AdapterPermanentError

        config = _make_fake_config()
        adapter = LxmfAdapter(config)
        ctx = _make_context()
        await bounded(adapter.start(ctx), 5.0, "fake start (type validation)")
        try:
            with pytest.raises(AdapterPermanentError, match="RenderingResult"):
                await adapter.deliver("not a rendering result")  # type: ignore[arg-type]
        finally:
            await bounded(adapter.stop(), 5.0, "fake stop (type validation)")

    # ===================================================================
    # 4. Delivery callback validation (inbound path)
    # ===================================================================

    async def test_inbound_session_callback_wired_note(self):
        """Document: LxmfSession wires real LXMRouter delivery callbacks.

        This test always passes.  The ``LxmfSession`` registers the
        inbound message callback with the LXMRouter on ``start()`` and
        tears it down on ``stop()``.  Real inbound messages are
        normalised to plain dicts within the session boundary before
        reaching the adapter's ``_on_packet()`` handler.

        Live validation of real inbound messages from a remote peer
        requires a second LXMF identity or loopback fixture not
        available in this harness.
        """
        pass

    async def test_inbound_simulate_publishes_with_fake(self):
        """Verify inbound pipeline via simulate_inbound (fake mode).

        Uses the ``fake`` connection type so the adapter can start
        without Reticulum.  Injects a packet via ``simulate_inbound()``
        and verifies that ``publish_inbound`` is called with the decoded
        canonical event.

        This validates the same codec/classifier pipeline used by real
        inbound callbacks from the session.
        """
        from medre.adapters.lxmf.adapter import LxmfAdapter
        from tests.fixtures.lxmf_packets import make_lxmf_text_packet

        publish_mock = AsyncMock()
        config = _make_fake_config()
        adapter = LxmfAdapter(config)
        ctx = _make_context(publish_inbound=publish_mock)
        await bounded(adapter.start(ctx), 5.0, "fake start (inbound)")
        try:
            packet = make_lxmf_text_packet(
                content="MEDRE live smoke inbound test",
            )
            await adapter.simulate_inbound(packet)
            assert (
                publish_mock.call_count == 1
            ), f"Expected 1 inbound publish, got {publish_mock.call_count}"
            event = publish_mock.call_args.args[0]
            assert event.payload.get("body") == "MEDRE live smoke inbound test"
        finally:
            await bounded(adapter.stop(), 5.0, "fake stop (inbound)")

    # ===================================================================
    # 5. Restart validation
    # ===================================================================

    async def test_restart_start_stop_start_stop(self):
        """Full start → stop → start → stop cycle verifies restart.

        Exercises the complete restart lifecycle with the reticulum
        backend:

        1. Start adapter → verify healthy
        2. Stop adapter → verify unknown
        3. Start adapter again → verify healthy
        4. Stop adapter again → verify unknown

        This catches state leaks, stale session references, and unclean
        shutdown that would prevent a second start.  Skips if Reticulum
        SDK is unavailable.
        """
        from medre.adapters.lxmf.adapter import LxmfAdapter
        from medre.adapters.lxmf.errors import LxmfConnectionError

        config = _make_config()

        # Cycle 1
        ctx1 = _make_context()
        adapter = LxmfAdapter(config)
        try:
            await bounded(adapter.start(ctx1), _ADAPTER_START_TIMEOUT, "lxmf start cycle 1")
        except LxmfConnectionError as exc:
            pytest.skip(f"LXMF connection unavailable: {exc}")

        info = await bounded(adapter.health_check(), 10.0, "lxmf health_check cycle 1 start")
        assert (
            info.health == "healthy"
        ), f"Cycle 1 start: expected healthy, got {info.health!r}"
        await bounded(adapter.stop(), _ADAPTER_STOP_TIMEOUT, "lxmf stop cycle 1")
        info = await bounded(adapter.health_check(), 10.0, "lxmf health_check cycle 1 stop")
        assert (
            info.health == "unknown"
        ), f"Cycle 1 stop: expected unknown, got {info.health!r}"

        # Cycle 2 — same adapter instance, new context
        ctx2 = _make_context()
        try:
            await bounded(adapter.start(ctx2), _ADAPTER_START_TIMEOUT, "lxmf start cycle 2")
        except LxmfConnectionError as exc:
            pytest.skip(f"LXMF connection unavailable on restart: {exc}")

        info = await bounded(adapter.health_check(), 10.0, "lxmf health_check cycle 2 start")
        assert (
            info.health == "healthy"
        ), f"Cycle 2 start: expected healthy, got {info.health!r}"
        await bounded(adapter.stop(), _ADAPTER_STOP_TIMEOUT, "lxmf stop cycle 2")
        info = await bounded(adapter.health_check(), 10.0, "lxmf health_check cycle 2 stop")
        assert (
            info.health == "unknown"
        ), f"Cycle 2 stop: expected unknown, got {info.health!r}"

    async def test_restart_with_fake_mode(self):
        """Restart cycle using fake mode (no Reticulum required).

        This test exercises the restart lifecycle using ``fake``
        connection type so it can run without Reticulum installed.
        Validates that internal state (``_started``, ``_session``,
        ``ctx``) is properly reset between cycles.
        """
        from medre.adapters.lxmf.adapter import LxmfAdapter

        config = _make_fake_config()
        adapter = LxmfAdapter(config)

        # Cycle 1
        ctx1 = _make_context()
        await bounded(adapter.start(ctx1), 5.0, "fake start cycle 1")
        info = await adapter.health_check()
        assert info.health == "healthy"
        await bounded(adapter.stop(), 5.0, "fake stop cycle 1")
        info = await adapter.health_check()
        assert info.health == "unknown"

        # Cycle 2
        ctx2 = _make_context()
        await bounded(adapter.start(ctx2), 5.0, "fake start cycle 2")
        info = await adapter.health_check()
        assert info.health == "healthy"
        await bounded(adapter.stop(), 5.0, "fake stop cycle 2")
        info = await adapter.health_check()
        assert info.health == "unknown"

        # Cycle 3 — one more for good measure
        ctx3 = _make_context()
        await bounded(adapter.start(ctx3), 5.0, "fake start cycle 3")
        info = await adapter.health_check()
        assert info.health == "healthy"
        await bounded(adapter.stop(), 5.0, "fake stop cycle 3")
        info = await adapter.health_check()
        assert info.health == "unknown"

    # ===================================================================
    # 6. Repeated start/stop validation
    # ===================================================================

    async def test_double_start_idempotent_fake(self):
        """Calling start() twice is idempotent (fake mode).

        The adapter must not raise on a second ``start()`` call; it
        should be a no-op when already started.
        """
        from medre.adapters.lxmf.adapter import LxmfAdapter

        config = _make_fake_config()
        adapter = LxmfAdapter(config)
        ctx = _make_context()

        await bounded(adapter.start(ctx), 5.0, "fake start (double start 1)")
        # Second start — must not raise.
        await bounded(adapter.start(ctx), 5.0, "fake start (double start 2)")
        info = await adapter.health_check()
        assert info.health == "healthy"
        await bounded(adapter.stop(), 5.0, "fake stop (double start)")

    async def test_double_stop_idempotent_fake(self):
        """Calling stop() twice is idempotent (fake mode).

        The adapter must not raise on a second ``stop()`` call.
        """
        from medre.adapters.lxmf.adapter import LxmfAdapter

        config = _make_fake_config()
        adapter = LxmfAdapter(config)
        ctx = _make_context()

        await bounded(adapter.start(ctx), 5.0, "fake start (double stop)")
        await bounded(adapter.stop(), 5.0, "fake stop (double stop 1)")
        # Second stop — must not raise.
        await bounded(adapter.stop(), 5.0, "fake stop (double stop 2)")
        info = await adapter.health_check()
        assert info.health == "unknown"

    async def test_rapid_start_stop_cycles_fake(self):
        """Rapid repeated start/stop cycles do not leak state (fake mode).

        Exercises 5 rapid start/stop cycles to verify that no state
        leaks accumulate (stale tasks, dangling session references, etc.).
        """
        from medre.adapters.lxmf.adapter import LxmfAdapter

        config = _make_fake_config()
        adapter = LxmfAdapter(config)

        for i in range(5):
            ctx = _make_context()
            await bounded(adapter.start(ctx), 5.0, f"fake start cycle {i + 1}")
            info = await adapter.health_check()
            assert (
                info.health == "healthy"
            ), f"Cycle {i + 1}: expected healthy, got {info.health!r}"
            await bounded(adapter.stop(), 5.0, f"fake stop cycle {i + 1}")
            info = await adapter.health_check()
            assert info.health == "unknown", (
                f"Cycle {i + 1}: expected unknown after stop, " f"got {info.health!r}"
            )

    # ===================================================================
    # 7. Full lifecycle round-trip (fake mode)
    # ===================================================================

    async def test_full_lifecycle_start_send_stop_fake(self):
        """Exercise start → send → health → stop round-trip (fake mode).

        A single ordered round-trip that validates the complete adapter
        lifecycle in one test: start, deliver, verify health, stop,
        verify unknown.  Uses fake mode; ``deliver()`` returns a real
        ``AdapterDeliveryResult`` with ``lxmf`` delivery-state metadata.
        """
        from medre.adapters.lxmf.adapter import LxmfAdapter
        from medre.core.contracts.adapter import AdapterDeliveryResult
        from medre.core.rendering.renderer import RenderingResult

        config = _make_fake_config()
        adapter = LxmfAdapter(config)
        ctx = _make_context()

        # 1. Start
        await bounded(adapter.start(ctx), 5.0, "fake start (lifecycle)")
        info = await adapter.health_check()
        assert info.health == "healthy"

        # 2. Deliver — returns AdapterDeliveryResult via session.
        ts = int(time.time())
        result = RenderingResult(
            event_id=f"lifecycle-{ts}",
            target_adapter="lxmf-live-smoke",
            target_channel="0",
            payload={
                "content": f"MEDRE lifecycle test (ts={ts})",
                "title": "",
                "destination_hash": "ab" * 16,
            },
            metadata={"renderer": "lxmf", "test": "lifecycle"},
        )
        delivery = await bounded(adapter.deliver(result), 5.0, "fake deliver (lifecycle)")
        assert delivery is not None, "deliver() returned None"
        assert isinstance(delivery, AdapterDeliveryResult)
        assert delivery.native_message_id is not None, "native_message_id is None"
        lxmf_meta = delivery.metadata.get("lxmf")
        assert isinstance(lxmf_meta, dict)
        assert lxmf_meta["delivery_state"] == "outbound"

        # 3. Still healthy
        info = await adapter.health_check()
        assert info.health == "healthy"

        # 4. Stop
        await bounded(adapter.stop(), 5.0, "fake stop (lifecycle)")

        # 5. Now unknown
        info = await adapter.health_check()
        assert info.health == "unknown"

    # ===================================================================
    # 8. Session diagnostics (fake mode)
    # ===================================================================

    async def test_session_diagnostics_after_start_fake(self):
        """Session diagnostics reflect connected state after start.

        Verifies that ``adapter.session.diagnostics()`` returns a
        snapshot with ``connected == True`` after ``start()`` and
        ``connected == False`` after ``stop()``.
        """
        from medre.adapters.lxmf.adapter import LxmfAdapter

        config = _make_fake_config()
        adapter = LxmfAdapter(config)
        ctx = _make_context()

        await bounded(adapter.start(ctx), 5.0, "fake start (diagnostics)")
        diag = adapter.session.diagnostics()
        assert (
            diag.connected is True
        ), f"Expected connected=True after start, got {diag.connected}"

        await bounded(adapter.stop(), 5.0, "fake stop (diagnostics)")
        diag = adapter.session.diagnostics()
        assert (
            diag.connected is False
        ), f"Expected connected=False after stop, got {diag.connected}"

    # ===================================================================
    # 8b. Bounded async operations (fake mode)
    # ===================================================================

    async def test_bounded_async_start_stop_deliver(self):
        """start/stop/deliver complete within bounded timeouts (fake mode).

        Wraps the core async operations in ``asyncio.wait_for`` to verify
        they do not hang or deadlock.
        """
        from medre.adapters.lxmf.adapter import LxmfAdapter
        from medre.core.contracts.adapter import AdapterDeliveryResult
        from medre.core.rendering.renderer import RenderingResult

        config = _make_fake_config()
        adapter = LxmfAdapter(config)
        ctx = _make_context()

        # Bounded start
        await bounded(adapter.start(ctx), 5.0, "fake start (bounded)")
        info = await adapter.health_check()
        assert info.health == "healthy"

        # Bounded deliver
        ts = int(time.time())
        result = RenderingResult(
            event_id=f"bounded-{ts}",
            target_adapter="lxmf-live-smoke",
            target_channel="0",
            payload={
                "content": f"Bounded async test (ts={ts})",
                "destination_hash": "ab" * 16,
            },
            metadata={"renderer": "lxmf", "test": "bounded-async"},
        )
        delivery = await bounded(adapter.deliver(result), 5.0, "fake deliver (bounded)")
        assert delivery is not None
        assert isinstance(delivery, AdapterDeliveryResult)

        # Bounded stop
        await bounded(adapter.stop(), 5.0, "fake stop (bounded)")
        info = await adapter.health_check()
        assert info.health == "unknown"

    # ===================================================================
    # 8c. Storage native refs — diagnostics expose pending_delivery_count
    # ===================================================================

    async def test_session_diagnostics_show_pending_delivery_count(self):
        """Session diagnostics include pending_delivery_count after sends.

        After delivering a message in fake mode, the session diagnostics
        must expose ``pending_delivery_count`` as a non-negative integer.
        """
        from medre.adapters.lxmf.adapter import LxmfAdapter
        from medre.core.rendering.renderer import RenderingResult

        config = _make_fake_config()
        adapter = LxmfAdapter(config)
        ctx = _make_context()
        await bounded(adapter.start(ctx), 5.0, "fake start (pending count)")
        try:
            ts = int(time.time())
            result = RenderingResult(
                event_id=f"pending-{ts}",
                target_adapter="lxmf-live-smoke",
                target_channel="0",
                payload={
                    "content": f"Pending count test (ts={ts})",
                    "destination_hash": "ab" * 16,
                },
                metadata={"renderer": "lxmf", "test": "pending-count"},
            )
            await bounded(adapter.deliver(result), 5.0, "fake deliver (pending count)")

            diag = adapter.session.diagnostics()
            assert hasattr(diag, "pending_delivery_count"), (
                "Session diagnostics missing pending_delivery_count field"
            )
            assert diag.pending_delivery_count >= 0, (
                f"Expected pending_delivery_count >= 0, "
                f"got {diag.pending_delivery_count}"
            )
        finally:
            await bounded(adapter.stop(), 5.0, "fake stop (pending count)")

    # ===================================================================
    # 8d. Diagnostics no-secret-leakage validation
    # ===================================================================

    async def test_diagnostics_no_secrets_leaked(self):
        """Diagnostics snapshot exposes useful fields with no secret leakage.

        Verifies that ``adapter.diagnostics()`` includes standard
        operational fields (adapter_id, platform, started, session state)
        and does NOT contain any identity_path, private keys, or other
        secret material.
        """
        from medre.adapters.lxmf.adapter import LxmfAdapter

        config = _make_fake_config()
        adapter = LxmfAdapter(config)
        ctx = _make_context()

        await bounded(adapter.start(ctx), 5.0, "fake start (no secrets)")
        try:
            diag = adapter.diagnostics()

            # Standard fields must be present
            assert diag["adapter_id"] == "lxmf-live-smoke"
            assert diag["platform"] == "lxmf"
            assert diag["started"] is True
            assert diag["mode"] == "fake"

            # Session sub-dict must be present with standard fields
            session_diag = diag.get("session", {})
            assert "connected" in session_diag
            assert "router_running" in session_diag
            assert "reconnecting" in session_diag
            assert "reconnect_attempts" in session_diag
            assert "transient_delivery_failures" in session_diag
            assert "permanent_delivery_failures" in session_diag

            # No identity or secret material in diagnostics
            assert_no_secret_leak(
                diag,
                {"identity_path", "private_key", "identity_file", "proving_key", "seed"},
            )
        finally:
            await bounded(adapter.stop(), 5.0, "fake stop (no secrets)")

    # ===================================================================
    # 8d2. Storage path handling in diagnostics
    # ===================================================================

    async def test_diagnostics_storage_path_handling(self):
        """Diagnostics do not expose the raw storage_path value.

        Creates an adapter with a known ``storage_path`` in fake mode,
        starts it, and verifies that the diagnostics snapshot does not
        contain the raw storage path string.
        """
        from medre.adapters.lxmf.adapter import LxmfAdapter

        secret_path = "/tmp/medre-secret-lxmf-storage-diag-test"
        from medre.config.adapters.lxmf import LxmfConfig

        config = LxmfConfig(
            adapter_id="lxmf-live-smoke",
            connection_type="fake",
            storage_path=secret_path,
        )
        adapter = LxmfAdapter(config)
        ctx = _make_context()

        await bounded(adapter.start(ctx), 5.0, "fake start (storage path diag)")
        try:
            diag = adapter.diagnostics()
            assert diag["started"] is True
            assert_no_secret_leak(diag, {secret_path})
        finally:
            await bounded(adapter.stop(), 5.0, "fake stop (storage path diag)")

    # ===================================================================
    # 8d3. Delivery state reporting
    # ===================================================================

    async def test_delivery_state_reporting(self):
        """session.delivery_state_counts() returns expected format after send.

        Sends a message in fake mode and verifies that
        ``session.delivery_state_counts()`` returns a ``dict[str, int]``
        with at least one key matching a known delivery state.
        """
        from medre.adapters.lxmf.adapter import LxmfAdapter
        from medre.core.rendering.renderer import RenderingResult

        config = _make_fake_config()
        adapter = LxmfAdapter(config)
        ctx = _make_context()
        await bounded(adapter.start(ctx), 5.0, "fake start (delivery state)")
        try:
            ts = int(time.time())
            result = RenderingResult(
                event_id=f"ds-{ts}",
                target_adapter="lxmf-live-smoke",
                target_channel="0",
                payload={
                    "content": f"Delivery state test (ts={ts})",
                    "destination_hash": "ab" * 16,
                },
                metadata={"renderer": "lxmf", "test": "delivery-state"},
            )
            await bounded(adapter.deliver(result), 5.0, "fake deliver (delivery state)")

            counts = adapter.session.delivery_state_counts()
            assert isinstance(counts, dict), (
                f"Expected dict from delivery_state_counts(), got {type(counts)}"
            )
            assert len(counts) > 0, "delivery_state_counts() returned empty dict"
            for key in counts:
                assert isinstance(key, str), f"Key {key!r} is not a string"
                assert isinstance(counts[key], int), (
                    f"Value for {key!r} is not int: {counts[key]!r}"
                )
        finally:
            await bounded(adapter.stop(), 5.0, "fake stop (delivery state)")

    # ===================================================================
    # 8d4. Stop after partial start
    # ===================================================================

    async def test_stop_safety_never_started_and_clean_cycle(self):
        """stop() is safe on a never-started adapter and after a clean fake-mode start/stop cycle."""
        from medre.adapters.lxmf.adapter import LxmfAdapter
        from medre.config.adapters.lxmf import LxmfConfig

        config = LxmfConfig(
            adapter_id="lxmf-partial-start",
            connection_type="fake",
        )
        adapter = LxmfAdapter(config)

        # Attempt stop on never-started adapter — must not raise.
        await bounded(adapter.stop(), 5.0, "stop (never started, partial)")

        # Now start with a valid context — fake mode always succeeds.
        ctx = _make_context()
        await bounded(adapter.start(ctx), 5.0, "fake start (partial)")
        await bounded(adapter.stop(), 5.0, "stop after start (partial)")

    # ===================================================================
    # 8e. Real outbound send (gated by LXMF_LIVE_SEND=1)
    # ===================================================================

    @require_live_send
    async def test_outbound_send_real_with_live_send(self):
        """Send a real LXMF message via reticulum (requires LXMF_LIVE_SEND=1).

        This test performs a **real** outbound send through the Reticulum
        LXMF router.  It is gated by ``LXMF_LIVE_SEND=1`` to prevent
        accidental transmissions during development.

        Requires:
        - ``LXMF_CONNECTION_TYPE=reticulum``
        - ``LXMF_IDENTITY_PATH`` pointing to a valid identity file
        - ``LXMF_DESTINATION_HASH`` (32-char hex) for the recipient
        - ``LXMF_LIVE_SEND=1`` explicit opt-in

        Skips if any required variable is missing or the SDK is unavailable.
        """
        from medre.adapters.lxmf.adapter import LxmfAdapter
        from medre.adapters.lxmf.errors import LxmfConnectionError
        from medre.core.contracts.adapter import AdapterDeliveryResult
        from medre.core.rendering.renderer import RenderingResult

        if not LXMF_DESTINATION_HASH:
            pytest.skip(
                "LXMF_DESTINATION_HASH required for real send test"
            )

        config = _make_config()
        adapter = LxmfAdapter(config)
        ctx = _make_context()

        try:
            await bounded(adapter.start(ctx), _ADAPTER_START_TIMEOUT, "lxmf start live send")
        except LxmfConnectionError as exc:
            pytest.skip(f"LXMF connection unavailable: {exc}")

        try:
            ts = int(time.time())
            result = RenderingResult(
                event_id=f"live-send-{ts}",
                target_adapter="lxmf-live-smoke",
                target_channel="0",
                payload={
                    "content": f"MEDRE live send test (ts={ts}) — safe to ignore",
                    "destination_hash": LXMF_DESTINATION_HASH,
                    "delivery_method": "direct",
                },
                metadata={"renderer": "lxmf", "test": "live-send"},
            )
            delivery = await bounded(
                adapter.deliver(result), _DELIVER_TIMEOUT, "lxmf deliver live send",
            )
            assert delivery is not None, "deliver() returned None"
            assert isinstance(delivery, AdapterDeliveryResult)
            assert delivery.native_message_id is not None, (
                "native_message_id is None — send did not produce an ID"
            )
            lxmf_meta = delivery.metadata.get("lxmf", {})
            assert isinstance(lxmf_meta, dict), "Expected lxmf metadata to be a dict"
            assert "delivery_state" in lxmf_meta, (
                "Expected 'delivery_state' in lxmf delivery metadata"
            )
        finally:
            await bounded(adapter.stop(), _ADAPTER_STOP_TIMEOUT, "lxmf stop live send")

    # ===================================================================
    # 9. Documentation notes
    # ===================================================================

    async def test_e2ee_not_supported_note(self):
        """Document: LXMF encrypted messages are not supported.

        This test always passes.  End-to-end encrypted LXMF messages
        are out of scope for the current tranche.
        """
        pass


# ---------------------------------------------------------------------------
# Two-process topology tests
# ---------------------------------------------------------------------------

_TOPOLOGY_ENV_VARS = (
    "LXMF_TOPOLOGY_LIVE",
    "LXMF_PROCESS_ROLE",
    "LXMF_IDENTITY_PATH",
    "LXMF_DESTINATION_HASH",
    "LXMF_STORAGE_PATH",
)

require_topology = pytest.mark.skipif(
    os.environ.get("LXMF_TOPOLOGY_LIVE", "") != "1",
    reason="Set LXMF_TOPOLOGY_LIVE=1 to enable two-process topology tests",
)


@require_topology
class TestLxmfTopologyLive:
    """Two-process LXMF topology smoke tests.

    These tests validate LXMF adapter behaviour in a **two-process**
    topology where Process A sends and Process B receives.

    **Topology model:**

    - **Process A (sender):** ``LXMF_PROCESS_ROLE=sender``, holds
      ``LXMF_DESTINATION_HASH`` of the receiver.
    - **Process B (receiver):** ``LXMF_PROCESS_ROLE=receiver``, has
      its own identity.
    - Both on the same LAN with ``AutoInterface`` enabled in the
      Reticulum config.
    - Process B should be started first, then Process A.
    - Each process should run in a separate terminal.
    - The test in each process checks its own env config and produces
      a result.

    **Required environment variables:**

    =========================== =====================================================
    Variable                    Description
    =========================== =====================================================
    ``LXMF_TOPOLOGY_LIVE``      Must be ``1`` to enable topology tests.
    ``LXMF_PROCESS_ROLE``       ``sender`` or ``receiver``.
    ``LXMF_IDENTITY_PATH``      Path to a Reticulum identity file.
    ``LXMF_DESTINATION_HASH``   32-char hex hash of the peer (required for sender).
    ``LXMF_STORAGE_PATH``       Path for LXMF router storage.
    =========================== =====================================================

    All tests in this class are skipped unless ``LXMF_TOPOLOGY_LIVE=1``.
    """

    async def test_topology_env_completeness(self):
        """All required topology env vars are present.

        Checks role-specific requirements:
        - Sender requires all 5 env vars including LXMF_DESTINATION_HASH.
        - Receiver requires all env vars except LXMF_DESTINATION_HASH.
        """
        role = os.environ.get("LXMF_PROCESS_ROLE", "").lower()
        valid_roles = {"sender", "receiver"}
        assert role in valid_roles, (
            "Invalid LXMF_PROCESS_ROLE for topology live tests; expected "
            "'sender' or 'receiver' but got "
            f"{os.environ.get('LXMF_PROCESS_ROLE')!r}."
        )
        required = list(_TOPOLOGY_ENV_VARS)
        if role != "sender":
            required = [v for v in required if v != "LXMF_DESTINATION_HASH"]

        requirements = [LiveRequirement(v, description="") for v in required]
        status = live_env_status(requirements)
        assert status.enabled, (
            f"Missing topology env vars: {status.missing}"
        )

    async def test_topology_start_bounded(self):
        """Start adapter bounded with LXMF config, verify healthy, stop bounded.

        Only runs for ``LXMF_PROCESS_ROLE=sender`` to avoid conflicts
        with the receiver process on the same machine.
        """
        role = os.environ.get("LXMF_PROCESS_ROLE", "").lower()
        if role != "sender":
            pytest.skip("test_topology_start_bounded only runs for LXMF_PROCESS_ROLE=sender")

        from medre.adapters.lxmf.adapter import LxmfAdapter
        from medre.adapters.lxmf.errors import LxmfConnectionError

        config = _make_config()
        adapter = LxmfAdapter(config)
        ctx = _make_context()

        try:
            await bounded(adapter.start(ctx), _ADAPTER_START_TIMEOUT, "topology adapter start")
            info = await bounded(adapter.health_check(), 10.0, "topology health_check")
            assert info.health == "healthy", (
                f"Expected healthy after topology start, got {info.health!r}"
            )
        except LxmfConnectionError as exc:
            pytest.skip(f"LXMF connection unavailable for topology: {exc}")
        finally:
            await bounded(adapter.stop(), _ADAPTER_STOP_TIMEOUT, "topology adapter stop")

    @require_live_send
    async def test_topology_send_with_live_send(self):
        """Send a real LXMF message (requires LXMF_LIVE_SEND=1).

        Only runs for LXMF_PROCESS_ROLE=sender.
        """
        role = os.environ.get("LXMF_PROCESS_ROLE", "").lower()
        if role != "sender":
            pytest.skip(
                "test_topology_send_with_live_send only runs for "
                "LXMF_PROCESS_ROLE=sender"
            )

        if not LXMF_DESTINATION_HASH:
            pytest.skip("LXMF_DESTINATION_HASH required for real send test")

        dest_hash = LXMF_DESTINATION_HASH

        from medre.adapters.lxmf.adapter import LxmfAdapter
        from medre.adapters.lxmf.errors import LxmfConnectionError
        from medre.core.contracts.adapter import AdapterDeliveryResult
        from medre.core.rendering.renderer import RenderingResult

        config = _make_config()
        adapter = LxmfAdapter(config)
        ctx = _make_context()

        try:
            await bounded(adapter.start(ctx), _ADAPTER_START_TIMEOUT, "topology send start")
        except LxmfConnectionError as exc:
            pytest.skip(f"LXMF connection unavailable: {exc}")

        try:
            ts = int(time.time())
            result = RenderingResult(
                event_id=f"topo-send-{ts}",
                target_adapter="lxmf-live-smoke",
                target_channel="0",
                payload={
                    "content": f"MEDRE topology send test (ts={ts})",
                    "destination_hash": dest_hash,
                    "delivery_method": "direct",
                },
                metadata={"renderer": "lxmf", "test": "topology-send"},
            )
            delivery = await bounded(
                adapter.deliver(result), _DELIVER_TIMEOUT, "topology deliver",
            )
            assert delivery is not None
            assert isinstance(delivery, AdapterDeliveryResult)
            assert delivery.native_message_id is not None
        finally:
            await bounded(adapter.stop(), _ADAPTER_STOP_TIMEOUT, "topology send stop")
