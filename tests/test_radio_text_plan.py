"""Radio wire plans preserve content and budgets before any native handoff."""

import hashlib
import json
from dataclasses import FrozenInstanceError, asdict
from pathlib import Path

import jsonschema
import pytest

from medre.core.rendering.radio_text import plan_radio_text


def test_short_message_has_no_numbering_and_stable_fingerprint():
    """Ordinary short traffic preserves its exact wire text and deterministic ID."""
    plan = plan_radio_text("hello", prefix="Jerem[LF]: ", max_text_bytes=40)
    assert plan.parts[0].wire_text == "Jerem[LF]: hello"
    assert plan.parts[0].body == "hello"
    assert plan.parts[0].ordinal == 1
    assert len(plan.parts) == 1
    assert plan == plan_radio_text("hello", prefix="Jerem[LF]: ", max_text_bytes=40)
    assert (
        plan.fingerprint
        != plan_radio_text("hello", prefix="Jerem[LF]: ", max_text_bytes=41).fingerprint
    )
    assert (
        plan.fingerprint
        != plan_radio_text("hello", prefix="Jerem[LT]: ", max_text_bytes=40).fingerprint
    )
    assert (
        plan.fingerprint
        != plan_radio_text("other", prefix="Jerem[LF]: ", max_text_bytes=40).fingerprint
    )


@pytest.mark.parametrize("budget", [24, 50, 137, 230])
@pytest.mark.parametrize(
    "body",
    [
        "a" * 250,
        "alpha beta gamma " * 18,
        "雪山 😀 café\n" * 20,
        " \t\n  " * 40,
        "a\u0301b👩\u200d💻\r\n" * 25,
    ],
)
def test_fragments_reconstruct_text_with_byte_safe_numbered_attribution(body, budget):
    """Both radios can use one plan without cutting UTF-8 or losing whitespace."""
    prefix = "J[LF]: "
    plan = plan_radio_text(body, prefix=prefix, max_text_bytes=budget, max_parts=128)
    assert "".join(part.body for part in plan.parts) == body
    assert plan.source_sha256 == hashlib.sha256(body.encode("utf-8")).hexdigest()
    for ordinal, part in enumerate(plan.parts, 1):
        marker = f"({ordinal}/{len(plan.parts)}) " if len(plan.parts) > 1 else ""
        assert part.ordinal == ordinal
        assert part.body
        assert part.wire_text == prefix + marker + part.body
        assert part.wire_bytes == len(part.wire_text.encode("utf-8")) <= budget
        assert (
            part.wire_sha256
            == hashlib.sha256(part.wire_text.encode("utf-8")).hexdigest()
        )


def test_words_stay_whole_when_they_fit_and_whitespace_is_not_normalized():
    """Numbered output retains authored spacing and line endings between words."""
    plan = plan_radio_text("one two\nthree four", max_text_bytes=14)
    assert [part.body for part in plan.parts] == ["one two\n", "three ", "four"]
    assert [part.wire_text for part in plan.parts] == [
        "(1/3) one two\n",
        "(2/3) three ",
        "(3/3) four",
    ]


def test_marker_digit_growth_replans_every_part():
    """The nine-to-ten transition must count both numerator and denominator bytes."""
    plan = plan_radio_text("a" * 110, prefix="p:", max_text_bytes=20)
    assert len(plan.parts) == 11
    assert plan.parts[0].wire_text == "p:(1/11) " + "a" * 11
    assert plan.parts[-1].wire_text.startswith("p:(11/11) ")
    assert all(part.wire_bytes <= 20 for part in plan.parts)


def test_leading_whitespace_does_not_waste_a_part_before_an_oversized_word():
    """Prefer a word boundary only when it follows actual word content."""
    plan = plan_radio_text(" " + "x" * 30, max_text_bytes=20)
    assert len(plan.parts) == 3
    assert plan.parts[0].body == " " + "x" * 13
    assert "".join(part.body for part in plan.parts) == " " + "x" * 30


def test_long_leading_whitespace_does_not_scan_quadratically():
    """Large all-whitespace slices stay intact and planning stays bounded."""
    text = " " * 120_000 + "end"
    plan = plan_radio_text(text, max_text_bytes=120_000, max_parts=3)
    assert len(plan.parts) == 2
    assert plan.parts[0].body.isspace()
    assert "".join(part.body for part in plan.parts) == text
    assert all(part.wire_bytes <= 120_000 for part in plan.parts)


def test_multibyte_prefix_counts_against_the_same_budget_as_body():
    """Compact Unicode sender names consume bytes even when they look short."""
    plan = plan_radio_text("雪" * 8, prefix="雪: ", max_text_bytes=20)
    assert len(plan.parts) > 1
    assert all(part.wire_bytes <= 20 for part in plan.parts)
    with pytest.raises(ValueError, match="Unicode code point"):
        plan_radio_text("😀😀😀", prefix="雪: ", max_text_bytes=13)


@pytest.mark.parametrize(
    "text,kwargs",
    [
        ("", {}),
        ("a", {"max_text_bytes": 0}),
        ("a", {"max_text_bytes": True}),
        ("a", {"max_text_bytes": 1.5}),
        ("a", {"max_parts": False}),
        ("a", {"max_parts": 129}),
        ("a" * 100, {"max_parts": 1}),
        ("a" * 100, {"max_parts": 2}),
        ("hello world", {"max_text_bytes": 6}),
        ("x", {"prefix": "name too long"}),
        ("\ud800", {}),
        ("a", {"prefix": "\ud800"}),
        ("a" * 1_048_577, {}),
        ("😀" * 262_145, {}),
    ],
    ids=[
        "empty",
        "zero-budget",
        "bool-budget",
        "float-budget",
        "bool-count",
        "unbounded-count",
        "one-part-cap",
        "two-part-cap",
        "marker-overhead",
        "prefix-overhead",
        "invalid-source",
        "invalid-prefix",
        "large-source",
        "large-utf8-source",
    ],
)
def test_unusable_budget_or_input_fails_before_any_partial_plan(text, kwargs):
    """A caller receives either a complete plan or an error, never silent truncation."""
    options = {"max_text_bytes": 10, **kwargs}
    with pytest.raises(ValueError):
        plan_radio_text(text, **options)


@pytest.mark.parametrize("text,prefix", [(None, ""), ("hello", None)])
def test_non_text_inputs_are_rejected(text, prefix):
    """Implicit string conversion cannot silently change native payload content."""
    with pytest.raises(TypeError):
        plan_radio_text(text, prefix=prefix, max_text_bytes=40)


def test_plan_and_parts_are_immutable_and_match_the_published_wire_schema():
    """Persistable snapshots carry stable part ordinals, budgets and wire hashes."""
    plan = plan_radio_text("longer text " * 10, max_text_bytes=30)
    with pytest.raises(FrozenInstanceError):
        plan.max_text_bytes = 99
    with pytest.raises(FrozenInstanceError):
        plan.parts[0].wire_text = "changed"
    schema = json.loads(
        (
            Path(__file__).resolve().parents[1]
            / "docs/schemas/radio-text-plan.schema.json"
        ).read_text()
    )
    jsonschema.Draft202012Validator.check_schema(schema)
    jsonschema.validate(json.loads(json.dumps(asdict(plan))), schema)
