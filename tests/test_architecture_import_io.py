"""Import-time blocking I/O checks.

Verifies that lightweight modules don't perform blocking I/O at import time
(open, read_text, sqlite3.connect, network calls, subprocess, etc.).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from tests.helpers.ast_imports import (
    parse_python,
    runtime_scope_imports,
    top_level_calls,
)

_REPO = Path(__file__).resolve().parents[1]
_SRC = _REPO / "src" / "medre"

# Directories/files to scan for blocking I/O at import time
_SCAN_PATHS: list[Path] = [
    _SRC / "core",
    _SRC / "config" / "model.py",
    _SRC / "interop" / "mmrelay.py",
]

# Additional specific files to scan
_SCAN_FILES: list[Path] = [
    _SRC / "adapters" / "matrix" / "codec.py",
    _SRC / "adapters" / "matrix" / "renderer.py",
    _SRC / "adapters" / "meshtastic" / "codec.py",
    _SRC / "adapters" / "meshtastic" / "renderer.py",
    _SRC / "adapters" / "meshcore" / "codec.py",
    _SRC / "adapters" / "meshcore" / "renderer.py",
    _SRC / "adapters" / "lxmf" / "codec.py",
    _SRC / "adapters" / "lxmf" / "renderer.py",
    _SRC / "config" / "__init__.py",
    _SRC / "config" / "adapters" / "__init__.py",
]

# Function names/patterns that indicate blocking I/O at import time
_BLOCKING_FUNCS: tuple[str, ...] = (
    "open",
    "read_text",
    "write_text",
    "sqlite3.connect",
    "requests.get",
    "requests.post",
    "requests.request",
    "urlopen",
    "create_connection",
    "subprocess.run",
    "subprocess.Popen",
    "subprocess.check_call",
    "subprocess.check_output",
    "asyncio.run",
    "time.sleep",
)

# Allowlist of (file_path_rel, func_name, reason) for known intentional cases
_ALLOWLIST: list[tuple[str, str, str]] = [
    # Add intentional cases here with justification
    # e.g., ("medre/config/model.py", "open", "Config loading during build")
]


def _scan_file(py_file: Path) -> list[str]:
    """Scan a single Python file for blocking I/O calls at module level."""
    violations: list[str] = []
    tree = parse_python(py_file)
    calls = top_level_calls(tree, file_path=str(py_file))
    rel = str(py_file.relative_to(_REPO))

    for call in calls:
        for blocking_func in _BLOCKING_FUNCS:
            if call.func == blocking_func or call.func.endswith(f".{blocking_func}"):
                # Check allowlist
                allowed = False
                for aw_rel, aw_func, _reason in _ALLOWLIST:
                    if rel == aw_rel and call.func == aw_func:
                        allowed = True
                        break
                if not allowed:
                    violations.append(
                        f"{rel}:{call.lineno}: blocking call {call.func}() at module level"
                    )
                break

    return violations


class TestNoBlockingIOAtImport:
    """Lightweight modules must not perform blocking I/O at import time."""

    def test_core_modules_no_blocking_io(self) -> None:
        all_violations: list[str] = []
        for path in _SCAN_PATHS:
            if path.is_dir():
                for py_file in sorted(path.rglob("*.py")):
                    all_violations.extend(_scan_file(py_file))
            elif path.is_file():
                all_violations.extend(_scan_file(path))

        for py_file in _SCAN_FILES:
            if py_file.exists():
                all_violations.extend(_scan_file(py_file))

        # Filter out __init__.py files that are just package markers
        all_violations = [
            v for v in all_violations
            if not v.endswith("__init__.py:")  # unlikely but safe
        ]

        assert not all_violations, (
            "Blocking I/O calls found at module level:\n" +
            "\n".join(all_violations)
        )
