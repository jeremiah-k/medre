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
    return {
        p.name[: -len("-capabilities.json")]: p
        for p in sorted(_PROFILES.glob("*-capabilities.json"))
    }


def _strip_markdown(cell: str) -> str:
    return cell.strip().strip("`").strip()


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
    """Return capability -> raw cells for every ``Capability | Value`` table."""
    rows: dict[str, list[str]] = {}
    lines = md_text.splitlines()
    for index, line in enumerate(lines):
        header = [c.strip().lower() for c in line.strip().strip("|").split("|")]
        if header != ["capability", "value"]:
            continue
        for row in lines[index + 1 :]:
            if not row.strip().startswith("|"):
                break
            if re.fullmatch(r"\|[\s:|-]+\|?", row.strip()):
                continue
            match = _CAPABILITY_ROW_RE.match(row.strip())
            if match is None:
                continue
            rows.setdefault(match.group(1), []).append(match.group(2))
    return rows


@pytest.mark.parametrize("transport", sorted(_transport_profiles()))
def test_profile_md_capability_values_match_declaration(transport: str) -> None:
    """Each stated md value must equal the JSON declaration entry."""
    json_path = _PROFILES / f"{transport}-capabilities.json"
    declaration = json.loads(json_path.read_text(encoding="utf-8"))
    assert declaration["transport"] == transport
    md_path = _PROFILES / f"{transport}.md"
    assert md_path.is_file(), f"capability declaration without profile page: {md_path}"
    rows = _capability_table_rows(md_path.read_text(encoding="utf-8"))
    problems: list[str] = []
    for capability, cells in sorted(rows.items()):
        json_value = declaration["capabilities"].get(capability)
        if json_value is None and capability not in declaration["capabilities"]:
            problems.append(
                f"{capability}: md table row is absent from {json_path.name}"
            )
            continue
        cell = cells[0]
        text = _strip_markdown(cell)
        if _FULL_SCALAR_RE.match(text):
            if not _same_value(_parse_scalar(text), json_value):
                problems.append(
                    f"{capability}: md states {text!r} but JSON declares "
                    f"{json_value!r}"
                )
            continue
        leading = _LEADING_SCALAR_RE.match(text)
        if leading is not None:
            scalar = _parse_scalar(leading.group("scalar"))
            if not _same_value(scalar, json_value):
                problems.append(
                    f"{capability}: md states {leading.group('scalar')!r} but JSON "
                    f"declares {json_value!r}"
                )
            continue
        if (
            isinstance(json_value, (int, float))
            and not isinstance(json_value, bool)
            and (number := _BARE_NUMBER_RE.search(text)) is not None
        ):
            if not _same_value(float(number.group()), float(json_value)):
                problems.append(
                    f"{capability}: md prose cites {number.group()!r} but JSON "
                    f"declares {json_value!r}"
                )
            continue
        # Presentation-only row (prose-first, no bindable scalar): report, do
        # not force a rewrite — omission of a plain value is not a
        # contradiction.
    assert (
        not problems
    ), f"{transport}.md capability table contradicts {json_path.name}: " + "; ".join(
        problems
    )


def test_every_capability_declaration_has_a_profile_page() -> None:
    """Each JSON declaration must sit beside its narrative profile page."""
    missing = [
        name
        for name in _transport_profiles()
        if not (_PROFILES / f"{name}.md").is_file()
    ]
    assert not missing, f"profile pages missing for: {sorted(missing)}"
