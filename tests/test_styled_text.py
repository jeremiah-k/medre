"""Optional presentation cannot alter plain text or acquire transport authority."""

from copy import deepcopy
from dataclasses import FrozenInstanceError
from datetime import datetime, timezone
import json
from pathlib import Path

import jsonschema
import msgspec
import pytest

from medre.core.events.canonical import CanonicalEvent
from medre.core.events.metadata import EventMetadata
from medre.core.events.text import (
    MAX_TEXT_SPANS,
    MAX_TEXT_STYLE_DEPTH,
    MAX_STYLED_TEXT_LENGTH,
    MAX_LINK_LENGTH,
    StyledText,
    TextSpan,
    TEXT_STYLES,
)


def test_unicode_nested_spans_roundtrip_through_canonical_event():
    value = StyledText(
        "café 🦉 and code",
        (
            TextSpan(5, 6, "emphasis"),
            TextSpan(0, 6, "strong"),
            TextSpan(11, 15, "code"),
        ),
    )
    assert value.spans[0] == TextSpan(0, 6, "strong")
    event = CanonicalEvent(
        event_id="styled",
        event_kind="message.created",
        schema_version=1,
        timestamp=datetime.now(timezone.utc),
        source_adapter="source",
        source_transport_id="actor",
        source_channel_id="room",
        parent_event_id=None,
        lineage=(),
        relations=(),
        payload=value.to_payload(),
        metadata=EventMetadata(),
    )
    restored = msgspec.json.decode(msgspec.json.encode(event), type=CanonicalEvent)
    assert StyledText.from_payload(restored.payload) == value
    assert restored.payload["body"] == "café 🦉 and code"
    assert restored.schema_version == 1


def test_values_are_immutable_and_payload_copies_are_independent():
    value = StyledText("hello", (TextSpan(0, 5, "strong"),))
    with pytest.raises(FrozenInstanceError):
        value.body = "changed"
    with pytest.raises(FrozenInstanceError):
        value.spans[0].start = 1
    payload = value.to_payload()
    payload["formatting"]["spans"][0]["end"] = 1
    assert value.to_payload()["formatting"]["spans"][0]["end"] == 5


@pytest.mark.parametrize(
    "args",
    [
        (-1, 2, "strong"),
        (True, 2, "strong"),
        (0, 1.0, "strong"),
        (0, 0, "strong"),
        (2, 1, "strong"),
        (0, 1, "unknown"),
        (0, 1, "strong", "https://example.org"),
        (0, 1, "link", None),
    ],
)
def test_span_rejects_invalid_shape(args):
    with pytest.raises(ValueError):
        TextSpan(*args)


@pytest.mark.parametrize(
    "href",
    [
        "https://example.org/a?b=1&c=2",
        "HTTP://example.org",
        "https://例え.test/文",
        "mailto:person@example.org",
    ],
)
def test_links_are_data_and_use_an_explicit_safe_scheme(href):
    span = TextSpan(0, 4, "link", href)
    assert StyledText.from_payload(StyledText("link", (span,)).to_payload()).spans == (
        span,
    )


@pytest.mark.parametrize(
    "href",
    [
        "javascript:alert(1)",
        "data:text/html,test",
        "file:///etc/passwd",
        "//example.org",
        "https://",
        "https://user:secret@example.org",
        "https://example.org:invalid",
        "https://example.org/\npath",
        "https://exa mple.org",
        "https://example.org/\x00",
        'https://example.org/"onmouseover="x',
        "https://example.org/<b>",
        "https://evil.example\\.example.org",
        "https://example.org/" + "a" * 2048,
    ],
)
def test_unsafe_or_unbounded_link_targets_are_refused(href):
    with pytest.raises(ValueError, match="URI"):
        TextSpan(0, 4, "link", href)


