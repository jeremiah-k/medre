"""Tests for CanonicalEvent validation: schema registry hardening,
schema version compatibility, relation validation, lineage validation,
malformed payload validation, schema migration, event taxonomy audit,
and protocol-neutral readiness.
"""

from __future__ import annotations

from datetime import datetime, timezone

import msgspec
import pytest

from medre.core.events import (
    CURRENT_SCHEMA_VERSION,
    KNOWN_KINDS,
    MIGRATION_REGISTRY,
    VALID_RELATION_TYPES,
    CanonicalEvent,
    EventKind,
    EventMetadata,
    EventRelation,
    NativeMetadata,
    NativeRef,
    SchemaRegistry,
    TransportMetadata,
    schema_version_from_event,
)


def _valid_kwargs() -> dict:
    """Module-level base kwargs that produce a valid CanonicalEvent.

    Used by tests that need a clean starting point for mutation.
    """
    return dict(
        event_id="evt-ok",
        event_kind="message.text",
        schema_version=1,
        timestamp=datetime.now(timezone.utc),
        source_adapter="test",
        source_transport_id="t-1",
        source_channel_id=None,
        parent_event_id=None,
        lineage=(),
        relations=(),
        payload={},
        metadata=EventMetadata(),
    )


# ===================================================================
# SchemaRegistry hardening
# ===================================================================


class TestSchemaRegistryHardening:
    """SchemaRegistry callable check, register_or_replace, unregistered kind."""

    def test_validate_rejects_non_callable_validator(self) -> None:
        """If a non-callable slips into the registry, validate returns False."""
        registry = SchemaRegistry()
        # Directly inject a non-callable to simulate corruption
        registry._schemas[("bad.kind", 1)] = "not-a-callable"  # type: ignore[assignment]
        errors: list[str] = []
        result = registry.validate("bad.kind", {}, errors=errors)
        assert result is False
        assert any("not callable" in e for e in errors)

    def test_register_or_replace_overwrites(self) -> None:
        """register_or_replace overwrites an existing validator."""
        registry = SchemaRegistry()
        registry.register("msg", 1, lambda p: ["old error"])
        registry.register_or_replace("msg", 1, lambda p: [])
        assert registry.validate("msg", {}) is True

    def test_register_or_replace_fresh(self) -> None:
        """register_or_replace works when no prior registration exists."""
        registry = SchemaRegistry()
        registry.register_or_replace("new.kind", 2, lambda p: [])
        assert registry.validate("new.kind", {}, schema_version=2) is True

    def test_unregistered_kind_returns_false(self) -> None:
        """validate returns False for an unregistered event kind."""
        registry = SchemaRegistry()
        assert registry.validate("absent.kind", {}) is False


# ===================================================================
# Schema version compatibility (Track 2)
# ===================================================================


class TestSchemaVersionCompatibility:
    """Schema versioning contract: v1 is current, future versions accepted,
    invalid versions rejected."""

    def test_current_schema_version_is_1(self) -> None:
        """CURRENT_SCHEMA_VERSION constant is 1."""
        assert CURRENT_SCHEMA_VERSION == 1

    def test_v1_event_is_valid(self) -> None:
        """schema_version=1 is the baseline contract and is accepted."""
        event = CanonicalEvent(**_valid_kwargs())
        assert event.schema_version == 1

    def test_future_version_accepted(self) -> None:
        """A high schema_version (future) is accepted at construction.
        Consumers should treat unknown fields normally and ignore
        unrecognised ones."""
        kw = _valid_kwargs()
        kw["schema_version"] = 999
        event = CanonicalEvent(**kw)
        assert event.schema_version == 999

    def test_schema_version_from_event_extracts_future(self) -> None:
        """schema_version_from_event handles future versions."""
        sv = schema_version_from_event("message.text", {"schema_version": 42})
        assert sv.version == 42

    def test_schema_version_from_event_non_int_defaults(self) -> None:
        """Non-int schema_version in payload defaults to 1."""
        sv = schema_version_from_event("message.text", {"schema_version": "bad"})
        assert sv.version == 1

    def test_valid_relation_types_constant(self) -> None:
        """VALID_RELATION_TYPES contains exactly the five known types."""
        assert VALID_RELATION_TYPES == frozenset(
            {"reply", "reaction", "edit", "delete", "thread"}
        )


