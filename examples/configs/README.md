# MEDRE Example Configurations

Reference TOML configs for common medre setups. Copy one, fill in your
credentials, and run `medre run --config <path>`.

| Config File                     | Purpose                                            | Adapters                                    | Requires Hardware/Network    | Notes                                                                                                                                                              |
| ------------------------------- | -------------------------------------------------- | ------------------------------------------- | ---------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `fake-bridge-smoke.toml`        | Cross-adapter event routing with zero dependencies | Fake Matrix, Fake Meshtastic, Fake MeshCore | No                           | Start here to validate routing logic in CI or local dev                                                                                                            |
| `fake-multi-adapter.toml`       | All four transports as fake adapters               | Fake Matrix, Meshtastic, MeshCore, LXMF     | No                           | Full-coverage dev/CI baseline; no SDK dependencies                                                                                                                 |
| `fake-retry-smoke.toml`         | Config-driven retry for transient failures         | Fake Matrix, Fake Meshtastic                | No                           | Demonstrates `[retry]` worker and per-route retry policy                                                                                                           |
| `matrix.toml`                   | Single Matrix adapter (plaintext or E2EE)          | Matrix                                      | Yes (homeserver)             | Copy and adjust for your bot account                                                                                                                               |
| `meshtastic-serial.toml`        | Single Meshtastic adapter over USB-serial          | Meshtastic                                  | Yes (radio)                  | Most common setup for a dedicated node                                                                                                                             |
| `live-matrix-meshtastic.toml`   | Live Matrix ↔ Meshtastic bridge                    | Matrix + Meshtastic                         | Yes (homeserver + radio)     | **Start here for live bring-up.** Canonical real-device bridge config. Routes use explicit targeting: `source_room`, `dest_room`, `source_channel`, `dest_channel` |
| `live-matrix-meshtastic-channel-map.toml` | Multi-channel Matrix ↔ Meshtastic bridge | Matrix + Meshtastic                  | Yes (homeserver + radio)     | Maps channels 0-2; extendable to 0-7 via `channel_room_map` |
| `mixed-matrix-meshtastic.toml`  | Earlier Matrix ↔ Meshtastic bridge variant         | Matrix + Meshtastic                         | Yes (homeserver + radio)     | **Superseded by `live-matrix-meshtastic.toml`.** Historical reference only.                                                                                        |
| `docker-matrix-bridge.toml`     | Real Matrix SDK against Docker Synapse             | Real Matrix, Fake Meshtastic                | Docker Synapse               | SDK-boundary validation; not for direct `medre run`                                                                                                                |
| `docker-meshtastic-bridge.toml` | Real Meshtastic SDK against Docker meshtasticd     | Real Meshtastic, Fake Matrix                | Docker meshtasticd           | SDK-boundary validation; tests TCP interface                                                                                                                       |
| `docker-bridge-smoke.toml`      | Real Matrix + real Meshtastic in Docker            | Real + Fake Matrix, Real + Fake Meshtastic  | Docker Synapse + meshtasticd | Full SDK-boundary smoke; credentials are placeholders                                                                                                              |

## Quick Start

```bash
# 1. No hardware? Validate with fake adapters:
medre run --config examples/configs/fake-bridge-smoke.toml

# 2. Have a Matrix bot and a Meshtastic radio? Use the live bridge:
cp examples/configs/live-matrix-meshtastic.toml my-bridge.toml
# Populate Matrix credentials via sidecar (no --config or --adapter-id flags):
medre adapter matrix auth login --homeserver https://matrix.example.com --user @bot:example.com
# Then edit my-bridge.toml — fill in room IDs, serial port, channel indexes
medre run --config my-bridge.toml
```

> **Note:** `medre adapter matrix auth login` performs an interactive login against the
> homeserver and saves credentials to a sidecar JSON file (not the TOML config).
> It accepts `--homeserver`, `--user`, `--password`, and `--password-stdin` flags.
> It does not accept `--config` or `--adapter-id`. It does
> not print the token to the terminal. See `docs/runbooks/secure-credentials.md`
> for full credential handling guidance.
