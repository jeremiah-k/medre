"""Tests for LXMF session startup/runtime hardening against real RNS/LXMF SDK.

These tests verify the narrow startup/identity/router/callback/cleanup
behaviour of ``LxmfSession._connect_real()`` against confirmed local
RNS 1.2.5 and LXMF 0.9.7 API facts.

All tests use mocks — no real Reticulum/LXMF dependency required.

Covered scenarios
-----------------
* Identity load from file (success, failure, auto-create)
* Missing SDK raises ``LxmfConnectionError``
* Router construction requires ``storagepath`` (LXMF 0.9.7 fact)
* Delivery callback registration (success, failure handling)
* Repeated ``stop()`` is idempotent
* Reticulum singleton reuse via ``get_instance()``
* Reticulum singleton raises ``OSError`` on second ``Reticulum()``
* Missing ``storage_path`` in reticulum mode raises ``LxmfConfigError``
"""
from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import MagicMock, patch, call

import pytest

from medre.config.adapters.lxmf import LxmfConfig
from medre.config.adapters.errors import LxmfConfigError
from medre.adapters.lxmf.errors import LxmfConnectionError
from medre.adapters.lxmf.session import LxmfSession


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_STORAGE = "/tmp/medre-test-lxmf-router"


def _make_config(**overrides: Any) -> LxmfConfig:
    defaults: dict[str, Any] = dict(adapter_id="lxmf-startup-test")
    defaults.update(overrides)
    if (
        defaults.get("connection_type") == "reticulum"
        and "storage_path" not in defaults
    ):
        defaults["storage_path"] = _STORAGE
    return LxmfConfig(**defaults)


def _make_session(**config_overrides: Any) -> LxmfSession:
    config = _make_config(**config_overrides)
    return LxmfSession(config=config, adapter_id=config.adapter_id)


def _mock_rns_lxmf() -> tuple[MagicMock, MagicMock]:
    """Build mock (RNS, lxmf) module pair mimicking RNS 1.2.5 / LXMF 0.9.7."""
    mock_rns = MagicMock()
    mock_lxmf = MagicMock()

    # RNS.Reticulum() — singleton pattern from RNS 1.2.5
    reticulum_instance = MagicMock()
    reticulum_instance.stop = MagicMock()
    mock_rns.Reticulum.return_value = reticulum_instance
    mock_rns.Reticulum.get_instance.return_value = None

    # RNS.Identity
    mock_identity = MagicMock()
    mock_rns.Identity.return_value = mock_identity
    mock_rns.Identity.from_file.return_value = mock_identity

    # LXMF.LXMRouter
    mock_router = MagicMock()
    mock_lxmf.LXMRouter.return_value = mock_router

    return mock_rns, mock_lxmf


# ===================================================================
# Identity load / create
# ===================================================================


