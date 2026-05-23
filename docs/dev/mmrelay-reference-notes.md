# MMRelay Reference Notes

> **Purpose:** Conceptual reference for MEDRE developers.
> **Source:** Local inspection of a meshtastic-matrix-relay clone.
> **Last audited:** 2026-05-22.

This document summarizes conceptual behavior observed in the MMRelay
(`meshtastic-matrix-relay`) codebase. It exists so that MEDRE contributors
can understand _what_ MMRelay does and _why_, without needing to read the
original source.

---

## Use conceptually

- General relay architecture (Matrix <-> Meshtastic bridging).
- Message flow patterns, prefix formatting, reaction handling.
- Outbound queue pacing and delay-between-messages behavior.
- UTF-8 byte truncation approach: encode, slice to byte budget,
  decode with `errors="ignore"` to avoid splitting codepoints.
- `DEFAULT_MESSAGE_TRUNCATE_BYTES = 227` as the default radio text budget.
- Auth sidecar credential file pattern (JSON file alongside config).
- Message-map reply/reaction correlation between transports.
- `broadcast_enabled` config gate controlling whether outbound radio
  sends are allowed at all.
- Startup stale/backlog packet suppression via `rxTime` comparison.
- Packet classification into RELAY / PLUGIN_ONLY / DROP categories.
- Matrix stable transaction-id retry for idempotent sends.
- Meshtastic outbound queue with explicit size checks and pacing.

## Do not copy

- Do not copy MMRelay code line-for-line into MEDRE.
- Do not import, vendor, merge, or cherry-pick MMRelay files.
- Do not use MMRelay class/module/type names in MEDRE source.
- Do not treat MMRelay as an authoritative protocol specification;
  the Meshtastic protobuf definitions and firmware are authoritative.
- Do not assume MMRelay behavior is correct; verify against the
  installed `mtjk` package and firmware.

---

## Matrix -> Meshtastic flow (conceptual)

1. Matrix event arrives via `nio` sync.
2. Event content is extracted (body, formatted body fallback).
3. A prefix is formatted with sender display name and mesh name:
   `"{display5}[M]: "` using first 5 chars of the display name.
4. Prefix + body text is assembled.
5. UTF-8 byte truncation is applied to the final text:
   `encode("utf-8")`, slice to `DEFAULT_MESSAGE_TRUNCATE_BYTES`,
   `decode("utf-8", errors="ignore")`.
6. The truncated text is sent via `sendText` on the Meshtastic
   interface with appropriate channel index and optional `replyId`.

## Meshtastic -> Matrix flow (conceptual)

1. Meshtastic packet arrives via `pubsub` callback.
2. Packet is classified (text, telemetry, etc.) and filtered.
3. Sender info (longname, shortname) is resolved from the node DB.
4. A Matrix prefix is formatted: `"[{long}/{mesh}]: "`.
5. The message is sent to the configured Matrix room via
   `room_send` with the assembled text.

## Auth sidecar credentials

MMRelay stores Matrix credentials in a JSON file alongside its YAML
config. The file contains the access token. This is a convenience
pattern for single-user deployments. MEDRE uses environment-variable
overrides (`MEDRE_ADAPTER__<TOKEN>__ACCESS_TOKEN`) and its own
credential sidecar module (`medre.config.adapters.matrix_credentials`).

## Message-map reply/reaction correlation

MMRelay maintains an in-memory mapping between Meshtastic packet IDs
and Matrix event IDs. When a Meshtastic packet arrives with a
`replyId`, MMRelay looks up the corresponding Matrix event to construct
a proper Matrix reply. Similarly, when a Matrix reaction is detected,
the mapping is used to find the Meshtastic packet ID for the
`replyId` field in the outbound radio message.

MEDRE uses a different architecture: the pipeline's `NativeMessageRef`
storage in SQLite is the authoritative mapping, and the
`RelationResolver` resolves `target_native_ref` to `target_event_id`.

## broadcast_enabled gate

MMRelay has a `broadcast_enabled` config flag. When `False`, outbound
Meshtastic sends are suppressed entirely. This allows running the
relay in listen-only mode. MEDRE does not have an equivalent gate in
the current tranche; the adapter always attempts delivery.

