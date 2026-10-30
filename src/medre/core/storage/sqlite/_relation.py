"""Relation mixins for SQLiteStorage.

Authority surface:
  - _relation_op:          internal (builds SQL/params, no I/O).
  - store_relation:        **create** (append-only alongside events).
  - list_relations:        **list/get** (forward relation read).
  - list_relation_sources: **list/get** (reverse relation read).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from msgspec.structs import replace

from medre.core.events import EventRelation
from medre.core.storage.sqlite._projections import RELATION_COLUMNS
from medre.core.storage.sqlite.serde import _encode_json, _now_iso, _row_to_relation
from medre.core.storage.sqlite.statements import (
    _INSERT_RELATION,
    _SELECT_RELATION_SOURCES,
    _SELECT_RELATION_SOURCES_FOR_NATIVE_REF,
)


class _RelationMixin:
    """Relation methods for SQLiteStorage.

    Accesses ``self._write`` and ``self._read_all`` from the base class
    via MRO.
    """

    if TYPE_CHECKING:

        async def _write_batch(
            self, ops: list[tuple[str, tuple[Any, ...]]]
        ) -> None: ...

        async def _read_all(
            self, sql: str, params: tuple[Any, ...] = ()
        ) -> list[dict[str, Any]]: ...

    @staticmethod
    def _relation_op(
        event_id: str, relation: EventRelation
    ) -> tuple[str, tuple[Any, ...]]:
        """Build an ``(sql, params)`` pair for inserting a single relation."""
        nref = relation.target_native_ref
        return (
            _INSERT_RELATION,
            (
                event_id,
                relation.relation_type,
                relation.target_event_id,
                nref.adapter if nref else None,
                nref.native_channel_id if nref else None,
                nref.native_message_id if nref else None,
                nref.native_thread_id if nref else None,
                relation.key,
                _encode_json(relation.metadata),
                _now_iso(),
            ),
        )

    @staticmethod
    def _relation_excerpt_op(
        relation: EventRelation,
    ) -> tuple[str, tuple[Any, ...]] | None:
        if relation.fallback_text is None:
            return None
        return (
            "INSERT INTO event_relation_excerpts (relation_id, excerpt) "
            "VALUES (last_insert_rowid(), ?)",
            (relation.fallback_text,),
        )

    async def store_relation(self, event_id: str, relation: EventRelation) -> None:
        """Persist a single relation for an existing event.

        Authority: **create**.
        """
        ops = [self._relation_op(event_id, relation)]
        excerpt_op = self._relation_excerpt_op(relation)
        if excerpt_op is not None:
            ops.append(excerpt_op)
            # Admission already owns the full event. Appending a relation
            # must never recreate a missing or released source dependency.
        await self._write_batch(ops)

    async def list_relations(self, event_id: str) -> list[EventRelation]:
        """Return all relations belonging to *event_id*.

        Authority: **list/get** (read-only).
        """
        rows = await self._read_all(
            f"SELECT {RELATION_COLUMNS} FROM event_relations "  # nosec B608 - closed schema projection; all values use bound parameters.
            "WHERE event_id=? ORDER BY id ASC",
            (event_id,),
        )
        return [
            replace(
                _row_to_relation(r), fallback_text=None, metadata={}, target_fact=None
            )
            for r in rows
        ]

    async def list_relation_sources(self, target_event_id: str) -> list[str]:
        """Return unique source event IDs targeting *target_event_id*.

        Results are ordered by the first matching relation row so reverse
        traversal is deterministic.
        """
        rows = await self._read_all(_SELECT_RELATION_SOURCES, (target_event_id,))
        return [str(row["event_id"]) for row in rows]

    async def list_relation_sources_for_native_ref(
        self,
        adapter: str,
        native_channel_id: str | None,
        native_message_id: str,
    ) -> list[str]:
        """Return relation-source events targeting one native identity."""
        rows = await self._read_all(
            _SELECT_RELATION_SOURCES_FOR_NATIVE_REF,
            (adapter, native_channel_id, native_message_id),
        )
        return [str(row["event_id"]) for row in rows]