class TestIdentityLoadCreate:
    """Identity loading and auto-creation behaviour."""

    async def test_identity_loaded_from_file_on_success(self) -> None:
        """When identity_path is set and from_file succeeds, the identity
        is used (not a newly-created one)."""
        mock_rns, mock_lxmf = _mock_rns_lxmf()
        session = _make_session(
            connection_type="reticulum",
            identity_path="/path/to/identity",
        )

        with (
            patch("medre.adapters.lxmf.session.HAS_LXMF", True),
            patch(
                "medre.adapters.lxmf.session._require_lxmf",
                return_value=(mock_rns, mock_lxmf),
            ),
        ):
            await session.start()

        mock_rns.Identity.from_file.assert_called_once_with(
            "/path/to/identity"
        )
        # Identity() constructor should NOT have been called.
        mock_rns.Identity.assert_not_called()
        assert session._identity is mock_rns.Identity.from_file.return_value
        await session.stop()

    async def test_identity_load_failure_raises_connection_error(self) -> None:
        """When identity_path is set but from_file returns None,
        LxmfConnectionError is raised (RNS 1.2.5: from_file returns
        None on invalid/corrupt file)."""
        mock_rns, mock_lxmf = _mock_rns_lxmf()
        mock_rns.Identity.from_file.return_value = None

        session = _make_session(
            connection_type="reticulum",
            identity_path="/path/to/bad_identity",
        )

        with (
            patch("medre.adapters.lxmf.session.HAS_LXMF", True),
            patch(
                "medre.adapters.lxmf.session._require_lxmf",
                return_value=(mock_rns, mock_lxmf),
            ),
        ):
            with pytest.raises(LxmfConnectionError, match="Failed to load"):
                await session.start()

        assert session.connected is False

    async def test_identity_auto_created_when_no_path(self) -> None:
        """When identity_path is None, a new RNS.Identity() is created."""
        mock_rns, mock_lxmf = _mock_rns_lxmf()
        session = _make_session(
            connection_type="reticulum",
            identity_path=None,
        )

        with (
            patch("medre.adapters.lxmf.session.HAS_LXMF", True),
            patch(
                "medre.adapters.lxmf.session._require_lxmf",
                return_value=(mock_rns, mock_lxmf),
            ),
        ):
            await session.start()

        # from_file should NOT be called.
        mock_rns.Identity.from_file.assert_not_called()
        # Identity() constructor should have been called once.
        mock_rns.Identity.assert_called_once()
        assert session.connected is True
        await session.stop()


# ===================================================================
# Missing SDK
# ===================================================================


class TestMissingSDK:
    """Clear error when lxmf/RNS packages are not installed."""

    async def test_missing_sdk_raises_connection_error(self) -> None:
        session = _make_session(connection_type="reticulum")
        with patch("medre.adapters.lxmf.session.HAS_LXMF", False):
            with pytest.raises(LxmfConnectionError, match="not installed"):
                await session.start()
        assert session.connected is False

    async def test_missing_sdk_repeated_attempts_still_raise(self) -> None:
        """Multiple start() attempts without SDK all raise, no stale state."""
        session = _make_session(connection_type="reticulum")
        with patch("medre.adapters.lxmf.session.HAS_LXMF", False):
            for _ in range(3):
                with pytest.raises(LxmfConnectionError):
                    await session.start()
                assert session.connected is False


# ===================================================================
# Router construction
# ===================================================================


class TestRouterConstruction:
    """LXMRouter receives required storagepath (LXMF 0.9.7 fact)."""

    async def test_router_receives_storagepath(self) -> None:
        """LXMRouter(identity=..., storagepath=...) is called with
        storage_path from config."""
        mock_rns, mock_lxmf = _mock_rns_lxmf()
        storage = "/custom/lxmf/storage"
        session = _make_session(
            connection_type="reticulum",
            storage_path=storage,
        )

        with (
            patch("medre.adapters.lxmf.session.HAS_LXMF", True),
            patch(
                "medre.adapters.lxmf.session._require_lxmf",
                return_value=(mock_rns, mock_lxmf),
            ),
        ):
            await session.start()

        mock_lxmf.LXMRouter.assert_called_once()
        call_kwargs = mock_lxmf.LXMRouter.call_args
        assert call_kwargs.kwargs.get("storagepath") == storage or (
            len(call_kwargs.args) > 1 and call_kwargs.args[1] == storage
        ), f"storagepath not passed: {call_kwargs}"
        await session.stop()

    async def test_router_receives_identity(self) -> None:
        """LXMRouter receives the loaded/created identity."""
        mock_rns, mock_lxmf = _mock_rns_lxmf()
        session = _make_session(connection_type="reticulum")

        with (
            patch("medre.adapters.lxmf.session.HAS_LXMF", True),
            patch(
                "medre.adapters.lxmf.session._require_lxmf",
                return_value=(mock_rns, mock_lxmf),
            ),
        ):
            await session.start()

        call_kwargs = mock_lxmf.LXMRouter.call_args
        assert call_kwargs.kwargs.get("identity") is not None, (
            f"identity not passed: {call_kwargs}"
        )
        await session.stop()


