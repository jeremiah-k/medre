# Chatmail Transport Profile

## Role and addressing

The Chatmail adapter bridges accepted groups and direct chats through the
pinned official Python RPC client and native RPC server. MEDRE owns one private
accounts directory per adapter instance and an explicit account ID. Routes use
ordinary local chat IDs as decimal channel strings; the bot and peer may have
different IDs for the same group. Only configured chats are routable.

Use `medre-chatmail init`, `join`, and `chats --groups --json` to prepare and
discover conversations. Profile creation and invitation acceptance are explicit
operator actions. Startup does not create accounts or accept invitations.

## Capability contract

The [machine-readable declaration](chatmail-capabilities.json) is checked
against the real adapter and SDK-free fake. Text, groups, direct conversations,
destination-scoped native replies with readable fallback, thread text fallbacks,
and native store-and-forward are supported.
Attachments, reactions, native edits/deletes, typing, presence, and forwarded
read receipts are unsupported. The configured text limit defaults to 16,384
UTF-8 bytes; it is enforced before native submission.

The renderer preserves source attribution in plain text, applies the configured
relay prefix, and binds native quote targets and bounded reply fallbacks to the
durable attempt. Native targets require core's unique destination binding and
provider-side validation of the active native profile and exact chat. Platform IDs
stay in native metadata and correlation references rather than being inferred from
visible text.

## Admission, recovery, and evidence

Fresh ordinary text messages and native quote references pass through canonical
admission. Control messages and unsupported media do not masquerade as text.
RPC reconnect and polling recover pending work; persistent checkpoints advance
only after admission succeeds. Profiles and journal state require private
storage and exclusive ownership.

Outbound work is deferred while the native SMTP operation is pending. Feedback
reports native transport acceptance or failure with the original attempt
identity. Native acceptance MUST NOT be promoted into remote recipient delivery
or read confirmation. `identity_encryption` is false; native encryption metadata
does not establish a MEDRE-verified peer identity.

## Qualification limits

SDK contracts cover native group text, quote fields, process recovery and local
storage. The provider's
[native reply qualification](https://github.com/jeremiah-k/medre-adapter-chatmail/blob/86e2783bd6f754d02a5561233881866eb0be0a64/docs/evidence/native-replies-rpc-2.66.0-2026-10-29.json)
records exact source hashes and core/provider revisions for a dedicated two-profile
2.66.0 campaign on fresh native profiles. Its independent encrypted-group peer
observes the native quote,
sender/origin presentation and SMTP handoff after reconnect and restart.
Offline native contracts also cover profile exclusion and same-version
stopped-state restoration. The separate 2.65.0 campaign remains historical
evidence for that SDK pair.

The local GreenMail SMTP/IMAP fixture does not qualify deployment of the official
Chatmail Docker service. Attachments, additional relation features and sustained
load remain separate evidence tiers. A native quote observed by that peer does
not promote SMTP handoff into a general recipient-delivery or read guarantee.
