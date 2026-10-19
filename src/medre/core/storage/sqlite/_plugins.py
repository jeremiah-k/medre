"""Atomic host-owned plugin evidence, derivation, and namespaced state.

Plugins never receive this storage interface. The execution host supplies
validated decisions and buffered state; no callback or plugin code runs in a
SQLite transaction. Recovery reads committed records before invoking a plugin.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

import msgspec

from medre.core.events import CanonicalEvent
from medre.core.storage.backend import (
    EventNotFoundError,
    PluginExecutionConflictError,
    PluginStateConflictError,
    StorageError,
)
from medre.plugins.records import (
    MAX_PLUGIN_DEPTH,
    PluginExecutionKey,
    PluginExecutionRecord,
    PluginStateSnapshot,
    canonical_json,
    state_digest,
    validate_scope,
)
from medre.registration import valid_registration_name


def _state_key(plugin_id: str, instance_id: str, scope: str) -> tuple[str, str]:
    if not valid_registration_name(plugin_id) or not valid_registration_name(
        instance_id
    ):
        raise ValueError("plugin and instance identities must be registration tokens")
    validate_scope(scope)
    # Tuple encoding avoids ambiguous separators or cross-instance
    # collisions in the schema-reserved (plugin_id, key) primary key.
    return plugin_id, canonical_json([instance_id, scope])


def _snapshot(raw: str | None) -> PluginStateSnapshot:
    if raw is None:
        return PluginStateSnapshot(0, {})
    data = json.loads(raw)
    if (
        set(data) != {"schema_version", "revision", "values"}
        or type(data["schema_version"]) is not int
        or data["schema_version"] != 1
    ):
        raise ValueError("invalid plugin state envelope")
    return PluginStateSnapshot(data["revision"], data["values"])


def _decode_record(raw: str) -> PluginExecutionRecord:
    return PluginExecutionRecord.from_data(json.loads(raw))


def _materialize(
    record: PluginExecutionRecord, source: CanonicalEvent
) -> tuple[CanonicalEvent, ...]:
    if record.proposals and max(source.depth, len(source.lineage)) >= MAX_PLUGIN_DEPTH:
        raise ValueError("plugin derivation exceeds maximum lineage depth")
    return tuple(
        msgspec.structs.replace(
            source,
            event_id=event_id,
            event_kind=proposal.kind,
            timestamp=record.recorded_at,
            parent_event_id=source.event_id,
            lineage=source.lineage + (source.event_id,),
            depth=source.depth + 1,
            payload=proposal.payload,
            source_native_ref=None,
            relations=source.relations if source.event_kind == proposal.kind else (),
            root_event_id=source.root_event_id or source.event_id,
            conversation_id=source.conversation_id
            or source.root_event_id
            or source.event_id,
        )
        for event_id, proposal in zip(
            record.produced_event_ids, record.proposals, strict=True
        )
    )


_SELECT_LATEST = """
SELECT record_json FROM plugin_execution_records
WHERE source_event_id=? AND plugin_id=? AND instance_id=? AND stage=? AND scope=?
ORDER BY attempt DESC LIMIT 1
"""


def _commit(
    db: sqlite3.Connection,
    lock: threading.Lock,
    *,
    record: PluginExecutionRecord,
    values: Mapping[str, Any] | None,
    event_ops: list[tuple[str, tuple[Any, ...]]],
) -> PluginExecutionRecord:
    with lock:
        try:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute(
                "SELECT record_json FROM plugin_execution_records WHERE record_id=?",
                (record.record_id,),
            ).fetchone()
            if existing is not None:
                stored = _decode_record(existing[0])
                if canonical_json(stored.to_data()) != canonical_json(record.to_data()):
                    raise PluginExecutionConflictError(
                        "plugin execution identity contains different evidence"
                    )
                # A retried commit acknowledges the committed result; it never
                # applies a second state write or relies on the stale revision.
                db.commit()
                return stored
            plan = db.execute(
                "SELECT record_json FROM plugin_execution_records WHERE source_event_id=? AND scope=? LIMIT 1",
                (record.key.source_event_id, record.key.scope),
            ).fetchone()
            if (
                plan is not None
                and _decode_record(plan[0]).plan_digest != record.plan_digest
            ):
                raise PluginExecutionConflictError(
                    "plugin execution plan changed; use an explicit replay scope"
                )
            latest = db.execute(_SELECT_LATEST, record.key.identity()).fetchone()
            previous = _decode_record(latest[0]) if latest is not None else None
            if previous is not None and previous.key != record.key:
                raise ValueError("plugin execution key does not match stored evidence")
            if previous is not None and previous.settled:
                raise PluginExecutionConflictError(
                    "settled plugin execution requires a separate replay scope"
                )
            if record.attempt != (previous.attempt + 1 if previous else 1):
                raise PluginExecutionConflictError(
                    "plugin attempt must follow the recorded attempt sequence"
                )
            if previous is not None and (
                previous.plugin_version,
                previous.config_digest,
            ) != (record.plugin_version, record.config_digest):
                raise PluginExecutionConflictError(
                    "plugin version or configuration changed during recovery"
                )
            state_key = _state_key(
                record.key.plugin_id, record.key.instance_id, record.key.scope
            )
            row = db.execute(
                "SELECT value FROM plugin_state WHERE plugin_id=? AND key=?", state_key
            ).fetchone()
            snapshot = _snapshot(row[0] if row else None)
            if snapshot.revision != record.state_revision:
                raise PluginStateConflictError(
                    "plugin state revision changed; discard the uncommitted result"
                )
            # A read-only or failed invocation records evidence only. The
            # state revision is an operational write counter, not an attempt
            # counter; retrying a timeout must see the same state snapshot.
            next_state = (
                PluginStateSnapshot(snapshot.revision + 1, values)
                if values is not None
                else None
            )
            for sql, params in event_ops:
                db.execute(sql, params)
            db.execute(
                """
                INSERT INTO plugin_execution_records
                    (record_id, source_event_id, plugin_id, instance_id, stage, scope,
                     attempt, record_json, recorded_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.record_id,
                    *record.key.identity(),
                    record.attempt,
                    canonical_json(record.to_data()),
                    record.recorded_at.isoformat(),
                ),
            )
            if next_state is not None:
                db.execute(
                    """
                    INSERT INTO plugin_state (plugin_id, key, value, updated_at)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(plugin_id, key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
                    """,
                    (
                        *state_key,
                        canonical_json(
                            {
                                "schema_version": 1,
                                "revision": next_state.revision,
                                "values": next_state.values,
                            }
                        ),
                        record.recorded_at.isoformat(),
                    ),
                )
            db.commit()
            return record
        except BaseException:
            try:
                db.rollback()
            except sqlite3.Error:
                pass
            raise


