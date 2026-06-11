"""LXMF periodic announce lifecycle tests.

Tests for announce loop behaviour: fake-mode skip, interval-zero
disabling, announce success/failure counters, cancellation/drain on
stop, and delivery identity registration.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import MagicMock, patch

from medre.adapters.lxmf.session import LxmfSession
from medre.config.adapters.lxmf import LxmfConfig


def _make_config(**overrides: Any) -> LxmfConfig:
    defaults: dict[str, Any] = dict(adapter_id="lxmf-announce-test")
    defaults.update(overrides)
    if (
        defaults.get("connection_type") == "reticulum"
        and "storage_path" not in overrides
    ):
        defaults["storage_path"] = "/tmp/medre-test-announce-router"
    return LxmfConfig(**defaults)


def _make_session(**config_overrides: Any) -> LxmfSession:
    config = _make_config(**config_overrides)
    return LxmfSession(config=config, adapter_id=config.adapter_id)


def _mock_reticulum_environment(
    announce_side_effect: Any = None,
) -> tuple[MagicMock, MagicMock, MagicMock]:
    """Build mock RNS/LXMF/router environment for reticulum-mode tests.

    Returns (mock_rns, mock_lxmf, mock_router).
    """
    mock_dest = MagicMock()
    mock_dest.hash = b"\xab" * 16

    mock_router = MagicMock()
    mock_router.register_delivery_callback.return_value = None
    mock_router.register_delivery_identity.return_value = mock_dest
    if announce_side_effect is not None:
        mock_router.announce.side_effect = announce_side_effect

    mock_rns = MagicMock()
    mock_lxmf = MagicMock()
    mock_rns.Reticulum.get_instance.return_value = None
    mock_rns.Reticulum.return_value = MagicMock()
    mock_rns.Identity.return_value = MagicMock()
    mock_lxmf.LXMRouter.return_value = mock_router

    return mock_rns, mock_lxmf, mock_router


def _patch_lxmf_env(mock_rns: MagicMock, mock_lxmf: MagicMock) -> Any:
    """Return a combined patch context for HAS_LXMF + _require_lxmf."""
    from contextlib import ExitStack

    stack = ExitStack()
    stack.enter_context(patch("medre.adapters.lxmf.session.HAS_LXMF", True))
    stack.enter_context(
        patch(
            "medre.adapters.lxmf.session._require_lxmf",
            return_value=(mock_rns, mock_lxmf),
        )
    )
    return stack


# ===================================================================
# Fake mode: announce task is never created
# ===================================================================


async def test_announce_task_none_in_fake_mode() -> None:
    """Fake mode never creates an announce task."""
    session = _make_session(
        connection_type="fake",
        announce_interval_seconds=600,
    )
    await session.start()
    assert session._announce_task is None
    await session.stop()


async def test_announce_counters_zero_in_fake_mode() -> None:
    """Fake mode announce counters start at zero."""
    session = _make_session(
        connection_type="fake",
        announce_interval_seconds=600,
    )
    await session.start()
    assert session.announces_sent == 0
    assert session.announce_failures == 0
    assert session.last_announce_error is None
    await session.stop()


# ===================================================================
# Interval zero: announce disabled
# ===================================================================


async def test_announce_disabled_when_interval_zero() -> None:
    """announce_interval_seconds=0 disables the announce task even in
    reticulum mode."""
    session = _make_session(
        connection_type="reticulum",
        announce_interval_seconds=0,
    )
    mock_rns, mock_lxmf, _ = _mock_reticulum_environment()

    with _patch_lxmf_env(mock_rns, mock_lxmf):
        await session.start()

    assert session._announce_task is None
    await session.stop()


# ===================================================================
# Reticulum mode: announce task created and runs
# ===================================================================


async def test_announce_task_created_in_reticulum_mode() -> None:
    """Reticulum mode with announce_interval_seconds > 0 creates the task."""
    session = _make_session(
        connection_type="reticulum",
        announce_interval_seconds=0.1,  # short for testing
    )
    mock_rns, mock_lxmf, mock_router = _mock_reticulum_environment()

    # Set up tracking announce BEFORE start.
    announce_event = asyncio.Event()

    def _tracking_announce(*args: Any, **kwargs: Any) -> None:
        announce_event.set()
        return None  # Do not call original (which is the mock itself)

    mock_router.announce.side_effect = _tracking_announce

    with _patch_lxmf_env(mock_rns, mock_lxmf):
        await session.start()

        assert session._announce_task is not None
        assert not session._announce_task.done()

        await asyncio.wait_for(announce_event.wait(), timeout=1.0)

    assert session.announces_sent >= 1
    assert session.announce_failures == 0

    await session.stop()


async def test_announce_increments_success_counter() -> None:
    """Successful announce increments announces_sent."""
    session = _make_session(
        connection_type="reticulum",
        announce_interval_seconds=0.05,
    )
    mock_rns, mock_lxmf, mock_router = _mock_reticulum_environment()

    with _patch_lxmf_env(mock_rns, mock_lxmf):
        await session.start()

        # Wait for multiple announces.
        await asyncio.sleep(0.3)

    assert session.announces_sent >= 1
    assert mock_router.announce.call_count >= 1

    await session.stop()


async def test_announce_failure_increments_failure_counter() -> None:
    """Announce exception increments announce_failures."""
    session = _make_session(
        connection_type="reticulum",
        announce_interval_seconds=0.05,
    )
    mock_rns, mock_lxmf, mock_router = _mock_reticulum_environment(
        announce_side_effect=RuntimeError("network error"),
    )

    with _patch_lxmf_env(mock_rns, mock_lxmf):
        await session.start()

        # Wait for at least one failed announce.
        await asyncio.sleep(0.2)

    assert session.announce_failures >= 1
    assert session.last_announce_error is not None
    assert "network error" in session.last_announce_error

    await session.stop()


# ===================================================================
# Cancellation and drain on stop
# ===================================================================


async def test_announce_task_cancelled_on_stop() -> None:
    """stop() cancels and drains the announce task."""
    session = _make_session(
        connection_type="reticulum",
        announce_interval_seconds=600,  # long — should not fire
    )
    mock_rns, mock_lxmf, _ = _mock_reticulum_environment()

    with _patch_lxmf_env(mock_rns, mock_lxmf):
        await session.start()

        assert session._announce_task is not None
        task = session._announce_task

    await session.stop()

    assert session._announce_task is None
    assert task.done() or task.cancelled()


async def test_announce_counters_reset_on_stop() -> None:
    """Announce counters reset to zero on stop."""
    session = _make_session(
        connection_type="reticulum",
        announce_interval_seconds=0.05,
    )
    mock_rns, mock_lxmf, _ = _mock_reticulum_environment()

    with _patch_lxmf_env(mock_rns, mock_lxmf):
        await session.start()
        await asyncio.sleep(0.2)

    pre_stop_sent = session.announces_sent
    assert pre_stop_sent >= 1

    await session.stop()

    assert session.announces_sent == 0
    assert session.announce_failures == 0
    assert session.last_announce_error is None


# ===================================================================
# Delivery identity registration
# ===================================================================


async def test_delivery_identity_registered_in_connect() -> None:
    """register_delivery_identity is called during _connect_real."""
    session = _make_session(
        connection_type="reticulum",
        announce_interval_seconds=0,
    )
    mock_rns, mock_lxmf, mock_router = _mock_reticulum_environment()

    with _patch_lxmf_env(mock_rns, mock_lxmf):
        await session.start()

    mock_router.register_delivery_identity.assert_called_once()
    assert session._delivery_destination_hash == b"\xab" * 16

    await session.stop()


async def test_no_delivery_identity_in_fake_mode() -> None:
    """Fake mode does not call register_delivery_identity."""
    session = _make_session(
        connection_type="fake",
        announce_interval_seconds=600,
    )
    await session.start()
    assert session._delivery_destination_hash is None
    await session.stop()


async def test_delivery_hash_cleared_before_register_attempt() -> None:
    """_delivery_destination_hash is cleared before each register_delivery_identity
    attempt so a None return does not leave stale state from a previous lifecycle."""
    session = _make_session(
        connection_type="reticulum",
        announce_interval_seconds=0,
    )
    mock_rns, mock_lxmf, mock_router = _mock_reticulum_environment()

    with _patch_lxmf_env(mock_rns, mock_lxmf):
        await session.start()

    # After first start, hash is set from mock_dest.hash
    assert session._delivery_destination_hash == b"\xab" * 16

    await session.stop()

    # Simulate: second start where register_delivery_identity returns None
    # (e.g. another identity already registered)
    mock_router.register_delivery_identity.return_value = None

    with _patch_lxmf_env(mock_rns, mock_lxmf):
        await session.start()

    # Hash must be None because we cleared it before the attempt and
    # the new attempt returned None.
    assert session._delivery_destination_hash is None

    await session.stop()


# ===================================================================
# Diagnostics
# ===================================================================


async def test_announce_diagnostics_in_adapter(
    make_adapter_context: Any,
) -> None:
    """Adapter diagnostics include announce counters."""
    from medre.adapters.lxmf.adapter import LxmfAdapter

    config = _make_config(
        connection_type="fake",
        announce_interval_seconds=600,
    )
    adapter = LxmfAdapter(config)
    ctx = make_adapter_context("lxmf-announce-diag")
    await adapter.start(ctx)

    diag = adapter.diagnostics()
    assert "session" in diag
    assert diag["session"]["announces_sent"] == 0
    assert diag["session"]["announce_failures"] == 0
    assert diag["session"]["last_announce_error"] is None

    await adapter.stop()


# ===================================================================
# Session-level diagnostics parity
# ===================================================================


async def test_session_diagnostics_includes_announce_counters_fake() -> None:
    """LxmfSession.diagnostics() returns announce counters in fake mode."""
    from dataclasses import fields

    from medre.adapters.lxmf.session import LxmfSessionDiagnostics

    session = _make_session(
        connection_type="fake",
        announce_interval_seconds=600,
    )
    await session.start()
    diag = session.diagnostics()
    await session.stop()

    assert isinstance(diag, LxmfSessionDiagnostics)
    assert diag.announces_sent == 0
    assert diag.announce_failures == 0
    assert diag.last_announce_error is None

    # Verify all diagnostic values are JSON-safe primitives.
    for f in fields(LxmfSessionDiagnostics):
        val = getattr(diag, f.name)
        assert val is None or isinstance(
            val, (bool, int, str)
        ), f"Field {f.name!r} is not JSON-safe: {type(val).__name__}"


async def test_session_diagnostics_includes_announce_counters_reticulum() -> None:
    """LxmfSession.diagnostics() reflects announce counters in reticulum mode."""
    session = _make_session(
        connection_type="reticulum",
        announce_interval_seconds=0.05,
    )
    mock_rns, mock_lxmf, mock_router = _mock_reticulum_environment(
        announce_side_effect=RuntimeError("boom"),
    )

    with _patch_lxmf_env(mock_rns, mock_lxmf):
        await session.start()
        await asyncio.sleep(0.2)

    diag = session.diagnostics()
    assert diag.announce_failures >= 1
    assert diag.last_announce_error is not None
    assert "boom" in diag.last_announce_error

    await session.stop()
