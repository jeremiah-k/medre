# MEDRE — Modular Event-driven Routing Engine

**Pre-release. No stable public API. Not production-ready.** Everything is
subject to change without notice.

MEDRE routes events between transport adapters (Matrix, Meshtastic,
MeshCore, LXMF) through a codec → renderer → session pipeline with a
config-file-first runtime.

## First Steps

One path from a clean machine to a verified core install. It needs no
credentials, no optional SDKs, and no Docker; pip fetches the two core
dependencies (msgspec, PyYAML) from PyPI:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e .          # prerelease: install from a source checkout

medre version             # version, Python, platform
medre paths               # resolved config/state/data/log directories
medre adapters            # transport inventory; fake adapters need no SDKs

medre config sample > /tmp/medre-sample.yaml
medre config check --config /tmp/medre-sample.yaml
medre smoke --config /tmp/medre-sample.yaml --json
```

`medre config check` validates a config file given by `--config` (or found
via discovery); it does not read a config from stdin. The sample config uses
fake adapters only — it proves the pipeline and storage, not any transport.
Expected output, the persistent-storage variant, and troubleshooting:
[docs/ops/install.md](docs/ops/install.md).

To verify a built artifact end to end — wheel build, clean core-only
install, installed-package proof — run, from a source checkout:

```bash
python scripts/check_installed_package.py
```

The script lives in the source repository and is not part of the installed
wheel.

Support level: MEDRE is developed and tested on Linux. CI runs the test
suite on CPython 3.11–3.14 on Ubuntu runners; local validation in this
buildout happened on a Linux workstation. Other hosts (Windows, macOS, ARM)
are unverified — not excluded. CI checks the pipeline against pinned SDK
contracts; it is not radio interoperability testing.

## Choosing a Transport

Optional extras add real connectivity. Exact SDK pins live in
`pyproject.toml`; install SDKs only through these extras — an unpinned
`pip install` of a transport SDK is not a supported install path and can
drift from the versions MEDRE is tested against. The transports differ in
prerequisites and guarantees; there is no feature parity between them.

| Transport  | Install (source checkout)                            | You need                                    | Setup guide                                                                      |
| ---------- | ---------------------------------------------------- | ------------------------------------------- | -------------------------------------------------------------------------------- |
| Matrix     | `pip install -e ".[matrix]"` (E2EE: `.[matrix-e2e]`) | Homeserver, bot account, access token       | [docs/ops/transport-setup/matrix.md](docs/ops/transport-setup/matrix.md)         |
| Meshtastic | `pip install -e ".[meshtastic]"`                     | Radio node over serial or TCP               | [docs/ops/transport-setup/meshtastic.md](docs/ops/transport-setup/meshtastic.md) |
| MeshCore   | `pip install -e ".[meshcore]"`                       | Companion node over TCP, serial, or BLE     | [docs/ops/transport-setup/meshcore.md](docs/ops/transport-setup/meshcore.md)     |
| LXMF       | `pip install -e ".[lxmf]"`                           | Reticulum instance and a node identity file | [docs/ops/transport-setup/lxmf.md](docs/ops/transport-setup/lxmf.md)             |

What a `sent` receipt means per transport is in
[docs/ops/running-medre.md](docs/ops/running-medre.md); per-transport
validation status and dated live records are in
[docs/ops/live-validation/](docs/ops/live-validation/).

## Running and Operating

```bash
medre run --config my-bridge.yaml
```

- Investigate a run (read-only, needs only the database path):
  `medre inspect event <event_id> --storage-path <db>` — workflow in
  [docs/ops/operator-workflows.md](docs/ops/operator-workflows.md).
- List unresolved deliveries: `medre recover --storage-path <db>`, paged
  with `--limit` and `--cursor`, optionally bounded by `--since` (which
  requires an explicit UTC offset).
- Re-deliver stored events: preview with
  `medre replay --mode dry_run --config my-bridge.yaml` (no side effects),
  then `--mode best_effort`. Replay requires `--config`; it has no
  `--storage-path` argument.

Credentials and identities stay private: Matrix tokens, native node keys,
and Reticulum identity files are secrets. Sanitized examples live in
`examples/configs/`.

## Documentation

- [docs/ops/](docs/ops/) — operating MEDRE: install, configuration,
  running, investigation workflows
- [docs/spec/](docs/spec/) — normative specification (the authority for
  runtime behavior)
- [docs/dev/](docs/dev/) — contributing, testing, extending adapters
- [docs/schemas/](docs/schemas/) — machine-readable JSON Schemas
- [docs/changes/](docs/changes/) — change fragments and release notes
- [LICENSE](LICENSE) — GPL-3.0-or-later
