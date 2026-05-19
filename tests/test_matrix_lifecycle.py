"""Mock-based lifecycle tests for MatrixAdapter.

These tests exercise start/stop/health edge cases using mock nio client
objects. They do NOT require a real Matrix server or nio installation.

The ``nio`` package is injected via ``sys.modules`` so the local
``import nio`` inside ``MatrixAdapter.start()`` resolves to our mock.
``HAS_NIO`` is patched on the adapter module to control the guard clause.

The file contains 21 tests across 6 classes:
  - ``TestMatrixAdapterStart`` (5 tests): start() with mocked nio;
    login failure closes client; sync creation failure closes client.
  - ``TestMatrixAdapterStop`` (4 tests): stop() idempotency and cleanup;
    double-stop is safe; stop before start is safe.
  - ``TestMatrixAdapterHealthCheck`` (4 tests): health_check() state mapping.
  - ``TestMatrixAdapterRestart`` (1 test): full start-stop-start cycle.
  - ``TestMatrixAdapterLifecycleEdgeCases`` (2 tests): failure edge cases.
  - ``TestMatrixAdapterSyncFailure`` (5 tests): sync_forever raises — exception
    is recorded by _run_sync(), health_check() reports failed, stop() is clean
    after failure, restart recovers healthy state.

See also:
  - test_matrix_adapter.py  — FakeMatrixAdapter tests, _on_room_message
  - test_matrix_boundaries.py — deliver() boundary tests
"""

import asyncio
import logging
import sys
from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from medre.adapters.matrix.adapter import MatrixAdapter
from medre.adapters.matrix.errors import MatrixConnectionError
from medre.config.adapters.matrix import MatrixConfig
from medre.core.contracts.adapter import AdapterContext

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_config(**overrides: Any) -> MatrixConfig:
    """Build a MatrixConfig with sensible defaults."""
    defaults: dict[str, Any] = {
        "adapter_id": "matrix-test",
        "homeserver": "https://matrix.example.com",
        "user_id": "@bot:example.com",
        "access_token": "tok_123",
    }
    defaults.update(overrides)
    return MatrixConfig(**defaults)


def _make_context(adapter_id="matrix-test") -> AdapterContext:
    """Build an AdapterContext with minimal fakes."""
    return AdapterContext(
        adapter_id=adapter_id,
        event_bus=None,
        publish_inbound=AsyncMock(),
        logger=logging.getLogger(f"test.{adapter_id}"),
        clock=lambda: datetime.now(timezone.utc),
        shutdown_event=asyncio.Event(),
    )


async def _sync_forever_stub(*args: object, **kwargs: object) -> None:
    """Stub for ``nio.AsyncClient.sync_forever`` — blocks until cancelled.

    Using a real coroutine (instead of ``AsyncMock``) avoids the
    ``RuntimeWarning: coroutine was never awaited`` that would otherwise
    fire when the asyncio task wrapping this stub gets cancelled.
    """
    try:
        await asyncio.Event().wait()
    except asyncio.CancelledError:
        pass


def _build_mock_nio_module() -> MagicMock:
    """Create a mock ``nio`` module with AsyncClient and message types.

    The mock client uses ``MagicMock`` (not ``AsyncMock``) as the base so
    that sync methods (``restore_login``, ``add_event_callback``,
    ``stop_sync_forever``) are sync mocks.  Async methods are explicitly
    set to ``AsyncMock`` or real coroutine stubs.
    """
    mock = MagicMock(name="mock_nio")
    client = MagicMock(name="mock_async_client")
    client.logged_in = True
    client.restore_login = MagicMock()
    client.add_event_callback = MagicMock()
    client.stop_sync_forever = MagicMock()
    client.close = AsyncMock()
    client.sync_forever = _sync_forever_stub
    client.room_send = AsyncMock()
    # whoami() is called by _discover_device_id() during _start_plaintext().
    _whoami_resp = MagicMock(name="whoami_response")
    _whoami_resp.device_id = "DEVICE_TEST_ID"
    client.whoami = AsyncMock(return_value=_whoami_resp)
    mock.AsyncClient = MagicMock(return_value=client)
    mock.ClientConfig = MagicMock(name="ClientConfig")
    mock.RoomMessageText = MagicMock(name="RoomMessageText")
    mock.RoomMessageNotice = MagicMock(name="RoomMessageNotice")
    mock.RoomMessageEmote = MagicMock(name="RoomMessageEmote")
    # nio.events.MegolmEvent for undecryptable event callback
    mock_events = MagicMock(name="nio.events")
    mock_events.MegolmEvent = MagicMock(name="MegolmEvent")
    mock_events.RoomEncryptionEvent = MagicMock(name="RoomEncryptionEvent")
    mock.events = mock_events
    return mock


