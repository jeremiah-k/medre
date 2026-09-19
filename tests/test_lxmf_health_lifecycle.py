"""LXMF adapter health lifecycle parity tests.

Verifies the phase rule: health is None before first health_check of a
lifecycle; health clears on start, stop, and restart boundaries.

The health string describes LOCAL session/router liveness only.  It
must track the owned session's observable lifecycle: a started adapter
whose local session has been torn down reports "failed", never
"healthy".  Peer reachability is unobservable without sending traffic
and is reported separately in diagnostics as an explicit "unknown".

Tests cover:
- diagnostics()["health"] is None before first health_check
- health_check populates _last_health
- stop() clears health to None
- start() clears health to None (restart boundary)
- Health after stop is "unknown" (not cached from prior session)
- Started adapter with a stopped local session reports "failed"
- diagnostics expose health_scope and peer_reachability truthfully
"""

from __future__ import annotations

from typing import Any

from medre.adapters.lxmf.adapter import LxmfAdapter
from medre.config.adapters.lxmf import LxmfConfig


def _make_config(**overrides: Any) -> LxmfConfig:
    defaults: dict[str, Any] = dict(adapter_id="lxmf-health-test")
    defaults.update(overrides)
    return LxmfConfig(**defaults)


# ===================================================================
# Health is None before first health_check of a lifecycle
# ===================================================================


async def test_diagnostics_health_none_before_first_check() -> None:
    """diagnostics()["health"] is None before health_check is called."""
    config = _make_config(connection_type="fake")
    adapter = LxmfAdapter(config)

    diag = adapter.diagnostics()
    assert diag["health"] is None


async def test_diagnostics_health_none_before_start() -> None:
    """Health is None even before start when no health_check called."""
    config = _make_config(connection_type="fake")
    adapter = LxmfAdapter(config)
    assert adapter._last_health is None
    assert adapter.diagnostics()["health"] is None


# ===================================================================
# Health check populates _last_health
# ===================================================================


async def test_health_check_populates_last_health(
    make_adapter_context: Any,
) -> None:
    """After health_check, _last_health is set and diagnostics shows it."""
    config = _make_config(connection_type="fake")
    adapter = LxmfAdapter(config)
    ctx = make_adapter_context("lxmf-health-test")
    await adapter.start(ctx)

    assert adapter._last_health is None  # Not yet checked

    info = await adapter.health_check()
    assert info.health == "healthy"
    assert adapter._last_health == "healthy"
    assert adapter.diagnostics()["health"] == "healthy"

    await adapter.stop()


# ===================================================================
# Health clears on stop
# ===================================================================


async def test_health_clears_on_stop(make_adapter_context: Any) -> None:
    """After stop(), _last_health is None and diagnostics shows None."""
    config = _make_config(connection_type="fake")
    adapter = LxmfAdapter(config)
    ctx = make_adapter_context("lxmf-health-test")
    await adapter.start(ctx)

    # Populate health
    await adapter.health_check()
    assert adapter._last_health is not None

    await adapter.stop()

    assert adapter._last_health is None
    assert adapter.diagnostics()["health"] is None


# ===================================================================
# Health clears on start (restart boundary)
# ===================================================================


async def test_health_clears_on_start_restart(
    make_adapter_context: Any,
) -> None:
    """start() clears stale _last_health from a prior session."""
    config = _make_config(connection_type="fake")
    adapter = LxmfAdapter(config)
    ctx = make_adapter_context("lxmf-health-test")

    # Simulate stale health from prior session
    adapter._last_health = "failed"

    await adapter.start(ctx)
    assert adapter._last_health is None

    await adapter.stop()


# ===================================================================
# Full restart cycle: health None → healthy → None → None → healthy
# ===================================================================


