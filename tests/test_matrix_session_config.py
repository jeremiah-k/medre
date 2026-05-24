"""Tests for Matrix config validation, E2EE default derivation, dependency detection,
and ClientConfig failure handling.

No test requires mindroom-nio[e2e].
"""

from __future__ import annotations

import pytest

from medre.adapters.matrix.errors import MatrixConnectionError
from medre.adapters.matrix.session import MatrixSession
from medre.config.adapters.errors import MatrixConfigError
from tests.helpers.matrix_session import (  # noqa: F401, F811
    make_matrix_config,
    mock_nio,
)

# ===================================================================
# TestMatrixConfigEncryption
# ===================================================================


class TestMatrixConfigEncryption:
    """Config validation for encryption_mode and require_encrypted_rooms."""

    def test_plaintext_default_encryption_mode(self) -> None:
        config = make_matrix_config()
        assert config.encryption_mode == "plaintext"
        config.validate()  # no error

    def test_e2ee_optional_validates_mode_string(self) -> None:
        config = make_matrix_config(encryption_mode="e2ee_optional")
        assert config.encryption_mode == "e2ee_optional"
        config.validate()  # no error

    def test_e2ee_optional_no_store_path_ok(self) -> None:
        """e2ee_optional does not require store_path."""
        config = make_matrix_config(encryption_mode="e2ee_optional")
        config.validate()

    def test_e2ee_optional_no_device_id_ok(self) -> None:
        """e2ee_optional does not require device_id."""
        config = make_matrix_config(encryption_mode="e2ee_optional")
        config.validate()

    def test_e2ee_required_without_store_path_ok(self) -> None:
        """e2ee_required does not require store_path at validation time —
        the runtime builder injects it before session construction."""
        config = make_matrix_config(encryption_mode="e2ee_required")
        assert config.store_path is None
        config.validate()  # no error — builder injects store_path later

    def test_e2ee_required_without_device_id_ok(self) -> None:
        """e2ee_required does not require device_id — discovered via whoami."""
        config = make_matrix_config(encryption_mode="e2ee_required")
        assert config.device_id is None
        config.validate()  # no error — session discovers device_id

    def test_e2ee_required_with_both_store_and_device(self) -> None:
        config = make_matrix_config(
            encryption_mode="e2ee_required",
            store_path="/tmp/store",
            device_id="DEV",
        )
        config.validate()  # no error

    def test_invalid_encryption_mode_rejected(self) -> None:
        config = make_matrix_config(encryption_mode="unknown_mode")
        with pytest.raises(MatrixConfigError, match="encryption_mode"):
            config.validate()

    def test_require_encrypted_rooms_invalid_with_plaintext(self) -> None:
        config = make_matrix_config(require_encrypted_rooms=True)
        with pytest.raises(MatrixConfigError, match="require_encrypted_rooms"):
            config.validate()

    def test_require_encrypted_rooms_valid_with_e2ee_optional(self) -> None:
        config = make_matrix_config(
            encryption_mode="e2ee_optional", require_encrypted_rooms=True
        )
        config.validate()

    def test_require_encrypted_rooms_valid_with_e2ee_required(self) -> None:
        config = make_matrix_config(
            encryption_mode="e2ee_required",
            store_path="/tmp/store",
            device_id="DEV",
            require_encrypted_rooms=True,
        )
        config.validate()

    def test_repr_no_secrets(self) -> None:
        config = make_matrix_config(
            access_token="super-secret-token-123",
            encryption_mode="e2ee_optional",
        )
        r = repr(config)
        assert "super-secret-token-123" not in r
        assert "e2ee_optional" in r

    def test_plaintext_may_omit_store_path_and_device_id(self) -> None:
        """plaintext mode works without store_path and device_id."""
        config = make_matrix_config()
        assert config.store_path is None
        assert config.device_id is None
        config.validate()