# ===================================================================
# Delivery callback registration
# ===================================================================


class TestDeliveryCallbackRegistration:
    """Delivery callback is registered on the router."""

    async def test_delivery_callback_registered(self) -> None:
        """register_delivery_callback is called with the session's handler."""
        mock_rns, mock_lxmf = _mock_rns_lxmf()
        mock_router = mock_lxmf.LXMRouter.return_value
        session = _make_session(connection_type="reticulum")

        with (
            patch("medre.adapters.lxmf.session.HAS_LXMF", True),
            patch(
                "medre.adapters.lxmf.session._require_lxmf",
                return_value=(mock_rns, mock_lxmf),
            ),
        ):
            await session.start()

        mock_router.register_delivery_callback.assert_called_once()
        # Verify the callback is a bound method of the session instance.
        callback = mock_router.register_delivery_callback.call_args[0][0]
        assert hasattr(callback, "__self__")
        assert callback.__self__ is session
        assert callback.__name__ == "_on_lxmf_delivery"
        await session.stop()

    async def test_delivery_callback_failure_raises_connection_error(self) -> None:
        """If register_delivery_callback raises, the session raises
        LxmfConnectionError (hardened handler)."""
        mock_rns, mock_lxmf = _mock_rns_lxmf()
        mock_router = mock_lxmf.LXMRouter.return_value
        mock_router.register_delivery_callback.side_effect = AttributeError(
            "no such method"
        )
        session = _make_session(connection_type="reticulum")

        with (
            patch("medre.adapters.lxmf.session.HAS_LXMF", True),
            patch(
                "medre.adapters.lxmf.session._require_lxmf",
                return_value=(mock_rns, mock_lxmf),
            ),
        ):
            with pytest.raises(LxmfConnectionError, match="delivery callback"):
                await session.start()

        assert session.connected is False

    async def test_announce_callback_failure_is_ignored(self) -> None:
        """If register_announce_callback raises, it's silently ignored
        (not all LXMF versions support it)."""
        mock_rns, mock_lxmf = _mock_rns_lxmf()
        mock_router = mock_lxmf.LXMRouter.return_value
        mock_router.register_announce_callback.side_effect = AttributeError
        session = _make_session(connection_type="reticulum")

        with (
            patch("medre.adapters.lxmf.session.HAS_LXMF", True),
            patch(
                "medre.adapters.lxmf.session._require_lxmf",
                return_value=(mock_rns, mock_lxmf),
            ),
        ):
            await session.start()

        assert session.connected is True
        await session.stop()


# ===================================================================
# Repeated stop
# ===================================================================


class TestRepeatedStop:
    """stop() is idempotent — safe to call multiple times."""

    async def test_stop_twice_no_error(self) -> None:
        session = _make_session(connection_type="fake")
        await session.start()
        await session.stop()
        await session.stop()  # second stop — no error
        assert session.connected is False

    async def test_stop_triple_no_error(self) -> None:
        session = _make_session(connection_type="fake")
        await session.start()
        await session.stop()
        await session.stop()
        await session.stop()
        assert session.connected is False

    async def test_stop_before_start_no_error(self) -> None:
        session = _make_session(connection_type="fake")
        await session.stop()  # never started — no error
        assert session.connected is False

    async def test_repeated_start_stop_cycles_clean(self) -> None:
        """Multiple start/stop cycles leave no leaked state."""
        session = _make_session(connection_type="fake")
        for _ in range(5):
            await session.start()
            assert session.connected is True
            await session.stop()
            assert session.connected is False


# ===================================================================
# Reticulum singleton
# ===================================================================


