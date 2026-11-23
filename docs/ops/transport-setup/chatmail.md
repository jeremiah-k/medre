# Chatmail transport setup

The Chatmail adapter bridges accepted groups and direct chats through the
official Python RPC client and native Chatmail core. Use a dedicated account
and private profile for each adapter instance.

## Before you begin

Provisioning proceeds through explicit stages with the provider's own
commands: create the profile, confirm the configured account, accept
conversations, and discover chat IDs. A deferred `local_queue` result records
only private journal retention; relay-side SMTP acceptance arrives later
through delivery feedback. Completing a provisioning step never proves relay
acceptance, delivery, or read evidence.

The provider installs its RPC client and native server together.

## Set up

### Install and provision

```bash
medre adapters install chatmail
medre adapter chatmail init --adapter mail
medre adapter chatmail accounts --adapter mail
medre adapter chatmail join --adapter mail
medre adapter chatmail chats --adapter mail --groups --json
```

These provider commands work inside MEDRE's persistent tool environment. In a
locked deployment project, prefix them with `uv run --locked`; the standalone
`medre-chatmail` helper is also available there. See [installation](../install.md).

`init` reads a private DCACCOUNT or DCLOGIN QR payload through hidden input.
`join` explicitly accepts a contact or group invitation. For automation,
`--qr-stdin` reads the payload from standard input. Profile creation and
invitation acceptance never happen implicitly during runtime startup.

The default accounts directory is
`{state}/adapters/mail/chatmail/accounts`. Keep the same `--adapter` and any
`--accounts-dir` override for provisioning and runtime. Stop MEDRE before
profile commands; one process exclusively owns the profile. An unrelated
Chatmail client does not honor MEDRE's lock and must not open it concurrently.

### Configure accepted chats

Save a private `connections.yaml`, using IDs discovered from this profile:

```yaml
adapters:
  chatmail:
    mail:
      adapter_kind: real
      account_id: 1
      chats: [12]
      origin_label: Chatmail
routes: {}
```

Replace `12` with an ordinary accepted chat ID. Omit `--groups` from discovery
to include direct conversations. Routes use decimal strings such as `"12"`;
IDs in another account or profile are not interchangeable. With a YAML file,
`medre adapter chatmail chats --config connections.yaml --adapter mail --json`
selects its profile and account, including environment overrides.

## Verify

After adding another adapter instance, run:

```bash
medre config bridge --config connections.yaml --output bridge.yaml
medre config check --config bridge.yaml
medre routes plan --config bridge.yaml
medre run --config bridge.yaml
```

### Delivery, recovery, and state

The adapter preserves text and source attribution. Replies send a native quote
when the durable destination binding resolves a usable parent and fall back to
readable text otherwise, subject to destination text budgets and normal
delivery authority; threads use readable text fallbacks. Native sender
display attribution is enabled by default. Attachments, native mutations,
reactions, read receipts, typing, and puppeting are unsupported.

First use establishes a history baseline. Later starts resume from durable
admission checkpoints. Outbound admission returns `deferred` with `local_queue`
confirmation after retention in the adapter's private queue, before native
submission. A later correlated SMTP result can report `remote_service`
acceptance or permanent failure. SMTP acceptance does not prove that a
recipient received or read the message.

## Options

| Key                         | Default                                   | What it changes                                       | When to touch it                                              |
| --------------------------- | ----------------------------------------- | ----------------------------------------------------- | ------------------------------------------------------------- |
| `account_id`                | `1`                                       | Which local account of the profile is used            | Multi-account profiles                                        |
| `chats`                     | empty                                     | Accepted chat IDs routed by this instance             | Every bridged chat; discovered IDs only                       |
| `accounts_dir`              | `{state}/adapters/mail/chatmail/accounts` | Profile storage location                              | Custom state layouts; keep provisioning and runtime identical |
| `rpc_server_path`           | provider default                          | Path to the Chatmail RPC helper binary                | Non-standard installs                                         |
| `rpc_timeout_seconds`       | `30.0`                                    | Bound per RPC call                                    | Slow relays                                                   |
| `poll_interval_seconds`     | `5.0`                                     | Interval between inbound polls                        | Latency versus relay load                                     |
| `reconnect_delay_seconds`   | `5.0`                                     | Base delay between reconnect attempts                 | Reconnect pacing                                              |
| `max_text_bytes`            | `16384`                                   | UTF-8 byte budget for rendered outbound text          | Destination text budgets                                      |
| `max_pending`               | `100`                                     | Adapter-local outbound capacity                       | Bursty outbound traffic                                       |
| `origin_label`              | empty                                     | Prefix identifying this origin on the other side      | Labeling in mixed bridges                                     |
| `relay_prefix`              | `[{origin_label}] {sender}: `             | Custom rendering template for relayed text            | Custom presentation                                           |
| `native_sender_attribution` | `true`                                    | Native sender display attribution on relayed messages | Presentation changes                                          |

## Troubleshooting

Uncertain handoffs require inspection before an explicit retry. Back up the
private accounts directory, adapter journal, and MEDRE storage coherently
while stopped.

For provisioning problems, re-check the profile lock rules from
[Install and provision](#install-and-provision): stop MEDRE before profile
commands; one process exclusively owns the profile, and an unrelated Chatmail
client must not open it concurrently.

## Reference

- [stopped-state snapshots](../stopped-state-snapshots.md) — coherent backup
  procedure.
- The [profile](../../spec/transport-profiles/chatmail.md) and provider-owned
  [configuration schema](https://github.com/jeremiah-k/medre-adapter-chatmail/blob/main/docs/schemas/config.schema.json).
- [guided configuration](../guided-configuration.md) — the route builder used
  to connect two adapters.
