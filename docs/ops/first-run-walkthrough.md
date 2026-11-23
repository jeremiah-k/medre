# First-run walkthrough

The shortest honest path for one bridge, in order. Each step names the
command to run, what good output looks like, and the one check that gates
moving on. Nothing here replaces the per-transport setup guides; it connects
the surfaces you will use while following them.

The walkthrough shows eight distinct read-only categories of knowledge:
`configured`, `installed`, `runtime observed`, `adapter accepted`,
`recipient observed`, `confirmation pending`, `uncertain`, and
`content expired` / `enforced`. They are deliberately separate: evidence in
one category never stands in for another. MEDRE's own output never claims
`recipient observed`; that fact comes only from checking the receiving
client or device yourself.

## Before you start

Run this and write the output down:

```bash
medre version
```

The first line (`medre X.Y.Z`) and the `Source` line identify the exact
installation you are about to configure. Every report below that carries a
`medre_version` field — the readiness report, the evidence bundle, and the
support bundle manifest — repeats that same string; if it ever differs from
what `medre version` prints now, you are reading evidence collected by a
different installation. Command flags shown here are those the CLI accepts
today; re-run `medre COMMAND --help` after any upgrade.

## 1. Install MEDRE and one adapter

Install the core, then the adapter package for your transport, with
[installation](install.md). For the Matrix example used throughout this page:

```bash
uv tool install --python 3.12 medre
medre adapters install matrix
medre version
medre adapters
```

Good output: `medre version` prints the expected version and a `Source`
line, and `medre adapters` lists your transport under `Installed adapters:`
with a version and a source label such as `pypi` or
`github:OWNER/REPO@deadbeef`.

Check before moving on: your transport appears under `Installed adapters:`.
If the SDK line for your transport reads `not installed`, install the
adapter's SDK extra too (see
[adapter installation](adapter-installation.md)); a first run can proceed
only when the adapter's own SDK requirement is satisfied for
`adapter_kind: real`.

## 2. Check the offline readiness report

```bash
medre readiness
```

Good output: the `Compatibility snapshot` block lists your transport with
`installed`, the `Configuration` line is absent until step 3 saves a file
(a missing configuration is a first-class outcome, not an error), and the
report ends with the boundary statement: offline checks never observe the
live runtime, never establish native acceptance, and never verify
recipients.

Check before moving on: your transport shows `installed` — not `missing`
(distribution absent) or `unregistered` (installed but broken; the report
prints repair guidance). `readiness` is read-only and performs no network
or hardware I/O; `installed` here is distribution presence, not a
connection.

## 3. Configure one adapter and one route

