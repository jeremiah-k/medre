# Getting Started

MEDRE connects conversations across Matrix, Meshtastic, MeshCore, LXMF, XMPP,
Chatmail and SimpleX Chat. Start with two connections; add more when you need them.
The example below connects Matrix and Meshtastic.

MEDRE is a working pre-release and **not production-ready**. Use test accounts
and chats first. Before updating a bridge, stop it and
[back up its private state](operator-workflows.md#pre-release-storage-reset).

## 1. Install MEDRE

Install [uv](https://docs.astral.sh/uv/getting-started/installation/) and Git,
then run:

```bash
uv tool install 'git+https://github.com/jeremiah-k/medre.git@main'
medre adapters install matrix meshtastic
```

If `medre` is not found, run `uv tool update-shell` and open a new terminal.
On Windows, run the extra installation command MEDRE prints after it exits.
For other networks, replace `matrix meshtastic` with the adapters you want.

## 2. Verify your installation

Run a local check before connecting accounts or radios:

```bash
medre config sample > sample.yaml
medre config check --config sample.yaml
medre smoke --config sample.yaml --storage-path smoke.sqlite3
```

Look for `PASS`. This checks your local setup; it does not send a message to
Matrix or a radio. The sample checks one-way forwarding only; choose both
ways for your real bridge in step 4. Keep the generated files private.

## 3. Connect your accounts or devices

For the Matrix and Meshtastic example, these prompts create your connection
settings. Have your Matrix account details and radio connection ready:

```bash
medre config native --transport matrix --adapter-id bot --output matrix.yaml
medre config native --transport meshtastic --adapter-id node \
  --config matrix.yaml --output connections.yaml
```

The prompts explain which values to enter. Creating the files does not start
the bridge. They may contain credentials: keep them outside version control
and do not share them. If working in a Git project, ignore `matrix.yaml`,
`connections.yaml`, and `bridge.yaml` **before** running these commands
(see [locked deployment](install.md#locked-deployment)).

For setup help or a different pair of networks, choose the guides you need:

| Network      | Setup guide                                                      |
| ------------ | ---------------------------------------------------------------- |
| Matrix       | [Account and room](transport-setup/matrix.md)                    |
| Meshtastic   | [Radio connection and channel](transport-setup/meshtastic.md)    |
| MeshCore     | [Companion radio and conversations](transport-setup/meshcore.md) |
| LXMF         | [Reticulum connection and destination](transport-setup/lxmf.md)  |
| XMPP         | [Account, room or contact](transport-setup/xmpp.md)              |
| Chatmail     | [Account and chats](transport-setup/chatmail.md)                 |
| SimpleX Chat | [Profile and conversations](transport-setup/simplexchat.md)      |

The two commands above are an example for those adapters. Other setup guides
explain how to create the equivalent connection settings for their networks.

## 4. Choose which conversations to connect

```bash
medre config bridge --config connections.yaml --output bridge.yaml
medre config check --config bridge.yaml
```

Choose your two connections and the room, chat or radio channel on each side.
Choose whether messages should travel one way or both ways. Review the summary
before saving. The wizard creates a new file and does not overwrite your input. For advanced
options or existing routes, see [guided configuration](guided-configuration.md).

## 5. Run your bridge

```bash
medre run --config bridge.yaml
```

Send a message from the configured source and verify it appears at the
destination. If you selected **both ways** in step 4, also test the reverse
direction. A one-way route must not forward messages in reverse. Leave MEDRE
running to keep the bridge connected; press **Ctrl+C** to stop it.

MEDRE stores messages and delivery records locally. Automatic message expiry is
not available yet. See [privacy and message storage](message-storage.md) before
using sensitive conversations.

## Keep a bridge running

After the first test works, use a
[locked deployment](install.md#locked-deployment) to keep your installed versions
consistent. See [running MEDRE](running-medre.md) for service setup and monitoring.
These are optional next steps, not prerequisites for trying the bridge.

## Need help?

See [troubleshooting](troubleshooting.md), or use
[AI-assisted setup](ai-assisted-setup.md) to understand a prompt. Share only
sanitized diagnostics; keep credentials, identity files and databases private.
