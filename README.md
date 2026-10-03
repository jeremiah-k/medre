# MEDRE — Modular Event-driven Routing Engine

**Pre-release. No stable public API. Not production-ready.** Everything is
subject to change without notice.

MEDRE routes events between transport adapters (Matrix, Meshtastic,
MeshCore, LXMF) through a codec → renderer → session pipeline with a
config-file-first runtime.

## First Steps

From a source checkout, use [uv](https://docs.astral.sh/uv/getting-started/installation/)
to install the committed dependency graph. This core smoke path needs no
credentials, optional SDKs, or Docker:

```bash
uv sync --locked          # core dependencies and an editable MEDRE install
source .venv/bin/activate

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
uv sync --locked --extra dev
uv run --no-sync python scripts/check_installed_package.py
```

The script lives in the source repository and is not part of the installed
wheel.

Support level: MEDRE is developed and tested on Linux. CI runs the test
suite on CPython 3.11–3.14 on Ubuntu runners. Other hosts (Windows, macOS, ARM)
are unverified — not excluded. CI checks the pipeline against pinned SDK
contracts; it is not radio interoperability testing.

## Choosing a Transport

Optional extras add real connectivity. Exact SDK pins live in
`pyproject.toml`; install SDKs only through these extras — an unpinned
`pip install` of a transport SDK is not a supported install path and can
drift from the versions MEDRE is tested against. The transports differ in
prerequisites and guarantees; there is no feature parity between them.

The commands below apply to source checkouts. Each sync selects the complete
extra set for `.venv`; repeat every transport needed by a bridge, and add
`--extra dev` for tests. Built-package and existing pip environments remain
supported; see [installation](docs/ops/install.md) and the
[development environment guide](docs/dev/environment.md).

| Transport  | Install (source checkout)                            | You need                                    | Setup guide                                                                      |
| ---------- | ---------------------------------------------------- | ------------------------------------------- | -------------------------------------------------------------------------------- |
| Matrix     | `uv sync --locked --extra matrix` (E2EE: `--extra matrix-e2e`) | Homeserver, bot account, access token       | [docs/ops/transport-setup/matrix.md](docs/ops/transport-setup/matrix.md)         |
| Meshtastic | `uv sync --locked --extra meshtastic`                     | Radio node over serial or TCP               | [docs/ops/transport-setup/meshtastic.md](docs/ops/transport-setup/meshtastic.md) |
| MeshCore   | `uv sync --locked --extra meshcore`                       | Companion node over TCP, serial, or BLE     | [docs/ops/transport-setup/meshcore.md](docs/ops/transport-setup/meshcore.md)     |
| LXMF       | `uv sync --locked --extra lxmf`                           | Reticulum instance and a node identity file | [docs/ops/transport-setup/lxmf.md](docs/ops/transport-setup/lxmf.md)             |

For example, a Matrix/Meshtastic bridge uses
`uv sync --locked --extra matrix --extra meshtastic`. Activate `.venv` as above
or prefix commands with `uv run --no-sync` after syncing.

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
