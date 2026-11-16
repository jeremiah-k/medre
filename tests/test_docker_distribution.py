"""Source selection tests for immutable, non-editable image installations."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import shutil
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from medre.tool_management import ToolManagementError
from tests.helpers.uv_packages import BACKEND, run

ROOT = Path(__file__).resolve().parents[1]


def module(relative: str):
    spec = importlib.util.spec_from_file_location(relative, ROOT / relative)
    assert spec is not None and spec.loader is not None
    result = importlib.util.module_from_spec(spec)
    with patch.object(sys, "path", [str(ROOT / "docker"), *sys.path]):
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
    assert "MEDRE_UV_NO_CACHE=1" in argv
    assert "--load" in argv


@pytest.mark.parametrize("override", ["unknown=/tmp", "matrix", "matrix=/missing"])
def test_build_rejects_unusable_local_contexts(override):
    with pytest.raises(ValueError):
        builder.command(arguments(local=[override]))


def test_build_rejects_missing_catalog(tmp_path):
    with pytest.raises(ValueError):
        builder.command(arguments(sources=tmp_path / "missing"))


@pytest.mark.parametrize(
    "url",
    [
        "https://user:secret@example.com/fork",
        "HTTPS://example.com/fork?token=secret",
        "git+https://user:secret@example.com/fork@main",
        "https://example.com/fork#token=secret",
    ],
)
def test_credential_bearing_sources_rejected_before_install_and_runtime_copy(
    tmp_path, url
):
    catalog = tmp_path / "sources.yaml"
    catalog.write_text(
        json.dumps({"adapters": {"matrix": "medre-adapter-matrix @ " + url}})
    )
    with pytest.raises(
        ToolManagementError, match="Image source URLs must be public"
    ) as error:
        installer.requirements(MANIFEST, "matrix", "git", set(), catalog)
    assert "secret" not in str(error.value)
    raw = json.dumps({"url": url, "archive_info": {}})
    with pytest.raises(ValueError, match="Image source URLs must be public") as error:
        verifier.source_record(raw)
    assert "secret" not in str(error.value)


def test_source_record_preserves_public_revision_and_refuses_editable():
    raw = {"url": "https://example.com/fork", "vcs_info": {"commit_id": "abc"}}
    assert verifier.source_record(json.dumps(raw)) == raw
    with pytest.raises(ValueError, match="editable"):
        verifier.source_record(
            '{"url":"file:///checkout","dir_info":{"editable":true}}'
        )


def test_source_policy_allows_integrity_hash_and_subdirectory(tmp_path):
    catalog = tmp_path / "sources.yaml"
    for url in (
        "https://example.com/adapter.whl#sha256=" + "a" * 64,
        "git+https://example.com/adapter.git@main#subdirectory=python",
    ):
        requirement = "medre-adapter-matrix @ " + url
        catalog.write_text(json.dumps({"adapters": {"matrix": requirement}}))
        assert installer.requirements(MANIFEST, "matrix", "git", set(), catalog) == [
            requirement
        ]


def test_local_requirements_bypass_uv_cache(tmp_path):
    requirements = installer.requirements(MANIFEST, "matrix", "git", {"matrix"})
    command = installer.install_command(tmp_path / "requirements.txt", requirements)
    assert "--no-cache" in command
    assert "-r" in command
    assert "--no-cache" not in installer.install_command(
        tmp_path / "requirements.txt",
        installer.requirements(MANIFEST, "matrix", "git", set()),
    )


def test_requested_catalog_must_be_present_and_match_digest(tmp_path):
    catalog = tmp_path / "sources.yaml"
    assert installer.checked_catalog(catalog, "none") is None
    with pytest.raises(ToolManagementError, match="missing or stale"):
        installer.checked_catalog(catalog, "a" * 64)
    catalog.write_text("adapters: {}")
    expected = hashlib.sha256(catalog.read_bytes()).hexdigest()
    assert installer.checked_catalog(catalog, expected) == catalog
    with pytest.raises(ToolManagementError, match="missing or stale"):
        installer.checked_catalog(catalog, "none")


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
    "tag",
    [
        "latest",
        "0.1.1",
        "dev/feature",
        "dev;echo-secret",
        "dev-" + "x" * 81,
        "dev-" + "a" * 40,
    ],
)
def test_manual_builds_cannot_replace_release_tags_or_inject_commands(tag):
    with pytest.raises(ValueError):
        tagger.tags("workflow_dispatch", "", False, tag, "d" * 40)


@pytest.mark.parametrize(
    "tag", ["0.1.1rc1", "v0.1.1rc1", "0.1.1a1", "0.1.1b1", "0.1.1.dev1"]
)
def test_prerelease_tags_are_shared_with_version_validation(tmp_path, tag):
    from scripts.ci.validate_release_version import validate_release_version

    path = tmp_path / "pyproject.toml"
    version = tag.removeprefix("v")
    path.write_text(f'[project]\nversion = "{version}"\n')
    assert validate_release_version(tag, path) == version
    assert tagger.tags("release", tag, True, "dev", "c" * 40) == [tag]
    with pytest.raises(ValueError, match="marked as prereleases"):
        tagger.tags("release", tag, False, "dev", "c" * 40)


@pytest.mark.parametrize(
    "tag", ["dev", "dev-feature", "latest", "v0.1.1rc", "0.1.1+local"]
)
def test_release_tags_cannot_collide_with_dev_namespace(tag):
    with pytest.raises(ValueError):
        tagger.tags("release", tag, False, "dev", "c" * 40)


def test_publish_policy_uses_workflow_revision_and_trusted_event(tmp_path, monkeypatch):
    # A selected checkout cannot supply publication tags. Only the checkout
    # action's resolved SHA crosses into the policy helper.
    workflow = yaml.safe_load((ROOT / ".github/workflows/docker-image.yml").read_text())
    qualify = workflow["jobs"]["qualify"]
    publish = workflow["jobs"]["publish"]
    assert qualify["steps"][0]["with"]["ref"] == "${{ inputs.core_ref || github.sha }}"
    assert qualify["outputs"] == {"revision": "${{ steps.checkout.outputs.commit }}"}
    assert publish["steps"][0]["with"]["ref"] == "${{ github.workflow_sha }}"
    authorize = publish["steps"][1]
    assert authorize["working-directory"] == "publication-policy"
    assert authorize["env"]["DEV_TAG"] == "${{ inputs.dev_tag || 'dev' }}"
    assert (
        publish["steps"][-1]["env"]["IMAGE_TAGS"] == "${{ steps.policy.outputs.tags }}"
    )
    assert publish["steps"][3]["name"] == "Verify qualified image provenance"
    output = tmp_path / "output"
    revision = "a" * 40
    monkeypatch.setenv("GITHUB_EVENT_NAME", "workflow_dispatch")
    monkeypatch.setenv("GITHUB_SHA", "b" * 40)
    monkeypatch.setenv("CORE_REF", "feature")
    monkeypatch.setenv("DEV_TAG", "dev-example")
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    monkeypatch.setenv("IMAGE_TAGS", '["latest"]')
    monkeypatch.setattr(sys, "argv", ["image_tags", "--revision", revision])
    tagger.main()
    assert json.loads(output.read_text().split("tags=", 1)[1]) == [
        "dev-example",
        "dev-example-" + revision,
    ]
    monkeypatch.setenv("CORE_REF", "")
    with pytest.raises(ValueError, match="triggering event SHA"):
        tagger.main()
    monkeypatch.setenv("CORE_REF", "d" * 40)
    with pytest.raises(ValueError, match="requested core SHA"):
        tagger.main()


def test_image_verification_rejects_wrong_installed_provider_revision():
    from scripts.docker.qualify import validate_installation

    manifest = json.loads((ROOT / ".github/adapter-compatibility.json").read_text())
    record = {
        "core": "0.1.1",
        "providers": [entry["transport"] for entry in manifest],
        "direct_sources": {
            entry["distribution"]: {
                "url": f"https://github.com/{entry['repository']}.git",
                "vcs_info": {"vcs": "git", "commit_id": entry["ref"]},
            }
            for entry in manifest
        },
    }
    validate_installation(record, manifest, "0.1.1")
    source = record["direct_sources"][manifest[0]["distribution"]]
    source["vcs_info"]["commit_id"] = "0" * 40
    with pytest.raises(ValueError, match="provider provenance mismatch"):
        validate_installation(record, manifest, "0.1.1")


@pytest.mark.parametrize("bad", ["runtime", "adapter", "oom", "running", "exit"])
def test_shutdown_requires_terminal_lifecycle_and_process_evidence(bad):
    from scripts.docker.qualify import validate_shutdown

    state = {"ExitCode": 0, "OOMKilled": False, "Running": False}
    lifecycle = {
        "runtime_state": "stopped",
        "adapters": {str(i): "stopped" for i in range(7)},
    }
    snapshot = {"lifecycle": lifecycle}
    validate_shutdown(state, snapshot)
    if bad == "runtime":
        lifecycle["runtime_state"] = "failed"
    elif bad == "adapter":
        lifecycle["adapters"]["0"] = "failed"
    elif bad == "oom":
        state["OOMKilled"] = True
    elif bad == "running":
        state["Running"] = True
    else:
        state["ExitCode"] = 1
    with pytest.raises(ValueError, match="shutdown was not clean"):
        validate_shutdown(state, snapshot)


def test_named_local_requirement_rebuilds_source_only_edits(tmp_path):
    uv = shutil.which("uv")
    if uv is None:
        pytest.skip("The local source cache contract requires uv")
    source = tmp_path / "local adapter with spaces"
    source.mkdir()
    (source / "pyproject.toml").write_text(
        '[project]\nname = "example-provider"\nversion = "1.0"\n'
        '[build-system]\nrequires = []\nbuild-backend = "backend"\nbackend-path = ["."]\n'
    )
    (source / "backend.py").write_text(
        BACKEND.replace(
            'name + ".py": "def main(): pass\\n",',
            'name + ".py": pathlib.Path(name + ".py").read_text(),',
        )
    )
    module = source / "example_provider.py"
    requirements = [f"example-provider @ {source.as_uri()}"]
    path = tmp_path / "requirements.txt"
    path.write_text("\n".join(requirements) + "\n")
    env = {key: value for key, value in os.environ.items() if not key.startswith("UV_")}
    env.pop("PYTHONPATH", None)
    env.update(
        UV_CACHE_DIR=str(tmp_path / "shared-cache"),
        UV_PYTHON_DOWNLOADS="never",
        UV_NO_CONFIG="1",
    )

    def installed_value(label: str, *, bypass_cache: bool) -> str:
        venv = tmp_path / label
        run([uv, "venv", "--python", sys.executable, str(venv)], env=env)
        python = venv / (
            "Scripts/python.exe" if sys.platform == "win32" else "bin/python"
        )
        command = installer.install_command(path, requirements)
        assert "--no-cache" in command
        command[0] = uv
        command[command.index("/opt/venv/bin/python")] = str(python)
        # The standalone fixture has no core package. Keep the named -r input.
        command.remove("/core")
        if not bypass_cache:
            command.remove("--no-cache")
        run([*command, "--no-index"], env=env)
        return run(
            [
                str(python),
                "-c",
                "import example_provider; print(example_provider.VALUE)",
            ],
            env=env,
        ).strip()

    module.write_text("VALUE = 'before'\n")
    assert installed_value("seed", bypass_cache=False) == "before"
    packaging_mtime = (source / "pyproject.toml").stat().st_mtime_ns
    module.write_text("VALUE = 'after'\n")
    assert (source / "pyproject.toml").stat().st_mtime_ns == packaging_mtime
    assert installed_value("rebuild", bypass_cache=True) == "after"
