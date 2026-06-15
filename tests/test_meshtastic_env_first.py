"""Meshtastic env-first runtime test: adapters built from env vars alone.

Proves that adapters can be created entirely from environment variables
(MEDRE_ADAPTER__<TOKEN>__TRANSPORT=meshtastic) with no YAML adapter stanzas,
wired into routes defined in YAML, and built into a running application via
RuntimeBuilder — all without live radio hardware.

The env-first creation model:
  MEDRE_ADAPTER__<TOKEN>__TRANSPORT=meshtastic

When TRANSPORT is set and the token matches no YAML adapter, a new adapter
is created from env vars. Routes must still be YAML-defined.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from medre.config.env import apply_env_overrides
from medre.config.errors import ConfigValidationError
from medre.config.loader import load_config
from medre.runtime.builder import RuntimeBuilder

# ---------------------------------------------------------------------------
# Minimal YAML config: no adapter stanzas, only runtime/storage/routes
# ---------------------------------------------------------------------------

_ENV_FIRST_YAML = """\
runtime:
  name: env-created-mesh-test

storage:
  backend: memory

routes:
  a_to_bridge:
    source_adapters: [radio-a]
    dest_adapters: [radio-b]
    directionality: source_to_dest
    enabled: true
"""

_ENV_FIRST_YAML_SINGLE_ROUTE = """\
runtime:
  name: env-created-mesh-test

storage:
  backend: memory

routes:
  a_to_bridge:
    source_adapters: [radio-a]
    dest_adapters: [radio-b]
    directionality: source_to_dest
    enabled: true
"""

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_config(tmp_path: Path, yaml_content: str = _ENV_FIRST_YAML) -> Path:
    """Write YAML config to a temp file and return its path."""
    config_path = tmp_path / "env_first.yaml"
    config_path.write_text(yaml_content)
    return config_path


def _set_both_radio_envs(monkeypatch: pytest.MonkeyPatch) -> None:
    """Set env vars to create radio-a and radio-b Meshtastic adapters."""
    monkeypatch.setenv("MEDRE_ADAPTER__RADIO_A__TRANSPORT", "meshtastic")
    monkeypatch.setenv("MEDRE_ADAPTER__RADIO_A__CONNECTION_TYPE", "fake")
    monkeypatch.setenv("MEDRE_ADAPTER__RADIO_A__ORIGIN_LABEL", "RadioA")
    monkeypatch.setenv("MEDRE_ADAPTER__RADIO_A__ADAPTER_KIND", "fake")

    monkeypatch.setenv("MEDRE_ADAPTER__RADIO_B__TRANSPORT", "meshtastic")
    monkeypatch.setenv("MEDRE_ADAPTER__RADIO_B__CONNECTION_TYPE", "fake")
    monkeypatch.setenv("MEDRE_ADAPTER__RADIO_B__ORIGIN_LABEL", "RadioB")
    monkeypatch.setenv("MEDRE_ADAPTER__RADIO_B__ADAPTER_KIND", "fake")


def _set_radio_a_env_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """Set env vars to create only radio-a."""
    monkeypatch.setenv("MEDRE_ADAPTER__RADIO_A__TRANSPORT", "meshtastic")
    monkeypatch.setenv("MEDRE_ADAPTER__RADIO_A__CONNECTION_TYPE", "fake")
    monkeypatch.setenv("MEDRE_ADAPTER__RADIO_A__ORIGIN_LABEL", "RadioA")
    monkeypatch.setenv("MEDRE_ADAPTER__RADIO_A__ADAPTER_KIND", "fake")


def _load_with_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, yaml_content: str = _ENV_FIRST_YAML
) -> tuple[Any, Any, Any]:
    """Write config, load YAML, apply env overrides, return (config, source, paths)."""
    config_path = _write_config(tmp_path, yaml_content)
    config, source, paths = load_config(str(config_path))
    config = apply_env_overrides(config)
    return config, source, paths


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestEnvCreatedMeshtasticAdaptersLoad:
    """Env-created Meshtastic adapters load with correct config values."""

    def test_env_created_meshtastic_adapters_load(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Both radio-a and radio-b created from env vars with correct fields."""
        _set_both_radio_envs(monkeypatch)
        config, _source, _paths = _load_with_env(monkeypatch, tmp_path)

        # Both adapters exist in meshtastic section
        assert "radio-a" in config.adapters.meshtastic
        assert "radio-b" in config.adapters.meshtastic

        radio_a = config.adapters.meshtastic["radio-a"]
        radio_b = config.adapters.meshtastic["radio-b"]

        # connection_type set from env
        assert radio_a.config.connection_type == "fake"
        assert radio_b.config.connection_type == "fake"

        # origin_label set from env
        assert radio_a.config.origin_label == "RadioA"
        assert radio_b.config.origin_label == "RadioB"

        # adapter_ids derived from env token
        assert radio_a.adapter_id == "radio-a"
        assert radio_b.adapter_id == "radio-b"


