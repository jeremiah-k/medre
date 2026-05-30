"""Import scanning helpers for architectural boundary tests."""

from __future__ import annotations

import re
from pathlib import Path

ADAPTER_PREFIXES = (
    "medre.adapters.matrix",
    "medre.adapters.meshtastic",
    "medre.adapters.meshcore",
    "medre.adapters.lxmf",
)
"""Concrete adapter package prefixes (excludes medre.core.contracts.adapter and fake_*)."""


def import_lines(source: str) -> list[str]:
    """Extract all import/from-import lines from source text."""
    return [
        line.strip()
        for line in source.splitlines()
        if line.strip().startswith(("import ", "from "))
    ]


def banned_imports(lines: list[str], banned: tuple[str, ...]) -> list[str]:
    """Return import lines referencing any banned package."""
    found: list[str] = []
    for line in lines:
        for b in banned:
            if re.search(rf"\b{re.escape(b)}\b", line):
                found.append(line)
                break
    return found


def scan_dir_for_prefixes(root: Path, prefixes: tuple[str, ...]) -> list[str]:
    """Scan all .py files under *root* for lines starting with any prefix.

    Returns a list of ``"relative_path:line_no: line"`` strings.
    Skips blank lines and comments.
    """
    assert root.exists(), f"missing directory: {root}"
    files = sorted(root.rglob("*.py"))
    assert files, f"no Python files scanned under {root}"
    violations: list[str] = []
    for p in files:
        for i, line in enumerate(p.read_text().splitlines(), 1):
            s = line.strip()
            if not s or s.startswith("#"):
                continue
            if any(s.startswith(prefix) for prefix in prefixes):
                violations.append(f"{p}:{i}: {s}")
    return violations


def scan_multiple_dirs_for_prefixes(
    roots: tuple[Path, ...], prefixes: tuple[str, ...]
) -> list[str]:
    """Scan multiple directories, collecting violations."""
    violations: list[str] = []
    for root in roots:
        if not root.exists():
            continue
        violations.extend(scan_dir_for_prefixes(root, prefixes))
    return violations


def scan_dir_for_plain_imports(root: Path, package_roots: tuple[str, ...]) -> list[str]:
    """Scan all .py files under *root* for forbidden plain import statements.

    Catches ``import <pkg>``, ``import <pkg> as ...`` at word boundaries
    so that ``import <pkg>.submodule`` is NOT flagged.
    """
    assert root.exists(), f"missing directory: {root}"
    pattern = re.compile(
        r"^import\s+(" + "|".join(re.escape(p) for p in package_roots) + r")(\s|$)"
    )
    violations: list[str] = []
    for p in sorted(root.rglob("*.py")):
        for i, line in enumerate(p.read_text().splitlines(), 1):
            s = line.strip()
            if not s or s.startswith("#"):
                continue
            if pattern.search(s):
                violations.append(f"{p}:{i}: {s}")
    return violations


def scan_multiple_dirs_for_plain_imports(
    roots: tuple[Path, ...], package_roots: tuple[str, ...]
) -> list[str]:
    """Scan multiple directories for forbidden plain imports."""
    violations: list[str] = []
    for root in roots:
        if not root.exists():
            continue
        violations.extend(scan_dir_for_plain_imports(root, package_roots))
    return violations
