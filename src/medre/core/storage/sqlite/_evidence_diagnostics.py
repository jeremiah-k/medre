"""Write-time separation and read-time resolution of delivery diagnostics.

Durable delivery facts (statuses, failure kinds, attempt generations,
confirmation levels, route correlation) stay inline in their evidence rows.
Free-form diagnostic text (error strings, capability reasons, rendering
evidence, error summaries, last errors) has a content lifetime instead: every
parent write stores that text only in the append-only
``delivery_evidence_diagnostics`` side table, inside the parent row's
transaction. Evidence rows carry no inline diagnostic columns.

Reads resolve every diagnostic field from the side table. Outbox lookups are
attempt-scoped, and a settled ``sent``/``queued`` outbox never surfaces a
same-attempt failure diagnostic; no diagnostic row exists without its parent
evidence row.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Mapping

from medre.core.storage.sqlite._content_identity import (
    CEILING_CLOCK_AT_EXECUTION,
    event_content_denied_sql,
)

#: Per family: the separated diagnostic field names, in parent-row column
#: order.  ``field`` is the side-table discriminator and the row key the
#: resolved value is attached to.
_DIAGNOSTIC_FIELDS: dict[str, tuple[str, ...]] = {
    "receipt": ("error", "capability_reason", "rendering_evidence"),
    "observation": ("error",),
    "outbox": ("failure_kind_detail", "error_summary"),
    "ingress": ("last_error",),
}

#: Families whose resolution is scoped to the parent row's current attempt
#: generation.  A superseded failure's diagnostics stay keyed to the attempt
#: they belong to and never surface on a later settled attempt.
_DIAGNOSTIC_ATTEMPT_COLUMNS: dict[str, str] = {"outbox": "attempt_number"}
_LOOKUP_CHUNK_SIZE = 500
#: Closed schema-owned parent lookup for read-time ceiling redaction. A late
#: diagnostic may be retained for immutable evidence but is never projected
#: after an enforcement receipt has committed for its owner event.
_DIAGNOSTIC_PARENTS: dict[str, tuple[str, str]] = {
    "receipt": ("delivery_receipts", "receipt_id"),
    "observation": ("delivery_observations", "observation_id"),
    "outbox": ("delivery_outbox", "outbox_id"),
    "ingress": ("durable_ingress_work", "event_id"),
}

_INSERT_OBSERVATION_DIAGNOSTIC = """
INSERT INTO delivery_evidence_diagnostics
    (family, row_id, attempt, field, diagnostic, created_at)
SELECT ?, ?, ?, ?, ?, ?
WHERE NOT EXISTS (
    SELECT 1 FROM delivery_observations o
    JOIN event_content_ceiling_log l ON l.event_id=o.event_id
    WHERE o.observation_id=?
)
"""

_INSERT_DIAGNOSTIC = """
INSERT INTO delivery_evidence_diagnostics
    (family, row_id, attempt, field, diagnostic, created_at)
VALUES (?, ?, ?, ?, ?, ?)
"""

# The outbox and ingress follow-up forms read the parent row's committed
# attempt/attempt-counter inside the parent's transaction, so the side-table
# key is derived from the same row state the parent write just committed.
_INSERT_OUTBOX_DIAGNOSTIC = """
INSERT INTO delivery_evidence_diagnostics
    (family, row_id, attempt, field, diagnostic, created_at)
SELECT 'outbox', outbox_id, attempt_number, ?, ?, ?
FROM delivery_outbox WHERE outbox_id = ?
  AND (? <> ''
       OR (
           SELECT d.diagnostic FROM delivery_evidence_diagnostics d
           WHERE d.family='outbox' AND d.row_id=delivery_outbox.outbox_id
             AND d.attempt=delivery_outbox.attempt_number AND d.field=?
           ORDER BY d.diag_id DESC LIMIT 1
       ) <> '')
"""

_INSERT_INGRESS_DIAGNOSTIC = """
INSERT INTO delivery_evidence_diagnostics
    (family, row_id, attempt, field, diagnostic, created_at)
