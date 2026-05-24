# LXMF Source-of-Truth Audit

> **Status:** Historical
> **Classification:** Audit
> **Authority:** Point-in-time audit of LXMF/Reticulum SDKs; not current authority for adapter behaviour
> **Last reviewed:** 2026-05-24
>
> Contract version: 1
> Last updated: 2026-05-08

This document records findings from auditing MEDRE's LXMF adapter
assumptions against the LXMF Python library
(`/home/jeremiah/dev/LXMF/LXMF/`, import name `LXMF`) and
Reticulum network stack (`/home/jeremiah/dev/Reticulum/RNS/`).

**Tranche status**: Audit only. No production Reticulum transport or
LXMF router running. This is pre-production foundation hardening for
the LXMF mesh messaging protocol over Reticulum.

---

## Tranche 1 Scope

This is a **pre-production audit**. The findings below are derived from
reading source code, not from running network captures or live
Reticulum sessions. Specifically:

- Test fixtures are **source-shaped approximations** built to match the
  LXMF wire format as described in the reference source. They are not
  captured from a running LXMF router.
- No real LXMF or Reticulum dependency is required for default tests.
  All tests use `FakeLxmfAdapter` and hand-crafted packet dicts.
- The adapter operates exclusively in `connection_type="fake"` mode.
  Real connectivity is deferred.

---

## 1. Reference Material Availability

### 1.1 LXMF Library

| Source               | Location                                  | Format                     |
| -------------------- | ----------------------------------------- | -------------------------- |
| LXMF source          | `/home/jeremiah/dev/LXMF/LXMF/`           | Python package             |
| Import name          | `LXMF`                                    | `import LXMF`              |
| Core modules         | `LXMF.py`, `LXMessage.py`, `LXMRouter.py` | Message, router, constants |
| LXMF version         | 0.9.6                                     |                            |
| Reticulum dependency | `rns>=1.2.0`                              | Required transport layer   |

### 1.2 Reticulum Network Stack

| Source             | Location                            | Format                        |
| ------------------ | ----------------------------------- | ----------------------------- |
| RNS source         | `/home/jeremiah/dev/Reticulum/RNS/` | Python package                |
| Identity module    | `Identity.py`                       | Ed25519/X25519 key handling   |
| Destination module | `Destination.py`                    | Addressable endpoint creation |
| Transport          | Packet, Link, Resource              | Wire-level primitives         |

All behavioral facts below are extracted from these sources.

---

## 2. Identity/Addressing Model

### 2.1 Keypair Identity

Reticulum uses a **dual-keypair identity**: Ed25519 for signing and
X25519 for encryption, both derived from a single 64-byte private key.

| Property             | Value                                 |
| -------------------- | ------------------------------------- |
| Signing key          | Ed25519                               |
| Encryption key       | X25519 (derived from Ed25519)         |
| Private key size     | 64 bytes                              |
| Public key size      | 64 bytes (Ed25519 + X25519)           |
| Identity hash        | `SHA-256(public_key)[:16]` = 16 bytes |
| Identity hash format | hex string, 32 hex chars              |

### 2.2 Destination Hash

A destination hash binds an application aspect to an identity:

```text
destination_hash = SHA-256(SHA-256("app.aspect")[:10] + identity_hash)[:16]
```

LXMF uses two aspects:

- `lxmf.delivery` for point-to-point messages
- `lxmf.propagation` for propagation node sync

### 2.3 Addressing Implications for MEDRE

| Finding                                                 | Status                                   |
| ------------------------------------------------------- | ---------------------------------------- |
| User-facing address is the identity hash (32 hex chars) | **Confirmed**                            |
| Destination hash cannot be reversed to identity hash    | **Confirmed**. One-way derivation.       |
| Identity hash can derive all destination hashes         | **Confirmed**. Forward derivation.       |
| No numeric node ID concept                              | **Confirmed**. Everything is hash-based. |
| Two LXMF aspects: delivery and propagation              | **Confirmed**                            |

---

## 3. LXMessage Structure

### 3.1 Wire Format

LXMessage is the fundamental message unit on the wire:

```yaml
Wire: [dest_hash:16][src_hash:16][sig:64][msgpack_payload]
msgpack_payload = [timestamp, title, content, fields, stamp?]
Message ID = SHA-256(dest_hash + src_hash + msgpack_payload) = 32 bytes
Signature = Ed25519 over (dest_hash + src_hash + msgpack_payload + message_id)
```

### 3.2 Payload Fields

| Field       | Type  | Description                  |
| ----------- | ----- | ---------------------------- |
| `timestamp` | float | UNIX seconds                 |
| `title`     | bytes | UTF-8 encoded subject line   |
| `content`   | bytes | UTF-8 encoded message body   |
| `fields`    | dict  | Integer-keyed extension dict |
| `stamp`     | bytes | Optional proof-of-work stamp |

