# 12: Adapter/Platform Identity Audit

**Status:** Draft
**Scope:** Tracks 1 (adapter identity concepts) and 4 (canonical identity pressure) from the PC specification
**Related:** 65-constrained-transport-comparison.md, 02-adapter-runtime-contract.md

## Overview

MEDRE's adapter layer carries several identity-related concepts. Some are scoped to a single adapter instance. Some belong to the platform (protocol) the adapter speaks. Some are about routing, some about rendering, and some about cryptographic verification. This document audits what those concepts are, where they overlap, where they're overloaded, and where pressure is building for a cleaner separation.

This is an audit, not a redesign. The goal is to document what exists and where the seams are, so future work knows what to tighten.

---

## 1. Current Identity Concepts

### 1.1 adapter_id

The `adapter_id` is a string that names a specific adapter instance. It comes from configuration. Examples: `"local-radio"`, `"meshcore-out"`, `"matrix_home"`. It's how the rest of the system refers to this particular adapter at runtime.

```python
# AdapterInfo (src/medre/core/contracts/adapter.py)
adapter_id: str

# AdapterContext (src/medre/core/contracts/adapter.py)
adapter_id: str

# AdapterContract (src/medre/core/contracts/adapter.py)
adapter_id: str
```

The adapter_id is **transport-local**. It names an instance within a running MEDRE process. Two different MEDRE deployments could use the same adapter_id for completely different radio hardware.

### 1.2 platform

The `platform` string identifies which protocol the adapter speaks: `"meshtastic"`, `"meshcore"`, `"matrix"`. It's a human-readable label that groups adapters by protocol family.

```python
# AdapterInfo (src/medre/core/contracts/adapter.py)
platform: str  # e.g. "meshtastic", "meshcore", "matrix"
```

The platform is **platform-level** (protocol identity). It doesn't vary per instance. Any Meshtastic adapter has `platform = "meshtastic"`. It answers "what language does this adapter speak?" not "which specific radio is this?"

### 1.3 role

The `AdapterRole` enum classifies the adapter's function:

```python
class AdapterRole(Enum):
    TRANSPORT = "transport"
    PRESENTATION = "presentation"
    HYBRID = "hybrid"
```

Matrix is PRESENTATION. Meshtastic and MeshCore are TRANSPORT. The role is **platform-level**, meaning it's a property of the protocol family, not the instance.

### 1.4 target_adapter

`target_adapter` appears in several places as a string naming the adapter that should receive a rendered event. It's an adapter_id value, not a platform name.

```python
# RenderingResult (src/medre/core/rendering/renderer.py)
target_adapter: str

# DeliveryReceipt (src/medre/core/events/canonical.py)
target_adapter: str

# Renderer.can_render(event, target_adapter, target_platform)
# Renderer.render(event, target_adapter, target_channel)
```

### 1.5 source_adapter

`source_adapter` on `CanonicalEvent` records which adapter produced the event. It's an adapter_id value.

```python
# CanonicalEvent (src/medre/core/events/canonical.py)
source_adapter: str
```

### 1.6 source_transport_id

`source_transport_id` on `CanonicalEvent` carries the native actor identity from the originating transport. What this string contains depends entirely on which adapter produced the event.

```python
# CanonicalEvent
source_transport_id: str
```

Examples by platform:

| Platform   | What source_transport_id carries    | Example                   |
| ---------- | ----------------------------------- | ------------------------- |
| Matrix     | MXID                                | `@alice:example.com`      |
| Meshtastic | Node number (stringified) or fromId | `"42"` or `"+1234567890"` |
| MeshCore   | Ed25519 public key hex              | `"a3f1b2c4..."`           |

### 1.7 source_native_ref

`source_native_ref` on `CanonicalEvent` is an optional `NativeRef` pointing back to the original message in the source adapter's namespace.

```python
# CanonicalEvent
source_native_ref: NativeRef | None = None

# NativeRef (src/medre/core/events/canonical.py)
#   adapter: str
#   native_channel_id: str | None
#   native_message_id: str
#   native_thread_id: str | None
```