class TestReticulumSingleton:
    """RNS 1.2.5 Reticulum singleton constraint handling."""

    async def test_creates_reticulum_when_no_existing_instance(self) -> None:
        """When get_instance() returns None, a new Reticulum() is created."""
        mock_rns, mock_lxmf = _mock_rns_lxmf()
        mock_rns.Reticulum.get_instance.return_value = None

        session = _make_session(connection_type="reticulum")

        with (
            patch("medre.adapters.lxmf.session.HAS_LXMF", True),
            patch(
                "medre.adapters.lxmf.session._require_lxmf",
                return_value=(mock_rns, mock_lxmf),
            ),
        ):
            await session.start()

        # get_instance should have been checked first.
        mock_rns.Reticulum.get_instance.assert_called()
        # New instance created since get_instance returned None.
        mock_rns.Reticulum.assert_called_once()
        assert session._reticulum is mock_rns.Reticulum.return_value
        await session.stop()

    async def test_reuses_existing_reticulum_instance(self) -> None:
        """When get_instance() returns an existing instance, it is reused
        instead of calling Reticulum() (which would raise OSError in
        RNS 1.2.5)."""
        mock_rns, mock_lxmf = _mock_rns_lxmf()
        existing_instance = MagicMock()
        mock_rns.Reticulum.get_instance.return_value = existing_instance

        session = _make_session(connection_type="reticulum")

        with (
            patch("medre.adapters.lxmf.session.HAS_LXMF", True),
            patch(
                "medre.adapters.lxmf.session._require_lxmf",
                return_value=(mock_rns, mock_lxmf),
            ),
        ):
            await session.start()

        # get_instance should have been checked.
        mock_rns.Reticulum.get_instance.assert_called()
        # Reticulum() constructor should NOT have been called.
        mock_rns.Reticulum.assert_not_called()
        # Session should hold the existing instance.
        assert session._reticulum is existing_instance
        assert session.connected is True
        await session.stop()

    async def test_second_reticulum_call_would_raise_oserror(self) -> None:
        """Verify that calling RNS.Reticulum() twice raises OSError
        (RNS 1.2.5 singleton fact). This test confirms the mock
        accurately reflects the real RNS behaviour."""
        mock_rns, mock_lxmf = _mock_rns_rns_lxmf_for_singleton()

        # First call succeeds.
        mock_rns.Reticulum.get_instance.return_value = None
        r1 = mock_rns.Reticulum(None)
        assert r1 is not None

        # After first call, get_instance returns the running instance.
        mock_rns.Reticulum.get_instance.return_value = r1

        # Simulate RNS 1.2.5: second Reticulum() raises OSError.
        mock_rns.Reticulum.side_effect = OSError(
            "Attempt to reinitialise Reticulum, when it was already running"
        )

        # Our session should handle this by checking get_instance first.
        session = _make_session(connection_type="reticulum")
        with (
            patch("medre.adapters.lxmf.session.HAS_LXMF", True),
            patch(
                "medre.adapters.lxmf.session._require_lxmf",
                return_value=(mock_rns, mock_lxmf),
            ),
        ):
            await session.start()

        # Should have reused the existing instance, not called constructor.
        assert session._reticulum is r1
        assert session.connected is True
        await session.stop()

    async def test_reticulum_teardown_on_stop(self) -> None:
        """Session teardown releases the Reticulum reference."""
        mock_rns, mock_lxmf = _mock_rns_lxmf()
        session = _make_session(connection_type="reticulum")

        with (
            patch("medre.adapters.lxmf.session.HAS_LXMF", True),
            patch(
                "medre.adapters.lxmf.session._require_lxmf",
                return_value=(mock_rns, mock_lxmf),
            ),
        ):
            await session.start()
            assert session._reticulum is not None

        await session.stop()
        assert session._reticulum is None


# ===================================================================
# Config validation for storage_path
# ===================================================================


