"""Explicit source initialization preserves user choices and installation ownership."""

from medre.cli.main import main
from medre.tool_management import DEFAULT_SOURCES, initialize_sources, load_sources


def test_init_creates_nested_catalog_without_packages(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("MEDRE_HOME", str(tmp_path / "home"))
    main(["init"])
    target = tmp_path / "home" / "sources.yaml"
    assert load_sources(target) == DEFAULT_SOURCES
    assert "Created" in capsys.readouterr().out
    assert not (target.parent / "config.yaml").exists()


def test_init_preserves_existing_catalog_byte_for_byte(tmp_path):
    target = tmp_path / "sources.yaml"
    content = "# Local choice\nadapters:\n  matrix: null\n"
    target.write_text(content)
    assert initialize_sources(target) == (target, False)
    assert target.read_text() == content


def test_init_rejects_invalid_existing_catalog_without_overwriting(tmp_path, capsys):
    import pytest

    target = tmp_path / "sources.yaml"
    target.write_text("adapters: null\n")
    with pytest.raises(SystemExit) as exc:
        main(["init", "--sources", str(target)])
    assert exc.value.code == 2
    assert "Installation error" in capsys.readouterr().err
    assert target.read_text() == "adapters: null\n"


def test_init_identifies_catalog_creation_failure(tmp_path, capsys):
    import pytest

    parent = tmp_path / "not-a-directory"
    parent.write_text("a file cannot contain the source catalog")
    target = parent / "sources.yaml"
    with pytest.raises(SystemExit) as exc:
        main(["init", "--sources", str(target)])
    assert exc.value.code == 2
    error = capsys.readouterr().err
    assert f"Cannot create source catalog at {target}" in error
    assert "cannot execute uv" not in error
    assert parent.is_file()


def test_init_loads_a_catalog_created_between_check_and_write(tmp_path, monkeypatch):
    """A catalog created after the existence check is loaded, never replaced."""
    from pathlib import Path

    target = tmp_path / "sources.yaml"
    real_open = Path.open

    def racing_open(self, mode="r", *args, **kwargs):
        if self == target and "x" in mode:
            target.write_text("adapters:\n  matrix: null\n")
            raise FileExistsError
        return real_open(self, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", racing_open)
    assert initialize_sources(target) == (target, False)
    assert target.read_text() == "adapters:\n  matrix: null\n"