This records where the event came from in the native protocol's terms. For Matrix, `native_message_id` is the event ID. For Meshtastic, it's the packet ID (stringified int). For MeshCore, it's the sender timestamp (stringified int).

### 1.8 NativeMessageRef.adapter

`NativeMessageRef` persists the mapping between a canonical event and a native message. Its `adapter` field is an adapter_id, recording which adapter instance owns that native namespace.

```python
# NativeMessageRef (src/medre/core/events/canonical.py)
adapter: str  # adapter_id
native_channel_id: str | None
native_message_id: str
native_thread_id: str | None
native_relation_id: str | None
direction: Literal["inbound", "outbound"]
```

### 1.9 RenderingResult.target_adapter

The output of rendering carries `target_adapter` so the delivery pipeline knows where to send it. This is an adapter_id value.

```python
# RenderingResult
target_adapter: str
```

### 1.10 Renderer.can_render(target_adapter) dispatch mechanism

The renderer dispatch is a capability-tiered lookup in each renderer's `can_render()` method:

1. **Platform match** (primary): `target_platform == "meshtastic"`. The pipeline passes the platform string from its internal registry.
2. **Adapter-name prefix** (fallback): `target_adapter.startswith("meshtastic")`.
3. **known_adapters set** (fallback): `target_adapter in self._known_adapters`.

```python
# MeshtasticRenderer.can_render() (src/medre/adapters/meshtastic/renderer.py)
if target_platform == self._PLATFORM:
    return True
if target_adapter.startswith("meshtastic"):
    return True
return target_adapter in self._known_adapters
```

All three renderers (Matrix, Meshtastic, MeshCore) follow this same pattern with their respective platform names and prefixes. The platform registry on `RenderingPipeline` populates the primary tier at build time. Prefix and `known_adapters` fallback tiers serve test code and ad hoc usage where the registry is not populated.

---

## 2. Where Concepts Overlap Incorrectly

### 2.1 Renderer selection relies on adapter naming conventions

The `can_render()` prefix match (`target_adapter.startswith("meshtastic")`) means the renderer selection logic is coupled to how operators name their adapters. If someone names a Meshtastic adapter `"local-radio"`, the prefix match fails and the renderer won't select it.

The `known_adapters` fallback was introduced to handle this case: operators pass the set of adapter IDs that should match. But this is manual configuration that duplicates per-transport-family. Every Meshtastic adapter ID has to be listed in the MeshtasticRenderer constructor. Every MeshCore adapter ID has to be listed in the MeshCoreRenderer constructor. This is fragile and doesn't scale.

### 2.2 known_adapters duplicates per-transport-family

The `known_adapters` parameter exists on both `MeshtasticRenderer` and `MeshCoreRenderer`. Each renderer instance maintains its own set of adapter IDs. When a new adapter is added, every renderer that might need to handle it must be updated. There's no central registry.

The platform registry on `RenderingPipeline` is the proper fix, and it is now in place as the primary dispatch tier. The `known_adapters` sets serve as fallback tiers for test and ad hoc usage where the registry is not populated.

### 2.3 target_adapter is overloaded

`target_adapter` serves two distinct purposes:

1. **Routing target**: "deliver this event to adapter X" (used by delivery pipeline, receipts).
2. **Renderer selection key**: "which renderer should format this event?" (used by `can_render()`).

These are different questions. The routing target is an adapter_id, which is an instance identifier. The renderer selection should be based on the platform (what protocol does this adapter speak?), not the instance name. When a single string serves both roles, the renderer dispatch ends up depending on instance naming conventions.

The platform registry decouples these two concerns. `target_adapter` remains the routing target. `target_platform` is the renderer selection key.

---

## 3. Transport-Local vs Platform-Level

The identity concepts split into two scopes:

| Concept               | Scope           | What it identifies                                              |
| --------------------- | --------------- | --------------------------------------------------------------- |
| `adapter_id`          | Transport-local | A specific adapter instance in this MEDRE process               |
| `platform`            | Platform-level  | The protocol family (Meshtastic, MeshCore, Matrix)              |
| `role`                | Platform-level  | The functional classification (TRANSPORT, PRESENTATION, HYBRID) |
| `target_adapter`      | Transport-local | Routing destination (an adapter_id)                             |
| `source_adapter`      | Transport-local | Event origin (an adapter_id)                                    |
| `source_transport_id` | Transport-local | Native actor identity (MXID, node number, pubkey)               |
| `target_platform`     | Platform-level  | Renderer selection key (resolved from adapter_id via registry)  |

