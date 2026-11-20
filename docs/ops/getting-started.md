# Getting Started

MEDRE connects conversations across Matrix, Meshtastic, MeshCore, LXMF, XMPP,
Chatmail and SimpleX Chat. Start with two connections; add more when you need them.
The example below connects Matrix and Meshtastic.

## 1. Install MEDRE

Install [uv](https://docs.astral.sh/uv/getting-started/installation/).
The Matrix provider requires Python 3.12 or later; explicitly choose it when
installing the isolated tool:

```bash
uv tool install --python 3.12 medre
medre adapters install matrix meshtastic
```

If `medre` is not found, run `uv tool update-shell` and open a new terminal.
On Windows, run the extra installation command MEDRE prints after it exits.
For other networks, replace `matrix meshtastic` with the adapters you want.
Upgrading preserves saved package requests. If you have Git-backed requests,
keep Git installed. See [package management](package-management.md).

## 2. Verify your installation

Run a local check before connecting accounts or radios:

```bash
medre config sample > sample.yaml
medre config check --config sample.yaml
medre smoke --config sample.yaml --storage-path smoke.sqlite3
```

Look for `PASS`. The sample checks one-way forwarding only, using simulated
adapters. Keep the generated files private.

## 3. Connect your accounts or devices

For the Matrix and Meshtastic example, these prompts create your connection
settings. Have your Matrix account details and radio connection ready. Name
the connections `bot` and `node` using `--adapter-id`:

```bash
medre config native --transport matrix --adapter-id bot --output matrix.yaml
medre config native --transport meshtastic --adapter-id node \
  --config matrix.yaml --output connections.yaml
```

The prompts explain which values to enter. Keep the generated files private
and outside version control because they may contain credentials.
If working in a Git project, ignore `matrix.yaml`,
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

## 4. Choose which conversations to connect

```bash
medre config bridge --config connections.yaml --output bridge.yaml
medre config check --config bridge.yaml
medre routes plan --config bridge.yaml
```

Select the `bot` and `node` connections. When the wizard asks for a context,
enter the Matrix room ID and the Meshtastic channel number for their respective
connections. Choose one-way or two-way forwarding. Review the plan before saving,
then check that the saved file connects the expected conversations in the
directions you chose. For advanced options or existing routes, see
[guided configuration](guided-configuration.md).

## 5. Run your bridge

```bash
medre run --config bridge.yaml
```

Send a test message from the configured source and verify it at the destination.
Then test the reverse direction.
A one-way route must not forward messages in reverse.
Press **Ctrl+C** to stop the bridge.

MEDRE stores messages and delivery records locally. For local retention,
an opt-in relay-only policy can logically collect eligible, settled message
text. For dispatch, an explicitly enabled hard content ceiling fences new local
native dispatch starts when the deadline is observed, and work already accepted
by a transport may still arrive later. The guarded
retirement sweep for expired unstarted work is disabled by default.
General timed retention profiles and physical erasure are not available, and
copies held by connected transports and providers are unaffected. See
[privacy and message storage](message-storage.md) before bridging sensitive
conversations.

## Keep a bridge running

After the first test works, use a
[locked deployment](install.md#locked-deployment) to keep your installed versions
consistent. See [running MEDRE](running-medre.md) for service setup and monitoring.
For updates and state backups, see [operator workflows](operator-workflows.md).

## Need help?

See [troubleshooting](troubleshooting.md) when a check or test fails. If you
use an agent, [AI-assisted setup](ai-assisted-setup.md) has prompts for
planning and checking routes.