## Startup stale/backlog suppression

MMRelay drops packets received within `STARTUP_PACKET_DRAIN_SECS` of
the first process-lifetime connect. It also drops packets whose
`rxTime < RELAY_START_TIME` (adjusted for clock skew). MEDRE has a
`startup_backlog_suppress_seconds` config field but does not wire it
to filtering logic yet.

## Packet classification: RELAY / PLUGIN_ONLY / DROP

MMRelay classifies each inbound Meshtastic packet into one of three
dispositions:

- **RELAY:** Normal text message to be bridged to Matrix.
- **PLUGIN_ONLY:** Handled by plugins but not relayed (e.g., detection
  sensor data).
- **DROP:** Ignored entirely (e.g., ACKs, telemetry the user opted out
  of).

MEDRE's packet classifier uses a simpler category model (`text`,
`ack`, `telemetry`, etc.) and drops everything except `text` in the
current tranche.

## Matrix stable transaction-id retry

MMRelay uses `txn_id` on Matrix `room_send` calls. The homeserver
deduplicates events with the same transaction ID within a time window,
allowing safe retries without duplicate messages. MEDRE's Matrix
adapter does not yet provide this idempotency surface; Matrix
transaction-id retry is deferred to a future tranche.

## Meshtastic queue explicit size checks / pacing

MMRelay's outbound queue checks its size against `MAX_QUEUE_SIZE` and
applies `DEFAULT_MESSAGE_DELAY` seconds between consecutive sends.
The minimum delay is enforced at `MINIMUM_MESSAGE_DELAY` seconds.

MEDRE's `MeshtasticOutboundQueue` uses a deque-based architecture with
configurable `message_delay_seconds` pacing. The concept is shared;
the implementation is independent.

**Queue overflow semantics differ from MMRelay.** When the queue is at
capacity, MEDRE raises `MeshtasticSendError(transient=True)` instead of
silently evicting the oldest item. This explicit rejection allows the
pipeline to classify the failure as `ADAPTER_TRANSIENT` and retry the
delivery. Queue stats (depth, max size, enqueued, sent, failed,
rejected) are visible in adapter diagnostics. Queued / locally
accepted does not mean RF-delivered.

## UTF-8 byte truncation (default 227 bytes)

MMRelay defines `DEFAULT_MESSAGE_TRUNCATE_BYTES = 227`. After
assembling the final radio text (prefix + body), the text is:

1. Encoded to UTF-8 bytes.
2. Sliced to the byte budget.
3. Decoded back with `errors="ignore"` to avoid splitting multi-byte
   codepoints.

MEDRE implements this conceptually in the Meshtastic renderer as
`_truncate_utf8_bytes(text, max_bytes)`. The default `max_text_bytes`
in `MeshtasticConfig` is `227`, informed by MMRelay's constant. The
MEDRE implementation is independent code following the same conceptual
approach.

---

## Stale MEDRE branches are not source material

Stale MEDRE branches (e.g., `mclub/*`, old feature branches) are **not**
source material for this or any other tranche. They may contain
outdated or abandoned code. Only the current branch state and the
MMRelay reference are considered.

## MEDRE canonical design remains authoritative

MEDRE's canonical design documents and contracts override any
behavioral observations recorded here. Specifically:

- **Canonical events** (`CanonicalEvent`) are MEDRE's internal
  representation, not copied from any reference.
- **Adapter contracts** (`AdapterContract`, `AdapterCapabilities`) are
  MEDRE's abstraction layer.
- **Native refs** (`NativeRef`, `NativeMessageRef`) are MEDRE's
  correlation model.
- **Rendering pipeline** is MEDRE's architecture (renderer produces
  `RenderingResult`, adapter consumes it).
- **Delivery receipts** and the evidence/receipt model are MEDRE's
  design.
- **Route engine** and route configuration are MEDRE's routing layer.
- **Evidence reports** and diagnostics are MEDRE's observability model.

---

## Packet classification lessons

This section documents what MEDRE learned from studying MMRelay's packet
classification model and how MEDRE's implementation differs intentionally.

### MMRelay's 3-action model

MMRelay classifies every inbound Meshtastic packet into one of three
dispositions:

