"""Matrix adapter health lifecycle parity and stale-sync degraded tests.

Tests the following lifecycle rules:

1. ``diagnostics()["health"]`` is ``None`` before the first ``health_check()``
   call in each lifecycle (fresh, post-start, post-stop, post-restart).
2. After ``health_check()`` the value is a valid health string.
3. ``stop()`` clears the cached health back to ``None``.
4. ``start()`` clears the cached health back to ``None``.
5. ``health_check()`` reports ``"degraded"`` when last successful sync
   is older than ``_SYNC_STALE_THRESHOLD_SECONDS`` (but ``None``,
   meaning no sync completed yet, does **not** trigger degraded).
6. ``health_check()`` reports ``"healthy"`` when last successful sync
   is recent, or when it is ``None`` (first sync not yet completed).
7. The clock used for stale detection is fakeable via ``adapter._clock``.

No network access, no nio dependency.  All tests use mock sessions.

See also:
  - test_matrix_lifecycle.py  — broader start/stop/health mock-nio tests
  - test_matrix_adapter.py   — FakeMatrixAdapter and inbound tests
"""

from __future__ import annotations

import asyncio
import logging
import sys
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from medre.adapters.matrix.adapter import (
    _SYNC_STALE_THRESHOLD_SECONDS,
    MatrixAdapter,
)
from medre.config.adapters.matrix import MatrixConfig
from medre.core.contracts.adapter import AdapterContext

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_MONO_BASE: float = 1000.0


def _make_config(**overrides: Any) -> MatrixConfig:
    """Build a MatrixConfig with sensible defaults."""
    defaults: dict[str, Any] = {
        "adapter_id": "matrix-health-test",
        "homeserver": "https://matrix.example.com",
        "user_id": "@bot:example.com",
        "access_token": "tok_health",
    }
    defaults.update(overrides)
    return MatrixConfig(**defaults)


def _make_context(adapter_id: str = "matrix-health-test") -> AdapterContext:
    """Build an AdapterContext with minimal fakes."""
    return AdapterContext(
        adapter_id=adapter_id,
        event_bus=None,
        publish_inbound=AsyncMock(),
        logger=logging.getLogger(f"test.{adapter_id}"),
        clock=lambda: datetime.now(timezone.utc),
        shutdown_event=asyncio.Event(),
    )


def _build_mock_nio_module() -> MagicMock:
    """Create a mock ``nio`` module with AsyncClient and message types."""
    mock = MagicMock(name="mock_nio")
    client = MagicMock(name="mock_async_client")
    client.logged_in = True
    client.restore_login = MagicMock()
    client.add_event_callback = MagicMock()
    client.stop_sync_forever = MagicMock()
    client.close = AsyncMock()

    # Healthy sync stub — yields then returns next_batch.
    async def _healthy_sync(*args: object, **kwargs: object) -> SimpleNamespace:
        await asyncio.sleep(0)
        return SimpleNamespace(next_batch="batch_token")

    client.sync = _healthy_sync
    client.room_send = AsyncMock()
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


def _make_mock_session(
    *,
    connected: bool = True,
    logged_in: bool = True,
    last_sync_error: Exception | None = None,
    last_successful_sync: float | None = None,
) -> MagicMock:
    """Build a mock MatrixSession with configurable state."""
    session = MagicMock(name="mock_session")
    session.connected = connected
    session.last_sync_error = last_sync_error
    session.last_successful_sync = last_successful_sync
    session.is_logged_in.return_value = logged_in
    return session


# ===================================================================
# Health lifecycle parity: diagnostics health is None before first check
# ===================================================================


async def test_diagnostics_health_none_before_first_check():
    """diagnostics()['health'] is None before any health_check() call."""
    config = _make_config()
    adapter = MatrixAdapter(config)
    diag = adapter.diagnostics()
    assert diag["health"] is None


