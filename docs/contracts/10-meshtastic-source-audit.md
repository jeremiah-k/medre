# Meshtastic Source-of-Truth Audit

> Contract version: 2
> Last updated: 2026-05-09

This document records findings from auditing MEDRE's Meshtastic adapter
assumptions against available reference material: the old MMRelay codebase
(`/home/jeremiah/dev/meshtastic-matrix-relay`) and the installed `mtjk`
(meshtastic-python fork) package. All API findings are verified from
direct GitHub/master-branch source evidence.

**Tranche 2 status**: Audit only. No production connection or hardware
support was added. This is still pre-production foundation hardening.

---

## 0. Master-Branch Evidence Summary

All findings below are verified from the mtjk/master branch at
`github.com/jeremiah-k/mtjk`. The following APIs have been confirmed
by direct source inspection:

| API                        | Verified Signature                                                                                                                                                | Notes                                                           |
| -------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------- |
| `TCPInterface.__init__`    | `(hostname, ..., portNumber=4403, noNodes=False, timeout=300)`                                                                                                    | Default port 4403, not 4402                                     |
| `SerialInterface.__init__` | `(devPath=None, ...)`                                                                                                                                             | `devPath` is optional                                           |
| `BLEInterface.__init__`    | `(address: Optional[str], ...)`                                                                                                                                   | Uses `address`, not `ble_address`                               |
| `sendText`                 | `(text, destinationId=BROADCAST_ADDR, wantAck=False, wantResponse=False, onResponse=None, channelIndex=0, portNum=TEXT_MESSAGE_APP, replyId=None, hopLimit=None)` | Returns `MeshPacket` with populated `id`                        |
| `sendData`                 | `(data, destinationId, portNum, ...)`                                                                                                                             | Returns `MeshPacket` with populated `id`; `sendText` wraps this |
| `close()`                  | Exists on TCP/Serial/BLE                                                                                                                                          | Clean shutdown                                                  |
| Pubsub subscribe           | `pub.subscribe(callback, "meshtastic.receive")`                                                                                                                   | Callback: `(packet, interface)`                                 |
| Connection topic           | `"meshtastic.connection.established"`                                                                                                                             | Fires on successful connection                                  |
| Receive topics             | `"meshtastic.receive"`, `"meshtastic.receive.text"`, `"meshtastic.receive.position"`, `"meshtastic.receive.user"`, `"meshtastic.receive.data.portnum"`            | Topic-hierarchy callbacks                                       |

---

## 1. Reference Material Availability

### 1.1 Old MMRelay Codebase

| Source          | Location                                                                          | Format                            |
| --------------- | --------------------------------------------------------------------------------- | --------------------------------- |
| MMRelay source  | `/home/jeremiah/dev/meshtastic-matrix-relay/src/mmrelay/`                         | Installed package (mmrelay 1.3.5) |
| MMRelay tests   | `/home/jeremiah/dev/meshtastic-matrix-relay/tests/`                               | pytest suite                      |
| MMRelay config  | `/home/jeremiah/dev/meshtastic-matrix-relay/src/mmrelay/tools/sample_config.yaml` | YAML reference                    |
| MMRelay history | `/home/jeremiah/Documents/mmrelay-history.txt`                                    | Historical notes                  |

All MMRelay behavioral facts below are extracted from these sources.

### 1.2 Installed mtjk (Meshtastic Python Fork)

| Property              | Value                                                                      |
| --------------------- | -------------------------------------------------------------------------- |
| Distribution name     | `mtjk`                                                                     |
| Installed version     | 2.7.8.post2                                                                |
| Import name           | `meshtastic`                                                               |
| Package path          | `/home/jeremiah/.platformio/penv/lib/python3.12/site-packages/meshtastic/` |
| Protobuf PortNum enum | `meshtastic.protobuf.portnums_pb2.PortNum`                                 |
| Master pyproject name | `meshtastic` v2.7.8                                                        |
| PyPI drop-in claim    | Import namespace remains `meshtastic`                                      |

The `mtjk` package is **not** the upstream `meshtastic` library — it is a
fork maintained at `github.com/jeremiah-k/mtjk`. The old MMRelay pins
`mtjk==2.7.8.post3`. MEDRE's pyproject.toml specifies no version pin.
The upstream/fork master branch uses pyproject name `meshtastic` v2.7.8
while the PyPI `mtjk` distribution advertises drop-in import namespace
compatibility (import as `meshtastic`).

