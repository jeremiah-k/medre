"""Architecture discovery scans CLI source without executing the CLI."""

import sys
from pathlib import Path

import pytest

from tests.helpers.import_scanner import import_lines, load_for_source_scan


def test_entrypoint_source_is_scanned_without_running_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Retain forbidden imports in the scan while preventing SystemExit."""
    package = tmp_path / "scan_fixture"
    package.mkdir()
    (package / "__init__.py").write_text("")
    source = package / "__main__.py"
    source.write_text("raise SystemExit('entrypoint executed')\nimport forbidden_sdk\n")
    monkeypatch.syspath_prepend(str(tmp_path))
    try:
        module = load_for_source_scan("scan_fixture.__main__")
        assert Path(module.__file__) == source
        assert import_lines(source.read_text()) == ["import forbidden_sdk"]
        assert "scan_fixture.__main__" not in sys.modules
    finally:
        sys.modules.pop("scan_fixture", None)
