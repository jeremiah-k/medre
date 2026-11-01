"""Content-free catalog projections used by ordinary storage inspection."""

# Never SELECT canonical payload or opaque metadata merely to redact it later.
# Routing contains MEDRE-owned route identifiers, not native/provider metadata.
_IDENTITY_COLUMNS = (
    "event_id, event_kind, schema_version, timestamp, source_adapter, "
    "source_transport_id, source_channel_id, parent_event_id, lineage"
)
_CORRELATION_COLUMNS = (
    "depth, trace_id, root_event_id, conversation_id, "
    "source_native_adapter, source_native_channel_id, "
    "source_native_message_id, source_native_thread_id"
)
# The composed routing namespace for one canonical_events row.  The typed
# ``event_routing_facts`` row wins when present; otherwise the routing value
# inline in the metadata JSON is the row's own stored truth and still serves
# consumers.  Both sources hold the same bounded route identifiers; neither
# grants content.
_TYPED_ROUTING_SQL = (
    "(SELECT json_object("
    "'matched_routes', json(f.matched_routes), "
    "'fanout_group', f.fanout_group, "
    "'route_trace', json(f.route_trace)"
    ") FROM event_routing_facts AS f "
    "WHERE f.event_id = canonical_events.event_id)"
)
_INLINE_ROUTING_SQL = "json_extract(canonical_events.metadata, '$.routing')"
_COMPOSED_ROUTING_SQL = f"COALESCE({_TYPED_ROUTING_SQL}, {_INLINE_ROUTING_SQL})"

EVENT_COLUMNS = (
    f"{_IDENTITY_COLUMNS}, '{{}}' AS payload, "
    f"json_object('routing', json({_COMPOSED_ROUTING_SQL})) AS metadata, "
    f"{_CORRELATION_COLUMNS}"
)
# The formatting marker selects canonical text without exposing its contents.
_REPLY_IDENTITY_AND_TEXT = (
    f"{_IDENTITY_COLUMNS}, "
    "CASE WHEN json_type(payload, '$.formatting') IS NOT NULL "
    "THEN '{\"formatting\":{}}' ELSE '{}' END AS payload"
)
# A live source authorizes provider-owned sender projection. A body-only quote
# grant does not authorize arbitrary native/custom metadata copies.
REPLY_COLUMNS = (
    f"{_REPLY_IDENTITY_AND_TEXT}, "
    f"json_set(metadata, '$.routing', json({_COMPOSED_ROUTING_SQL})) AS metadata, "
    f"{_CORRELATION_COLUMNS}"
)
REPLY_BODY_COLUMNS = (
    f"{_REPLY_IDENTITY_AND_TEXT}, '{{}}' AS metadata, {_CORRELATION_COLUMNS}"
)
# Authorized owner reads (ingress, outbox, replay) project the row's own
# opaque namespaces with the composed routing namespace folded in.  Every
# column is schema-owned; no content column is added by the composition.
AUTHORIZED_EVENT_COLUMNS = (
    f"{_IDENTITY_COLUMNS}, payload, "
    f"json_set(metadata, '$.routing', json({_COMPOSED_ROUTING_SQL})) AS metadata, "
    f"{_CORRELATION_COLUMNS}"
)

# Relation key may contain reaction or other user text. Return only identities.
RELATION_COLUMNS = (
    "relation_type, target_event_id, target_native_adapter, "
    "target_native_channel_id, target_native_message_id, "
    "target_native_thread_id, NULL AS key, '{}' AS metadata"
)


# Closed reviewed allowlists. Future schema columns are withheld by default.
_INSPECTION_COLUMNS = {
    "delivery_receipts": frozenset(
        "sequence receipt_id event_id delivery_plan_id target_adapter target_channel route_id status receipt_kind failure_kind capability_level capability_field delivery_strategy adapter_message_id next_retry_at attempt_number parent_receipt_id source replay_run_id retry_max_attempts retry_backoff_base retry_max_delay retry_jitter outbox_id confirmation_level created_at".split()
    ),
    "native_message_refs": frozenset(
        "id event_id adapter native_channel_id native_message_id native_thread_id native_relation_id direction created_at".split()
    ),
    "delivery_observations": frozenset(
        "sequence observation_id event_id delivery_plan_id target_adapter target_channel native_channel_id outbox_id attempt_number adapter_message_id state confirmation_level observed_at".split()
    ),
    "delivery_outbox": frozenset(
        "outbox_id event_id route_id delivery_plan_id target_adapter target_channel target_address attempt_number active_attempt status failure_kind next_attempt_at created_at updated_at last_attempt_at locked_at lease_until worker_id payload_hash receipt_id parent_receipt_id dispatch_source replay_run_id".split()
    ),
}


def inspection_sql(sql: str, table: str, metadata_only: bool) -> str:
    """Choose a safe SQL projection before any opaque evidence reaches Python.

    The caller supplies schema-owned SQL and a closed table identifier. Raw
    recovery reads retain their separate privileged contract; operator readers
    explicitly request metadata_only. No persisted evidence is modified.
    """
    if not metadata_only:
        return sql
    from medre.core.storage.sqlite.schema import _REQUIRED_COLUMNS

    allowed = _INSPECTION_COLUMNS[table]
    columns = ", ".join(
        (
            column
            if column in allowed
            else ("'{}' AS metadata" if column == "metadata" else f"NULL AS {column}")
        )
        for column in sorted(_REQUIRED_COLUMNS[table])
    )
    if "SELECT *" not in sql:
        raise ValueError("Inspection requires a schema-owned row query")
    return sql.replace("SELECT *", "SELECT " + columns)
