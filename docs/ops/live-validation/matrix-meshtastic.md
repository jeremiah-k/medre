# Matrix ↔ Meshtastic Live Validation

End-to-end bring-up procedure for a cross-transport bridge between Matrix and
Meshtastic using real hardware. This guide wires a Matrix homeserver adapter to
a Meshtastic serial radio with a bidirectional route, validates both directions,
and explains what success means at each step.

## Prerequisites

| Requirement        | Details                                                                     |
| ------------------ | --------------------------------------------------------------------------- |
| Matrix homeserver  | Synapse or Conduit reachable over the network (localhost is fine)           |
| Matrix bot account | Dedicated user, not your personal account                                   |
| Meshtastic radio   | Connected via USB-serial (`/dev/ttyACM0` typical)                           |
| Python             | 3.11 or later                                                               |
| Package install    | `pip install ".[matrix,meshtastic]"`                                        |
| Serial port access | User must have read/write on the tty device (e.g. `dialout` group on Linux) |

## Auth

Before starting the runtime, obtain a Matrix access token. The recommended way
is the built-in auth command:

```bash
medre adapter matrix auth login --homeserver https://matrix.example.com --user @bot:example.com
```

This performs an interactive login against the homeserver and stores the token
in the Matrix sidecar JSON file. The command prompts for the password securely
and keeps the token out of terminal output.

Alternatively, extract a token via `curl` against the Synapse login API or copy
it from Element's Settings → Help and About pane. Regardless of method, never
commit the token to version control.

## Configuration

Copy the example config to a working location and fill in your real values:

```bash
cp examples/configs/live-matrix-meshtastic.toml /tmp/medre-live.toml
```

Edit `/tmp/medre-live.toml` and populate the following fields.

### Matrix adapter

```toml
[adapters.matrix.matrix]
homeserver = "https://matrix.example.com"  # your homeserver URL
user_id = "@mesh-bot:example.com"           # bot user ID
access_token = ""                           # token from medre adapter matrix auth login
room_allowlist = ["!room:example.com"]      # Matrix rooms to bridge
encryption_mode = "plaintext"
```

Set `room_allowlist` to the room IDs the adapter should monitor. Room IDs use
the `!opaque:server` format — for example `!abc123:example.com`. Room aliases
(`#name:server`) will not work.

### Meshtastic adapter

```toml
[adapters.meshtastic.radio]
connection_type = "serial"
serial_port = "/dev/ttyACM0"
meshnet_name = "medre-radio"
```

### Route targeting fields

Routes use four targeting fields to select source and destination endpoints:

- **`source_room`** — Matrix room ID where the forward leg listens (`!opaque:server`).
- **`dest_room`** — Matrix room ID for reverse-leg delivery.
- **`source_channel`** — Meshtastic channel index for the reverse leg's source.
- **`dest_channel`** — Meshtastic channel index for the forward leg's destination.

Channel indexes are strings. The default Meshtastic channel is `"0"`.

```toml
[[routes]]
from_adapter = "bridge"
to_adapter = "radio"
source_room = "!room:example.com"
dest_room = "!room:example.com"
source_channel = "0"
dest_channel = "0"
```

The example config uses a single bidirectional route that expands into two
legs:

```toml
[routes.matrix_radio_bridge]
source_adapters = ["matrix"]
dest_adapters = ["radio"]
directionality = "bidirectional"
source_room = "!room:example.com"
dest_channel = "0"
```

## Runtime

Start the bridge with the populated config:

```bash
medre run --config /tmp/medre-live.toml
```

To capture a shutdown snapshot for later inspection, provide a path argument
after `--snapshot-on-shutdown`:

```bash
medre run --config /tmp/medre-live.toml --snapshot-on-shutdown /tmp/medre-live-snapshot.json
```

### Expected startup output

```text
INFO  medre  PipelineRunner started
INFO  medre  MatrixAdapter matrix started
INFO  medre  MeshtasticAdapter radio started
INFO  medre  live-matrix-meshtastic running — awaiting shutdown signal
```

Press Ctrl-C to stop. The runtime drains adapters for up to
`shutdown_timeout_seconds` (default 30) then exits.

## Diagnostics

To check adapter health without keeping a long-running process, use the
diagnostics subcommand with `--refresh-health`:

```bash
medre diagnostics --refresh-health --config /tmp/medre-live.toml
```

The `--refresh-health` flag starts a **short-lived** runtime, probes each adapter's health endpoint, prints a summary table, and exits. It **does not require** an already-running runtime — it brings up its own temporary instance.

## Verification

After the bridge has processed messages, inspect delivery evidence:

```bash
medre inspect receipts
```

This lists receipt records showing which events were routed and their terminal
status.

To drill into a specific event's timeline:

```bash
medre inspect event <event-id>
```

### Interpreting sent/success

A `sent` or `success` status means the **local adapter** or **radio** accepted
the packet — the Matrix sidecar sent it to the homeserver API, or the local
Meshtastic radio acknowledged the mesh broadcast. It does **not** confirm
that a remote node received the packet over the air or that a remote Matrix
user saw the message. Treat these statuses as local acceptance only.

## Directional Semantics

### Matrix → Meshtastic (primary path)

The **primary** and **first** direction to validate is Matrix → Meshtastic.
This path exercises the well-tested Matrix inbound pipeline: the adapter
receives a message from the allowlisted room, the codec renders it, and the
Meshtastic adapter delivers it to the radio. Validate this direction first
by sending a message in the Matrix room and confirming it appears on the
Meshtastic device.

### Meshtastic → Matrix (higher risk / unproven)

The reverse direction — Meshtastic → Matrix — is considered **higher risk** and
relatively **unproven** compared to the forward path. The Meshtastic inbound
path depends on serial event reliability, mesh delivery timing, and radio
configuration in ways that have seen less production exposure. Test this
direction separately and expect rough edges.

## What Success Means

A successful bring-up produces:

1. **Auth** — `medre adapter matrix auth login` completes and the token is
   stored.
2. **Startup** — `medre run --config /tmp/medre-live.toml` starts without
   errors, both adapters report healthy.
3. **Forward direction** — A Matrix message appears on the Meshtastic radio.
4. **Reverse direction** — A Meshtastic textMessage appears in the Matrix room.
5. **Evidence** — `medre inspect receipts` shows `sent`/`success` for routed
   events. Remember: this confirms local adapter or radio acceptance, not
   remote delivery confirmation.

## Deterministic Operational Harness

Before live validation, run the deterministic operational test harness. These
tests exercise the full Matrix ↔ Meshtastic rendering, codec, queue, lifecycle,
and capability paths using fake adapters — no homeserver or radio required.

```bash
# 48 tests covering bidirectional flow, relations, loop prevention,
# queue backpressure, byte-budget truncation, failure classification,
# adapter lifecycle, capability decisions, and cross-platform reactions
pytest tests/operational/test_matrix_meshtastic_path.py -v
```

All tests must pass before proceeding to live bring-up. They complete in under
3 seconds on commodity hardware.

## See Also

- [examples/configs/live-matrix-meshtastic.toml](../../../examples/configs/live-matrix-meshtastic.toml) — canonical real-device bridge config
- [transport-setup/matrix.md](../transport-setup/matrix.md) — Matrix transport setup guide
- [transport-setup/meshtastic.md](../transport-setup/meshtastic.md) — Meshtastic transport setup guide
- [diagnostics-and-evidence.md](../diagnostics-and-evidence.md) — evidence provenance and bundle collection
