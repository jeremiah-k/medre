"""Keep operator installation instructions aligned with package boundaries."""

from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_OPS = _ROOT / "docs" / "ops"


def _doc(name: str) -> str:
    return (_OPS / name).read_text(encoding="utf-8")


def test_getting_started_keeps_a_reproducible_deployment_environment() -> None:
    first_run = _doc("getting-started.md")
    assert "install.md#locked-deployment" in first_run
    guide = _doc("install.md")
    assert "uv init --bare" in guide
    assert "uv add" in guide
    assert "uv run --locked medre" in guide
    assert "uv sync --locked" in guide
    assert "uv.lock" in guide
    assert "MEDRE_HOME" in guide


def test_persistent_cli_install_includes_the_provider_in_the_same_tool() -> None:
    install = _doc("install.md")
    start = install.index("```bash\nuv tool install")
    end = install.index("```", start + 7)
    command = install[start:end]
    assert "medre adapters install matrix" in command
    assert "uv tool install --python 3.12 medre" in command
    assert "medre version" in command
    assert "uv.lock" in install
    assert "same tool environment" in install


def test_installation_examples_use_installation_not_runtime_as_package_manager() -> (
    None
):
    installation = _doc("adapter-installation.md")
    assert "does not" in installation.lower()
    assert "`medre.adapters`" in installation
    assert "uv sync --locked" in installation
    assert "tool.uv.sources" in installation
    assert "git+https://github.com" in installation
    assert "MEDRE does not" in installation


def test_smoke_in_install_guide_persists_evidence_without_config_rewrite() -> None:
    install = _doc("install.md")
    assert (
        "medre smoke --config sample.yaml --storage-path smoke.sqlite3 --json"
        in install
    )
    assert "medre inspect event EVENT_ID --storage-path smoke.sqlite3" in install
    assert "not network or RF reception" in install


def test_readme_has_a_runnable_installation_path_and_setup_guidance() -> None:
    readme = (_ROOT / "README.md").read_text(encoding="utf-8")
    assert "uv tool install" in readme
    assert "medre config check --config sample.yaml" in readme
    assert "medre smoke --config sample.yaml" in readme
    assert "simulated adapters" in readme
    assert "medre run --config bridge.yaml" in readme
    assert "https://matrix.to/#/#medre:matrix.org" in readme
    assert "docs/ops/getting-started.md" in readme


def test_operator_index_is_a_valid_two_column_navigation_table() -> None:
    index = _doc("README.md")
    rows = [line for line in index.splitlines() if line.startswith("|")]
    assert len(rows) >= 8
    assert all(line.count("|") == 3 for line in rows)
    assert "[Getting Started](getting-started.md)" in index
    assert "[Recovery and replay](recovery-and-replay.md)" in index


def test_new_source_commands_are_not_documented_as_available_in_010() -> None:
    package_management = _doc("package-management.md")
    assert "0.1.0" in package_management
    assert "Core 0.1.0 predates them" in package_management
    installation_sources = (_ROOT / "docs/dev/installation-sources.md").read_text(
        encoding="utf-8"
    )
    assert "0.1.0" in installation_sources
    assert "does not recognize" in installation_sources
    for guide in (package_management, installation_sources):
        assert (
            "uv tool install --python 3.12 'git+https://github.com/jeremiah-k/medre.git@main'"
            in guide
        )
        assert "uv tool install 'medre==0.1.0'" not in guide


def test_matrix_tool_examples_select_a_compatible_python() -> None:
    for path in ("getting-started.md", "install.md", "package-management.md"):
        assert "uv tool install --python 3.12" in _doc(path)
    readme = (_ROOT / "README.md").read_text(encoding="utf-8")
    assert "Python 3.12" in readme
    assert "```bash\nuv tool install medre\n```" in readme
    assert "medre adapters install matrix meshtastic" in readme


def test_container_templates_have_explicit_resolution_procedure() -> None:
    guide = _doc("docker.md")
    config = _doc("configuration.md")
    assert "docker.md#prepare-a-docker-bridge-example" in config
    assert "Neither MEDRE nor Compose substitutes variables" in config
    for name in (
        "MEDRE_HOMESERVER",
        "MEDRE_USER_ID",
        "MEDRE_ACCESS_TOKEN",
        "MEDRE_ROOM_ID",
        "MESHTASTIC_HOST",
    ):
        assert f"${{{name}}}" in guide
    assert "make check" in guide
    assert "FROM python:3.11-slim" not in config


def test_operator_sdk_commands_use_the_installed_tool_environment() -> None:
    install = _doc("install.md")
    assert '"$(uv tool dir)/medre/bin/python"' in install
    assert '"$(uv tool dir)/medre/bin/meshtastic"' in install
    assert "medre/Scripts/python.exe" in install
    assert "medre/Scripts/meshtastic.exe" in install
    assert '"$(uv tool dir)/medre/bin/python"' in _doc("transport-setup/meshcore.md")
    assert '"$(uv tool dir)/medre/bin/meshtastic"' in _doc(
        "transport-setup/meshtastic.md"
    )


def test_rejected_store_reset_points_to_wal_safe_backup() -> None:
    spec = (_ROOT / "docs/spec/storage.md").read_text(encoding="utf-8")
    reset = spec.split("### 10.5 Pre-Release Stale Database Reset", 1)[1].split(
        "## 11.", 1
    )[0]
    assert "Do not copy only the main file" in reset
    assert "WAL/SHM" in reset
    assert "operator-workflows.md#step-by-step-reset-procedure" in reset
    assert "not a verified SQLite snapshot" in reset
