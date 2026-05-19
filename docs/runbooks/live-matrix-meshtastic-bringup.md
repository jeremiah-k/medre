# Live Matrix ↔ Meshtastic Bridge Bring-Up Runbook

> Document version: 1
> Last updated: 2026-05-17
> Status: Controlled manual smoke — not unattended production

This runbook walks through bringing up a live bridge between a real Matrix
room and a real Meshtastic radio channel. The operator watches logs and
verifies results manually. This is a smoke test, not a production deployment.

## 1. What This Does

This procedure bridges a **real Matrix room** to a **real Meshtastic radio
channel** via the MEDRE runtime. Both adapters are live — no fakes.

Key points:

- **Controlled live smoke test.** The operator starts the runtime, watches
  logs, sends test messages, and verifies delivery manually. This is not
  unattended.
- **Matrix → Meshtastic is the primary path.** The Matrix adapter has been
  proven separately in live smoke tests (see
  `docs/runbooks/matrix-live-smoke.md`). Outbound delivery through the
  pipeline is well-tested in unit tests.
- **Meshtastic → Matrix is higher risk.** Meshtastic inbound callback
  reliability is unproven (see `docs/architecture/alpha-readiness-gaps.md`).
  The reverse path may not work. Document what you observe regardless of
  outcome.
- **Use throwaway resources only.** Create a throwaway Matrix room and select
  a non-critical Meshtastic channel. Test messages are visible to all
  participants on both sides.
- **Matrix auto-join.** The Matrix bot automatically joins rooms that appear in
  route targeting fields (`source_room`, `dest_room`, or `channel_room_map`
  values) and any rooms listed in `auto_join_rooms` on the adapter config.
  Invitations to rooms not referenced by routes or the auto-join list are
  ignored.
- **Unmapped Meshtastic channels are dropped.** Packets arriving on a
  Meshtastic channel that has no matching route are silently discarded. They
  are not broadcast or relayed anywhere.

## 2. Prerequisites

| Requirement                              | How to verify                                                                        |
| ---------------------------------------- | ------------------------------------------------------------------------------------ |
| Python 3.11+                             | `python3 --version`                                                                  |
| medre installed with both transports     | `pip install -e ".[matrix,meshtastic,dev]"`                                          |
| Matrix account with access token         | Obtain via Element → Settings → Help & About, or `/_matrix/client/v3/login` endpoint |
| Meshtastic radio node accessible         | Serial (`/dev/ttyACM0` or `/dev/ttyUSB0`) or TCP (`meshtastic.local:4403`)           |
| Throwaway Matrix room created            | Bot user invited and joined                                                          |
| Non-critical Meshtastic channel selected | Not used for emergency or critical communications                                    |

Optional but recommended:

- A second Meshtastic node or the Meshtastic phone app (for testing
  Meshtastic → Matrix direction).
- A separate terminal for running diagnostic commands.

## 3. Edit Config

Copy the live bridge config template and edit it with your credentials and
connection details:

```bash
cp examples/configs/live-matrix-meshtastic.toml /tmp/medre-live.toml
```

### Authenticate with Matrix (auth-first)

Before editing the config manually, use the auth CLI to obtain and store a
Matrix access token:

```bash
medre adapter matrix auth login \
  --config /tmp/medre-live.toml \
  --adapter-id matrix \
  --homeserver https://matrix.example.com \
  --user @bot:example.com
```

This opens an interactive login flow against the homeserver, writes the
resulting `homeserver`, `user_id`, and `access_token` directly into the config
file, and does **not** print the token to the terminal. After this step, all
credential fields in `[adapters.matrix.matrix]` are populated. You only need to
edit the remaining fields: `room_allowlist`, route targeting fields
(`source_room`, `dest_room`, `source_channel`, `dest_channel`), serial/TCP
connection details for the Meshtastic adapter, and the channel index.

If the template does not exist, create one from scratch using
`medre config sample` and modify it, or use the following as a starting
point:

```bash
cat > /tmp/medre-live.toml <<'EOF'
[runtime]
name = "live-bridge"
shutdown_timeout_seconds = 10

[logging]
level = "INFO"
format = "text"

[storage]
backend = "sqlite"
path = "/tmp/medre-live.sqlite"

[adapters.matrix.matrix]
enabled = true
adapter_kind = "real"
homeserver = "https://matrix.example.com"   # populated by medre adapter matrix auth login
user_id = "@bot:example.com"                  # populated by medre adapter matrix auth login
access_token = ""                             # populated by medre adapter matrix auth login — treat as a secret
room_allowlist = ["!room:example.com"] # FILL IN — your throwaway room
encryption_mode = "plaintext"

[adapters.meshtastic.radio]
enabled = true
adapter_kind = "real"
connection_type = "serial"             # or "tcp"
serial_port = "/dev/ttyACM0"           # FILL IN — check with ls /dev/ttyACM* /dev/ttyUSB*
# host = "meshtastic.local"           # uncomment for TCP
# port = 4403                         # uncomment for TCP
meshnet_name = "live-bridge-test"

# Routes — two unidirectional routes form the full bridge
# Each route uses explicit targeting fields to select source and dest:
#   source_room / dest_room        — Matrix room IDs (!opaque:server)
#   source_channel / dest_channel  — Meshtastic channel indexes as strings

[routes.matrix_to_radio]
source_adapters = ["matrix"]
dest_adapters = ["radio"]
directionality = "source_to_dest"
enabled = true
source_room = "!room:example.com"           # FILL IN — Matrix room to listen on
dest_channel = "0"                          # FILL IN — Meshtastic channel index

[routes.radio_to_matrix]
source_adapters = ["radio"]
dest_adapters = ["matrix"]
directionality = "source_to_dest"
enabled = true
source_channel = "0"                        # FILL IN — Meshtastic channel to listen on
dest_room = "!room:example.com"             # FILL IN — Matrix room to deliver to
EOF
```

Edit the following fields in `/tmp/medre-live.toml` (credential fields are
already populated by `medre adapter matrix auth login`):

### [adapters.matrix.matrix]

> After running `medre adapter matrix auth login`, the `homeserver`, `user_id`, and
> `access_token` fields are already populated. Edit only the fields below.

| Field            | Set to                                                            |
| ---------------- | ----------------------------------------------------------------- |
| `room_allowlist` | List with your throwaway room ID (e.g. `["!abc123:example.com"]`) |

### Routes (targeting fields)

Each route declares explicit targeting fields that select which Matrix room and
Meshtastic channel to use as source and destination:

| Field            | Route             | Set to                                                      |
| ---------------- | ----------------- | ----------------------------------------------------------- |
| `source_room`    | `matrix_to_radio` | Matrix room ID to listen on (e.g. `"!abc123:example.com"`)  |
| `dest_channel`   | `matrix_to_radio` | Meshtastic channel index as string (e.g. `"0"`)             |
| `source_channel` | `radio_to_matrix` | Meshtastic channel index to listen on (e.g. `"0"`)          |
| `dest_room`      | `radio_to_matrix` | Matrix room ID to deliver to (e.g. `"!abc123:example.com"`) |

### [adapters.meshtastic.radio]

**Serial connection** (default):

| Field             | Set to                            |
| ----------------- | --------------------------------- |
| `connection_type` | `"serial"`                        |
| `serial_port`     | Device path (e.g. `/dev/ttyACM0`) |

**TCP connection** (alternative):

| Field             | Set to                                        |
| ----------------- | --------------------------------------------- |
| `connection_type` | `"tcp"`                                       |
| `host`            | Node hostname or IP (e.g. `meshtastic.local`) |
| `port`            | TCP port (default `4403`)                     |
| `serial_port`     | Remove or comment out                         |

## 4. Validate Config

Before starting the runtime, validate the config file:

```bash
medre config check --config /tmp/medre-live.toml
```

**Expected:** `Config valid`

**If you see `"access_token must be non-empty"`:** Fill in the `access_token`
field in the `[adapters.matrix.matrix]` section. The validator correctly
rejects empty tokens for real adapters.

**If you see `"serial_port required"`:** Set `serial_port` to a valid device
path, or switch to TCP by setting `connection_type = "tcp"` and providing
`host`.

```bash
# Also verify routes are declared correctly
medre routes validate --config /tmp/medre-live.toml
```

## 5. Matrix → Meshtastic

This is the primary direction. The Matrix adapter has been validated
separately in live smoke tests.

### Start the runtime

```bash
medre run --config /tmp/medre-live.toml
```

### Watch for adapter startup

Look for these log lines:

```text
Adapter matrix started
Adapter radio started
```

Both adapters must report started before proceeding. If the Meshtastic
adapter fails to start, check the serial/TCP connection (see §8
Troubleshooting).

### Send a test message from Matrix

Open your Matrix client (Element, etc.) and send a text message to the
throwaway room. For example: `"MEDRE live bridge test — Matrix → Meshtastic"`.

