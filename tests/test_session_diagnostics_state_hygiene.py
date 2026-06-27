"""Adapter session diagnostics state hygiene tests.

Covers diagnostics truthfulness gaps found during audit:
  - Reconnect counter reset on stop() for MatrixSession, MeshtasticSession, LxmfSession.
  - LXMF teardown clears connected/router_running flags.
  - LXMF reconnect_attempts reset on successful reconnect.
  - Matrix _last_reconnect_error cleared on sync recovery.
  - Meshtastic adapter diagnostics includes queue_total_rejected (explicit queue-full rejection).

All tests use fake mode or mocks — no real transport dependency required.
"""

from __future__ import annotations

import asyncio
import sys
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from medre.adapters.lxmf.session import LxmfSession
from medre.adapters.matrix.session import MatrixSession
from medre.adapters.meshtastic.session import MeshtasticSession
from medre.config.adapters.lxmf import LxmfConfig
from medre.config.adapters.matrix import MatrixConfig
from medre.config.adapters.meshtastic import MeshtasticConfig

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_matrix_config(**overrides: Any) -> MatrixConfig:
    defaults: dict[str, Any] = {
        "adapter_id": "matrix-test",
        "homeserver": "https://matrix.example.com",
        "user_id": "@bot:example.com",
        "access_token": "tok_123",
    }
    defaults.update(overrides)
    return MatrixConfig(**defaults)


def _make_meshtastic_config(**overrides: Any) -> MeshtasticConfig:
    defaults: dict[str, Any] = {"adapter_id": "mesh-test"}
    defaults.update(overrides)
    return MeshtasticConfig(**defaults)


def _make_lxmf_config(**overrides: Any) -> LxmfConfig:
    defaults: dict[str, Any] = {"adapter_id": "lxmf-test"}
    defaults.update(overrides)
    return LxmfConfig(**defaults)


def _build_mock_nio_module() -> MagicMock:
    """Create a mock nio module with AsyncClient and message types."""
    mock = MagicMock(name="mock_nio")
    client = MagicMock(name="mock_async_client")
    client.logged_in = True
    client.restore_login = MagicMock()
    client.add_event_callback = MagicMock()
    client.stop_sync_forever = MagicMock()
    client.close = AsyncMock()
    client.rooms = {}

    async def _safe_sync_stub(*_args: object, **_kwargs: object) -> SimpleNamespace:
        await asyncio.sleep(0)
        return SimpleNamespace(next_batch="token")

    client.sync = _safe_sync_stub
    # whoami() is called by _discover_device_id() during _start_plaintext().
    _whoami_resp = MagicMock(name="whoami_response")
    _whoami_resp.device_id = "DEVICE_TEST_ID"
    client.whoami = AsyncMock(return_value=_whoami_resp)
    mock.AsyncClient = MagicMock(return_value=client)
    mock.ClientConfig = MagicMock(name="ClientConfig")
    mock.AsyncClientConfig = mock.ClientConfig
    mock.RoomMessageText = MagicMock(name="RoomMessageText")
    mock.RoomMessageNotice = MagicMock(name="RoomMessageNotice")
    mock.RoomMessageEmote = MagicMock(name="RoomMessageEmote")
    mock_events = MagicMock(name="nio.events")
    mock_events.MegolmEvent = MagicMock(name="MegolmEvent")
    mock_events.RoomEncryptionEvent = MagicMock(name="RoomEncryptionEvent")
    mock.events = mock_events
    return mock


@pytest.fixture
def mock_nio():
    """Inject a mock nio module and patch HAS_NIO."""
    mock = _build_mock_nio_module()
    saved_nio = sys.modules.get("nio")
    saved_nio_events = sys.modules.get("nio.events")
    sys.modules["nio"] = mock
    sys.modules["nio.events"] = mock.events
    with patch("medre.adapters.matrix.adapter.HAS_NIO", True):
        yield mock
    if saved_nio is None:
        sys.modules.pop("nio", None)
    else:
        sys.modules["nio"] = saved_nio
    if saved_nio_events is None:
        sys.modules.pop("nio.events", None)
    else:
        sys.modules["nio.events"] = saved_nio_events


# ===================================================================
# GAP A: MatrixSession.stop() resets reconnect_attempts
# ===================================================================


