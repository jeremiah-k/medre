# MEDRE Example Configurations

Reference TOML configs for common medre setups. Copy one, fill in your
credentials, and run `medre run --config <path>`.

| Config File | Purpose | Adapters | Requires Hardware/Network | Notes |
|---|---|---|---|---|
| `fake-bridge-smoke.toml` | Cross-adapter event routing with zero dependencies | Fake Matrix, Fake Meshtastic, Fake MeshCore | No | Start here to validate routing logic in CI or local dev |
| `fake-multi-adapter.toml` | All four transports as fake adapters | Fake Matrix, Meshtastic, MeshCore, LXMF | No | Full-coverage dev/CI baseline; no SDK dependencies |
| `fake-retry-smoke.toml` | Config-driven retry for transient failures | Fake Matrix, Fake Meshtastic | No | Demonstrates `[retry]` worker and per-route retry policy |
| `matrix.toml` | Single Matrix adapter (plaintext or E2EE) | Matrix | Yes (homeserver) | Copy and adjust for your bot account |
| `meshtastic-serial.toml` | Single Meshtastic adapter over USB-serial | Meshtastic | Yes (radio) | Most common setup for a dedicated node |
| `live-matrix-meshtastic.toml` | Live Matrix ↔ Meshtastic bridge | Matrix + Meshtastic | Yes (homeserver + radio) | **Start here for live bring-up.** Canonical real-device bridge config |
| `mixed-matrix-meshtastic.toml` | Earlier Matrix ↔ Meshtastic bridge variant | Matrix + Meshtastic | Yes (homeserver + radio) | **Superseded by `live-matrix-meshtastic.toml`.** Retained for backward compatibility |
| `docker-matrix-bridge.toml` | Real Matrix SDK against Docker Synapse | Real Matrix, Fake Meshtastic | Docker Synapse | SDK-boundary validation; not for direct `medre run` |
| `docker-meshtastic-bridge.toml` | Real Meshtastic SDK against Docker meshtasticd | Real Meshtastic, Fake Matrix | Docker meshtasticd | SDK-boundary validation; tests TCP interface |
| `docker-bridge-smoke.toml` | Real Matrix + real Meshtastic in Docker | Real + Fake Matrix, Real + Fake Meshtastic | Docker Synapse + meshtasticd | Full SDK-boundary smoke; credentials are placeholders |

## Quick Start

```bash
# 1. No hardware? Validate with fake adapters:
medre run --config examples/configs/fake-bridge-smoke.toml

# 2. Have a Matrix bot and a Meshtastic radio? Use the live bridge:
cp examples/configs/live-matrix-meshtastic.toml my-bridge.toml
# Edit my-bridge.toml — fill in homeserver, user_id, access_token, room_allowlist
medre run --config my-bridge.toml
```
