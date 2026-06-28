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
from collections.abc import Callable
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
def mock_nio() -> MagicMock:
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
# Matrix reconnect counter reset on stop
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
# Meshtastic reconnect counter reset on stop
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
# LXMF reconnect counter reset on stop
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
# LXMF teardown clears connected flags
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
# Meshtastic adapter diagnostics queue rejected
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
# Matrix sync recovery clears stale reconnect state
# ===================================================================


class TestMatrixReconnectErrorStateRecovery:
    """Drives the production sync recovery path and asserts it clears
    pre-set stale reconnect state on a successful sync. Replaces a
    former state-invariant test that only set the fields and
    re-asserted them."""

    async def test_sync_recovery_clears_stale_reconnect_state(self) -> None:
        """Behavior: ``_sync_with_reconnect`` clears stale state on a
        successful sync.

        Pre-sets stale ``_reconnect_attempts`` and ``_last_reconnect_error``
        (simulating state left by a prior failed reconnect), then drives
        the production recovery method with a fake client whose
        ``sync()`` returns a successful response. Asserts the recovery
        code at ``src/medre/adapters/matrix/session.py`` clears both
        fields and drops the reconnecting flag.
        """
        config = _make_matrix_config()
        session = MatrixSession(config)

        # Minimal fake client — bypasses start() and the real nio SDK.
        fake_client = MagicMock()
        fake_client.olm = None  # skip the E2EE key-management block
        fake_client.send_to_device_messages = AsyncMock()

        async def _sync_then_stop(**_kwargs: object) -> SimpleNamespace:
            # Stop the loop after one successful iteration so the test
            # exits cleanly instead of looping forever.
            session._stop_requested = True
            return SimpleNamespace(next_batch="recovered-token")

        fake_client.sync = _sync_then_stop
        session._client = fake_client

        # Pre-condition: stale state from a prior failed reconnect.
        session._reconnect_attempts = 4
        session._last_reconnect_error = "previous failure"
        session._reconnecting = True

        # Drive the production recovery method.
        await session._sync_with_reconnect()

        # Production recovery cleared the stale state.
        assert session._reconnect_attempts == 0
        assert session._last_reconnect_error is None
        assert session._reconnecting is False


# ===================================================================
# LXMF reconnect loop clears state after failure then success
# ===================================================================


class TestLxmfReconnectCounterRecovery:
    """Drives the production LXMF reconnect loop and asserts it clears
    stale reconnect state after a failure-then-success cycle. Replaces
    a former state-invariant test that only set the counter and
    re-asserted it."""

    async def test_reconnect_loop_clears_state_after_failure_then_success(
        self,
    ) -> None:
        """Behavior: ``_reconnect_loop`` records a failed attempt then
        clears state when ``_connect_real`` succeeds.

        Patches ``_connect_real`` to fail on the first call and succeed
        on the second, drives the production ``_reconnect_loop`` method,
        and asserts the recovery code at
        ``src/medre/adapters/lxmf/session.py`` clears
        ``reconnect_attempts`` and ``reconnecting`` after the successful
        reconnect.
        """
        session = LxmfSession(
            config=_make_lxmf_config(),
            adapter_id="lxmf-test",
        )

        call_count = 0

        async def _connect_fail_then_succeed(self: LxmfSession) -> None:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("simulated connect failure")
            # Mimic the success side-effects of the real _connect_real
            # without resetting reconnect_attempts — the loop's own
            # reset (lxmf/session.py:1768) is the behavior under test.
            self._diag.connected = True
            self._diag.router_running = True

        original_sleep = asyncio.sleep

        async def _fast_sleep(delay: float) -> None:
            """Fast no-op for positive backoff delays; yields only for
            non-positive delays.

            Positive delays are skipped entirely (test-speed
            optimization); the reconnect loop's other ``await`` points
            from ``_connect_real`` provide sufficient cooperative yields.
            """
            if delay <= 0:
                await original_sleep(0)

        # _connect_real is awaited by the loop. Patch at the class level
        # so the async function becomes a bound method matching the
        # production call shape.
        with patch.object(
            LxmfSession, "_connect_real", _connect_fail_then_succeed
        ), patch("asyncio.sleep", side_effect=_fast_sleep):
            await session._reconnect_loop()

        # One failure then one success — the failure-then-success cycle ran.
        assert call_count == 2
        # Production loop reset the counter after the successful reconnect.
        assert session._diag.reconnect_attempts == 0
        assert session._diag.reconnecting is False


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
def make_adapter_context() -> Callable[[str], Any]:
    """Create an AdapterContext factory for testing."""
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
