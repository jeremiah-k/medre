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
EVENT_COLUMNS = (
    f"{_IDENTITY_COLUMNS}, '{{}}' AS payload, "
    "json_object('routing', json_extract(metadata, '$.routing')) AS metadata, "
    f"{_CORRELATION_COLUMNS}"
)
# Only an authorized reply-context operation may select source sender metadata.
REPLY_COLUMNS = (
    f"{_IDENTITY_COLUMNS}, "
    "CASE WHEN json_type(payload, '$.formatting') IS NOT NULL "
    "THEN '{\"formatting\":{}}' ELSE '{}' END AS payload, "
    f"metadata, {_CORRELATION_COLUMNS}"
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
