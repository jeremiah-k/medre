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


# ---------------------------------------------------------------------------
# Live tests
# ---------------------------------------------------------------------------
@require_live
class TestMatrixLiveSmoke:
    """Live Matrix connectivity smoke tests.

    These tests connect to a real Matrix homeserver and verify the
    adapter lifecycle, outbound delivery, health transitions, and (where
    feasible without a second actor) self-message suppression.

    All tests require the four MATRIX_* environment variables to be set.
    Run with::

        pytest tests/test_matrix_live.py -m live -v

    The tests are ordered so that the most fundamental operations
    (start/stop/health) come first, followed by delivery, followed by
    optional suppression checks that may be flaky without a second user.
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
