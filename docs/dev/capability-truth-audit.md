# Capability Truth Audit

**Work Package**: Capability Truth Audit
**Branch**: `adapter-lifecycle-parity`
**Baseline**: after merging `adapter-sdk-parity` (#99)
**Status**: Implementation audit synced with source/tests/docs
**Date**: 2026-06-10
**Scope**: All 4 real adapters (Matrix, Meshtastic, MeshCore, LXMF), their fakes, JSON declarations, and conformance tests.

## 1. Goal

Verify that every `AdapterCapabilities` declaration and transport profile
capability JSON matches actual runtime behavior. Flag overclaims where a
capability is declared but not exercised in code, and underclaims where the
adapter genuinely supports something it does not declare.

**Core constraint**: capability flags must never overclaim behavior; adapters
report facts; no compatibility shims; no schema churn.

## 2. Sources Audited

| Source                       | Path                                                                   |
| ---------------------------- | ---------------------------------------------------------------------- |
| AdapterCapabilities contract | `src/medre/core/contracts/adapter.py`                                  |
| Matrix adapter               | `src/medre/adapters/matrix/adapter.py` (`_MATRIX_CAPABILITIES`)        |
| Meshtastic adapter           | `src/medre/adapters/meshtastic/adapter.py` (`__init__` per-config)     |
| MeshCore adapter             | `src/medre/adapters/meshcore/adapter.py` (`_MESHCORE_CAPS_BASE`)       |
| LXMF adapter                 | `src/medre/adapters/lxmf/adapter.py` (`_LXMF_CAPABILITIES`)            |
| Fake Matrix                  | `src/medre/adapters/fakes/matrix.py` (`_FAKE_MATRIX_CAPABILITIES`)     |
| Fake Meshtastic              | `src/medre/adapters/fakes/meshtastic.py` (per-config)                  |
| Fake MeshCore                | `src/medre/adapters/fakes/meshcore.py` (`_FAKE_MESHCORE_CAPABILITIES`) |
| Fake LXMF                    | `src/medre/adapters/fakes/lxmf.py` (`_FAKE_LXMF_CAPABILITIES`)         |
| Matrix JSON                  | `docs/spec/transport-profiles/matrix-capabilities.json`                |
| Meshtastic JSON              | `docs/spec/transport-profiles/meshtastic-capabilities.json`            |
| MeshCore JSON                | `docs/spec/transport-profiles/meshcore-capabilities.json`              |
| LXMF JSON                    | `docs/spec/transport-profiles/lxmf-capabilities.json`                  |
| Capability tests             | `tests/test_capabilities.py`                                           |
| Conformance tests            | `tests/test_capability_conformance.py`                                 |
| Audit companion tests        | `tests/test_capability_audit.py`                                       |
| Prior audit                  | `docs/dev/adapter-reality-audit.md`                                    |

## 3. Conformance Test Gate

`tests/test_capability_conformance.py` enforces a hard gate: every
`AdapterCapabilities` field must appear in each JSON file, every JSON key
must be a valid field, and every value must match the adapter source code.
This suite runs on every CI pass with zero network or hardware dependencies.

`tests/test_capabilities.py` additionally verifies that each fake adapter's
declared capabilities match the expected values and survive
`serialize_adapter_capabilities` / JSON round-trip.

`tests/test_capability_audit.py` is the audit-specific companion test that
directly validates the findings recorded in this document (overclaim checks,
underclaim checks, per-adapter matrix assertions). It complements
`test_capability_conformance.py`, which remains the JSON↔code conformance
guard enforcing field-value parity across all four transport profiles.

## 4. Capability Audit Matrix

### Legend

- **implemented**: Declared and exercised in adapter code; runtime evidence confirms it.
- **partially implemented**: Declared and partially exercised; some paths exist but coverage is incomplete or semantics differ from what the flag name implies.
- **unsupported**: Declared as `False` / `"unsupported"` / `None` and no runtime path exercises it. Honest omission.

### 4.1 Matrix (Presentation Adapter)

| Capability            | JSON            | Code            | Fake            | Status      | Evidence                                                                                                                                                       |
| --------------------- | --------------- | --------------- | --------------- | ----------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `text`                | `true`          | `True`          | `True`          | implemented | `room_send` with `m.room.message` msgtype.                                                                                                                     |
| `title`               | `false`         | `False`         | `False`         | unsupported | Matrix has no native title field.                                                                                                                              |
| `replies`             | `"native"`      | `"native"`      | `"native"`      | implemented | `m.in_reply_to` via `MatrixRelationHandler`. Codec decodes inbound replies; renderer produces outbound reply payloads.                                         |
| `reactions`           | `"native"`      | `"native"`      | `"native"`      | implemented | `m.reaction` annotation via `_matrix_event_type`. `M_DUPLICATE_ANNOTATION` handled as permanent (adapter-reality-audit R1).                                    |
| `edits`               | `"unsupported"` | `"unsupported"` | `"unsupported"` | unsupported | No edit/redaction path in adapter.                                                                                                                             |
| `deletes`             | `"unsupported"` | `"unsupported"` | `"unsupported"` | unsupported | No redaction path in adapter.                                                                                                                                  |
| `attachments`         | `false`         | `False`         | `False`         | unsupported | No file upload code.                                                                                                                                           |
| `metadata_fields`     | `false`         | `False`         | `False`         | unsupported | No structured metadata field transmission.                                                                                                                     |
| `delivery_receipts`   | `true`          | `True`          | `True`          | implemented | `room_send` returns `event_id` on success; adapter populates `AdapterDeliveryResult.native_message_id`. This is a server-acknowledged delivery fact. See §5.1. |
| `store_and_forward`   | `false`         | `False`         | `False`         | unsupported | Matrix has no native store-and-forward exposed to MEDRE.                                                                                                       |
| `direct_messages`     | `true`          | `True`          | `True`          | implemented | DM rooms handled through normal room mechanism.                                                                                                                |
| `channels`            | `true`          | `True`          | `True`          | implemented | Matrix rooms are channels. `target_channel` maps to `room_id`.                                                                                                 |
| `ack_tracking`        | `false`         | `False`         | `False`         | unsupported | No transport-level ACK tracking exposed.                                                                                                                       |
| `async_delivery`      | `true`          | `True`          | `True`          | implemented | Sync loop runs async; delivery completes after `room_send` returns.                                                                                            |
| `identity_encryption` | `false`         | `False`         | `False`         | unsupported | E2EE is Megolm (session-based), not identity-based encryption in the LXMF sense.                                                                               |
| `presence`            | `false`         | `False`         | `False`         | unsupported | No presence tracking code.                                                                                                                                     |
| `topic_rooms`         | `true`          | `True`          | `True`          | implemented | Matrix rooms support named destinations. `room_id` is the routing key.                                                                                         |
| `mesh_routing`        | `false`         | `False`         | `False`         | unsupported | Matrix is not a mesh protocol.                                                                                                                                 |
| `priority_delivery`   | `false`         | `False`         | `False`         | unsupported | No priority handling.                                                                                                                                          |
| `max_text_bytes`      | `null`          | `None`          | `None`          | correct     | No hard byte limit at protocol level.                                                                                                                          |
| `max_text_chars`      | `null`          | `None`          | `None`          | correct     | No hard char limit at protocol level.                                                                                                                          |

### 4.2 Meshtastic (Transport Adapter)

| Capability            | JSON            | Code            | Fake            | Status      | Evidence                                                                                                                              |
| --------------------- | --------------- | --------------- | --------------- | ----------- | ------------------------------------------------------------------------------------------------------------------------------------- |
| `text`                | `true`          | `True`          | `True`          | implemented | `sendText()` via session. Queue-based delivery with `reply_id` and `emoji` passthrough.                                               |
| `title`               | `false`         | `False`         | `False`         | unsupported | Meshtastic has no title field.                                                                                                        |
| `replies`             | `"native"`      | `"native"`      | `"native"`      | implemented | Protobuf `Data.reply_id` field. `send_one()` passes `reply_id` to session. Codec decodes inbound. Confirmed in adapter-reality-audit. |
| `reactions`           | `"native"`      | `"native"`      | `"native"`      | implemented | Protobuf `Data.emoji` field. `send_one()` passes `emoji` to session. Codec decodes inbound. Confirmed in adapter-reality-audit.       |
| `edits`               | `"unsupported"` | `"unsupported"` | `"unsupported"` | unsupported | No edit mechanism in Meshtastic.                                                                                                      |
| `deletes`             | `"unsupported"` | `"unsupported"` | `"unsupported"` | unsupported | No delete mechanism in Meshtastic.                                                                                                    |
| `attachments`         | `false`         | `False`         | `False`         | unsupported | No file transfer code.                                                                                                                |
| `metadata_fields`     | `true`          | `True`          | `True`          | implemented | Classifier enriches with `fromId`/`toId`, portnum, channel, node info (longname/shortname). Rich native metadata in codec output.     |
| `delivery_receipts`   | `false`         | `False`         | `False`         | unsupported | Queue-based `deliver()` returns `"enqueued"`; no ACK delivery confirmation tracked back to MEDRE.                                     |
| `store_and_forward`   | `false`         | `False`         | `False`         | unsupported | Not implemented.                                                                                                                      |
| `direct_messages`     | `false`         | `False`         | `False`         | unsupported | DM packets classified and ignored (`REASON_DIRECT_MESSAGE`). Honest.                                                                  |
| `channels`            | `true`          | `True`          | `True`          | implemented | Meshtastic channel indices used as destinations.                                                                                      |
| `ack_tracking`        | `false`         | `False`         | `False`         | unsupported | No ACK tracking exposed.                                                                                                              |
| `async_delivery`      | `true`          | `True`          | `True`          | implemented | Queue-based: `deliver()` enqueues, returns `delivery_status="enqueued"`. Background drain task sends asynchronously.                  |
| `identity_encryption` | `false`         | `False`         | `False`         | unsupported | Meshtastic encryption is channel-key-based, not identity-based.                                                                       |
| `presence`            | `false`         | `False`         | `False`         | unsupported | No presence tracking.                                                                                                                 |
| `topic_rooms`         | `false`         | `False`         | `False`         | unsupported | Meshtastic channels are numeric, not named topics.                                                                                    |
| `mesh_routing`        | `true`          | `True`          | `True`          | implemented | Meshtastic is a mesh protocol.                                                                                                        |
| `priority_delivery`   | `false`         | `False`         | `False`         | unsupported | No priority handling.                                                                                                                 |
| `max_text_bytes`      | `227`           | `227` (default) | `227`           | correct     | Default from `MeshtasticConfig.max_text_bytes`. See §5.2.                                                                             |
| `max_text_chars`      | `null`          | `None`          | `None`          | correct     | No char limit enforced.                                                                                                               |

### 4.3 MeshCore (Transport Adapter)

| Capability            | JSON            | Code            | Fake            | Status      | Evidence                                                                                          |
| --------------------- | --------------- | --------------- | --------------- | ----------- | ------------------------------------------------------------------------------------------------- |
| `text`                | `true`          | `True`          | `True`          | implemented | `send_text()` via session. `local_acceptance` metadata on success.                                |
| `title`               | `false`         | `False`         | `False`         | unsupported | MeshCore has no title field.                                                                      |
| `replies`             | `"unsupported"` | `"unsupported"` | `"unsupported"` | unsupported | No native reply mechanism. Confirmed in adapter-reality-audit.                                    |
| `reactions`           | `"unsupported"` | `"unsupported"` | `"unsupported"` | unsupported | No native reaction mechanism. Confirmed in adapter-reality-audit.                                 |
| `edits`               | `"unsupported"` | `"unsupported"` | `"unsupported"` | unsupported | No edit mechanism.                                                                                |
| `deletes`             | `"unsupported"` | `"unsupported"` | `"unsupported"` | unsupported | No delete mechanism.                                                                              |
| `attachments`         | `false`         | `False`         | `False`         | unsupported | No attachment support.                                                                            |
| `metadata_fields`     | `false`         | `False`         | `False`         | unsupported | No structured metadata.                                                                           |
| `delivery_receipts`   | `false`         | `False`         | `False`         | unsupported | ACK is for contact DMs only, not tracked by MEDRE as delivery receipts.                           |
| `store_and_forward`   | `false`         | `False`         | `False`         | unsupported | Not implemented.                                                                                  |
| `direct_messages`     | `false`         | `False`         | `False`         | unsupported | MEDRE does not initiate outbound DMs. Inbound PRIV packets are relayed but relay ≠ DM initiation. |
| `channels`            | `true`          | `True`          | `True`          | implemented | MeshCore has channel concept (`channel_idx`).                                                     |
| `ack_tracking`        | `false`         | `False`         | `False`         | unsupported | No ACK tracking exposed.                                                                          |
| `async_delivery`      | `true`          | `True`          | `True`          | implemented | Delivery returns `native_id` (expected_ack for DMs) with `local_acceptance` metadata.             |
| `identity_encryption` | `false`         | `False`         | `False`         | unsupported | E2EE is always-on (AES-128 + HMAC) but not identity-based in the `AdapterCapabilities` sense.     |
| `presence`            | `false`         | `False`         | `False`         | unsupported | No presence tracking.                                                                             |
| `topic_rooms`         | `false`         | `False`         | `False`         | unsupported | Channels are numeric indices, not named topics.                                                   |
| `mesh_routing`        | `true`          | `True`          | `True`          | implemented | MeshCore is a mesh protocol.                                                                      |
| `priority_delivery`   | `false`         | `False`         | `False`         | unsupported | No priority handling.                                                                             |
| `max_text_bytes`      | `512`           | `512` (default) | `512`           | correct     | Default from `MeshCoreConfig.max_text_bytes`. Configurable per-instance.                          |
| `max_text_chars`      | `null`          | `None`          | `None`          | correct     | No char limit; bytes are enforced.                                                                |

### 4.4 LXMF (Transport Adapter)

| Capability            | JSON            | Code            | Fake            | Status      | Evidence                                                                                                                        |
| --------------------- | --------------- | --------------- | --------------- | ----------- | ------------------------------------------------------------------------------------------------------------------------------- |
| `text`                | `true`          | `True`          | `True`          | implemented | `send_text()` via session with content field.                                                                                   |
| `title`               | `true`          | `True`          | `True`          | implemented | LXMF supports title field natively. Session `send_text()` accepts title. Renderer produces it.                                  |
| `replies`             | `"unsupported"` | `"unsupported"` | `"unsupported"` | unsupported | LXMF has no native reply/threading mechanism. No fallback either.                                                               |
| `reactions`           | `"unsupported"` | `"unsupported"` | `"unsupported"` | unsupported | No reaction mechanism in LXMF.                                                                                                  |
| `edits`               | `"unsupported"` | `"unsupported"` | `"unsupported"` | unsupported | No edit mechanism.                                                                                                              |
| `deletes`             | `"unsupported"` | `"unsupported"` | `"unsupported"` | unsupported | No delete mechanism.                                                                                                            |
| `attachments`         | `false`         | `False`         | `False`         | unsupported | No attachment support implemented.                                                                                              |
| `metadata_fields`     | `true`          | `True`          | `True`          | implemented | LXMF fields dict passed through `send_text()`.                                                                                  |
| `delivery_receipts`   | `false`         | `False`         | `False`         | unsupported | LXMF has a 9-state delivery model, but MEDRE does not wire delivery confirmations back through the capability system. See §5.3. |
| `store_and_forward`   | `true`          | `True`          | `True`          | implemented | LXMRouter has store-and-forward by design. Messages queued until destination reachable.                                         |
| `direct_messages`     | `true`          | `True`          | `True`          | implemented | LXMF is inherently a DM protocol (source_hash → destination_hash).                                                              |
| `channels`            | `false`         | `False`         | `False`         | unsupported | LXMF has no channel/group concept. Correct.                                                                                     |
| `ack_tracking`        | `false`         | `False`         | `False`         | unsupported | No ACK tracking exposed.                                                                                                        |
| `async_delivery`      | `true`          | `True`          | `True`          | implemented | 9-state async delivery model. `deliver()` returns with `delivery_state` in metadata.                                            |
| `identity_encryption` | `true`          | `True`          | `True`          | implemented | Reticulum identity-based encryption. Confirmed in adapter-reality-audit.                                                        |
| `presence`            | `false`         | `False`         | `False`         | unsupported | No presence tracking.                                                                                                           |
| `topic_rooms`         | `false`         | `False`         | `False`         | unsupported | No topic/room concept.                                                                                                          |
| `mesh_routing`        | `true`          | `True`          | `True`          | implemented | Reticulum is a mesh network.                                                                                                    |
| `priority_delivery`   | `false`         | `False`         | `False`         | unsupported | No priority handling.                                                                                                           |
| `max_text_bytes`      | `null`          | `None`          | `None`          | correct     | No byte limit enforced by MEDRE.                                                                                                |
| `max_text_chars`      | `16384`         | `16384`         | `16384`         | correct     | LXMF 16KB character limit.                                                                                                      |

## 5. Detailed Findings

### 5.1 Matrix `delivery_receipts=True` — Semantics Clarification

The `AdapterCapabilities.delivery_receipts` docstring defines it as "Whether the
adapter can confirm delivery back to the framework." Matrix's `room_send`
returns an `event_id` on success, which the adapter populates in
`AdapterDeliveryResult.native_message_id`. This is a server-acknowledged
delivery fact, not an end-to-end read receipt. The flag is honest: the
adapter does confirm delivery (to the homeserver) back to the framework.

This is distinct from Matrix read receipts (`m.receipt`), which are not
tracked. No overclaim.

### 5.2 Meshtastic `max_text_bytes=227` — Known Risk

Per `adapter-reality-audit.md` R12: 227 bytes doesn't account for protobuf
overhead when `reply_id` is set. The value is the config default and matches
across JSON, code, and fake. The byte budget may overclaim available space
when structured fields (reply_id, emoji) consume protobuf overhead. This is a
known risk item, not an overclaim in the flag itself — the adapter genuinely
attempts to enforce this limit.

### 5.3 LXMF `delivery_receipts=False` — Honest Despite 9-State Model

LXMF has a rich 9-state delivery model (`outbound` → `sent` → `delivered`,
etc.), but the adapter does not wire these states back through the MEDRE
delivery receipt system. The `delivery_state` appears in `metadata["lxmf"]`
only. Setting `delivery_receipts=False` is the honest declaration. The
session tracks delivery state for diagnostic observability but does not produce
MEDRE-level delivery receipts.

### 5.4 MeshCore `identity_encryption=False` — Honest Despite Always-On E2EE

MeshCore has always-on AES-128 + HMAC encryption, but this is not identity-based
encryption in the `AdapterCapabilities` sense (which models the LXMF/Reticulum
identity hash model). The flag correctly reflects that MeshCore does not expose
identity-level encryption semantics to MEDRE.

### 5.5 Meshtastic `replies="native"` and `reactions="native"` — Confirmed

Both are backed by first-class protobuf fields (`Data.reply_id` and
`Data.emoji`) confirmed in adapter-reality-audit.md against firmware protobufs.
The adapter's `send_one()` passes both fields through. The codec decodes them
inbound. These are honest native support declarations.

## 6. Fake-to-Real Parity

All four fake adapters declare capabilities identical to their corresponding
real adapters:

| Adapter    | Fake Source                           | Parity       | Note                                                                                                                                                                                                                    |
| ---------- | ------------------------------------- | ------------ | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Matrix     | `_FAKE_MATRIX_CAPABILITIES`           | exact match  | Same constant values.                                                                                                                                                                                                   |
| Meshtastic | per-config `AdapterCapabilities(...)` | exact match  | Both derive `max_text_bytes` from config default (227).                                                                                                                                                                 |
| MeshCore   | `_FAKE_MESHCORE_CAPABILITIES`         | matches base | Fake uses fixed 512; real uses `dataclasses.replace(_MESHCORE_CAPS_BASE, max_text_bytes=config.max_text_bytes)`. Default config yields 512. Test verifies parity (`test_real_adapter_default_capabilities_match_fake`). |
| LXMF       | `_FAKE_LXMF_CAPABILITIES`             | exact match  | Same constant values.                                                                                                                                                                                                   |

## 7. JSON-to-Code Conformance

`tests/test_capability_conformance.py` enforces three invariants per transport:

1. **Value match**: Every JSON value matches the adapter's `AdapterCapabilities` field.
2. **No undocumented fields**: Every `AdapterCapabilities` field appears in the JSON.
3. **No unknown keys**: Every JSON key is a valid `AdapterCapabilities` field.

All four transports pass all three checks as of this audit.

## 8. Overclaim Assessment

**No overclaims identified.** Every `True` / `"native"` / `"fallback"` capability
flag is backed by runtime code paths that exercise the capability. No adapter
declares support for a capability that exists only in SDK theory without MEDRE
code to exercise it.

Specific non-overclaims confirmed:

- Matrix `delivery_receipts`: server ACK, not end-to-end. Honest.
- Meshtastic `reactions`: protobuf `Data.emoji` is exercised. Honest.
- Meshtastic `replies`: protobuf `Data.reply_id` is exercised. Honest.
- LXMF `store_and_forward`: LXMRouter design. Honest.
- LXMF `identity_encryption`: Reticulum identity model. Honest.
- LXMF `delivery_receipts=False`: honest despite 9-state model in session.

## 9. Underclaim Assessment

No actionable underclaims identified. All capabilities that the adapters
genuinely support at runtime are declared. The following are correctly set to
`False` / `"unsupported"`:

- Matrix `edits`, `deletes`: Matrix spec supports these but MEDRE has no
  implementation. Correct to not declare until implemented.
- Matrix `presence`: Matrix spec supports presence but MEDRE has no
  implementation. Correct.
- Meshtastic `store_and_forward`: Meshtastic firmware has store-and-forward
  but MEDRE does not exercise it. Correct.
- MeshCore `direct_messages`: MEDRE relays inbound PRIV but does not initiate
  outbound DMs. Correct per adapter code comment.

## 10. Risk Items from Prior Audit

From `docs/dev/adapter-reality-audit.md` §7, the following items remain
relevant to capability truth:

| Item                              | Risk     | Impact on Capability Truth                                                                                                                                       |
| --------------------------------- | -------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| R11 `delivery_receipts` semantics | Low      | Matrix claims `True` (server ACK), LXMF claims `False` (despite 9-state model). Both are honest per their specific semantics. No change needed.                  |
| Meshtastic byte budget (B2)       | Medium   | `max_text_bytes=227` may not account for protobuf overhead with `reply_id`. The flag value is correct as-declared but may be optimistic for structured messages. |
| MeshCore hardcoded port 4000      | Low      | No capability flag impact.                                                                                                                                       |
| MeshCore reconnect parameters     | Low      | No capability flag impact.                                                                                                                                       |
| `M_UNKNOWN` HTTP status check     | Cosmetic | Affects error classification, not capability declarations.                                                                                                       |
| LXMF destination nuance           | Low      | No capability flag impact.                                                                                                                                       |

## 11. Summary

All 4 adapters × 21 capability fields = **84 capability declarations** audited.

- **84/84** JSON-to-code conformance: PASS
- **84/84** fake-to-real parity: PASS
- **0** overclaims identified
- **0** actionable underclaims identified
- **4** adapters with honest, conservative capability declarations

This audit confirms that the MEDRE capability system is in a truthful state
as of `adapter-lifecycle-parity` (after `adapter-sdk-parity` was merged). No source code, test, JSON, or
spec changes are required. This document serves as the authoritative
capability truth record.
