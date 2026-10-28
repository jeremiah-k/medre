"""Count query mixins for SQLiteStorage.

Authority surface: all methods are **list/get** (read-only).  Pure
aggregation queries that never mutate storage.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from medre.core.storage.content import CONTENT_SURFACES, inventory_sql
from medre.core.storage.backend import StorageError


class _CountMixin:
    """Count methods for SQLiteStorage.

    Accesses ``self._read_one`` from the base class via MRO.
    """

    if TYPE_CHECKING:

        async def _read_all(
            self, sql: str, params: tuple[Any, ...] = ()
        ) -> list[dict[str, Any]]: ...

        async def _read_one(
            self, sql: str, params: tuple[Any, ...] = ()
        ) -> dict[str, Any] | None: ...

    async def inspect_content_inventory(self) -> dict[str, Any]:
        """Count content surfaces without returning messages or granting deletion.

        A single compound SELECT keeps all aggregates in one read snapshot.
        This explicit inspection scans retained values; it is not a startup
        probe. Physical bytes, provider stores and existing backups are outside
        the measured core SQLite scope.
        """
        present = {
            str(row["name"])
            for row in await self._read_all(
                "SELECT name FROM sqlite_schema WHERE type = 'table'"
            )
        }
        if any(surface.table not in present for surface in CONTENT_SURFACES):
            raise StorageError("Content inventory requires initialized content storage")
        rows = await self._read_all(inventory_sql())
        return {
            "format": "medre-content-inventory",
            "version": 1,
            "scope": "core_sqlite",
            "retention_authorized": False,
            "surfaces": [
                {
                    "table": surface.table,
                    "columns": list(surface.columns),
                    "consumers": list(surface.consumers),
                    "rows": int(row["rows"]),
                    "logical_bytes": int(row["logical_bytes"]),
                }
                for surface, row in zip(CONTENT_SURFACES, rows, strict=True)
            ],
            "unmeasured": [
                "correlation_identifiers",
                "sqlite_free_pages_and_wal",
                "provider_profiles_and_histories",
                "snapshots_and_exports",
                "logs",
            ],
        }

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

    async def count_delivery_observations(self) -> int:
        """Return the total number of post-handoff delivery observations."""
        row = await self._read_one("SELECT COUNT(*) AS cnt FROM delivery_observations")
        return int(row["cnt"]) if row is not None else 0

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
        return int(row["cnt"]) if row else 0

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
        return int(row["cnt"]) if row else 0

    async def count_replay_runs(self) -> int:
        """Return the number of distinct durable replay run IDs.

        A named replay run becomes durable when either immutable receipt evidence
        exists or a dispatchable target has atomically claimed an outbox
        generation.  Counting the union keeps operator diagnostics truthful
        across the crash window between outbox admission and first receipt.
        """
        row = await self._read_one(
            "SELECT COUNT(*) AS cnt FROM ("
            "SELECT replay_run_id FROM delivery_receipts "
            "WHERE replay_run_id IS NOT NULL AND replay_run_id <> '' "
            "UNION "
            "SELECT replay_run_id FROM delivery_outbox "
            "WHERE replay_run_id IS NOT NULL AND replay_run_id <> ''"
            ")",
        )
        return int(row["cnt"]) if row else 0