- **RELAY:** Normal text message to be bridged to Matrix.
- **PLUGIN_ONLY:** Handled by plugins but not relayed (e.g., detection
  sensor data, encrypted packets).
- **DROP:** Ignored entirely (e.g., ACKs, opted-out telemetry).

### MMRelay's classification priority

MMRelay evaluates packets in priority order:

1. **Encrypted** → `PLUGIN_ONLY` by default (plugins may handle, relay skips).
2. **Disabled message types** → `DROP` (user config opts out).
3. **Chat-type overrides** → per-portnum config can force `RELAY` or `DROP`.
4. **Type defaults** → each portnum has a built-in default (text=RELAY,
   telemetry=DROP, detection_sensor=PLUGIN_ONLY, etc.).
5. **Catch-all** → unknown types default to `DROP`.

Key behaviors:

- **Encrypted packets** default to `PLUGIN_ONLY`.  Plugins may decrypt
  and handle them, but the relay core does not attempt decryption.
- **Detection sensor** packets default to `PLUGIN_ONLY` if plugins are
  loaded; otherwise `DROP`.  When `detection_sensor_enabled=True` in
  MMRelay config, they become `RELAY`.
- **DM (direct messages)** are not relayed by default in MMRelay.  Plugins
  see DMs first and may relay them, but the core relay skips them.
- **Channel mapping** is the final gate: even a `RELAY` packet is dropped
  if no Matrix channel is mapped for the packet's Meshtastic channel
  index.

### Startup stale/backlog/clock-skew suppression

MMRelay drops packets received within `STARTUP_PACKET_DRAIN_SECS` of the
first process-lifetime connect.  It also drops packets whose `rxTime` is
older than `RELAY_START_TIME` (adjusted for clock skew between the radio
and the host).  This prevents relaying stale backlog that accumulated
while the relay was offline.

### Where MEDRE intentionally differs

MEDRE uses a **4-action model** instead of MMRelay's 3-action model:

| Action     | Meaning                                              |
|------------|------------------------------------------------------|
| `relay`    | Text message proceeds to decode and publish          |
| `ignore`   | Packet is skipped with no side effects               |
| `drop`     | Packet is rejected (malformed, encrypted)            |
| `deferred` | Packet is set aside for future handling (plugins)    |

Key differences:

1. **`deferred` action**: MMRelay's `PLUGIN_ONLY` maps roughly to
   MEDRE's `deferred`.  MEDRE does not have a plugin system yet, so
   deferred packets are counted and logged but not processed.  When
   MEDRE adds a plugin system, deferred packets will be the entry point.

2. **Encrypted packets → `drop`**: MMRelay treats encrypted as
   `PLUGIN_ONLY` (plugins may decrypt).  MEDRE conservatively drops
   encrypted packets because there is no decryption infrastructure yet.
   This may change to `deferred` when a decryption plugin exists.

3. **Detection sensor → `deferred`**: MMRelay relays detection sensor
   data when enabled.  MEDRE defers all detection sensor packets because
   there is no handler for them yet.

4. **Unknown portnums → `deferred`**: MMRelay drops unknown types.
   MEDRE defers them so future handlers can pick them up without
   classifier changes.

5. **Explicit reason strings**: Every MEDRE classification includes a
   human-readable `reason` string explaining the decision.  This supports
   structured logging and diagnostics without string-matching on
   category names.

6. **Inbound evidence counters**: MEDRE tracks per-action and per-reason
   counters in the adapter (seen, relayed, ignored, dropped, deferred,
   malformed, encrypted, detection_sensor, DM, empty_text, unknown_portnum).
   These are exposed via `diagnostics()` for observability without
   external tools.  MMRelay does not expose equivalent counters.

7. **`ClassificationResult` dataclass**: MEDRE returns a frozen dataclass
   from the classifier instead of a dict.  The dataclass carries action,
   category, reason, and all metadata fields in a typed, immutable
   structure.  MMRelay uses dicts throughout.

8. **No policy DSL**: MEDRE does not implement MMRelay's per-portnum
   config overrides or chat-type config DSL.  Classification policy is
   coded directly in the classifier decision tree.  A policy DSL may be
   added in a future tranche.