The important distinction: `adapter_id` is meaningful only within a single MEDRE deployment. `platform` is meaningful across deployments and even across implementations. Code that needs to answer "what protocol is this?" should use `platform`, not `adapter_id`.

---

## 4. Renderer Selection Dependencies on Adapter Naming

Before the platform registry, renderer selection had this dependency chain:

```
adapter_id (config) → prefix match in can_render() → renderer selected
```

If adapter_id was `"meshtastic_radio"`, the prefix `"meshtastic"` matched and the MeshtasticRenderer was selected. If adapter_id was `"local-radio"`, it didn't match unless the operator also added it to `known_adapters`.

With the platform registry, the dependency chain is:

```
adapter_id (config) → RenderingPipeline._adapter_platforms lookup → platform string → renderer selected
```

The platform registry breaks the naming dependency. The pipeline resolves `adapter_id` → `platform` at startup, then passes `platform` to each renderer's `can_render()`. The renderer never sees the adapter_id for selection purposes. It only sees it for routing (in `RenderingResult.target_adapter`).

The prefix match and `known_adapters` remain as fallbacks for code paths that don't populate the registry (test code, ad hoc usage). They should not be used in production paths.

---

## 5. Emerging Canonical Identity Pressure (Track 4)

MEDRE currently conflates several distinct identity categories into `source_transport_id` (a string) and `NativeMetadata.data` (a dict). These categories will need clearer boundaries as the identity system matures.

### 5.1 Four identity categories

**Transport-local identity** is the native actor ID from the protocol itself:

| Platform   | Transport-local ID | Type         | Notes                                 |
| ---------- | ------------------ | ------------ | ------------------------------------- |
| Matrix     | MXID               | String       | `@user:server.org`                    |
| Meshtastic | NodeNum / fromId   | Int / String | Numeric node number or phone-style ID |
| MeshCore   | pubkey_prefix      | Hex string   | First bytes of Ed25519 public key     |

This is what `source_transport_id` currently carries. It works as a string, but it loses type information. A Meshtastic node number `42` becomes the string `"42"`. A MeshCore pubkey hex `"a3f1..."` is just a string. The consumer has to know which platform produced the event to interpret it.

**Canonical actor identity** is what `IdentityResolver` normalizes to. A `CanonicalActor` has a stable UUID and links to one or more `NativeIdentity` instances:

```python
# NativeIdentity (src/medre/core/identity/actor.py)
platform: str
adapter_id: str
native_id: str

# CanonicalActor
actor_id: str  # stable UUID
linked_identities: list[NativeIdentity]
```

The resolver indexes by `(platform, adapter_id, native_id)` triples. This works, but it means the canonical identity is tied to the adapter instance. Two Meshtastic adapters seeing the same node would create two different `NativeIdentity` entries (different `adapter_id`) that need to be manually linked to the same `CanonicalActor`.

**Cryptographic identity** is key-based verification:

| Platform   | Crypto identity                                    | Verification   |
| ---------- | -------------------------------------------------- | -------------- |
| Matrix     | None at the protocol layer (TLS handles transport) | Server-trusted |
| Meshtastic | None native (optional admin key)                   | None           |
| MeshCore   | Ed25519 public key                                 | Always-on E2EE |

MeshCore has real cryptographic identity baked in. Every message is signed. Every contact is a public key. Meshtastic has nothing comparable at the protocol layer. Matrix delegates trust to the homeserver. The `VerificationStatus` enum anticipates this:

```python
class VerificationStatus(Enum):
    UNVERIFIED = "unverified"
    CRYPTOGRAPHIC = "cryptographic"
    OPERATOR_LINKED = "operator_linked"
    ADAPTER_ASSERTED = "adapter_asserted"
```