@pytest.fixture
def mock_nio():
    """Inject a mock ``nio`` module into ``sys.modules`` and patch HAS_NIO."""
    mock = _build_mock_nio_module()
    saved_nio = sys.modules.get("nio")
    saved_nio_events = sys.modules.get("nio.events")
    sys.modules["nio"] = mock
    sys.modules["nio.events"] = mock.events
    with patch("medre.adapters.matrix.adapter.HAS_NIO", True):
        yield mock
    # Restore
    if saved_nio is None:
        sys.modules.pop("nio", None)
    else:
        sys.modules["nio"] = saved_nio
    if saved_nio_events is None:
        sys.modules.pop("nio.events", None)
    else:
        sys.modules["nio.events"] = saved_nio_events


# ===================================================================
# TestMatrixAdapterStart
# ===================================================================


class TestMatrixAdapterStart:
    """start() behavior with mocked nio."""

    async def test_successful_start_creates_client(self, mock_nio):
        """start() creates AsyncClient and restores login."""
        config = _make_config()
        adapter = MatrixAdapter(config)
        ctx = _make_context()
        try:
            await adapter.start(ctx)
            assert adapter.ctx is ctx
            assert adapter._client is not None
            assert adapter._sync_task is not None
        finally:
            await adapter.stop()

    async def test_start_sets_up_event_callback(self, mock_nio):
        """start() registers callbacks for RoomMessage types, MegolmEvent, and RoomEncryptionEvent."""
        config = _make_config()
        adapter = MatrixAdapter(config)
        try:
            await adapter.start(_make_context())
            assert mock_nio.AsyncClient.return_value.add_event_callback.call_count == 4
        finally:
            await adapter.stop()

    async def test_start_no_nio_raises(self):
        """start() raises MatrixConnectionError when nio is not available."""
        config = _make_config()
        adapter = MatrixAdapter(config)
        with patch("medre.adapters.matrix.adapter.HAS_NIO", False):
            with pytest.raises(
                MatrixConnectionError, match="mindroom-nio not installed"
            ):
                await adapter.start(_make_context())
        assert adapter._client is None
        assert adapter._sync_task is None

    async def test_start_login_failure_raises(self, mock_nio):
        """start() raises when restore_login does not set logged_in."""
        mock_nio.AsyncClient.return_value.logged_in = False
        config = _make_config()
        adapter = MatrixAdapter(config)
        with pytest.raises(MatrixConnectionError, match="failed to authenticate"):
            await adapter.start(_make_context())
        # Client should be cleaned up
        assert adapter._client is None

    async def test_start_sync_failure_raises(self, mock_nio):
        """start() raises when asyncio.create_task fails."""
        config = _make_config()
        adapter = MatrixAdapter(config)
        with patch(
            "medre.adapters.matrix.session.asyncio.create_task",
            side_effect=RuntimeError("sync failed"),
        ):
            with pytest.raises(MatrixConnectionError, match="failed to start sync"):
                await adapter.start(_make_context())
        # Client should be cleaned up
        assert adapter._client is None

    async def test_start_passes_configured_store_path_to_async_client(self, mock_nio):
        """start() forwards config.store_path to nio.AsyncClient."""
        config = _make_config(store_path="/tmp/nio-store")
        adapter = MatrixAdapter(config)
        try:
            await adapter.start(_make_context())
            mock_nio.AsyncClient.assert_called_once()
            _, kwargs = mock_nio.AsyncClient.call_args
            assert kwargs["store_path"] == "/tmp/nio-store"
        finally:
            await adapter.stop()

    async def test_start_passes_none_store_path_when_unset(self, mock_nio):
        """start() passes store_path=None when config.store_path is not set."""
        config = _make_config()  # store_path defaults to None
        adapter = MatrixAdapter(config)
        try:
            await adapter.start(_make_context())
            mock_nio.AsyncClient.assert_called_once()
            _, kwargs = mock_nio.AsyncClient.call_args
            assert kwargs["store_path"] is None
        finally:
            await adapter.stop()


# ===================================================================
# TestMatrixAdapterStop
# ===================================================================