Create the connection settings and the bridge routes with the guided
commands. The `--transport` value is the installed adapter type named by
`medre config native --help`; keep your credentials in a private file
outside version control (see
[locked deployment](install.md#locked-deployment)). Follow the per-transport
prompts in [transport setup](transport-setup/README.md); for the Matrix
example:

```bash
medre config native --transport matrix --adapter-id bot --output matrix.yaml
medre config bridge --config matrix.yaml --output bridge.yaml
medre config check --config bridge.yaml
```

Good output: the wizards print what they wrote, and `config check` ends with
`Config valid`.

Check before moving on: `Config valid`. Then confirm what is now
`configured` — distinct from `installed`:

```bash
medre readiness --config bridge.yaml
```

The `Adapters:` block lists every configured instance with `preflight
passed`; the `Configuration:` line reports `valid` with the adapter count.
A row reading `blocked` with a category such as `sdk_distribution_missing`
or `config_invalid` names exactly what is still missing — fix it before
starting anything. The report still ends with the same boundary statement:
`runtime` and `recipients` stay `not_observed`.

## 4. Observe the runtime once

Build-time snapshots never show a running system. The one command that
reports live adapter state is `diagnostics` with `--refresh-health`; it
builds the runtime, starts the configured adapters, polls each adapter's
health once, captures the snapshot, and stops the runtime cleanly. With
real adapters this opens real connections to your account or device.

```bash
medre diagnostics --refresh-health --config bridge.yaml
```

Good output: the snapshot is a live one, and every configured adapter id
appears with `health healthy` and `adapter_state ready`. In JSON
(`--format json`) this is `health.live_health.adapters.<id>` with
`health: "healthy"`; the adapter summary also states `fake_or_live`, so a
simulated adapter can never be mistaken for a connected transport.

Check before moving on: every configured adapter id reports `healthy`. Any
`degraded`, `unavailable`, or `failed` entry — or a missing one — means the
runtime did not fully come up; see [troubleshooting](troubleshooting.md)
before sending anything. Without `--refresh-health`, `diagnostics` reports
only build-time state: every adapter reads `health unknown` and
`live_health` stays null — that absence is the honest offline answer, and
no other command fills it in.

## 5. Send one test message and read the evidence

Start the bridge, send one message from the source conversation, check the
destination client yourself, then stop with Ctrl+C. `medre run` is the one
command in this walkthrough that sends messages; everything after it is
read-only.

```bash
medre run --config bridge.yaml
```

After stopping the bridge, read what the message left behind. The
inspection commands open the SQLite database file directly; find its path
first (the `Global DB` path shown here, unless `bridge.yaml` selects
another `storage.path`):

```bash
medre paths
```

To go from the message you sent to its canonical event id, resolve the
source message's native id — the id your source client shows for that
message — with the adapter id you configured:

```bash
medre inspect native-ref --adapter bot --channel '!room:example.com' \
  --message '$SOURCE_MESSAGE_ID' --storage-path /path/to/medre.sqlite
```

Good output: a ref whose `resolves_to` field is the canonical event id and
whose `direction` is `inbound`. If the ref is not found, the message never
entered durable ingress — nothing downstream happened either; re-check the
source conversation and the route's source channel.

Then read the event and its receipts:

```bash
medre inspect event EVENT_ID --timeline --storage-path /path/to/medre.sqlite
medre inspect receipts --event EVENT_ID --storage-path /path/to/medre.sqlite
```

Good output: `inspect event` shows the stored canonical event with its
`source_adapter` and native reference; `inspect receipts` shows one receipt
per destination with `status: "sent"`, an `adapter_message_id`, and a
`confirmation_level` — `local_queue`, `local_transport`, `remote_service`,
or `end_to_end`, the strongest delivery fact proven at adapter hand-off
time. `confirmation_level` is adapter acceptance strength, never recipient
observation: `end_to_end` means the transport reported an end-to-end fact
at hand-off, not that a recipient client displayed the message.

Check before moving on: each destination has a `sent` receipt whose
`confirmation_level` you can name, and you have seen the message arrive in
the destination client yourself. A missing receipt (`[]`) means the adapter
never accepted a delivery — an incident, not a healthy state; follow
[the incident walkthrough](incident-walkthrough.md).

## Reading the eight categories on your bridge

After step 5, every category has a home. This table is the page's whole
summary; if a column's evidence is missing, the category is unknown — never
substitute another column's success.

| Category                   | Command and field that establishes it                                                                                                                                               | What its absence looks like                                                                             |
| -------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------- |
| configured                 | `medre readiness --config bridge.yaml` — `Configuration: valid`, one `Adapters:` row per instance; `medre config check`                                                             | `config_not_found`, `validation_error`, or the instance missing from the `Adapters:` block              |
| installed                  | `medre readiness` — manifest row `installed` with `registration registered` and a version; `medre adapters` — `Installed adapters:` entry                                           | manifest row `missing` or `unregistered`; transport absent from `Installed adapters:`                   |
| runtime observed           | `medre diagnostics --refresh-health --config bridge.yaml` — `health.live_health.adapters.<id>.health: "healthy"`                                                                    | `live_health` null and `health unknown` on every adapter; readiness `runtime: not_observed`             |
| adapter accepted           | `medre inspect receipts --event E --storage-path DB` — receipt `status: "sent"` plus `confirmation_level` and `adapter_message_id`                                                  | empty receipt array `[]`, or `failed` / `dead_lettered` with a `failure_kind`                           |
| recipient observed         | Nothing in MEDRE. Check the destination client yourself; readiness reports `recipient: not_observed` always, and `inspect work-status` reads `Recipient delivery: unknown`          | Always absent from MEDRE output; absence is never a negative answer about the recipient                 |
| confirmation pending       | `medre inspect work-status --storage-path DB` — `native queue N` greater than zero; `medre resolve ... --resolution ...` preview with `expected_status: "queued"`                   | `native queue 0` and no `queued` receipt for the event                                                  |
| uncertain                  | `medre inspect work-status` — `uncertain outcome N` or reserved generations without attempt evidence; `medre inspect radio-attempt OUTBOX ATTEMPT` — a part with `state: "unknown"` | uncertainty totals all zero; every radio part `accepted` or `unsent`                                    |
| content expired / enforced | `medre storage ceiling audit --storage-path DB --event E` — `state: "elapsed_pending"`, `"residual_enforced"`, or `"enforced_clean"`                                                | `state: "not_elapsed"`; before the sweep runs, an expired event stays `elapsed_pending` with no receipt |

A clean first run shows the first four categories populated, `recipient
observed` established only by your own check of the destination client, and
the last three empty. Empty is a fact, not a problem: `confirmation
pending`, `uncertain`, and `content expired` describe situations that a
healthy first run does not create. The incident walkthrough shows each one
worked end to end: see
[the incident walkthrough](incident-walkthrough.md).

## Where this walkthrough stops

- The sample smoke run in [getting started](getting-started.md) uses
  simulated adapters; its `sent` receipts prove local pipeline handoff and
  never touch a transport. This page's step 5 is the first step whose
  receipts describe a real adapter hand-off.
- Retention and the hard content ceiling are off by default. Read
  [privacy and message storage](message-storage.md) before bridging
  sensitive conversations, and
  [the ceiling audit](content-ceiling-audit.md) before enabling a ceiling.
- Keeping the bridge running, backups, and updates continue in
  [operator workflows](operator-workflows.md) and
  [running MEDRE](running-medre.md).
