# Transport-Native Identity Enrichment Audit

Factual audit of how each MEDRE transport adapter projects its native
sender identity into the generic `RelayAttribution` sender fields. This
document is evidence of review, not normative authority. The normative
sender-identity semantics live in
[Routing and Delivery §17.5.9](../spec/routing-delivery.md), and the
per-transport projection rules live in each
[transport profile](../spec/transport-profiles/). Where this audit
conflicts with the spec, the spec takes precedence.

The companion audit
[Relay Prefix and Provenance Audit](relay-prefix-attribution-audit.md)
covers prefix-template rendering and provenance. This audit focuses on
the identity-enrichment boundary: adapter-native state and metadata
projected to generic sender fields that the renderer consumes.

## Scope and Boundary

Identity enrichment flows in one direction:

```text
adapter-native state/metadata
        │
        ▼
adapter-local enrichment/projection (per-adapter attribution module)
        │
        ▼
generic RelayAttribution sender fields
        │
        ▼
renderer templates ({sender}, {sender_short}, {sender_id}, {sender_handle})
```

Core rendering is transport-neutral. It consumes only the generic
`RelayAttribution` struct fields and never inspects native transport
keys. Each adapter owns its native-to-generic projection helper
(`project_<transport>_attribution`); the dispatch module
(`_attribution_dispatch.project_source_fields`) only detects the
platform and delegates.

The four generic sender fields audited here:

| Canonical field             | Template alias    | Meaning                                      |
| --------------------------- | ----------------- | -------------------------------------------- |
| `source_sender_id`          | `{sender_id}`     | Stable native sender identifier (may opaque) |
| `source_sender_label`       | `{sender}`        | Best human-readable sender label             |
| `source_sender_short_label` | `{sender_short}`  | Compact human-readable sender label          |
| `source_sender_handle`      | `{sender_handle}` | Address/handle form when meaningful          |

Identity enrichment is **observational**. It is not delivery evidence,
not authoritative storage state, and may be stale. Prefix rendering
remains safe when every identity label is empty: the formatter
coalesces `None` to the empty string and never renders the literal
`"None"`.

---

## Matrix

### Native identity at ingress (Matrix)

The Matrix codec decodes each inbound room event into native metadata
carrying the sender MXID (`@user:domain`) in the `sender` key. After
codec decode, the adapter enriches native metadata with the room-member
display name. When no member display name is available, the adapter
falls back to the MXID as the `displayname` value (an adapter-level
decision for live rendering).

### Locally-available contact data (Matrix)

Matrix room-member display names come from the homeserver sync state
held by the `mindroom-nio` client. No extra network call is issued
during enrichment; the display name is read from already-synced member
state. Display names converge with homeserver member state and may lag
profile changes on the homeserver.

### Projection (`project_matrix_attribution`)

| Generic field               | Source                                                                     |
| --------------------------- | -------------------------------------------------------------------------- |
| `source_sender_id`          | `sender` (full MXID)                                                       |
| `source_sender_handle`      | `sender` (full MXID; Matrix MXID is the handle form)                       |
| `source_sender_label`       | `displayname` or `display_name` (display name only, no localpart fallback) |
| `source_sender_short_label` | MXID localpart via `extract_mxid_localpart` (empty when absent)            |

The dispatch projection populates `source_sender_label` from the display
name only. An empty or absent display name leaves `source_sender_label`
as `None`; the localpart is never substituted into the label. The
adapter-level displayname fallback (MXID-as-displayname) affects live
rendering enrichment, not the dispatch projection rules.

`extract_mxid_localpart` is deterministic for malformed MXIDs: an empty
localpart after `@` (e.g. `@:domain`) returns `""` rather than including
the colon and domain.

### What remains opaque (Matrix)

Nothing. The MXID is a stable, globally-scoped identifier and is always
exposed via `source_sender_id` and `source_sender_handle`.

### mmrelay wire-key boundary

The Matrix display name is **never** converted to mmrelay wire keys
(`KEY_LONGNAME` / `KEY_SHORTNAME`). The renderer reads mmrelay wire keys
from the inbound event content (`_capture_mmrelay_fields`) when they are
present; it does not synthesize them from the display name. mmrelay
`KEY_LONGNAME` / `KEY_SHORTNAME` are isolated wire-compatibility fields,
not MEDRE attribution variables.

### Intentionally deferred (Matrix)

No Matrix-specific identity enrichment is deferred. Display-name
staleness is inherent to homeserver member-state sync and is not a
MEDRE-managed concern.

---

## Meshtastic

### Native identity at ingress (Meshtastic)

Each inbound Meshtastic packet carries a numeric `from` node identifier
(`fromId`) and a `channel` index. Text packets do not carry a
packet-level name field; sender names live in the SDK node database,
populated by `NODEINFO_APP` packets.

### Locally-available contact data (Meshtastic)

The Meshtastic session exposes the SDK client's in-memory `nodes` dict
via `session.get_node_info(node_id)`. This is a network-free read of
state already populated by the SDK's NODEINFO handling. The lookup
returns `{"longname": ..., "shortname": ...}` or `None`, and tolerates
partial, `None`, non-dict, and empty entries.

