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
    ) -> dict[int, Any]:
        """Insert a MEDRE envelope into *fields* under the envelope key.

        Returns a **new** dict — the original *fields* is not mutated.

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

        Returns
        -------
        dict
            Updated fields dict with the MEDRE envelope inserted.
        """
        updated = dict(fields)
        envelope: dict[str, Any] = {
            "schema_version": 1,
            "event_id": event_id,
            "relations": [
                {
                    "relation_type": getattr(r, "relation_type", None),
                    "target_event_id": getattr(r, "target_event_id", None),
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
