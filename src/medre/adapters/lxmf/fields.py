"""LXMF fields envelope helper for MEDRE metadata embedding.

:class:`LxmfFieldsHelper` provides static methods to embed and extract
MEDRE metadata envelopes within LXMF message ``fields`` dicts.

The envelope uses a custom field key (``0xFD`` / ``FIELD_CUSTOM_META``)
to carry MEDRE-specific metadata without conflicting with standard LXMF
field keys.

Envelope structure::

    {
        "schema_version": 1,
        "event_id": "<canonical-event-id>",
        "source_adapter": "<adapter-id>",
        "source_transport_id": "<transport-id>" | None,
        "source_channel_id": "<channel-id>" | None,
        "lineage": [...],
        "relations": [...],
        "metadata_keys": [...]
    }
"""
from __future__ import annotations

from typing import Any


LXMF_NAMESPACE: str = "medre"
"""Namespace identifier for MEDRE within LXMF fields."""

FIELD_MEDRE_ENVELOPE: int = 0xFD
"""Custom field key used for the MEDRE metadata envelope.

Uses the FIELD_CUSTOM_META range to avoid conflicts with standard
LXMF field keys.
"""

# Known attachment field keys in LXMF messages.
_ATTACHMENT_FIELD_KEYS: frozenset[int] = frozenset({
    0x05,  # FILE_ATTACHMENTS
    0x06,  # IMAGE
    0x07,  # AUDIO
})


class LxmfFieldsHelper:
    """Static helper for MEDRE envelope operations on LXMF fields dicts.

    All methods are static — this class has no mutable state.
    """

    @staticmethod
    def embed_envelope(
        fields: dict[int, Any],
        event_id: str,
        relations: tuple[Any, ...],
        metadata: dict[str, Any],
        source_adapter: str = "",
        source_transport_id: str | None = None,
        source_channel_id: str | None = None,
        lineage: tuple[str, ...] | None = None,
    ) -> dict[int, Any]:
        """Insert a MEDRE envelope into *fields* under the envelope key.

        Returns a **new** dict — the original *fields* is not mutated.

        The embedded envelope includes provenance data (source_adapter,
        source_transport_id, source_channel_id, lineage) alongside the
        event ID, relations, and metadata keys.

        Relations are serialised as dicts with ``relation_type``,
        ``target_event_id``, ``target_native_ref`` (if present), and
        ``fallback_text``.

        No secrets, private keys, or raw message blobs are embedded.

        Parameters
        ----------
        fields:
            Existing LXMF fields dict (may be empty).
        event_id:
            Canonical event ID to embed.
        relations:
            Event relations tuple.
        metadata:
            Event metadata dict to embed keys from.
        source_adapter:
            Adapter that produced the event.
        source_transport_id:
            Transport-level identity (e.g. source hash).
        source_channel_id:
            Channel identifier, if applicable.
        lineage:
            Tuple of ancestor event IDs.

        Returns
        -------
        dict
            Updated fields dict with the MEDRE envelope inserted.
        """
        updated = dict(fields)

        def _serialise_native_ref(ref: Any) -> dict[str, Any] | None:
            if ref is None:
                return None
            return {
                "adapter": getattr(ref, "adapter", None),
                "native_channel_id": getattr(ref, "native_channel_id", None),
                "native_message_id": getattr(ref, "native_message_id", None),
            }

        envelope: dict[str, Any] = {
            "schema_version": 1,
            "event_id": event_id,
            "source_adapter": source_adapter,
            "source_transport_id": source_transport_id,
            "source_channel_id": source_channel_id,
            "lineage": list(lineage) if lineage else [],
            "relations": [
                {
                    "relation_type": getattr(r, "relation_type", None),
                    "target_event_id": getattr(r, "target_event_id", None),
                    "target_native_ref": _serialise_native_ref(
                        getattr(r, "target_native_ref", None)
                    ),
                    "fallback_text": getattr(r, "fallback_text", None),
                }
                for r in relations
            ] if relations else [],
            "metadata_keys": list(metadata.keys()) if metadata else [],
        }
        updated[FIELD_MEDRE_ENVELOPE] = {LXMF_NAMESPACE: envelope}
        return updated

    @staticmethod
    def extract_envelope(fields: dict[int, Any]) -> dict[str, Any] | None:
        """Extract the MEDRE envelope from *fields*.

        Returns the full envelope dict, or ``None`` if absent, corrupt,
        or missing required fields (``schema_version``, ``event_id``).

        Parameters
        ----------
        fields:
            LXMF fields dict to inspect.

        Returns
        -------
        dict | None
            The envelope dict, or ``None`` if absent or corrupt.
        """
        if not isinstance(fields, dict):
            return None

        raw = fields.get(FIELD_MEDRE_ENVELOPE)
        if raw is None:
            return None

        try:
            if isinstance(raw, dict):
                envelope = raw.get(LXMF_NAMESPACE)
                if isinstance(envelope, dict):
                    # Validate required fields
                    if "schema_version" not in envelope:
                        return None
                    if "event_id" not in envelope:
                        return None
                    return envelope
        except (AttributeError, TypeError):
            pass

        return None

    @staticmethod
    def has_attachment(fields: dict[int, Any]) -> bool:
        """Return ``True`` if *fields* contains attachment-like keys.

        Checks for FILE_ATTACHMENTS (``0x05``), IMAGE (``0x06``),
        and AUDIO (``0x07``) keys.

        Parameters
        ----------
        fields:
            LXMF fields dict to inspect.

        Returns
        -------
        bool
            Whether any attachment key is present.
        """
        if not isinstance(fields, dict):
            return False
        return any(key in fields for key in _ATTACHMENT_FIELD_KEYS)

    @staticmethod
    def envelope_has_relations(envelope: dict[str, Any]) -> bool:
        """Return ``True`` if *envelope* contains non-empty relations.

        Parameters
        ----------
        envelope:
            The MEDRE envelope dict (as returned by
            :meth:`extract_envelope`).

        Returns
        -------
        bool
            Whether the envelope has at least one relation entry.
        """
        return bool(envelope.get("relations"))
