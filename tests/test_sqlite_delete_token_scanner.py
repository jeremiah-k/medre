"""Storage DELETE scans must cover whole SQL string tokens, not source lines.

A triple-quoted statement that puts ``DELETE`` on one line and ``FROM``
on the next is accepted by SQLite, but a per-line scanner checks neither
line: the prefix needs both words on the same physical line. This module
scans complete string constants instead and pins that every DELETE
destination in the SQLite storage package is allowlisted, pytest
function style.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

_STORAGE_MODULE_DIR = (
    Path(__file__).resolve().parent.parent
    / "src"
    / "medre"
    / "core"
    / "storage"
    / "sqlite"
)

#: Same identifier syntaxes as the line-based scanner, but across any
#: whitespace including real newlines inside triple-quoted statements.
_DELETE_PREFIX = r"(?:\\[nrt]|(?<![\w$]))DELETE\s+FROM\b"
_TOKEN_TARGET_PATTERN = re.compile(
    _DELETE_PREFIX
    + r'\s+(?:"((?:[^"]|"")+)"|`((?:[^`]|``)+)`|\[((?:[^\]]|\]\])+?)\]|([^\s;(),=<>!{}\\\'"`]+))',
    re.IGNORECASE | re.DOTALL,
)
_LINE_TARGET_PATTERN = re.compile(
    _DELETE_PREFIX
    + r'\s+(?:"((?:[^"]|"")+)"|`((?:[^`]|``)+)`|\[((?:[^\]]|\]\])+?)\]|([^\s;(),=<>!{}\\\'"`]+))',
    re.IGNORECASE,
)

_COLLECTOR_PERMITTED = {"event_body_content", "event_relation_excerpts"}
_CEILING_PERMITTED = {
    "event_body_content",
    "event_relation_excerpts",
    "delivery_evidence_diagnostics",
    "radio_attempt_progress",
    "delivery_outbox_snapshots",
    "event_attachment_associations",
    "attachment_blobs",
}


def _token_delete_targets(sql_text: str) -> tuple[str, ...]:
    """Parse every DELETE destination in one complete SQL string token."""
    targets = []
    for match in _TOKEN_TARGET_PATTERN.finditer(sql_text):
        target = next((group for group in match.groups() if group), None)
        if target is None:
            return ()
        targets.append(target)
    return tuple(targets)


def _storage_source_files() -> list[Path]:
    """All .py files in the sqlite storage module directory."""
    return sorted(_STORAGE_MODULE_DIR.glob("*.py"))


def _string_constants(source: str) -> list[str]:
    """Every non-docstring string constant, including f-string fragments."""
    tree = ast.parse(source)
    docstrings: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(
            node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
        ):
            body = getattr(node, "body", [])
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
            ):
                docstrings.add(id(body[0].value))
    constants: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if id(node) not in docstrings:
                constants.append(node.value)
        elif isinstance(node, ast.JoinedStr):
            for value in node.values:
                if isinstance(value, ast.Constant) and isinstance(value.value, str):
                    constants.append(value.value)
    return constants


def test_split_line_statement_is_invisible_to_a_line_scan_but_caught_by_tokens() -> (
    None
):
    split = 'sql = """DELETE\nFROM delivery_receipts WHERE event_id=?"""'
    # The per-line gap: no physical line carries both DELETE and FROM, so
    # each line is skipped by a line-based prefix scan.
    for line in split.split("\n"):
        assert _LINE_TARGET_PATTERN.search(line) is None
    # The token-level scan parses the destination across the newline.
    assert _token_delete_targets(split) == ("delivery_receipts",)


def test_every_string_token_delete_target_is_allowlisted() -> None:
    violations: list[str] = []
    for py_file in _storage_source_files():
        constants = _string_constants(py_file.read_text())
        for constant in constants:
            for target in _token_delete_targets(constant):
                if py_file.name == "_collection.py":
                    if target not in _COLLECTOR_PERMITTED:
                        violations.append(f"{py_file.name}: {target}")
                elif py_file.name == "_ceiling_enforcement.py":
                    if target not in _CEILING_PERMITTED:
                        violations.append(f"{py_file.name}: {target}")
                elif py_file.name == "statements.py":
                    # Frozen work context: one exact-table DELETE guarded by
                    # a terminal transition in the same transaction.
                    if (
                        target != "delivery_outbox_snapshots"
                        or constant.strip()
                        != "DELETE FROM delivery_outbox_snapshots WHERE outbox_id = ?"
                    ):
                        violations.append(f"{py_file.name}: {target}")
                else:
                    violations.append(f"{py_file.name}: {target}")
    assert not violations, "Unapproved DELETE destination: " + repr(violations)


def test_token_scan_sees_the_whole_ceiling_purge_battery() -> None:
    """The scanner must not silently pass the guarded purge's own deletes."""
    found: set[str] = set()
    for py_file in _storage_source_files():
        if py_file.name != "_ceiling_enforcement.py":
            continue
        for constant in _string_constants(py_file.read_text()):
            found.update(_token_delete_targets(constant))
    assert found == _CEILING_PERMITTED
