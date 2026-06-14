"""mmrelay sender-name resolution (KEY_LONGNAME / KEY_SHORTNAME).

Tests for ``MatrixRenderer._resolve_mmrelay_sender_names`` resolution
order per field.

Per-field precedence (longname / shortname each resolved independently):

1. Meshtastic-native namespaced (``meshtastic.longname`` /
   ``meshtastic.shortname``) — primary, emitted by the codec.
2. External mmrelay wire (``meshtastic_longname`` /
   ``meshtastic_shortname``).
3. mmrelay KEY constants (:data:`KEY_LONGNAME` / :data:`KEY_SHORTNAME`).
4. Legacy bare keys (``longname`` / ``shortname``) — input tolerance.
5. Empty string.

Matrix ``displayname`` never projects into these wire fields.
"""

from __future__ import annotations

from medre.adapters.matrix.renderer import MatrixRenderer
from medre.interop.mmrelay import KEY_LONGNAME, KEY_SHORTNAME


def test_namespaced_longname_is_primary() -> None:
    """Namespaced meshtastic.longname resolves to KEY_LONGNAME."""
    longname, _ = MatrixRenderer._resolve_mmrelay_sender_names(
        {"meshtastic.longname": "Alpha Node"}
    )
    assert longname == "Alpha Node"


def test_namespaced_shortname_is_primary() -> None:
    """Namespaced meshtastic.shortname resolves to KEY_SHORTNAME."""
    _, shortname = MatrixRenderer._resolve_mmrelay_sender_names(
        {"meshtastic.shortname": "AN"}
    )
    assert shortname == "AN"


def test_namespaced_wins_over_bare() -> None:
    """When both namespaced and bare keys exist, namespaced wins."""
    longname, shortname = MatrixRenderer._resolve_mmrelay_sender_names(
        {
            "meshtastic.longname": "Namespaced Long",
            "longname": "Bare Long",
            "meshtastic.shortname": "NS",
            "shortname": "BS",
        }
    )
    assert longname == "Namespaced Long"
    assert shortname == "NS"


def test_wire_key_resolves_when_no_namespaced() -> None:
    """External mmrelay wire fields resolve when namespaced is absent."""
    longname, shortname = MatrixRenderer._resolve_mmrelay_sender_names(
        {
            "meshtastic_longname": "Wire Long",
            "meshtastic_shortname": "WS",
        }
    )
    assert longname == "Wire Long"
    assert shortname == "WS"


def test_key_constant_resolves() -> None:
    """KEY_LONGNAME / KEY_SHORTNAME constant-named keys resolve."""
    longname, shortname = MatrixRenderer._resolve_mmrelay_sender_names(
        {KEY_LONGNAME: "Const Long", KEY_SHORTNAME: "CS"}
    )
    assert longname == "Const Long"
    assert shortname == "CS"


def test_bare_key_legacy_tolerance() -> None:
    """Bare longname/shortname still resolve (legacy input tolerance)."""
    longname, shortname = MatrixRenderer._resolve_mmrelay_sender_names(
        {"longname": "Bare Long", "shortname": "BS"}
    )
    assert longname == "Bare Long"
    assert shortname == "BS"


def test_namespaced_wins_over_wire() -> None:
    """Namespaced takes precedence over external mmrelay wire fields."""
    longname, _ = MatrixRenderer._resolve_mmrelay_sender_names(
        {
            "meshtastic.longname": "Primary",
            "meshtastic_longname": "Wire",
        }
    )
    assert longname == "Primary"


def test_wire_wins_over_bare() -> None:
    """External mmrelay wire fields take precedence over legacy bare."""
    longname, _ = MatrixRenderer._resolve_mmrelay_sender_names(
        {
            "meshtastic_longname": "Wire",
            "longname": "Bare",
        }
    )
    assert longname == "Wire"


def test_empty_when_no_identity_keys() -> None:
    """No identity keys anywhere yields empty strings."""
    longname, shortname = MatrixRenderer._resolve_mmrelay_sender_names({})
    assert longname == ""
    assert shortname == ""


def test_displayname_does_not_leak() -> None:
    """Matrix displayname never populates KEY_LONGNAME/KEY_SHORTNAME."""
    longname, shortname = MatrixRenderer._resolve_mmrelay_sender_names(
        {"displayname": "Alice Display"}
    )
    assert longname == ""
    assert shortname == ""


def test_longname_and_shortname_resolve_independently() -> None:
    """Each field resolves independently through its own chain."""
    longname, shortname = MatrixRenderer._resolve_mmrelay_sender_names(
        {
            "meshtastic.longname": "Long From Namespaced",
            "shortname": "Short From Bare",
        }
    )
    assert longname == "Long From Namespaced"
    assert shortname == "Short From Bare"
