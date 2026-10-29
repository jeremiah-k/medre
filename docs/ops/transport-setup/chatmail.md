# Chatmail transport setup

The Chatmail adapter bridges accepted groups and direct chats through the
official Python RPC client and native Chatmail core. Use a dedicated account
and private profile for each adapter instance.

## Install and provision

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
The provider installs its RPC client and native server together.

`init` reads a private DCACCOUNT or DCLOGIN QR payload through hidden input.
`join` explicitly accepts a contact or group invitation. For automation,
`--qr-stdin` reads the payload from standard input. Profile creation and
invitation acceptance never happen implicitly during runtime startup.

The default accounts directory is
`{state}/adapters/mail/chatmail/accounts`. Keep the same `--adapter` and any
`--accounts-dir` override for provisioning and runtime. Stop MEDRE before
profile commands; one process exclusively owns the profile. An unrelated
Chatmail client does not honor MEDRE's lock and must not open it concurrently.

## Configure accepted chats

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

After adding another adapter instance, run:

```bash
medre config bridge --config connections.yaml --output bridge.yaml
medre config check --config bridge.yaml
medre routes plan --config bridge.yaml
medre run --config bridge.yaml
```

## Delivery, recovery, and state

The adapter preserves text and source attribution; replies and threads use
readable text fallbacks. Native sender display attribution is enabled by
default. Attachments, native mutations, reactions, read receipts, typing, and
puppeting are unsupported.

First use establishes a history baseline. Later starts resume from durable
admission checkpoints. Outbound admission returns `deferred` with `local_queue`
confirmation after retention in the adapter's private queue, before native
submission. A later correlated SMTP result can report `remote_service`
acceptance or permanent failure. SMTP acceptance does not prove that a
recipient received or read the message. Uncertain handoffs require inspection
before an explicit retry.

Back up the private accounts directory, adapter journal, and MEDRE storage
coherently while stopped. See [stopped-state snapshots](../stopped-state-snapshots.md),
the [profile](../../spec/transport-profiles/chatmail.md), and provider-owned
[configuration schema](https://github.com/jeremiah-k/medre-adapter-chatmail/blob/main/docs/schemas/config.schema.json).
