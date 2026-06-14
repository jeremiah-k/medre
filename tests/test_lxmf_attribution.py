"""Tests for LXMF native-to-generic attribution projection.

Function-style tests covering:
- ``project_lxmf_attribution`` main entry point (dict return).
- ``normalize_source_hash`` bytes/str normalisation.
- Display-name-driven label projection with strict label typing:
  only ``str`` / ``bytes`` / ``bytearray`` values populate label
  fields; other types (int, dict, list, ...) yield ``None`` rather
  than being coerced via ``str()``.
- Edge cases: absent keys, empty values, non-text types, bytes
  source_hash.

Policy under test: the opaque ``source_hash`` projects to
``source_sender_id`` only and is never used as a human-readable label.
``source_sender_label`` / ``source_sender_short_label`` are populated
from ``lxmf.display_name`` / ``lxmf.short_name`` when present (and
text-bearing) and remain ``None`` otherwise so that ``{sender}``
renders empty rather than a truncated hash.
"""

from __future__ import annotations

from medre.adapters.lxmf.attribution import (
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
# project_lxmf_attribution — source_hash → sender_id (no labels)
# ---------------------------------------------------------------------------


def test_project_with_hex_string_source_hash() -> None:
    """source_hash projects to source_sender_id; labels stay None."""
    native = {"source_hash": "ab" * 16}
    fields = project_lxmf_attribution(native)
    assert fields["source_sender_id"] == "ab" * 16
    assert fields["source_sender_label"] is None
    assert fields["source_sender_short_label"] is None


def test_project_with_bytes_source_hash() -> None:
    """Bytes source_hash normalises to hex for sender_id."""
    native = {"source_hash": b"\xab\xcd\xef\x01\x23\x45\x67\x89"}
    fields = project_lxmf_attribution(native)
    assert fields["source_sender_id"] == "abcdef0123456789"
    assert fields["source_sender_label"] is None
    assert fields["source_sender_short_label"] is None


def test_project_missing_source_hash() -> None:
    """Missing source_hash returns all-None fields."""
    fields = project_lxmf_attribution({})
    assert fields["source_sender_id"] is None
    assert fields["source_sender_label"] is None
    assert fields["source_sender_short_label"] is None


def test_project_empty_source_hash() -> None:
    """Empty string source_hash returns None sender_id."""
    fields = project_lxmf_attribution({"source_hash": ""})
    assert fields["source_sender_id"] is None
    assert fields["source_sender_label"] is None
    assert fields["source_sender_short_label"] is None


def test_project_none_source_hash() -> None:
    """Explicit None source_hash returns None sender_id."""
    fields = project_lxmf_attribution({"source_hash": None})
    assert fields["source_sender_id"] is None


def test_project_bytearray_source_hash() -> None:
    """Bytearray source_hash normalises to hex."""
    native = {"source_hash": bytearray(b"\xde\xad\xbe\xef")}
    fields = project_lxmf_attribution(native)
    assert fields["source_sender_id"] == "deadbeef"


def test_project_ignores_extra_keys() -> None:
    """Extra native keys are ignored; labels stay None without display name."""
    native = {
        "source_hash": "deadbeef",
        "destination_hash": "cafebabe",
        "message_id": "msg-001",
    }
    fields = project_lxmf_attribution(native)
    assert fields["source_sender_id"] == "deadbeef"
    assert fields["source_sender_label"] is None
    assert fields["source_sender_short_label"] is None


def test_project_typical_lxmf_hash() -> None:
    """Typical 32-char LXMF hash projects to sender_id, labels None."""
    h = "e9768cd45f12a3b4c5d6e7f8091a2b3c"
    fields = project_lxmf_attribution({"source_hash": h})
    assert fields["source_sender_id"] == h
    assert fields["source_sender_label"] is None
    assert fields["source_sender_short_label"] is None


def test_project_returns_dict() -> None:
    """project_lxmf_attribution returns a plain dict, not a dataclass."""
    fields = project_lxmf_attribution({"source_hash": "abcd"})
    assert isinstance(fields, dict)
    # All three canonical keys are always present.
    assert set(fields.keys()) == {
        "source_sender_id",
        "source_sender_label",
        "source_sender_short_label",
    }


# ---------------------------------------------------------------------------
# project_lxmf_attribution — display name → labels
# ---------------------------------------------------------------------------


def test_project_display_name_populates_labels() -> None:
    """Display name populates sender_label; short label derived compact."""
    native = {
        "source_hash": "ab" * 16,
        "lxmf.display_name": "Alice Walker",
    }
    fields = project_lxmf_attribution(native)
    assert fields["source_sender_id"] == "ab" * 16
    assert fields["source_sender_label"] == "Alice Walker"
    assert fields["source_sender_short_label"] == "AliceWalker"


def test_project_display_name_and_short_name() -> None:
    """Both display_name and short_name populate their respective labels."""
    native = {
        "source_hash": "cd" * 16,
        "lxmf.display_name": "Alice Walker",
        "lxmf.short_name": "AW",
    }
    fields = project_lxmf_attribution(native)
    assert fields["source_sender_label"] == "Alice Walker"
    assert fields["source_sender_short_label"] == "AW"


def test_project_short_name_only() -> None:
    """Short name alone populates short_label but not sender_label."""
    native = {
        "source_hash": "ef" * 16,
        "lxmf.short_name": "Bob",
    }
    fields = project_lxmf_attribution(native)
    assert fields["source_sender_label"] is None
    assert fields["source_sender_short_label"] == "Bob"


def test_project_display_name_empty_string() -> None:
    """Empty display_name leaves labels None."""
    native = {
        "source_hash": "ab" * 16,
        "lxmf.display_name": "",
    }
    fields = project_lxmf_attribution(native)
    assert fields["source_sender_id"] == "ab" * 16
    assert fields["source_sender_label"] is None
    assert fields["source_sender_short_label"] is None


def test_project_display_name_none() -> None:
    """Explicit None display_name leaves labels None."""
    native = {
        "source_hash": "ab" * 16,
        "lxmf.display_name": None,
    }
    fields = project_lxmf_attribution(native)
    assert fields["source_sender_label"] is None


def test_project_display_name_single_word() -> None:
    """Single-word display name: compact form equals the name itself."""
    native = {
        "source_hash": "ab" * 16,
        "lxmf.display_name": "Alice",
    }
    fields = project_lxmf_attribution(native)
    assert fields["source_sender_label"] == "Alice"
    assert fields["source_sender_short_label"] == "Alice"


def test_project_display_name_bytes() -> None:
    """Bytes display_name is decoded as UTF-8."""
    native = {
        "source_hash": "ab" * 16,
        "lxmf.display_name": "Café".encode("utf-8"),
    }
    fields = project_lxmf_attribution(native)
    assert fields["source_sender_label"] == "Café"


def test_int_display_name_not_coerced() -> None:
    """Non-text display_name (int) is not coerced; label stays None."""
    native = {
        "source_hash": "ab" * 16,
        "lxmf.display_name": 12345,
    }
    fields = project_lxmf_attribution(native)
    # Strict label typing: int is rejected, not coerced via str().
    assert fields["source_sender_label"] is None
    assert fields["source_sender_short_label"] is None


def test_dict_display_name_not_coerced() -> None:
    """Non-text display_name (dict) is not coerced; label stays None."""
    native = {
        "source_hash": "ab" * 16,
        "lxmf.display_name": {"key": "val"},
    }
    fields = project_lxmf_attribution(native)
    # Strict label typing: dict is rejected, not coerced via str().
    assert fields["source_sender_label"] is None
    assert fields["source_sender_short_label"] is None


def test_bytes_display_name_decoded() -> None:
    """bytes display_name is decoded as UTF-8 to a real label."""
    native = {
        "source_hash": b"\xab",
        "lxmf.display_name": b"Alice",
    }
    fields = project_lxmf_attribution(native)
    assert fields["source_sender_id"] == "ab"
    assert fields["source_sender_label"] == "Alice"
    assert fields["source_sender_short_label"] == "Alice"


def test_short_name_int_not_coerced() -> None:
    """Non-text short_name (int) is not coerced; short label stays None."""
    native = {
        "source_hash": "ab" * 16,
        "lxmf.display_name": "Alice",
        "lxmf.short_name": 99,
    }
    fields = project_lxmf_attribution(native)
    # display_name still projects; int short_name is rejected.
    assert fields["source_sender_label"] == "Alice"
    # Falls back to compact form of display_name, not str(99).
    assert fields["source_sender_short_label"] == "Alice"


def test_project_short_name_empty_falls_back_to_compact() -> None:
    """Empty short_name falls back to compact display_name."""
    native = {
        "source_hash": "ab" * 16,
        "lxmf.display_name": "Alice Walker",
        "lxmf.short_name": "",
    }
    fields = project_lxmf_attribution(native)
    assert fields["source_sender_label"] == "Alice Walker"
    assert fields["source_sender_short_label"] == "AliceWalker"


def test_project_hash_never_becomes_label() -> None:
    """Opaque source_hash never appears in label fields."""
    h = "e9768cd45f12a3b4c5d6e7f8091a2b3c"
    fields = project_lxmf_attribution({"source_hash": h})
    # The hash must not leak into label fields.
    assert fields["source_sender_label"] is None
    assert fields["source_sender_short_label"] is None
    assert fields["source_sender_id"] == h


# ---------------------------------------------------------------------------
# project_lxmf_attribution — short_name edge cases
# ---------------------------------------------------------------------------


def test_project_display_name_spaces_only() -> None:
    """Display name that is only spaces: label set, compact form None."""
    native = {
        "source_hash": "ab" * 16,
        "lxmf.display_name": "   ",
    }
    fields = project_lxmf_attribution(native)
    # "   " is non-empty so label is "   ".
    assert fields["source_sender_label"] == "   "
    # Compact form strips spaces → empty → None.
    assert fields["source_sender_short_label"] is None


def test_project_all_fields_combined() -> None:
    """Realistic native dict with display name and short name."""
    native = {
        "source_hash": b"\xe9\x76\x8c\xd4\x5f\x12\xa3\xb4",
        "destination_hash": "00" * 16,
        "message_id": "ff" * 32,
        "lxmf.display_name": "Mesh Node Alpha",
        "lxmf.short_name": "MNA",
    }
    fields = project_lxmf_attribution(native)
    assert fields["source_sender_id"] == "e9768cd45f12a3b4"
    assert fields["source_sender_label"] == "Mesh Node Alpha"
    assert fields["source_sender_short_label"] == "MNA"
