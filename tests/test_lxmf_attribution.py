"""Tests for LXMF native-to-generic attribution projection.

Function-style tests covering:
- ``project_lxmf_attribution`` main entry point.
- ``normalize_source_hash`` bytes/str normalisation.
- ``derive_label`` and ``derive_short_label`` label derivation.
- Edge cases: absent keys, empty values, non-string types.
"""

from __future__ import annotations

from medre.adapters.lxmf.attribution import (
    LxmfAttribution,
    derive_label,
    derive_short_label,
    normalize_source_hash,
    project_lxmf_attribution,
)

# ---------------------------------------------------------------------------
# normalize_source_hash
# ---------------------------------------------------------------------------


def test_normalize_source_hash_hex_string() -> None:
    """Hex string source_hash is returned unchanged."""
    h = "abcdef0123456789"
    assert normalize_source_hash(h) == h


def test_normalize_source_hash_bytes() -> None:
    """Bytes source_hash is converted to hex string."""
    raw = b"\xab\xcd\xef"
    assert normalize_source_hash(raw) == "abcdef"


def test_normalize_source_hash_bytearray() -> None:
    """Bytearray source_hash is converted to hex string."""
    raw = bytearray(b"\x01\x23\x45")
    assert normalize_source_hash(raw) == "012345"


def test_normalize_source_hash_none() -> None:
    """None input returns None."""
    assert normalize_source_hash(None) is None


def test_normalize_source_hash_empty_string() -> None:
    """Empty string returns None (absent, not malformed)."""
    assert normalize_source_hash("") is None


def test_normalize_source_hash_empty_bytes() -> None:
    """Empty bytes returns None."""
    assert normalize_source_hash(b"") is None


def test_normalize_source_hash_int_returns_none() -> None:
    """Non-bytes/non-string types return None."""
    assert normalize_source_hash(12345) is None


def test_normalize_source_hash_long_hex() -> None:
    """Typical LXMF-length hash (32 hex chars) passes through."""
    h = "ab" * 16
    assert normalize_source_hash(h) == h


# ---------------------------------------------------------------------------
# derive_label
# ---------------------------------------------------------------------------


def test_derive_label_short_hash() -> None:
    """Hash at exactly the truncate threshold is kept in full."""
    h = "a" * 16
    assert derive_label(h) == h


def test_derive_label_below_threshold() -> None:
    """Hash below the truncate threshold is kept in full."""
    h = "abcdef"
    assert derive_label(h) == "abcdef"


def test_derive_label_above_threshold() -> None:
    """Hash above the truncate threshold is truncated with ellipsis."""
    h = "a" * 32
    result = derive_label(h)
    assert result == "a" * 16 + "\u2026"
    assert result.startswith("a" * 16)


def test_derive_label_exactly_threshold_no_ellipsis() -> None:
    """Hash at exactly 16 chars has no ellipsis."""
    h = "0123456789abcdef"
    assert derive_label(h) == h
    assert "\u2026" not in derive_label(h)


def test_derive_label_single_char() -> None:
    """Single character hash is returned as-is."""
    assert derive_label("f") == "f"


# ---------------------------------------------------------------------------
# derive_short_label
# ---------------------------------------------------------------------------


def test_derive_short_label_long_hash() -> None:
    """Long hash produces 8-char short label."""
    h = "abcdef0123456789deadbeef"
    assert derive_short_label(h) == "abcdef01"


def test_derive_short_label_short_hash() -> None:
    """Hash shorter than 8 chars is returned in full."""
    assert derive_short_label("abc") == "abc"


def test_derive_short_label_exactly_8() -> None:
    """Hash at exactly 8 chars is returned in full."""
    h = "01234567"
    assert derive_short_label(h) == h


# ---------------------------------------------------------------------------
# project_lxmf_attribution
# ---------------------------------------------------------------------------


def test_project_with_hex_string_source_hash() -> None:
    """Full projection from hex string source_hash."""
    native = {"source_hash": "ab" * 16}
    attr = project_lxmf_attribution(native)
    assert isinstance(attr, LxmfAttribution)
    assert attr.sender_id == "ab" * 16
    assert attr.label is not None
    assert attr.short_label == "abababab"


def test_project_with_bytes_source_hash() -> None:
    """Full projection from bytes source_hash."""
    native = {"source_hash": b"\xab\xcd\xef\x01\x23\x45\x67\x89"}
    attr = project_lxmf_attribution(native)
    assert attr.sender_id == "abcdef0123456789"
    assert attr.short_label == "abcdef01"


def test_project_missing_source_hash() -> None:
    """Missing source_hash returns all-None attribution."""
    attr = project_lxmf_attribution({})
    assert attr.sender_id is None
    assert attr.label is None
    assert attr.short_label is None


def test_project_empty_source_hash() -> None:
    """Empty string source_hash returns all-None attribution."""
    attr = project_lxmf_attribution({"source_hash": ""})
    assert attr.sender_id is None


def test_project_none_source_hash() -> None:
    """Explicit None source_hash returns all-None attribution."""
    attr = project_lxmf_attribution({"source_hash": None})
    assert attr.sender_id is None


def test_project_ignores_extra_keys() -> None:
    """Extra native keys (destination_hash, etc.) are ignored."""
    native = {
        "source_hash": "deadbeef",
        "destination_hash": "cafebabe",
        "message_id": "msg-001",
    }
    attr = project_lxmf_attribution(native)
    assert attr.sender_id == "deadbeef"
    assert attr.short_label == "deadbeef"


def test_project_label_for_long_hash() -> None:
    """Long hash (32 hex chars) gets truncated label."""
    h = "a" * 32
    attr = project_lxmf_attribution({"source_hash": h})
    assert attr.label == "a" * 16 + "\u2026"


def test_project_label_for_short_hash() -> None:
    """Short hash (8 hex chars) label is the full hash."""
    h = "abcd1234"
    attr = project_lxmf_attribution({"source_hash": h})
    assert attr.label == h


def test_project_frozen() -> None:
    """LxmfAttribution is frozen (immutable)."""
    attr = project_lxmf_attribution({"source_hash": "abcd"})
    try:
        attr.sender_id = "changed"  # type: ignore[misc]
        raise AssertionError("should have raised FrozenInstanceError")
    except AttributeError:
        pass


def test_project_typical_lxmf_hash() -> None:
    """Typical 32-char LXMF hash projects correctly."""
    h = "e9768cd45f12a3b4c5d6e7f8091a2b3c"
    attr = project_lxmf_attribution({"source_hash": h})
    assert attr.sender_id == h
    assert attr.short_label == "e9768cd4"
    # 32 chars > 16 threshold → truncated label
    assert attr.label == "e9768cd45f12a3b4" + "\u2026"


def test_project_bytearray_source_hash() -> None:
    """Bytearray source_hash normalises to hex."""
    native = {"source_hash": bytearray(b"\xde\xad\xbe\xef")}
    attr = project_lxmf_attribution(native)
    assert attr.sender_id == "deadbeef"