class _PluginsMixin:
    if TYPE_CHECKING:
        _lock: threading.Lock

        def _require_db(self) -> sqlite3.Connection: ...
        async def _run_in_thread(self, func: Any, *args: Any, **kwargs: Any) -> Any: ...
        async def _read_one(
            self, sql: str, params: tuple[Any, ...] = ()
        ) -> dict[str, Any] | None: ...
        async def get(self, event_id: str) -> CanonicalEvent | None: ...
        def _event_admission_ops(
            self, event: CanonicalEvent
        ) -> list[tuple[str, tuple[Any, ...]]]: ...

    async def get_plugin_state(
        self, plugin_id: str, instance_id: str, *, scope: str = "live"
    ) -> PluginStateSnapshot:
        """Read one bounded instance snapshot; never expose arbitrary KV access."""
        row = await self._read_one(
            "SELECT value FROM plugin_state WHERE plugin_id=? AND key=?",
            _state_key(plugin_id, instance_id, scope),
        )
        try:
            return _snapshot(row["value"] if row else None)
        except (TypeError, ValueError, KeyError) as exc:
            raise StorageError("plugin state does not match its contract") from exc

    async def get_plugin_execution(
        self, key: PluginExecutionKey
    ) -> PluginExecutionRecord | None:
        """Read the latest attempt, including a deferred or settled failure."""
        row = await self._read_one(_SELECT_LATEST, key.identity())
        if row is None:
            return None
        try:
            record = _decode_record(row["record_json"])
            if record.key != key:
                raise ValueError("plugin execution key does not match stored evidence")
            return record
        except (TypeError, ValueError, KeyError) as exc:
            raise StorageError(
                "plugin execution evidence does not match its contract"
            ) from exc

    async def commit_plugin_execution(
        self,
        record: PluginExecutionRecord,
        *,
        state: Mapping[str, Any] | None = None,
    ) -> PluginExecutionRecord:
        """Commit evidence, derived events, and state as one compare-and-swap.

        No ingress work is created for children: recovery must resume their
        recorded forward cursor, never treat them as fresh transport ingress.
        A failed invocation cannot persist plugin state or produced events.
        Exact repeated commits acknowledge evidence without mutating state.
        """
        if not isinstance(record, PluginExecutionRecord):
            raise TypeError("record must be a PluginExecutionRecord")
        if record.failure is not None and state is not None:
            raise ValueError("a failed plugin execution cannot update plugin state")
        values = (
            PluginStateSnapshot(record.state_revision, state).values
            if state is not None
            else None
        )
        if record.state_digest != (
            state_digest(values) if values is not None else None
        ):
            raise ValueError("buffered state does not match the execution state digest")
        source = await self.get(record.key.source_event_id)
        if source is None:
            raise EventNotFoundError("plugin source event is not stored")
        events = _materialize(record, source)
        operations = [op for event in events for op in self._event_admission_ops(event)]
        db = self._require_db()
        try:
            return await self._run_in_thread(
                _commit,
                db,
                self._lock,
                record=record,
                values=values,
                event_ops=operations,
            )
        except PluginExecutionConflictError:
            raise
        except (sqlite3.Error, TypeError, ValueError, KeyError) as exc:
            raise StorageError(
                "plugin execution transaction failed; recover from stored evidence"
            ) from exc