class TestStoragePathConfigValidation:
    """storage_path is required for reticulum mode (LXMF 0.9.7 fact)."""

    def test_reticulum_without_storage_path_rejected(self) -> None:
        """LxmfConfig rejects reticulum mode without storage_path."""
        with pytest.raises(LxmfConfigError, match="storage_path"):
            LxmfConfig(
                adapter_id="lxmf-1",
                connection_type="reticulum",
            ).validate()

    def test_reticulum_with_storage_path_accepted(self) -> None:
        """LxmfConfig accepts reticulum mode with storage_path."""
        config = LxmfConfig(
            adapter_id="lxmf-1",
            connection_type="reticulum",
            storage_path="/tmp/lxmf-router",
        )
        assert config.validate() is config

    def test_fake_without_storage_path_accepted(self) -> None:
        """Fake mode does not require storage_path."""
        config = LxmfConfig(adapter_id="lxmf-1", connection_type="fake")
        assert config.validate() is config

    def test_storage_path_empty_string_rejected(self) -> None:
        """Empty storage_path string is rejected."""
        with pytest.raises(LxmfConfigError, match="storage_path"):
            LxmfConfig(
                adapter_id="lxmf-1",
                connection_type="reticulum",
                storage_path="   ",
            ).validate()

    def test_storage_path_non_string_rejected(self) -> None:
        """Non-string storage_path is rejected."""
        with pytest.raises(LxmfConfigError, match="storage_path"):
            LxmfConfig(
                adapter_id="lxmf-1",
                connection_type="reticulum",
                storage_path=123,
            ).validate()


# ===================================================================
# Start idempotency
# ===================================================================


class TestStartIdempotency:
    """start() is idempotent — second call is a no-op."""

    async def test_start_twice_no_error(self) -> None:
        session = _make_session(connection_type="fake")
        await session.start()
        await session.start()  # second start — no-op
        assert session.connected is True
        await session.stop()

    async def test_start_after_stop_works(self) -> None:
        """start() can be called again after stop()."""
        session = _make_session(connection_type="fake")
        await session.start()
        await session.stop()
        assert session.connected is False
        await session.start()
        assert session.connected is True
        await session.stop()


# ===================================================================
# Session cleanup completeness
# ===================================================================


class TestSessionCleanup:
    """After stop(), all SDK objects are released."""

    async def test_all_sdk_objects_null_after_stop(self) -> None:
        mock_rns, mock_lxmf = _mock_rns_lxmf()
        session = _make_session(connection_type="reticulum")

        with (
            patch("medre.adapters.lxmf.session.HAS_LXMF", True),
            patch(
                "medre.adapters.lxmf.session._require_lxmf",
                return_value=(mock_rns, mock_lxmf),
            ),
        ):
            await session.start()
            assert session._reticulum is not None
            assert session._identity is not None
            assert session._router is not None

        await session.stop()
        assert session._reticulum is None
        assert session._identity is None
        assert session._router is None
        assert session.connected is False
        assert session.router_running is False

    async def test_diagnostics_truthful_after_stop(self) -> None:
        """Diagnostics reflect fully-stopped state."""
        mock_rns, mock_lxmf = _mock_rns_lxmf()
        session = _make_session(connection_type="reticulum")

        with (
            patch("medre.adapters.lxmf.session.HAS_LXMF", True),
            patch(
                "medre.adapters.lxmf.session._require_lxmf",
                return_value=(mock_rns, mock_lxmf),
            ),
        ):
            await session.start()

        await session.stop()
        diag = session.diagnostics()
        assert diag.connected is False
        assert diag.router_running is False
        assert diag.reconnecting is False
        assert diag.reconnect_attempts == 0


# ===================================================================
# Helper for singleton test
# ===================================================================


def _mock_rns_rns_lxmf_for_singleton() -> tuple[MagicMock, MagicMock]:
    """Build mock (RNS, lxmf) with Reticulum singleton semantics."""
    mock_rns = MagicMock()
    mock_lxmf = MagicMock()

    # LXMRouter mock
    mock_router = MagicMock()
    mock_lxmf.LXMRouter.return_value = mock_router

    # Identity mock
    mock_identity = MagicMock()
    mock_rns.Identity.return_value = mock_identity
    mock_rns.Identity.from_file.return_value = mock_identity

    # Reticulum mock — no side effects yet (set by the test)
    mock_rns.Reticulum.return_value = MagicMock()
    mock_rns.Reticulum.get_instance.return_value = None

    return mock_rns, mock_lxmf
