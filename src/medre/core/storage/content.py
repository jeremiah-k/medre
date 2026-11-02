"""Read-only content accounting without selecting message values.

These aggregates describe logical persisted bytes, not physical disk usage or
an erasure certificate. References, free pages, WAL, snapshots and provider
histories have independent lifetimes. No row count grants retention authority.
"""

from __future__ import annotations

from dataclasses import dataclass

#: Copies outside canonical logical collection; shared by accounting and policy.
UNMEASURED_CONTENT_SCOPES = (
    "provider_native_queues_histories_and_peers",
    "plugin_state_and_adapter_checkpoints",
    "sqlite_free_pages_and_wal",
    "backups_exports_and_snapshots",
    "application_and_provider_logs",
    "copies_embedded_in_other_events",
)


@dataclass(frozen=True, slots=True)
class ContentSurface:
    """One closed, schema-owned group of potentially content-bearing columns."""

    table: str
    columns: tuple[str, ...]
    consumers: tuple[str, ...]


CONTENT_SURFACES = (
    ContentSurface(
        "canonical_events",
        ("payload", "metadata"),
        ("delivery", "reply_quotes", "history", "replay"),
    ),
    ContentSurface(
        "event_relations",
        ("key", "metadata"),
        ("reply_quotes", "reaction_fallbacks"),
    ),
    ContentSurface(
        "event_body_content", ("body",), ("delivery", "reply_quotes", "replay")
    ),
    ContentSurface("event_relation_excerpts", ("excerpt",), ("reply_quotes",)),
    ContentSurface("native_message_refs", ("metadata",), ("native_correlation",)),
    ContentSurface(
        "delivery_receipts",
        ("error", "capability_reason", "rendering_evidence"),
        ("delivery_evidence",),
    ),
    ContentSurface(
        "delivery_observations", ("error", "metadata"), ("delivery_evidence",)
    ),
    ContentSurface(
        "delivery_outbox",
        ("failure_kind_detail", "error_summary", "metadata"),
        ("delivery", "recovery"),
    ),
    # Write-time separated free-form diagnostics for the delivery-evidence
    # families (receipts, observations, outbox records, ingress work).  A
    # diagnostic row never exists without its parent, but separate diagnostic
    # text is retained independently and never blocks logical collection.
    ContentSurface(
        "delivery_evidence_diagnostics",
        ("diagnostic",),
        ("delivery_evidence", "delivery", "recovery", "ingress_recovery"),
    ),
    ContentSurface(
        "radio_attempt_progress",
        ("plan_json", "provenance_json", "parts_json"),
        ("exact_packet_recovery", "packet_evidence"),
    ),
    # Privately owned frozen dispatch context for one outbox generation. The
    # document may carry arbitrary origin, destination, and policy values; the
    # row exists only while retry or uncompleted native work requires it and is
    # deleted by guarded terminal settlement (an unretired row blocks release
    # through the unretired_outbox_snapshots live-ownership probe).
    ContentSurface(
        "delivery_outbox_snapshots",
        ("context_json",),
        ("delivery", "recovery"),
    ),
    ContentSurface("durable_ingress_work", ("last_error",), ("ingress_recovery",)),
    ContentSurface("attachment_blobs", ("data",), ("attachment_delivery",)),
    ContentSurface("adapter_checkpoints", ("cursor", "metadata"), ("native_recovery",)),
    ContentSurface("plugin_state", ("value",), ("reserved_plugin_state",)),
)


def inventory_sql() -> str:
    """Aggregate every surface in one SQLite read snapshot.

    Identifiers come exclusively from the closed schema constants above.
    SQLite counts UTF-8 bytes with BLOB casts; JSON punctuation and empty
    envelopes count, shared attachment blobs count once, and no payload is
    returned to Python. All rows participate, including terminal deliveries.
    """
    queries = []
    for surface in CONTENT_SURFACES:
        sizes = " + ".join(
            f'COALESCE(length(CAST("{column}" AS BLOB)), 0)'
            for column in surface.columns
        )
        queries.append(
            f"SELECT '{surface.table}' AS surface, COUNT(*) AS rows, "  # nosec B608 - identifiers come only from the closed CONTENT_SURFACES constants above; no user input
            f'COALESCE(SUM({sizes}), 0) AS logical_bytes FROM "{surface.table}"'
        )
    return " UNION ALL ".join(queries)
