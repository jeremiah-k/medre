# External Reference Implementations

This document summarizes external reference implementations that informed
MEDRE's design. It records conceptual patterns worth understanding and
explicit boundaries on what should not be copied.

## MMRelay (meshtastic-matrix-relay)

MMRelay is a Meshtastic-to-Matrix bridge that served as a conceptual reference
for MEDRE's architecture. Last audited 2026-05-24.

### What to use conceptually

These patterns from MMRelay informed MEDRE's design:

**Relay architecture.** The basic Matrix-to-Meshtastic bridge pattern:
event arrives on one transport, gets classified, formatted, and sent to
the other transport.

**Message flow and prefix formatting.** MMRelay formats a prefix with sender
display name and mesh name (`{display5}[M]:` using first 5 chars of the
display name). MEDRE's renderers follow a similar conceptual approach but
with different formatting conventions.

**Outbound queue pacing.** MMRelay applies a delay between consecutive sends
to respect radio duty cycle constraints. MEDRE's `MeshtasticOutboundQueue`
uses a deque-based architecture with configurable `message_delay_seconds`
pacing. The concept is shared; the implementation is independent.

**UTF-8 byte truncation.** MMRelay defines `DEFAULT_MESSAGE_TRUNCATE_BYTES = 227`.
After assembling the final radio text, it encodes to UTF-8 bytes, slices to
the byte budget, and decodes back with `errors="ignore"` to avoid splitting
multi-byte codepoints. MEDRE implements this conceptually in the Meshtastic
renderer as `_truncate_utf8_bytes(text, max_bytes)`. The default
`max_text_bytes` in `MeshtasticConfig` is `227`, informed by MMRelay's
constant. The MEDRE implementation is independent code following the same
conceptual approach.

**Auth sidecar credentials.** MMRelay stores Matrix credentials in a JSON file
alongside its YAML config. MEDRE uses environment-variable overrides
(`MEDRE_ADAPTER__<TOKEN>__ACCESS_TOKEN`) and its own credential sidecar
module (`medre.config.adapters.matrix_credentials`).

**Message-map reply/reaction correlation.** MMRelay maintains an in-memory
mapping between Meshtastic packet IDs and Matrix event IDs for cross-transport
reply correlation. MEDRE uses a different architecture: `NativeMessageRef`
storage in SQLite is the authoritative mapping, and the `RelationResolver`
resolves `target_native_ref` to `target_event_id`.

**Startup stale/backlog suppression.** MMRelay drops packets received within
`STARTUP_PACKET_DRAIN_SECS` of first connect and drops packets whose `rxTime`
is older than the relay start time (adjusted for clock skew). MEDRE
implements startup backlog suppression: the Meshtastic adapter delegates
cutoff comparison to the transport-neutral helper
`medre.core.policies.startup_backlog_suppress.should_suppress_startup_backlog`.

**Packet classification.** MMRelay classifies packets into RELAY / PLUGIN_ONLY
/ DROP. MEDRE uses a 4-action model (relay / ignore / drop / deferred).
See the [source audits](./source-audits.md#meshtastic) for details.

**Matrix stable transaction-id retry.** MMRelay uses `txn_id` on Matrix
`room_send` calls for homeserver deduplication. MEDRE's Matrix adapter uses
a deterministic transaction ID derived from a hash of event and target
metadata, prefixed with `medre_`. Both projects use the same `txn_id`
mechanism independently.

### What not to copy

- Do not copy MMRelay code line-for-line into MEDRE.
- Do not import, vendor, merge, or cherry-pick MMRelay files.
- Do not use MMRelay class/module/type names in MEDRE source.
- Do not treat MMRelay as an authoritative protocol specification. The
  Meshtastic protobuf definitions and firmware are authoritative.
- Do not assume MMRelay behavior is correct; verify against the installed
  `mtjk` package and firmware.

### Packet classification differences

MMRelay classifies each inbound Meshtastic packet into one of three
dispositions. MEDRE uses four actions:

| MMRelay         | MEDRE equivalent  | Key difference                                 |
| --------------- | ----------------- | ---------------------------------------------- |
| `RELAY`         | `relay`           | Same concept                                   |
| `PLUGIN_ONLY`   | `deferred`        | MEDRE has no plugin system yet                 |
| `DROP`          | `drop` / `ignore` | MEDRE splits into malformed (drop) vs valid    |
| (no equivalent) | `ignore`          | Valid packets that don't need relay (ACKs, DM) |

Key design differences:

1. MEDRE has explicit reason strings on every classification.
2. MEDRE tracks per-action and per-reason diagnostic counters.
3. MEDRE uses a frozen `ClassificationResult` dataclass, not dicts.
4. MEDRE's classification policy is a coded decision tree, not a config DSL.
5. MEDRE explicitly handles empty text and malformed packets at the
   classification layer.
6. MEDRE's encrypted packets get `drop` (no decryption infrastructure),
   while MMRelay gives them `PLUGIN_ONLY`.

### MMRelay wire-format constants

MEDRE's `src/medre/interop/mmrelay.py` defines wire-format protocol constants
for cross-adapter message exchange. These key names come from MMRelay's Matrix
message schema:

```python
KEY_ID = "meshtastic_id"
KEY_LONGNAME = "meshtastic_longname"
KEY_SHORTNAME = "meshtastic_shortname"
KEY_MESHNET = "meshtastic_meshnet"
KEY_PORTNUM = "meshtastic_portnum"
KEY_TEXT = "meshtastic_text"
KEY_REPLY_ID = "meshtastic_replyId"
KEY_EMOJI = "meshtastic_emoji"
KEY_REACTION_KEY = "meshtastic_reaction_key"  # MEDRE extension
```

These constants live outside any adapter package because they define a
cross-adapter wire contract, not an implementation detail of any single
adapter.

### Queue semantics differences

MMRelay's outbound queue silently evicts the oldest item when at capacity.
MEDRE's `MeshtasticOutboundQueue` raises `MeshtasticSendError(transient=True)`
instead. This explicit rejection allows the pipeline to classify the failure
as `ADAPTER_TRANSIENT` and retry the delivery.

Queue stats (depth, max size, enqueued, sent, failed, rejected) are visible
in adapter diagnostics. "Queued" does not mean "RF-delivered".

## MEDRE canonical design is authoritative

MEDRE's canonical design documents override any behavioral observations
recorded from external references:

- **Canonical events** (`CanonicalEvent`) are MEDRE's internal representation
- **Adapter contracts** (`AdapterContract`, `AdapterCapabilities`) are MEDRE's
  abstraction layer
- **Native refs** (`NativeRef`, `NativeMessageRef`) are MEDRE's correlation
  model
- **Rendering pipeline** is MEDRE's architecture
- **Delivery receipts** and the evidence/receipt model are MEDRE's design
- **Route engine** and route configuration are MEDRE's routing layer
- **Evidence reports** and diagnostics are MEDRE's observability model

## Stale branches are not source material

Stale MEDRE branches (e.g., `mclub/*`, old feature branches) are not source
material for development. They may contain outdated or abandoned code. Only
the current branch state and the MMRelay reference are considered.
