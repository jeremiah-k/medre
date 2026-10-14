# MEDRE — Modular Event-driven Routing Engine

**Pre-release. No stable public API. Not production-ready.** Everything is
subject to change without notice.

MEDRE routes events between transport adapters (Matrix, Meshtastic,
MeshCore, LXMF) through a codec → renderer → session pipeline with a
config-file-first runtime.

## First Steps

From a source checkout, use [uv](https://docs.astral.sh/uv/getting-started/installation/)
to install the committed core graph and the sample bridge providers. This
fake-adapter smoke path needs no credentials, native SDKs, or Docker:

```bash
uv sync --locked          # core dependencies and an editable MEDRE install
# Pre-release providers for the sample; pin reviewed commit refs for repeatability.
uv pip install --no-deps \
  'git+https://github.com/jeremiah-k/medre-adapter-matrix.git@main' \
  'git+https://github.com/jeremiah-k/medre-adapter-meshtastic.git@main'
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

Support level: Linux is the verified development host. CI is configured for
adapter-free core checks on Linux, macOS, and Windows, plus cross-adapter and
SDK contracts on CPython 3.11–3.14 on Ubuntu. Native service, hardware, and ARM
qualification remain separate from those checks.

## Choosing and Installing Transports

Transport adapters are **independent Python distributions**. They can be
installed from a package index (if published), a Git repository, or a local
checkout. MEDRE discovers installed `medre.adapters` entry points, regardless
of where the distribution came from. Runtime startup never downloads packages.

| Transport  | Distribution (when published)          | Source repository                                             | Prerequisites                         |
| ---------- | -------------------------------------- | ------------------------------------------------------------- | ------------------------------------- |
| Matrix     | `medre-adapter-matrix` (E2EE: `[e2e]`) | [Git](https://github.com/jeremiah-k/medre-adapter-matrix)     | Homeserver, bot account, access token |
| Meshtastic | `medre-adapter-meshtastic`             | [Git](https://github.com/jeremiah-k/medre-adapter-meshtastic) | Serial/TCP node                       |
| MeshCore   | `medre-adapter-meshcore`               | [Git](https://github.com/jeremiah-k/medre-adapter-meshcore)   | TCP/serial/BLE companion              |
| LXMF       | `medre-adapter-lxmf`                   | [Git](https://github.com/jeremiah-k/medre-adapter-lxmf)       | Reticulum and identity file           |

Create a deployment project with dependencies and pinned Git/registry sources,
then resolve/lock it with `uv` (or use pip direct Git URL requirements). See
**[adapter installation: PyPI, Git, and local
sources](docs/ops/adapter-installation.md)** for complete examples and
reproducible deployment guidance. For a checked-out package, `python -m pip
install -e ../medre-adapter-matrix` is also supported in an existing MEDRE
environment.

```bash
medre adapters check --config my-bridge.yaml  # requires only package metadata
medre config check --config my-bridge.yaml    # validates adapter configuration
medre run --config my-bridge.yaml
```

Temporary core SDK extras remain for older CI/development workflows while
adapter package ownership is finalized; they **do not** install the external
adapter distributions. For new deployments use the adapter distributions as
the dependency authority, and do not combine mismatched SDK pins. See the
[development environment guide](docs/dev/environment.md).

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
