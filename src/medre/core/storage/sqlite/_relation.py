"""Relation mixins for SQLiteStorage."""

from __future__ import annotations

from typing import Any

from medre.core.events import EventRelation
from medre.core.storage.sqlite.serde import _encode_json, _now_iso, _row_to_relation
from medre.core.storage.sqlite.statements import _INSERT_RELATION, _SELECT_RELATIONS


class _RelationMixin:
    """Relation methods for SQLiteStorage.

    Accesses ``self._write`` and ``self._read_all`` from the base class
    via MRO.
    """

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
                relation.fallback_text,
                _encode_json(relation.metadata),
                _now_iso(),
            ),
        )

    async def store_relation(self, event_id: str, relation: EventRelation) -> None:
        """Persist a single relation for an existing event."""
        sql, params = self._relation_op(event_id, relation)
        await self._write(sql, params)

    async def list_relations(self, event_id: str) -> list[EventRelation]:
        """Return all relations belonging to *event_id*."""
        rows = await self._read_all(_SELECT_RELATIONS, (event_id,))
        return [_row_to_relation(r) for r in rows]
