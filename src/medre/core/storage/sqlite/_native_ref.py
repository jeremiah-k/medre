"""Native message ref mixins for SQLiteStorage."""

from __future__ import annotations

from medre.core.events import NativeMessageRef
from medre.core.storage.sqlite.serde import _encode_json, _row_to_native_ref
from medre.core.storage.sqlite.statements import (
    _GET_NATIVE_REF,
    _INSERT_NATIVE_REF,
    _RESOLVE_NATIVE_REF,
    _SELECT_NREFS_FOR_EVENT,
)


class _NativeRefMixin:
    """Native message ref methods for SQLiteStorage.

    Accesses ``self._read_one``, ``self._read_all``, and ``self._write``
    from the base class via MRO.
    """

    async def store_native_ref(self, ref: NativeMessageRef) -> None:
        """Persist a native-to-canonical message mapping.

        Duplicate ``(adapter, native_channel_id, native_message_id)`` triples
        are silently ignored (idempotent).  When *native_channel_id* is
        ``None``, SQLite's UNIQUE constraint cannot detect duplicates
        because ``NULL != NULL``.  This method therefore performs an
        explicit resolve-before-insert check so that NULL-channel refs
        also dedupe deterministically.

        Use :meth:`resolve_native_ref` to retrieve the canonical
        ``event_id`` for an existing mapping.
        """
        # Resolve-before-insert: handles NULL native_channel_id which
        # SQLite UNIQUE treats as distinct per SQL standard.
        existing = await self._read_one(
            _RESOLVE_NATIVE_REF,
            (ref.adapter, ref.native_channel_id, ref.native_message_id),
        )
        if existing is not None:
            return

        await self._write(
            _INSERT_NATIVE_REF,
            (
                ref.id,
                ref.event_id,
                ref.adapter,
                ref.native_channel_id,
                ref.native_message_id,
                ref.native_thread_id,
                ref.native_relation_id,
                ref.direction,
                _encode_json(ref.metadata),
                ref.created_at.isoformat(),
            ),
        )

    async def resolve_native_ref(
        self,
        adapter: str,
        native_channel_id: str | None,
        native_message_id: str,
    ) -> str | None:
        """Look up the canonical event ID for a native message reference."""
        row = await self._read_one(
            _RESOLVE_NATIVE_REF,
            (adapter, native_channel_id, native_message_id),
        )
        return row["event_id"] if row else None

    async def get_native_ref(
        self,
        adapter: str,
        native_channel_id: str | None,
        native_message_id: str,
    ) -> NativeMessageRef | None:
        """Return the stored NativeMessageRef for the given triple.

        Returns ``None`` when no mapping exists.  Uses ``IS`` for proper
        ``NULL`` comparison of *native_channel_id*.
        """
        row = await self._read_one(
            _GET_NATIVE_REF,
            (adapter, native_channel_id, native_message_id),
        )
        return _row_to_native_ref(row) if row else None

    async def list_native_refs_for_event(
        self,
        event_id: str,
    ) -> list[NativeMessageRef]:
        """Return all native message refs for a specific event.

        Native refs are ordered by ``created_at`` ascending, which reflects
        the chronological order in which adapters materialised the event
        into their native namespaces.
        """
        rows = await self._read_all(
            _SELECT_NREFS_FOR_EVENT,
            (event_id,),
        )
        return [_row_to_native_ref(r) for r in rows]