# ===================================================================
# Relation validation (Track 2)
# ===================================================================


class TestRelationValidation:
    """EventRelation validates relation_type at construction time."""

    def test_invalid_relation_type_raises(self) -> None:
        """An unknown relation_type raises ValueError in __post_init__."""
        with pytest.raises(ValueError, match="relation_type"):
            EventRelation(
                relation_type="invalid",  # type: ignore[arg-type]
                target_event_id="t-1",
                target_native_ref=None,
                key=None,
                fallback_text=None,
            )

    def test_empty_relation_type_raises(self) -> None:
        """Empty string relation_type raises ValueError."""
        with pytest.raises(ValueError, match="relation_type"):
            EventRelation(
                relation_type="",  # type: ignore[arg-type]
                target_event_id="t-1",
                target_native_ref=None,
                key=None,
                fallback_text=None,
            )

    @pytest.mark.parametrize(
        "rel_type",
        sorted(VALID_RELATION_TYPES),
    )
    def test_all_valid_relation_types_accepted(self, rel_type: str) -> None:
        """Every valid relation_type is accepted."""
        rel = EventRelation(
            relation_type=rel_type,  # type: ignore[arg-type]
            target_event_id=None,
            target_native_ref=None,
            key=None,
            fallback_text=None,
        )
        assert rel.relation_type == rel_type

    def test_relation_with_no_targets_is_valid(self) -> None:
        """A relation with neither target_event_id nor target_native_ref
        is accepted (pending resolution)."""
        rel = EventRelation(
            relation_type="reply",
            target_event_id=None,
            target_native_ref=None,
            key=None,
            fallback_text="some fallback",
        )
        assert rel.target_event_id is None
        assert rel.target_native_ref is None
        assert rel.fallback_text == "some fallback"

    def test_relation_with_both_targets_is_valid(self) -> None:
        """A relation carrying both canonical and native references is
        allowed (canonical takes precedence at resolution time)."""
        nref = NativeRef(adapter="test", native_channel_id="c", native_message_id="m")
        rel = EventRelation(
            relation_type="reply",
            target_event_id="evt-1",
            target_native_ref=nref,
            key=None,
            fallback_text=None,
        )
        assert rel.target_event_id == "evt-1"
        assert rel.target_native_ref is not None


# ===================================================================
# Lineage validation (Track 2)
# ===================================================================


class TestLineageValidation:
    """CanonicalEvent validates lineage content."""

    def test_lineage_with_empty_string_raises(self) -> None:
        """An empty string in lineage raises ValueError."""
        kw = _valid_kwargs()
        kw["lineage"] = ("",)
        with pytest.raises(ValueError, match="lineage\\[0\\]"):
            CanonicalEvent(**kw)

    def test_lineage_with_non_string_raises(self) -> None:
        """A non-string item in lineage raises ValueError."""
        kw = _valid_kwargs()
        kw["lineage"] = (123,)  # type: ignore[arg-type]
        with pytest.raises(ValueError, match="lineage\\[0\\]"):
            CanonicalEvent(**kw)

    def test_lineage_second_item_invalid(self) -> None:
        """Only the second item is invalid – index must be correct."""
        kw = _valid_kwargs()
        kw["lineage"] = ("valid-id", "")
        with pytest.raises(ValueError, match="lineage\\[1\\]"):
            CanonicalEvent(**kw)

    def test_empty_lineage_is_valid(self) -> None:
        """An empty lineage (root event) is accepted."""
        kw = _valid_kwargs()
        kw["lineage"] = ()
        event = CanonicalEvent(**kw)
        assert event.lineage == ()

    def test_lineage_with_valid_ids(self) -> None:
        """A lineage with all valid non-empty strings is accepted."""
        kw = _valid_kwargs()
        kw["lineage"] = ("evt-a", "evt-b", "evt-c")
        event = CanonicalEvent(**kw)
        assert event.lineage == ("evt-a", "evt-b", "evt-c")

    def test_lineage_parent_consistency(self) -> None:
        """When parent_event_id is set, it typically appears in lineage.
        This is a structural test, not enforced as invariant."""
        kw = _valid_kwargs()
        kw["parent_event_id"] = "parent-1"
        kw["lineage"] = ("root", "parent-1")
        event = CanonicalEvent(**kw)
        assert event.parent_event_id == "parent-1"
        assert event.parent_event_id in event.lineage