@pytest.mark.parametrize(
    "spans",
    [
        (TextSpan(0, 6, "strong"),),
        (TextSpan(0, 3, "strong"), TextSpan(2, 5, "emphasis")),
        (TextSpan(0, 3, "strong"), TextSpan(0, 3, "strong")),
        (TextSpan(0, 5, "code"), TextSpan(1, 3, "strong")),
        (
            TextSpan(0, 5, "link", "https://example.org"),
            TextSpan(1, 3, "link", "https://example.org"),
        ),
        (TextSpan(1, 4, "quote"),),
    ],
)
def test_body_bounds_crossings_duplicates_and_opaque_code_are_refused(spans):
    with pytest.raises(ValueError):
        StyledText("hello", spans)


def test_equal_ranges_have_deterministic_safe_nesting_and_blocks_cover_lines():
    value = StyledText("code", (TextSpan(0, 4, "code"), TextSpan(0, 4, "strong")))
    assert [s.style for s in value.spans] == ["strong", "code"]
    assert (
        StyledText("one\ntwo", (TextSpan(0, 3, "quote"), TextSpan(4, 7, "pre"))).body
        == "one\ntwo"
    )


def test_metadata_resource_limits_leave_plain_body_available():
    with pytest.raises(ValueError):
        StyledText(
            "a" * (MAX_TEXT_SPANS + 1),
            tuple(TextSpan(i, i + 1, "strong") for i in range(MAX_TEXT_SPANS + 1)),
        )
    with pytest.raises(ValueError):
        StyledText(
            "a" * 40,
            tuple(
                TextSpan(i, 40 - i, "strong") for i in range(MAX_TEXT_STYLE_DEPTH + 1)
            ),
        )
    body = "a" * (MAX_STYLED_TEXT_LENGTH + 1)
    with pytest.raises(ValueError):
        StyledText(body, (TextSpan(0, 1, "strong"),))
    assert StyledText.from_payload(
        {
            "body": body,
            "formatting": {
                "version": 1,
                "spans": [{"start": 0, "end": 1, "style": "strong"}],
            },
        }
    ) == StyledText(body)


@pytest.mark.parametrize(
    "formatting",
    [
        None,
        {
            "version": 1,
            "spans": [{"start": 0, "end": 1, "style": "strong", "href": None}],
        },
        {},
        {"version": True, "spans": []},
        {"version": 1.0, "spans": []},
        {"version": 2, "spans": []},
        {"version": 1, "spans": [], "html": "<script>"},
        {"version": 1, "spans": "wrong"},
        {
            "version": 1,
            "spans": [{"start": 0, "end": 1, "style": "strong", "actor": "admin"}],
        },
        {"version": 1, "spans": [{"start": 0, "end": 50, "style": "strong"}]},
        {"version": 1, "spans": [{"start": 0, "end": 1, "style": []}]},
        {
            "version": 1,
            "spans": [
                {"start": 0, "end": 1, "style": "link", "href": "javascript:alert(1)"}
            ],
        },
    ],
)
def test_invalid_metadata_is_ignored_as_a_whole_without_mutating_body(formatting):
    payload = {
        "body": "authoritative <text>",
        "formatting": formatting,
        "native": "untouched",
    }
    before = deepcopy(payload)
    assert StyledText.from_payload(payload) == StyledText(payload["body"])
    assert payload == before


def test_legacy_plain_payloads_need_no_formatting():
    assert StyledText.from_payload({"text": "legacy"}).to_payload() == {
        "body": "legacy"
    }
    assert StyledText.from_payload({}).body == ""
    with pytest.raises(ValueError):
        StyledText.from_payload({"body": 123})


