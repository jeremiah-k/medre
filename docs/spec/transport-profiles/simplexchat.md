# SimpleX Chat Transport Profile

## Role and addressing

The SimpleX adapter bridges active groups and accepted direct conversations
through the pinned official Python client and an owned native worker process.
Routes use explicit profile-local `group:ID` and `contact:ID` channel strings.
Both source and destination conversations must be allowlisted. IDs from another
profile are not interchangeable.

Use `medre-simplexchat init`, `groups`, `contacts`, `join`, `accept-group`, and
`connect` for explicit profile and membership management. Profile initialization
and invitation acceptance do not occur implicitly during runtime startup.

## Capability contract

The [machine-readable declaration](simplexchat-capabilities.json) is checked
against the real adapter and SDK-free fake. Ordinary text, groups, direct
conversations, reply/thread text fallbacks, encrypted identities, and native
store-and-forward are declared. Native quotes, attachments, edits, deletions,
reactions, typing, presence, and puppeting are unsupported. Private group support
chats and live partial messages do not enter fresh-message admission.

The compatibility renderer applies source attribution and the configured relay
prefix to plain text. Its configured limit defaults to 16,384 UTF-8 bytes and is
enforced before native submission.

## Admission, recovery, and evidence

Admission cursors and bounded history pagination persist per profile and
conversation. Rejected admission does not advance a durable cursor. Profile and
handoff ledgers require private storage and exclusive ownership. Worker failure
releases ownership so an explicit restart can reuse the profile.

A successful send reports `local_queue` confirmation with the native item ID.
It MUST NOT claim transport acceptance, an independent peer receipt, or read
state. A persisted handoff ledger prevents duplicate native queue submission
when the same delivery attempt is retried after restart.

## Qualification limits

The native SDK tier proves profile creation, group history, local queue
acceptance, durable duplicate suppression, and worker cleanup. It does not prove
receipt by an independent profile. SimpleX is outside the six-adapter native
campaign until an independent peer helper and dedicated group fixture are
qualified. SDK-free pair tests provide routing and rendering evidence only.