# ===================================================================
# Malformed payload validation (Track 2)
# ===================================================================


class TestMalformedPayloadValidation:
    """CanonicalEvent rejects malformed payloads at construction time."""

    def test_payload_dict_accepted(self) -> None:
        """A regular dict payload is frozen and accepted."""
        kw = _valid_kwargs()
        kw["payload"] = {"body": "hello", "count": 42}
        event = CanonicalEvent(**kw)
        assert event.payload["body"] == "hello"
        assert event.payload["count"] == 42

    def test_nested_payload_preserved(self) -> None:
        """Deeply nested payload values are preserved and frozen."""
        kw = _valid_kwargs()
        kw["payload"] = {"nested": {"deep": {"key": "val"}, "list": [1, 2]}}
        event = CanonicalEvent(**kw)
        assert event.payload["nested"]["deep"]["key"] == "val"  # type: ignore[index]
        # Lists are converted to tuples by _FrozenDict
        assert event.payload["nested"]["list"] == (1, 2)  # type: ignore[index]

    def test_empty_payload_accepted(self) -> None:
        """An empty payload dict is accepted."""
        kw = _valid_kwargs()
        kw["payload"] = {}
        event = CanonicalEvent(**kw)
        assert event.payload == {}

    def test_event_id_none_rejected_by_msgspec(self) -> None:
        """event_id=None is rejected (by __post_init__ validation)."""
        kw = _valid_kwargs()
        kw["event_id"] = None  # type: ignore[arg-type]
        with pytest.raises(ValueError, match="event_id"):
            CanonicalEvent(**kw)

    def test_event_kind_none_rejected_by_msgspec(self) -> None:
        """event_kind=None is rejected (by __post_init__ validation)."""
        kw = _valid_kwargs()
        kw["event_kind"] = None  # type: ignore[arg-type]
        with pytest.raises(ValueError, match="event_kind"):
            CanonicalEvent(**kw)

    def test_schema_version_none_rejected(self) -> None:
        """schema_version=None is rejected."""
        kw = _valid_kwargs()
        kw["schema_version"] = None  # type: ignore[arg-type]
        with pytest.raises((msgspec.ValidationError, TypeError)):
            CanonicalEvent(**kw)

    def test_timestamp_none_rejected(self) -> None:
        """timestamp=None is rejected."""
        kw = _valid_kwargs()
        kw["timestamp"] = None  # type: ignore[arg-type]
        with pytest.raises((msgspec.ValidationError, TypeError, AttributeError)):
            CanonicalEvent(**kw)


# ===================================================================
# Schema migration behavior (Track 2)
# ===================================================================


