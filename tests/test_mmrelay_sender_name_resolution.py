# medre-test-tier: compatibility
"""Meshtastic sender-name projection into generic attribution.

Tests for ``project_source_fields`` / ``project_meshtastic_attribution``
resolution order per field.

Per-field precedence (sender label / short label resolved independently):

1. Versioned ``native.meshtastic`` identity fields — primary, emitted by
   the codec.
2. ``source_transport_id`` — the transport-neutral identity fallback.
3. ``None``.

Bare adapter-native root keys and Matrix ``displayname`` are not part of
the supported metadata shape and never project into sender identity.
"""

from __future__ import annotations

from medre.adapters.metadata import project_source_fields
from tests.helpers.native_metadata import meshtastic_native_data


def _project(native_data: dict, *, source_transport_id: str | None = None) -> dict:
    return project_source_fields(
        native_data,
        source_adapter="mesh-1",
        source_transport_id=source_transport_id,
    )


def test_meshtastic_longname_is_primary() -> None:
    """Versioned Meshtastic longname resolves to the sender label."""
    fields = _project(meshtastic_native_data({"longname": "Alpha Node"}))
    assert fields["source_sender_label"] == "Alpha Node"


def test_meshtastic_shortname_is_primary() -> None:
    """Versioned Meshtastic shortname resolves to the short sender label."""
    fields = _project(meshtastic_native_data({"shortname": "AN"}))
    assert fields["source_sender_label"] == "AN"
    assert fields["source_sender_short_label"] == "AN"


def test_meshtastic_namespace_ignores_unversioned_root_fields() -> None:
    """Versioned identity wins over unrelated root-level fields."""
    native = {
        **meshtastic_native_data({"longname": "Namespaced Long", "shortname": "NS"}),
        "longname": "Bare Long",
        "shortname": "BS",
    }
    fields = _project(native)
    assert fields["source_sender_label"] == "Namespaced Long"
    assert fields["source_sender_short_label"] == "NS"


def test_transport_id_resolves_when_no_namespaced_identity() -> None:
    """The transport-neutral identity resolves when the namespace is empty."""
    fields = _project(meshtastic_native_data({}), source_transport_id="!node-1")
    assert fields["source_sender_id"] == "!node-1"
    assert fields["source_sender_label"] == "!node-1"


def test_transport_id_fallback_inside_namespace_from_id() -> None:
    """``from_id`` in the namespace wins over the transport-neutral ID."""
    fields = _project(
        meshtastic_native_data({"from_id": "!abcdef01"}),
        source_transport_id="!node-1",
    )
    assert fields["source_sender_id"] == "!abcdef01"


def test_unversioned_root_identity_is_not_interpreted() -> None:
    """Root-level Meshtastic identity is not canonical native metadata.

    Without a versioned namespace the source platform is undetected and
    no sender identity projects at all.
    """
    fields = _project({"longname": "Bare Long", "shortname": "BS"})
    assert fields["source_platform"] is None
    assert "source_sender_label" not in fields
    assert "source_sender_short_label" not in fields


def test_empty_when_no_identity_keys() -> None:
    """No identity keys anywhere yields no projected identity."""
    fields = _project(meshtastic_native_data({}))
    assert fields["source_sender_id"] is None
    assert fields["source_sender_label"] is None
    assert fields["source_sender_short_label"] is None


def test_displayname_does_not_leak() -> None:
    """Matrix displayname never becomes sender identity."""
    fields = _project({"displayname": "Alice Display"})
    assert fields["source_platform"] is None
    assert "source_sender_label" not in fields

    namespaced = meshtastic_native_data({})
    namespaced["displayname"] = "Alice Display"
    fields = _project(namespaced)
    assert fields["source_sender_label"] is None
    assert fields["source_sender_short_label"] is None


def test_longname_and_shortname_resolve_independently() -> None:
    """Each field resolves independently through its own chain."""
    fields = _project(
        meshtastic_native_data({"longname": "Long From Namespace", "shortname": "SN"})
    )
    assert fields["source_sender_label"] == "Long From Namespace"
    assert fields["source_sender_short_label"] == "SN"


def test_compact_short_label_falls_back_to_compact_longname() -> None:
    """The short label falls back to the space-stripped longname."""
    fields = _project(meshtastic_native_data({"longname": "Alpha Node"}))
    assert fields["source_sender_short_label"] == "AlphaNode"
