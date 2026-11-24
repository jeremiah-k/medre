# SimpleX Chat transport setup

The SimpleX Chat adapter bridges allowlisted active groups and accepted direct
conversations through the official Python client and an owned native worker.
Groups are the primary qualified workflow. Each instance uses a dedicated profile.

## Before you begin

Provisioning is explicit: runtime startup does not create an identity or
accept invitations. Stop MEDRE before profile commands — profile commands
exclusively lock the profile and must not race a running relay. Pick one
`--adapter` ID and reuse the same selection for setup and runtime.

The official SDK downloads its native library on first initialization; an
existing installation can be selected with `libs_dir`.
Adapter CI qualifies Linux x86-64; other SDK platforms need their own validation.

## Set up

### Install and create the profile

```bash
medre adapters install simplexchat
medre adapter simplexchat init --adapter sx --name 'MEDRE bridge'
medre adapter simplexchat join --adapter sx
medre adapter simplexchat groups --adapter sx --json
```

For a locked deployment project, prefix commands with `uv run --locked`.
The standalone `medre-simplexchat` helper is also available in that environment.
See [installation](../install.md).

`join` reads a private group/contact link through hidden input, or through
`--link-stdin` in automation. It persists the request and keeps networking
active for a bounded interval. Connection observation is separate from active
group membership. Inspect `groups`, then accept an invited group explicitly:

```bash
medre adapter simplexchat accept-group --adapter sx --group-id 7
medre adapter simplexchat contacts --adapter sx --json
```

Use `connect` to resume pending native connections; the remote peer may need to
approve. Initialization and membership changes are explicit operator actions.

### Configure profile-local conversations

Save a private `connections.yaml` with discovered IDs:

```yaml
runtime:
  adapter_start_timeout_seconds: 120
adapters:
  simplexchat:
    sx:
      adapter_kind: real
      user_id: 1
      chats: [group:7, contact:3]
      origin_label: SimpleX Chat
routes: {}
```

Routes use exact strings such as `"group:7"` and `"contact:3"`. Both sides of
a routed conversation must be allowlisted. IDs belong to this profile and are
not global identities. The default profile prefix is
`{state}/adapters/sx/simplexchat/profile`; use the same `--adapter` and any
`--database-prefix` override during setup. After saving YAML, `--config
connections.yaml --adapter sx` selects its profile and environment overrides.
For an encrypted profile, use `MEDRE_ADAPTER__SX__DATABASE_KEY` rather than
placing the key in YAML or arguments.

## Verify

Add another adapter instance, then run:

```bash
medre config bridge --config connections.yaml --output bridge.yaml
medre config check --config bridge.yaml
medre routes plan --config bridge.yaml
medre run --config bridge.yaml
```

### Ownership, recovery, and evidence

First use baselines native history. Restart recovery walks retained history and
admits missing messages through durable checkpoints; large profiles can need
additional startup time. The example allows 120 seconds for the full adapter
start because the native session's default `startup_timeout_seconds` is 90.
Increase the runtime budget as needed for retained history; keep it above the
adapter's own session budget.
A successful outbound handoff confirms the native local queue. An uncertain
reservation requires inspection rather than blind resubmission.

Text attribution, native quote replies, and thread text fallbacks are
supported: a reply uses a native quote when the retained parent is verified in
the active profile, and falls back to readable inline text when no usable
parent exists, subject to destination text budgets and normal delivery
authority. Attachments, edits, deletions, reactions, typing, and puppeting
are unsupported. Local health does not establish peer reachability. Independent
group receipts have separate qualification from direct-contact and
worker-death recovery.

## Options

| Key                       | Default                                   | What it changes                                            | When to touch it                                       |
| ------------------------- | ----------------------------------------- | ---------------------------------------------------------- | ------------------------------------------------------ |
| `user_id`                 | `1`                                       | Which native user of the profile is used                   | Multi-user profiles                                    |
| `chats`                   | empty                                     | Allowlisted `group:ID` / `contact:ID` conversations routed | Every bridged conversation; discovered IDs only        |
| `database_prefix`         | `{state}/adapters/sx/simplexchat/profile` | Native profile database location                           | Custom state layouts; keep setup and runtime identical |
| `database_key`            | empty                                     | Encryption key for an encrypted profile; sensitive         | Encrypted profiles, via the environment override       |
| `state_dir`               | resolved state directory                  | Base directory for adapter state                           | Custom state layouts                                   |
| `native_cache_dir`        | adapter default                           | Native library cache location                              | Non-standard installs                                  |
| `libs_dir`                | SDK default                               | Existing native library installation                       | Offline or preinstalled libraries                      |
| `command_timeout_seconds` | `30.0`                                    | Bound per native command                                   | Slow hosts                                             |
| `startup_timeout_seconds` | `90.0`                                    | Native session start budget                                | Large retained history                                 |
| `poll_interval_seconds`   | `1.0`                                     | Interval between inbound polls                             | Latency versus load                                    |
| `max_text_bytes`          | `16384`                                   | UTF-8 byte budget for rendered outbound text               | Destination text budgets                               |
| `origin_label`            | empty                                     | Prefix identifying this origin on the other side           | Labeling in mixed bridges                              |
| `relay_prefix`            | unset                                     | Custom rendering template for relayed text                 | Custom presentation                                    |

The `runtime.adapter_start_timeout_seconds` value bounds the whole adapter
start; raise it together with `startup_timeout_seconds` for large profiles.

## Troubleshooting

Stop MEDRE before profile commands. A surviving native worker retains ownership
until it exits; leader exit alone does not release the profile. Unrelated
SimpleX clients do not honor MEDRE's lock and must not open it concurrently.
Back up the native profile, ownership sidecar, adapter ledgers, and core storage
together.

## Reference

- [stopped-state snapshots](../stopped-state-snapshots.md) — coherent backup
  procedure.
- The [profile](../../spec/transport-profiles/simplexchat.md) and adapter-owned
  [configuration schema](https://github.com/jeremiah-k/medre-adapter-simplexchat/blob/main/docs/schemas/config.schema.json).
- [guided configuration](../guided-configuration.md) — the route builder used
  to connect two adapters.
