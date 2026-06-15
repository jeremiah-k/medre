# MEDRE Example Configurations

Reference YAML configs for common medre setups. Copy one, fill in your
credentials, and run `medre run --config <path>`.

| Config File                               | Purpose                                            | Adapters                                    | Requires Hardware/Network    | Notes                                                                                                                                                              |
| ----------------------------------------- | -------------------------------------------------- | ------------------------------------------- | ---------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `fake-bridge-smoke.yaml`                  | Cross-adapter event routing with zero dependencies | Fake Matrix, Fake Meshtastic, Fake MeshCore | No                           | Start here to validate routing logic in CI or local dev                                                                                                            |
| `fake-multi-adapter.yaml`                 | All four transports as fake adapters               | Fake Matrix, Meshtastic, MeshCore, LXMF     | No                           | Full-coverage dev/CI baseline; no SDK dependencies                                                                                                                 |
| `fake-retry-smoke.yaml`                   | Config-driven retry for transient failures         | Fake Matrix, Fake Meshtastic                | No                           | Demonstrates the `retry` worker and per-route retry policy                                                                                                         |
| `matrix.yaml`                             | Single Matrix adapter (plaintext or E2EE)          | Matrix                                      | Yes (homeserver)             | Copy and adjust for your bot account                                                                                                                               |
| `meshtastic-serial.yaml`                  | Single Meshtastic adapter over USB-serial          | Meshtastic                                  | Yes (radio)                  | Most common setup for a dedicated node                                                                                                                             |
| `live-matrix-meshtastic.yaml`             | Live Matrix ↔ Meshtastic bridge                    | Matrix + Meshtastic                         | Yes (homeserver + radio)     | **Start here for live bring-up.** Canonical real-device bridge config. Routes use explicit targeting: `source_room`, `dest_room`, `source_channel`, `dest_channel` |
| `live-matrix-meshtastic-channel-map.yaml` | Multi-channel Matrix ↔ Meshtastic bridge           | Matrix + Meshtastic                         | Yes (homeserver + radio)     | Maps channels 0-2; extendable to 0-7 via `channel_room_map`                                                                                                        |
| `mixed-matrix-meshtastic.yaml`            | Earlier Matrix ↔ Meshtastic bridge variant         | Matrix + Meshtastic                         | Yes (homeserver + radio)     | **Superseded by `live-matrix-meshtastic.yaml`.** Historical reference only.                                                                                        |
| `docker-matrix-bridge.yaml`               | Real Matrix SDK against Docker Synapse             | Real Matrix, Fake Meshtastic                | Docker Synapse               | SDK-boundary validation; not for direct `medre run`                                                                                                                |
| `docker-meshtastic-bridge.yaml`           | Real Meshtastic SDK against Docker meshtasticd     | Real Meshtastic, Fake Matrix                | Docker meshtasticd           | SDK-boundary validation; tests TCP interface                                                                                                                       |
| `docker-bridge-smoke.yaml`                | Real Matrix + real Meshtastic in Docker            | Real + Fake Matrix, Real + Fake Meshtastic  | Docker Synapse + meshtasticd | Full SDK-boundary smoke; credentials are placeholders                                                                                                              |

## Quick Start

```bash
# 1. No hardware? Validate with fake adapters:
medre run --config examples/configs/fake-bridge-smoke.yaml

# 2. Have a Matrix bot and a Meshtastic radio? Use the live bridge:
cp examples/configs/live-matrix-meshtastic.yaml my-bridge.yaml
# Populate Matrix credentials via the sidecar auth command:
medre adapter matrix auth login --homeserver https://matrix.example.com --user @bot:example.com
# Then edit my-bridge.yaml — fill in room IDs, serial port, channel indexes
medre run --config my-bridge.yaml
```

> **Note:** `medre adapter matrix auth login` performs an interactive login against the
> homeserver and saves credentials to the Matrix sidecar JSON file. Accepted flags
> are `--homeserver`, `--user`, `--password`, and `--password-stdin`. The command
> prompts securely by default and keeps the token out of terminal output. See
> `docs/ops/configuration.md` for full credential handling guidance.

MEDRE uses a boring subset of YAML: explicit mappings and lists only. No
anchors, aliases, merge keys, or custom tags are supported. Quote
values that YAML could misread — Matrix room IDs (`"!room:server"`), MXIDs
(`"@user:server"`), channel IDs where string semantics matter (`"0"`), and
path placeholders like `"{state}/medre.sqlite"`.
