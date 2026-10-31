"""Write-time separation and read-time coalescing of delivery diagnostics.

Durable delivery facts (statuses, failure kinds, attempt generations,
confirmation levels, route correlation) stay inline in their evidence rows.
Free-form diagnostic text (error strings, capability reasons, rendering
evidence, error summaries, last errors) has a content lifetime instead: every
new parent write stores that text only in the append-only
``delivery_evidence_diagnostics`` side table, inside the parent row's
transaction, and leaves the legacy inline columns ``NULL``.

Reads coalesce: historical inline values win for immutable receipts and
observations. Mutable ingress work uses its latest transition diagnostic;
mutable outboxes use current-attempt diagnostics before inline fallback. Inline historical values are
never rewritten; no diagnostic row exists without its parent evidence row.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Mapping

#: Per family: the separated ``(field, inline column)`` pairs, in parent-row
#: column order.  ``field`` is the side-table discriminator; the inline column
#: names the legacy evidence column the value used to occupy.
_DIAGNOSTIC_FIELDS: dict[str, tuple[tuple[str, str], ...]] = {
    "receipt": (
        ("error", "error"),
        ("capability_reason", "capability_reason"),
        ("rendering_evidence", "rendering_evidence"),
    ),
    "observation": (("error", "error"),),
    "outbox": (
        ("failure_kind_detail", "failure_kind_detail"),
        ("error_summary", "error_summary"),
    ),
    "ingress": (("last_error", "last_error"),),
}

#: Families whose coalescing lookup is scoped to the parent row's current
#: attempt generation.  A superseded failure's diagnostics stay keyed to the
#: attempt they belong to and never surface on a later settled attempt.
_DIAGNOSTIC_ATTEMPT_COLUMNS: dict[str, str] = {"outbox": "attempt_number"}
_LOOKUP_CHUNK_SIZE = 500
_MUTABLE_FAMILIES = frozenset({"outbox", "ingress"})

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
       OR CASE ? WHEN 'error_summary' THEN error_summary
                 ELSE failure_kind_detail END IS NOT NULL
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
    for field, _inline in _DIAGNOSTIC_FIELDS[family]:
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
    for field, _inline in _DIAGNOSTIC_FIELDS["outbox"]:
        value = values.get(field)
        if isinstance(value, str):
            ops.append(
                (
                    _INSERT_OUTBOX_DIAGNOSTIC,
                    (field, value, created_at, outbox_id, value, field, field),
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
    """Coalescing reads over the separated diagnostic side table."""

    if TYPE_CHECKING:

        async def _read_all(
            self, sql: str, params: tuple[Any, ...] = ()
        ) -> list[dict[str, Any]]: ...

    async def _coalesce_evidence_diagnostics(
        self,
        family: str,
        rows: list[dict[str, Any]],
        *,
        identity_column: str,
    ) -> list[dict[str, Any]]:
        """Fill empty inline diagnostic columns from the side table, in place.

        Legacy inline values take precedence for immutable evidence rows.
        Mutable ingress work uses the latest side-table value; outboxes use
        the current attempt before any earlier inline text. Outbox lookups
        are attempt-scoped; absent side values preserve the inline fallback.
        """
        if not rows:
            return rows
        wanted: dict[str, set[str]] = {}
        for row in rows:
            # Successful handoff and adapter queue acceptance explicitly clear
            # transient failure text.  A diagnostic from the same attempt must
            # remain in history, not reappear as the current outbox failure.
            if family == "outbox" and row.get("status") in {"sent", "queued"}:
                continue
            for field, inline in _DIAGNOSTIC_FIELDS[family]:
                # A new outbox attempt can supersede a legacy inline failure.
                # Immutable evidence rows keep historical inline precedence.
                if family not in _MUTABLE_FAMILIES and row.get(inline):
                    continue
                wanted.setdefault(str(row[identity_column]), set()).add(field)
        if not wanted:
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
            for field, inline in _DIAGNOSTIC_FIELDS[family]:
                if family not in _MUTABLE_FAMILIES and row.get(inline):
                    continue
                if attempt_column is None:
                    value = latest_unscoped.get((row_id, field))
                else:
                    value = latest_scoped.get(
                        (row_id, field, int(row.get(attempt_column) or 0))
                    )
                if value is not None:
                    row[inline] = value or None
        return rows