### Enrichment pipeline

The enrichment pipeline runs end-to-end at ingress:

1. `adapter._enrich_with_node_info(packet)` calls
   `session.get_node_info(from_id)` and returns `None` on any exception.
2. `codec.decode(packet, node_info=...)` embeds the bare `longname` and
   `shortname` keys into native metadata. These are
   Meshtastic-characteristic keys recognised by the dispatch platform
   detector.

### Projection (`project_meshtastic_attribution`)

| Generic field               | Source                                                          |
| --------------------------- | --------------------------------------------------------------- |
| `source_sender_id`          | `from_id`, falling back to `source_transport_id`                |
| `source_sender_label`       | `longname` → `shortname` → `source_sender_id`                   |
| `source_sender_short_label` | `shortname` → compact(`longname`) → compact(`source_sender_id`) |
| `source_sender_handle`      | Not produced (Meshtastic has no handle/address concept)         |

"Compact" means `str.replace(" ", "")`. The fallback chains ensure a
non-empty short label is available whenever any identifying field is
present.

### Resolution order

Packet/event names sourced from `node_info` always win over local node
metadata names, which always win over the bare `sender_id` fallback.
Text packets source names exclusively from `node_info`; no packet-level
name field competes.

### What remains opaque (Meshtastic)

The numeric node ID is exposed via `source_sender_id`. When a node is
unknown to the local node database (no `NODEINFO_APP` exchange observed),
only the numeric ID is available and the label fields fall back to that
ID.

### Intentionally deferred (Meshtastic)

No Meshtastic-specific identity enrichment is deferred. Byte-safe prefix
truncation in the renderer (`_truncate_utf8_bytes`) is independent of
identity projection and unchanged.

---

## MeshCore

### Native identity at ingress (MeshCore)

Each inbound MeshCore event carries a sender public-key prefix
(`pubkey_prefix`, a 6-byte hex truncation of the Ed25519 public key) and
a `sender_timestamp`. MeshCore identity is always pubkey-based; there is
no numeric node ID.

### Locally-available contact data (MeshCore)

The MeshCore session exposes a synchronous, in-memory contact lookup via
`session.resolve_contact_label(pubkey_prefix)`. It calls
`MeshCore.get_contact_by_key_prefix(prefix)` against the SDK's
locally-cached `contacts` dict, populated by `CONTACTS` events. It
returns the contact's advertised name (`adv_name`, stripped) or `None`.
No network call is issued, and the method never raises.

### Enrichment pipeline (MeshCore)

At ingress, the adapter resolves the contact label and passes it to the
codec, which records `meshcore.contact_label` and
`meshcore.contact_short_label` in native metadata. Contact keys are
intentionally excluded from `MESHCORE_NAMESPACED_KEYS`: platform
detection relies on the core identity keys (`pubkey_prefix`,
`sender_id`, `channel`, `packet_id`) only, and contact labels are
enrichment layered on top.

### Projection (`project_meshcore_attribution`)

| Generic field               | Source                                                                                                        |
| --------------------------- | ------------------------------------------------------------------------------------------------------------- |
| `source_sender_id`          | `meshcore.pubkey_prefix` → `meshcore.sender_id` → bare `pubkey_prefix`                                        |
| `source_sender_label`       | `meshcore.contact_label` only (human label; opaque pubkey never becomes label)                                |
| `source_sender_short_label` | `meshcore.contact_short_label`, else compact derivation of `contact_label` (first whitespace-delimited token) |
| `source_sender_handle`      | Not produced (MeshCore pubkey prefix is exposed via `source_sender_id`)                                       |

When the sender is not a locally-known contact, both label fields are
`None`. The opaque pubkey prefix never populates `source_sender_label`,
so `{sender}` renders empty rather than a truncated hex string.
Operators who want the pubkey in a prefix use `{sender_id}`.

The projection also emits `source_native_channel_id` and
`source_native_message_id`.

### What remains opaque (MeshCore)

The 6-byte pubkey prefix is stable per sender but is not human-readable.
When no contact match exists, only the opaque prefix is available.

### Intentionally deferred (MeshCore)

No topology or contact canonical events are emitted. The adapter does
not model contact reachability or RF delivery. Contact enrichment is
limited to the local SDK contact cache; there is no cross-transport
pubkey-to-name resolution.

---

## LXMF

### Native identity at ingress (LXMF)

Each inbound LXMF message carries a 16-byte `source_hash` (a truncated
SHA-256 of the sender Reticulum public key, hex-encoded). The message
hash is content-addressed. LXMF display names live in the sender
Identity `announce` `app_data`, not in the message itself.

### Locally-available contact data (LXMF)

The adapter performs a defensive ingress capture of any display name
attached to the inbound message. `_normalise_inbound_message` reads
`getattr(message, "source_name", None)` without issuing a network call.
The current LXMF library does not populate `source_name` on `LXMessage`,
so this read returns `None` and no display name is captured. The codec
maps a captured `source_name` to `lxmf.display_name` in native metadata
when one is present.