class TestSchemaMigrationBehavior:
    """Schema migration registry and contract behavior."""

    def test_migration_registry_starts_empty(self) -> None:
        """The global MIGRATION_REGISTRY has no registered migrations."""
        # We test the singleton; other tests should not have registered
        # migrations, but we check the API works.
        reg = MIGRATION_REGISTRY
        # The registry may have migrations from other tests, but the
        # lookup for a specific key should return None.
        assert reg.get("message.text", 1, 2) is None

    def test_migration_registry_register_and_get(self) -> None:
        """A migration can be registered and retrieved."""
        from medre.core.events.schema import _MigrationRegistry

        reg = _MigrationRegistry()

        def fn(p):
            return {**p, "new_field": "default"}

        reg.register("message.text", 1, 2, fn)
        result = reg.get("message.text", 1, 2)
        assert result is fn

    def test_migration_registry_get_unregistered(self) -> None:
        """Looking up an unregistered migration returns None."""
        from medre.core.events.schema import _MigrationRegistry

        reg = _MigrationRegistry()
        assert reg.get("message.text", 1, 2) is None

    def test_migration_registry_registered_keys(self) -> None:
        """registered_keys returns a frozenset of all registered keys."""
        from medre.core.events.schema import _MigrationRegistry

        reg = _MigrationRegistry()

        def fn(p):
            return p

        reg.register("message.text", 1, 2, fn)
        reg.register("telemetry.received", 2, 3, fn)
        keys = reg.registered_keys
        assert ("message.text", 1, 2) in keys
        assert ("telemetry.received", 2, 3) in keys

    def test_migration_registry_overwrite(self) -> None:
        """Registering the same key overwrites the previous migration."""
        from medre.core.events.schema import _MigrationRegistry

        reg = _MigrationRegistry()

        def fn1(p):
            return {**p, "v": 1}

        def fn2(p):
            return {**p, "v": 2}

        reg.register("message.text", 1, 2, fn1)
        reg.register("message.text", 1, 2, fn2)
        assert reg.get("message.text", 1, 2) is fn2

    def test_current_schema_version_is_1(self) -> None:
        """v1 is the current compatibility contract."""
        assert CURRENT_SCHEMA_VERSION == 1

    def test_schema_version_must_be_positive(self) -> None:
        """schema_version < 1 is rejected at construction."""
        kw = _valid_kwargs()
        kw["schema_version"] = 0
        with pytest.raises(ValueError, match="schema_version"):
            CanonicalEvent(**kw)

    def test_schema_version_1_accepted(self) -> None:
        """schema_version=1 is the baseline and always accepted."""
        kw = _valid_kwargs()
        kw["schema_version"] = 1
        event = CanonicalEvent(**kw)
        assert event.schema_version == 1


# ===================================================================
# Event taxonomy audit (Track 2)
# ===================================================================


class TestEventTaxonomyAudit:
    """Verify code taxonomy matches the documented contract."""

    def test_known_kinds_matches_event_kind_class(self) -> None:
        """Every EventKind constant appears in KNOWN_KINDS."""

        for attr in dir(EventKind):
            if attr.startswith("_"):
                continue
            val = getattr(EventKind, attr)
            if isinstance(val, str) and "." in val:
                assert (
                    val in KNOWN_KINDS
                ), f"EventKind.{attr}={val!r} missing from KNOWN_KINDS"

    def test_all_domains_covered(self) -> None:
        """All documented top-level domains are present."""
        domains = {kind.split(".")[0] for kind in KNOWN_KINDS}
        assert "message" in domains
        assert "telemetry" in domains
        assert "presence" in domains
        assert "identity" in domains
        assert "delivery" in domains
        assert "system" in domains
        assert "plugin" in domains

    def test_event_kind_count(self) -> None:
        """The number of known kinds is stable at 18."""
        assert len(KNOWN_KINDS) == 18

    def test_relation_types_match_constant(self) -> None:
        """EventRelation Literal types match VALID_RELATION_TYPES."""
        assert VALID_RELATION_TYPES == frozenset(
            {"reply", "reaction", "edit", "delete", "thread"}
        )

    def test_delivery_kinds_are_separate_from_message(self) -> None:
        """Delivery kinds use the 'delivery.' namespace, not 'message.'."""
        delivery_kinds = [k for k in KNOWN_KINDS if k.startswith("delivery.")]
        message_kinds = [k for k in KNOWN_KINDS if k.startswith("message.")]
        assert len(delivery_kinds) > 0
        assert len(message_kinds) > 0
        assert set(delivery_kinds).isdisjoint(set(message_kinds))


# ===================================================================
# Protocol-neutral readiness (Track 5)
# ===================================================================