---

## 2. Packet Callback Shapes

### 2.1 MMRelay-observed Shape (from `on_meshtastic_message`)

MMRelay subscribes to `pubsub` topic `"meshtastic.receive"` and receives a
two-argument callback: `on_meshtastic_message(packet: dict, interface)`.

The **packet dict** is produced by the mtjk library's
`_normalize_packet_from_radio` which calls `MessageToDict()` on the protobuf
`mesh_pb2.MeshPacket`, then enriches it with:

```python
# From mtjk's _enrich_packet_identity:
packet_dict["fromId"] = interface._node_num_to_id(packet_dict["from"], isDest=False)
packet_dict["toId"] = interface._node_num_to_id(packet_dict["to"])
```

**Fields present in real packets:**

| Key               | Type     | Source                          | Notes                                                    |
| ----------------- | -------- | ------------------------------- | -------------------------------------------------------- |
| `from`            | int      | protobuf `MeshPacket.from`      | Numeric node number; always present                      |
| `to`              | int      | protobuf `MeshPacket.to`        | Numeric node number or `0xFFFFFFFF` (broadcast)          |
| `id`              | int      | protobuf `MeshPacket.id`        | Monotonic packet ID                                      |
| `channel`         | int      | protobuf `MeshPacket.channel`   | Channel index (may be unset → defaults to 0)             |
| `decoded`         | dict     | protobuf `MeshPacket.decoded`   | Contains portnum, payload bytes, decoded fields          |
| `decoded.portnum` | str      | `PortNum.Name()`                | String name like `"TEXT_MESSAGE_APP"`, `"TELEMETRY_APP"` |
| `decoded.payload` | bytes    | protobuf `Data.payload`         | Raw protobuf payload bytes                               |
| `decoded.text`    | str      | `_on_text_receive`              | UTF-8 decoded from `payload` bytes                       |
| `decoded.routing` | dict     | protobuf `Routing`              | Present for ROUTING_APP; contains `errorReason`          |
| `decoded.replyId` | int      | protobuf `Data.reply_id`        | Reply-to packet ID (optional)                            |
| `decoded.emoji`   | int      | protobuf `Data.emoji`           | Emoji flag (1 = emoji/reaction); optional                |
| `rxTime`          | int      | protobuf `MeshPacket.rx_time`   | Receive timestamp (Unix secs); optional                  |
| `rxRssi`          | int      | protobuf `MeshPacket.rx_rssi`   | RSSI in dBm; optional                                    |
| `rxSnr`           | float    | protobuf `MeshPacket.rx_snr`    | SNR in dB; optional                                      |
| `hopLimit`        | int      | protobuf `MeshPacket.hop_limit` | Hop limit; optional                                      |
| `hopStart`        | int      | protobuf `MeshPacket.hop_start` | Initial hop limit; optional                              |
| `priority`        | str      | protobuf `MeshPacket.priority`  | String name of priority enum; optional                   |
| `fromId`          | str      | `_enrich_packet_identity`       | Node ID string like `"!abc123def456"` or `"^all"`        |
| `toId`            | str      | `_enrich_packet_identity`       | Node ID string or `None` if unknown                      |
| `raw`             | protobuf | `_normalize_packet_from_radio`  | Original protobuf MeshPacket                             |
| `encrypted`       | bool     | protobuf `MeshPacket.encrypted` | True if packet is encrypted                              |

### 2.2 Key Packet Shape Findings for MEDRE

| Finding                                                                                | Status                       |
| -------------------------------------------------------------------------------------- | ---------------------------- |
| `fromId`/`toId` are populated by interface's `_node_num_to_id` lookup, not by firmware | **Confirmed**                |
| `to` (int) is always present; `toId` may be `None` for unknown nodes                   | **Confirmed**                |
| `decoded.portnum` is the protobuf enum **name** string (e.g., `"TEXT_MESSAGE_APP"`)    | **Confirmed**                |
| `decoded.payload` is raw bytes, decoded into `decoded.text` by `_on_text_receive`      | **Confirmed**                |
| `decoded.replyId` is an optional int                                                   | **Confirmed**                |
| `decoded.emoji` is an optional int (1 = emoji)                                         | **Confirmed**                |
| `channel` may be absent → MEDRE defaults to `None`/`0`                                 | **Correct**                  |
| `id` is always present (MeshPacket requires it)                                        | **Correct**                  |
| `rxTime` is used for backlog suppression (rxTime < connect time → stale)               | **Not implemented in MEDRE** |
| `encrypted` flag exists on real packets                                                | **Not handled in MEDRE**     |

