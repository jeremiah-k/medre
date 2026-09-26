"""Attachment configuration: model validation, YAML loading, env overrides.

Run narrowly::

    pytest tests/test_attachment_config.py -q
"""

from __future__ import annotations

import pytest

from medre.config.errors import ConfigValidationError
from medre.config.model import AttachmentConfig, RuntimeConfig


class TestAttachmentConfigValidation:
    def test_defaults_are_conservative(self) -> None:
        config = AttachmentConfig().validate()
        assert config.enabled is False
        assert config.max_attachment_bytes == 10_485_760
        assert config.max_retained_bytes == 268_435_456
        assert config.max_concurrent_transfers == 2
        assert config.transfer_timeout_seconds == 60.0
        assert RuntimeConfig().attachments == config

    @pytest.mark.parametrize(
        "overrides",
        [
            {"max_attachment_bytes": True},
            {"max_retained_bytes": "big"},
            {"max_attachment_bytes": 0},
            {"max_retained_bytes": -1},
            {"max_concurrent_transfers": 0},
            {"max_concurrent_transfers": True},
            {"transfer_timeout_seconds": 0},
            {"transfer_timeout_seconds": float("inf")},
            {"transfer_timeout_seconds": True},
            # per-attachment cap larger than the total retained budget
            {"max_attachment_bytes": 512, "max_retained_bytes": 256},
        ],
    )
    def test_invalid_combinations_rejected(self, overrides: dict) -> None:
        with pytest.raises(ConfigValidationError):
            AttachmentConfig(**overrides).validate()


class TestAttachmentYamlLoading:
    def _load(self, tmp_path, body: str) -> RuntimeConfig:
        from medre.config.loader import load_config

        config_file = tmp_path / "config.yaml"
        config_file.write_text(body, encoding="utf-8")
        config, _source, _paths = load_config(str(config_file))
        return config

    def test_section_parses_and_validates(self, tmp_path) -> None:
        config = self._load(
            tmp_path,
            "attachments:\n"
            "  enabled: true\n"
            "  max_attachment_bytes: 5242880\n"
            "  max_retained_bytes: 20971520\n"
            "  max_concurrent_transfers: 3\n"
            "  transfer_timeout_seconds: 45.5\n"
            "adapters: {}\n"
            "routes: {}\n",
        )
        attachments = config.attachments
        assert attachments.enabled is True
        assert attachments.max_attachment_bytes == 5_242_880
        assert attachments.max_retained_bytes == 20_971_520
        assert attachments.max_concurrent_transfers == 3
        assert attachments.transfer_timeout_seconds == 45.5

    def test_absent_section_means_disabled_defaults(self, tmp_path) -> None:
        config = self._load(tmp_path, "adapters: {}\nroutes: {}\n")
        assert config.attachments.enabled is False

    def test_unknown_keys_rejected(self, tmp_path) -> None:
        with pytest.raises(ConfigValidationError):
            self._load(
                tmp_path,
                "attachments:\n  enabled: true\n  max_size: 10\n"
                "adapters: {}\nroutes: {}\n",
            )

    def test_enabled_must_be_boolean(self, tmp_path) -> None:
        with pytest.raises(ConfigValidationError):
            self._load(
                tmp_path,
                "attachments:\n  enabled: maybe\nadapters: {}\nroutes: {}\n",
            )


class TestAttachmentEnvOverrides:
    def test_overrides_apply_and_revalidate(self) -> None:
        import medre.config.env as env_module

        config = RuntimeConfig(attachments=AttachmentConfig(enabled=True).validate())
        result = env_module.apply_attachment_overrides(
            config,
            {"enabled": "false", "max_concurrent_transfers": "5"},
        )
        assert result.attachments.enabled is False
        assert result.attachments.max_concurrent_transfers == 5

    def test_bool_string_for_integer_field_rejected(self) -> None:
        import medre.config.env as env_module

        with pytest.raises(ConfigValidationError):
            env_module.apply_attachment_overrides(
                RuntimeConfig(),
                {"max_attachment_bytes": "true"},
            )

    def test_invalid_override_combination_rejected(self) -> None:
        import medre.config.env as env_module

        with pytest.raises(ConfigValidationError):
            env_module.apply_attachment_overrides(
                RuntimeConfig(),
                # shrinking the retained budget below the per-attachment
                # cap violates the cross-field rule on revalidation
                {"max_retained_bytes": "1"},
            )
