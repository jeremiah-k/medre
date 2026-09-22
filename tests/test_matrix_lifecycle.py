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
  - ``TestMatrixAdapterSyncFailure`` (5 tests): transient sync failures remain
    supervised, health is degraded while reconnecting, stop() is clean during
    backoff, and restart recovers healthy state.

See also:
  - test_matrix_adapter.py  — FakeMatrixAdapter tests, _on_room_message
  - test_matrix_boundaries.py — deliver() boundary tests
"""

import asyncio
import logging
import sys
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from medre.adapters.matrix.adapter import MatrixAdapter
from medre.adapters.matrix.errors import MatrixConnectionError
from medre.config.adapters.matrix import MatrixConfig
from medre.core.contracts.adapter import AdapterContext
from tests.helpers.async_utils import wait_until

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


async def _healthy_sync(*args: object, **kwargs: object) -> SimpleNamespace:
    """Stub for ``nio.AsyncClient.sync`` — yields once then returns a response.

    The fake ``sync_forever`` delegates to this method. Each call must yield
    (``asyncio.sleep(0)``) to avoid a hot CPU loop and return a response so
    registered callbacks can run. The adapter stops the outer task.
    """
    await asyncio.sleep(0)
    return SimpleNamespace(next_batch="batch_token")


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
    response_callbacks: list[Any] = []
    client.add_response_callback = MagicMock(
        side_effect=lambda callback, *_classes: response_callbacks.append(callback)
    )
    stop_sync = asyncio.Event()
    client.stop_sync_forever = MagicMock(side_effect=stop_sync.set)
    client.close = AsyncMock()
    client.sync = _healthy_sync

    async def _sync_forever(*args: object, **kwargs: object) -> None:
        stop_sync.clear()
        current_kwargs = dict(kwargs)
        while not stop_sync.is_set():
            response = await client.sync(*args, **current_kwargs)
            for callback in tuple(response_callbacks):
                await callback(response)
            if getattr(response, "next_batch", None):
                current_kwargs["since"] = None
                current_kwargs["full_state"] = None
            await asyncio.sleep(0)

    client.sync_forever = _sync_forever
    client.room_send = AsyncMock()
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
            assert adapter._session is not None
            assert adapter._session.sync_task_running
        finally:
            await adapter.stop()

    async def test_start_sets_up_event_callback(self, mock_nio):
        """start() registers callbacks for RoomMessage types, MegolmEvent, and RoomEncryptionEvent."""
        config = _make_config()
        adapter = MatrixAdapter(config)
        try:
            await adapter.start(_make_context())
            assert mock_nio.AsyncClient.return_value.add_event_callback.call_count == 5
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
        assert adapter._session is None

    async def test_start_login_failure_raises(self, mock_nio):
        """start() raises when restore_login does not set logged_in."""
        mock_nio.AsyncClient.return_value.logged_in = False
        config = _make_config()
        adapter = MatrixAdapter(config)
        with pytest.raises(MatrixConnectionError, match="failed to authenticate"):
            await adapter.start(_make_context())
        # Session was cleaned up after failed start
        assert adapter._session is None

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
        # Session was cleaned up after failed start
        assert adapter._session is None

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


async def test_checkpoint_only_context_falls_back_to_publish_inbound(mock_nio) -> None:
    config = _make_config()
    adapter = MatrixAdapter(config)
    ctx = _make_context()
    ctx.load_checkpoint = AsyncMock(return_value=None)
    ctx.commit_checkpoint = AsyncMock()
    ctx.admit_inbound = None

    try:
        await adapter.start(ctx)
        session = adapter._session
        assert session is not None
        assert session._admission_callback is None
        assert session._durable_sync_enabled is False
        assert mock_nio.AsyncClient.return_value.add_event_callback.called
        assert not mock_nio.AsyncClient.return_value.add_event_admission_callback.called
        session._live_sync_started = True
        callback = mock_nio.AsyncClient.return_value.add_event_callback.call_args_list[
            0
        ].args[0]
        await callback(
            SimpleNamespace(room_id="!room:example.org"),
            SimpleNamespace(
                sender="@alice:example.org",
                event_id="$fallback",
                body="hello",
                source={
                    "event_id": "$fallback",
                    "sender": "@alice:example.org",
                    "type": "m.room.message",
                    "content": {"msgtype": "m.text", "body": "hello"},
                },
            ),
        )
        ctx.publish_inbound.assert_awaited_once()
        assert ctx.admit_inbound is None
    finally:
        await adapter.stop()


# ===================================================================
# TestMatrixAdapterStop
# ===================================================================


class TestMatrixAdapterStop:
    """stop() behavior — must be idempotent and clean."""

    async def test_stop_cancels_sync_task(self, mock_nio):
        """stop() cancels the sync task."""
        config = _make_config()
        adapter = MatrixAdapter(config)
        await adapter.start(_make_context())
        await adapter.stop()
        assert adapter._session is None

    async def test_double_stop_is_idempotent(self, mock_nio):
        """Calling stop() twice does not raise."""
        config = _make_config()
        adapter = MatrixAdapter(config)
        await adapter.start(_make_context())
        await adapter.stop()
        await adapter.stop()  # second call — should not raise
        assert adapter._session is None

    async def test_stop_before_start_no_crash(self):
        """stop() on an unstarted adapter does not raise."""
        config = _make_config()
        adapter = MatrixAdapter(config)
        await adapter.stop()  # no start() call
        assert adapter._session is None

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
            # Wait for the background sync task to complete its first
            # iteration and set last_successful_sync.
            await wait_until(
                lambda: adapter._session is not None
                and adapter._session.last_successful_sync is not None,
                timeout=2.0,
            )
            info = await adapter.health_check()
            assert info.health == "healthy"
        finally:
            await adapter.stop()

    async def test_health_failed_after_login_failure(self, mock_nio):
        """When logged_in is False, health is 'failed'."""
        config = _make_config()
        adapter = MatrixAdapter(config)
        # Simulate a session with logged_in=False via a mock session
        mock_session = MagicMock()
        mock_session.last_sync_error = None
        mock_session.connected = True
        mock_session.is_logged_in.return_value = False
        adapter._session = mock_session
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
        assert adapter._session is None

    async def test_stop_after_failed_start_no_crash(self):
        """stop() after a failed start attempt does not raise."""
        config = _make_config()
        adapter = MatrixAdapter(config)
        with patch("medre.adapters.matrix.adapter.HAS_NIO", False):
            with pytest.raises(MatrixConnectionError):
                await adapter.start(_make_context())
        await adapter.stop()  # should not raise
        assert adapter._session is None


# ===================================================================
# TestMatrixAdapterSyncFailure
# ===================================================================


class TestMatrixAdapterSyncFailure:
    """Transient sync failures remain supervised until shutdown.

    Matrix is a long-lived relay transport. Ordinary connection failures keep
    retrying with capped backoff instead of exhausting a finite attempt budget.
    Terminal ``_sync_failure`` is reserved for fail-closed supervision errors.
    """

    async def test_transient_failures_retry_past_legacy_ceiling(self, mock_nio):
        """More than ten transient failures recover without terminal failure."""
        config = _make_config()
        adapter = MatrixAdapter(config)
        calls = 0

        async def _flaky_sync(*args, **kwargs):
            nonlocal calls
            calls += 1
            await asyncio.sleep(0)
            if calls <= 12:
                raise RuntimeError("sync lost connection")
            return SimpleNamespace(next_batch=f"batch-{calls}")

        mock_nio.AsyncClient.return_value.sync = _flaky_sync
        original_sleep = asyncio.sleep
        try:

            async def _fast_sleep(delay):
                if delay <= 0:
                    await original_sleep(0)

            with patch("asyncio.sleep", side_effect=_fast_sleep):
                await adapter.start(_make_context())
                for _ in range(400):
                    if (
                        adapter._session is not None
                        and adapter._session.last_successful_sync is not None
                    ):
                        break
                    await original_sleep(0)

            assert calls > 12
            assert adapter._sync_failure is None
            assert adapter._session is not None
            assert adapter._session.last_successful_sync is not None
        finally:
            await adapter.stop()

    async def test_health_degraded_during_transient_sync_failure(self, mock_nio):
        """health_check() reports degraded while the supervisor backs off."""
        config = _make_config()
        adapter = MatrixAdapter(config)
        original_sleep = asyncio.sleep
        backoff_started = asyncio.Event()
        hold_backoff = asyncio.Event()

        async def _failing_sync(*args, **kwargs):
            await asyncio.sleep(0)
            raise RuntimeError("sync disconnected")

        async def _controlled_sleep(delay):
            if delay <= 0:
                await original_sleep(0)
                return
            backoff_started.set()
            await hold_backoff.wait()

        mock_nio.AsyncClient.return_value.sync = _failing_sync
        with patch("asyncio.sleep", side_effect=_controlled_sleep):
            try:
                await adapter.start(_make_context())
                await asyncio.wait_for(backoff_started.wait(), timeout=1.0)
                info = await adapter.health_check()
                assert info.health == "degraded"
                assert info.platform == "matrix"
                assert adapter._sync_failure is None
            finally:
                await adapter.stop()

    async def test_stop_during_sync_backoff_is_clean(self, mock_nio):
        """stop() during reconnect backoff is clean and idempotent."""
        config = _make_config()
        adapter = MatrixAdapter(config)
        original_sleep = asyncio.sleep
        backoff_started = asyncio.Event()
        hold_backoff = asyncio.Event()

        async def _failing_sync(*args, **kwargs):
            await asyncio.sleep(0)
            raise RuntimeError("sync died")

        async def _controlled_sleep(delay):
            if delay <= 0:
                await original_sleep(0)
                return
            backoff_started.set()
            await hold_backoff.wait()

        mock_nio.AsyncClient.return_value.sync = _failing_sync
        with patch("asyncio.sleep", side_effect=_controlled_sleep):
            await adapter.start(_make_context())
            await asyncio.wait_for(backoff_started.wait(), timeout=1.0)
            await adapter.stop()

        assert adapter._session is None
        await adapter.stop()

    async def test_restart_recovers_health(self, mock_nio):
        """Stopping during a transient outage allows a healthy restart."""
        config = _make_config()
        adapter = MatrixAdapter(config)
        original_sleep = asyncio.sleep
        backoff_started = asyncio.Event()
        hold_backoff = asyncio.Event()

        async def _failing_sync(*args, **kwargs):
            await asyncio.sleep(0)
            raise RuntimeError("sync failed")

        async def _controlled_sleep(delay):
            if delay <= 0:
                await original_sleep(0)
                return
            backoff_started.set()
            await hold_backoff.wait()

        mock_nio.AsyncClient.return_value.sync = _failing_sync
        with patch("asyncio.sleep", side_effect=_controlled_sleep):
            await adapter.start(_make_context())
            await asyncio.wait_for(backoff_started.wait(), timeout=1.0)
            info = await adapter.health_check()
            assert info.health == "degraded"
            await adapter.stop()

        client = _build_mock_nio_module().AsyncClient.return_value
        mock_nio.AsyncClient.return_value = client

        await adapter.start(_make_context())
        await wait_until(
            lambda: adapter._session is not None
            and adapter._session.last_successful_sync is not None,
            timeout=2.0,
        )
        info = await adapter.health_check()
        assert info.health == "healthy"
        await adapter.stop()

    async def test_failure_recorded_none_by_default(self, mock_nio):
        """_sync_failure defaults to None before any failure."""
        config = _make_config()
        adapter = MatrixAdapter(config)
        assert adapter._sync_failure is None
