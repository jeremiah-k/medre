"""Live Matrix adapter connectivity tests.

These tests are **skipped by default**.  To run them:

1. Start a local Matrix homeserver (Synapse or Conduit), or use an
   existing homeserver where you have a bot account.

2. Obtain an access token for the bot account (e.g. via Element's
   "Help & About → Access Token" or by running ``pip install matrix-nio
   && python -c \"from nio import AsyncClient; ...\"``).

3. Set the required environment variables:

   .. code-block:: bash

       export MATRIX_HOMESERVER="https://matrix.example.com"
       export MATRIX_USER_ID="@bot:example.com"
       export MATRIX_ACCESS_TOKEN="...your token..."
       export MATRIX_ROOM_ID="!room:example.com"

4. Run the live tests:

   .. code-block:: bash

       pip install medre[matrix]
       pytest tests/test_matrix_live.py -m live -v

   Or run the entire suite excluding live tests (this is the default):

   .. code-block:: bash

       pytest   # live tests are automatically excluded

   Or override the exclusion to include live tests:

   .. code-block:: bash

       pytest -m ""   # run ALL tests including live

Quick-start with local Synapse:

.. code-block:: bash

    # Start Synapse (requires Docker)
    docker run -d --name synapse \
      -p 8008:8008 \
      -e SYNAPSE_SERVER_NAME=localhost \
      -e SYNAPSE_REPORT_STATS=no \
      matrixdotorg/synapse:latest

    # Register a bot user
    docker exec synapse register_new_matrix_user \
      -u bot -p secret -c /data/homeserver.yaml http://localhost:8008

    # Get the access token (login as bot)
    curl -X POST -d '{"type":"m.login.password","user":"bot","password":"secret"}' \
      http://localhost:8008/_matrix/client/v3/login

    # Create a room
    # (use Element or a Matrix client to create a room and invite the bot)

    export MATRIX_HOMESERVER="http://localhost:8008"
    export MATRIX_USER_ID="@bot:localhost"
    export MATRIX_ACCESS_TOKEN="...from login response..."
    export MATRIX_ROOM_ID="!room:localhost"

    pip install -e ".[matrix]"
    pytest tests/test_matrix_live.py -m live -v

Quick-start with local Conduit:

.. code-block:: bash

    # Conduit is a lightweight Matrix homeserver written in Rust
    # https://gitlab.com/famedly/conduit
    docker run -d --name conduit \
      -p 6167:6167 \
      -e CONDUIT_SERVER_NAME=localhost \
      -e CONDUIT_ALLOW_REGISTRATION=true \
      -e CONDUIT_ALLOW_FEDERATION=false \
      matrixconduit/conduit:latest

    # Register and token acquisition is similar to Synapse
    # See https://docs.conduit.rs for details

Known limitations:
- No E2EE support
- No reactions, edits, deletes, or attachments
- No production credential storage
- No admin API
- Storage is authoritative; metadata envelope is secondary
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
# Environment variable gate — every test in this file skips unless all four
# variables are set, so CI and local runs never fail noisily.
# ---------------------------------------------------------------------------
MATRIX_HOMESERVER = os.environ.get("MATRIX_HOMESERVER")
MATRIX_USER_ID = os.environ.get("MATRIX_USER_ID")
MATRIX_ACCESS_TOKEN = os.environ.get("MATRIX_ACCESS_TOKEN")
MATRIX_ROOM_ID = os.environ.get("MATRIX_ROOM_ID")

require_live = pytest.mark.skipif(
    not all([MATRIX_HOMESERVER, MATRIX_USER_ID, MATRIX_ACCESS_TOKEN, MATRIX_ROOM_ID]),
    reason="Set MATRIX_HOMESERVER, MATRIX_USER_ID, MATRIX_ACCESS_TOKEN, and MATRIX_ROOM_ID env vars",
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _make_config():
    from medre.adapters.matrix.config import MatrixConfig

    assert MATRIX_HOMESERVER and MATRIX_USER_ID and MATRIX_ACCESS_TOKEN
    return MatrixConfig(
        adapter_id="matrix-live",
        homeserver=MATRIX_HOMESERVER,
        user_id=MATRIX_USER_ID,
        access_token=MATRIX_ACCESS_TOKEN,
    )


def _make_context():
    from medre.adapters.base import AdapterContext

    return AdapterContext(
        adapter_id="matrix-live",
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
class TestMatrixLiveConnectivity:
    """Live Matrix connectivity tests.

    These tests connect to a real Matrix homeserver and require:
    - MATRIX_HOMESERVER environment variable
    - MATRIX_USER_ID environment variable
    - MATRIX_ACCESS_TOKEN environment variable
    - MATRIX_ROOM_ID environment variable

    Run with:
        pytest tests/test_matrix_live.py -m live -v
    """

    async def test_connect_and_start(self):
        """Connect to the homeserver and start the adapter."""
        from medre.adapters.matrix.adapter import MatrixAdapter

        adapter = MatrixAdapter(_make_config())
        ctx = _make_context()
        await adapter.start(ctx)
        try:
            info = await adapter.health_check()
            assert info.health == "healthy"
            assert info.platform == "matrix"
        finally:
            await adapter.stop()

    async def test_send_text_message(self):
        """Send a text message and verify the returned event_id."""
        from medre.adapters.matrix.adapter import MatrixAdapter
        from medre.core.rendering.renderer import RenderingResult

        adapter = MatrixAdapter(_make_config())
        ctx = _make_context()
        await adapter.start(ctx)
        try:
            result = RenderingResult(
                event_id="live-test-001",
                target_adapter="matrix-live",
                target_channel=MATRIX_ROOM_ID,
                payload={
                    "msgtype": "m.text",
                    "body": "MEDRE live connectivity test — ignore",
                },
                metadata={"renderer": "matrix", "test": "live"},
            )
            delivery = await adapter.deliver(result)
            assert delivery is not None
            assert delivery.native_message_id is not None
            assert delivery.native_message_id.startswith("$")
            assert delivery.native_channel_id == MATRIX_ROOM_ID
        finally:
            await adapter.stop()