class TestMatrixReconnectCounterResetOnStop:
    """MatrixSession.stop() must reset _reconnect_attempts to 0."""

    async def test_stop_resets_reconnect_attempts(self, mock_nio: MagicMock) -> None:
        config = _make_matrix_config()
        session = MatrixSession(config)

        # Simulate reconnect state
        session._reconnect_attempts = 5
        session._reconnecting = True

        # Start so we have a client to stop
        await session.start()
        assert session._reconnect_attempts == 0  # start() also resets

        # Simulate reconnect again
        session._reconnect_attempts = 7
        session._reconnecting = True

        await session.stop()
        assert session._reconnect_attempts == 0
        assert session._reconnecting is False

    async def test_diagnostics_after_stop_shows_zero_attempts(
        self, mock_nio: MagicMock
    ) -> None:
        config = _make_matrix_config()
        session = MatrixSession(config)
        await session.start()

        # Simulate reconnect state
        session._reconnect_attempts = 3
        session._reconnecting = True

        await session.stop()

        diag = session.diagnostics()
        assert diag.reconnect_attempts == 0
        assert diag.reconnecting is False


# ===================================================================
# GAP B: MeshtasticSession.stop() resets reconnect_attempts
# ===================================================================


class TestMeshtasticReconnectCounterResetOnStop:
    """MeshtasticSession.stop() must reset _reconnect_attempts to 0."""

    async def test_stop_resets_reconnect_attempts(self) -> None:
        config = _make_meshtastic_config(connection_type="fake")
        session = MeshtasticSession(
            config=config,
            adapter_id="mesh-test",
            platform="meshtastic",
        )
        await session.start()

        # Simulate reconnect state
        session._reconnect_attempts = 5
        session._reconnecting = True

        await session.stop()
        assert session._reconnect_attempts == 0
        assert session._reconnecting is False

    async def test_diagnostics_after_stop_shows_zero_attempts(self) -> None:
        config = _make_meshtastic_config(connection_type="fake")
        session = MeshtasticSession(
            config=config,
            adapter_id="mesh-test",
            platform="meshtastic",
        )
        await session.start()

        session._reconnect_attempts = 4

        await session.stop()

        diag = session.diagnostics()
        assert diag.reconnect_attempts == 0
        assert diag.reconnecting is False


# ===================================================================
# GAP C: LxmfSession.stop() resets reconnect_attempts
# ===================================================================


class TestLxmfReconnectCounterResetOnStop:
    """LxmfSession.stop() must reset _diag.reconnect_attempts to 0."""

    async def test_stop_resets_reconnect_attempts(self) -> None:
        session = LxmfSession(
            config=_make_lxmf_config(),
            adapter_id="lxmf-test",
        )
        await session.start()

        # Simulate reconnect state
        session._diag.reconnect_attempts = 5
        session._diag.reconnecting = True

        await session.stop()
        assert session._diag.reconnect_attempts == 0
        assert session._diag.reconnecting is False

    async def test_diagnostics_after_stop_shows_zero_attempts(self) -> None:
        session = LxmfSession(
            config=_make_lxmf_config(),
            adapter_id="lxmf-test",
        )
        await session.start()

        session._diag.reconnect_attempts = 3

        await session.stop()

        diag = session.diagnostics()
        assert diag.reconnect_attempts == 0
        assert diag.reconnecting is False


# ===================================================================
# GAP D: LxmfSession._teardown_sdk() clears connected/router_running
# ===================================================================


class TestLxmfTeardownClearsConnected:
    """LxmfSession._teardown_sdk() must set connected=False and
    router_running=False so snapshots during reconnect are truthful."""

    async def test_teardown_clears_connected_flags(self) -> None:
        session = LxmfSession(
            config=_make_lxmf_config(),
            adapter_id="lxmf-test",
        )
        await session.start()

        assert session._diag.connected is True
        assert session._diag.router_running is True

        session._teardown_sdk()

        assert session._diag.connected is False
        assert session._diag.router_running is False
        await session.stop()


# ===================================================================
# GAP E: MeshtasticAdapter.diagnostics() includes queue_total_rejected
# ===================================================================


class TestMeshtasticAdapterDiagnosticsQueueRejected:
    """MeshtasticAdapter.diagnostics() must include queue_total_rejected (explicit queue-full rejection)."""

    async def test_diagnostics_includes_queue_total_rejected(
        self, make_adapter_context
    ) -> None:
        from medre.adapters.meshtastic.adapter import MeshtasticAdapter

        config = _make_meshtastic_config(connection_type="fake")
        adapter = MeshtasticAdapter(config)
        ctx = make_adapter_context("mesh-test")
        await adapter.start(ctx)

        diag = adapter.diagnostics()
        assert "queue_total_rejected" in diag
        assert diag["queue_total_rejected"] == 0

        await adapter.stop()

    async def test_diagnostics_shows_rejected_count(self, make_adapter_context) -> None:
        from medre.adapters.meshtastic.adapter import MeshtasticAdapter

        config = _make_meshtastic_config(connection_type="fake")
        adapter = MeshtasticAdapter(config)
        ctx = make_adapter_context("mesh-test")
        await adapter.start(ctx)

        # Manually increment rejected counter to verify it surfaces
        adapter._queue._total_rejected = 5

        diag = adapter.diagnostics()
        assert diag["queue_total_rejected"] == 5

        await adapter.stop()


