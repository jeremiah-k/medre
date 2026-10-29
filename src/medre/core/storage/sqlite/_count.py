"""Count query mixins for SQLiteStorage.

Authority surface: all methods are **list/get** (read-only).  Pure
aggregation queries that never mutate storage.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from medre.core.storage.backend import StorageError
from medre.core.storage.content import CONTENT_SURFACES, inventory_sql


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
        for surface in CONTENT_SURFACES:
            columns = {
                str(row["name"])
                for row in await self._read_all(
                    "SELECT name FROM pragma_table_info(?)", (surface.table,)
                )
            }
            if not columns or not set(surface.columns) <= columns:
                raise StorageError(
                    "Content inventory requires initialized content storage"
                )
        rows = await self._read_all(inventory_sql())
        by_surface = {str(row["surface"]): row for row in rows}
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
                    "rows": int(by_surface[surface.table]["rows"]),
                    "logical_bytes": int(by_surface[surface.table]["logical_bytes"]),
                }
                for surface in CONTENT_SURFACES
            ],
            "unmeasured": [
                "correlation_identifiers",
                "sqlite_free_pages_and_wal",
                "provider_profiles_and_histories",
                "snapshots_and_exports",
                "logs",
            ],
        }

    async def inspect_event_content_dependencies(self, event_id: str) -> dict[str, Any]:
        """Preview current core content consumers without reading retained bytes.

        This is an advisory snapshot, not a retention lease or permission to
        delete. Provider-owned queues, external copies and unknown consumers
        remain explicitly outside this observation.
        """
        if not event_id or len(event_id) > 512:
            raise ValueError("Content dependency preview requires a bounded event ID")
        # One statement observes the event and all counts in the same SQLite
        # snapshot, including incoming relations that may retain reply excerpts.
        rows = await self._read_all(
            "WITH selected(event_id) AS (VALUES (?)) "
            "SELECT 'event' AS kind, '' AS status, COUNT(*) AS count "
            "FROM canonical_events WHERE event_id=(SELECT event_id FROM selected) "
            "UNION ALL SELECT 'outbox', status, COUNT(*) FROM delivery_outbox "
            "WHERE event_id=(SELECT event_id FROM selected) GROUP BY status "
            "UNION ALL SELECT 'ingress', status, COUNT(*) FROM durable_ingress_work "
            "WHERE event_id=(SELECT event_id FROM selected) GROUP BY status "
            "UNION ALL SELECT 'relations', '', COUNT(*) FROM event_relations "
            "WHERE event_id=(SELECT event_id FROM selected) "
            "UNION ALL SELECT 'incoming_relations', '', COUNT(*) FROM event_relations "
            "WHERE target_event_id=(SELECT event_id FROM selected) "
            "UNION ALL SELECT 'receipts', '', COUNT(*) FROM delivery_receipts "
            "WHERE event_id=(SELECT event_id FROM selected) "
            "UNION ALL SELECT 'observations', '', COUNT(*) FROM delivery_observations "
            "WHERE event_id=(SELECT event_id FROM selected) "
            "UNION ALL SELECT 'attachments', '', COUNT(*) "
            "FROM event_attachment_associations "
            "WHERE event_id=(SELECT event_id FROM selected) "
            "UNION ALL SELECT 'radio_plans', '', COUNT(*) FROM radio_attempt_progress r "
            "JOIN delivery_outbox o ON o.outbox_id=r.outbox_id "
            "WHERE o.event_id=(SELECT event_id FROM selected) "
            "ORDER BY kind, status",
            (event_id,),
        )
        copies = {str(row["kind"]): int(row["count"]) for row in rows}
        if not copies.get("event"):
            raise StorageError("Content dependency event does not exist")
        outbox = [row for row in rows if row["kind"] == "outbox"]
        ingress = [row for row in rows if row["kind"] == "ingress"]
        return {
            "format": "medre-content-dependency-preview",
            "version": 1,
            "scope": "core_sqlite_event",
            "event_id": event_id,
            "retention_authorized": False,
            "deletion_authorized": False,
            "delivery_hold_candidate": any(
                row["status"] not in {"sent", "cancelled"} for row in outbox
            )
            or any(row["status"] != "completed" for row in ingress),
            "outbox": [
                {"status": row["status"], "count": int(row["count"])} for row in outbox
            ],
            "ingress": [
                {"status": row["status"], "count": int(row["count"])} for row in ingress
            ],
            "content_copies": {
                key: copies[key]
                for key in (
                    "relations",
                    "incoming_relations",
                    "receipts",
                    "observations",
                    "attachments",
                    "radio_plans",
                )
            },
            "unmeasured": [
                "provider_native_queues_and_histories",
                "snapshots_and_backups",
                "remote_peers",
                "logs",
                "optional_plugin_consumers",
                "canonical_payload_and_metadata",
                "native_reference_and_conversation_projections",
                "sqlite_free_pages_and_wal",
            ],
            "note": "Advisory counts only; no content read or retention decision made.",
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
