"""Current read contracts for canned-event planning test backends."""

from msgspec.structs import replace

from medre.core.events.metadata import EventMetadata
from medre.core.events.text import select_message_body
from medre.core.storage.read_contracts import AuthorizedEventRead, ReplyContextRead


class CannedContentReads:
    """Stored fixtures are explicitly authorized; generic reads stay metadata-only."""

    async def get(self, event_id):
        return await self.get_metadata(event_id)

    async def get_metadata(self, event_id):
        event = await self._read_event(event_id)
        return (
            None
            if event is None
            else replace(
                event,
                payload={},
                metadata=EventMetadata(routing=event.metadata.routing),
                relations=tuple(
                    replace(
                        relation,
                        key=None,
                        fallback_text=None,
                        metadata={},
                        target_fact=None,
                    )
                    for relation in event.relations
                ),
            )
        )

    async def read_reply_context(self, event_id, *, now=None):
        event = await self._read_event(event_id)
        if event is None:
            return ReplyContextRead("denied")
        present = isinstance(event.payload.get("body"), str) or (
            "body" not in event.payload and isinstance(event.payload.get("text"), str)
        )
        body = select_message_body(event.payload, strict=False) if present else None
        event = replace(
            event, payload={} if body is None else {"body": body}, relations=()
        )
        return ReplyContextRead("unavailable" if body is None else "retained", event)

    async def read_event_for_replay(self, event_id, *, now=None):
        event = await self._read_event(event_id)
        return AuthorizedEventRead("denied" if event is None else "retained", event)


def inspection_rows(value, table, metadata_only):
    """Withhold opaque fields in deliberate evidence doubles as storage does."""
    if not metadata_only or value is None:
        return value
    if isinstance(value, list | tuple):
        return [inspection_rows(row, table, True) for row in value]
    import copy
    import dataclasses

    import msgspec

    excluded = {
        "delivery_receipts": {
            "error": None,
            "capability_reason": None,
            "rendering_evidence": None,
        },
        "native_message_refs": {"metadata": {}},
        "delivery_observations": {"error": None, "metadata": {}},
        "delivery_outbox": {
            "error_summary": None,
            "failure_kind_detail": None,
            "metadata": {},
        },
    }[table]
    if isinstance(value, dict):
        return {
            **value,
            **{
                key: replacement
                for key, replacement in excluded.items()
                if key in value
            },
        }
    changes = {
        key: replacement for key, replacement in excluded.items() if hasattr(value, key)
    }
    if isinstance(value, msgspec.Struct):
        return msgspec.structs.replace(value, **changes)
    if dataclasses.is_dataclass(value):
        return dataclasses.replace(value, **changes)
    result = copy.copy(value)
    for key, replacement in changes.items():
        setattr(result, key, replacement)
    return result