### 3.3 Fields Dict (Extension Fields)

Defined in `LXMF.py` with integer keys:

| Key    | Name                     | Description                          |
| ------ | ------------------------ | ------------------------------------ |
| `0x01` | `FIELD_EMBEDDED_LXMS`    | Embedded LXMF messages               |
| `0x02` | `FIELD_TELEMETRY`        | Telemetry data                       |
| `0x05` | `FIELD_FILE_ATTACHMENTS` | File attachments                     |
| `0x06` | `FIELD_IMAGE`            | Image data                           |
| `0x07` | `FIELD_AUDIO`            | Audio data                           |
| `0x08` | `FIELD_THREAD`           | Conversation grouping                |
| `0x09` | `FIELD_COMMANDS`         | Command interface                    |
| `0x0C` | `FIELD_TICKET`           | Reply permission (expiry + 16 bytes) |
| `0x0F` | `FIELD_RENDERER`         | Custom renderer hint                 |
| `0xFB` | `FIELD_CUSTOM_TYPE`      | Custom type identifier               |
| `0xFC` | `FIELD_CUSTOM_DATA`      | Custom payload data                  |
| `0xFD` | `FIELD_CUSTOM_META`      | Custom metadata                      |
| `0xFE` | `FIELD_NON_SPECIFIC`     | Non-specific extension               |
| `0xFF` | `FIELD_DEBUG`            | Debug information                    |

### 3.4 Key Message Shape Findings for MEDRE

| Finding                                             | Status        |
| --------------------------------------------------- | ------------- |
| `dest_hash` and `src_hash` are 16 bytes each        | **Confirmed** |
| Message ID is a 32-byte SHA-256 hash                | **Confirmed** |
| Signature is 64-byte Ed25519                        | **Confirmed** |
| Payload uses msgpack encoding                       | **Confirmed** |
| Both `title` and `content` are separate fields      | **Confirmed** |
| `fields` dict uses integer keys, not string keys    | **Confirmed** |
| `FIELD_TICKET` carries reply permission with expiry | **Confirmed** |
| `FIELD_THREAD` groups conversation messages         | **Confirmed** |

---

## 4. Delivery Methods

### 4.1 Method Overview

| Method        | Code   | Description                                                                                  |
| ------------- | ------ | -------------------------------------------------------------------------------------------- |
| DIRECT        | `0x02` | Link-based, reliable. Up to 319 bytes per packet. Larger payloads use RNS Resource transfer. |
| OPPORTUNISTIC | `0x01` | Single packet, fire-and-forget. Max 295 bytes encrypted.                                     |
| PROPAGATED    | `0x03` | Store-and-forward via propagation node.                                                      |
| PAPER         | `0x05` | Offline transfer via QR code or URI.                                                         |

### 4.2 Delivery Method Implications for MEDRE

| MEDRE Concern         | LXMF Behavior                                             |
| --------------------- | --------------------------------------------------------- |
| Reliable delivery     | DIRECT mode with link establishment and Resource transfer |
| Best-effort delivery  | OPPORTUNISTIC mode, single packet, no ACK                 |
| Offline delivery      | PROPAGATED mode via propagation node                      |
| Air-gapped transfer   | PAPER mode for QR/URI export                              |
| Large message support | DIRECT + RNS Resource handles arbitrary sizes             |

---

## 5. Delivery Callbacks

### 5.1 Per-Message Callbacks

```python
message.register_delivery_callback(cb)   # cb(msg) on DELIVERED or SENT
message.register_failed_callback(cb)     # cb(msg) on FAILED/REJECTED/CANCELLED
```

### 5.2 Router-Level Callbacks

```python
router.register_delivery_callback(cb)    # cb(msg) for every received message
```

### 5.3 Callback Implications for MEDRE

| MEDRE Concern         | LXMF Behavior                                                 |
| --------------------- | ------------------------------------------------------------- |
| Inbound message hook  | Router delivery callback fires for all received messages      |
| Outbound confirmation | Per-message delivery callback on SENT or DELIVERED            |
| Failure notification  | Per-message failed callback with state detail                 |
| Multiple listeners    | Router callback is global, message callbacks are per-instance |

---

## 6. What Requires Reticulum

The following operations cannot be performed without a running
Reticulum transport:

| Operation                          | RNS Dependency               |
| ---------------------------------- | ---------------------------- |
| Identity creation/recall           | `RNS.Identity`               |
| Destination creation               | `RNS.Destination`            |
| Packet sending                     | `RNS.Packet`                 |
| Link establishment                 | `RNS.Link`                   |
| Resource transfer (large payloads) | `RNS.Resource`               |
| Encryption/decryption              | Handled by `RNS.Destination` |
| Ed25519 signatures                 | Handled by `RNS.Identity`    |
| Path requests                      | RNS transport layer          |
| Announces                          | RNS announce system          |

---

