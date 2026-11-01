# Privacy and message storage

MEDRE reads messages so it can forward them to another network. It also keeps
local message content and delivery records. Your messaging app's encryption
does not encrypt MEDRE's local database.

## What MEDRE keeps today

- Message text, and sometimes excerpts used to quote replies.
- Delivery records and message identifiers used for retries and replies.
- Attachment bytes when attachment retention is enabled.
- Native account/profile data kept by the adapters you use.

Timed retention profiles (`recent-context`, `history`), delete-after-handoff
and a history-free privacy mode remain design candidates, not configuration
settings. One opt-in retention policy exists today: the `content_retention`
`relay-only` mode described below. Disabling attachments prevents new
attachment transfers; it does not remove previously stored bytes. Stopping
MEDRE does not erase its data.

Protect the state directory and backups. To see logical core storage totals
without displaying messages, first find the state directory with `medre paths`,
then run:

```bash
medre inspect content --storage-path /path/to/state/medre.sqlite
```

This is an inventory, not a deletion command. It does not measure native account
stores, backups, SQLite free pages or all physical disk usage.

## Opt-in relay-only content retention

The `content_retention` configuration section declares an operator retention
policy. It is disabled by default: with no declaration, nothing ever
collects. With `mode: relay-only` (which additionally requires
`enabled: true`), bounded collection of eligible, already-settled ordinary
message text may run — and only for text admitted after the policy's
activation watermark, so enabling the policy never sweeps earlier history.
Activation is durable: the store records an immutable epoch, and each
re-activation opens a new epoch with a fresh high-water boundary. The
policy declaration, activation epochs, and preview contract are specified in
[content retention](../spec/content-retention.md).

Collection stays best-effort and bounded: blocked, refused, and unmeasured
copies are retained and reported. It is not physical erasure. Attachment
bytes, delivery evidence, native provider stores, relation excerpts on other
events, SQLite free pages, WAL files, and backups are all retained — see
[content collection guard](../spec/content-collection-guard.md) for the
exact boundaries.

### Operator commands and live-storage safety

| Command                                   | Mutates storage                                     | Safe against a live database |
| ----------------------------------------- | --------------------------------------------------- | ---------------------------- |
| `medre storage retention status`          | No (read-only)                                      | Yes                          |
| `medre storage retention activate --yes`  | Opens a new activation epoch (one small transaction) | Yes                          |
| `medre storage collect` (no `--apply`)    | No (read-only preview)                              | Yes                          |
| `medre storage collect --apply --yes`     | Collects eligible text                              | No — stopped bridge storage only |

`medre storage retention status` reports the latest activation epoch, the
worker heartbeat with an honest freshness observation, the number of
collected events, and the bounded retention preview — counts only, with no
message text and no identifiers. `medre storage retention activate --yes`
opens a new operator epoch whose watermark is the current admission
high-water; a running worker observes the new epoch on its next sweep. The
exact-event `medre storage collect --apply` workflow remains the maintenance
path for stopped bridge storage and re-evaluates every event inside its own
write transaction.

## Why replies need some context

A native message identifier can point a reply at the correct message without
keeping its text. If that identifier is unavailable, MEDRE can use a short quote
instead—but only if the relevant text is available. Without either, it can show
a readable reply marker rather than guess at another message.

Keeping a short-lived reply excerpt is different from keeping a complete message
archive. Keeping delivery identifiers is also different from keeping message text.
These distinctions guide the [content lifecycle design](../dev/content-lifecycle.md).

## Privacy choices

The relay-only retention policy above is a configuration setting you can
enable today. It is narrower than the original "relay only" idea: delivery
and correlation records are retained, reply excerpts on other events are
retained, and only eligible settled ordinary text admitted after activation
is collected. The remaining profile choices are still being designed — they
are proposed choices, not configuration settings you can enable today:

| Choice         | What it would keep after delivery                                                        | Trade-off                                                                                                   |
| -------------- | ---------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------- |
| Recent context | A bounded excerpt window for replies, without a full message archive.                    | Some recent text remains available for useful fallback quotes.                                              |
| History        | Message content for an explicit period, plus bounded reply context.                      | More history and replay capability, with more retained content.                                             |

The planned controls also distinguish **delivery expiry**—stop trying to send an
old message—from **content retention**—stop keeping its text. A message already
accepted by a native transport cannot safely be treated as never sent.

Future plugins may request content for a stated purpose and limited time. The
operator's limits take precedence. A plugin should not silently turn a relay-only
installation into an archive; an expired request should receive an explicit
content-unavailable result.

## Other copies still matter

A core privacy policy cannot erase a recipient's messages, a native application's
history, exported diagnostics or an existing backup. Data removal needs to say
which stores it covers. A logical deletion is not a claim that SQLite free pages,
WAL files or filesystem backups have been securely erased.

For backup and update procedures, use [operator workflows](operator-workflows.md).
