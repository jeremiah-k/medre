"""Attachment configuration: model validation, YAML loading, env overrides.

Run narrowly::

    pytest tests/test_attachment_config.py -q
"""

from __future__ import annotations

import asyncio
import logging
from unittest.mock import MagicMock

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
            {"transfer_timeout_seconds": 10**1000},
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


class TestRuntimeAttachmentSeam:
    """The runtime assembles the transfer seam only when policy allows."""

    @staticmethod
    def _app(attachments: AttachmentConfig, storage: object) -> object:
        from unittest.mock import MagicMock

        from medre.runtime.app import MedreApp

        return MedreApp(
            config=RuntimeConfig(attachments=attachments),
            paths=MagicMock(),
            storage=storage,
            rendering_pipeline=MagicMock(),
            router=MagicMock(),
            fallback_resolver=MagicMock(),
            relation_resolver=MagicMock(),
            pipeline_runner=MagicMock(),
            diagnostician=MagicMock(),
            adapters={},
            shutdown_event=asyncio.Event(),
            event_bus=MagicMock(),
        )

    def test_disabled_policy_assembles_nothing(self) -> None:
        app = self._app(AttachmentConfig(enabled=False), storage=MagicMock())
        app._assemble_attachment_seam()
        assert app._attachment_seam is None
        assert app._attachment_limits is None
        assert app._attachment_permits is None

    def test_enabled_without_storage_warns_and_assembles_nothing(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        app = self._app(AttachmentConfig(enabled=True), storage=None)
        caplog.set_level(logging.WARNING, logger="medre.runtime.app")
        app._assemble_attachment_seam()
        assert app._attachment_seam is None
        assert any(
            "durable storage is absent" in record.message for record in caplog.records
        )

    def test_enabled_with_storage_builds_bounded_seam(self) -> None:
        from medre.adapters.matrix.outbound import CONTENT_REF_PATTERN  # noqa: F401
        from medre.core.ingress.content import (
            AttachmentPolicyState,
            AttachmentRuntimeSeam,
        )
        from medre.core.storage.sqlite.storage import StorageAttachmentAccess

        config = AttachmentConfig(
            enabled=True,
            max_attachment_bytes=2048,
            max_retained_bytes=8192,
            max_concurrent_transfers=3,
            transfer_timeout_seconds=7.5,
        )
        app = self._app(config, storage=object())
        app._assemble_attachment_seam()

        seam = app._attachment_seam
        assert isinstance(seam, AttachmentRuntimeSeam)
        assert isinstance(seam.content, StorageAttachmentAccess)
        assert seam.permits.closed is False
        policy = seam.policy
        assert isinstance(policy, AttachmentPolicyState)
        assert policy.enabled is True
        assert policy.max_attachment_bytes == 2048
        assert policy.transfer_timeout_seconds == 7.5
        assert app._attachment_limits is not None
        assert app._attachment_limits.max_retained_bytes == 8192

        # Shutdown closes the permit gate exactly once.
        app._attachment_permits.close()
        assert seam.permits.closed is True
