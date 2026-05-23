# MMRelay Reference Notes

> **Purpose:** Conceptual reference for MEDRE developers.
> **Source:** Local inspection of a meshtastic-matrix-relay clone.
> **Last audited:** 2026-05-22.

This document summarizes conceptual behavior observed in the MMRelay
(`meshtastic-matrix-relay`) codebase.  It exists so that MEDRE contributors
can understand *what* MMRelay does and *why*, without needing to read the
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
config.  The file contains the access token.  This is a convenience
pattern for single-user deployments.  MEDRE uses environment-variable
overrides (`MEDRE_ADAPTER__<TOKEN>__ACCESS_TOKEN`) and its own
credential sidecar module (`medre.config.adapters.matrix_credentials`).

## Message-map reply/reaction correlation

MMRelay maintains an in-memory mapping between Meshtastic packet IDs
and Matrix event IDs.  When a Meshtastic packet arrives with a
`replyId`, MMRelay looks up the corresponding Matrix event to construct
a proper Matrix reply.  Similarly, when a Matrix reaction is detected,
the mapping is used to find the Meshtastic packet ID for the
`replyId` field in the outbound radio message.

MEDRE uses a different architecture: the pipeline's `NativeMessageRef`
storage in SQLite is the authoritative mapping, and the
`RelationResolver` resolves `target_native_ref` to `target_event_id`.

## broadcast_enabled gate

MMRelay has a `broadcast_enabled` config flag.  When `False`, outbound
Meshtastic sends are suppressed entirely.  This allows running the
relay in listen-only mode.  MEDRE does not have an equivalent gate in
the current tranche; the adapter always attempts delivery.

## Startup stale/backlog suppression

MMRelay drops packets received within `STARTUP_PACKET_DRAIN_SECS` of
the first process-lifetime connect.  It also drops packets whose
`rxTime < RELAY_START_TIME` (adjusted for clock skew).  MEDRE has a
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

MMRelay uses `txn_id` on Matrix `room_send` calls.  The homeserver
deduplicates events with the same transaction ID within a time window,
allowing safe retries without duplicate messages.  MEDRE's Matrix
adapter does not yet provide this idempotency surface; Matrix
transaction-id retry is deferred to a future tranche.

## Meshtastic queue explicit size checks / pacing

MMRelay's outbound queue checks its size against `MAX_QUEUE_SIZE` and
applies `DEFAULT_MESSAGE_DELAY` seconds between consecutive sends.
The minimum delay is enforced at `MINIMUM_MESSAGE_DELAY` seconds.

MEDRE's `MeshtasticOutboundQueue` uses a deque-based architecture with
configurable `message_delay_seconds` pacing.  The concept is shared;
the implementation is independent.

## UTF-8 byte truncation (default 227 bytes)

MMRelay defines `DEFAULT_MESSAGE_TRUNCATE_BYTES = 227`.  After
assembling the final radio text (prefix + body), the text is:

1. Encoded to UTF-8 bytes.
2. Sliced to the byte budget.
3. Decoded back with `errors="ignore"` to avoid splitting multi-byte
   codepoints.

MEDRE implements this conceptually in the Meshtastic renderer as
`_truncate_utf8_bytes(text, max_bytes)`.  The default `max_text_bytes`
in `MeshtasticConfig` is `227`, informed by MMRelay's constant.  The
MEDRE implementation is independent code following the same conceptual
approach.

---

## Stale MEDRE branches are not source material

Stale MEDRE branches (e.g., `mclub/*`, old feature branches) are **not**
source material for this or any other tranche.  They may contain
outdated or abandoned code.  Only the current branch state and the
MMRelay reference are considered.

## MEDRE canonical design remains authoritative

MEDRE's canonical design documents and contracts override any
behavioral observations recorded here.  Specifically:

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
