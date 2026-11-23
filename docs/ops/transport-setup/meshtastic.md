# Meshtastic Transport Setup

Install the provider in the same environment as MEDRE using the
[installation guide](../install.md). Activate a project environment before
invoking CLI tools; persistent uv tools already expose `medre` on PATH.
The provider supplies both its SDK-free fake and its native SDK dependencies.

Setting up and running the MEDRE Meshtastic adapter against a real radio node.

Fake mode is the default and recommended path for all development and testing. Real connectivity modes (TCP, serial) are opt-in for live validation only.

## Before you begin

### Prerequisites

| Requirement          | Details                                                                                      |
| -------------------- | -------------------------------------------------------------------------------------------- |
| Meshtastic node      | A real radio node (e.g. LilyGO T-Beam, Heltec v3, RAK WisBlock) accessible via TCP or serial |
| Python               | 3.11 or later                                                                                |
| Package install      | Install the provider in the MEDRE environment; see [installation](../install.md)             |
| Network access (TCP) | Your machine can reach the node's IP address on port 4403                                    |
| Serial access        | USB cable connecting the node; user in `dialout` group on Linux                              |
| Radio channel        | A channel index (default 0) not used for critical or emergency communications                |

### Node Setup

#### TCP Connectivity (recommended)

Most Meshtastic nodes expose a TCP API when connected via WiFi or Ethernet. Default port: 4403.

1. Power on the node and connect it to your network.
1. Find the node's IP address (check the DHCP table).
1. Verify TCP connectivity:

   ```bash
   nc -zv meshtastic.local 4403
   ```