### Projection (`project_lxmf_attribution`)

| Generic field               | Source                                                                |
| --------------------------- | --------------------------------------------------------------------- |
| `source_sender_id`          | `normalize_source_hash(source_hash)` (bytes/str → canonical hex)      |
| `source_sender_label`       | `lxmf.display_name` only (non-empty; opaque hash never becomes label) |
| `source_sender_short_label` | `lxmf.short_name`, else compact(`lxmf.display_name`) (space-stripped) |
| `source_sender_handle`      | Not produced (Reticulum hash is exposed via `source_sender_id`)       |

When no display name is present, both label fields are `None`. The
opaque `source_hash` never populates `source_sender_label`, so `{sender}`
renders empty rather than a truncated hash. Operators who want the hash
in a prefix use `{sender_id}`.

The attribution module returns `dict[str, str | None]`, matching the
Meshtastic and MeshCore pattern. The older `LxmfAttribution` dataclass
and the `derive_label` / `derive_short_label` hash-derivation helpers
are removed; they previously produced misleading hash-as-label values
that the dispatch path already discarded.

### What remains opaque (LXMF)

The 16-byte Reticulum identity hash is stable per sender but is not
human-readable. Without an announce-derived display name, only the
opaque hash is available.

### Intentionally deferred (LXMF)

Announce-based display-name enrichment is not implemented. LXMF display
names live in Identity `announce` `app_data`; a local announce-cache
lookup is feasible but is outside this implementation scope. Until a
display name is captured, LXMF-origin events render `{sender}` and
`{sender_short}` as empty strings. The announce loop diagnostics are
preserved and unaffected.

---

## Per-Channel Origin Labels (deferred)

Per-channel origin labels — different `origin_label` values for
different channels within a single route, including routes expanded by
`channel_room_map` — are not implemented. Route-level
`source_origin_label` and `dest_origin_label` apply to all deliveries on
that route regardless of channel.

Operators who need channel-specific origin labels use separate routes
per channel, each with its own direction-aware label. This is the
documented workaround in
[Routing and Delivery §17.5.8](../spec/routing-delivery.md).

The `channel_room_map` shorthand (documented in
[configuration.md](../ops/configuration.md)) expands a single route into
N channel-to-room pairs for routing targets. It does not provide
per-channel labels and does not imply per-channel label support.

---

## Diagnostics and Privacy

Identity labels may appear in rendered message text and in
renderer-local metadata (`RenderingResult.metadata`, including the
normalized `relay_prefix_*` keys). Identity enrichment is observational:
it is not delivery evidence and must not be treated as provenance. The
authoritative machine-readable provenance source is the MEDRE metadata
namespace (`medre.envelope` on Matrix, `fields[0xFD]` on LXMF,
`RenderingResult.metadata` on all transports).

Adapter SDK objects are never exposed in diagnostics, storage, or JSON
evidence. Each session keeps SDK object access inside its boundary and
returns only plain `str` / `int` / `bool` / `None` values. JSON evidence
remains stable and safe; identity enrichment adds no SDK objects to
evidence.

Secrets are never logged. This is enforced by the existing adapter
patterns: diagnostics exclude access tokens, private keys, identity
files, Matrix credentials, session blobs, BLE pairing PINs, and
unredacted device secrets. See the per-transport diagnostics tables in
each transport profile and the
[security and privacy specification](../spec/security-privacy.md) for
the policy.

---

## Inspected Files

### Attribution and dispatch

| File                                           | Status |
| ---------------------------------------------- | ------ |
| `src/medre/core/rendering/attribution.py`      | Full   |
| `src/medre/adapters/_attribution_dispatch.py`  | Full   |
| `src/medre/adapters/matrix/attribution.py`     | Full   |
| `src/medre/adapters/meshtastic/attribution.py` | Full   |
| `src/medre/adapters/meshcore/attribution.py`   | Full   |
| `src/medre/adapters/lxmf/attribution.py`       | Full   |

### Enrichment entry points

| File                                       | Focus                                              |
| ------------------------------------------ | -------------------------------------------------- |
| `src/medre/adapters/matrix/adapter.py`     | Room-member display-name enrichment at ingress     |
| `src/medre/adapters/meshtastic/session.py` | `get_node_info` (SDK `nodes` dict read)            |
| `src/medre/adapters/meshtastic/adapter.py` | `_enrich_with_node_info` ingress wiring            |
| `src/medre/adapters/meshtastic/codec.py`   | `decode(node_info=...)` bare-key embedding         |
| `src/medre/adapters/meshcore/session.py`   | `resolve_contact_label` (SDK contacts dict lookup) |
| `src/medre/adapters/meshcore/adapter.py`   | Contact-label resolution at ingress                |
| `src/medre/adapters/meshcore/codec.py`     | `meshcore.contact_label` native metadata           |
| `src/medre/adapters/lxmf/session.py`       | `_normalise_inbound_message` `source_name` capture |
| `src/medre/adapters/lxmf/codec.py`         | `source_name` → `lxmf.display_name` mapping        |
