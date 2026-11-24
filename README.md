# MEDRE

Bridge conversations across Matrix, Meshtastic, MeshCore, LXMF, XMPP, Chatmail,
and SimpleX Chat. Connect rooms, chats, and radio channels with configurable
one-way or two-way routes. Independent adapters let you install just the
networks you need.

## Install MEDRE

Install [uv](https://docs.astral.sh/uv/getting-started/installation/) and MEDRE.

```bash
uv tool install medre
```

Matrix requires Python 3.12 or later. Use `uv tool install --python 3.12 medre`
for that adapter. To add the Matrix and Meshtastic adapters:

```bash
medre adapters install matrix meshtastic
```

See [installation options](docs/ops/install.md) for interpreter and platform
setup.

## Run a bridge

Use [Getting Started](docs/ops/getting-started.md#3-connect-your-accounts-or-devices)
to connect your accounts or devices and create `bridge.yaml`. Then run:

```bash
medre config check --config bridge.yaml
medre run --config bridge.yaml
```

Leave MEDRE running to keep the bridge connected. Press **Ctrl+C** to stop it.
For container deployments, see [Docker](docs/ops/docker.md).

### Optional: try an offline demo

```bash
medre config sample > sample.yaml
medre config check --config sample.yaml
medre smoke --config sample.yaml --storage-path smoke.sqlite3
```

The sample sends a test message through a one-way route using simulated adapters.
It runs locally without accounts or radios.

## Adapters

Use each adapter's setup guide to prepare the connection. The
[transport profiles](docs/spec/transport-profiles/README.md) describe supported
features.

| Adapter                                                                 | Transport                                              | Setup                                            |
| ----------------------------------------------------------------------- | ------------------------------------------------------ | ------------------------------------------------ |
| [Matrix](https://github.com/jeremiah-k/medre-adapter-matrix)            | Accounts, rooms, optional E2EE                         | [Guide](docs/ops/transport-setup/matrix.md)      |
| [Meshtastic](https://github.com/jeremiah-k/medre-adapter-meshtastic)    | Radio nodes and channels                               | [Guide](docs/ops/transport-setup/meshtastic.md)  |
| [MeshCore](https://github.com/jeremiah-k/medre-adapter-meshcore)        | Companion nodes, contacts and channels                 | [Guide](docs/ops/transport-setup/meshcore.md)    |
| [LXMF](https://github.com/jeremiah-k/medre-adapter-lxmf)                | Reticulum identities and destinations                  | [Guide](docs/ops/transport-setup/lxmf.md)        |
| [XMPP](https://github.com/jeremiah-k/medre-adapter-xmpp)                | Multi-user rooms and direct conversations              | [Guide](docs/ops/transport-setup/xmpp.md)        |
| [Chatmail](https://github.com/jeremiah-k/medre-adapter-chatmail)        | Accepted groups and direct chats through Chatmail core | [Guide](docs/ops/transport-setup/chatmail.md)    |
| [SimpleX Chat](https://github.com/jeremiah-k/medre-adapter-simplexchat) | Allowlisted groups and direct conversations            | [Guide](docs/ops/transport-setup/simplexchat.md) |

For more adapters, package updates, or Git and local sources, see
[package management](docs/ops/package-management.md) and
[adapter installation](docs/ops/adapter-installation.md).

## Documentation

| Start here                                     | For                                           |
| ---------------------------------------------- | --------------------------------------------- |
| [Getting Started](docs/ops/getting-started.md) | Install MEDRE and connect your first networks |
| [Operator guides](docs/ops/README.md)          | Run, update, and troubleshoot a bridge        |
| [Developer guides](docs/dev/README.md)         | Contribute, test, and build adapters          |
| [Technical specification](docs/spec/README.md) | Event formats, routing, and storage contracts |

## Community and testing

Join [#medre:matrix.org](https://matrix.to/#/#medre:matrix.org) for setup help,
development discussion, and testing feedback. Try MEDRE with your accounts and
hardware, share what works, and help improve the adapters and documentation.

[Report issues](https://github.com/jeremiah-k/medre/issues) with your installed
versions, the networks involved, and relevant sanitized logs.

## License

MEDRE-owned core source is [MIT-licensed](LICENSE). Adapter source and its SDKs
retain their respective terms, which may include GPL and the Reticulum License.
See [dependency licensing](docs/dev/licensing.md) before redistribution.