SELECT 'ingress', event_id, attempts, 'last_error', ?, ?
FROM durable_ingress_work WHERE event_id = ?
"""


def diagnostic_write_ops(
    family: str,
    row_id: str,
    attempt: int,
    values: Mapping[str, str | None],
    created_at: str,
) -> list[tuple[str, tuple[Any, ...]]]:
    """Build append-only INSERT ops for every non-empty diagnostic value.

    Empty and ``None`` values produce no row: a successful delivery never
    writes diagnostic content anywhere.
    """
    ops: list[tuple[str, tuple[Any, ...]]] = []
    for field in _DIAGNOSTIC_FIELDS[family]:
        value = values.get(field)
        if isinstance(value, str) and value:
            params = (family, row_id, attempt, field, value, created_at)
            if family == "observation":
                ops.append((_INSERT_OBSERVATION_DIAGNOSTIC, (*params, row_id)))
            else:
                ops.append((_INSERT_DIAGNOSTIC, params))
    return ops


def outbox_diagnostic_ops(
    values: Mapping[str, str | None], created_at: str, outbox_id: str
) -> list[tuple[str, tuple[Any, ...]]]:
    """Build outbox diagnostic ops keyed by the row's committed attempt.

    Each op reads ``attempt_number`` from the outbox row inside the parent
    write's transaction, so the key always matches the generation the parent
    transition just finalized. Empty strings record an explicit clear only
    when the latest diagnostic is nonempty; redundant clears do not grow the
    append-only history.
    """
    ops: list[tuple[str, tuple[Any, ...]]] = []
    for field in _DIAGNOSTIC_FIELDS["outbox"]:
        value = values.get(field)
        if isinstance(value, str):
            ops.append(
                (
                    _INSERT_OUTBOX_DIAGNOSTIC,
                    (field, value, created_at, outbox_id, value, field),
                )
            )
    return ops


def ingress_diagnostic_ops(
    error: str | None, created_at: str, event_id: str
) -> list[tuple[str, tuple[Any, ...]]]:
    """Build an ingress ``last_error`` op keyed by the committed attempt count."""
    if not isinstance(error, str):
        return []
    return [(_INSERT_INGRESS_DIAGNOSTIC, (error, created_at, event_id))]


class _EvidenceDiagnosticsReadMixin:
    """Side-table resolution of separated delivery diagnostics."""

    if TYPE_CHECKING:

        async def _read_all(
            self, sql: str, params: tuple[Any, ...] = ()
        ) -> list[dict[str, Any]]: ...

    async def _resolve_evidence_diagnostics(
        self,
        family: str,
        rows: list[dict[str, Any]],
        *,
        identity_column: str,
    ) -> list[dict[str, Any]]:
        """Attach side-table diagnostic values to evidence rows, in place.

        Every diagnostic field resolves from the append-only side table; the
        rows carry no inline diagnostic columns. Each row leaves this call
        with every family field present — ``None`` where the evidence has no
        (or a suppressed) diagnostic — so row mappers see a complete shape.
        """
        fields = _DIAGNOSTIC_FIELDS[family]
        if not rows:
            return rows
        wanted: dict[str, set[str]] = {}
        for row in rows:
            # Successful handoff and adapter queue acceptance explicitly clear
            # transient failure text.  A diagnostic from the same attempt must
            # remain in history, not reappear as the current outbox failure.
            if family == "outbox" and row.get("status") in {"sent", "queued"}:
                continue
            wanted.setdefault(str(row[identity_column]), set()).update(fields)
        if not wanted:
            # Still normalize the row shape for the suppressed families.
            for row in rows:
                for field in fields:
                    row.setdefault(field, None)
            return rows
        fetched: list[dict[str, Any]] = []
        parent_table, parent_id = _DIAGNOSTIC_PARENTS[family]
        identities = list(wanted)
        observed = CEILING_CLOCK_AT_EXECUTION
        event_id = (
            "d.row_id"
            if family == "ingress"
            else (
                f"(SELECT p.event_id FROM {parent_table} p WHERE p.{parent_id}=d.row_id)"  # nosec B608 - closed parent table and column names
            )
        )
        # Keep only the closed legacy uncertainty label when diagnostic text
        # is denied; the original attempt can still accept late evidence.
        denied = event_content_denied_sql(event_id)
        for start in range(0, len(identities), _LOOKUP_CHUNK_SIZE):
            chunk = identities[start : start + _LOOKUP_CHUNK_SIZE]
            placeholders = ",".join("?" for _ in chunk)
            sql = (
                "SELECT 0 AS source_order, d.diag_id, d.row_id, d.field, d.attempt, "  # nosec B608 - closed SQL and bound identities
                f"CASE WHEN NOT {denied} THEN d.diagnostic "  # nosec B608 - closed SQL and bound identities
                "WHEN d.family='outbox' AND d.field='failure_kind_detail' "
                "AND d.diagnostic='uncertain_native_handoff' "
                "THEN 'uncertain_native_handoff' END AS diagnostic "
                "FROM delivery_evidence_diagnostics d "
                f"WHERE d.family = ? AND d.row_id IN ({placeholders})"  # nosec B608 - closed SQL and bound identities
            )
            params: tuple[Any, ...] = (observed, family, *chunk)
            if family == "outbox":
                # Resolve the typed fallback in the same statement snapshot:
                # a separate read could miss an uncertainty row that lands
                # between the diagnostic lookup and the fallback lookup.
                sql += (  # nosec B608 - closed SQL and bound identities
                    " UNION ALL "  # nosec B608 - closed SQL and bound identities
                    "SELECT 1 AS source_order, NULL AS diag_id, "
                    "u.outbox_id AS row_id, 'failure_kind_detail' AS field, "
                    "u.attempt_number AS attempt, u.state AS diagnostic "
                    "FROM delivery_handoff_uncertainty u "
                    "JOIN delivery_outbox o ON o.outbox_id=u.outbox_id "
                    f"WHERE u.outbox_id IN ({placeholders}) "
                    "AND u.state='uncertain_native_handoff' "
                    "AND o.status IN ('retry_wait', 'in_progress') "
                    "AND NOT EXISTS (SELECT 1 FROM delivery_evidence_diagnostics d "
                    "WHERE d.family='outbox' AND d.row_id=u.outbox_id "
                    "AND d.attempt=u.attempt_number "
                    "AND d.field='failure_kind_detail')"
                )
                params += tuple(chunk)
            fetched.extend(await self._read_all(sql, params))
        # Preserve global append order across chunks, then apply typed facts
        # after the diagnostic text they may fall back from.
        fetched.sort(
            key=lambda row: (
                int(row.get("source_order") or 0),
                int(row["diag_id"]) if row["diag_id"] is not None else 0,
            )
        )
        attempt_column = _DIAGNOSTIC_ATTEMPT_COLUMNS.get(family)
        latest_scoped: dict[tuple[str, str, int], str] = {}
        latest_unscoped: dict[tuple[str, str], str] = {}
        typed_uncertainty: dict[tuple[str, int], str] = {}
        for diagnostic_row in fetched:
            row_id = str(diagnostic_row["row_id"])
            field = str(diagnostic_row["field"])
            diagnostic = diagnostic_row["diagnostic"] or ""
            if int(diagnostic_row.get("source_order") or 0) == 1:
                # Typed uncertainty is a fallback for a row with no
                # failure_kind_detail diagnostic; it is not itself a
                # diagnostic for terminal rows.
                typed_uncertainty[(row_id, int(diagnostic_row["attempt"]))] = str(
                    diagnostic
                )
                continue
            if attempt_column is None:
                latest_unscoped[(row_id, field)] = str(diagnostic)
            else:
                latest_scoped[(row_id, field, int(diagnostic_row["attempt"]))] = str(
                    diagnostic
                )
        for row in rows:
            row_id = str(row[identity_column])
            for field in fields:
                if family == "outbox" and row.get("status") in {"sent", "queued"}:
                    row[field] = None
                    continue
                if attempt_column is None:
                    value = latest_unscoped.get((row_id, field))
                else:
                    value = latest_scoped.get(
                        (row_id, field, int(row.get(attempt_column) or 0))
                    )
                row[field] = value if value else None
        if family == "outbox":
            for row in rows:
                if row.get("failure_kind_detail") is not None or row.get(
                    "status"
                ) not in {"retry_wait", "in_progress"}:
                    continue
                typed = typed_uncertainty.get(
                    (
                        str(row[identity_column]),
                        int(row.get("attempt_number") or 0),
                    )
                )
                if typed is not None:
                    row["failure_kind_detail"] = typed
        return rows