async def test_health_through_full_restart_cycle(
    make_adapter_context: Any,
) -> None:
    """Health follows the phase rule through a full restart cycle.

    Sequence: create → start → check → stop → start → check → stop
    """
    config = _make_config(connection_type="fake")
    adapter = LxmfAdapter(config)
    ctx = make_adapter_context("lxmf-health-test")

    # Before start: health is None
    assert adapter.diagnostics()["health"] is None

    # Start: health cleared
    await adapter.start(ctx)
    assert adapter._last_health is None

    # Check: health populated
    info = await adapter.health_check()
    assert info.health == "healthy"
    assert adapter.diagnostics()["health"] == "healthy"

    # Stop: health cleared
    await adapter.stop()
    assert adapter._last_health is None
    assert adapter.diagnostics()["health"] is None

    # Restart: health cleared
    await adapter.start(ctx)
    assert adapter._last_health is None

    # Check again: health populated fresh
    info = await adapter.health_check()
    assert info.health == "healthy"

    await adapter.stop()


# ===================================================================
# Health before first check after start returns unknown via health_check
# ===================================================================


async def test_health_check_before_start_returns_unknown() -> None:
    """health_check() on a never-started adapter returns unknown."""
    config = _make_config(connection_type="fake")
    adapter = LxmfAdapter(config)

    info = await adapter.health_check()
    assert info.health == "unknown"
    # Diagnostics cache is now populated
    assert adapter.diagnostics()["health"] == "unknown"


# ===================================================================
# Health after stop returns unknown via health_check
# ===================================================================


async def test_health_check_after_stop_returns_unknown(
    make_adapter_context: Any,
) -> None:
    """health_check() after stop returns unknown, not cached value."""
    config = _make_config(connection_type="fake")
    adapter = LxmfAdapter(config)
    ctx = make_adapter_context("lxmf-health-test")

    await adapter.start(ctx)
    await adapter.health_check()
    assert adapter._last_health == "healthy"

    await adapter.stop()

    # health_check after stop computes fresh — should be unknown
    info = await adapter.health_check()
    assert info.health == "unknown"


# ===================================================================
# Failed state: local session torn down under a started adapter
# ===================================================================


async def test_health_failed_when_session_stopped_under_started_adapter(
    make_adapter_context: Any,
) -> None:
    """A started adapter whose local session was stopped reports "failed".

    Guards the honest health contract: the started flag alone must
    never be presented as local router health.  Stopping the owned
    session through its public API is the observable lifecycle failure.
    """
    config = _make_config(connection_type="fake")
    adapter = LxmfAdapter(config)
    ctx = make_adapter_context("lxmf-health-test")
    await adapter.start(ctx)

    info = await adapter.health_check()
    assert info.health == "healthy"

    await adapter.session.stop()
    assert adapter.session.connected is False
    assert adapter.session.router_running is False

    info = await adapter.health_check()
    assert info.health == "failed"
    assert adapter.diagnostics()["health"] == "failed"

    await adapter.stop()


# ===================================================================
# Diagnostics scope: local-only health, explicit unknown reachability
# ===================================================================


async def test_diagnostics_scope_and_peer_reachability_across_lifecycle(
    make_adapter_context: Any,
) -> None:
    """health_scope and peer_reachability are truthful at every phase.

    Even while the local session is healthy, peer reachability must
    stay "unknown": the pinned SDK exposes no supported peer-liveness
    API and MEDRE never probes merely to answer health.
    """
    config = _make_config(connection_type="fake")
    adapter = LxmfAdapter(config)

    diag = adapter.diagnostics()
    assert diag["health_scope"] == "local_session_and_router"
    assert diag["peer_reachability"] == "unknown"

    ctx = make_adapter_context("lxmf-health-test")
    await adapter.start(ctx)
    info = await adapter.health_check()
    assert info.health == "healthy"

    diag = adapter.diagnostics()
    assert diag["health_scope"] == "local_session_and_router"
    assert diag["peer_reachability"] == "unknown"
    assert diag["session"]["connected"] is True
    assert diag["session"]["router_running"] is True

    await adapter.session.stop()
    info = await adapter.health_check()
    assert info.health == "failed"
    diag = adapter.diagnostics()
    assert diag["health"] == "failed"
    assert diag["health_scope"] == "local_session_and_router"
    assert diag["peer_reachability"] == "unknown"
    assert diag["session"]["connected"] is False

    await adapter.stop()
