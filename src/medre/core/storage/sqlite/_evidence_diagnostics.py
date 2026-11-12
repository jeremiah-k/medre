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
       OR EXISTS (
           SELECT 1 FROM delivery_evidence_diagnostics d
           WHERE d.family='outbox' AND d.row_id=delivery_outbox.outbox_id
             AND d.attempt=delivery_outbox.attempt_number AND d.field=?
             AND d.diagnostic<>''
       ))
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
            ops.append(
                (
                    _INSERT_DIAGNOSTIC,
                    (family, row_id, attempt, field, value, created_at),
                )
            )
    return ops


def outbox_diagnostic_ops(
    values: Mapping[str, str | None], created_at: str, outbox_id: str
) -> list[tuple[str, tuple[Any, ...]]]:
    """Build outbox diagnostic ops keyed by the row's committed attempt.

    Each op reads ``attempt_number`` from the outbox row inside the parent
    write's transaction, so the key always matches the generation the parent
    transition just finalized. Empty strings record an explicit clear so a
    previous diagnostic cannot reappear in the same attempt.
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
        identities = list(wanted)
        for start in range(0, len(identities), _LOOKUP_CHUNK_SIZE):
            chunk = identities[start : start + _LOOKUP_CHUNK_SIZE]
            placeholders = ",".join("?" for _ in chunk)
            fetched.extend(
                await self._read_all(
                    "SELECT diag_id, row_id, field, attempt, diagnostic"
                    " FROM delivery_evidence_diagnostics"
                    f" WHERE family = ? AND row_id IN ({placeholders})"
                    " ORDER BY diag_id ASC",  # nosec B608 - static identifiers and bound values only
                    (family, *chunk),
                )
            )
        # Preserve the global append order when the IDs span multiple chunks.
        fetched.sort(key=lambda row: int(row["diag_id"]))
        attempt_column = _DIAGNOSTIC_ATTEMPT_COLUMNS.get(family)
        latest_scoped: dict[tuple[str, str, int], str] = {}
        latest_unscoped: dict[tuple[str, str], str] = {}
        for diagnostic_row in fetched:
            row_id = str(diagnostic_row["row_id"])
            field = str(diagnostic_row["field"])
            if attempt_column is None:
                latest_unscoped[(row_id, field)] = str(diagnostic_row["diagnostic"])
            else:
                latest_scoped[(row_id, field, int(diagnostic_row["attempt"]))] = str(
                    diagnostic_row["diagnostic"]
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
        return rows