### 2.3 Gaps Between MEDRE Fixtures and Real Shapes

| MEDRE Assumption                                      | Real Behavior                                                                 | Gap                                                                           |
| ----------------------------------------------------- | ----------------------------------------------------------------------------- | ----------------------------------------------------------------------------- |
| `decoded` always contains `text` key for text packets | `text` is **added** by `_on_text_receive` after decoding from `payload` bytes | MEDRE fixtures set `text` directly, which matches the post-processed shape    |
| `channel` always present                              | May be absent in sparse callbacks; `MessageToDict` omits default values       | MEDRE classifiers handle missing channel correctly                            |
| `from` (numeric) always matches `fromId` hex          | `fromId` requires node DB lookup; may be `None` if node unknown               | MEDRE handles `fromId` fallback to numeric `from` correctly                   |
| No `encrypted` field tested                           | Real encrypted packets carry `encrypted: true`                                | MEDRE has no encrypted packet handling — out of scope for tranche 1           |
| No `rxTime` field tested                              | Real packets carry `rxTime` for backlog suppression                           | MEDRE has no backlog suppression — future tranche item                        |
| No `decoded.emoji` field tested                       | Real packets may carry `emoji: 1` for reactions                               | MEDRE has no reaction support — out of scope                                  |
| No `decoded.payload` bytes field                      | Real packets carry raw `payload` bytes alongside decoded fields               | MEDRE codec reads `decoded.text` not `payload` — matches post-processed shape |

---

## 3. Portnum Values

### 3.1 Real Protobuf PortNum Enum (from `mtjk 2.7.8.post2`)

The authoritative PortNum values come from the protobuf definition at
`meshtastic.protobuf.portnums_pb2.PortNum`:

| Name                          | Value | Category    |
| ----------------------------- | ----- | ----------- |
| `UNKNOWN_APP`                 | 0     | —           |
| `TEXT_MESSAGE_APP`            | 1     | text        |
| `REMOTE_HARDWARE_APP`         | 2     | —           |
| `POSITION_APP`                | 3     | position    |
| `NODEINFO_APP`                | 4     | nodeinfo    |
| `ROUTING_APP`                 | 5     | routing/ack |
| `ADMIN_APP`                   | 6     | admin       |
| `TEXT_MESSAGE_COMPRESSED_APP` | 7     | —           |
| `WAYPOINT_APP`                | 8     | —           |
| `AUDIO_APP`                   | 9     | —           |
| `DETECTION_SENSOR_APP`        | 10    | —           |
| `ALERT_APP`                   | 11    | —           |
| `KEY_VERIFICATION_APP`        | 12    | —           |
| `REMOTE_SHELL_APP`            | 13    | —           |
| `REPLY_APP`                   | 32    | —           |
| `IP_TUNNEL_APP`               | 33    | —           |
| `PAXCOUNTER_APP`              | 34    | —           |
| `STORE_FORWARD_PLUSPLUS_APP`  | 35    | —           |
| `NODE_STATUS_APP`             | 36    | —           |
| `SERIAL_APP`                  | 64    | —           |
| `STORE_FORWARD_APP`           | 65    | —           |
| `RANGE_TEST_APP`              | 66    | —           |
| `TELEMETRY_APP`               | 67    | telemetry   |
| `ZPS_APP`                     | 68    | —           |
| `SIMULATOR_APP`               | 69    | —           |
| `TRACEROUTE_APP`              | 70    | —           |
| `NEIGHBORINFO_APP`            | 71    | —           |
| `ATAK_PLUGIN`                 | 72    | —           |
| `MAP_REPORT_APP`              | 73    | —           |
| `POWERSTRESS_APP`             | 74    | —           |
| `LORAWAN_BRIDGE`              | 75    | —           |
| `RETICULUM_TUNNEL_APP`        | 76    | —           |
| `CAYENNE_APP`                 | 77    | —           |
| `ATAK_PLUGIN_V2`              | 78    | —           |
| `GROUPALARM_APP`              | 112   | —           |
| `PRIVATE_APP`                 | 256   | —           |
| `ATAK_FORWARDER`              | 257   | —           |
| `MAX`                         | 511   | —           |

