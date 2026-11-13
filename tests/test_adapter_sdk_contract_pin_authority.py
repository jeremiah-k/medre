# medre-test-tier: compatibility
"""Structural guards for optional adapter SDK contract pin authority."""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from tests.helpers.sdk_contract import declared_extra_pins


@pytest.mark.parametrize(
    ("extra", "expected_distributions"),
    [
        ("matrix", {"mindroom-nio"}),
        ("lxmf", {"lxmf", "rns"}),
        ("meshtastic", {"mtjk", "pypubsub"}),
        ("meshcore", {"meshcore"}),
    ],
)
def test_adapter_sdk_contract_extras_are_exact_pins(
    extra: str,
    expected_distributions: set[str],
) -> None:
    """Adapter distribution metadata, not test literals, owns SDK versions."""
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
_VERSION_LITERAL = re.compile(r"\d+(?:\.\d+){1,3}(?:[a-zA-Z0-9.+_-]*)?")


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


def _version_literal_comparisons(source: str) -> list[int]:
    """Find equality checks between installed-version queries and literal pins."""
    tree = ast.parse(source)
    hits: list[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Compare) or not any(
            isinstance(op, (ast.Eq, ast.NotEq)) for op in node.ops
        ):
            continue
        operands = (node.left, *node.comparators)
        version_query = any(
            isinstance(value, ast.Call)
            and (
                isinstance(value.func, ast.Name)
                and value.func.id == "version"
                or isinstance(value.func, ast.Attribute)
                and value.func.attr == "version"
            )
            for value in operands
        )
        literal_pin = any(
            isinstance(value, ast.Constant)
            and isinstance(value.value, str)
            and _VERSION_LITERAL.fullmatch(value.value)
            for value in operands
        )
        if version_query and literal_pin:
            hits.append(node.lineno)
    return hits


@pytest.mark.parametrize(
    "source",
    [
        'from importlib.metadata import version\nassert version("simplex-chat") == "7.0.2"',
        'assert "7.0.2" == version("simplex-chat")',
        'assert metadata.version("simplex-chat") != "7.0.2"',
    ],
)
def test_version_literal_guard_detects_assertion_forms(source: str) -> None:
    assert _version_literal_comparisons(source) == [2 if "\n" in source else 1]


def test_version_literal_guard_allows_metadata_and_derived_versions() -> None:
    assert not _version_literal_comparisons(
        'actual = version("simplex-chat")\nassert actual == expected_version'
    )


def test_no_installed_package_version_queries_compare_to_literal_pins() -> None:
    """Installed SDK versions are dynamic; test expectations follow declared pins."""
    offenders: list[str] = []
    for root in (_REPO_ROOT / "src", _REPO_ROOT / "scripts", _REPO_ROOT / "tests"):
        for path in root.rglob("*.py"):
            if not _is_source_file(path):
                continue
            for line in _version_literal_comparisons(path.read_text(encoding="utf-8")):
                offenders.append(f"{path.relative_to(_REPO_ROOT)}:{line}")
    assert not offenders, (
        "Do not compare installed package versions to hard-coded literals; "
        "derive expectations from provider metadata:\n" + "\n".join(offenders)
    )


def test_optional_sdk_versions_are_not_duplicated_outside_project_metadata() -> None:
    """Core docs/source/tests must not duplicate adapter-owned SDK version pins.

    Installed adapter distribution metadata is the version authority. This guard
    derives exact pins from the adapter rather than assuming core SDK extras.
    """
    patterns: dict[str, re.Pattern[str]] = {}
    for extra in ("matrix", "lxmf", "meshtastic", "meshcore"):
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
        "SDK versions belong in adapter pyproject.toml; "
        "derive expected versions from project metadata and use version-neutral "
        "wording elsewhere:\n" + "\n".join(offenders)
    )