Alternatively, use curl:

```bash
curl -s -X PUT \
  -H "Authorization: Bearer YOUR_ACCESS_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"msgtype":"m.text","body":"MEDRE live bridge test — Matrix → Meshtastic"}' \
  "https://matrix.example.com/_matrix/client/v3/rooms/!room:example.com/send/m.room.message/$(date +%s)"
```

### Verify delivery

Watch the medre logs for:

1. **Inbound from Matrix:** Log line showing the message was received from
   the Matrix adapter (event_id starting with `$`).
2. **Routing:** Log line showing the event was routed to the Meshtastic
   adapter.
3. **Delivery receipt:** Log line showing delivery to the radio adapter,
   including the `packet_id`.

### Verify on the radio

Check the Meshtastic radio shows the message. This is a manual step:

- Check the node's screen (if it has one).
- Or connect via the Meshtastic CLI/phone app and observe received messages.
- Or watch serial output if connected via USB.

### Health check (optional, separate terminal)

```bash
medre diagnostics --refresh-health --config /tmp/medre-live.toml
```

This starts a **short-lived runtime** internally — it creates adapters, polls
their health, reports results, and exits. It does **not** require an
already-running medre runtime. Use it standalone or in a separate terminal while
the main runtime is running to get an independent health snapshot.

### What this proves

This proves the **Matrix send → adapter → pipeline → Meshtastic outbound**
path works end-to-end. Radio transmission to remote nodes is fire-and-forget —
`success=True` means the local radio accepted the packet, not that a remote
node received it. See `docs/contracts/36-radio-limitations.md`.

## 6. Meshtastic → Matrix

> **⚠️ Higher risk section.** Meshtastic inbound callback reliability is a
> known gap. This may not work. Document what you observe regardless of
> outcome.

### Send from Meshtastic

Send a text message to the same channel from:

- Another Meshtastic node on the same mesh, or
- The Meshtastic phone app connected to the same node, or
- The Meshtastic CLI: `meshtastic --sendtext "MEDRE bridge test — Meshtastic → Matrix"`

### Watch for inbound processing

Look for these log lines in order:

1. **Inbound packet received:** Log line showing a packet arrived from the
   Meshtastic radio.
2. **Codec decode:** Packet decoded to canonical event format.
3. **Publish:** Event published to the internal event bus.
4. **Route:** Event matched the route and dispatched to the Matrix adapter.
5. **Matrix deliver:** Message sent to the Matrix room (event_id in log).

### Verify on Matrix

Check the throwaway Matrix room for the message. It should appear as a new
message from the bot account.

### If nothing arrives

This is an honest possible outcome. Known gaps:

- Meshtastic inbound callback may not fire reliably.
- Backlog suppression may filter stale packets on startup.
- Channel index mismatch may silently drop packets.

**Document what you observe** — even a negative result is valuable evidence.
Record:

- Whether any log lines appeared for the inbound packet.
- Whether the packet appeared in diagnostics counters.
- The radio firmware version and connection type used.

## 7. Shutdown & Artifacts

### Graceful shutdown

In the terminal running medre:

```text
Ctrl+C
```

Wait for the drain to complete. The runtime logs a shutdown message when all
adapters have stopped.

### Alternative: snapshot on shutdown

```bash
medre run --snapshot-on-shutdown /tmp/medre-live-snapshot.json --config /tmp/medre-live.toml
```

This writes a snapshot to the specified path with runtime counters, capacity
gauges, and route statistics.

### Collect evidence via inspect

After shutdown, use inspect commands to review what happened. These are
read-only and need only `--storage-path`.

```bash
# List all delivery receipts
medre inspect receipts --storage-path /tmp/medre-live.sqlite
```

Expected: JSON array of delivery receipts showing Matrix → Meshtastic
deliveries (and Meshtastic → Matrix if the reverse path worked).

Pick an `event_id` from the receipts and drill into the timeline:

```bash
medre inspect event <event_id> --timeline --storage-path /tmp/medre-live.sqlite
```

The timeline shows every stage the event passed through.

If you need a structured JSON bundle for offline review, use the smoke script
in `--event-id` mode to replay a specific event's artifacts:

```bash
scripts/live-matrix-meshtastic-smoke.sh --event-id <event_id>
```

This replays inspection data for the given event, producing a structured
output suitable for archival or comparison.

### Artifacts summary