### 3.2 MEDRE Scaffold vs Real Protobuf — Mismatches

The MEDRE `_NUMERIC_PORTNUM_MAP` in `packet_classifier.py` has these
deviations from the real protobuf enum:

| Map Key | MEDRE Value          | Real Value                  | Status                                           |
| ------- | -------------------- | --------------------------- | ------------------------------------------------ |
| `0`     | `"routing"`          | `"unknown"`                 | **WRONG**: 0 is `UNKNOWN_APP`, not `ROUTING_APP` |
| `1`     | `"text_message"`     | `"text_message"`            | Correct                                          |
| `2`     | `"text_message_ack"` | `"remote_hardware"`         | **WRONG**: no `TEXT_MESSAGE_ACK_APP` in protobuf |
| `3`     | `"position"`         | `"position"`                | Correct                                          |
| `4`     | `"nodeinfo"`         | `"nodeinfo"`                | Correct                                          |
| `5`     | `"telemetry"`        | `"routing"`                 | **WRONG**: 5 is `ROUTING_APP`                    |
| `6`     | `"store_forward"`    | `"admin"`                   | **WRONG**: 6 is `ADMIN_APP`                      |
| `7`     | `"waypoint"`         | `"text_message_compressed"` | **WRONG**: 7 is `TEXT_MESSAGE_COMPRESSED_APP`    |
| `9`     | `"audio"`            | `"audio"`                   | Correct                                          |
| `10`    | `"remote_hardware"`  | `"detection_sensor"`        | **WRONG**: 10 is `DETECTION_SENSOR_APP`          |
| `11`    | `"private"`          | `"alert"`                   | **WRONG**: 11 is `ALERT_APP`                     |
| `68`    | `"paxcounter"`       | `"zps"`                     | **WRONG**: 68 is `ZPS_APP`                       |
| `71`    | `"neighbor_info"`    | `"neighbor_info"`           | Correct                                          |
| `72`    | `"traceroute"`       | `"traceroute"`              | Correct                                          |

**Note**: There is **no** `TEXT_MESSAGE_ACK_APP` in the protobuf PortNum enum.
The MEDRE `"text_message_ack"` normalized portnum does not correspond to any
real protobuf value. ACKs in Meshtastic arrive via `ROUTING_APP` with
`decoded.routing.errorReason == "NONE"`.

### 3.3 MMRelay Portnum Usage

MMRelay uses **direct protobuf imports** rather than a custom map:

```python
from meshtastic.protobuf import portnums_pb2

portnums_pb2.PortNum.TEXT_MESSAGE_APP  # int 1
portnums_pb2.PortNum.Name(portnum)     # string "TEXT_MESSAGE_APP"
```

MMRelay's constants file defines only two portnum constants:

- `PORTNUM_TEXT_MESSAGE_APP = 1`
- `PORTNUM_DETECTION_SENSOR_APP = 10`

MMRelay's packet_routing uses `portnums_pb2.PortNum.Name()` for resolution
and string comparisons (`"TEXT_MESSAGE_APP"`, `"DETECTION_SENSOR_APP"`,
`"TELEMETRY_APP"`, `"RANGE_TEST_APP"`, etc.) for configuration matching.

### 3.4 Conclusion — MEDRE Numeric Portnum Map

The MEDRE `_NUMERIC_PORTNUM_MAP` is **fixture-scaffold only**. It is not
derived from the real protobuf enum, not derived from old MMRelay, and not
verified against any authoritative source. The symbolic map
(`_SYMBOLIC_PORTNUM_MAP`) is correct for tranche-1 categories but the
numeric map should not be treated as protocol authority.

---

## 4. MMRelay Behavioral Facts

### 4.1 DM Detection

