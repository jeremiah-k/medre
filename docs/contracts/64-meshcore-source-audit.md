# MeshCore Source-of-Truth Audit

> **Status:** Historical
> **Classification:** Audit
> **Authority:** Point-in-time audit of MeshCore SDK; not current authority for adapter behaviour
> **Last reviewed:** 2026-05-26
>
> Contract version: 2
> Last updated: 2026-05-26

This document records findings from auditing MEDRE's MeshCore adapter
assumptions against the MeshCore Python library
(`/home/jeremiah/dev/meshcore/meshcore_py/`, import name `meshcore`) and
firmware source (`/home/jeremiah/dev/meshcore/MeshCore/`).

**Tranche status**: Audit only. No production connection or hardware
support. This is pre-production foundation hardening for the MeshCore
mesh radio protocol.

---

## 1. Reference Material Availability

### 1.1 MeshCore Python Library

| Source             | Location                                   | Format                        |
| ------------------ | ------------------------------------------ | ----------------------------- |
| meshcore_py source | `/home/jeremiah/dev/meshcore/meshcore_py/` | Python package                |
| Import name        | `meshcore`                                 | `import meshcore`             |
| Entry point        | `MeshCore` class                           | TCP, Serial, BLE constructors |
| Event types        | `meshcore.EventType`                       | Enum                          |
| Commands           | `meshcore.commands`                        | Async send API                |

### 1.2 MeshCore Firmware

| Source          | Location                                | Format                     |
| --------------- | --------------------------------------- | -------------------------- |
| Firmware source | `/home/jeremiah/dev/meshcore/MeshCore/` | C/C++                      |
| Radio protocol  | Custom binary, no protobuf              | LoRa frames, max 255 bytes |

All behavioral facts below are extracted from these sources.

---

## 2. Identity/Addressing Model

### 2.1 Keypair Identity

MeshCore uses **Ed25519 keypair identity**. Each node has a 32-byte public key
represented as a hex string. There is no numeric node number.

| Property          | Value                                                        |
| ----------------- | ------------------------------------------------------------ |
| Key type          | Ed25519                                                      |
| Public key size   | 32 bytes                                                     |
| Public key format | hex string (64 hex chars)                                    |
| Node hash         | first N bytes of pubkey hex (configurable: 1, 2, or 3 bytes) |

### 2.2 Contact-Based Addressing

MeshCore addresses messages to **public keys**, not node numbers. The contact
list lives at `meshcore.contacts` as a dict keyed by pubkey hex.

```python
# Contact structure
contact = {
    "adv_name": str,        # Advertised name
    "public_key": str,      # Full pubkey hex
    "adv_lat": float,       # Advertised latitude
    "adv_lon": float,       # Advertised longitude
    "out_path": str,        # Routing path
    "out_path_len": int,    # Path length
    "type": int,            # Contact type
}
```

### 2.3 Addressing Implications for MEDRE

| Finding                                          | Status                                               |
| ------------------------------------------------ | ---------------------------------------------------- |
| No broadcast address concept                     | **Confirmed**. Send to specific pubkey or use flood. |
| No numeric node ID                               | **Confirmed**. Identity is always pubkey hex.        |
| Contact list is dict keyed by pubkey hex         | **Confirmed**.                                       |
| Node hash is truncated pubkey, not a separate ID | **Confirmed**.                                       |

---

## 3. Packet/Message Structures

### 3.1 Direct Messages

Direct messages arrive as `EventType.CONTACT_MSG_RECV` with a payload dict:

```python
{
    "type": "PRIV",
    "pubkey_prefix": "hex6",       # Truncated sender pubkey
    "sender_timestamp": int,       # Sender's timestamp
    "txt_type": int,               # Text type code
    "text": "str",                 # Message body
    # ... additional fields
}
```

### 3.2 Channel Messages

Channel messages arrive as `EventType.CHANNEL_MSG_RECV` with a payload dict:

```python
{
    "type": "CHAN",
    "channel_idx": int,            # Channel index
    "sender_timestamp": int,       # Sender's timestamp
    "txt_type": int,               # Text type code
    "text": "str",                 # Message body
    # ... additional fields
}
```

### 3.3 Native Feature Gaps

| Feature            | Meshtastic                 | MeshCore                                                                   |
| ------------------ | -------------------------- | -------------------------------------------------------------------------- |
| Reply-to (replyId) | `decoded.replyId` (int)    | **No native mechanism**. Replies are application-level convention in text. |
| Reactions/emoji    | `decoded.emoji` (int flag) | **No native mechanism**.                                                   |
| Wire format        | Protobuf `MeshPacket`      | **Custom binary** (no protobuf).                                           |

### 3.4 Key Packet Shape Findings for MEDRE

