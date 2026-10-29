# Privacy and message storage

MEDRE reads messages so it can forward them to another network. It also keeps
local message content and delivery records. Your messaging app's encryption
does not encrypt MEDRE's local database.

## What MEDRE keeps today

- Message text, and sometimes excerpts used to quote replies.
- Delivery records and message identifiers used for retries and replies.
- Attachment bytes when attachment retention is enabled.
- Native account/profile data kept by the adapters you use.

Automatic event expiry, delete-after-handoff and a history-free privacy mode
are **not implemented**. Disabling attachments prevents new attachment transfers;
it does not remove previously stored bytes. Stopping MEDRE does not erase its data.

Protect the state directory and backups. To see logical core storage totals
without displaying messages, first find the state directory with `medre paths`,
then run:

```bash
medre inspect content --storage-path /path/to/state/medre.sqlite
```

This is an inventory, not a deletion command. It does not measure native account
stores, backups, SQLite free pages or all physical disk usage.

## Why replies need some context

A native message identifier can point a reply at the correct message without
keeping its text. If that identifier is unavailable, MEDRE can use a short quote
instead—but only if the relevant text is available. Without either, it can show
a readable reply marker rather than guess at another message.

Keeping a short-lived reply excerpt is different from keeping a complete message
archive. Keeping delivery identifiers is also different from keeping message text.
These distinctions guide the [content lifecycle design](../dev/content-lifecycle.md).

## Privacy choices being designed

These are proposed choices, not configuration settings you can enable today:

| Choice         | What it would keep after delivery                                                        | Trade-off                                                                                                   |
| -------------- | ---------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------- |
| Relay only     | Minimal delivery/correlation records, with optional short-lived reply context in memory. | A restart loses in-memory fallback quotes; native replies can continue where their references remain valid. |
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
