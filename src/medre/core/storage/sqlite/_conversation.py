"""Derived conversation-membership storage for SQLiteStorage.

Canonical events and relation rows are append-only evidence.  This mixin owns
only the mutable, rebuildable projection that answers current conversation
membership after late relation targets become resolvable.
"""

from __future__ import annotations

from typing import Any

from medre.core.storage.backend import ConversationMembership
from medre.core.storage.sqlite.serde import _now_iso


class _ConversationMixin:
    """Conversation projection methods for :class:`SQLiteStorage`."""

    async def put_conversation_membership(
        self, membership: ConversationMembership
    ) -> bool:
        """Atomically upsert one projection row when semantic content changed."""
        changed = await self._write_rowcount(
            """
            INSERT INTO conversation_membership
                (event_id, root_event_id, conversation_id,
                 resolved_target_event_id, relation_type, depth,
                 resolution_state, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(event_id) DO UPDATE SET
                root_event_id=excluded.root_event_id,
                conversation_id=excluded.conversation_id,
                resolved_target_event_id=excluded.resolved_target_event_id,
                relation_type=excluded.relation_type,
                depth=excluded.depth,
                resolution_state=excluded.resolution_state,
                updated_at=excluded.updated_at
            WHERE conversation_membership.root_event_id IS NOT excluded.root_event_id
               OR conversation_membership.conversation_id IS NOT excluded.conversation_id
               OR conversation_membership.resolved_target_event_id
                    IS NOT excluded.resolved_target_event_id
               OR conversation_membership.relation_type IS NOT excluded.relation_type
               OR conversation_membership.depth IS NOT excluded.depth
               OR conversation_membership.resolution_state IS NOT excluded.resolution_state
            """,
            (
                membership.event_id,
                membership.root_event_id,
                membership.conversation_id,
                membership.resolved_target_event_id,
                membership.relation_type,
                membership.depth,
                membership.resolution_state,
                _now_iso(),
            ),
        )
        return changed == 1

    async def get_conversation_membership(
        self, event_id: str
    ) -> ConversationMembership | None:
        """Return current projection membership for one event."""
        row = await self._read_one(
            """
            SELECT event_id, root_event_id, conversation_id,
                   resolved_target_event_id, relation_type, depth,
                   resolution_state
            FROM conversation_membership
            WHERE event_id = ?
            """,
            (event_id,),
        )
        return self._row_to_conversation_membership(row) if row else None

    @staticmethod
    def _row_to_conversation_membership(row: dict[str, Any]) -> ConversationMembership:
        return ConversationMembership(
            event_id=str(row["event_id"]),
            root_event_id=str(row["root_event_id"]),
            conversation_id=str(row["conversation_id"]),
            resolved_target_event_id=row.get("resolved_target_event_id"),
            relation_type=row.get("relation_type"),
            depth=int(row["depth"]),
            resolution_state=str(row["resolution_state"]),
        )