| Finding                                                      | Status        |
| ------------------------------------------------------------ | ------------- |
| Direct messages carry `pubkey_prefix` (truncated sender key) | **Confirmed** |
| Channel messages carry `channel_idx`                         | **Confirmed** |
| Both carry `sender_timestamp`, `txt_type`, `text`            | **Confirmed** |
| No native `replyId` field exists                             | **Confirmed** |
| No native `emoji`/reaction field exists                      | **Confirmed** |
| No protobuf involved at any layer                            | **Confirmed** |

---

## 4. Send API

### 4.1 Basic Send

```python
event = await meshcore.commands.send_msg(dst, msg, timestamp=None)
```

Returns an `Event` with:

- `type == EventType.MSG_SENT`
- `payload["expected_ack"]`: 4-byte hex string (CRC of sent message)
- `payload["suggested_timeout"]`: int, suggested ACK timeout in milliseconds

### 4.2 Send with Retry

```python
event = await meshcore.commands.send_msg_with_retry(dst, msg, ...)
```

Built-in retry loop with ACK waiting. Handles timeout and retransmission
internally.

### 4.3 ACK Handling

ACK events arrive separately via the event dispatcher:

- `EventType.ACK` with payload `{"code": "hex8"}`
- The `expected_ack` from `send_msg` is correlated against incoming ACK
  `code` values

### 4.4 Send-Result Implications for MEDRE

| MEDRE Concern              | MeshCore Behavior                                                   |
| -------------------------- | ------------------------------------------------------------------- |
| Outbound native message ID | `expected_ack` (4 bytes hex) acts as the delivery correlation token |
| Send returns synchronously | Returns Event immediately; ACK is async                             |
| Retry logic                | Available via `send_msg_with_retry` or manual ACK watching          |
| ACK timeout                | `suggested_timeout` provided by firmware                            |

---

## 5. Message Events

### 5.1 EventDispatcher Pattern

MeshCore uses a pub/sub `EventDispatcher` with attribute-based filtering.
All message events carry an `attributes` dict that can be used for
subscription filtering.

### 5.2 Event Attributes

| Event Type         | Attributes                                  |
| ------------------ | ------------------------------------------- |
| `CONTACT_MSG_RECV` | `{"pubkey_prefix": "...", "txt_type": int}` |
| `CHANNEL_MSG_RECV` | `{"channel_idx": int, "txt_type": int}`     |

### 5.3 Event Types Relevant to MEDRE

| EventType          | Purpose                                       |
| ------------------ | --------------------------------------------- |
| `CONTACT_MSG_RECV` | Direct (private) message received             |
| `CHANNEL_MSG_RECV` | Channel (group) message received              |
| `MSG_SENT`         | Outbound message sent, carries `expected_ack` |
| `ACK`              | Delivery acknowledgment, carries `code`       |

---

## 6. Connection Types

### 6.1 Constructors

```python
# TCP
mc = MeshCore.create_tcp(host, port)          # default port 4000

# Serial
mc = MeshCore.create_serial(port, baudrate=115200)

# BLE
mc = MeshCore.create_ble(address, pin=...)
```

### 6.2 Auto-Reconnect

Optional, configurable `max_attempts`. Not enabled by default.

### 6.3 Connection Implications for MEDRE

| MEDRE Concern            | MeshCore Behavior              |
| ------------------------ | ------------------------------ |
| Multiple transport types | TCP, Serial, BLE all supported |
| Default TCP port         | 4000                           |
| Default serial baudrate  | 115200                         |
| BLE pairing              | Pin-based optional             |
| Reconnect                | Optional, configurable         |

---

## 7. Wire Protocol Reference

> **Note**: This section is for reference only. Wire protocol details are
> **not required** for MEDRE tranche 1. The Python library abstracts all
> wire-level concerns.

### 7.1 Frame Structure

| Field        | Size     | Description                               |
| ------------ | -------- | ----------------------------------------- |
| Route type   | 2 bits   | Flood vs direct, optional transport codes |
| Payload type | 4 bits   | Message kind                              |
| Version      | 2 bits   | Protocol version                          |
| Payload      | variable | Up to 255 bytes total frame               |

### 7.2 Payload Types

| Code | Name     | Description    |
| ---- | -------- | -------------- |
| 0    | REQ      | Request        |
| 1    | RESPONSE | Response       |
| 2    | TXT_MSG  | Text message   |
| 3    | ACK      | Acknowledgment |
| 4    | ADVERT   | Advertisement  |
| 5    | GRP_TXT  | Group text     |
| 6    | GRP_DATA | Group data     |

### 7.3 Encryption

- All text/request messages use **always-on E2EE**: AES-128 with 2-byte HMAC
  MAC, keyed by ECDH shared secret
- Group messages encrypted with channel secret
- No optional/per-packet encryption toggle

