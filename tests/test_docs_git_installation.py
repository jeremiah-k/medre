"""Keep Git-first operator installation instructions aligned with package boundaries."""

from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_OPS = _ROOT / "docs" / "ops"


def _doc(name: str) -> str:
    return (_OPS / name).read_text(encoding="utf-8")


def test_getting_started_keeps_a_reproducible_deployment_environment() -> None:
    guide = _doc("getting-started.md")
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
    assert (
        "--with 'git+https://github.com/jeremiah-k/medre-adapter-matrix.git" in command
    )
    assert "'git+https://github.com/jeremiah-k/medre.git" in command
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


def test_readme_has_a_runnable_git_first_path_and_data_warning() -> None:
    readme = (_ROOT / "README.md").read_text(encoding="utf-8")
    assert "uv tool install" in readme
    assert "medre config check --config sample.yaml" in readme
    assert "medre smoke --config sample.yaml" in readme
    assert "not** remote delivery" in readme
    assert "back up persistent state" in readme
    assert "not production-ready" in readme
    assert "docs/ops/getting-started.md" in readme


def test_operator_index_is_a_valid_two_column_navigation_table() -> None:
    index = _doc("README.md")
    rows = [line for line in index.splitlines() if line.startswith("|")]
    assert len(rows) >= 8
    assert all(line.count("|") == 3 for line in rows)
    assert "[Getting Started](getting-started.md)" in index
    assert "[Recovery and replay](recovery-and-replay.md)" in index