1. Optionally verify with the Meshtastic CLI supplied by the provider. Use
   the appropriate environment (stop any MEDRE process owning the radio first):

   ```bash
   # Activated deployment project:
   meshtastic --host meshtastic.local --info
   # Persistent uv tool (POSIX):
   "$(uv tool dir)/medre/bin/meshtastic" --host meshtastic.local --info
   ```

   On Windows PowerShell:

   ```powershell
   & (Join-Path (uv tool dir) 'medre/Scripts/meshtastic.exe') --host meshtastic.local --info
   ```

   See [the same-environment SDK instructions](../install.md#run-provider-sdk-tools-in-the-same-environment).

#### Serial Connectivity

Connect the node via USB. The node appears as a serial device.

```bash
# Find the serial port
ls /dev/ttyUSB* /dev/ttyACM*

# Ensure serial port access
sudo usermod -aG dialout $USER
# Log out and back in
```

#### BLE Connectivity

BLE is a supported connection type but has not been validated against real hardware.

```bash
bluetoothctl scan on
# Note the MAC address of your Meshtastic node
```

#### Firmware Compatibility

The provider distribution pins its Meshtastic SDK (`mtjk`) directly in its
package metadata, which also supplies the `meshtastic` CLI used above; the
installed adapter's declared pin is the version authority. If you encounter
protocol errors, upgrade the installed provider and node firmware together,
then rerun the installed-SDK and local validation tiers.

## Set up

Install the adapter:

```bash
medre adapters install meshtastic
```

Save a private `connections.yaml` with one real TCP instance. The common case
needs the connection type and the node host; the default TCP port is shown in
[Options](#options):

```yaml
adapters:
  meshtastic:
    radio-a:
      adapter_kind: real
      connection_type: tcp
      host: meshtastic.local
routes: {}
```

Add another adapter instance (a second radio, or a different transport), then
build routes with [guided configuration](../guided-configuration.md).

### Environment Overrides

Override an existing YAML adapter:

```bash
export MEDRE_ADAPTER__RADIO_A__SERIAL_PORT=/dev/ttyUSB0
```

Create an adapter entirely from env vars:

```bash
export MEDRE_ADAPTER__RADIO_A__TRANSPORT=meshtastic
export MEDRE_ADAPTER__RADIO_A__CONNECTION_TYPE=serial
export MEDRE_ADAPTER__RADIO_A__SERIAL_PORT=/dev/ttyUSB0
```

`MEDRE_MESHTASTIC_*` runtime config variables are unsupported. Use
`MEDRE_ADAPTER__<TOKEN>__<FIELD>`; see
[configuration](../configuration.md#environment-variable-overrides) for the
complete override contract.

## Verify

Check and run the bridge configuration saved by guided configuration:

```bash
medre config check --config bridge.yaml
medre routes plan --config bridge.yaml
medre run --config bridge.yaml
```

A healthy start logs `MeshtasticAdapter radio-a started (mode=tcp)` and
health settles at `healthy`. Configurations missing the fields required for
their connection type are rejected at load — see
[Configuration Validation](#configuration-validation).

### Health States

| State     | Meaning                                                         |
| --------- | --------------------------------------------------------------- |
| `unknown` | Adapter has not started, or has been stopped                    |
| `healthy` | Adapter started successfully; client is connected               |
| `failed`  | Client exists but start did not complete (subscription failure) |

### Delivery Semantics

#### Two-Phase Delivery

Meshtastic delivery is two-phase: `queued` (local queue acceptance) then `sent` (queue drain completed radio send). Neither `queued` nor `sent` means RF confirmation, remote-node receipt, or ACK.

- The queue is in-memory and non-durable across process restart.
- If the process crashes between phases, evidence correctly shows `queued` with no `sent` receipt.
- `sent` means the local node queued the packet for LoRa transmission. Remote receipt is unknown. Fire-and-forget.

#### Outbound Gate

When configured with `outbound_mode = "listen_only"`, outbound deliveries are suppressed before RF transmission. Suppressed deliveries appear as non-retryable adapter failures with detail `outbound suppressed: listen_only mode`.

#### Renderer Truncation

The renderer applies a UTF-8 byte budget of 227 bytes (`max_text_bytes=227` default) for outbound text. Messages exceeding this are truncated.

## Options

| Key                                | Default    | What it changes                                                            | When to touch it                                       |
| ---------------------------------- | ---------- | -------------------------------------------------------------------------- | ------------------------------------------------------ |
| `port`                             | SDK (4403) | TCP port of the node API                                                   | Non-default node port                                  |
| `serial_port`                      | unset      | Serial device path (required for `serial`)                                 | Serial connections                                     |
| `ble_address`                      | unset      | BLE MAC address (required for `ble`)                                       | BLE connections                                        |
| `outbound_mode`                    | `enabled`  | `listen_only` suppresses all outbound sends before RF transmission         | Receive-only monitoring                                |
| `max_text_bytes`                   | `227`      | UTF-8 byte budget for rendered radio text                                  | Longer payloads on LoRa                                |
| `message_delay_seconds`            | `2.2`      | Minimum spacing between outbound messages                                  | Congested channels                                     |
| `tcp_liveness_interval_seconds`    | `60.0`     | Seconds between TCP round-trip liveness probes; `0` disables               | Long-running TCP bridges keep the default              |
| `tcp_liveness_timeout_seconds`     | `30.0`     | Bound for each liveness probe                                              | Slow nodes                                             |
| `startup_packet_drain_seconds`     | `15.0`     | Startup drain for relay-classified backlog; `0` disables                   | Radios that replay queued packets on connect           |
| `startup_backlog_suppress_seconds` | `5.0`      | Post-reconnect stale-`rxTime` tolerance; `0` disables the timestamp cutoff | Clock-synchronized radios or deliberate replay testing |
| `queue_send_max_attempts`          | `3`        | Send attempts per queued item before exhaustion                            | Lossy local links                                      |
| `queue_max_size`                   | `1024`     | Adapter-local outbound queue capacity                                      | Bursty outbound traffic                                |
| `queue_warning_threshold_pct`      | `75.0`     | Utilization entering advisory `warning` pressure                           | Tuning queue alerts                                    |
| `queue_critical_threshold_pct`     | `90.0`     | Utilization degrading adapter health                                       | Tuning queue alerts                                    |
| `origin_label`                     | empty      | Prefix identifying this origin on the other side                           | Labeling in mixed bridges                              |
| `relay_prefix`                     | unset      | Custom rendering template for relayed text                                 | Custom presentation                                    |

### Configuration Validation

`MeshtasticConfig.validate()` enforces:

| Connection type | Required fields             |
| --------------- | --------------------------- |
| `fake`          | `adapter_id` only           |
| `tcp`           | `adapter_id`, `host`        |
| `serial`        | `adapter_id`, `serial_port` |
| `ble`           | `adapter_id`, `ble_address` |

Invalid configurations raise `MeshtasticConfigError` before any connection attempt.

The startup drain and reconnect receive watermark behavior behind
`startup_packet_drain_seconds` and `startup_backlog_suppress_seconds` is
described in the
[Meshtastic internals page](../../dev/transport-internals/meshtastic.md).

## Troubleshooting

Protocol errors after a firmware update point at the SDK pin; see
[Firmware Compatibility](#firmware-compatibility). Suppressed outbound
deliveries with detail `outbound suppressed: listen_only mode` are the
[outbound gate](#outbound-gate) working as configured. A healthy adapter
proves only the MEDRE-to-local-radio API round trip, never remote mesh peer
reachability.

### Known Limitations

1. **Fire-and-forget delivery.** `sent` means local node acceptance. No remote receipt confirmation.
2. **In-memory queue is non-durable.** Process crash loses queued items. Receipts in SQLite survive.
3. **Recovery is local-client supervision, not peer reachability.** TCP liveness proves the MEDRE↔local-radio API round trip only. A healthy adapter does not imply that any remote mesh peer is reachable.
4. **No ACK confirmation for broadcast sends.** Local send acceptance is not remote receipt confirmation.
5. **BLE not validated.** BLE connectivity is implemented but not exercised against real hardware.
6. **Single-channel operation.** Current validation covers text on a single channel index only.
7. **No inbound pubsub delivery proven at Docker level.** meshtasticd simulation mode may not relay packets between TCP clients.

### Sharing a room with MMRelay

[MMRelay](https://github.com/jeremiah-k/meshtastic-matrix-relay) can share a
Matrix room with a MEDRE deployment that owns its own mesh: MEDRE needs only
the room in the Matrix adapter's `room_allowlist`, and the MMRelay operator
must trust the MEDRE bot's Matrix user ID on their side. Keep one relay per
radio mesh: two relays attached to the same mesh while sharing a room relay
each other's messages back until the mesh and the room flood. See
[sharing a room with MMRelay](../shared-rooms-with-mmrelay.md) for what each
side sets, verification, privacy notes, and account migration.

## Reference

- Connection mode construction, manual adapter wiring, startup and shutdown
  sequences, queue diagnostics, and packet classification:
  [Meshtastic transport internals](../../dev/transport-internals/meshtastic.md)
- [guided configuration](../guided-configuration.md) — the route builder used
  to connect two adapters

### See Also

- [live-validation/meshtastic.md](../../dev/live-validation/meshtastic.md) — live smoke test procedures
- [diagnostics-and-evidence.md](../diagnostics-and-evidence.md) — evidence provenance and bundle collection
- [recovery-and-replay.md](../recovery-and-replay.md) — crash recovery and replay
