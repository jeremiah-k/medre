"""Matrix metadata envelope for embedding provenance in message content.

The :class:`MatrixMetadataEnvelope` carries MEDRE provenance information
inside the ``content["medre"]["envelope"]`` subtree of a Matrix event.
This allows events to carry their routing lineage across the Matrix
protocol without leaking secrets.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class MatrixMetadataEnvelope:
    """Immutable metadata envelope embedded in Matrix message content.

    Attributes
    ----------
    schema_version:
        Schema version of the envelope structure.
    canonical_event_id:
        The canonical event ID this message corresponds to.
    source_adapter:
        The adapter that produced the original event.
    source_channel:
        The source channel / room ID.
    provenance:
        Provenance descriptor string.
    relation_info:
        Relation type descriptor (e.g. ``"reply"``, ``"reaction"``).
    lineage_pointer:
        Optional pointer to a parent lineage event ID.
    metadata_mode:
        The embedding mode used (``"safe"``, ``"minimal"``, etc.).
    native_source_summary:
        Brief human-readable summary of the native source.
    """

    schema_version: int = 1
    canonical_event_id: str = ""
    source_adapter: str = ""
    source_channel: str = ""
    provenance: str = ""
    relation_info: str = ""
    lineage_pointer: str = ""
    metadata_mode: str = "safe"
    native_source_summary: str = ""

    @classmethod
    def from_content(cls, content: dict) -> Optional[MatrixMetadataEnvelope]:
        """Extract an envelope from a Matrix event content dict.

        Expects the ``content["medre"]["envelope"]`` subtree.  Returns
        ``None`` if the key is missing or the data is corrupt.

        Parameters
        ----------
        content:
            The Matrix event ``content`` dictionary.

        Returns
        -------
        MatrixMetadataEnvelope | None
            The extracted envelope, or ``None`` on missing / corrupt data.
        """
        try:
            envelope_data = content["medre"]["envelope"]
        except (KeyError, TypeError):
            return None

        if not isinstance(envelope_data, dict):
            return None

        try:
            return cls(
                schema_version=envelope_data.get("schema_version", 1),
                canonical_event_id=envelope_data.get("canonical_event_id", ""),
                source_adapter=envelope_data.get("source_adapter", ""),
                source_channel=envelope_data.get("source_channel", ""),
                provenance=envelope_data.get("provenance", ""),
                relation_info=envelope_data.get("relation_info", ""),
                lineage_pointer=envelope_data.get("lineage_pointer", ""),
                metadata_mode=envelope_data.get("metadata_mode", "safe"),
                native_source_summary=envelope_data.get("native_source_summary", ""),
            )
        except (TypeError, ValueError):
            return None

    def to_content(self) -> dict:
        """Serialise the envelope into a Matrix content subtree.

        Returns a dict suitable for merging into a Matrix event content
        object under the ``"medre"`` key.  **Never** includes access
        tokens or secrets.

        Returns
        -------
        dict
            ``{"medre": {"envelope": {...}}}``
        """
        return {
            "medre": {
                "envelope": {
                    "schema_version": self.schema_version,
                    "canonical_event_id": self.canonical_event_id,
                    "source_adapter": self.source_adapter,
                    "source_channel": self.source_channel,
                    "provenance": self.provenance,
                    "relation_info": self.relation_info,
                    "lineage_pointer": self.lineage_pointer,
                    "metadata_mode": self.metadata_mode,
                    "native_source_summary": self.native_source_summary,
                }
            }
        }