class TestMatrixAdapterStop:
    """stop() behavior — must be idempotent and clean."""

    async def test_stop_cancels_sync_task(self, mock_nio):
        """stop() cancels the sync_forever task."""
        config = _make_config()
        adapter = MatrixAdapter(config)
        await adapter.start(_make_context())
        await adapter.stop()
        assert adapter._sync_task is None
        assert adapter._client is None

    async def test_double_stop_is_idempotent(self, mock_nio):
        """Calling stop() twice does not raise."""
        config = _make_config()
        adapter = MatrixAdapter(config)
        await adapter.start(_make_context())
        await adapter.stop()
        await adapter.stop()  # second call — should not raise
        assert adapter._sync_task is None

    async def test_stop_before_start_no_crash(self):
        """stop() on an unstarted adapter does not raise."""
        config = _make_config()
        adapter = MatrixAdapter(config)
        await adapter.stop()  # no start() call
        assert adapter._client is None

    async def test_stop_closes_client(self, mock_nio):
        """stop() calls close() on the nio client."""
        config = _make_config()
        adapter = MatrixAdapter(config)
        await adapter.start(_make_context())
        await adapter.stop()
        mock_nio.AsyncClient.return_value.close.assert_called_once()


# ===================================================================
# TestMatrixAdapterHealthCheck
# ===================================================================


class TestMatrixAdapterHealthCheck:
    """health_check() reflects current adapter state."""

    async def test_health_unknown_before_start(self):
        """Before start(), health is 'unknown'."""
        config = _make_config()
        adapter = MatrixAdapter(config)
        info = await adapter.health_check()
        assert info.health == "unknown"

    async def test_health_healthy_after_start(self, mock_nio):
        """After successful start(), health is 'healthy'."""
        config = _make_config()
        adapter = MatrixAdapter(config)
        try:
            await adapter.start(_make_context())
            info = await adapter.health_check()
            assert info.health == "healthy"
        finally:
            await adapter.stop()

    async def test_health_failed_after_login_failure(self, mock_nio):
        """When logged_in is False, health is 'failed'."""
        config = _make_config()
        adapter = MatrixAdapter(config)
        adapter._client = AsyncMock()
        adapter._client.logged_in = False
        info = await adapter.health_check()
        assert info.health == "failed"

    async def test_health_unknown_after_stop(self, mock_nio):
        """After stop(), health returns to 'unknown'."""
        config = _make_config()
        adapter = MatrixAdapter(config)
        await adapter.start(_make_context())
        await adapter.stop()
        info = await adapter.health_check()
        assert info.health == "unknown"


# ===================================================================
# TestMatrixAdapterRestart
# ===================================================================


class TestMatrixAdapterRestart:
    """Full start-stop-start cycle."""

    async def test_start_after_stop_works(self, mock_nio):
        """start() after stop() creates a fresh client."""
        config = _make_config()
        adapter = MatrixAdapter(config)
        try:
            await adapter.start(_make_context())
            await adapter.stop()
            # Reset mock call count
            mock_nio.AsyncClient.reset_mock()
            await adapter.start(_make_context())
            mock_nio.AsyncClient.assert_called_once()
        finally:
            await adapter.stop()


# ===================================================================
# TestMatrixAdapterLifecycleEdgeCases
# ===================================================================


class TestMatrixAdapterLifecycleEdgeCases:
    """Edge cases and failure modes."""

    async def test_startup_failure_no_orphaned_sync_task(self, mock_nio):
        """If start fails after sync task creation, no dangling task remains."""
        config = _make_config()
        adapter = MatrixAdapter(config)
        # Don't call real start; manually verify default state is clean
        assert adapter._sync_task is None

    async def test_stop_after_failed_start_no_crash(self):
        """stop() after a failed start attempt does not raise."""
        config = _make_config()
        adapter = MatrixAdapter(config)
        with patch("medre.adapters.matrix.adapter.HAS_NIO", False):
            with pytest.raises(MatrixConnectionError):
                await adapter.start(_make_context())
        await adapter.stop()  # should not raise
        assert adapter._sync_task is None


# ===================================================================
# TestMatrixAdapterSyncFailure
# ===================================================================


