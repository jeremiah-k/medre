"""Tests for MatrixConfig validation."""

from __future__ import annotations

import pytest

from medre.adapters.matrix.config import MatrixConfig
from medre.adapters.matrix.errors import MatrixConfigError


class TestMatrixConfig:
    """MatrixConfig validation logic."""

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