## 7. Pure LXMF (Conceptual, No RNS Required)

These message-level concepts can be understood and manipulated without
a live Reticulum instance:

- Message structure: `dest_hash`, `src_hash`, `signature`, `payload`
- Payload composition: `timestamp`, `title`, `content`, `fields`
- Message ID computation via SHA-256
- Field definitions and conventions
- Paper message encoding (QR/URI format)

This is useful for test fixtures and offline validation, but any real
send/receive requires Reticulum.

---

## 8. Key Differences from Meshtastic/MeshCore

| Aspect           | Meshtastic          | MeshCore               | LXMF                          |
| ---------------- | ------------------- | ---------------------- | ----------------------------- |
| Identity         | NodeNum (int)       | Ed25519 pubkey (hex)   | Ed25519 hash (16B)            |
| Message ID       | packet_id (int)     | sender_timestamp (int) | SHA-256 hash (32B)            |
| Wire format      | Protobuf            | Custom binary          | msgpack binary                |
| Message fields   | `decoded.text` only | text only              | title + content + dict fields |
| Reply model      | replyId (int)       | None                   | Ticket-based (PoW bypass)     |
| Rich content     | None                | None                   | Title, fields dict, resources |
| Large messages   | Fragmented protobuf | 255B frame limit       | RNS Resource (arbitrary size) |
| Offline delivery | No                  | No                     | PROPAGATED + PAPER modes      |
| Encryption       | Optional per-packet | Always-on E2EE         | Always-on via RNS Destination |

---

## 9. MEDRE Assumptions Supported

| MEDRE Assumption                                            | LXMF Evidence                                          | Verdict       |
| ----------------------------------------------------------- | ------------------------------------------------------ | ------------- |
| `source_transport_id` from hex(source_hash)                 | `source_hash` is 16 bytes, displayable as 32 hex chars | **Supported** |
| `content_as_string()` provides message body                 | `content` field is UTF-8 bytes                         | **Supported** |
| `fields` dict can carry MEDRE metadata envelope             | Integer-keyed dict supports arbitrary nesting          | **Supported** |
| `message.hash` is unique message ID for `source_native_ref` | SHA-256 of dest+src+payload = 32 bytes                 | **Supported** |
| Timestamp is float UNIX seconds                             | `timestamp` field is float                             | **Supported** |
| No native reply ID, but tickets allow correlation           | `FIELD_TICKET` with expiry + 16 bytes                  | **Supported** |
| Separate title and body fields                              | `title` and `content` are distinct                     | **Supported** |
| Identity is hash-based, not numeric                         | 16-byte truncated SHA-256 of pubkey                    | **Supported** |
| Multiple delivery modes available                           | DIRECT, OPPORTUNISTIC, PROPAGATED, PAPER               | **Supported** |
| Always-on encryption                                        | RNS Destination handles encryption transparently       | **Supported** |

---

## 10. MEDRE Assumptions Still Scaffold

| MEDRE Assumption                         | Status                                                                                                                                                                          | Action Required                                    |
| ---------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------- |
| Real Reticulum/LXMF dependency in tests  | **Scaffold**. No RNS instance in test suite.                                                                                                                                    | Set up RNS test transport or mock layer.           |
| DIRECT/OPPORTUNISTIC/PROPAGATED delivery | **Scaffold**. No real delivery code.                                                                                                                                            | Wire `LXMRouter` with RNS transport.               |
| Identity file loading                    | **Scaffold**. No `RNS.Identity()` recall.                                                                                                                                       | Load or create identity from storage.              |
| Announce/advertisement                   | **Scaffold**. No announce logic.                                                                                                                                                | Call `destination.announce()` for presence.        |
| Resource transfer (attachments)          | **Scaffold**. No `RNS.Resource` usage.                                                                                                                                          | Implement for `FIELD_FILE_ATTACHMENTS`.            |
| Ticket-based reply correlation           | **Scaffold**. Tickets not generated or validated.                                                                                                                               | Implement `FIELD_TICKET` creation and parsing.     |
| Relation reconstruction from fields      | **Deferred**. The fields envelope can carry relation metadata, but reconstructing `EventRelation` objects from inbound field data is not implemented.                           | Wire relation extraction in codec when needed.     |
| Fields envelope format                   | **Scaffold**. MEDRE convention only, not enforced by LXMF. The MEDRE fields envelope can express structured metadata, but this is a MEDRE convention, not a protocol guarantee. | Define and document field key mapping.             |
| Propagation node sync                    | **Scaffold**. No propagation node client.                                                                                                                                       | Implement `lxmf.propagation` destination handling. |
| Paper message encode/decode              | **Scaffold**. No QR/URI generation.                                                                                                                                             | Wire PAPER mode if offline transfer needed.        |

---

_This document was produced by auditing available reference sources. It does
not replace live transport testing. All findings are based on source code
analysis, not running Reticulum network captures._