async def test_diagnostics_health_set_after_check():
    """After health_check(), diagnostics()['health'] is a string."""
    config = _make_config()
    adapter = MatrixAdapter(config)
    info = await adapter.health_check()
    assert isinstance(info.health, str)
    assert info.health == "unknown"  # no session → unknown
    # diagnostics mirrors it
    diag = adapter.diagnostics()
    assert diag["health"] == "unknown"


async def test_diagnostics_health_none_after_stop():
    """stop() clears _last_health so diagnostics shows None."""
    config = _make_config()
    adapter = MatrixAdapter(config)
    # Simulate: set _last_health, then stop
    adapter._last_health = "healthy"
    await adapter.stop()
    assert adapter._last_health is None
    diag = adapter.diagnostics()
    assert diag["health"] is None


async def test_diagnostics_health_none_after_start(mock_nio):
    """start() clears _last_health so diagnostics shows None even if set before."""
    config = _make_config()
    adapter = MatrixAdapter(config)
    adapter._last_health = "failed"
    await adapter.start(_make_context())
    try:
        assert adapter._last_health is None
        diag = adapter.diagnostics()
        assert diag["health"] is None
    finally:
        await adapter.stop()


async def test_restart_clears_health(mock_nio):
    """Full start → health_check → stop → start cycle clears health."""
    config = _make_config()
    adapter = MatrixAdapter(config)
    await adapter.start(_make_context())
    try:
        info = await adapter.health_check()
        assert info.health in ("healthy", "degraded", "unknown", "failed")
        assert adapter._last_health is not None

        await adapter.stop()
        assert adapter._last_health is None

        # Restart with fresh mock client
        client = MagicMock(name="mock_async_client_2")
        client.logged_in = True
        client.restore_login = MagicMock()
        client.add_event_callback = MagicMock()
        client.stop_sync_forever = MagicMock()
        client.close = AsyncMock()

        async def _sync2(*a: object, **kw: object) -> SimpleNamespace:
            await asyncio.sleep(0)
            return SimpleNamespace(next_batch="batch_2")

        client.sync = _sync2
        client.room_send = AsyncMock()
        _whoami2 = MagicMock(name="whoami_2")
        _whoami2.device_id = "DEVICE_2"
        client.whoami = AsyncMock(return_value=_whoami2)
        mock_nio.AsyncClient.return_value = client

        await adapter.start(_make_context())
        assert adapter._last_health is None
    finally:
        await adapter.stop()


# ===================================================================
# Stale sync watchdog
# ===================================================================


async def test_stale_sync_reports_degraded():
    """health_check reports 'degraded' when last_successful_sync is stale."""
    config = _make_config()
    adapter = MatrixAdapter(config)
    now = _MONO_BASE + 100.0
    adapter._clock = lambda: now
    # Session is connected+logged_in, but last sync was long ago.
    stale_time = now - _SYNC_STALE_THRESHOLD_SECONDS - 10.0
    adapter._session = _make_mock_session(
        connected=True,
        logged_in=True,
        last_successful_sync=stale_time,
    )
    info = await adapter.health_check()
    assert info.health == "degraded"


async def test_fresh_sync_reports_healthy():
    """health_check reports 'healthy' when last_successful_sync is recent."""
    config = _make_config()
    adapter = MatrixAdapter(config)
    now = _MONO_BASE + 100.0
    adapter._clock = lambda: now
    fresh_time = now - 10.0  # well within threshold
    adapter._session = _make_mock_session(
        connected=True,
        logged_in=True,
        last_successful_sync=fresh_time,
    )
    info = await adapter.health_check()
    assert info.health == "healthy"


