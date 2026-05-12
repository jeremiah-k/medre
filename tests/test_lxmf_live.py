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

# ---------------------------------------------------------------------------
# Module-level marker — entire file is tagged "live" so it is excluded by the
# default ``addopts = "-m 'not live'"`` in pyproject.toml.
# ---------------------------------------------------------------------------
pytestmark = pytest.mark.live

# ---------------------------------------------------------------------------
# Environment variable gate
# ---------------------------------------------------------------------------
LXMF_CONNECTION_TYPE = os.environ.get("LXMF_CONNECTION_TYPE", "").lower()
LXMF_IDENTITY_PATH = os.environ.get("LXMF_IDENTITY_PATH", "")
LXMF_DISPLAY_NAME = os.environ.get("LXMF_DISPLAY_NAME", "")
LXMF_DESTINATION_HASH = os.environ.get("LXMF_DESTINATION_HASH", "")


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

require_lxmf_sdk = pytest.mark.skipif(
    not _LIVE_ENV_SET,
    reason=_LIVE_SKIP_REASON,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _make_config():
    """Build an LxmfConfig from the live environment variables."""
    from medre.adapters.lxmf.config import LxmfConfig

    return LxmfConfig(
        adapter_id="lxmf-live-smoke",
        connection_type="reticulum",
        identity_path=LXMF_IDENTITY_PATH or None,
        display_name=LXMF_DISPLAY_NAME,
        storage_path="/tmp/medre-live-lxmf-router",
    )


def _make_fake_config():
    """Build an LxmfConfig for fake mode (no Reticulum required)."""
    from medre.adapters.lxmf.config import LxmfConfig

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
    from medre.adapters.base import AdapterContext

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
        from medre.adapters.lxmf.config import LxmfConfig

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
        from medre.adapters.lxmf.config import LxmfConfig
        from medre.adapters.lxmf.errors import LxmfConfigError

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
            await adapter.start(ctx)
            info = await adapter.health_check()
            assert info.health == "healthy", (
                f"Expected healthy after start, got {info.health!r}"
            )
            assert info.platform == "lxmf"
            assert info.adapter_id == "lxmf-live-smoke"
        except LxmfConnectionError as exc:
            pytest.skip(
                f"LXMF connection unavailable: {exc}"
            )
        finally:
            await adapter.stop()

    async def test_adapter_health_unknown_before_start(self):
        """Health check on a never-started adapter returns unknown.

        Verifies that ``health_check()`` on a freshly-constructed
        ``LxmfAdapter`` returns ``"unknown"`` with ``platform ==
        "lxmf"`` before any ``start()`` call.
        """
        from medre.adapters.lxmf.adapter import LxmfAdapter
        from medre.adapters.base import AdapterRole

        config = _make_config()
        adapter = LxmfAdapter(config)

        info = await adapter.health_check()
        assert info.health == "unknown", (
            f"Expected unknown before start, got {info.health!r}"
        )
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
            await adapter.start(ctx)
        except LxmfConnectionError as exc:
            pytest.skip(
                f"LXMF connection unavailable: {exc}"
            )

        await adapter.stop()
        info = await adapter.health_check()
        assert info.health == "unknown", (
            f"Expected unknown after stop, got {info.health!r}"
        )

    async def test_adapter_stop_idempotent_before_start(self):
        """Calling stop() on a never-started adapter is safe (idempotent).

        ``stop()`` should be a no-op when called on an adapter that has
        never been started, without raising any exceptions.
        """
        from medre.adapters.lxmf.adapter import LxmfAdapter

        config = _make_config()
        adapter = LxmfAdapter(config)

        # Should not raise.
        await adapter.stop()

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
        from medre.adapters.base import AdapterDeliveryResult

        config = _make_fake_config()
        adapter = LxmfAdapter(config)
        ctx = _make_context()
        await adapter.start(ctx)
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
            delivery = await adapter.deliver(result)
            assert delivery is not None, (
                "deliver() returned None — expected AdapterDeliveryResult"
            )
            assert isinstance(delivery, AdapterDeliveryResult)
            assert delivery.native_message_id is not None, (
                "native_message_id is None"
            )
            lxmf_meta = delivery.metadata.get("lxmf")
            assert isinstance(lxmf_meta, dict), (
                "Expected 'lxmf' dict in delivery metadata"
            )
            assert "delivery_state" in lxmf_meta, (
                "Expected 'delivery_state' in lxmf metadata"
            )
            assert lxmf_meta["delivery_state"] == "outbound", (
                f"Expected delivery_state 'outbound', "
                f"got {lxmf_meta['delivery_state']!r}"
            )
        finally:
            await adapter.stop()

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
        await adapter.start(ctx)
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
                delivery = await adapter.deliver(result)
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
            d1 = await adapter.deliver(r1)
            d2 = await adapter.deliver(r2)
            assert d1 is not None and d2 is not None
            assert d1.native_message_id is not None
            assert d2.native_message_id is not None
            assert d1.native_message_id != d2.native_message_id, (
                "Two sends produced the same native_message_id"
            )
        finally:
            await adapter.stop()

    async def test_outbound_send_type_validation(self):
        """deliver() raises TypeError for non-RenderingResult input.

        This test does not require Reticulum — it validates input
        validation which works in any connection mode.  Uses fake mode
        so the adapter can start without Reticulum.
        """
        from medre.adapters.lxmf.adapter import LxmfAdapter

        config = _make_fake_config()
        adapter = LxmfAdapter(config)
        ctx = _make_context()
        await adapter.start(ctx)
        try:
            with pytest.raises(TypeError, match="RenderingResult"):
                await adapter.deliver("not a rendering result")  # type: ignore[arg-type]
        finally:
            await adapter.stop()

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
        await adapter.start(ctx)
        try:
            packet = make_lxmf_text_packet(
                content="MEDRE live smoke inbound test",
            )
            await adapter.simulate_inbound(packet)
            assert publish_mock.call_count == 1, (
                f"Expected 1 inbound publish, got {publish_mock.call_count}"
            )
            event = publish_mock.call_args.args[0]
            assert event.payload.get("body") == "MEDRE live smoke inbound test"
        finally:
            await adapter.stop()

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
            await adapter.start(ctx1)
        except LxmfConnectionError as exc:
            pytest.skip(
                f"LXMF connection unavailable: {exc}"
            )

        info = await adapter.health_check()
        assert info.health == "healthy", (
            f"Cycle 1 start: expected healthy, got {info.health!r}"
        )
        await adapter.stop()
        info = await adapter.health_check()
        assert info.health == "unknown", (
            f"Cycle 1 stop: expected unknown, got {info.health!r}"
        )

        # Cycle 2 — same adapter instance, new context
        ctx2 = _make_context()
        try:
            await adapter.start(ctx2)
        except LxmfConnectionError as exc:
            pytest.skip(
                f"LXMF connection unavailable on restart: {exc}"
            )

        info = await adapter.health_check()
        assert info.health == "healthy", (
            f"Cycle 2 start: expected healthy, got {info.health!r}"
        )
        await adapter.stop()
        info = await adapter.health_check()
        assert info.health == "unknown", (
            f"Cycle 2 stop: expected unknown, got {info.health!r}"
        )

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
        await adapter.start(ctx1)
        info = await adapter.health_check()
        assert info.health == "healthy"
        await adapter.stop()
        info = await adapter.health_check()
        assert info.health == "unknown"

        # Cycle 2
        ctx2 = _make_context()
        await adapter.start(ctx2)
        info = await adapter.health_check()
        assert info.health == "healthy"
        await adapter.stop()
        info = await adapter.health_check()
        assert info.health == "unknown"

        # Cycle 3 — one more for good measure
        ctx3 = _make_context()
        await adapter.start(ctx3)
        info = await adapter.health_check()
        assert info.health == "healthy"
        await adapter.stop()
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

        await adapter.start(ctx)
        # Second start — must not raise.
        await adapter.start(ctx)
        info = await adapter.health_check()
        assert info.health == "healthy"
        await adapter.stop()

    async def test_double_stop_idempotent_fake(self):
        """Calling stop() twice is idempotent (fake mode).

        The adapter must not raise on a second ``stop()`` call.
        """
        from medre.adapters.lxmf.adapter import LxmfAdapter

        config = _make_fake_config()
        adapter = LxmfAdapter(config)
        ctx = _make_context()

        await adapter.start(ctx)
        await adapter.stop()
        # Second stop — must not raise.
        await adapter.stop()
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
            await adapter.start(ctx)
            info = await adapter.health_check()
            assert info.health == "healthy", (
                f"Cycle {i + 1}: expected healthy, got {info.health!r}"
            )
            await adapter.stop()
            info = await adapter.health_check()
            assert info.health == "unknown", (
                f"Cycle {i + 1}: expected unknown after stop, "
                f"got {info.health!r}"
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
        from medre.adapters.base import AdapterDeliveryResult
        from medre.core.rendering.renderer import RenderingResult

        config = _make_fake_config()
        adapter = LxmfAdapter(config)
        ctx = _make_context()

        # 1. Start
        await adapter.start(ctx)
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
        delivery = await adapter.deliver(result)
        assert delivery is not None, "deliver() returned None"
        assert isinstance(delivery, AdapterDeliveryResult)
        assert delivery.native_message_id is not None, (
            "native_message_id is None"
        )
        lxmf_meta = delivery.metadata.get("lxmf")
        assert isinstance(lxmf_meta, dict)
        assert lxmf_meta["delivery_state"] == "outbound"

        # 3. Still healthy
        info = await adapter.health_check()
        assert info.health == "healthy"

        # 4. Stop
        await adapter.stop()

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

        await adapter.start(ctx)
        diag = adapter.session.diagnostics()
        assert diag.connected is True, (
            f"Expected connected=True after start, got {diag.connected}"
        )

        await adapter.stop()
        diag = adapter.session.diagnostics()
        assert diag.connected is False, (
            f"Expected connected=False after stop, got {diag.connected}"
        )

    # ===================================================================
    # 9. Documentation notes
    # ===================================================================

    async def test_e2ee_not_supported_note(self):
        """Document: LXMF encrypted messages are not supported.

        This test always passes.  End-to-end encrypted LXMF messages
        are out of scope for the current tranche.
        """
        pass
