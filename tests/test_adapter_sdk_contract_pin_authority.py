"""Structural guards for optional adapter SDK contract pin authority."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from tests.helpers.sdk_contract import declared_extra_pins


@pytest.mark.parametrize(
    ("extra", "expected_distributions"),
    [
        ("matrix", {"mindroom-nio"}),
        ("matrix-e2e", {"mindroom-nio"}),
        ("lxmf", {"lxmf", "rns"}),
        ("meshtastic", {"mtjk", "pypubsub"}),
        ("meshcore", {"meshcore"}),
    ],
)
def test_adapter_sdk_contract_extras_are_exact_pins(
    extra: str,
    expected_distributions: set[str],
) -> None:
    """Project metadata, not duplicated test literals, owns SDK versions."""
    pins = declared_extra_pins(extra)
    assert set(pins) == expected_distributions
    assert all(pin.version for pin in pins.values())


_REPO_ROOT = Path(__file__).resolve().parents[1]
_PIN_SCAN_ROOTS = (
    _REPO_ROOT / "README.md",
    _REPO_ROOT / ".github",
    _REPO_ROOT / "docs",
    _REPO_ROOT / "examples",
    _REPO_ROOT / "src",
    _REPO_ROOT / "scripts",
    _REPO_ROOT / "tests",
)
_SCANNED_SUFFIXES = frozenset(
    {"", ".md", ".py", ".txt", ".json", ".yaml", ".yml", ".toml", ".sh"}
)
_IGNORED_SCAN_PARTS = frozenset({"build", "dist", "__pycache__"})


def _is_source_file(path: Path) -> bool:
    """Return whether *path* is source-controlled-like text worth scanning.

    Editable installs generate ``*.egg-info`` under ``src/``; those files copy
    dependency metadata from ``pyproject.toml`` and are therefore resolved
    build artifacts, not a competing source authority.
    """
    if not path.is_file() or path.suffix not in _SCANNED_SUFFIXES:
        return False
    if any(
        part in _IGNORED_SCAN_PARTS or part.endswith(".egg-info") for part in path.parts
    ):
        return False
    return True


def test_optional_sdk_versions_are_not_duplicated_outside_project_metadata() -> None:
    """Current docs/source/tests name SDK contracts without copying pin values.

    ``pyproject.toml`` is the declared version authority; ``uv.lock`` records
    resolved artifacts.  This guard derives the current pin strings from project
    metadata so dependency updates do not require a second literal update path.
    """
    patterns: dict[str, re.Pattern[str]] = {}
    for extra in ("matrix", "matrix-e2e", "lxmf", "meshtastic", "meshcore"):
        for pin in declared_extra_pins(extra).values():
            # Ban exact version tokens, not prefixes embedded in longer values.
            # The section marker exclusion prevents unrelated ``§1.2.3`` prose
            # from being mistaken for a dependency declaration.
            version = pin.version.casefold()
            patterns[version] = re.compile(
                rf"(?<!§)(?<![\w.]){re.escape(version)}(?![\w.])"
            )

    offenders: list[str] = []
    for root in _PIN_SCAN_ROOTS:
        paths = [root] if root.is_file() else root.rglob("*")
        for path in paths:
            if not _is_source_file(path):
                continue
            try:
                text = path.read_text(encoding="utf-8").casefold()
            except UnicodeDecodeError:
                continue
            hits = sorted(
                version for version, pattern in patterns.items() if pattern.search(text)
            )
            if hits:
                offenders.append(f"{path.relative_to(_REPO_ROOT)}: {', '.join(hits)}")

    assert offenders == [], (
        "optional SDK versions must be declared only in pyproject.toml; "
        "derive expected versions from project metadata and use version-neutral "
        "wording elsewhere:\n" + "\n".join(offenders)
    )