class TestEnvCreatedAdaptersBuildViaRuntimeBuilder:
    """Env-created adapters survive RuntimeBuilder.build() and app.start()."""

    @pytest.mark.asyncio
    async def test_env_created_adapters_build_via_runtimebuilder(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Build and start a runtime with env-created Meshtastic adapters."""
        _set_both_radio_envs(monkeypatch)
        config, _source, paths = _load_with_env(monkeypatch, tmp_path)

        builder = RuntimeBuilder(config, paths)
        app = builder.build()

        try:
            await app.start()

            # Both adapters are in the app
            assert "radio-a" in app.adapters
            assert "radio-b" in app.adapters

            # Adapters have correct origin_label from their config.
            # For fake adapters, _build_fake_adapter creates a default
            # MeshtasticConfig — so we verify the RuntimeConfig that was
            # passed to the builder carries the env-created origin_label.
            assert config.adapters.meshtastic["radio-a"].config.origin_label == "RadioA"
            assert config.adapters.meshtastic["radio-b"].config.origin_label == "RadioB"

            # Built adapters themselves are the correct type and platform.
            radio_a_adapter = app.adapters["radio-a"]
            radio_b_adapter = app.adapters["radio-b"]
            assert radio_a_adapter.platform == "meshtastic"
            assert radio_b_adapter.platform == "meshtastic"
        finally:
            try:
                await app.stop()
            except Exception:
                pass


class TestEnvCreatedRoutesValidate:
    """Routes referencing env-created adapters validate correctly."""

    def test_env_created_routes_validate(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Routes have correct source/dest adapter IDs after env overlay."""
        _set_both_radio_envs(monkeypatch)
        config, _source, _paths = _load_with_env(monkeypatch, tmp_path)

        # At least one route exists
        assert len(config.routes.routes) >= 1

        route = config.routes.routes[0]
        assert route.source_adapters == ("radio-a",)
        assert route.dest_adapters == ("radio-b",)
        assert route.enabled is True


class TestEnvCreatedNoCrossContamination:
    """Only adapters with TRANSPORT set are created; others are absent."""

    def test_env_created_no_cross_contamination(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """radio-a is created; radio-b is NOT (no TRANSPORT, no config stanza)."""
        _set_radio_a_env_only(monkeypatch)
        config, _source, _paths = _load_with_env(monkeypatch, tmp_path)

        # radio-a exists
        assert "radio-a" in config.adapters.meshtastic
        assert config.adapters.meshtastic["radio-a"].config.origin_label == "RadioA"

        # radio-b does NOT exist — no TRANSPORT set, no config stanza
        assert "radio-b" not in config.adapters.meshtastic


class TestEnvCreatedWithoutTransportRejected:
    """Env vars without TRANSPORT on unknown token raise ConfigValidationError."""

    def test_env_created_without_transport_rejected(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Setting CONNECTION_TYPE without TRANSPORT on unknown token raises."""
        config_path = _write_config(tmp_path)
        config, _source, paths = load_config(str(config_path))

        # Set CONNECTION_TYPE but NOT TRANSPORT for an unknown token
        monkeypatch.setenv("MEDRE_ADAPTER__RADIO_A__CONNECTION_TYPE", "fake")

        with pytest.raises(ConfigValidationError, match="Unknown adapter token"):
            apply_env_overrides(config)


class TestEnvCreatedMeshtasticDefaults:
    """Env-created adapter with only TRANSPORT uses dataclass defaults."""

    def test_env_created_meshtastic_defaults(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Only TRANSPORT set; connection_type defaults to 'fake'."""
        monkeypatch.setenv("MEDRE_ADAPTER__RADIO_A__TRANSPORT", "meshtastic")
        monkeypatch.setenv("MEDRE_ADAPTER__RADIO_A__ADAPTER_KIND", "fake")

        config_path = _write_config(tmp_path)
        config, _source, _paths = load_config(str(config_path))
        config = apply_env_overrides(config)

        assert "radio-a" in config.adapters.meshtastic
        adapter = config.adapters.meshtastic["radio-a"]
        # MeshtasticConfig.connection_type defaults to "fake"
        assert adapter.config is not None
        assert adapter.config.connection_type == "fake"