```python
myId = interface.myInfo.my_node_num
toId = packet.get("to")

if toId == myId:
    is_dm = True
elif toId == BROADCAST_NUM or toId is None:  # BROADCAST_NUM = 0xFFFFFFFF
    is_dm = False
else:
    # Message for another node — drop entirely
    return
```

MMRelay compares the numeric `to` field against `myInfo.my_node_num`.
MEDRE currently compares `toId` string fallback instead. Both approaches
work but MMRelay's approach is more faithful to the protobuf shape.

### 4.2 Reaction Handling

MMRelay detects reactions via:

```python
emoji_flag = decoded.get("emoji") == 1  # EMOJI_FLAG_VALUE
reply_id = decoded.get("replyId")
```

When both `emoji == 1` and `replyId` are present, the packet is treated as
a reaction to the referenced `replyId`. MEDRE has no reaction handling.

### 4.3 Reply Handling

MMRelay uses:

```python
replyId = decoded.get("replyId")  # int
```

No `emoji` flag means it's a text reply. MEDRE's codec correctly extracts
`replyId` from `decoded` into an `EventRelation`.

### 4.4 Send Path

MMRelay uses protobuf construction directly (not public `sendText`):

```python
data_msg = mesh_pb2.Data()
data_msg.portnum = portnums_pb2.PortNum.TEXT_MESSAGE_APP
data_msg.payload = text.encode("utf-8")
data_msg.reply_id = reply_id

mesh_packet = mesh_pb2.MeshPacket()
mesh_packet.channel = channelIndex
mesh_packet.decoded.CopyFrom(data_msg)
mesh_packet.id = interface._generatePacketId()

return interface._sendPacket(mesh_packet, destinationId=..., wantAck=...)
```

This sends via `_sendPacket` (private API) and generates a packet ID via
`_generatePacketId()`. The returned `MeshPacket` protobuf has the `id`
field populated.

### 4.5 Queue/Pacing

MMRelay has a separate message queue (`message_queue.py`) with:

- `MAX_QUEUE_SIZE = 500`
- `DEFAULT_MESSAGE_DELAY = 2.5` seconds (between consecutive sends)
- `MINIMUM_MESSAGE_DELAY = 2.0` (firmware minimum)
- Single-worker executor for serialized sends

MEDRE's `MeshtasticOutboundQueue` uses a different architecture (deque-based
with `process_one` scaffold). The pacing concept is shared but the
implementation is independent.

### 4.6 Startup Backlog Suppression

MMRelay drops packets received within `STARTUP_PACKET_DRAIN_SECS = 15.0`
seconds of the first process-lifetime connect, and also drops packets whose
`rxTime < RELAY_START_TIME` (adjusted for clock skew).

MEDRE has no backlog suppression yet. The config field
`startup_backlog_suppress_seconds` exists but is not implemented.

### 4.7 ACK Handling

MMRelay only uses ACKs for health probes (ADMIN_APP with `wantAck=True`),
not for normal message acknowledgment. ROUTING_APP packets with
`routing.errorReason == "NONE"` are handled only in the health probe
context. Normal message ACKs are ignored.

MEDRE's approach of dropping ACK packets at the classifier level is
consistent with MMRelay behavior — neither system processes ACKs for
normal messages.

### 4.8 Channel Index Handling

MMRelay validates channel index against `MESHTASTIC_CHANNEL_MIN=0` and
`MESHTASTIC_CHANNEL_MAX=7`. MEDRE has no channel range validation.

---

## 5. Send-Result and Outbound ID Audit

### 5.1 Real mtjk `sendText` Return Value

Verified from master branch source:

```python
def sendText(self, text, destinationId=BROADCAST_ADDR, wantAck=False,
             wantResponse=False, onResponse=None, channelIndex=0,
             portNum=TEXT_MESSAGE_APP, replyId=None, hopLimit=None):
    return self.sendData(text.encode("utf-8"), destinationId, portNum=portNum,
                         wantAck=wantAck, wantResponse=wantResponse,
                         onResponse=onResponse, channelIndex=channelIndex,
                         hopLimit=hopLimit, replyId=replyId)
```

`sendText` returns a `mesh_pb2.MeshPacket` protobuf object. The returned
packet has its `id` field populated with the packet ID assigned by the
interface's `_send_pipeline`. This packet ID can be used for ACK/NAK
tracking.

**Key `sendText` parameters (verified):**