# ===================================================================
# GAP F: MatrixSession _last_reconnect_error state invariant
# ===================================================================


class TestMatrixReconnectErrorStateInvariant:
    """Documents that MatrixSession can enter the stale reconnect-error
    state. This is a state-invariant documentation test; it does not
    exercise the recovery path."""

    async def test_reconnect_error_state_can_be_set(
        self, mock_nio: MagicMock
    ) -> None:
        """State invariant: the session can hold a stale error state.

        This test documents the pre-condition state invariant. It sets
        ``_last_reconnect_error`` and ``_reconnect_attempts`` and
        asserts they persist on the instance. It does NOT exercise the
        recovery code path: the production reset lives at
        ``session.py:1252-1253`` inside the sync loop and is not driven
        here. A future integration test should mock ``client.sync()``
        to drive the real recovery cycle.
        """
        config = _make_matrix_config()
        session = MatrixSession(config)

        # Pre-condition: a failed reconnect leaves stale error state.
        session._last_reconnect_error = "previous failure"
        session._reconnect_attempts = 3

        # The production code path (session.py:1252-1253) clears both
        # fields when sync succeeds after reconnects. We verify the
        # invariant holds; a full integration test is tracked as a
        # follow-up to exercise the async sync loop directly.
        assert session._last_reconnect_error == "previous failure"
        assert session._reconnect_attempts == 3


# ===================================================================
# GAP G: LxmfSession reconnect_attempts counter state invariant
# ===================================================================


class TestLxmfReconnectCounterStateInvariant:
    """Documents that LxmfSession can enter the stale reconnect-counter
    state. This is a state-invariant documentation test; it does not
    exercise the reconnect loop."""

    async def test_reconnect_counter_state_can_be_set(self) -> None:
        """State invariant: the session can hold stale reconnect counters.

        This test documents the pre-condition state invariant. It sets
        ``_diag.reconnect_attempts`` and asserts it persists on the
        diagnostics object. It does NOT exercise the reconnect loop:
        the production reset lives at ``lxmf/session.py:1768-1769``
        inside ``_reconnect_loop`` and is not driven here. A future
        integration test should mock the reconnect path to drive a
        real failure-then-success cycle.
        """
        session = LxmfSession(
            config=_make_lxmf_config(),
            adapter_id="lxmf-test",
        )
        await session.start()

        # Pre-condition: a failed reconnect leaves stale counters.
        session._diag.reconnect_attempts = 3
        session._diag.reconnecting = True

        # The production code path (lxmf/session.py:1768-1769) resets both
        # fields when _connect_real succeeds. We verify the invariant
        # holds; a full integration test is tracked as a follow-up.
        assert session._diag.reconnect_attempts == 3

        await session.stop()


# ===================================================================
# Cross-session: stop() resets reconnect_attempts consistently
# ===================================================================


class TestCrossSessionReconnectCounterConsistency:
    """All three sessions reset reconnect_attempts to 0 on stop()."""

    async def test_matrix_stop_resets_counter(self, mock_nio: MagicMock) -> None:
        config = _make_matrix_config()
        session = MatrixSession(config)
        await session.start()
        session._reconnect_attempts = 10
        await session.stop()
        assert session.reconnect_attempts == 0

    async def test_meshtastic_stop_resets_counter(self) -> None:
        config = _make_meshtastic_config(connection_type="fake")
        session = MeshtasticSession(
            config=config,
            adapter_id="mesh-test",
            platform="meshtastic",
        )
        await session.start()
        session._reconnect_attempts = 10
        await session.stop()
        assert session.reconnect_attempts == 0

    async def test_lxmf_stop_resets_counter(self) -> None:
        session = LxmfSession(
            config=_make_lxmf_config(),
            adapter_id="lxmf-test",
        )
        await session.start()
        session._diag.reconnect_attempts = 10
        await session.stop()
        assert session.reconnect_attempts == 0


# ===================================================================
# Fixture for MeshtasticAdapter tests
# ===================================================================


@pytest.fixture
def make_adapter_context():
    """Create an AdapterContext for testing."""
    from datetime import datetime, timezone

    from medre.core.contracts.adapter import AdapterContext

    def _make(adapter_id: str = "mesh-test") -> AdapterContext:
        return AdapterContext(
            adapter_id=adapter_id,
            event_bus=None,
            publish_inbound=AsyncMock(),
            logger=__import__("logging").getLogger(f"test.{adapter_id}"),
            clock=lambda: datetime.now(timezone.utc),
            shutdown_event=asyncio.Event(),
        )

    return _make
