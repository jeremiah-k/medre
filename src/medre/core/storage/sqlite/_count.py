"""Count query mixins for SQLiteStorage.

Authority surface: all methods are **list/get** (read-only).  Pure
aggregation queries that never mutate storage.
"""

from __future__ import annotations


class _CountMixin:
    """Count methods for SQLiteStorage.

    Accesses ``self._read_one`` from the base class via MRO.
    """

    async def count_events(self) -> int:
        """Return the total number of persisted canonical events.

        Returns
        -------
        int
            Count of rows in ``canonical_events``.
        """
        row = await self._read_one("SELECT COUNT(*) AS cnt FROM canonical_events")
        if row is not None:
            return int(row["cnt"])
        return 0

    async def count_receipts(self) -> int:
        """Return the total number of delivery receipt rows.

        Returns
        -------
        int
            Count of rows in ``delivery_receipts``.
        """
        row = await self._read_one("SELECT COUNT(*) AS cnt FROM delivery_receipts")
        if row is not None:
            return int(row["cnt"])
        return 0

    async def count_native_refs(self) -> int:
        """Return the total number of native message ref records.

        Returns
        -------
        int
            Count of rows in ``native_message_refs``.
        """
        row = await self._read_one("SELECT COUNT(*) AS cnt FROM native_message_refs")
        return row["cnt"] if row else 0

    async def count_receipts_by_source(self, source: str) -> int:
        """Return the number of delivery receipts matching *source*.

        Parameters
        ----------
        source:
            The ``source`` column value to match (e.g. ``"live"`` or
            ``"replay"``).

        Returns
        -------
        int
            Count of rows in ``delivery_receipts`` with the given source.
        """
        row = await self._read_one(
            "SELECT COUNT(*) AS cnt FROM delivery_receipts WHERE source = ?",
            (source,),
        )
        return row["cnt"] if row else 0

    async def count_replay_runs(self) -> int:
        """Return the number of distinct ``replay_run_id`` values.

        Counts only non-null ``replay_run_id`` values in
        ``delivery_receipts``.

        Returns
        -------
        int
            Count of distinct replay run IDs.
        """
        row = await self._read_one(
            "SELECT COUNT(DISTINCT replay_run_id) AS cnt FROM delivery_receipts "
            "WHERE replay_run_id IS NOT NULL",
        )
        return row["cnt"] if row else 0
