# MEDRE — Modular Event-driven Routing Engine

**Pre-release. No stable public API. Not production-ready.** Everything is
subject to change without notice.

MEDRE routes events between independently installed platform adapters through
a codec → renderer → session pipeline with a config-file-first runtime.
First-party providers cover Matrix, Meshtastic, MeshCore, and LXMF; community
providers use the same contracts and ordinary Python packaging.

## Getting Started

Start with the **[Getting Started guide](docs/ops/getting-started.md)**. It walks
through installing MEDRE and adapters directly from Git with uv, running an
offline smoke, and building a real two-way route. It also explains adapter
instances, contexts, prefixes, delivery evidence, and how to add more routes.

For a repeatable deployment, use one uv project containing core and your chosen
providers, and keep its `pyproject.toml` and `uv.lock`. No PyPI release or source
checkout is required. From that project, `uv run --locked medre ...` runs the
CLI, or activate `.venv` once and simply run `medre ...`. See
[installation options](docs/ops/install.md) for a persistent `uv tool install`
straight from Git and a one-off `uvx` alternative. Tool environments do not
inherit providers installed in a different Python environment.

Adapters are separate Git packages. Pin reviewed Git commits before deploying.
Runtime startup discovers installed packages and never downloads adapters.

Already have an environment? Use
[installation and validation](docs/ops/install.md) for source, wheel, and pip
workflows, or [adapter installation](docs/ops/adapter-installation.md) for mixed
Git, index, and local sources. Developers should begin with
[the development environment](docs/dev/environment.md).

Support level: Linux is the verified development host. CI checks adapter-free
core behavior on Linux, macOS, and Windows, plus cross-adapter and SDK contracts
on CPython 3.11–3.14 on Ubuntu. Native service, hardware, and ARM qualification
remain separate from those checks.

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

Core's only optional extra is `dev`. Adapter distributions own their SDK
requirements and SDK-free fakes; install the providers your configuration
references in the same Python environment as MEDRE. Matrix encryption uses
the adapter's `[e2e]` extra. See the
[development environment guide](docs/dev/environment.md) and
[external adapter ownership and CI](docs/dev/external-adapters.md).

Adapter authors can use the [registration reference](docs/dev/adapter-registration.md)
for every hook signature, the public metadata API, and optional fake/SDK
conformance tools. Native schemas and examples belong to the adapter repositories.

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

## Licensing

MEDRE-owned core code is MIT-licensed. Adapter distributions have their own
licenses, and their installed SDKs may impose additional obligations or use
restrictions. The MIT core license does **not** relicense those dependencies.
See [dependency licensing](docs/dev/licensing.md) before distributing a
deployment containing third-party providers.

## Documentation

- [docs/ops/](docs/ops/) — operating MEDRE: install, configuration,
  running, investigation workflows
- [docs/spec/](docs/spec/) — normative specification (the authority for
  runtime behavior)
- [docs/dev/](docs/dev/) — contributing, testing, extending adapters
- [docs/schemas/](docs/schemas/) — machine-readable JSON Schemas
- [docs/changes/](docs/changes/) — change fragments and release notes
- [LICENSE](LICENSE) — MIT (MEDRE-owned core source); [dependency licensing](docs/dev/licensing.md)
