"""Installed schema resources remain visible to offline support bundles."""

from pathlib import Path

import pytest

from medre.runtime import support_bundle


def test_packaged_schemas_are_used_without_checkout_resources(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    schemas = tmp_path / "_schemas"
    schemas.mkdir()
    (schemas / "runtime-config.schema.json").write_text(
        '{"$id":"urn:installed-schema","$schema":"https://json-schema.org/draft/2020-12/schema"}',
        encoding="utf-8",
    )
    (schemas / "adapter-config.schema.json").write_text("invalid", encoding="utf-8")
    monkeypatch.setattr(support_bundle, "_SCHEMAS_DIR", tmp_path / "missing")
    monkeypatch.setattr(
        support_bundle, "_VALIDATE_EXAMPLE_CONFIGS_SCRIPT", tmp_path / "missing-script"
    )
    monkeypatch.setattr(
        support_bundle.importlib.resources, "files", lambda _package: tmp_path
    )

    result = support_bundle._build_schemas_member()

    assert result.schema_source == "package-data"
    assert result.runtime_config_schema.present
    assert result.runtime_config_schema.id == "urn:installed-schema"
    assert not result.adapter_config_schema.present
    assert not result.routing_config_schema.present
    assert not result.validate_example_configs_script_present


def test_checkout_schemas_take_precedence_over_package_data(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "runtime-config.schema.json").write_text(
        '{"$id":"urn:checkout-schema"}', encoding="utf-8"
    )
    monkeypatch.setattr(support_bundle, "_SCHEMAS_DIR", source)
    monkeypatch.setattr(
        support_bundle.importlib.resources,
        "files",
        lambda _package: pytest.fail("checkout must not use package data"),
    )

    result = support_bundle._build_schemas_member()

    assert result.schema_source == "source-tree"
    assert result.runtime_config_schema.id == "urn:checkout-schema"
