"""Source selection tests for immutable, non-editable image installations."""

from __future__ import annotations

import argparse
import importlib.util
from pathlib import Path

import pytest

from medre.tool_management import ToolManagementError

ROOT = Path(__file__).resolve().parents[1]


def module(relative: str):
    spec = importlib.util.spec_from_file_location(relative, ROOT / relative)
    assert spec is not None and spec.loader is not None
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


installer = module("docker/install_adapters.py")
builder = module("scripts/docker/build.py")
verifier = module("docker/verify.py")
MANIFEST = [
    {
        "transport": "matrix",
        "distribution": "medre-adapter-matrix",
        "extras": "e2e",
        "repository": "example/matrix",
        "ref": "a" * 40,
    },
    {
        "transport": "meshtastic",
        "distribution": "medre-adapter-meshtastic",
        "repository": "example/meshtastic",
        "ref": "b" * 40,
    },
]


def test_default_git_selection_uses_exact_compatibility_refs():
    assert installer.requirements(MANIFEST, "all", "git", set()) == [
        "medre-adapter-matrix[e2e] @ git+https://github.com/example/matrix.git@"
        + "a" * 40,
        "medre-adapter-meshtastic @ git+https://github.com/example/meshtastic.git@"
        + "b" * 40,
    ]


def test_index_selection_retains_required_extras():
    assert installer.requirements(MANIFEST, "matrix", "index", set()) == [
        "medre-adapter-matrix[e2e]"
    ]


def test_core_only_does_not_install_providers():
    assert installer.requirements(MANIFEST, "none", "git", set()) == []


def test_local_context_is_an_ordinary_requirement_with_extras():
    assert installer.requirements(MANIFEST, "matrix", "git", {"matrix"}) == [
        "medre-adapter-matrix[e2e] @ file:///sources/matrix"
    ]


@pytest.mark.parametrize(
    "selection,local",
    [("matrix matrix", set()), ("unknown", set()), ("none", {"matrix"})],
)
def test_invalid_adapter_selection_is_rejected(selection, local):
    with pytest.raises(ToolManagementError):
        installer.requirements(MANIFEST, selection, "git", local)


def test_catalog_can_disable_or_replace_packages_without_changing_pins(tmp_path):
    path = tmp_path / "sources.yaml"
    path.write_text(
        'adapters:\n  matrix: null\n  meshtastic: "medre-adapter-meshtastic==0.1.0"\n'
    )
    assert installer.requirements(MANIFEST, "all", "git", set(), path) == [
        "medre-adapter-meshtastic==0.1.0"
    ]
    assert MANIFEST[1]["ref"] == "b" * 40


@pytest.mark.parametrize(
    "content",
    [
        "core: medre\nadapters: {}\n",
        "adapters:\n  matrix: medre\n",
        "adapters:\n  matrix: medre-adapter-matrix\n  matrix: null\n",
    ],
)
def test_catalog_rejects_core_replacement_and_ambiguous_sources(tmp_path, content):
    path = tmp_path / "sources.yaml"
    path.write_text(content)
    with pytest.raises(ToolManagementError):
        installer.requirements(MANIFEST, "all", "git", set(), path)


def arguments(**overrides):
    result = argparse.Namespace(
        tag="medre:test",
        adapters="matrix",
        source="git",
        sources=None,
        local=[],
        no_cache=False,
        platform=None,
    )
    for key, value in overrides.items():
        setattr(result, key, value)
    return result


def test_build_uses_separate_arguments_and_named_local_context(tmp_path):
    context = tmp_path / "fork with spaces"
    context.mkdir()
    (context / "pyproject.toml").write_text('[project]\nname="medre-adapter-matrix"\n')
    catalog = tmp_path / "sources.yaml"
    catalog.write_text("adapters: {}\n")
    argv = builder.command(
        arguments(local=[f"matrix={context}"], sources=catalog, no_cache=True)
    )
    assert argv[:3] == ["docker", "buildx", "build"]
    assert "matrix=" + str(context) in argv
    assert "id=sources,src=" + str(catalog) in argv
    assert "MEDRE_LOCAL_ADAPTERS=matrix" in argv
    assert "--no-cache" in argv
    assert "--load" in argv


@pytest.mark.parametrize("override", ["unknown=/tmp", "matrix", "matrix=/missing"])
def test_build_rejects_unusable_local_contexts(override):
    with pytest.raises(ValueError):
        builder.command(arguments(local=[override]))


def test_build_rejects_missing_catalog(tmp_path):
    with pytest.raises(ValueError):
        builder.command(arguments(sources=tmp_path / "missing"))


def test_record_redacts_source_credentials_without_losing_revision():
    assert verifier.source_record(
        '{"url":"https://user:secret@example.com/fork?token=private","vcs_info":{"commit_id":"abc"}}'
    ) == {
        "url": "https://example.com/fork?redacted",
        "vcs_info": {"commit_id": "abc"},
    }


def test_secret_catalog_changes_invalidate_build_cache(tmp_path):
    catalog = tmp_path / "sources.yaml"
    catalog.write_text("adapters: {}\n")
    before = builder.command(arguments(sources=catalog))
    catalog.write_text("adapters:\n  matrix: null\n")
    after = builder.command(arguments(sources=catalog))
    assert before != after
    assert next(
        value for value in before if value.startswith("MEDRE_SOURCES_DIGEST=")
    ) != next(value for value in after if value.startswith("MEDRE_SOURCES_DIGEST="))


tagger = module("scripts/docker/image_tags.py")


def test_dev_tags_are_separate_from_release_tags_and_bind_revision():
    revision = "c" * 40
    assert tagger.tags("workflow_dispatch", "", False, "dev-feature", revision) == [
        "dev-feature",
        "dev-feature-" + revision,
    ]
    assert tagger.tags("release", "0.1.1", False, "dev", revision) == [
        "0.1.1",
        "latest",
    ]
    assert tagger.tags("release", "0.1.1rc1", True, "dev", revision) == ["0.1.1rc1"]
    assert tagger.tags("pull_request", "", False, "dev", revision) == []
    assert tagger.tags("push", "", False, "dev", revision) == []


@pytest.mark.parametrize(
    "tag", ["latest", "0.1.1", "dev/feature", "dev;echo-secret", "dev-" + "x" * 81]
)
def test_manual_builds_cannot_replace_release_tags_or_inject_commands(tag):
    with pytest.raises(ValueError):
        tagger.tags("workflow_dispatch", "", False, tag, "d" * 40)