But nothing in the current system populates `CRYPTOGRAPHIC` for MeshCore yet. The identity resolver creates all actors as `UNVERIFIED`. The cryptographic verification layer is a future tranche.

**Presentation identity** is the human-facing metadata:

| Platform   | Display name                     | Avatar     | Profile      |
| ---------- | -------------------------------- | ---------- | ------------ |
| Matrix     | Display name from profile        | Avatar URL | Full profile |
| Meshtastic | Short name from config (4 chars) | None       | None         |
| MeshCore   | `adv_name` from contact          | None       | None         |

`NativeIdentity.display_name` captures this at observation time, but there's no mechanism to update it when it changes, and no concept of "preferred" display name across platforms.

### 5.2 Where the conflation lives

These four categories are currently stuffed into two fields:

- `source_transport_id` (string on CanonicalEvent): carries transport-local identity. Loses all type information.
- `NativeMetadata.data` (dict): carries everything else (radio metadata, contact metadata, display names) in an unstructured bag.

The `NativeIdentity` model is cleaner, with separate `platform`, `adapter_id`, `native_id`, `display_name`, and `metadata` fields. But it's only used by the identity subsystem. The event pipeline still works with raw strings and dicts.

### 5.3 The pressure

This isn't broken yet, but it's getting tighter. Three specific pressure points:

1. **Cross-adapter linking**: When the same physical operator uses both Meshtastic and MeshCore radios, linking their identities requires manual operator intervention (`OPERATOR_LINKED`). There's no automated way to say "Meshtastic node 42 and MeshCore pubkey a3f1... are the same person." The infrastructure for this exists in `IdentityResolver.link_identity()`, but there's no automation behind it.

2. **Type loss in source_transport_id**: Downstream code that needs to interpret the transport ID has to check `source_adapter` or `platform` first. This works, but it's implicit. There's no type tag on the string itself.

3. **Cryptographic verification gap**: MeshCore provides cryptographic identity that MEDRE ignores. Every MeshCore event arrives with a verified Ed25519 public key, but MEDRE treats it the same as an unverified Meshtastic node number. Future work should propagate MeshCore's cryptographic verification into `VerificationStatus.CRYPTOGRAPHIC`.

These are documented here as pressure points, not as design requirements for the current tranche.

---

## 6. Transport-Family Semantics (Track 3)

This section documents the semantic differences between the three adapter families across the dimensions that matter for MEDRE's abstractions.

### 6.1 Message graph richness

| Dimension | Matrix                   | Meshtastic              | MeshCore |
| --------- | ------------------------ | ----------------------- | -------- |
| Replies   | Native: `m.in_reply_to`  | Native: `replyId` (int) | None     |
| Reactions | Native: `m.reaction`     | None                    | None     |
| Edits     | Native: `m.replace`      | None                    | None     |
| Deletes   | Native: redaction event  | None                    | None     |
| Threads   | Native: thread relations | None                    | None     |

Matrix has a rich message graph with first-class relations for replies, reactions, edits, and deletes. Meshtastic has a single `replyId` field at the packet layer. MeshCore has nothing. MEDRE's `EventRelation` model handles Matrix and Meshtastic but assumes the protocol can carry a relation reference, which MeshCore cannot.

### 6.2 Reply semantics

**Matrix**: Native reply via `m.relates_to` / `m.in_reply_to`. The reply references the target event's `event_id`. Rich: includes fallback text and rendered reply body.

**Meshtastic**: `replyId` field on the MeshPacket protobuf. References the original packet's integer ID. Minimal: no fallback text, no structured body.

**MeshCore**: No native reply mechanism. Any reply relationship would need to be expressed at the application layer (e.g., quoting text in the message body). The adapter can populate `EventRelation` with `target_native_ref = None` and `fallback_text` from parsed message content, or simply not populate relation fields.

### 6.3 Native refs

| Platform   | Message ID type        | Example                | Scope              |
| ---------- | ---------------------- | ---------------------- | ------------------ |
| Matrix     | `event_id` string      | `"$abc123:server.org"` | Global (federated) |
| Meshtastic | `packet_id` int        | `42`                   | Local (per-node)   |
| MeshCore   | `sender_timestamp` int | `1715234567`           | Per-sender         |