| Parameter       | Default                     | Notes                                         |
| --------------- | --------------------------- | --------------------------------------------- |
| `text`          | (required)                  | String message body                           |
| `destinationId` | `BROADCAST_ADDR` (`"^all"`) | Target node ID or broadcast                   |
| `wantAck`       | `False`                     | Request acknowledgment                        |
| `wantResponse`  | `False`                     | Request response                              |
| `onResponse`    | `None`                      | Callback for response/ACK                     |
| `channelIndex`  | `0`                         | Radio channel index                           |
| `portNum`       | `TEXT_MESSAGE_APP`          | Protobuf portnum                              |
| `replyId`       | `None`                      | Reference to previous packet ID for threading |
| `hopLimit`      | `None`                      | Max hops (None = firmware default)            |

### 5.2 Real mtjk `sendData` Return Value

`sendData` is the lower-level send that `sendText` wraps:

```python
def sendData(self, data, destinationId, portNum=TEXT_MESSAGE_APP,
             wantAck=False, wantResponse=False, onResponse=None,
             channelIndex=0, hopLimit=None, replyId=None):
    return self._send_pipeline.sendData(data, destinationId, portNum, ...)
```

Both `sendText` and `sendData` return the same type:
`mesh_pb2.MeshPacket` with populated `id`. The `id` is assigned by
`_send_pipeline` before transmission.

### 5.3 sendText/sendData Uncertainty

The following aspects have **not** been verified with hardware captures:

1. **ID assignment timing.** Whether `id` is assigned client-side before
   transmission or firmware-confirmed after over-the-air delivery.
2. **`onResponse` callback behavior.** The mechanism for receiving ACK/NAK
   responses has been identified in source but not tested against real
   hardware.
3. **`hopLimit` default.** When `hopLimit=None`, the firmware's default
   hop limit is used. The exact default value depends on firmware version.
4. **Packet ID uniqueness.** Whether IDs are monotonic, sequential, or
   have collision risk across reconnections.

### 5.4 MMRelay Send Return

MMRelay's direct protobuf send path (`_sendPacket`) also returns the sent
`MeshPacket` protobuf with `id` populated. A fresh ID is generated via
`interface._generatePacketId()`.

### 5.5 Implications for MEDRE Outbound Native Refs

Both mtjk and MMRelay **do return useful packet IDs** from their send APIs.
This means the `FakeMeshtasticClient` returning deterministic sequential IDs
is a reasonable model for what real sends would return. The real adapter's
future `deliver()` should capture the returned packet ID and return it as
`AdapterDeliveryResult.native_message_id`.

Current MEDRE approach (FakeMeshtasticAdapter returns IDs, real adapter
returns `None`) is correct for tranche 1 scaffolding. The real adapter
should be wired to return IDs once the send path is implemented.

### 5.6 Strategy for Real Send

Future real send implementation should:

1. Call `client.sendText(text, channelIndex=channel_index, ...)` on the
   mtjk interface
2. Capture the returned `MeshPacket.id` (int)
3. Return `AdapterDeliveryResult(native_message_id=str(packet_id), ...)`
4. If send raises, catch and translate to `MeshtasticSendError` (session/internal-layer), then normalize to `AdapterSendError`/`AdapterPermanentError` at the runtime boundary
5. Do NOT reimplement MMRelay's protobuf construction approach — use the
   public `sendText` API unless replyId requires protobuf construction

---

## 6. What Remains Unverified

| Area                                               | Status                                       | Risk     |
| -------------------------------------------------- | -------------------------------------------- | -------- |
| Real TCP/serial/BLE connection lifecycle           | Not tested                                   | Medium   |
| mtjk callback packet shapes match fixtures exactly | Not verified with hardware capture           | Medium   |
| PortNum numeric enum values (MEDRE scaffold)       | Verified against protobuf → mismatches found | **High** |
| Startup backlog suppression behavior               | Not verified                                 | Medium   |
| ACK tracking and correlation                       | Not verified                                 | Low      |
| Firmware/radio send ID behavior                    | Not verified                                 | Low      |
| Telemetry/position/nodeinfo payload shapes         | Not verified                                 | Low      |
| Node database / name cache behavior                | Not verified                                 | Medium   |
| Payload size limits and truncation                 | Not verified                                 | Low      |
| Python protobuf `MessageToDict` output shape       | Verified through mtjk source                 | Low      |
| `fromId` population from node DB                   | Verified through mtjk source                 | Low      |