---

## 8. Key Differences from Meshtastic

| Aspect         | Meshtastic                       | MeshCore                                        |
| -------------- | -------------------------------- | ----------------------------------------------- |
| Identity       | NodeNum (int) + fromId (str)     | Ed25519 public key (hex)                        |
| Addressing     | Broadcast + DM by NodeNum        | Contact-based by pubkey                         |
| Wire format    | Protobuf `MeshPacket`            | Custom binary (LoRa frame)                      |
| Send return    | `MeshPacket` with `id` field     | Event with `expected_ack` + `suggested_timeout` |
| ACK            | ROUTING_APP protobuf             | Separate ACK event with CRC code                |
| Reply          | `decoded.replyId` (int)          | No native reply mechanism                       |
| Reactions      | `decoded.emoji` (int flag)       | No native reactions                             |
| Encryption     | Optional per-packet              | Always-on E2EE by default                       |
| Node discovery | NodeInfo broadcast + node DB     | Contact advertisement + contact list            |
| Channel model  | Channel index + channel settings | Channel index + channel secret                  |

---

## 9. What Remains Unverified

| Area                                                            | Status                     | Risk   |
| --------------------------------------------------------------- | -------------------------- | ------ |
| Real Python callback packet shapes match MEDRE fixtures exactly | Not verified with hardware | Medium |
| TCP/Serial/BLE connection lifecycle nuances                     | Not tested                 | Medium |
| ACK correlation and timeout behavior details                    | Not verified               | Medium |
| Channel message encryption/decryption details                   | Deferred                   | Low    |
| Message retry and delivery confirmation edge cases              | Not verified               | Low    |
| Contact advertisement and discovery timing                      | Not verified               | Low    |
| `txt_type` field meaning and possible values                    | Not verified               | Low    |
| `pubkey_prefix` truncation length in real callbacks             | Not verified               | Low    |
| Payload size limits and message fragmentation                   | Not verified               | Low    |
| Flood message behavior and scope                                | Not verified               | Low    |

---

## 10. MEDRE Assumptions Supported

| MEDRE Assumption                              | MeshCore Evidence                                | Verdict       |
| --------------------------------------------- | ------------------------------------------------ | ------------- |
| Identity is pubkey hex, not numeric node ID   | Confirmed. Ed25519 keypair, no NodeNum.          | **Supported** |
| Contact list is dict keyed by pubkey          | Confirmed. `meshcore.contacts` dict.             | **Supported** |
| Direct messages carry truncated sender pubkey | Confirmed. `pubkey_prefix` in CONTACT_MSG_RECV.  | **Supported** |
| Channel messages carry channel index          | Confirmed. `channel_idx` in CHANNEL_MSG_RECV.    | **Supported** |
| No native reply mechanism                     | Confirmed. No replyId field exists.              | **Supported** |
| No native reaction mechanism                  | Confirmed. No emoji field exists.                | **Supported** |
| Send returns correlation token for ACK        | Confirmed. `expected_ack` + `suggested_timeout`. | **Supported** |
| ACK events arrive separately from sends       | Confirmed. EventType.ACK with code.              | **Supported** |
| Multiple connection transports supported      | Confirmed. TCP, Serial, BLE constructors.        | **Supported** |
| Always-on E2EE                                | Confirmed. AES-128 + HMAC, no toggle.            | **Supported** |
| Event dispatcher with attribute filtering     | Confirmed. Attributes on all message events.     | **Supported** |

---

## 11. MEDRE Assumptions Initially Scaffold (Historical Baseline)

> **Note:** Several items in this table were resolved in Tranche 4 (see §12.1) and Tranche 6 (see §12.4). This table is preserved as the historical baseline. See §12.2 for remaining scaffold items.

| MEDRE Assumption                      | Status                                           | Action Required                                      |
| ------------------------------------- | ------------------------------------------------ | ---------------------------------------------------- |
| MeshCore adapter connection lifecycle | **Scaffold**. No real connection code.           | Wire `MeshCore.create_tcp()` etc. in real adapter.   |
| Outbound message delivery correlation | **Scaffold**. No ACK watching implemented.       | Use `expected_ack` / ACK event correlation.          |
| Contact-based sender resolution       | **Scaffold**. No contact list lookup.            | Map `pubkey_prefix` to contact for sender identity.  |
| Channel message routing               | **Scaffold**. No channel subscription logic.     | Subscribe to CHANNEL_MSG_RECV with attribute filter. |
| Flood message handling                | **Scaffold**. No flood send/receive support.     | Future tranche.                                      |
| Message retry logic                   | **Scaffold**. No retry implementation.           | Use `send_msg_with_retry` or manual retry.           |
| `txt_type` field handling             | **Scaffold**. Values not documented.             | Map txt_type to MEDRE text categories when known.    |
| Payload size limits                   | **Scaffold**. 255-byte frame limit not enforced. | Enforce in renderer/adapter.                         |
| Reconnection behavior                 | **Scaffold**. Auto-reconnect not wired.          | Configure `max_attempts` in connection setup.        |