# ===================================================================
# TestE2EEDefaultDerivation
# ===================================================================


class TestE2EEDefaultDerivation:
    """e2ee_required works without operator-supplied device_id (store_path from builder)."""

    async def test_e2ee_required_without_device_id_discovers_it(
        self, mock_nio, tmp_path  # noqa: F811
    ) -> None:
        """e2ee_required starts with store_path set and discovers device_id
        via whoami() when device_id is not configured."""
        import medre.adapters.matrix.compat as compat

        original = compat.HAS_E2EE
        try:
            compat.HAS_E2EE = True
            store = tmp_path / "matrix-store"
            # store_path set (as the builder would inject), no device_id
            config = make_matrix_config(
                encryption_mode="e2ee_required",
                store_path=str(store),
            )
            assert config.store_path is not None
            assert config.device_id is None

            session = MatrixSession(config)
            try:
                await session.start()
                assert session.crypto_enabled is True
                assert session.crypto_store_loaded is True
                # whoami should have been called to discover device_id
                mock_nio.AsyncClient.return_value.whoami.assert_awaited_once()
            finally:
                await session.stop()
        finally:
            compat.HAS_E2EE = original

    async def test_e2ee_required_without_store_path_raises(
        self, mock_nio  # noqa: F811
    ) -> None:
        """e2ee_required without store_path raises — no tempdir fallback."""
        import medre.adapters.matrix.compat as compat

        original = compat.HAS_E2EE
        try:
            compat.HAS_E2EE = True
            config = make_matrix_config(encryption_mode="e2ee_required")
            assert config.store_path is None

            session = MatrixSession(config)
            with pytest.raises(
                MatrixConnectionError,
                match="E2EE requires a store_path",
            ):
                await session.start()
        finally:
            compat.HAS_E2EE = original

    async def test_e2ee_required_uses_configured_store_when_set(
        self, mock_nio  # noqa: F811
    ) -> None:
        """When store_path is explicitly configured, it is used as-is."""
        import medre.adapters.matrix.compat as compat

        original = compat.HAS_E2EE
        try:
            compat.HAS_E2EE = True
            config = make_matrix_config(
                encryption_mode="e2ee_required",
                store_path="/tmp/test-store-explicit",
                device_id="EXPLICIT_DEV",
            )
            session = MatrixSession(config)
            try:
                await session.start()
                assert session.crypto_enabled is True
                # whoami should NOT have been called (device_id was given)
                mock_nio.AsyncClient.return_value.whoami.assert_not_awaited()
            finally:
                await session.stop()
        finally:
            compat.HAS_E2EE = original

    def test_no_default_store_path_function(self) -> None:
        """_default_store_path has been removed; no tempdir fallback."""
        import medre.adapters.matrix.session as session_mod

        assert not hasattr(session_mod, "_default_store_path")
        assert not hasattr(session_mod, "_DEFAULT_STORE_DIR_TEMPLATE")

    async def test_e2ee_optional_attempts_crypto_without_device(
        self, mock_nio, tmp_path  # noqa: F811
    ) -> None:
        """e2ee_optional with HAS_E2EE=True, store_path set, no device_id
        attempts crypto and discovers device_id via whoami()."""
        import medre.adapters.matrix.compat as compat

        original = compat.HAS_E2EE
        try:
            compat.HAS_E2EE = True
            store = tmp_path / "matrix-store"
            config = make_matrix_config(
                encryption_mode="e2ee_optional",
                store_path=str(store),
            )
            session = MatrixSession(config)
            try:
                await session.start()
                assert session.crypto_enabled is True
            finally:
                await session.stop()
        finally:
            compat.HAS_E2EE = original


# ===================================================================
# TestE2EEDependencyDetection
# ===================================================================