---

## 7. MEDRE Assumptions Supported by Old Behavior

| MEDRE Assumption                                               | Old MMRelay Evidence                                      | Verdict       |
| -------------------------------------------------------------- | --------------------------------------------------------- | ------------- |
| Packet dict has `fromId`, `toId`, `id`, `channel`, `decoded`   | Confirmed — mtjk populates these                          | **Supported** |
| `decoded.portnum` is string name (e.g., `"TEXT_MESSAGE_APP"`)  | Confirmed — `PortNum.Name()` in mtjk                      | **Supported** |
| `decoded.text` carries message body                            | Confirmed — populated by `_on_text_receive`               | **Supported** |
| `decoded.replyId` is int                                       | Confirmed — protobuf `Data.reply_id`                      | **Supported** |
| `to` field used for DM detection (int comparison)              | Confirmed — MMRelay compares against `myInfo.my_node_num` | **Supported** |
| `channel` is int channel index                                 | Confirmed                                                 | **Supported** |
| `id` is int packet ID                                          | Confirmed — protobuf `MeshPacket.id`                      | **Supported** |
| TEXT_MESSAGE_APP packets carry text                            | Confirmed — always RELAY in MMRelay                       | **Supported** |
| ACK packets arrive as ROUTING_APP with `errorReason == "NONE"` | Confirmed — used in health probes                         | **Supported** |
| `from` (numeric) is always present                             | Confirmed                                                 | **Supported** |
| ACK packets are ignorable for normal message flow              | Confirmed — MMRelay only processes ACKs for health probes | **Supported** |

## 8. MEDRE Assumptions Still Tranche-1 Scaffold Only

| MEDRE Assumption                            | Status                                      | Action Required                                              |
| ------------------------------------------- | ------------------------------------------- | ------------------------------------------------------------ |
| Numeric PortNum map values                  | **Scaffold only** — does not match protobuf | Downgrade to explicit scaffold; add optional protobuf import |
| `max_text_bytes=512` / `max_text_chars=512` | **Implemented** — now `max_text_bytes=227` (configurable), `max_text_chars=None`; renderer enforces UTF-8 byte truncation | `feat/meshtastic-byte-budget-rendering` |
| `startup_backlog_suppress_seconds`          | **Scaffold** — not implemented              | Future tranche                                               |
| `connection_type` values                    | **Scaffold** — no real connection code      | Future tranche                                               |
| `sync_timeout_ms`                           | **Scaffold** — no sync operations           | Future tranche                                               |
| Outbound queue pacing                       | **Scaffold** — `process_one` is no-op       | Future tranche                                               |
| Host/port/serial_port config fields         | **Scaffold** — no real connection code      | Future tranche                                               |
| Channel mapping (channel_index → name)      | **Scaffold** — not used                     | Future tranche                                               |

---

## 9. Fixture Provenance

The fixture corpus at `tests/fixtures/meshtastic_packets.py` uses a
four-tier provenance labeling system:

| Label                  | Meaning                                                   | Examples                                                                                                                                   |
| ---------------------- | --------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------ |
| **mtjk-derived**       | Shape verified against mtjk source code                   | `make_text_packet`, `make_symbolic_text_packet`, `make_symbolic_routing_ack_packet`, `make_packet_with_numeric_to`                         |
| **MMRelay-derived**    | Shape observed in old MMRelay codebase                    | `make_mmrelay_style_text_packet`, `make_rxtime_packet`, `make_emoji_reaction_packet`, `make_encrypted_packet`, `make_stale_backlog_packet` |
| **synthetic scaffold** | Invented for MEDRE test coverage; basic structure correct | `make_telemetry_packet`, `make_position_packet`, `make_nodeinfo_packet`, `make_admin_packet`, `make_ack_packet`, `make_plugin_packet`      |
| **unverified**         | Not verified against mtjk or MMRelay; may diverge         | `make_numeric_portnum_packet` (numeric map is scaffold-only)                                                                               |

Each fixture factory includes a **provenance label** in its docstring
indicating the derivation source and confidence level.