class TestProtocolNeutralReadiness:
    """Verify that existing canonical mechanisms support future externally
    initiated adapters (webhooks, request/response) without schema
    changes.

    These tests exercise the usage patterns documented in
    docs/contracts/phase-1-limitations.md Section 2.2.
    """

    # -- Correlation via trace_id --

    def test_trace_id_survives_construction(self) -> None:
        """trace_id can be set to any string value."""
        kw = _valid_kwargs()
        kw["trace_id"] = "corr-abc-123"
        event = CanonicalEvent(**kw)
        assert event.trace_id == "corr-abc-123"

    def test_trace_id_none_is_valid(self) -> None:
        """Events without correlation context leave trace_id as None."""
        kw = _valid_kwargs()
        kw["trace_id"] = None
        event = CanonicalEvent(**kw)
        assert event.trace_id is None

    def test_trace_id_json_round_trip(self) -> None:
        """trace_id survives JSON encode/decode."""
        kw = _valid_kwargs()
        kw["trace_id"] = "webhook-corr-xyz"
        event = CanonicalEvent(**kw)
        decoded = msgspec.json.decode(msgspec.json.encode(event), type=CanonicalEvent)
        assert decoded.trace_id == "webhook-corr-xyz"

    def test_trace_id_msgpack_round_trip(self) -> None:
        """trace_id survives msgpack encode/decode."""
        kw = _valid_kwargs()
        kw["trace_id"] = "ext-trace-456"
        event = CanonicalEvent(**kw)
        decoded = msgspec.msgpack.decode(
            msgspec.msgpack.encode(event), type=CanonicalEvent
        )
        assert decoded.trace_id == "ext-trace-456"

    # -- Idempotency via metadata.custom --

    def test_idempotency_key_in_custom(self) -> None:
        """metadata.custom can carry an idempotency key."""
        meta = EventMetadata(custom={"idempotency_key": "req_abc123"})
        event = CanonicalEvent(**{**_valid_kwargs(), "metadata": meta})
        assert event.metadata.custom["idempotency_key"] == "req_abc123"

    def test_idempotency_key_round_trip(self) -> None:
        """Idempotency key in custom dict survives JSON round-trip."""
        meta = EventMetadata(
            custom={"idempotency_key": "req_def456", "source": "webhook"}
        )
        event = CanonicalEvent(**{**_valid_kwargs(), "metadata": meta})
        decoded = msgspec.json.decode(msgspec.json.encode(event), type=CanonicalEvent)
        assert decoded.metadata.custom["idempotency_key"] == "req_def456"
        assert decoded.metadata.custom["source"] == "webhook"

    def test_idempotency_key_immutability(self) -> None:
        """The idempotency key in custom is frozen after construction."""
        meta = EventMetadata(custom={"idempotency_key": "req_ghi789"})
        with pytest.raises(TypeError, match="immutable"):
            meta.custom["idempotency_key"] = "tampered"

    # -- Principal/auth context via metadata.custom --

    def test_principal_context_in_custom(self) -> None:
        """metadata.custom can carry a principal dict."""
        principal = {
            "type": "bearer_token",
            "subject": "service-account-42",
            "claims": {"role": "operator"},
        }
        meta = EventMetadata(custom={"principal": principal})
        event = CanonicalEvent(**{**_valid_kwargs(), "metadata": meta})
        stored = event.metadata.custom["principal"]
        assert isinstance(stored, dict)
        assert stored["type"] == "bearer_token"
        assert stored["subject"] == "service-account-42"

    def test_principal_context_round_trip(self) -> None:
        """Principal dict survives JSON round-trip with deep freezing."""
        principal = {"type": "apikey", "subject": "client-7", "scopes": ("read",)}
        meta = EventMetadata(custom={"principal": principal})
        event = CanonicalEvent(**{**_valid_kwargs(), "metadata": meta})
        decoded = msgspec.json.decode(msgspec.json.encode(event), type=CanonicalEvent)
        p = decoded.metadata.custom["principal"]
        assert isinstance(p, dict)
        assert p["type"] == "apikey"
        assert p["subject"] == "client-7"

    def test_principal_context_immutable(self) -> None:
        """Principal dict in custom is deeply frozen."""
        principal = {"type": "basic", "subject": "user-1"}
        meta = EventMetadata(custom={"principal": principal})
        p = meta.custom["principal"]
        assert isinstance(p, dict)
        with pytest.raises(TypeError, match="immutable"):
            p["subject"] = "tampered"

    # -- Request/response lineage --

    def test_request_response_lineage(self) -> None:
        """A response event can link to its request via parent_event_id
        and lineage."""
        now = datetime.now(timezone.utc)
        request = CanonicalEvent(
            event_id="req-001",
            event_kind="message.text",
            schema_version=1,
            timestamp=now,
            source_adapter="webhook-incoming",
            source_transport_id="api-client-1",
            source_channel_id="/webhooks/alerts",
            parent_event_id=None,
            lineage=(),
            relations=(),
            payload={"body": "alert triggered"},
            metadata=EventMetadata(
                transport=TransportMetadata(protocol="http"),
                custom={"idempotency_key": "req_001"},
            ),
            trace_id="trace-webhook-1",
        )

        response = CanonicalEvent(
            event_id="resp-001",
            event_kind="message.text",
            schema_version=1,
            timestamp=now,
            source_adapter="bridge-engine",
            source_transport_id="internal",
            source_channel_id=None,
            parent_event_id="req-001",
            lineage=("req-001",),
            relations=(),
            payload={"body": "alert forwarded"},
            metadata=EventMetadata(),
            trace_id="trace-webhook-1",
        )

        assert response.parent_event_id == "req-001"
        assert request.event_id in response.lineage
        assert response.trace_id == request.trace_id

    def test_lineage_chain_preserved_in_round_trip(self) -> None:
        """A multi-hop lineage chain survives serialization."""
        kw = _valid_kwargs()
        kw["parent_event_id"] = "evt-parent"
        kw["lineage"] = ("evt-origin", "evt-parent")
        kw["trace_id"] = "multi-hop-trace"
        event = CanonicalEvent(**kw)
        decoded = msgspec.json.decode(msgspec.json.encode(event), type=CanonicalEvent)
        assert decoded.lineage == ("evt-origin", "evt-parent")
        assert decoded.parent_event_id == "evt-parent"
        assert decoded.trace_id == "multi-hop-trace"

    # -- Inbound provenance --

    def test_inbound_provenance_fields(self) -> None:
        """source_adapter, source_transport_id, and source_channel_id
        can represent an externally initiated source."""
        meta = EventMetadata(
            transport=TransportMetadata(protocol="http", gateway_id="webhook-relay"),
        )
        event = CanonicalEvent(
            event_id="evt-wh-1",
            event_kind="message.text",
            schema_version=1,
            timestamp=datetime.now(timezone.utc),
            source_adapter="webhook-incoming",
            source_transport_id="api-client-42",
            source_channel_id="/webhooks/alerts",
            parent_event_id=None,
            lineage=(),
            relations=(),
            payload={"body": "incoming webhook payload"},
            metadata=meta,
            trace_id="wh-trace-1",
        )
        assert event.source_adapter == "webhook-incoming"
        assert event.source_transport_id == "api-client-42"
        assert event.source_channel_id == "/webhooks/alerts"
        assert event.metadata.transport is not None
        assert event.metadata.transport.protocol == "http"
        assert event.metadata.transport.gateway_id == "webhook-relay"

    def test_provenance_round_trip(self) -> None:
        """Externally initiated provenance fields survive serialization."""
        meta = EventMetadata(
            transport=TransportMetadata(protocol="http"),
            custom={
                "http.method": "POST",
                "http.path": "/webhooks/alerts",
                "idempotency_key": "wh_req_123",
            },
        )
        event = CanonicalEvent(
            event_id="evt-wh-2",
            event_kind="message.text",
            schema_version=1,
            timestamp=datetime.now(timezone.utc),
            source_adapter="webhook-incoming",
            source_transport_id="ext-svc-1",
            source_channel_id="/api/v1/events",
            parent_event_id=None,
            lineage=(),
            relations=(),
            payload={"body": "test"},
            metadata=meta,
            trace_id="wh-trace-2",
        )
        decoded = msgspec.json.decode(msgspec.json.encode(event), type=CanonicalEvent)
        assert decoded.source_adapter == "webhook-incoming"
        assert decoded.source_transport_id == "ext-svc-1"
        assert decoded.source_channel_id == "/api/v1/events"
        assert decoded.metadata.transport is not None
        assert decoded.metadata.transport.protocol == "http"
        assert decoded.metadata.custom["http.method"] == "POST"
        assert decoded.metadata.custom["idempotency_key"] == "wh_req_123"
        assert decoded.trace_id == "wh-trace-2"

    # -- Combined protocol-neutral event --

    def test_full_protocol_neutral_event_round_trip(self) -> None:
        """An event using all protocol-neutral mechanisms survives full
        JSON and msgpack round-trip with every field intact."""
        meta = EventMetadata(
            transport=TransportMetadata(
                protocol="http",
                gateway_id="api-gateway",
            ),
            native=NativeMetadata(
                data={"http.headers": {"content-type": "application/json"}}
            ),
            custom={
                "idempotency_key": "req_full_001",
                "principal": {
                    "type": "bearer_token",
                    "subject": "svc-acct-1",
                },
                "http.method": "POST",
                "http.path": "/webhooks/events",
            },
        )
        now = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
        event = CanonicalEvent(
            event_id="evt-pn-full",
            event_kind="message.text",
            schema_version=1,
            timestamp=now,
            source_adapter="webhook-incoming",
            source_transport_id="external-service-a",
            source_channel_id="/webhooks/events",
            parent_event_id="evt-origin-1",
            lineage=("evt-origin-1",),
            relations=(),
            payload={"body": "full protocol-neutral test"},
            metadata=meta,
            trace_id="pn-trace-full-001",
        )

        # JSON round-trip
        json_decoded = msgspec.json.decode(
            msgspec.json.encode(event), type=CanonicalEvent
        )
        assert json_decoded.event_id == "evt-pn-full"
        assert json_decoded.trace_id == "pn-trace-full-001"
        assert json_decoded.source_adapter == "webhook-incoming"
        assert json_decoded.source_transport_id == "external-service-a"
        assert json_decoded.source_channel_id == "/webhooks/events"
        assert json_decoded.parent_event_id == "evt-origin-1"
        assert json_decoded.lineage == ("evt-origin-1",)
        assert json_decoded.metadata.custom["idempotency_key"] == "req_full_001"
        principal = json_decoded.metadata.custom["principal"]
        assert isinstance(principal, dict)
        assert principal["subject"] == "svc-acct-1"
        assert json_decoded.metadata.transport is not None
        assert json_decoded.metadata.transport.protocol == "http"

        # msgpack round-trip
        msgpack_decoded = msgspec.msgpack.decode(
            msgspec.msgpack.encode(event), type=CanonicalEvent
        )
        assert msgpack_decoded.event_id == "evt-pn-full"
        assert msgpack_decoded.trace_id == "pn-trace-full-001"
        assert msgpack_decoded.metadata.custom["idempotency_key"] == "req_full_001"

    # -- Native namespace extensibility --

    def test_native_namespace_carries_adapter_specific_data(self) -> None:
        """metadata.native can carry arbitrary transport-specific fields
        without affecting the canonical schema."""
        meta = EventMetadata(
            native=NativeMetadata(
                data={
                    "webhook": {
                        "signature": "sha256=abc123",
                        "event_type": "incident.created",
                        "delivery_id": "dlv-xyz",
                    }
                }
            ),
        )
        event = CanonicalEvent(**{**_valid_kwargs(), "metadata": meta})
        assert event.metadata.native is not None
        native_data = event.metadata.native.data
        wh = native_data["webhook"]
        assert isinstance(wh, dict)
        assert wh["event_type"] == "incident.created"
        assert wh["delivery_id"] == "dlv-xyz"

        # Round-trip preserves native data
        decoded = msgspec.json.decode(msgspec.json.encode(event), type=CanonicalEvent)
        assert decoded.metadata.native is not None
        wh_rt = decoded.metadata.native.data["webhook"]
        assert isinstance(wh_rt, dict)
        assert wh_rt["event_type"] == "incident.created"