async def test_no_sync_yet_preserves_healthy():
    """health_check preserves 'healthy' when last_successful_sync is None.

    ``None`` means the first sync loop has not completed yet — the
    adapter just started and is connected/logged-in, so it should not
    be penalised as stale.  Only a real (non-None) timestamp older
    than the threshold should trigger ``degraded``.
    """
    config = _make_config()
    adapter = MatrixAdapter(config)
    adapter._clock = lambda: _MONO_BASE
    adapter._session = _make_mock_session(
        connected=True,
        logged_in=True,
        last_successful_sync=None,
    )
    info = await adapter.health_check()
    assert info.health == "healthy"


async def test_stale_does_not_override_failed():
    """Stale sync does not override 'failed' — failure takes priority."""
    config = _make_config()
    adapter = MatrixAdapter(config)
    adapter._clock = lambda: _MONO_BASE
    adapter._session = _make_mock_session(
        connected=True,
        logged_in=True,
        last_sync_error=RuntimeError("sync broken"),
        last_successful_sync=_MONO_BASE - 1000.0,
    )
    info = await adapter.health_check()
    assert info.health == "failed"


async def test_stale_does_not_override_unknown():
    """Stale sync does not override 'unknown' (no session)."""
    config = _make_config()
    adapter = MatrixAdapter(config)
    adapter._clock = lambda: _MONO_BASE
    adapter._session = None
    info = await adapter.health_check()
    assert info.health == "unknown"


# ===================================================================
# Fakeable clock
# ===================================================================


async def test_fakeable_clock_controls_degradation():
    """Advancing the fake clock past the threshold causes degradation."""
    config = _make_config()
    adapter = MatrixAdapter(config)

    sync_time = _MONO_BASE
    adapter._session = _make_mock_session(
        connected=True,
        logged_in=True,
        last_successful_sync=sync_time,
    )

    # Fresh: clock close to sync time → healthy
    adapter._clock = lambda: sync_time + 10.0
    info = await adapter.health_check()
    assert info.health == "healthy"

    # Advance clock past threshold → degraded
    adapter._clock = lambda: sync_time + _SYNC_STALE_THRESHOLD_SECONDS + 1.0
    info = await adapter.health_check()
    assert info.health == "degraded"


# ===================================================================
# Diagnostics JSON safety
# ===================================================================


async def test_diagnostics_json_safe():
    """All diagnostics values are JSON-safe (no secrets, no non-serializable)."""
    import json
    from types import SimpleNamespace

    config = _make_config()
    adapter = MatrixAdapter(config)
    # Build a mock session whose diagnostics() returns a proper
    # SimpleNamespace with JSON-safe values (no MagicMock leakage).
    mock_session = MagicMock()
    mock_session.connected = True
    mock_session.last_sync_error = None
    mock_session.last_successful_sync = 100.0
    mock_session.is_logged_in.return_value = True
    mock_session.diagnostics.return_value = SimpleNamespace(
        connected=True,
        logged_in=True,
        sync_task_running=True,
        last_sync_error=None,
        store_path_configured=False,
        device_id_configured=False,
        encryption_mode="plaintext",
        crypto_enabled=False,
        last_crypto_error=None,
        encrypted_room_seen=False,
        undecryptable_event_count=0,
        sync_running=True,
        reconnecting=False,
        reconnect_attempts=0,
        last_successful_sync=100.0,
        crypto_store_loaded=False,
        olm_loaded=False,
        store_loaded=False,
        device_keys_uploaded=False,
        key_query_needed=False,
        device_id_in_use=None,
        store_path_exists=False,
        initial_sync_completed=False,
        encrypted_room_count=0,
        plaintext_room_count=0,
    )
    adapter._session = mock_session
    # Trigger health_check so _last_health is set
    await adapter.health_check()
    diag = adapter.diagnostics()
    # Must survive a real JSON round-trip without default=str fallback.
    # This proves all values are genuinely JSON-serializable.
    serialized = json.dumps(diag)
    assert isinstance(serialized, str)
    round_tripped = json.loads(serialized)
    assert isinstance(round_tripped, dict)
    # health must be a string or None (both JSON-safe)
    assert diag["health"] is None or isinstance(diag["health"], str)