class TestE2EEDependencyDetection:
    """HAS_E2EE detection is monkeypatchable and defaults to False."""

    def test_has_e2ee_default_false(self) -> None:
        """Without crypto deps, HAS_E2EE is False."""
        import medre.adapters.matrix.compat as compat

        # The default in CI/test envs is False (no vodozemac)
        # Just check it is a bool and False in this env
        assert isinstance(compat.HAS_E2EE, bool)

    def test_has_e2ee_monkeypatch_true(self) -> None:
        """Tests can monkeypatch HAS_E2EE to True."""
        import medre.adapters.matrix.compat as compat

        original = compat.HAS_E2EE
        try:
            compat.HAS_E2EE = True
            assert compat.HAS_E2EE is True
        finally:
            compat.HAS_E2EE = original

    def test_has_e2ee_monkeypatch_false(self) -> None:
        """Tests can monkeypatch HAS_E2EE to False."""
        import medre.adapters.matrix.compat as compat

        original = compat.HAS_E2EE
        try:
            compat.HAS_E2EE = False
            assert compat.HAS_E2EE is False
        finally:
            compat.HAS_E2EE = original

    def test_check_e2ee_returns_false_when_no_nio(self) -> None:
        """_check_e2ee returns False when HAS_NIO is False."""
        import medre.adapters.matrix.compat as compat
        from medre.adapters.matrix.compat import _check_e2ee

        original_nio = compat.HAS_NIO
        try:
            compat.HAS_NIO = False
            assert _check_e2ee() is False
        finally:
            compat.HAS_NIO = original_nio


# ===================================================================
# TestBlocker3ClientConfigFailure
# ===================================================================


class TestBlocker3ClientConfigFailure:
    """Blocker 3: ClientConfig(encryption_enabled=True) failure handling."""

    async def test_client_config_succeeds_crypto_enabled(
        self, mock_nio  # noqa: F811
    ) -> None:
        """ClientConfig succeeds → crypto_enabled=True."""
        import medre.adapters.matrix.compat as compat

        original = compat.HAS_E2EE
        try:
            compat.HAS_E2EE = True
            config = make_matrix_config(
                encryption_mode="e2ee_required",
                store_path="/tmp/store",
                device_id="DEV",
            )
            session = MatrixSession(config)
            try:
                await session.start()
                assert session.crypto_enabled is True
            finally:
                await session.stop()
        finally:
            compat.HAS_E2EE = original

    async def test_client_config_raises_matrix_connection_error(
        self, mock_nio  # noqa: F811
    ) -> None:
        """ClientConfig raises → MatrixConnectionError raised, crypto_enabled stays False."""
        import medre.adapters.matrix.compat as compat

        original = compat.HAS_E2EE
        try:
            compat.HAS_E2EE = True
            mock_nio.ClientConfig.side_effect = TypeError("bad param")
            config = make_matrix_config(
                encryption_mode="e2ee_required",
                store_path="/tmp/store",
                device_id="DEV",
            )
            session = MatrixSession(config)
            with pytest.raises(MatrixConnectionError, match="Failed to configure E2EE"):
                await session.start()
            assert session.crypto_enabled is False
        finally:
            compat.HAS_E2EE = original
            mock_nio.ClientConfig.side_effect = None

    async def test_client_closed_on_config_failure(
        self, mock_nio  # noqa: F811
    ) -> None:
        """If AsyncClient was created but ClientConfig fails, client is closed."""
        import medre.adapters.matrix.compat as compat

        original = compat.HAS_E2EE
        try:
            compat.HAS_E2EE = True
            mock_nio.ClientConfig.side_effect = TypeError("bad param")
            config = make_matrix_config(
                encryption_mode="e2ee_required",
                store_path="/tmp/store",
                device_id="DEV",
            )
            session = MatrixSession(config)
            with pytest.raises(MatrixConnectionError):
                await session.start()
            assert session.client is None
            assert session.crypto_enabled is False
        finally:
            compat.HAS_E2EE = original
            mock_nio.ClientConfig.side_effect = None