Matrix event IDs are globally unique across federation. Meshtastic packet IDs are locally scoped; two nodes can produce the same packet ID. MeshCore sender timestamps are unique per-sender but can collide across senders.

### 6.4 Actor identity

| Platform   | Actor ID                      | Persistence       | Uniqueness      |
| ---------- | ----------------------------- | ----------------- | --------------- |
| Matrix     | MXID (`@user:server.org`)     | Server-bound      | Globally unique |
| Meshtastic | NodeNum (int) or fromId (str) | Ephemeral session | Local mesh      |
| MeshCore   | Ed25519 pubkey (32B hex)      | Key material      | Globally unique |

Matrix and MeshCore have stable, globally unique actor identities. Meshtastic node numbers are ephemeral; a node that leaves and rejoins the mesh may get a different number.

### 6.5 Addressing model

| Platform   | Model         | Send targets                                  |
| ---------- | ------------- | --------------------------------------------- |
| Matrix     | Room-based    | Room ID (group) or room ID (DM room)          |
| Meshtastic | Broadcast/DM  | Channel index (broadcast) or node number (DM) |
| MeshCore   | Contact-based | Pubkey (direct) or flood (broadcast)          |

Matrix routes messages to rooms. Meshtastic routes to channels (broadcast) or specific nodes. MeshCore routes to contacts (pubkeys) or floods. All three reduce to `target_channel` and optional routing metadata at the MEDRE boundary.

### 6.6 Delivery expectations

| Platform   | Model                | Confirmation                                |
| ---------- | -------------------- | ------------------------------------------- |
| Matrix     | Sync `/sync` confirm | Implicit in sync response                   |
| Meshtastic | Async ACK packet     | ROUTING_APP ACK, separate from send         |
| MeshCore   | Async ACK event      | ACK event with CRC code, separate from send |

Matrix confirms delivery synchronously through the sync loop. Both radio protocols use asynchronous ACKs. MeshCore's ACK includes a CRC code that the sender can verify against the original payload.

### 6.7 Constrained payloads

| Platform   | Payload limit | Encoding overhead       |
| ---------- | ------------- | ----------------------- |
| Matrix     | ~100 KB       | JSON (verbose)          |
| Meshtastic | ~228 bytes    | Protobuf (compact)      |
| MeshCore   | 184 bytes     | Custom binary (minimal) |

Both constrained transports require aggressive payload truncation. The `max_text_bytes` capability declaration handles this, and each adapter declares its limit at registration. The renderers apply truncation during rendering.

### 6.8 Pacing and queue ownership

Each adapter owns its own send queue. Meshtastic paces at roughly 0.5 seconds between packets. MeshCore paces at roughly 2 seconds. Matrix has no meaningful rate limit at meshnet scale.

The queue ownership is adapter-local. No shared pacing state. This is clean and protocol-neutral.

### 6.9 Canonical-event pressure: transport-neutral vs accidentally shaped

**Genuinely transport-neutral abstractions:**

- `source_transport_id` as a string (all three reduce to strings)
- `NativeMetadata.data` dict (swallows all metadata without structural assumptions)
- `max_text_bytes` / `max_text_chars` capability declarations
- Adapter-owned pacing queues
- `AdapterDeliveryResult` with adapter-internal ID extraction
- `AdapterRole` enum
- `IdentityResolver` native-to-canonical mapping

**Accidentally shaped by specific protocols:**

- `EventRelation.target_native_ref` assumes the protocol carries a reply reference (true for Matrix and Meshtastic, false for MeshCore). The relation model should be capability-gated.
- `native_message_id` as a single scalar string works for all three, but MeshCore's send result bundles `expected_ack` and `suggested_timeout` alongside the timestamp ID. If delivery tracking needs the richer structure, `AdapterDeliveryResult` may need an optional field.
- `NativeIdentity.native_id` is a string, losing the type distinction between an MXID (structured), a node number (integer), and a pubkey (binary). This works at the resolver level but makes cross-protocol comparison harder.