| Artifact          | Location                                                           |
| ----------------- | ------------------------------------------------------------------ |
| SQLite database   | `/tmp/medre-live.sqlite`                                           |
| Shutdown snapshot | `/tmp/medre-live-snapshot.json` (if `--snapshot-on-shutdown` used) |
| Inspect output    | stdout (redirect to file)                                          |
| Config file       | `/tmp/medre-live.toml`                                             |

## 8. Troubleshooting

| Symptom                                  | Cause                                | Fix                                                                                                                                                                            |
| ---------------------------------------- | ------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `"access_token must be non-empty"`       | Empty access token in config         | Run `medre adapter matrix auth login --config /tmp/medre-live.toml --adapter-id matrix --homeserver ... --user ...` to populate the token, or fill in `access_token` manually. |
| `"serial_port required"`                 | No serial device path configured     | Check USB connection. Run `ls /dev/ttyACM* /dev/ttyUSB*` to find the device. Set `serial_port` in config.                                                                      |
| Permission denied on serial port         | User not in `dialout` group          | `sudo usermod -aG dialout $USER` then log out and back in.                                                                                                                     |
| `"host is required"`                     | TCP connection type without host     | Set `host` in `[adapters.meshtastic.radio]`, or switch `connection_type` to `"serial"`.                                                                                        |
| Matrix adapter not healthy               | Invalid or expired access token      | Re-run `medre adapter matrix auth login` to obtain a fresh token, or verify token via Element.                                                                                 |
| Radio not responding                     | Connection issue or firmware problem | Check USB cable, verify firmware version, try the Meshtastic CLI tool (`meshtastic --info`).                                                                                   |
| No messages arriving                     | Room allowlist mismatch              | Ensure `room_allowlist` contains the actual room ID (format: `!opaque:server`).                                                                                                |
| Matrix adapter starts but radio fails    | Radio SDK not installed              | Run `pip install -e ".[meshtastic]"`. Verify with `medre adapters`.                                                                                                            |
| Radio adapter starts but Matrix fails    | Matrix SDK not installed             | Run `pip install -e ".[matrix]"`. Verify with `medre adapters`.                                                                                                                |
| Inbound Meshtastic packets not processed | Known gap — callback reliability     | Document observation. Try restarting the runtime. Check firmware version.                                                                                                      |
| Duplicate messages on radio              | Retry policy producing duplicates    | Expected behavior with retry (up to 3 attempts). See `docs/contracts/36-radio-limitations.md`.                                                                                 |
| Health stays `degraded`                  | Reconnect cycle in progress          | Wait for reconnect or check transport availability.                                                                                                                            |

## Scope and Exclusions

This bring-up procedure is explicitly limited to:

- Text messages only. No reactions, edits, media, or attachments.
- A single Matrix room and a single Meshtastic channel.
- Manual observation by a human operator.
- A single runtime session (no reconnect resilience testing).

The following are explicitly **out of scope**:

- E2EE (encrypted Matrix rooms). Use `encryption_mode = "plaintext"` only.
- Sustained load testing or throughput measurement.
- Multi-room or multi-channel configurations.
- Production deployment, credential management, or token rotation.
- Automated monitoring or alerting.
- BLE Meshtastic connections.

See `docs/runbooks/matrix-live-smoke.md` and
`docs/runbooks/meshtastic-live-smoke.md` for per-transport live smoke
procedures that validate each adapter independently.

## 9. Execution Checklist

Use this checklist to track progress through a bring-up session. Each step
must pass before proceeding to the next.

- [ ] **Install.** `pip install -e ".[matrix,meshtastic,dev]"` — verify with
      `medre adapters` showing both SDKs available.
- [ ] **Config check.** `medre config check --config /tmp/medre-live.toml`
      reports `Config valid`.
- [ ] **Auth.** `medre adapter matrix auth login --config /tmp/medre-live.toml
--adapter-id matrix --homeserver ... --user ...` — confirm `access_token`
      is populated in the config file.
- [ ] **Run.** `medre run --config /tmp/medre-live.toml` — both adapters
      report `started` in logs.
- [ ] **Test messages.** Send a Matrix message and verify delivery on the
      radio. Optionally test Meshtastic → Matrix direction.
- [ ] **Shutdown.** `Ctrl+C` — wait for graceful drain. Optionally use
      `--snapshot-on-shutdown`.
- [ ] **Inspect artifacts.** `medre inspect receipts --storage-path
/tmp/medre-live.sqlite` — verify delivery receipts are present. Drill into
      specific events with `medre inspect event <event_id> --timeline`.
