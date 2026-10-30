"""Event CRUD mixins for SQLiteStorage.

Authority surface:
  - append:  **create** (append-only).  Canonical events are ingress facts;
    once persisted they are never updated or deleted by runtime code.
  - get:     **list/get** (read-only).
  - query:   **list/get** (read-only).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, AsyncGenerator

from medre.core.events import CanonicalEvent, EventRelation
from medre.core.storage.backend import EventFilter
from medre.core.storage.sqlite._content_identity import (
    body_admission_op,
    body_content_ops,
    envelope_payload,
    ingress_dependency_op,
)
from medre.core.storage.sqlite._projections import EVENT_COLUMNS, RELATION_COLUMNS
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
)


class _EventMixin:
    """Event CRUD methods for SQLiteStorage.

    Accesses ``self._write_batch``, ``self._read_one``, ``self._read_all``,
    and ``self._relation_op`` from the base and sibling mixins via MRO.
    """

    if TYPE_CHECKING:

        async def _write_batch(
            self, ops: list[tuple[str, tuple[Any, ...]]]
        ) -> None: ...

        async def _read_one(
            self, sql: str, params: tuple[Any, ...] = ()
        ) -> dict[str, Any] | None: ...

        async def _read_all(
            self, sql: str, params: tuple[Any, ...] = ()
        ) -> list[dict[str, Any]]: ...

        @staticmethod
        def _relation_excerpt_op(
            relation: EventRelation,
        ) -> tuple[str, tuple[Any, ...]] | None: ...

        @staticmethod
        def _relation_op(
            event_id: str, relation: EventRelation
        ) -> tuple[str, tuple[Any, ...]]: ...

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
                    _encode_json(envelope_payload(event)),
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
        ops.append(body_admission_op(event, _now_iso()))
        ops.extend(body_content_ops(event, _now_iso()))
        for rel in event.relations:
            ops.append(self._relation_op(event.event_id, rel))
            excerpt_op = self._relation_excerpt_op(rel)
            if excerpt_op is not None:
                ops.append(excerpt_op)
        ops.append(ingress_dependency_op(event, _now_iso(), owner_kind="append"))
        await self._write_batch(ops)

    async def get_envelope(self, event_id: str) -> CanonicalEvent | None:
        """Fetch an event's metadata-only projection; no content grant implied."""
        row = await self._read_one(
            f"SELECT {EVENT_COLUMNS} FROM canonical_events WHERE event_id=?",  # nosec B608 - closed schema projection; all values use bound parameters.
            (event_id,),
        )
        if row is None:
            return None
        relations = await self._read_all(
            f"SELECT {RELATION_COLUMNS} FROM event_relations "  # nosec B608 - closed schema projection; all values use bound parameters.
            "WHERE event_id=? ORDER BY id ASC",
            (event_id,),
        )
        event = _row_to_event(row, [_row_to_relation(r) for r in relations])
        return event

    async def get_metadata(self, event_id: str) -> CanonicalEvent | None:
        """Read non-content identity; use owner-bound APIs for rendering."""
        return await self.get_envelope(event_id)

    async def event_exists(self, event_id: str) -> bool:
        """Check canonical identity without decoding payload or relation content.

        This read-only metadata operation makes no claim about content
        availability or the validity of the event's serialized body.
        """
        row = await self._read_one(
            "SELECT 1 AS present FROM canonical_events WHERE event_id=?", (event_id,)
        )
        return row is not None

    async def get(self, event_id: str) -> CanonicalEvent | None:
        """Return metadata only. Full content requires an authorized owner read."""
        return await self.get_metadata(event_id)

    async def list_event_ids_page(
        self, *, after_event_id: str | None, limit: int
    ) -> list[str]:
        """Return one bounded lexicographic page of canonical event IDs."""
        if limit < 1:
            raise ValueError("event ID page limit must be positive")
        if after_event_id is None:
            rows = await self._read_all(
                "SELECT event_id FROM canonical_events "
                "ORDER BY event_id ASC LIMIT ?",
                (limit,),
            )
        else:
            rows = await self._read_all(
                "SELECT event_id FROM canonical_events "
                "WHERE event_id > ? ORDER BY event_id ASC LIMIT ?",
                (after_event_id, limit),
            )
        return [str(row["event_id"]) for row in rows]

    async def query_metadata(
        self, event_filter: EventFilter
    ) -> AsyncGenerator[CanonicalEvent, None]:
        """Select metadata without loading body/excerpt tables or opaque fields."""
        sql, params = _build_query_sql(event_filter)
        rows = await self._read_all(sql, params)
        ids = [row["event_id"] for row in rows]
        rel_map: dict[str, list[EventRelation]] = {}
        for offset in range(0, len(ids), 900):
            chunk = ids[offset : offset + 900]
            placeholders = ",".join("?" for _ in chunk)
            relations = await self._read_all(
                f"SELECT {RELATION_COLUMNS}, event_id FROM event_relations "  # nosec B608 - closed schema projection; all values use bound parameters.
                f"WHERE event_id IN ({placeholders}) ORDER BY id ASC",
                tuple(chunk),
            )
            for row in relations:
                rel_map.setdefault(row["event_id"], []).append(_row_to_relation(row))
        for row in rows:
            yield _row_to_event(row, rel_map.get(row["event_id"], []))

    async def query(
        self, event_filter: EventFilter
    ) -> AsyncGenerator[CanonicalEvent, None]:
        """Return metadata-only results, not an implicit content grant."""
        async for event in self.query_metadata(event_filter):
            yield event
