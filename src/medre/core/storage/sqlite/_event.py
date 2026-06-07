"""Event CRUD mixins for SQLiteStorage.

Authority surface:
  - append:  **create** (append-only).  Canonical events are ingress facts;
    once persisted they are never updated or deleted by runtime code.
  - get:     **list/get** (read-only).
  - query:   **list/get** (read-only).
"""

from __future__ import annotations

from typing import Any, AsyncGenerator

from medre.core.events import CanonicalEvent, EventRelation
from medre.core.storage.backend import EventFilter
from medre.core.storage.sqlite.query import _build_query_sql
from medre.core.storage.sqlite.serde import (
    _encode_json,
    _now_iso,
    _row_to_event,
    _row_to_relation,
    _serialize_metadata,
)
from medre.core.storage.sqlite.statements import (
    _INSERT_EVENT,
    _SELECT_EVENT,
    _SELECT_RELATIONS,
)


class _EventMixin:
    """Event CRUD methods for SQLiteStorage.

    Accesses ``self._write_batch``, ``self._read_one``, ``self._read_all``,
    and ``self._relation_op`` from the base and sibling mixins via MRO.
    """

    async def append(self, event: CanonicalEvent) -> None:
        """Persist a canonical event together with its inline relations.

        Authority: **create** (append-only).
        """
        snr = event.source_native_ref
        ops: list[tuple[str, tuple[Any, ...]]] = [
            (
                _INSERT_EVENT,
                (
                    event.event_id,
                    event.event_kind,
                    event.schema_version,
                    event.timestamp.isoformat(),
                    event.source_adapter,
                    event.source_transport_id,
                    event.source_channel_id,
                    event.parent_event_id,
                    _encode_json(event.lineage),
                    _encode_json(event.payload),
                    _serialize_metadata(event.metadata),
                    event.depth,
                    event.trace_id,
                    event.root_event_id,
                    event.conversation_id,
                    snr.adapter if snr else None,
                    snr.native_channel_id if snr else None,
                    snr.native_message_id if snr else None,
                    snr.native_thread_id if snr else None,
                    _now_iso(),
                ),
            )
        ]
        for rel in event.relations:
            ops.append(self._relation_op(event.event_id, rel))
        await self._write_batch(ops)

    async def get(self, event_id: str) -> CanonicalEvent | None:
        """Retrieve a single event by ID, including its relations.

        Authority: **list/get** (read-only).
        """
        row = await self._read_one(_SELECT_EVENT, (event_id,))
        if row is None:
            return None
        rel_rows = await self._read_all(_SELECT_RELATIONS, (event_id,))
        return _row_to_event(row, [_row_to_relation(r) for r in rel_rows])

    async def query(
        self, event_filter: EventFilter
    ) -> AsyncGenerator[CanonicalEvent, None]:
        """Yield events matching *event_filter*, ordered by timestamp ascending.

        Authority: **list/get** (read-only).
        """
        sql, params = _build_query_sql(event_filter)
        rows = await self._read_all(sql, params)
        if not rows:
            return

        # Fetch relations for all matched events in bounded batches.
        # SQLite's host-parameter limit is 999 in older builds; chunk
        # well below that ceiling.
        event_ids = [r["event_id"] for r in rows]
        rel_map: dict[str, list[EventRelation]] = {}
        _CHUNK_SIZE = 900
        for offset in range(0, len(event_ids), _CHUNK_SIZE):
            chunk = event_ids[offset : offset + _CHUNK_SIZE]
            placeholders = ",".join("?" for _ in chunk)
            rel_sql = (
                "SELECT * FROM event_relations WHERE event_id "
                f"IN ({placeholders})"  # nosec: placeholders are only ? markers, values passed as params
            )
            rel_rows = await self._read_all(rel_sql, tuple(chunk))
            for rr in rel_rows:
                rel_map.setdefault(rr["event_id"], []).append(_row_to_relation(rr))

        for row in rows:
            yield _row_to_event(row, rel_map.get(row["event_id"], []))
