# Phase 1 Limitations

> Document version: 1
> Last updated: 2026-05-08

This document explicitly records what Phase 1 does **not** implement, what is reserved for future phases, and what behavioral contracts are locked in for backward compatibility.

---

## 1. Schema Migration

### Current State

- **`CURRENT_SCHEMA_VERSION = 1`** is the baseline compatibility contract.
- **No migrations are executed.** The `_MigrationRegistry` provides a registry-only hook (`register` / `get` API) but no automatic migration pipeline.
- Events with `schema_version > 1` are accepted at construction without transformation.
- Events with `schema_version < 1` are rejected by `CanonicalEvent.__post_init__`.

### Contract Guarantees

| Guarantee | Description |
|-----------|-------------|
| New fields append with defaults | Future schema versions add fields; existing consumers read `v1` fields normally |
| Existing fields deprecated, not removed | A deprecated field remains populated for at least one version cycle |
| Unknown fields preserved | msgspec skips unknown struct fields during decode (forward compatibility) |
| `schema_version >= 1` | Enforced at construction; the minimum valid version is 1 |

### What Phase 1 Does NOT Do

- No automatic payload migration on decode
- No schema negotiation between adapters and runtime
- No deprecation warnings at runtime
- No schema version downgrade logic
- Adapters are responsible for producing events at the version they support

---

## 2. Protocol-Neutral Readiness

### What Exists

The canonical event model is transport-agnostic by design:

| Feature | Location | Status |
|---------|----------|--------|
| Correlation IDs | `trace_id` field on `CanonicalEvent` | Available, not populated by default |
| Idempotency keys | `metadata.custom["idempotency_key"]` | Convention; not enforced |
| Principal/auth context | `metadata.custom["principal"]` | Reserved; not populated |
| Request/response lineage | `lineage` + `parent_event_id` | Mechanism exists |
| Inbound provenance | `source_adapter` + `source_transport_id` | Always populated |
| Event kind registry | `EventKind` constants + `KNOWN_KINDS` | 18 kinds across 7 domains |

### What Phase 1 Does NOT Implement

- No HTTP/webhook server or listener
- No RPC framework or API surface
- No authentication or authorization framework
- No Matrix transport implementation
- No real transport adapters (only the event model and contracts)
- No protocol-specific fields beyond what adapters define in `metadata.native`

### Future Webhook Readiness

The following protocol-neutral concepts are documented here for future reference but are **not** implemented:

| Concept | Notes |
|---------|-------|
| **Correlation IDs** | `trace_id` on `CanonicalEvent`; maps to HTTP `X-Correlation-ID` or similar headers |
| **Idempotency keys** | Consumers should use `metadata.custom["idempotency_key"]` for deduplication |
| **Principal/auth context** | Reserved in `metadata.custom["principal"]`; no auth framework exists |
| **Request/response lineage** | Use `parent_event_id` and `lineage` to correlate request-response pairs |
| **Inbound provenance** | `source_adapter` + `source_transport_id` identify the origin; extensible for new transports |

---

## 3. Event Taxonomy

### Locked-In Kinds (18 total)

The following 18 event kinds are the canonical taxonomy for Phase 1:

**Message domain** (6): `message.created`, `message.text`, `message.reacted`, `message.edited`, `message.deleted`, `message.file`

**Telemetry domain** (2): `telemetry.received`, `telemetry.position`

**Presence domain** (1): `presence.changed`

**Identity domain** (1): `identity.updated`

**Delivery domain** (5): `delivery.accepted`, `delivery.queued`, `delivery.sent`, `delivery.confirmed`, `delivery.failed`

**System domain** (2): `system.audit`, `system.lifecycle`

**Plugin domain** (1): `plugin.custom`

### Taxonomy Notes

- Kinds follow `<domain>.<action>` naming convention.
- The `plugin.custom` kind reserves a namespace for extension events.
- Plugins should append sub-kinds in the payload rather than inventing new top-level kinds.
- The taxonomy is exported in `EventKind` constants and `KNOWN_KINDS` frozenset.

### Divergence from Earlier Spec

The initial spec document listed a simplified taxonomy (`telemetry`, `position`, `presence`, `metrics.update`, `channel.announcement`, `plugin.event`, `delivery.receipt`, `transform.output`, `policy.action`). The code taxonomy is more granular:

| Spec Kind | Code Equivalent |
|-----------|-----------------|
| `telemetry` | `telemetry.received` |
| `position` | `telemetry.position` |
| `presence` | `presence.changed` |
| `delivery.receipt` | Tracked via `DeliveryReceipt` records, not event kinds |
| `plugin.event` | `plugin.custom` |
| `metrics.update` | Not implemented (future) |
| `channel.announcement` | Not implemented (future) |
| `transform.output` | Not implemented (future) |
| `policy.action` | Not implemented (future) |

---

## 4. Serialization

### Current Behavior

- **JSON**: `msgspec.json.encode()` / `msgspec.json.decode()` — deterministic field ordering, forward-compatible (unknown fields skipped).
- **MessagePack**: `msgspec.msgpack.encode()` / `msgspec.msgpack.decode()` — binary encoding, same forward-compatibility.
- **Immutability**: All dict fields wrapped in `_FrozenDict`; tuples for ordered collections.
- **Determinism**: Repeated encoding of the same `CanonicalEvent` produces identical bytes.

### Limitations

- No schema validation on decode (msgspec validates types but not semantic constraints).
- No content-type negotiation.
- No compression or encoding options.

---

## 5. Validation

### What Is Validated

| Invariant | Enforced By | Phase |
|-----------|-------------|-------|
| `event_id` non-empty string | `CanonicalEvent.__post_init__` | Construction |
| `event_kind` non-empty string | `CanonicalEvent.__post_init__` | Construction |
| `schema_version >= 1` | `CanonicalEvent.__post_init__` | Construction |
| `timestamp` timezone-aware | `CanonicalEvent.__post_init__` | Construction |
| `depth >= 0` | `CanonicalEvent.__post_init__` | Construction |
| `lineage` not None | `CanonicalEvent.__post_init__` | Construction |
| `relations` not None | `CanonicalEvent.__post_init__` | Construction |
| `lineage` items non-empty strings | `CanonicalEvent.__post_init__` | Construction |
| `relation_type` in known set | `EventRelation.__post_init__` | Construction |

### What Is NOT Validated

| Not Validated | Notes |
|---------------|-------|
| `event_id` is UUIDv7 | Only checked for non-empty string |
| `event_kind` is registered | Any non-empty string accepted; `is_registered()` available for optional checking |
| Payload structure per kind | Payload is opaque at this layer; schema validators registered via `SchemaRegistry` |
| `parent_event_id` references | No referential integrity check |
| `lineage` ordering | Items are checked for validity but not for chronological ordering |
| `lineage` / `parent_event_id` consistency | Not enforced; `parent_event_id` may or may not appear in `lineage` |