### 9.1 Fixture Coverage Matrix

| Packet Type                   | Fixture                            | Provenance         | Portnum Style                   |
| ----------------------------- | ---------------------------------- | ------------------ | ------------------------------- |
| Text message                  | `make_text_packet`                 | mtjk-derived       | Normalized (`text_message`)     |
| Text (symbolic)               | `make_symbolic_text_packet`        | mtjk-derived       | Real (`TEXT_MESSAGE_APP`)       |
| Direct message                | `make_direct_message_packet`       | mtjk-derived       | Normalized                      |
| Channel broadcast             | `make_broadcast_packet`            | mtjk-derived       | Normalized                      |
| Channel message (non-default) | `make_channel_message_packet`      | mtjk-derived       | Normalized                      |
| Reply                         | `make_text_packet_with_reply`      | mtjk-derived       | Normalized                      |
| ACK (scaffold)                | `make_ack_packet`                  | synthetic scaffold | Normalized (`text_message_ack`) |
| ACK (real)                    | `make_symbolic_routing_ack_packet` | mtjk-derived       | Real (`ROUTING_APP`)            |
| Telemetry                     | `make_telemetry_packet`            | synthetic scaffold | Normalized                      |
| Telemetry (symbolic)          | `make_symbolic_telemetry_packet`   | mtjk-derived       | Real (`TELEMETRY_APP`)          |
| Position                      | `make_position_packet`             | synthetic scaffold | Normalized                      |
| Position (symbolic)           | `make_symbolic_position_packet`    | mtjk-derived       | Real (`POSITION_APP`)           |
| Nodeinfo                      | `make_nodeinfo_packet`             | synthetic scaffold | Normalized                      |
| Nodeinfo (symbolic)           | `make_symbolic_nodeinfo_packet`    | mtjk-derived       | Real (`NODEINFO_APP`)           |
| Admin                         | `make_admin_packet`                | synthetic scaffold | Normalized                      |
| Admin (symbolic)              | `make_symbolic_admin_packet`       | mtjk-derived       | Real (`ADMIN_APP`)              |
| Emoji reaction                | `make_emoji_reaction_packet`       | MMRelay-derived    | Real (`TEXT_MESSAGE_APP`)       |
| Encrypted                     | `make_encrypted_packet`            | MMRelay-derived    | Real (`TEXT_MESSAGE_APP`)       |
| rxTime packet                 | `make_rxtime_packet`               | MMRelay-derived    | Real (`TEXT_MESSAGE_APP`)       |
| Stale backlog                 | `make_stale_backlog_packet`        | MMRelay-derived    | Real (`TEXT_MESSAGE_APP`)       |
| MMRelay-style full            | `make_mmrelay_style_text_packet`   | MMRelay-derived    | Real (`TEXT_MESSAGE_APP`)       |
| Numeric to/from               | `make_packet_with_numeric_to/from` | mtjk-derived       | Normalized                      |
| Numeric portnum               | `make_numeric_portnum_packet`      | unverified         | Integer                         |
| Unknown portnum               | `make_unknown_portnum_packet`      | synthetic scaffold | Custom string                   |
| Plugin/custom                 | `make_plugin_packet`               | synthetic scaffold | Custom string                   |
| Minimal (no decoded)          | `make_minimal_packet`              | synthetic scaffold | None                            |

---

## 10. Live Smoke Harness

An optional live smoke test harness exists at
`tests/test_meshtastic_live.py` with a companion runbook at
`docs/runbooks/meshtastic-live-smoke.md`. The harness:

- Is tagged with `pytest.mark.live` and excluded by default
- Requires `MESHTASTIC_CONNECTION_TYPE` env var (and corresponding
  connection params) to activate
- Tests raw mtjk interface connectivity (TCP/serial), not the MEDRE
  adapter's real connection code (which is not yet implemented)
- Verifies `sendText`/`sendData` return packets with IDs
- Verifies pubsub callback reception
- Includes documentation-only tests for unimplemented features
- Does not require hardware/network in default CI

---

_This document was produced by auditing available reference sources
including direct master-branch source inspection. It does not replace
hardware-verified testing. All findings are based on source code analysis,
not live radio captures. See `docs/runbooks/meshtastic-live-smoke.md` for
optional hardware verification procedures._
