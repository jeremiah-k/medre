# MEDRE

**Modular Event-driven Routing Engine** — bridge conversations across Matrix,
Meshtastic, MeshCore, LXMF, XMPP, Chatmail, and SimpleX Chat. MEDRE owns routing,
persistence, and delivery evidence; each transport is an independently installed
Python adapter. MEDRE began with mesh and LoRa bridges and supports messaging
services through the same adapter, routing, and evidence contracts.

> **Working pre-release — testers welcome; not production-ready.** MEDRE is
> alpha software: expect breaking changes, test with disposable state, and
> back up persistent state before updates. Do not delete an active database to
> work around a schema mismatch: it may contain pending messages and evidence.
>
> Frozen and evolving are per-artifact facts, not a blanket promise. The
> published 0.1.0 schemas are frozen — [docs/schemas/README.md](docs/schemas/README.md)
> and the [freeze manifest](docs/schemas/v0.1.0.freeze.json) record exactly
> which released files are frozen — and the tagged 0.1.0 SQLite catalog is the
> released storage baseline. The current configuration schema still evolves
> additively between releases
> ([current adapter config schema](docs/schemas/current/adapter-config.schema.json)),
> and there is no general database migration: startup accepts the released
> catalog and explicitly allowlisted additive predecessors, refuses other stale
> prerelease layouts without modifying them, and recovery from a rejected
> database is a manual backup and reset
> ([storage policy](docs/spec/storage.md)).

## Install MEDRE

Install [uv](https://docs.astral.sh/uv/getting-started/installation/). The
steps below use the published 0.1.0 core, whose adapter installation defaults
to Git, so install uv and Git. Main already defaults new adapter requests to
package indexes; a pure index installation without Git becomes available with
the first published core release containing source switching.

**1. Install the CLI and example providers.**

```bash
uv tool install medre
medre adapters install matrix meshtastic
medre adapters
```

On Windows, follow the printed uv command after MEDRE exits: the running
executable cannot safely replace itself. For a reproducible, long-running
bridge use a [locked deployment project](docs/ops/install.md#locked-deployment)
instead of floating Git branches.

**2. Verify the pipeline before connecting anything.**

```bash
medre config sample > sample.yaml
medre config check --config sample.yaml
medre smoke --config sample.yaml --storage-path smoke.sqlite3 --json
```

A fake `sent` receipt proves local routing and persistence, **not** remote
delivery, RF reception, or native inbound codec decoding. The generated sample
uses a one-way diagnostic route; build your two-way route in the next step.
The same installation runs live bridges.

**3. Create your first route.**

```bash
medre config bridge --output bridge.yaml
```

Select two adapter instances, map their conversation addresses, and inspect
the generated route plan before saving. The builder never connects to a device
or starts a service. See **[Getting Started](docs/ops/getting-started.md)** for
the complete walkthrough and live setup.

For container deployments, see [Docker](docs/ops/docker.md).

## Adapters

All seven adapters participate in configuration, routing, persistence, diagnostics,
and SDK-free smoke testing. Native features and delivery confirmation vary by
transport; each [transport profile](docs/spec/transport-profiles/README.md)
describes those boundaries.

| Adapter                                                                 | Transport                                              | Setup                                            |
| ----------------------------------------------------------------------- | ------------------------------------------------------ | ------------------------------------------------ |
| [Matrix](https://github.com/jeremiah-k/medre-adapter-matrix)            | Accounts, rooms, optional E2EE                         | [Guide](docs/ops/transport-setup/matrix.md)      |
| [Meshtastic](https://github.com/jeremiah-k/medre-adapter-meshtastic)    | Radio nodes and channels                               | [Guide](docs/ops/transport-setup/meshtastic.md)  |
| [MeshCore](https://github.com/jeremiah-k/medre-adapter-meshcore)        | Companion nodes, contacts and channels                 | [Guide](docs/ops/transport-setup/meshcore.md)    |
| [LXMF](https://github.com/jeremiah-k/medre-adapter-lxmf)                | Reticulum identities and destinations                  | [Guide](docs/ops/transport-setup/lxmf.md)        |
| [XMPP](https://github.com/jeremiah-k/medre-adapter-xmpp)                | Multi-user rooms and direct conversations              | [Guide](docs/ops/transport-setup/xmpp.md)        |
| [Chatmail](https://github.com/jeremiah-k/medre-adapter-chatmail)        | Accepted groups and direct chats through Chatmail core | [Guide](docs/ops/transport-setup/chatmail.md)    |
| [SimpleX Chat](https://github.com/jeremiah-k/medre-adapter-simplexchat) | Allowlisted groups and direct conversations            | [Guide](docs/ops/transport-setup/simplexchat.md) |

Install only the adapters you need, for example:

```bash
medre adapters install xmpp chatmail simplexchat
```

Community adapters can implement the same entry-point contracts; runtime startup
never installs Python packages. Native libraries and account/profile preparation
follow each provider's setup guide. See
[package management](docs/ops/package-management.md) for persistent CLI tools
and [adapter sources](docs/ops/adapter-installation.md) for pinned projects.

## Documentation

| Start here                                     | For                                                              |
| ---------------------------------------------- | ---------------------------------------------------------------- |
| [Getting Started](docs/ops/getting-started.md) | Install, smoke, guided bridge, native connections                |
| [Operator guides](docs/ops/README.md)          | Package management, running, evidence, recovery, troubleshooting |
| [Developer guides](docs/dev/README.md)         | Contributing, testing, authoring adapters                        |
| [Technical specification](docs/spec/README.md) | Normative contracts and supported guarantees                     |

New operators can optionally use [AI-assisted setup](docs/ops/ai-assisted-setup.md).
Real-world testing and reproducible, sanitized bug reports are welcome—never
share credentials, radio keys, identity files, or unredacted databases.

## License

MEDRE-owned core source is [MIT-licensed](LICENSE). Adapter source and its SDKs
retain their respective terms, which may include GPL and the Reticulum License.
See [dependency licensing](docs/dev/licensing.md) before redistribution.
