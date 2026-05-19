"""Tests for MatrixConfig validation."""

from __future__ import annotations

import pytest

from medre.config.adapters.errors import MatrixConfigError
from medre.config.adapters.matrix import MatrixConfig


class TestMatrixConfig:
    """MatrixConfig validation logic."""

    @pytest.fixture(autouse=True)
    def _no_sidecar(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Prevent sidecar credential file from interfering with validation tests."""
        monkeypatch.setattr(
            "medre.config.adapters.matrix_credentials.load_credentials_json",
            lambda: None,
        )

    def test_valid_config(self) -> None:
        config = MatrixConfig(
            adapter_id="matrix-1",
            homeserver="https://matrix.example.com",
            user_id="@bot:example.com",
            access_token="s3cret",
        )
        # No exception raised
        config.validate()

    def test_valid_config_all_fields(self) -> None:
        config = MatrixConfig(
            adapter_id="matrix-1",
            homeserver="https://matrix.example.com",
            user_id="@bot:example.com",
            device_id="DEVICE",
            access_token="s3cret",
            room_allowlist={"!room:example.com"},
            metadata_embedding_mode="safe",
            store_path="/tmp/nio",
            sync_timeout_ms=10000,
        )
        result = config.validate()
        assert result is config

    def test_invalid_homeserver_url(self) -> None:
        config = MatrixConfig(
            adapter_id="matrix-1",
            homeserver="ftp://matrix.example.com",
            user_id="@bot:example.com",
            access_token="s3cret",
        )
        with pytest.raises(MatrixConfigError, match="homeserver"):
            config.validate()

    def test_invalid_user_id_format(self) -> None:
        config = MatrixConfig(
            adapter_id="matrix-1",
            homeserver="https://matrix.example.com",
            user_id="bot:example.com",
            access_token="s3cret",
        )
        with pytest.raises(MatrixConfigError, match="user_id"):
            config.validate()

    def test_empty_access_token(self) -> None:
        config = MatrixConfig(
            adapter_id="matrix-1",
            homeserver="https://matrix.example.com",
            user_id="@bot:example.com",
            access_token="",
        )
        with pytest.raises(MatrixConfigError, match="access_token"):
            config.validate()

    def test_room_allowlist_default_none(self) -> None:
        config = MatrixConfig(
            adapter_id="matrix-1",
            homeserver="https://matrix.example.com",
            user_id="@bot:example.com",
            access_token="s3cret",
        )
        assert config.room_allowlist is None

    def test_http_homeserver_supported(self) -> None:
        """http:// scheme is valid for local Synapse/Conduit."""
        config = MatrixConfig(
            adapter_id="matrix-1",
            homeserver="http://localhost:8008",
            user_id="@bot:example.com",
            access_token="s3cret",
        )
        config.validate()  # no error

    def test_blank_homeserver_rejected(self) -> None:
        """Blank/whitespace-only homeserver is rejected."""
        config = MatrixConfig(
            adapter_id="matrix-1",
            homeserver="  ",
            user_id="@bot:example.com",
            access_token="s3cret",
        )
        with pytest.raises(MatrixConfigError, match="homeserver"):
            config.validate()

    def test_empty_homeserver_rejected(self) -> None:
        """Empty string homeserver is rejected."""
        config = MatrixConfig(
            adapter_id="matrix-1",
            homeserver="",
            user_id="@bot:example.com",
            access_token="s3cret",
        )
        with pytest.raises(MatrixConfigError, match="homeserver"):
            config.validate()

    def test_blank_user_id_rejected(self) -> None:
        """Blank/whitespace-only user_id is rejected."""
        config = MatrixConfig(
            adapter_id="matrix-1",
            homeserver="https://matrix.example.com",
            user_id="  ",
            access_token="s3cret",
        )
        with pytest.raises(MatrixConfigError, match="user_id"):
            config.validate()

    def test_whitespace_access_token_rejected(self) -> None:
        """Whitespace-only access_token is rejected."""
        config = MatrixConfig(
            adapter_id="matrix-1",
            homeserver="https://matrix.example.com",
            user_id="@bot:example.com",
            access_token="   ",
        )
        with pytest.raises(MatrixConfigError, match="access_token"):
            config.validate()

    def test_room_allowlist_with_blank_entry_rejected(self) -> None:
        """room_allowlist with a blank string entry is rejected."""
        config = MatrixConfig(
            adapter_id="matrix-1",
            homeserver="https://matrix.example.com",
            user_id="@bot:example.com",
            access_token="s3cret",
            room_allowlist={"!valid:example.com", "  "},
        )
        with pytest.raises(MatrixConfigError, match="room_allowlist"):
            config.validate()

    def test_room_allowlist_with_non_string_entry_rejected(self) -> None:
        """room_allowlist with a non-string entry is rejected."""
        # Build a set containing a non-string entry to exercise
        # runtime validation.  Construct via a kwargs dict to avoid
        # a static type conflict on the mixed set literal.
        from typing import Any

        kwargs: dict[str, Any] = {
            "adapter_id": "matrix-1",
            "homeserver": "https://matrix.example.com",
            "user_id": "@bot:example.com",
            "access_token": "s3cret",
            "room_allowlist": {"!valid:example.com", 42},
        }
        config = MatrixConfig(**kwargs)
        with pytest.raises(MatrixConfigError, match="room_allowlist"):
            config.validate()

    def test_room_allowlist_valid_set_accepted(self) -> None:
        """Valid room_allowlist passes validation."""
        config = MatrixConfig(
            adapter_id="matrix-1",
            homeserver="https://matrix.example.com",
            user_id="@bot:example.com",
            access_token="s3cret",
            room_allowlist={"!room1:example.com", "!room2:example.com"},
        )
        config.validate()  # no error


# ===================================================================
# Secret safety
# ===================================================================


class TestMatrixConfigSecretSafety:
    """access_token must not leak through repr or logs."""

    def test_repr_redacts_access_token(self) -> None:
        """__repr__ must not expose the full access_token."""
        config = MatrixConfig(
            adapter_id="matrix-1",
            homeserver="https://matrix.example.com",
            user_id="@bot:example.com",
            access_token="supersecret12345",
        )
        r = repr(config)
        assert "supersecret12345" not in r
        assert "access_token=" in r

    def test_repr_short_token_redacted(self) -> None:
        """Very short tokens are fully masked in repr."""
        config = MatrixConfig(
            adapter_id="matrix-1",
            homeserver="https://matrix.example.com",
            user_id="@bot:example.com",
            access_token="ab",
        )
        r = repr(config)
        assert "ab" not in r
        assert "***" in r

    def test_repr_shows_homeserver_and_user_id(self) -> None:
        """Non-sensitive fields are visible in repr."""
        config = MatrixConfig(
            adapter_id="matrix-1",
            homeserver="https://matrix.example.com",
            user_id="@bot:example.com",
            access_token="tok",
        )
        r = repr(config)
        assert "matrix.example.com" in r
        assert "@bot:example.com" in r


# ===================================================================
# encryption_mode field
# ===================================================================


class TestMatrixConfigEncryptionMode:
    """encryption_mode field defaults and validation."""

    def test_default_is_plaintext(self) -> None:
        """Default encryption_mode is 'plaintext'."""
        config = MatrixConfig(
            adapter_id="matrix-1",
            homeserver="https://matrix.example.com",
            user_id="@bot:example.com",
            access_token="s3cret",
        )
        assert config.encryption_mode == "plaintext"

    def test_explicit_e2ee_required(self) -> None:
        """Explicitly setting encryption_mode='e2ee_required'."""
        config = MatrixConfig(
            adapter_id="matrix-1",
            homeserver="https://matrix.example.com",
            user_id="@bot:example.com",
            access_token="s3cret",
            encryption_mode="e2ee_required",
        )
        assert config.encryption_mode == "e2ee_required"

    def test_explicit_e2ee_optional(self) -> None:
        """Explicitly setting encryption_mode='e2ee_optional'."""
        config = MatrixConfig(
            adapter_id="matrix-1",
            homeserver="https://matrix.example.com",
            user_id="@bot:example.com",
            access_token="s3cret",
            encryption_mode="e2ee_optional",
        )
        assert config.encryption_mode == "e2ee_optional"

    def test_invalid_mode_raises(self) -> None:
        """Invalid encryption_mode raises."""
        config = MatrixConfig(
            adapter_id="matrix-1",
            homeserver="https://matrix.example.com",
            user_id="@bot:example.com",
            access_token="s3cret",
            encryption_mode="invalid",
        )
        with pytest.raises(MatrixConfigError, match="encryption_mode"):
            config.validate()


# ===================================================================
# auto_join_rooms field
# ===================================================================


class TestMatrixConfigAutoJoinRooms:
    """auto_join_rooms field defaults and validation."""

    def test_default_is_empty_tuple(self) -> None:
        """Default auto_join_rooms is an empty tuple."""
        config = MatrixConfig(
            adapter_id="matrix-1",
            homeserver="https://matrix.example.com",
            user_id="@bot:example.com",
            access_token="s3cret",
        )
        assert config.auto_join_rooms == ()

    def test_valid_rooms_accepted(self) -> None:
        """Valid canonical room IDs pass validation."""
        config = MatrixConfig(
            adapter_id="matrix-1",
            homeserver="https://matrix.example.com",
            user_id="@bot:example.com",
            access_token="s3cret",
            auto_join_rooms=("!room1:example.com", "!room2:example.com"),
        )
        result = config.validate()
        assert result.auto_join_rooms == ("!room1:example.com", "!room2:example.com")

    def test_non_bang_id_rejected(self) -> None:
        """auto_join_rooms entry not starting with '!' is rejected."""
        config = MatrixConfig(
            adapter_id="matrix-1",
            homeserver="https://matrix.example.com",
            user_id="@bot:example.com",
            access_token="s3cret",
            auto_join_rooms=("#room:example.com",),
        )
        with pytest.raises(MatrixConfigError, match="auto_join_rooms"):
            config.validate()

    def test_empty_string_rejected(self) -> None:
        """Empty string in auto_join_rooms is rejected."""
        config = MatrixConfig(
            adapter_id="matrix-1",
            homeserver="https://matrix.example.com",
            user_id="@bot:example.com",
            access_token="s3cret",
            auto_join_rooms=("",),
        )
        with pytest.raises(MatrixConfigError, match="auto_join_rooms"):
            config.validate()

    def test_non_tuple_rejected(self) -> None:
        """Non-tuple auto_join_rooms is rejected."""
        config = MatrixConfig(
            adapter_id="matrix-1",
            homeserver="https://matrix.example.com",
            user_id="@bot:example.com",
            access_token="s3cret",
            auto_join_rooms=["!room:example.com"],  # type: ignore[arg-type]
        )
        with pytest.raises(MatrixConfigError, match="auto_join_rooms must be a tuple"):
            config.validate()

    def test_preserves_room_allowlist(self) -> None:
        """Setting auto_join_rooms does not affect room_allowlist."""
        config = MatrixConfig(
            adapter_id="matrix-1",
            homeserver="https://matrix.example.com",
            user_id="@bot:example.com",
            access_token="s3cret",
            room_allowlist={"!room1:example.com"},
            auto_join_rooms=("!room2:example.com",),
        )
        result = config.validate()
        assert result.room_allowlist == {"!room1:example.com"}
        assert result.auto_join_rooms == ("!room2:example.com",)

    def test_missing_domain_rejected(self) -> None:
        """auto_join_rooms entry without ':server' part is rejected."""
        config = MatrixConfig(
            adapter_id="matrix-1",
            homeserver="https://matrix.example.com",
            user_id="@bot:example.com",
            access_token="s3cret",
            auto_join_rooms=("!no_domain",),
        )
        with pytest.raises(MatrixConfigError, match="auto_join_rooms"):
            config.validate()
