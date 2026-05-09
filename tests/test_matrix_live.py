"""Live Matrix adapter connectivity smoke tests.

These tests connect to a **real** Matrix homeserver and exercise the
MEDRE Matrix adapter's lifecycle, outbound delivery, and self-message
suppression.  They are **skipped by default** and require explicit
opt-in via environment variables.

**Running live tests:**

1. Start a local Matrix homeserver (Synapse or Conduit), or use an
   existing homeserver where you have a bot account.

2. Obtain an access token for the bot account (e.g. via Element's
   "Help & About → Access Token" or by hitting the
   ``/_matrix/client/v3/login`` endpoint directly).

3. Set the required environment variables:

   .. code-block:: bash

       export MATRIX_HOMESERVER="https://matrix.example.com"
       export MATRIX_USER_ID="@bot:example.com"
       export MATRIX_ACCESS_TOKEN="syt_...your_token..."
       export MATRIX_ROOM_ID="!room:example.com"

4. Run the live tests:

   .. code-block:: bash

       pip install -e ".[matrix]"
       pytest tests/test_matrix_live.py -m live -v

   Default ``pytest`` run (no live tests):

   .. code-block:: bash

       pytest   # live tests excluded by addopts

   Override to include live tests:

   .. code-block:: bash

       pytest -m ""   # run ALL tests including live

**Required environment variables:**

======================== =====================================================
Variable                 Description
======================== =====================================================
``MATRIX_HOMESERVER``    Full URL of the Matrix homeserver
                         (e.g. ``http://localhost:8008``)
``MATRIX_USER_ID``       Fully-qualified Matrix user ID
                         (e.g. ``@bot:localhost``)
``MATRIX_ACCESS_TOKEN``  Access token for the bot account
                         (e.g. ``syt_xxx...``)
``MATRIX_ROOM_ID``       Room ID to send test messages to
                         (e.g. ``!abc:localhost``)
======================== =====================================================

If any variable is unset, every test in this file skips cleanly with a
descriptive reason string.

**Known limitations (explicit):**

- **No E2EE.** These tests target unencrypted rooms only.
  End-to-end encryption is not part of the Matrix tranche 1 scope.
- **No reactions, edits, deletes, or attachments.** Only ``m.text``
  delivery is tested.
- **No admin API usage.** Tests do not call Matrix admin endpoints.
- **No webhook/HTTP server.** Tests exercise the nio sync loop only.
- **No non-Matrix connectivity.** Meshtastic, MeshCore, LXMF, and
  other adapters are out of scope.
- **No auth command / credential storage.** The current tranche uses
  environment-variable access tokens exclusively.  A future mmrelay-like
  ``auth`` command for interactive login and credential management may
  be useful but is not implemented in this tranche.

**Local homeserver setup (no Docker required):**

Synapse via pip (recommended for testing):

.. code-block:: bash

    pip install matrix-synapse
    # Generate a minimal homeserver config
    python -m synapse.app.homeserver \\
      --server-name localhost \\
      --config-path homeserver.yaml \\
      --generate-config \\
      --report-stats=no
    # Start Synapse
    python -m synapse.app.homeserver --config-path homeserver.yaml
    # Register a bot user and obtain a token:
    #   register_new_matrix_user -c homeserver.yaml \\
    #     -u bot -p secret http://localhost:8008
    # Then login and extract access_token from the JSON response:
    #   curl -X POST \\
    #     -d '{"type":"m.login.password","user":"bot","password":"secret"}' \\
    #     http://localhost:8008/_matrix/client/v3/login

Conduit via binary (lightweight Rust homeserver):

.. code-block:: bash

    # Download from https://conduit.rs or build from source
    # https://gitlab.com/famedly/conduit
    conduit  # starts on port 6167 by default
    # Register via any Matrix client (Element, etc.)
    # Set MATRIX_HOMESERVER="http://localhost:6167"

Docker (optional, not required):

.. code-block:: bash

    docker run -d --name synapse -p 8008:8008 \\
      -e SYNAPSE_SERVER_NAME=localhost \\
      -e SYNAPSE_REPORT_STATS=no \\
      matrixdotorg/synapse:latest
    docker exec synapse register_new_matrix_user \\
      -u bot -p secret -c /data/homeserver.yaml http://localhost:8008
    curl -X POST \\
      -d '{"type":"m.login.password","user":"bot","password":"secret"}' \\
      http://localhost:8008/_matrix/client/v3/login
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
# Environment variable gate — every test in this file skips unless all four
# required variables are set, so CI and local runs never fail noisily.
# ---------------------------------------------------------------------------
MATRIX_HOMESERVER = os.environ.get("MATRIX_HOMESERVER")
MATRIX_USER_ID = os.environ.get("MATRIX_USER_ID")
MATRIX_ACCESS_TOKEN = os.environ.get("MATRIX_ACCESS_TOKEN")
MATRIX_ROOM_ID = os.environ.get("MATRIX_ROOM_ID")

_LIVE_ENV_SET = all(
    [MATRIX_HOMESERVER, MATRIX_USER_ID, MATRIX_ACCESS_TOKEN, MATRIX_ROOM_ID]
)

require_live = pytest.mark.skipif(
    not _LIVE_ENV_SET,
    reason=(
        "Set MATRIX_HOMESERVER, MATRIX_USER_ID, MATRIX_ACCESS_TOKEN, "
        "and MATRIX_ROOM_ID env vars to run live Matrix tests"
    ),
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _make_config():
    """Build a MatrixConfig from the live environment variables."""
    from medre.adapters.matrix.config import MatrixConfig

    assert MATRIX_HOMESERVER and MATRIX_USER_ID and MATRIX_ACCESS_TOKEN
    return MatrixConfig(
        adapter_id="matrix-live-smoke",
        homeserver=MATRIX_HOMESERVER,
        user_id=MATRIX_USER_ID,
        access_token=MATRIX_ACCESS_TOKEN,
    )


def _make_context():
    """Build an AdapterContext suitable for live smoke tests."""
    from medre.adapters.base import AdapterContext

    return AdapterContext(
        adapter_id="matrix-live-smoke",
        event_bus=None,
        publish_inbound=AsyncMock(),
        logger=logging.getLogger("test.matrix-live"),
        clock=lambda: datetime.now(timezone.utc),
        shutdown_event=asyncio.Event(),
    )


def _make_config_with_allowlist(allowlist: set[str] | None):
    """Build a MatrixConfig with a specific room_allowlist."""
    from medre.adapters.matrix.config import MatrixConfig

    assert MATRIX_HOMESERVER and MATRIX_USER_ID and MATRIX_ACCESS_TOKEN
    return MatrixConfig(
        adapter_id="matrix-live-smoke",
        homeserver=MATRIX_HOMESERVER,
        user_id=MATRIX_USER_ID,
        access_token=MATRIX_ACCESS_TOKEN,
        room_allowlist=allowlist,
    )


# ---------------------------------------------------------------------------
# Live tests
# ---------------------------------------------------------------------------
@require_live
class TestMatrixLiveSmoke:
    """Live Matrix connectivity smoke tests.

    These tests connect to a real Matrix homeserver and verify the
    adapter lifecycle, outbound delivery, health transitions, self-message
    suppression, room allowlist enforcement, restart idempotency, and
    adapter redelivery.

    All tests require the four MATRIX_* environment variables to be set.
    Run with::

        pytest tests/test_matrix_live.py -m live -v

    The tests are ordered so that the most fundamental operations
    (start/stop/health) come first, followed by delivery, followed by
    echo suppression, allowlist enforcement, restart idempotency, and
    adapter redelivery smoke tests.  Suppression and allowlist checks may be flaky
    without a second user but are included for defense-in-depth coverage.
    """

    # -- Lifecycle: start, health, stop ------------------------------------

    async def test_adapter_starts_and_reports_healthy(self):
        """Start the adapter and verify health_check reports healthy.

        This validates:
        - ``mindroom-nio`` is installed and importable.
        - The access token is accepted by the homeserver.
        - ``restore_login`` results in ``logged_in == True``.
        - ``health_check()`` returns ``"healthy"`` and ``platform == "matrix"``.
        """
        from medre.adapters.matrix.adapter import MatrixAdapter

        adapter = MatrixAdapter(_make_config())
        ctx = _make_context()
        await adapter.start(ctx)
        try:
            info = await adapter.health_check()
            assert info.health == "healthy", (
                f"Expected healthy after start, got {info.health!r}"
            )
            assert info.platform == "matrix"
        finally:
            await adapter.stop()

    async def test_adapter_health_unknown_after_stop(self):
        """Stop the adapter and verify health_check reports unknown.

        After ``stop()``, the internal ``_client`` is set to ``None``
        and ``health_check()`` must return ``"unknown"``.
        """
        from medre.adapters.matrix.adapter import MatrixAdapter

        adapter = MatrixAdapter(_make_config())
        ctx = _make_context()
        await adapter.start(ctx)
        await adapter.stop()
        info = await adapter.health_check()
        assert info.health == "unknown", (
            f"Expected unknown after stop, got {info.health!r}"
        )

    async def test_adapter_health_unknown_before_start(self):
        """Health check on a never-started adapter returns unknown."""
        from medre.adapters.matrix.adapter import MatrixAdapter

        adapter = MatrixAdapter(_make_config())
        info = await adapter.health_check()
        assert info.health == "unknown"
        assert info.platform == "matrix"

    # -- Outbound delivery --------------------------------------------------

    async def test_send_text_message_captures_event_id(self):
        """Send a text message and verify the returned event_id.

        This validates:
        - ``room_send`` succeeds against the real homeserver.
        - ``RoomSendResponse.event_id`` is populated.
        - The event_id follows the Matrix convention of starting with ``$``.
        - ``native_channel_id`` matches the target room.
        """
        from medre.adapters.matrix.adapter import MatrixAdapter
        from medre.core.rendering.renderer import RenderingResult

        adapter = MatrixAdapter(_make_config())
        ctx = _make_context()
        await adapter.start(ctx)
        try:
            ts = int(time.time())
            result = RenderingResult(
                event_id=f"live-smoke-{ts}",
                target_adapter="matrix-live-smoke",
                target_channel=MATRIX_ROOM_ID,
                payload={
                    "msgtype": "m.text",
                    "body": f"MEDRE live smoke test (ts={ts}) — safe to ignore",
                },
                metadata={"renderer": "matrix", "test": "live-smoke"},
            )
            delivery = await adapter.deliver(result)
            assert delivery is not None, "deliver() returned None"
            assert delivery.native_message_id is not None, (
                "native_message_id is None — homeserver did not return event_id"
            )
            assert delivery.native_message_id.startswith("$"), (
                f"Matrix event_id should start with '$', "
                f"got {delivery.native_message_id!r}"
            )
            assert delivery.native_channel_id == MATRIX_ROOM_ID, (
                f"Expected channel {MATRIX_ROOM_ID!r}, "
                f"got {delivery.native_channel_id!r}"
            )
        finally:
            await adapter.stop()

    # -- Full lifecycle round-trip ------------------------------------------

    async def test_full_lifecycle_start_send_stop(self):
        """Exercise start → send → health(healthy) → stop → health(unknown).

        A single ordered round-trip that validates the complete adapter
        lifecycle in one test, catching ordering or state-leak bugs.
        """
        from medre.adapters.matrix.adapter import MatrixAdapter
        from medre.core.rendering.renderer import RenderingResult

        adapter = MatrixAdapter(_make_config())
        ctx = _make_context()

        # 1. Start
        await adapter.start(ctx)
        info = await adapter.health_check()
        assert info.health == "healthy"

        # 2. Send
        ts = int(time.time())
        result = RenderingResult(
            event_id=f"lifecycle-{ts}",
            target_adapter="matrix-live-smoke",
            target_channel=MATRIX_ROOM_ID,
            payload={
                "msgtype": "m.text",
                "body": f"MEDRE lifecycle test (ts={ts}) — safe to ignore",
            },
            metadata={"renderer": "matrix", "test": "lifecycle"},
        )
        delivery = await adapter.deliver(result)
        assert delivery is not None
        assert delivery.native_message_id is not None

        # 3. Still healthy
        info = await adapter.health_check()
        assert info.health == "healthy"

        # 4. Stop
        await adapter.stop()

        # 5. Now unknown
        info = await adapter.health_check()
        assert info.health == "unknown"

    # -- Suppression: self-message and MEDRE-origin -------------------------
    #
    # Self-message suppression is tested deterministically in unit tests
    # (test_matrix_lifecycle.py, test_matrix_codec.py, etc.) because:
    #
    # 1. With only one Matrix account and no second actor, reliably
    #    triggering a self-echo from the homeserver sync stream is flaky.
    #    The sync may not have delivered the echo event before the test
    #    checks ``publish_inbound``.
    #
    # 2. A reliable live suppression test would require either:
    #    - A second Matrix account to observe the echoed message, OR
    #    - A poll/wait loop with timeout, which introduces timing
    #      sensitivity and makes the test suite non-deterministic.
    #
    # 3. The deterministic unit tests already cover:
    #    - Sender check suppression (sender == config.user_id)
    #    - MEDRE-origin envelope suppression (envelope.source_adapter match)
    #    - Missing sender passthrough
    #    - Corrupt envelope tolerance
    #
    # If a second test account becomes available in the future, a live
    # suppression test can be added here that:
    #    - Sends a message via the adapter
    #    - Waits for the echo via sync
    #    - Asserts publish_inbound was NOT called with the echo
    #
    # For now, live coverage of suppression is explicitly limited to
    # the documented note above and the deterministic fake/unit tests.

    async def test_self_message_suppression_note(self):
        """Document: self-message suppression is covered by unit tests.

        This test always passes.  It exists to document why live
        suppression testing is limited and to keep the coverage note
        visible in test reports.
        """
        # Self-message suppression (sender == config.user_id) is
        # unconditionally enforced in _on_room_message before decode.
        # Live validation requires a second actor or unreliable waits.
        # Deterministic coverage: tests/test_matrix_lifecycle.py,
        # tests/test_matrix_codec.py, tests/test_matrix_adapter.py.
        pass

    async def test_medre_origin_envelope_suppression_note(self):
        """Document: MEDRE-origin suppression is covered by unit tests.

        This test always passes.  The MEDRE-origin envelope check is a
        secondary suppression path; the primary path is the sender check.
        Both are tested deterministically in unit tests.
        """
        # MEDRE-origin envelope suppression (envelope.source_adapter match)
        # is defense-in-depth.  Storage is authoritative for dedup.
        # Deterministic coverage: tests/test_matrix_adapter.py.
        pass

    # -- Send and verify: echo suppression round-trip ------------------------

    async def test_live_send_and_receive(self):
        """Send a message via deliver(), wait for sync, verify echo suppression.

        Validates the complete send → sync → suppress pipeline:

        1. ``deliver()`` sends an ``m.text`` message to ``MATRIX_ROOM_ID``.
        2. The nio sync loop receives the echo event from the homeserver.
        3. ``_on_room_message`` suppresses the self-echo because
           ``event.sender == config.user_id``.
        4. ``publish_inbound`` is never called with the echo event.

        The test waits 5 seconds to give the sync loop time to process
        the echo.  In a quiet room ``publish_inbound`` should have zero
        calls; in an active room none of the calls should correspond to
        our sent message.

        **Caveat**: without a second Matrix account we cannot verify that
        non-self messages *are* published.  Unit tests in
        ``test_matrix_lifecycle.py`` cover the non-self path.
        """
        from medre.adapters.matrix.adapter import MatrixAdapter
        from medre.adapters.base import AdapterContext
        from medre.core.rendering.renderer import RenderingResult

        publish_mock = AsyncMock()
        ctx = AdapterContext(
            adapter_id="matrix-live-smoke",
            event_bus=None,
            publish_inbound=publish_mock,
            logger=logging.getLogger("test.matrix-live.echo"),
            clock=lambda: datetime.now(timezone.utc),
            shutdown_event=asyncio.Event(),
        )

        adapter = MatrixAdapter(_make_config())
        await adapter.start(ctx)
        try:
            ts = int(time.time())
            body_text = (
                f"MEDRE echo-suppression live test (ts={ts}) — safe to ignore"
            )
            result = RenderingResult(
                event_id=f"live-echo-{ts}",
                target_adapter="matrix-live-smoke",
                target_channel=MATRIX_ROOM_ID,
                payload={
                    "msgtype": "m.text",
                    "body": body_text,
                },
                metadata={"renderer": "matrix", "test": "echo-suppression"},
            )
            delivery = await adapter.deliver(result)
            assert delivery is not None, "deliver() returned None"
            assert delivery.native_message_id is not None, (
                "native_message_id is None — homeserver did not return event_id"
            )
            event_id_sent = delivery.native_message_id

            # Give the sync loop time to process the echo event.
            await asyncio.sleep(5.0)

            # Self-message suppression should have blocked the echo.
            # Verify no published event contains our sent event_id or body.
            for call in publish_mock.call_args_list:
                args = call.args
                if args:
                    event = args[0]
                    # Check native refs for our sent event_id
                    native_refs = (
                        event.metadata.native_refs
                        if hasattr(event, "metadata") and event.metadata
                        else ()
                    )
                    for ref in native_refs:
                        if hasattr(ref, "native_message_id"):
                            assert ref.native_message_id != event_id_sent, (
                                f"Self-echo leaked through: event with native "
                                f"message id {event_id_sent!r} was published "
                                f"inbound"
                            )
                    # Check payload body doesn't match our test message
                    if hasattr(event, "payload") and isinstance(event.payload, dict):
                        assert event.payload.get("text") != body_text, (
                            "Self-echo leaked through: payload body matches "
                            "our sent message"
                        )
        finally:
            await adapter.stop()

    # -- Allowlist enforcement -----------------------------------------------

    async def test_live_allowlist_enforcement(self):
        """Verify room_allowlist blocks inbound events from non-allowlisted rooms.

        Strategy:

        1. Create an adapter whose ``room_allowlist`` does NOT include
           ``MATRIX_ROOM_ID``.  Start it and verify health is ``healthy``
           (allowlist filtering is an inbound concern, not a startup gate).
        2. Send a message to ``MATRIX_ROOM_ID`` — outbound delivery succeeds
           because ``deliver()`` does not apply the allowlist.
        3. Wait for the sync echo — ``_on_room_message`` filters the echo
           at the allowlist check (before the self-message suppression).
        4. Stop the adapter.
        5. Create a second adapter with ``room_allowlist={MATRIX_ROOM_ID}``.
           Start it and verify health is ``healthy``.
        6. Stop the second adapter.

        **Caveat**: with a single Matrix account the allowlist filter and
        self-message suppression both fire for our own messages.  Unit
        tests in ``test_matrix_lifecycle.py`` and ``test_matrix_codec.py``
        isolate the allowlist check from self-message suppression.
        """
        from medre.adapters.matrix.adapter import MatrixAdapter
        from medre.adapters.base import AdapterContext
        from medre.core.rendering.renderer import RenderingResult

        wrong_allowlist = {"!nonexistent-room:example.com"}

        # --- Phase 1: wrong allowlist ---
        config_blocked = _make_config_with_allowlist(wrong_allowlist)
        publish_blocked = AsyncMock()
        ctx_blocked = AdapterContext(
            adapter_id="matrix-live-allowlist-blocked",
            event_bus=None,
            publish_inbound=publish_blocked,
            logger=logging.getLogger("test.matrix-live.allowlist-blocked"),
            clock=lambda: datetime.now(timezone.utc),
            shutdown_event=asyncio.Event(),
        )
        adapter_blocked = MatrixAdapter(config_blocked)
        await adapter_blocked.start(ctx_blocked)
        try:
            info = await adapter_blocked.health_check()
            assert info.health == "healthy", (
                f"Expected healthy with wrong allowlist (inbound filter), "
                f"got {info.health!r}"
            )

            # Outbound delivery should still work (allowlist is inbound-only)
            ts = int(time.time())
            result = RenderingResult(
                event_id=f"live-allowlist-blocked-{ts}",
                target_adapter="matrix-live-allowlist-blocked",
                target_channel=MATRIX_ROOM_ID,
                payload={
                    "msgtype": "m.text",
                    "body": (
                        f"MEDRE allowlist blocked test (ts={ts}) "
                        f"— safe to ignore"
                    ),
                },
                metadata={
                    "renderer": "matrix",
                    "test": "allowlist-blocked",
                },
            )
            delivery = await adapter_blocked.deliver(result)
            assert delivery is not None, "deliver() returned None"
            assert delivery.native_message_id is not None, (
                "native_message_id is None with wrong allowlist"
            )

            # Wait for sync echo — should be suppressed by allowlist
            # (and also by self-message suppression as defense-in-depth).
            await asyncio.sleep(3.0)

            # Verify no inbound events were published for our message.
            # With wrong allowlist, the allowlist filter fires BEFORE
            # self-message suppression in _on_room_message.
            for call in publish_blocked.call_args_list:
                args = call.args
                if args:
                    event = args[0]
                    if hasattr(event, "payload") and isinstance(event.payload, dict):
                        assert (
                            event.payload.get("text")
                            != f"MEDRE allowlist blocked test (ts={ts}) "
                            f"— safe to ignore"
                        ), (
                            "Allowlist enforcement failure: message from "
                            "non-allowlisted room was published inbound"
                        )
        finally:
            await adapter_blocked.stop()

        # --- Phase 2: correct allowlist ---
        assert MATRIX_ROOM_ID is not None  # narrowed by @require_live gate
        config_allowed = _make_config_with_allowlist({MATRIX_ROOM_ID})
        ctx_allowed = AdapterContext(
            adapter_id="matrix-live-allowlist-allowed",
            event_bus=None,
            publish_inbound=AsyncMock(),
            logger=logging.getLogger("test.matrix-live.allowlist-allowed"),
            clock=lambda: datetime.now(timezone.utc),
            shutdown_event=asyncio.Event(),
        )
        adapter_allowed = MatrixAdapter(config_allowed)
        await adapter_allowed.start(ctx_allowed)
        try:
            info = await adapter_allowed.health_check()
            assert info.health == "healthy", (
                f"Expected healthy with correct allowlist, "
                f"got {info.health!r}"
            )
        finally:
            await adapter_allowed.stop()

    # -- Health check operational --------------------------------------------

    async def test_live_health_check_operational(self):
        """Verify health_check reflects connected state with full AdapterInfo.

        After ``start()``, health_check must return:

        - ``health == "healthy"`` (client is logged in)
        - ``platform == "matrix"``
        - ``role == AdapterRole.PRESENTATION``
        - ``adapter_id`` matches config

        After ``stop()``, health_check must return:

        - ``health == "unknown"`` (client is None)
        - ``platform`` and ``role`` still populated
        """
        from medre.adapters.matrix.adapter import MatrixAdapter
        from medre.adapters.base import AdapterContext, AdapterRole

        adapter = MatrixAdapter(_make_config())
        ctx = AdapterContext(
            adapter_id="matrix-live-smoke",
            event_bus=None,
            publish_inbound=AsyncMock(),
            logger=logging.getLogger("test.matrix-live.health"),
            clock=lambda: datetime.now(timezone.utc),
            shutdown_event=asyncio.Event(),
        )

        # Before start
        info = await adapter.health_check()
        assert info.health == "unknown"
        assert info.platform == "matrix"

        # After start — full operational state
        await adapter.start(ctx)
        try:
            info = await adapter.health_check()
            assert info.health == "healthy"
            assert info.platform == "matrix"
            assert info.role == AdapterRole.PRESENTATION
            assert info.adapter_id == "matrix-live-smoke"
        finally:
            await adapter.stop()

        # After stop — back to unknown
        info = await adapter.health_check()
        assert info.health == "unknown"
        assert info.platform == "matrix"

    # -- Restart idempotency ------------------------------------------------

    async def test_live_restart_idempotency(self):
        """Full start → stop → start → stop cycle verifies idempotent restart.

        Exercises the complete restart lifecycle:

        1. Start adapter → verify healthy
        2. Stop adapter → verify unknown
        3. Start adapter again → verify healthy
        4. Stop adapter again → verify unknown

        This catches state leaks, stale client references, and unclean
        shutdown that would prevent a second start.
        """
        from medre.adapters.matrix.adapter import MatrixAdapter
        from medre.adapters.base import AdapterContext

        config = _make_config()

        # Cycle 1
        ctx1 = AdapterContext(
            adapter_id="matrix-live-smoke",
            event_bus=None,
            publish_inbound=AsyncMock(),
            logger=logging.getLogger("test.matrix-live.restart"),
            clock=lambda: datetime.now(timezone.utc),
            shutdown_event=asyncio.Event(),
        )
        adapter = MatrixAdapter(config)
        await adapter.start(ctx1)
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
        ctx2 = AdapterContext(
            adapter_id="matrix-live-smoke",
            event_bus=None,
            publish_inbound=AsyncMock(),
            logger=logging.getLogger("test.matrix-live.restart"),
            clock=lambda: datetime.now(timezone.utc),
            shutdown_event=asyncio.Event(),
        )
        await adapter.start(ctx2)
        info = await adapter.health_check()
        assert info.health == "healthy", (
            f"Cycle 2 start: expected healthy, got {info.health!r}"
        )
        await adapter.stop()
        info = await adapter.health_check()
        assert info.health == "unknown", (
            f"Cycle 2 stop: expected unknown, got {info.health!r}"
        )

    # -- Adapter redelivery smoke --------------------------------------------

    async def test_live_adapter_redelivery_smoke(self):
        """Verify the adapter supports re-delivery of a previously sent event.

        This test sends an event, then re-sends the same logical event
        (same ``canonical_event_id`` but a new ``native_message_id``) to
        verify:

        1. Both sends succeed (Matrix allows duplicate content).
        2. The second ``deliver()`` returns a **new** ``native_message_id``
           (Matrix assigns unique event IDs even for identical content).
        3. Both deliveries target the same room.

        This is **not** a core replay-engine test.  This validates that the
        Matrix adapter can redeliver rendered output through ``room_send``
        and receive a new native ``event_id``.  See ``test_replay.py`` for
        the storage-level replay mechanism.
        """
        from medre.adapters.matrix.adapter import MatrixAdapter
        from medre.adapters.base import AdapterContext
        from medre.core.rendering.renderer import RenderingResult

        adapter = MatrixAdapter(_make_config())
        ctx = AdapterContext(
            adapter_id="matrix-live-smoke",
            event_bus=None,
            publish_inbound=AsyncMock(),
            logger=logging.getLogger("test.matrix-live.redelivery"),
            clock=lambda: datetime.now(timezone.utc),
            shutdown_event=asyncio.Event(),
        )
        await adapter.start(ctx)
        try:
            ts = int(time.time())
            canonical_id = f"live-redelivery-{ts}"

            # First send
            result1 = RenderingResult(
                event_id=canonical_id,
                target_adapter="matrix-live-smoke",
                target_channel=MATRIX_ROOM_ID,
                payload={
                    "msgtype": "m.text",
                    "body": f"MEDRE adapter redelivery smoke test (ts={ts}) — safe to ignore",
                },
                metadata={"renderer": "matrix", "test": "redelivery-smoke"},
            )
            delivery1 = await adapter.deliver(result1)
            assert delivery1 is not None, "First deliver() returned None"
            assert delivery1.native_message_id is not None, (
                "First delivery: native_message_id is None"
            )
            native_id_1 = delivery1.native_message_id

            # Second send — same canonical ID, identical payload
            result2 = RenderingResult(
                event_id=canonical_id,
                target_adapter="matrix-live-smoke",
                target_channel=MATRIX_ROOM_ID,
                payload={
                    "msgtype": "m.text",
                    "body": f"MEDRE adapter redelivery smoke test (ts={ts}) — safe to ignore",
                },
                metadata={"renderer": "matrix", "test": "redelivery-smoke"},
            )
            delivery2 = await adapter.deliver(result2)
            assert delivery2 is not None, "Second deliver() returned None"
            assert delivery2.native_message_id is not None, (
                "Second delivery: native_message_id is None"
            )
            native_id_2 = delivery2.native_message_id

            # Matrix assigns unique event IDs even for identical content.
            assert native_id_1 != native_id_2, (
                f"Expected distinct native event IDs for two sends, "
                f"got {native_id_1!r} for both"
            )

            # Both should target the same room.
            assert delivery1.native_channel_id == MATRIX_ROOM_ID
            assert delivery2.native_channel_id == MATRIX_ROOM_ID
        finally:
            await adapter.stop()
