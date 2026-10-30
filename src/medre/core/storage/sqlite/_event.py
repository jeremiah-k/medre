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
        """Retrieve event identity and relations, without body or excerpts."""
        row = await self._read_one(_SELECT_EVENT, (event_id,))
        if row is None:
            return None
        relations = await self._read_all(
            "SELECT * FROM event_relations WHERE event_id=? ORDER BY id ASC",
            (event_id,),
        )
        return _row_to_event(row, [_row_to_relation(r) for r in relations])

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
        """Retrieve a single event by ID, including its relations.

        Authority: **list/get** (read-only).
        """
        row = await self._read_one(_SELECT_EVENT, (event_id,))
        if row is None:
            return None
        rel_rows = await self._read_all(_SELECT_RELATIONS, (event_id,))
        body_rows = await self._read_all(
            "SELECT body, field FROM event_body_content WHERE event_id=?", (event_id,)
        )
        result = _row_to_event(row, [_row_to_relation(r) for r in rel_rows])
        if not body_rows:
            return result
        from msgspec.structs import replace

        return replace(
            result,
            payload={**result.payload, **{r["field"]: r["body"] for r in body_rows}},
        )

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
        # Some SQLite builds retain a conservative 999 host-parameter limit;
        # chunk reads to stay below it.
        event_ids = [r["event_id"] for r in rows]
        rel_map: dict[str, list[EventRelation]] = {}
        _CHUNK_SIZE = 900
        content_map: dict[str, dict[str, str]] = {}
        for offset in range(0, len(event_ids), _CHUNK_SIZE):
            chunk = event_ids[offset : offset + _CHUNK_SIZE]
            placeholders = ",".join("?" for _ in chunk)
            rel_sql = (
                "SELECT r.*, x.excerpt AS resolved_fallback_text "
                "FROM event_relations r "
                "LEFT JOIN event_relation_excerpts x ON x.relation_id=r.id "
                "WHERE r.event_id "
                f"IN ({placeholders})"  # nosec: placeholders are only ? markers, values passed as params
            )
            rel_rows = await self._read_all(rel_sql, tuple(chunk))
            for rr in rel_rows:
                rel_map.setdefault(rr["event_id"], []).append(_row_to_relation(rr))
            content_sql = (
                "SELECT event_id, field, body FROM event_body_content "
                f"WHERE event_id IN ({placeholders})"  # nosec: placeholders are only ? markers, values passed as params
            )
            content_rows = await self._read_all(content_sql, tuple(chunk))
            for content_row in content_rows:
                content_map.setdefault(content_row["event_id"], {})[
                    content_row["field"]
                ] = content_row["body"]

        # General query consumers still receive retained content. Selected
        # runtime paths use the explicit owner-bound read APIs instead.
        from msgspec.structs import replace

        for row in rows:
            result = _row_to_event(row, rel_map.get(row["event_id"], []))
            content = content_map.get(row["event_id"])
            yield (
                replace(result, payload={**result.payload, **content})
                if content
                else result
            )