---

## 12. Tranche 4 Resolution (2026-05-26)

Tranche 4 (`t4-meshcore-maturation`) resolved several scaffold items from section 11 through lifecycle hardening in `MeshCoreSession` and renderer byte budget verification. No production adapter code was changed; only tests and docs were updated.

### 12.1 Gaps Closed

| MEDRE Assumption                      | Pre-T4 Status                                | Post-T4 Status                    | Resolution                                                                                                              |
| ------------------------------------- | -------------------------------------------- | --------------------------------- | ----------------------------------------------------------------------------------------------------------------------- |
| Connection lifecycle (TCP/serial/BLE) | Scaffold — no real connection code           | **Verified via mocked SDK tests** | `MeshCoreSession` wires `create_tcp`, `create_serial`, `create_ble` factory calls; mocked tests verify constructor args |
| Event subscription wiring             | Scaffold — no subscription logic             | **Verified via mocked SDK tests** | Session subscribes to CONTACT_MSG_RECV, CHANNEL_MSG_RECV, DISCONNECTED; mocked tests verify 3 subscriptions registered  |
| Outbound send with retry              | Scaffold — no retry                          | **Implemented in session**        | `send_text()` retries transient failures up to 3 times; SDK ERROR classified as permanent; counters tracked             |
| Reconnection behavior                 | Scaffold — auto-reconnect not wired          | **Implemented in session**        | Bounded exponential backoff (1s → 30s cap, ±25% jitter, max 10 attempts) in `_reconnect_loop()`                         |
| Inbound callback normalization        | Scaffold — no normalization                  | **Implemented in session**        | `_on_sdk_event()` extracts dict from SDK Event, normalizes non-dict payloads to `{}`, catches callback exceptions       |
| Payload size limits (renderer)        | Scaffold — 255-byte frame limit not enforced | **Verified via tests**            | `_truncate_utf8_bytes()` enforces configurable `max_text_bytes` (default 512); multi-byte codepoints never split        |

### 12.2 Gaps Still Scaffold

| MEDRE Assumption                              | Status                                       | Action Required                                      |
| --------------------------------------------- | -------------------------------------------- | ---------------------------------------------------- |
| Contact-based sender resolution               | **Scaffold**. No contact list lookup.        | Map `pubkey_prefix` to contact for sender identity.  |
| Channel message routing with attribute filter | **Scaffold**. No attribute-based filtering.  | Subscribe to CHANNEL_MSG_RECV with attribute filter. |
| Flood message handling                        | **Scaffold**. No flood send/receive support. | Future tranche.                                      |
| `txt_type` field handling                     | **Scaffold**. Values not documented.         | Map txt_type to MEDRE text categories when known.    |
| ACK / delivery confirmation tracking          | **Scaffold**. `expected_ack` not correlated. | Implement ACK watcher for delivery receipts.         |

### 12.3 Remaining Unverified (Hardware Required)

All items from section 9 remain unverified. Tranche 4 added no hardware validation. Mocked SDK tests verify API wiring but not real radio behavior.

### 12.4 Tranche 6 Resolution (2026-05-26)

Tranche 6 (`t6-evidence-diagnostics`) added session hardening tests and doc cleanup. No production adapter code was changed beyond session edge-case fixes.

#### Gaps Closed

| MEDRE Assumption                  | Pre-T6 Status                                | Post-T6 Status         | Resolution                                                                                                      |
| --------------------------------- | -------------------------------------------- | ---------------------- | --------------------------------------------------------------------------------------------------------------- |
| Sync callback handling            | Not tested — sync callbacks caused TypeError | **Verified via tests** | `_on_sdk_event()` checks `asyncio.iscoroutine()` before awaiting; sync callbacks no longer produce false errors |
| Failed-start cleanup              | Not tested — failed start left stale state   | **Verified via tests** | `start()` wraps `_connect_real()` in try/except; on failure clears `_message_callback`                          |
| Inbound callback exception safety | Partial — callback exceptions untested       | **Verified via tests** | Fire-and-forget tasks have `_log_task_exception()` done callback; exceptions logged, not swallowed              |

#### Gaps Still Scaffold

Same as §12.2. Tranche 6 did not close any additional scaffold items beyond session edge-case hardening.

#### Remaining Unverified

Same as §12.3. Tranche 6 added no hardware validation.

---

_This document was produced by auditing available reference sources. It does
not replace hardware-verified testing. All findings are based on source code
analysis, not live radio captures._
