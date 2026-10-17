# MEDRE

**Modular Event-driven Routing Engine** — route conversations across Matrix,
Meshtastic, MeshCore, and LXMF without coupling the routing core to their SDKs.
Each transport is an independently installed adapter. Community adapters can
use the same public contracts.

> **Working pre-release — testers welcome; not production-ready.** Real transports and recovery paths
> have been exercised, but configuration, event, and database schemas are not
> frozen. Expect breaking changes between Git revisions. Start with disposable
> data, back up persistent state before updating, and be prepared to initialize
> a fresh test database when a schema changes. Never delete an active database
> just to make an upgrade succeed; it may contain pending deliveries and evidence.

## Get started

The fastest way to explore MEDRE is to install it **directly from Git**. No PyPI
publication is required. This example installs the Matrix provider alongside
MEDRE into one persistent uv tool environment:

```bash
uv tool install \
  --with 'git+https://github.com/jeremiah-k/medre-adapter-matrix.git' \
  'git+https://github.com/jeremiah-k/medre.git'

medre version
medre adapters
```

Next, run a **fake, offline** routing test—no homeserver, radio, or credentials
required:

```bash
medre config sample > sample.yaml
medre config check --config sample.yaml
medre smoke --config sample.yaml --storage-path smoke.sqlite3 --json
```

A fake receipt verifies MEDRE's local routing pipeline, **not** remote delivery.
For an ongoing bridge, use a [locked uv deployment](docs/ops/getting-started.md)
with reviewed Git commit pins rather than floating `main` branches. The guide
walks through a real two-way route and protects credentials/state.

## Choose a transport

| Adapter | Connects to | Setup |
| --- | --- | --- |
| [Matrix](https://github.com/jeremiah-k/medre-adapter-matrix) | Matrix accounts and rooms, optional E2EE | [Guide](docs/ops/transport-setup/matrix.md) |
| [Meshtastic](https://github.com/jeremiah-k/medre-adapter-meshtastic) | Radio nodes and channels | [Guide](docs/ops/transport-setup/meshtastic.md) |
| [MeshCore](https://github.com/jeremiah-k/medre-adapter-meshcore) | Companion nodes and contacts | [Guide](docs/ops/transport-setup/meshcore.md) |
| [LXMF](https://github.com/jeremiah-k/medre-adapter-lxmf) | Reticulum/LXMF identities and destinations | [Guide](docs/ops/transport-setup/lxmf.md) |

Adapters can come from Git, local checkouts, or package indexes. They must
share an environment with MEDRE; runtime startup never downloads code.
See [adapter installation](docs/ops/adapter-installation.md) for reproducible
mixed-source deployments and community providers.

## Need help configuring a route?

MEDRE is flexible, and the first configuration can be intimidating.
Use `medre config bridge --output bridge.yaml` for
[guided bridge configuration](docs/ops/guided-configuration.md): choose
instances and contexts, preview both directions, and save a validated draft
without starting adapters.

The [AI-assisted setup guide](docs/ops/ai-assisted-setup.md) explains how an
AI coding assistant such as [OpenCode](https://opencode.ai/) can walk you
through `medre config bridge`, explain its prompts, and help you validate the
saved configuration. You can use free models when available; the assistant is
optional, and MEDRE itself does not require AI.

## Documentation

- **[Getting Started](docs/ops/getting-started.md)** — install from Git,
  offline smoke, and a guided first bridge.
- **[Operator guides](docs/ops/README.md)** — configure, run, inspect, recover,
  and troubleshoot.
- **[Adapter authoring](docs/dev/adapter-authoring.md)** — build an independent
  provider using the shared event and lifecycle contracts.
- **[Technical specification](docs/spec/README.md)** — normative behavior,
  limitations, and evidence definitions.

**Contributions and real-world testing are welcome.** Report reproducible
failures with the exact MEDRE/adapter revisions and sanitized diagnostics; do
not upload access tokens, radio keys, identity files, or unredacted databases.

## License

MEDRE-owned core source is [MIT-licensed](LICENSE). Adapter source and its SDK
may have different terms—including GPL and the Reticulum License. Review
[dependency licensing](docs/dev/licensing.md) before redistribution.
