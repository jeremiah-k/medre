"""Shared source-file reader for static boundary tests.

Provides direct path resolution — no import machinery used.
"""

from __future__ import annotations

from pathlib import Path

_SRC = Path(__file__).resolve().parents[2] / "src" / "medre"


def source_path_for_module(module_name: str) -> Path:
    """Resolve a medre.* module name to its source file path."""
    assert module_name == "medre" or module_name.startswith(
        "medre."
    ), f"Expected medre.* module, got: {module_name}"
    rel = module_name.removeprefix("medre").strip(".").replace(".", "/")
    if not rel:
        pkg = _SRC / "__init__.py"
        if pkg.exists():
            return pkg
    py = _SRC / f"{rel}.py"
    pkg = _SRC / rel / "__init__.py"
    if py.exists():
        return py
    if pkg.exists():
        return pkg
    raise ModuleNotFoundError(module_name)


def source_of(module_name: str) -> str:
    """Read the source text of a medre.* module by name."""
    return source_path_for_module(module_name).read_text(encoding="utf-8")
