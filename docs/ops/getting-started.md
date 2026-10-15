# Getting Started

MEDRE connects conversations across independently installed platforms. You choose
the connections, the messages that may cross them, and how their source appears
to recipients. Core handles routing, persistence, lifecycle, and delivery
evidence; each adapter owns its platform's configuration, protocol, and SDK.

Start with a local fake bridge to learn the commands, then connect real accounts
or devices. MEDRE is pre-release: use reviewed revisions and keep your deployment
lockfile.

## Install from Git with uv

Install [uv](https://docs.astral.sh/uv/getting-started/installation/) and Git.
A small deployment project keeps MEDRE and your chosen adapters in **one Python
environment**, with their complete dependency graph recorded in a lockfile.
You do not need a source checkout or a published PyPI release.

The extraction is currently on the core branch shown below. After it merges,
use the compatible core revision on main. Adapter repositories are independent;
their main branches already contain the extracted implementations.

```bash
uv init --bare --python 3.13 my-medre-bridge
cd my-medre-bridge
uv add \
  'medre @ git+https://github.com/jeremiah-k/medre.git@build/external-adapter-extraction' \
  'medre-adapter-matrix @ git+https://github.com/jeremiah-k/medre-adapter-matrix.git@main' \
  'medre-adapter-meshtastic @ git+https://github.com/jeremiah-k/medre-adapter-meshtastic.git@main'

uv run --locked medre version
uv run --locked medre adapters
```

This example installs Matrix and Meshtastic with their SDK dependencies.
Choose only the adapters you need. The [adapter installation guide](adapter-installation.md)
covers Git commit pins, local checkouts, private sources, and package indexes.
For a repeatable deployment, replace moving refs with reviewed full commit SHAs
and retain both `pyproject.toml` and `uv.lock`. Core's
[compatibility manifest](../../.github/adapter-compatibility.json) records the
adapter revisions tested together in CI.

Here, uv manages Python, the virtual environment, and packages. Run commands
with `uv run --locked` from this directory; there is no activation step. To
restore the recorded environment later, use `uv sync --locked`. Keep credentials,
state databases, and generated private configs out of version control.

### Where uvx fits

[uvx](https://docs.astral.sh/uv/guides/tools/) runs a command in an isolated tool
environment. It is convenient for trying core help or version output:

```bash
uvx --from 'git+https://github.com/jeremiah-k/medre.git@build/external-adapter-extraction' medre version
```

That environment does not inherit adapters from your deployment project. Any
adapters needed by a uvx invocation must be supplied with `--with`. Prefer the
locked project above for a bridge you will run repeatedly. `uv tool install` is
another option for a persistent CLI, but a deployment project makes the complete
core-and-adapter environment easier to review and reproduce.

## Prove the installation without connecting anything

Installed first-party providers contribute fake configurations. Fakes exercise
routing and storage without opening a homeserver or device connection.

```bash
uv run --locked medre config sample > sample.yaml
uv run --locked medre adapters check --config sample.yaml
uv run --locked medre config check --config sample.yaml
uv run --locked medre smoke --config sample.yaml --storage-path smoke.sqlite3 --json
```

The smoke report should show status passed, a source adapter, matched targets,
and delivery receipts. It also reports `source_candidates`: a normal smoke
exercises one source. For configurations with multiple source adapters, repeat
with `--source-adapter` and another eligible **instance ID** to cover each source.
A pass proves the fake pipeline;
it does not prove network access or recipient delivery.

With no sample-contributing provider installed, the sample has empty adapter
and route sections and prints installation guidance. Install a provider before
expecting a routing smoke to pass.

## Learn the pieces

| Concept              | Meaning                                                      | Example                                                               |
| -------------------- | ------------------------------------------------------------ | --------------------------------------------------------------------- |
| Provider / transport | An installed package registering a platform type.            | medre-adapter-matrix registers matrix.                                |
| Adapter instance     | One configured connection with a unique ID.                  | bot connects one Matrix account; node connects one Meshtastic device. |
| Canonical event      | Core's platform-neutral representation of an incoming event. | A message, its native reference, and source metadata.                 |
| Context              | A platform-owned address for a conversation.                 | A Matrix room ID or a Meshtastic channel index.                       |
| Route                | A rule connecting adapter instances and contexts.            | Room A to channel 0, with a reverse leg.                              |
| Receipt              | Evidence of an attempted or suppressed delivery.             | Accepted by a local transport, accepted by Matrix, or policy denied.  |

Adapters decode native events and render outbound events. Core does not need to
know whether a future provider uses a mesh, a decentralized chat system, or a
hosted service. Community authors choose their own repositories, package names,
and distribution methods.

## Build a real two-way route

Save this as `bridge.yaml` in your deployment directory. Replace the homeserver,
bot user ID, room ID, and serial device path with your own values. Invite the
bot to a private test room. Provision matching Meshtastic radio settings and
channel keys on your nodes separately; MEDRE connects to the gateway and selects
a channel, rather than configuring its RF region or keys.

```yaml
runtime:
  name: my-bridge

storage:
  backend: sqlite
  path: "{state}/medre.sqlite3"

adapters:
  matrix:
    bot:
      adapter_kind: real
      homeserver: https://matrix.example.org
      user_id: "@bridge:example.org"
      access_token: "" # Supplied through the environment below.
      encryption_mode: plaintext
      room_allowlist:
        - "!test-room:example.org"
      relay_prefix: "[{origin_label}] {sender}: "
  meshtastic:
    node:
      adapter_kind: real
      connection_type: serial
      serial_port: /dev/ttyACM0
      message_delay_seconds: 2.2
      radio_relay_prefix: "[{origin_label}] {sender}: "

routes:
  conversation:
    source_adapters: [bot]
    dest_adapters: [node]
    directionality: bidirectional
    context_map:
      "!test-room:example.org":
        dest_context: "0"
        source_origin_label: Matrix
        dest_origin_label: Local mesh
    policy:
      allowed_event_types: [message.created, message.text]
```

The mapping key is a context on the source adapter. This route forwards the
Matrix room to Meshtastic channel 0 and creates a reverse leg from that channel
back to the room. Other rooms and channels have no matching leg.

Labels describe the originating side: Matrix-to-mesh text gets a Matrix label;
mesh-to-Matrix text gets a Local mesh label. Prefixes affect presentation, not
sender identity or authorization. Native reply support and text limits depend on
the destination provider; delivery evidence records fallback and truncation.

For encrypted Matrix rooms, add the Matrix `e2e` extra to your deployment,
configure `encryption_mode: e2ee_required`, and follow the
[Matrix setup guide](transport-setup/matrix.md) before starting. The plaintext
example above is for an unencrypted test room.

Supply the bot token through your shell's secret mechanism or service manager.
For an interactive Bash shell, this avoids placing it in shell history:

```bash
read -r -s -p 'Matrix bot token: ' MEDRE_ADAPTER__BOT__ACCESS_TOKEN
export MEDRE_ADAPTER__BOT__ACCESS_TOKEN
echo
uv run --locked medre adapters check --config bridge.yaml
uv run --locked medre config check --config bridge.yaml
uv run --locked medre run --config bridge.yaml
```

On other shells or operating systems, set the same environment variable using
their supported secret input mechanism. Environment tokens use the adapter
instance ID, not the transport name.

Send a short message as a separate Matrix user and confirm it on a second
Meshtastic node. Reply from that node and confirm the return event in Matrix.
This checks both native boundaries; a successful local send receipt alone is
not proof that the second node received the packet.

## Extend the bridge deliberately

- **Another room/channel:** add another `context_map` entry with its own room ID
  and channel index. Use per-entry labels when recipients need to distinguish
  conversations.
- **One-way forwarding:** use `directionality: source_to_dest`. This is useful for
  announcements or multiple sources sharing one destination.
- **Fanout:** add separate routes to other adapter instances. Each target keeps
  its own delivery outcome, capability decisions, and rendering limits.
- **Restrict senders or event kinds:** add route policy allowlists. Policy on a
  bidirectional declaration applies to both legs; use separate one-way routes
  when each direction needs different rules.
- **A native addressed destination:** use a structured destination supported by
  the owning provider, such as an LXMF destination hash. Its provider supplies
  validation; core does not carry a fixed catalog of platform addresses.

See [configuration](configuration.md) for complete route forms, environment
overrides, and policy rules. The
[multi-context example](../../examples/configs/live-matrix-meshtastic-channel-map.yaml)
shows several channels and directional alternatives.

## Inspect what happened

The runtime prints resolved paths at startup; `medre paths` also shows them.
Use the database path from your deployment when inspecting persistent evidence:

```bash
uv run --locked medre inspect event EVENT_ID --storage-path /path/to/medre.sqlite3
uv run --locked medre inspect receipts --event EVENT_ID --storage-path /path/to/medre.sqlite3
```

Distinguish local queue acceptance, local transport handoff, and remote service
acceptance. RF reception needs an independent recipient observation; encrypted
LXMF delivery has its own native confirmation behavior.
[Operator workflows](operator-workflows.md) explains tracing, replay, and
recovery without guessing from a successful send log.

## Choose another platform

| Provider repository                                                  | What it owns                                                                 | Setup                                             |
| -------------------------------------------------------------------- | ---------------------------------------------------------------------------- | ------------------------------------------------- |
| [Matrix](https://github.com/jeremiah-k/medre-adapter-matrix)         | Matrix SDK, accounts, rooms, encryption, metadata rendering, and fake.       | [Matrix setup](transport-setup/matrix.md)         |
| [Meshtastic](https://github.com/jeremiah-k/medre-adapter-meshtastic) | Serial/TCP/BLE node connection, packets, channels, and fake.                 | [Meshtastic setup](transport-setup/meshtastic.md) |
| [MeshCore](https://github.com/jeremiah-k/medre-adapter-meshcore)     | Companion connection, channel/contact addressing, firmware limits, and fake. | [MeshCore setup](transport-setup/meshcore.md)     |
| [LXMF](https://github.com/jeremiah-k/medre-adapter-lxmf)             | Reticulum/LXMF identities, destinations, native delivery, and fake.          | [LXMF setup](transport-setup/lxmf.md)             |

Each repository owns its dependencies and licensing. Install only the providers
your deployment uses, and review their licenses and upstream dependencies when
redistributing. Separating a provider repository does not itself change the
license obligations of combining packages.

To build a new provider, start with [adapter authoring](../dev/adapter-authoring.md).
Briar, SimpleX Chat, and other privacy-oriented platforms are possible future
integrations, not currently implemented providers. The same public contracts
also allow authors to integrate proprietary platforms.
