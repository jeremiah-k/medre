# XMPP Transport Profile

## Purpose and Role

The XMPP adapter is a **hybrid adapter** that connects to an XMPP server through Slixmpp and bridges direct conversations and multi-user chat rooms into the MEDRE canonical event stream. It delivers outbound rendered payloads as message bodies and reports local stream acceptance without claiming server or recipient delivery.

The adapter delegates the SDK client lifecycle to its session; the adapter owns semantic conversion (codec decode, admission, event publishing) and configuration validation.

**Platform identifier:** `xmpp`

**Maturity note:** XMPP integration is at a **pre-release** stage. Delivery status reflects local stream handoff only; server acceptance, recipient delivery and read state are separate.

---

## Configuration Fields

| Field                     | Type             | Default                         | Description                                                                 |
| ------------------------- | ---------------- | ------------------------------- | --------------------------------------------------------------------------- |
| `adapter_id`              | `str`            | _(required)_                    | Unique adapter instance identifier                                          |
| `jid`                     | `str`            | `""`                            | Bot account JID (an optional resource suffix selects the bot's resource)    |
| `password`                | `str`            | `""`                            | Account password; sensitive — never exposed in diagnostics/logs/errors      |
| `rooms`                   | `tuple[str,...]` | `()`                            | Bare JIDs of multi-user chat rooms to join and relay                        |
| `peers`                   | `tuple[str,...]` | `()`                            | Bare JIDs of direct conversations allowed to relay                          |
| `nickname`                | `str`            | `"medre"`                       | Room nickname used when joining                                             |
| `host`                    | `str \| None`    | `None`                          | Server host override; supply `port` together with it, omit both for DNS SRV |
| `port`                    | `int \| None`    | `None`                          | Server port for the host override                                           |
| `connect_timeout_seconds` | `float`          | `30.0`                          | Bounded startup time for the connection and room joins                      |
| `reconnect_delay_seconds` | `float`          | `5.0`                           | Positive delay between reconnection attempts                                |
| `max_text_bytes`          | `int`            | `16384`                         | UTF-8 byte budget for outbound message bodies                               |
| `inbound_queue_size`      | `int`            | `100`                           | Bounded ingress queue depth                                                 |
| `origin_label`            | `str`            | `""`                            | Platform-neutral operator-defined source label for relay prefixes           |
| `relay_prefix`            | `str`            | `"[{origin_label}] {sender}: "` | Relay prefix template for outbound text                                     |

Configured room and peer JIDs must already be **case-folded** (RFC 7622 preparation): servers deliver prepared addresses, and validation rejects ASCII uppercase so a mixed-case conversation cannot silently drop inbound traffic.

---

## Conversation Contexts

Inbound source contexts are exact bare JIDs: a room's bare JID for multi-user chat (occupant identities are retained on the event), and a peer's bare JID for direct conversations. Room and peer sets must be disjoint. Outbound destinations use the same bare-JID form.

---

## Capabilities

Each instance exposes its configured `max_text_bytes` budget to planning and health.

Machine-readable capability declaration: [`xmpp-capabilities.json`](xmpp-capabilities.json)

> Capability levels map to the CapabilityLevel type alias (adapter-runtime.md §6.2):
> `"native"` = `TRUE`, `"fallback"` = degraded inline text, and
> `"unsupported"` = `FALSE`.

| Capability          | Value                                                                                          |
| ------------------- | ---------------------------------------------------------------------------------------------- |
| text                | `True`                                                                                         |
| title               | `False`                                                                                        |
| replies             | `"native"` (destination-scoped XEP-0461 replies; unknown targets retain readable fallback) |
| threads             | `"fallback"`                                                                                   |
| reactions           | `"unsupported"`                                                                                |
| edits               | `"unsupported"`                                                                                |
| deletes             | `"unsupported"`                                                                                |
| attachments         | `False`                                                                                        |
| metadata_fields     | `False`                                                                                        |
| store_and_forward   | `False`                                                                                        |
| direct_messages     | `True`                                                                                         |
| channels            | `True`                                                                                         |
| identity_encryption | `False`                                                                                        |
| typing              | `True`                                                                                         |
| presence            | `False`                                                                                        |
| topic_rooms         | `True`                                                                                         |
| mesh_routing        | `False`                                                                                        |
| priority_delivery   | `False`                                                                                        |

---

The configured default text budget is 16,384 UTF-8 bytes. The capability
profile describes that default instance budget; an explicit configuration
override is reflected in runtime capabilities.

## Protocol Boundaries

Room reply targets use trusted room-issued XEP-0359 IDs. Outbound room messages
use bounded own-echo correlation to retain that native identity; an unavailable
echo leaves the reference absent without changing successful local handoff into
a retry. XEP-0428 compatibility quotes use Unicode code-point ranges. Neither a
room echo nor native reply support establishes recipient delivery or threading.

XEP-0085 chat states are treated as ephemeral activity rather than durable messages.
Typing forwarding is explicitly opt-in through the bounded [transient activity
contract](../transient-typing.md), with negotiated direct peers and configured rooms. It
does not confer full presence or delivery evidence. The adapter does not forward read
receipts, attachments, OMEMO content, native edits or deletes, archives, or puppeting;
each of those requires its own negotiation, expiry, evidence and ownership decisions and
is out of scope for the text bridge.
