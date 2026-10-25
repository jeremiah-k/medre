"""Fixed plain-text presentation fixtures, independent of provider renderers."""

from __future__ import annotations

from collections.abc import Iterable

SUITE = "baseline-v1"
FIXTURES = ("unicode", "lines", "literal", "budget")
_RADIOS = frozenset({"meshtastic", "meshcore", "lxmf"})
_TEXT = {
    "unicode": "café ✓ 雪 🦉",
    "lines": "café ✓\n  second line\tend",
    "literal": "café ✓ <b>literal</b> & [text]",
    "budget": "café ✓",
}


def text(name: str, marker: str, transport: str) -> str:
    """Build authored text from a fixed fixture; keep radio ingress bounded.

    The budget fixture supplies an oversized body only through IP services.
    All radios remain destinations of that fixture; radio-origin turns use the
    short variant. No expected body is obtained from a renderer or outbox.
    """
    result = marker + _TEXT[name]
    if name == "budget" and transport not in _RADIOS:
        result += " " + "🌍é" * 64
    return result


def turns(sources: Iterable[str], suite: str) -> list[tuple[str, str]]:
    """Enumerate source/fixture coverage before any native peer starts."""
    if suite not in {"smoke", SUITE}:
        raise ValueError("Unknown native text fixture suite")
    return [
        (source, name)
        for source in sources
        for name in (FIXTURES if suite == SUITE else ("smoke",))
    ]


def validate_attributions(endpoints: dict[str, dict]) -> None:
    """Require fixture-authored sender claims before native baseline owners start."""
    for endpoint in endpoints.values():
        expected = endpoint.get("expected_source_attribution")
        if (
            not isinstance(expected, dict)
            or set(expected) != {"sender_name", "origin_label", "verification"}
            or not all(
                value is None or isinstance(value, str) and len(value) <= 1024
                for key, value in expected.items()
                if key != "verification"
            )
            or not isinstance(expected["verification"], str)
            or not 0 < len(expected["verification"]) <= 1024
        ):
            raise ValueError(
                "Baseline requires explicit expected_source_attribution for every source"
            )


def verify_bridge_context(
    record: dict, event: dict, source: str, transport: str, expected: dict
) -> None:
    """Compare native Matrix hints with independent fixture claims and scoped ingress facts."""
    context = record.get("native_details", {}).get("bridge_context")
    assert isinstance(context, dict), "Native Matrix bridge context is missing"
    assert (
        type(context.get("schema_version")) is int and context["schema_version"] == 1
    ), "Native context version differs"
    claim = context["source"]
    assert (
        claim["adapter"] == source and claim["platform"] == transport
    ), "Native context source differs"
    assert (
        claim["canonical_event_id"] == event["event_id"]
    ), "Native context event correlation differs"
    assert (
        claim["channel"] == event["source_channel_id"]
    ), "Native context source conversation differs"
    assert (
        claim["sender_id"] == event["source_transport_id"]
    ), "Native context sender ID differs"
    assert (
        claim["sender_name"] == expected["sender_name"]
    ), "Native context sender name differs"
    assert (
        claim["origin_label"] == expected["origin_label"]
    ), "Native context origin differs"
    assert (
        claim["sender_verification"] == expected["verification"]
    ), "Native context verification differs"
    actual_ref = event.get("source_native_ref")
    assert claim["native_ref"] == (
        {
            field: actual_ref.get(field)
            for field in (
                "adapter",
                "native_channel_id",
                "native_message_id",
                "native_thread_id",
            )
        }
        if actual_ref is not None
        else None
    ), "Native context scoped reference differs"
    assert context["relations"] == [], "Plain native text acquired an invented relation"