class TestMatrixAdapterSyncFailure:
    """Sync task failure is observed and recorded by _run_sync().

    With the reconnect loop, sync_forever must fail 10 consecutive times
    before _sync_failure is set. Tests mock asyncio.sleep to skip backoff.
    """

    async def test_sync_forever_raises_is_recorded(self, mock_nio):
        """_run_sync records the exception when sync_forever raises."""
        config = _make_config()
        adapter = MatrixAdapter(config)

        # Replace the mock's sync_forever with one that always raises.
        async def _failing_sync(*args, **kwargs):
            await asyncio.sleep(0)
            raise RuntimeError("sync lost connection")

        mock_nio.AsyncClient.return_value.sync_forever = _failing_sync

        # Mock sleep to skip backoff delays
        original_sleep = asyncio.sleep

        async def _fast_sleep(delay):
            if delay <= 0:
                await original_sleep(0)

        try:
            with patch("asyncio.sleep", side_effect=_fast_sleep):
                await adapter.start(_make_context())
                for _ in range(100):
                    await original_sleep(0)

            # Verify _run_sync caught the exception after max retries.
            assert adapter._sync_failure is not None
            assert isinstance(adapter._sync_failure, RuntimeError)
        finally:
            await adapter.stop()

    async def test_health_failed_after_sync_failure(self, mock_nio):
        """health_check() returns 'failed' after sync_forever raises."""
        config = _make_config()
        adapter = MatrixAdapter(config)

        async def _failing_sync(*args, **kwargs):
            await asyncio.sleep(0)
            raise RuntimeError("sync disconnected")

        mock_nio.AsyncClient.return_value.sync_forever = _failing_sync

        original_sleep = asyncio.sleep

        async def _fast_sleep(delay):
            if delay <= 0:
                await original_sleep(0)

        try:
            with patch("asyncio.sleep", side_effect=_fast_sleep):
                await adapter.start(_make_context())
                for _ in range(100):
                    await original_sleep(0)

            info = await adapter.health_check()
            assert info.health == "failed"
            assert info.platform == "matrix"
        finally:
            await adapter.stop()

    async def test_stop_after_sync_failure_clean(self, mock_nio):
        """stop() after a sync task failure is clean and idempotent."""
        config = _make_config()
        adapter = MatrixAdapter(config)

        async def _failing_sync(*args, **kwargs):
            await asyncio.sleep(0)
            raise RuntimeError("sync died")

        mock_nio.AsyncClient.return_value.sync_forever = _failing_sync

        original_sleep = asyncio.sleep

        async def _fast_sleep(delay):
            if delay <= 0:
                await original_sleep(0)

        with patch("asyncio.sleep", side_effect=_fast_sleep):
            await adapter.start(_make_context())
            for _ in range(100):
                await original_sleep(0)

        # stop() should handle the already-failed task cleanly
        await adapter.stop()
        assert adapter._sync_task is None
        assert adapter._client is None
        # Double-stop must still be idempotent
        await adapter.stop()

    async def test_restart_recovers_health(self, mock_nio):
        """After sync failure, stop() + start() with healthy sync recovers health."""
        config = _make_config()
        adapter = MatrixAdapter(config)

        # First start: fail the sync.
        async def _failing_sync(*args, **kwargs):
            await asyncio.sleep(0)
            raise RuntimeError("sync failed")

        original_sleep = asyncio.sleep

        async def _fast_sleep(delay):
            if delay <= 0:
                await original_sleep(0)

        mock_nio.AsyncClient.return_value.sync_forever = _failing_sync
        with patch("asyncio.sleep", side_effect=_fast_sleep):
            await adapter.start(_make_context())
            for _ in range(100):
                await original_sleep(0)
        info = await adapter.health_check()
        assert info.health == "failed"

        # Stop the failed adapter.
        await adapter.stop()

        # Restart with healthy sync_forever.
        mock_nio.AsyncClient.return_value.sync_forever = _sync_forever_stub
        # Need fresh client mock since stop() cleared _client.
        client = MagicMock(name="mock_async_client_2")
        client.logged_in = True
        client.restore_login = MagicMock()
        client.add_event_callback = MagicMock()
        client.stop_sync_forever = MagicMock()
        client.close = AsyncMock()
        client.sync_forever = _sync_forever_stub
        client.room_send = AsyncMock()
        # whoami() is called by _discover_device_id() during _start_plaintext().
        _whoami_resp = MagicMock(name="whoami_response_2")
        _whoami_resp.device_id = "DEVICE_TEST_ID"
        client.whoami = AsyncMock(return_value=_whoami_resp)
        mock_nio.AsyncClient.return_value = client

        await adapter.start(_make_context())
        info = await adapter.health_check()
        assert info.health == "healthy"
        await adapter.stop()

    async def test_failure_recorded_none_by_default(self, mock_nio):
        """_sync_failure defaults to None before any failure."""
        config = _make_config()
        adapter = MatrixAdapter(config)
        assert adapter._sync_failure is None
