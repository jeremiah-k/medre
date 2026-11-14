# medre-test-tier: compatibility
"""Value-level binding between transport-profile tables and capability JSONs.

Every value stated in a ``docs/spec/transport-profiles/<transport>.md``
capability table must equal the corresponding entry of the sibling
``<transport>-capabilities.json`` declaration.  The test parses files only —
no adapters, SDKs, or network — so it runs in the SDK-free tier everywhere
and kills md-table drift in CI even where capability runtime conformance
itself skips without SDKs.

Presentation rules: a profile table may omit JSON rows (omission is not a
contradiction) and may annotate a value with parenthetical prose.  A cell is
bound when its leading token parses as a scalar; prose-first cells
(e.g. ``Prefix-aware (127 before device handshake)``) are bound through their
bare number when the JSON entry is numeric, and otherwise count as
presentation-only.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest

_PROFILES = Path(__file__).resolve().parents[1] / "docs" / "spec" / "transport-profiles"

_FULL_SCALAR_RE = re.compile(
    r"""^(?:
        True|False|true|false
      | None|null
      | -?\d+(?:\.\d+)?
      | "[^"]*"
      | '[^']*'
    )$""",
    re.VERBOSE,
)

# Leading scalar optionally followed by parenthetical prose, as in
# `"native"` (destination-scoped XEP-0461 replies; ...).
_LEADING_SCALAR_RE = re.compile(
    r"""^(?P<scalar>
        True|False|true|false|None|null|-?\d+(?:\.\d+)?
      | "[^"]*"
      | '[^']*'
    )\s*(?:\(.*\))?$""",
    re.VERBOSE,
)

_BARE_NUMBER_RE = re.compile(r"-?\d+(?:\.\d+)?")

_CAPABILITY_ROW_RE = re.compile(r"^\|\s*([^|]+?)\s*\|\s*(.+?)\s*\|\s*$")


def _transport_profiles() -> dict[str, Path]:
    """Discover both sides so orphan Markdown profiles cannot escape checks."""
    declared = {
        p.name[: -len("-capabilities.json")]
        for p in _PROFILES.glob("*-capabilities.json")
    }
    documented = {p.stem for p in _PROFILES.glob("*.md") if p.name != "README.md"}
    return {
        name: _PROFILES / f"{name}-capabilities.json"
        for name in sorted(declared | documented)
    }


def _strip_markdown(cell: str) -> str:
    text = cell.strip()
    # Keep parenthetical annotations while removing code ticks from the
    # leading scalar: `False` (not yet qualified) is still a bound value.
    text = re.sub(r"^`([^`]+)`(?=\s*(?:\(|$))", r"\1", text)
    return text.strip()


def _parse_scalar(text: str) -> Any:
    if text in ("True", "true"):
        return True
    if text in ("False", "false"):
        return False
    if text in ("None", "null"):
        return None
    if text.startswith(('"', "'")):
        return text[1:-1]
    if re.fullmatch(r"-?\d+", text):
        return int(text)
    return float(text)


def _same_value(md: Any, json_value: Any) -> bool:
    if isinstance(md, bool) != isinstance(json_value, bool):
        return False
    return md == json_value


def _capability_table_rows(md_text: str) -> dict[str, list[str]]:
    """Return all values, including duplicates, from both Markdown table forms."""
    rows: dict[str, list[str]] = {}
    lines = md_text.splitlines()
    for index, line in enumerate(lines):
        header = [c.strip().lower() for c in line.strip().strip("|").split("|")]
        if header != ["capability", "value"]:
            continue
        for row in lines[index + 1 :]:
            cells = [c.strip() for c in row.strip().strip("|").split("|")]
            if len(cells) != 2 or not all(cells):
                break
            if all(re.fullmatch(r":?-{3,}:?", cell) for cell in cells):
                continue
            rows.setdefault(cells[0], []).append(cells[1])
    return rows


def _profile_binding_problems(transport: str) -> list[str]:
    json_path = _PROFILES / f"{transport}-capabilities.json"
    md_path = _PROFILES / f"{transport}.md"
    if not json_path.is_file() or not md_path.is_file():
        return [f"{transport}: missing profile/declaration sibling"]
    declaration = json.loads(json_path.read_text(encoding="utf-8"))
    assert declaration["transport"] == transport
    rows = _capability_table_rows(md_path.read_text(encoding="utf-8"))
    if not rows:
        return [f"{transport}: missing or empty Capability | Value table"]
    problems: list[str] = []
    for capability, cells in sorted(rows.items()):
        json_value = declaration["capabilities"].get(capability)
        if json_value is None and capability not in declaration["capabilities"]:
            problems.append(
                f"{capability}: md table row is absent from {json_path.name}"
            )
            continue
        for cell in cells:
            text = _strip_markdown(cell)
            if _FULL_SCALAR_RE.fullmatch(text):
                if not _same_value(_parse_scalar(text), json_value):
                    problems.append(
                        f"{capability}: md states {text!r} but JSON declares {json_value!r}"
                    )
                continue
            leading = _LEADING_SCALAR_RE.fullmatch(text)
            if leading is not None:
                scalar = _parse_scalar(leading.group("scalar"))
                if not _same_value(scalar, json_value):
                    problems.append(
                        f"{capability}: md states {leading.group('scalar')!r} "
                        f"but JSON declares {json_value!r}"
                    )
                continue
            if (
                isinstance(json_value, (int, float))
                and not isinstance(json_value, bool)
                and (number := _BARE_NUMBER_RE.search(text)) is not None
                and not _same_value(float(number.group()), float(json_value))
            ):
                problems.append(
                    f"{capability}: md prose cites {number.group()!r} "
                    f"but JSON declares {json_value!r}"
                )
    return problems


@pytest.mark.parametrize("transport", sorted(_transport_profiles()))
def test_profile_md_capability_values_match_declaration(transport: str) -> None:
    """Every advertised scalar, including duplicates, binds to JSON."""
    problems = _profile_binding_problems(transport)
    assert not problems, f"{transport} capability profile drift: " + "; ".join(problems)


def test_every_capability_declaration_has_a_profile_page() -> None:
    """Every declared or documented transport must have both siblings."""
    for name in _transport_profiles():
        assert (_PROFILES / f"{name}.md").is_file(), name
        assert (_PROFILES / f"{name}-capabilities.json").is_file(), name


@pytest.mark.parametrize(
    "row",
    [
        "| text | `False` (comment) |",
        "text | `False` (comment)",
        "| text | `False` (comment) |\n| text | `True` (later duplicate) |",
    ],
)
def test_capability_parser_catches_annotated_values_without_outer_pipes(row):
    rows = _capability_table_rows("Capability | Value\n--- | ---\n" + row)
    assert len(rows.get("text", [])) == (2 if "later duplicate" in row else 1)
    assert _strip_markdown(rows["text"][0]) == "False (comment)"


def test_profile_inventory_includes_markdown_without_json(monkeypatch, tmp_path):
    (tmp_path / "orphan.md").write_text("# orphan\n")
    import sys

    monkeypatch.setattr(sys.modules[__name__], "_PROFILES", tmp_path)
    assert "orphan" in _transport_profiles()


def test_duplicate_capability_cells_are_all_bound_to_declaration(monkeypatch, tmp_path):
    import sys

    (tmp_path / "example-capabilities.json").write_text(
        json.dumps({"transport": "example", "capabilities": {"text": True}})
    )
    (tmp_path / "example.md").write_text(
        "Capability | Value\n--- | ---\ntext | `True` (original)\n"
        "text | `False` (conflicting later value)\n"
    )
    monkeypatch.setattr(sys.modules[__name__], "_PROFILES", tmp_path)
    problems = _profile_binding_problems("example")
    assert len(problems) == 1
    assert "False" in problems[0]


def test_missing_capability_table_is_not_silently_skipped(monkeypatch, tmp_path):
    import sys

    (tmp_path / "example-capabilities.json").write_text(
        json.dumps({"transport": "example", "capabilities": {"text": True}})
    )
    (tmp_path / "example.md").write_text("# prose only\n")
    monkeypatch.setattr(sys.modules[__name__], "_PROFILES", tmp_path)
    assert "missing or empty" in _profile_binding_problems("example")[0]