def test_published_schema_tracks_shape_vocabulary_limits_and_example():
    root = Path(__file__).resolve().parents[1]
    schema = json.loads((root / "docs/schemas/styled-text.schema.json").read_text())
    jsonschema.Draft202012Validator.check_schema(schema)
    assert set(schema["$defs"]["TextSpan"]["properties"]) == set(TextSpan.__slots__)
    assert set(schema["$defs"]["TextSpan"]["properties"]["style"]["enum"]) == set(
        TEXT_STYLES
    )
    assert (
        schema["properties"]["formatting"]["properties"]["spans"]["maxItems"]
        == MAX_TEXT_SPANS
    )
    assert (
        schema["allOf"][0]["then"]["properties"]["body"]["maxLength"]
        == MAX_STYLED_TEXT_LENGTH
    )
    assert (
        schema["$defs"]["TextSpan"]["properties"]["href"]["maxLength"]
        == MAX_LINK_LENGTH
    )
    validator = jsonschema.Draft202012Validator(schema)
    body = "a" * (MAX_STYLED_TEXT_LENGTH + 1)
    assert validator.is_valid({"body": body})
    assert not validator.is_valid(
        {
            "body": body,
            "formatting": {
                "version": 1,
                "spans": [{"start": 0, "end": 1, "style": "strong"}],
            },
        }
    )
    example = json.loads(
        (root / "docs/schemas/examples/styled-text-example.json").read_text()
    )
    jsonschema.validate(example, schema)
    assert StyledText.from_payload(example).to_payload() == example


@pytest.mark.parametrize(
    "href",
    [
        "javascript:alert(1)",
        "data:text/html,x",
        "https://example.org/<b>",
        "https://example.org/space here",
    ],
)
def test_schema_rejects_unsafe_links_without_format_assertions(href):
    root = Path(__file__).resolve().parents[1]
    schema = json.loads((root / "docs/schemas/styled-text.schema.json").read_text())
    payload = {
        "body": "link",
        "formatting": {
            "version": 1,
            "spans": [{"start": 0, "end": 4, "style": "link", "href": href}],
        },
    }
    assert not jsonschema.Draft202012Validator(schema).is_valid(payload)


def test_unicode_links_validate_with_format_assertions_enabled():
    root = Path(__file__).resolve().parents[1]
    schema = json.loads((root / "docs/schemas/styled-text.schema.json").read_text())
    validator = jsonschema.Draft202012Validator(
        schema, format_checker=jsonschema.FormatChecker()
    )
    for href in (
        "https://例え.test/文",
        "HTTP://example.org",
        "mailto:person@example.org",
    ):
        validator.validate(
            StyledText("link", (TextSpan(0, 4, "link", href),)).to_payload()
        )


@pytest.mark.parametrize(
    "payload,expected",
    [
        ({"body": "new body", "text": "legacy alias"}, "legacy alias"),
        (
            {
                "body": "new body",
                "text": "legacy alias",
                "formatting": {"version": 1, "spans": []},
            },
            "new body",
        ),
        (
            {
                "body": "new body",
                "text": "legacy alias",
                "formatting": {"invalid": True},
            },
            "new body",
        ),
        ({"body": "only body"}, "only body"),
        ({"text": "only text"}, "only text"),
    ],
)
def test_shared_text_authority_matches_styled_and_plain_rendering(payload, expected):
    """Text-only and styled renderers must agree on the selected source bytes."""
    from medre.core.events.text import select_message_body

    before = deepcopy(payload)
    assert select_message_body(payload) == expected
    assert StyledText.from_payload(payload).body == expected
    assert payload == before


def test_shared_text_authority_rejects_invalid_selected_value():
    """Invalid native text cannot be stringified into unexpected wire content."""
    from medre.core.events.text import select_message_body

    with pytest.raises(ValueError, match="must be text"):
        select_message_body({"text": 42, "body": "valid but not selected"})
    with pytest.raises(ValueError, match="must be text"):
        select_message_body({"body": 42, "text": "legacy"})
    with pytest.raises(ValueError, match="must be text"):
        StyledText.from_payload({"body": 42, "text": "legacy"})
    assert select_message_body({"text": 42}, strict=False) == "42"


def test_formatting_without_authored_body_cannot_decorate_legacy_alias():
    """Span offsets must never acquire authority over a fallback text alias."""
    payload = {
        "text": "legacy",
        "formatting": {
            "version": 1,
            "spans": [{"start": 0, "end": 3, "style": "strong"}],
        },
    }
    selected = StyledText.from_payload(payload)
    assert selected.body == "legacy"
    assert selected.spans == ()
