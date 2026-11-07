# Transport Limitations

Cross-transport limitation summary, inherent constraints, and known gaps.

Transport-specific claims are verified against the seven transport pins
recorded in `.github/adapter-compatibility.json` (the pin authority) and the
provider repositories at those commits; "the inspected pin" in a section
refers to that commit. See
[`known-limitations.md`](known-limitations.md) for the full provenance table
and SDK pins.

---

## 1. Core Limitations (All Transports)

1. **No exactly-once delivery.** Messages can be lost, duplicated, or dropped
   at any stage. Adapter-level delivery receipts exist and are persisted in
   storage, but there is no end-to-end exactly-once guarantee. The delivery
   pipeline is at-least-once with duplicate suppression on inbound native refs.

2. **No dead-letter admin UI.** Dead-lettered receipts are recorded in storage
   but there is no dedicated CLI command or UI for browsing, replaying, or
   managing dead-lettered events. Operators can inspect them via `medre inspect`
   or evidence bundles.

3. **Local delivery outbox is durable but not exactly-once.** The outbox
   persists pending, retry, and dead-lettered items across process restart.
   Crash timing may cause resend. No end-to-end tracking (no RF confirmation,
   no ACK, no remote receipt).

4. **Runtime capacity control exists; transport-aware rate limiting is
   incomplete.** The runtime enforces configurable in-flight bounds for
   deliveries, replay events, and inbound admissions; inbound saturation
   rejects with counted evidence past the admission timeout instead of
   growing without limit. Meshtastic has bounded adapter-local outbound
   queue retry. Matrix M_LIMIT_EXCEEDED responses are classified as
   transient. Full adaptive transport backoff as runtime policy is not yet
   implemented.

5. **Graceful shutdown is bounded, not fully durable outside MEDRE-owned state.**
   Durable-ingress grace and in-flight capacity drain share one
   `limits.shutdown_drain_timeout_seconds` deadline. Work already admitted to
   MEDRE remains persisted, but work still only inside an adapter SDK or
   adapter-local queue can be abandoned when transport-specific shutdown
   guarantees run out. Adapter stop and retry-worker stop are cooperative and
   best-effort: a task that suppresses `CancelledError` can outlive its cancel
   grace and be abandoned rather than forcibly killed.

6. **Durable ingress starts at canonical admission, not at raw transport receipt.**
   Runtime live adapter ingress requires storage. Normal live callbacks atomically
   persist the canonical event, inbound native reference, and pending work marker
   before returning; there is no storage-less runtime fallback. Failures before
   canonical admission still depend on transport cursor/ACK/recovery semantics.

7. **Structured correlation is process-local execution context.** Managed JSON
   logs inherit trace/event/route/plan/outbox/receipt correlation fields, but
   MEDRE does not provide distributed tracing or an external log collector.

8. **Metrics export is snapshot-based.** `medre diagnostics --format prometheus`
   emits numeric/boolean diagnostics as Prometheus text, but MEDRE does not run
   a Prometheus HTTP endpoint or statsd exporter.

9. **Single-operator only.** Everything is tested and documented for a single
   person on a single machine. Multi-node, multi-operator, and deployment
   scenarios do not exist.

## 2. Transport-Specific Limitations

### 2.1 Matrix

- Multi-room concurrent inbound has not been tested against a real homeserver
  (the provider's live harnesses pin a single-room allowlist;
  `medre-adapter-matrix/tests/test_matrix_live.py` at the inspected pin).
- Decrypted Matrix ingress normalizes reactions, edits, redactions, and media.
  Outbound relations are native: replies, threads, reactions, edits, and deletes
  render through the Matrix adapter, with replacement events preserving the
  original relation per the relation semantics. Outbound attachments relay
  through the durable bounded media transfer: per-attachment byte caps, a
  transfer deadline, bounded retry, and descriptor-only admission on fetch
  exhaustion. MEDRE still does not manage a room-key backup/import/export
  workflow — `auth restore-signing` imports account-signing keys only, never
  historical encrypted-room message keys (`medre-adapter-matrix` README at the
  inspected pin).
- Own-device cross-signing is implemented against the provider-pinned
  `mindroom-nio[e2e]` surface. Peer-device trust defaults to
  `verification_policy: permissive` without an additional MEDRE peer-trust scan
  or trust-gap warnings; opt-in `strict` refuses encrypted text and
  media until the complete recipient view is affirmatively verified in MEDRE's
  own nio store, including sibling devices. Strict retains the SDK send-time
  device fence; it never auto-verifies peers or falls back to plaintext. No
  operator peer-verification workflow is provided. The provider's
  `tests/test_matrix_device_trust_sdk_contract.py` covers the pinned SDK surface;
  `tests/test_matrix_device_trust_docker.py` qualifies strict refusal, verified
  delivery with peer decryption, trust reset, restart persistence, and default
  permissive delivery on Docker Synapse. The Docker tier is opt-in, not a required
  CI gate, and proves no federation, external network, or cross-signing ceremony.
- Missing/mismatched local cross-signing identity is never replaced at runtime.
  Deliberate reset/rotation requires the authenticated
  `medre adapter matrix auth login` command with `--reset-cross-signing`
  (browser-approved OAuth or fresh password); the reset replaces the account
  identity and requires re-verification in other clients.
- Startup token validation depends on session mode: plaintext startup always
  resolves the device via `whoami()` before `restore_login()` (raising on
  authentication failure or user mismatch). E2EE/durable startup does the same
  when no device ID is configured. In E2EE/durable mode with a configured
  `device_id`, `restore_login()` performs no eager server-side validation; an
  invalid token surfaces from the first sync response
  (`session.py::_discover_device_id`,
  `_start_plaintext`, `_start_e2ee_required`, and the durable start path).
- The durable SDK store requires POSIX file leases; Windows production
  sessions are not supported by the pinned store (`medre-adapter-matrix`
  README at the inspected pin).

### 2.2 Meshtastic

- Inbound processing is text messages only. Telemetry, position, and nodeinfo
  portnum types are not decodable as text events; the codec rejects any
  non-text packet category (`medre-adapter-meshtastic/src/medre/adapters/meshtastic/codec.py`
  at the inspected pin).
- The `mtjk` SDK is exact-pinned in the provider's own package
  metadata; a dedicated installed-SDK contract tier
  (`medre-adapter-meshtastic/tests/test_meshtastic_sdk_contract.py`) freezes
  the private send/protobuf/pubsub surfaces MEDRE uses without duplicating the
  version literal.
- `sendText` and `sendData` are synchronous in mtjk; MEDRE wraps them in
  `asyncio.to_thread()` (`session.py` at the inspected pin).
- MEDRE's supported send path never requests acknowledgements:
  `sendText` defaults to `wantAck=False` and structured sends pass
  `wantAck=False` explicitly. No observable `ROUTING_APP` result qualifies as
  remote or end-to-end delivery evidence; the only routing result that can
  reference a MEDRE-sent broadcast is the local node's implicit
  acknowledgment for an overheard rebroadcast, which proves mesh-flooding
  activity only
  (`medre-adapter-meshtastic/src/medre/adapters/meshtastic/routing_observations.py`).
- Node identity in MEDRE is the `!`-prefixed hex node number derived from the
  pinned SDK's `myInfo.myNodeNum` (`session.py` at the inspected pin).
  Whether a node number survives leave/rejoin is Meshtastic protocol
  behavior that cannot be verified from the pinned provider repositories;
  this index makes no stability claim for it.
- The declared `max_text_bytes` budget (227 bytes by default in
  `medre-adapter-meshtastic/src/medre/config/adapters/meshtastic.py`) is the
  protocol-layer text budget. Structured fields sent alongside text (for
  example `reply_id` and
  `emoji`, which ride first-class protobuf fields) add framing overhead that
  is **not** deducted from the budget; tight-budget links should validate
  structured sends against a real node.

### 2.3 MeshCore

- The `meshcore` SDK is exact-pinned in the provider's own package metadata;
  MEDRE's compatibility manifest pins the provider commit. BLE session-layer
  behavior was live-validated June 2026 against a MeshCore node on Linux
  BlueZ (dated observation evidence; no newer hardware run is recorded at
  the inspected pin). TCP session-layer behavior is exercised by a
  deterministic local TCP companion-protocol endpoint using the real SDK
  (`medre-adapter-meshcore/tests/test_meshcore_local_integration.py`);
  serial transport has no dedicated non-hardware coverage — the live harness
  supports it only through its hardware-gated environment configuration.
  BLE requires pre-pairing and is subject to BlueZ stack limitations (stale
  device cleanup and pre-scan before connect; see `session.py` at the
  inspected pin).
- No native reply mechanism. Replies and threads relay as inline fallback
  text; reactions, edits, and deletes are unsupported and suppressed
  (`medre-adapter-meshcore` README and `codec.py` at the inspected pin;
  relations are capability-gated via `CapabilityDecisionResolver`, and
  unsupported relation types produce `capability_suppressed` delivery
  outcomes).
- No startup backlog suppression (intentionally absent: MeshCore has no
  store-and-forward; the capability declares `store_and_forward=False`).
- Sender identity is a 6-byte pubkey prefix (not globally unique).
- Received MeshCore payloads carry no native message identifier — only a
  sender-assigned `sender_timestamp` with one-second resolution (the firmware
  itself relies on timestamp + text + sender to make the radio packet hash
  unique). MEDRE therefore derives the inbound `native_message_id` as a
  deterministic full SHA-256 digest (`mc1-` prefix) over the
  identity-bearing fields (direct flag, sender, channel, timestamp,
  sub-type, text), excluding reception-volatile fields such as RSSI/SNR;
  the digest — not the timestamp — is the durable dedup identity, so
  distinct same-second messages survive admission. Remaining ambiguity: two
  distinct messages identical in all of those fields are indistinguishable
  on the wire and are treated as one retransmission; MEDRE does not claim
  exactly-once separation for them. Packets without `sender_timestamp`
  claim no native identity at all
  (`medre-adapter-meshcore/src/medre/adapters/meshcore/identity.py`).
- MeshCore group messages are subject to a 160-byte firmware text limit that
  includes the firmware's `<device name>: ` prefix; the adapter uses a
  conservative 127-byte budget until the live handshake refreshes the
  effective budget, and `max_text_bytes` (default 160) is an operator upper
  bound. Native sends that bypass the budget fail permanently before SDK
  I/O (`medre-adapter-meshcore` README at the inspected pin).

### 2.4 LXMF

- Multi-hop mesh delivery is not tested. The provider's tiers are
  SDK-contract, deterministic loopback/local-integration, and an RNode
  lifecycle profile that explicitly establishes no delivery or
  reachability (`medre-adapter-lxmf` tests at the inspected pin).
- E2EE beyond Reticulum's native link-layer encryption is not in scope.
- Bounded local retries apply only to failures before the native router
  call. Once the native router call is entered, any raised exception is an
  uncertain handoff outcome: the local handoff journal fences repeated
  submission of the same core attempt, automatic resubmission is
  suppressed, and operator reconciliation is required before an explicit
  retry (`medre-adapter-lxmf/docs/spec/runtime-ownership.md` "Native
  submission uncertainty" at the inspected pin). None of this implies
  end-to-end delivery.
- `LXMessage.source` is the local `RNS.Destination` returned by
  `register_delivery_identity()`; an `LXMRouter` is not a valid source
  (`session.py` at the inspected pin).
- Delivery confirmation is asynchronous and may never arrive. Attempt
  contexts that receive no terminal fact — including uncertain handoffs and
  propagated messages that remain `SENT` — expire after 15 minutes
  in-memory without implying non-delivery and without changing the durable
  handoff journal (`medre-adapter-lxmf/docs/spec/native-worker.md`).
- Propagated messages have no delivery time guarantee and require an explicit
  `outbound_propagation_node` destination hash (validated as a 32-character
  hex hash by the provider's configuration model).
- Production Reticulum adapters run the SDK in an adapter-owned worker
  process: the Reticulum singleton, its native threads, and SDK exit hooks
  stay inside that process, keeping them away from the core event loop and
  the host's signal handlers (`medre-adapter-lxmf/docs/spec/native-worker.md`).
  The pinned RNS surface exposes no per-instance stop primitive, so session
  teardown never shuts down a shared Reticulum service; the worker process is
  the lifecycle boundary and is stopped cooperatively, then forcibly at the
  shutdown deadline. Windows descendant ownership is not qualified: leader
  termination alone does not establish process-tree cleanup there.
- MEDRE has no production transport-down subscription or automatic session
  reconnect trigger. Reticulum owns physical-interface reconnect; the same
  MEDRE router can remain active while an interface is offline and recovering.
  The pinned SDKs expose no transport-down event, so a silently lost transport
  is first noticed on the next outbound send. A successful local handoff can
  still queue undeliverable work; asynchronous SDK observations report
  delivery outcomes (`adapter.py::health_check` at the inspected pin).
- RNode physical-interface retry internals belong to the pinned RNS SDK and
  cannot be verified from the pinned provider repositories; this index makes
  no claim about detach/reconnect races inside the SDK. The provider's
  hardware profile (`medre-adapter-lxmf/tests/test_native_worker_hardware.py`)
  opens two consecutive workers against a private RNode profile and requires
  acknowledged clean exits; it explicitly establishes no message delivery,
  peer reachability, or data-plane qualification.
- Adapter health is local-scope only: `health_check()` reflects the
  MEDRE-owned session/router lifecycle (`healthy` only while the local
  router is connected and running, `degraded` while a worker status probe
  is delayed, `failed` when it is missing or torn down, `unknown` when not
  started). The pinned SDKs expose no supported peer-liveness API and MEDRE
  never probes merely to answer health, so `diagnostics()` reports
  `peer_reachability="unknown"` explicitly and separately; `healthy` is
  never a reachability claim.
- Cross-instance relation preservation over the `0xFD`
  (`FIELD_CUSTOM_META`) MEDRE envelope has a reproducible local
  two-process regression harness
  (`medre-adapter-lxmf/tests/test_lxmf_local_integration.py::test_relation_preserved_across_two_local_instances`,
  loopback Reticulum `UDPInterface` pair only). The harness belongs to the
  provider's local-integration tier, which core's required local-integration
  job runs against the pinned adapter revision; the latest recorded pass is
  dated 2026-09-18 gate evidence. It does not constitute external
  multi-hop, live-hardware, or third-party interoperability evidence.

### 2.5 XMPP

- Shutdown uses one budget for cleanup-lock acquisition and owned native work.
  A failed close or cancellation-resistant worker leaves an `ABANDONED` session
  with failed health and retained ownership. Reconnect, reuse, and fresh ingress
  remain fenced until a cleanup retry succeeds; asyncio cannot forcibly retire
  a callback that suppresses cancellation. The provider's session retirement
  regressions and two-account Docker Prosody test cover those local boundaries,
  not recipient reading or external-network reliability.

- The provider bridges live text only. XHTML, attachments, presence, read
  receipts, native mutations (edits/retracts/reactions), OMEMO, and
  puppeting are not implemented or advertised; unsupported
  edit/retract/reaction compatibility bodies are excluded from ingress
  (`medre-adapter-xmpp` README at the inspected pin).
- The live bridge does not recover offline messages or room archives: MUC
  joins request zero history, delayed history is excluded inbound, and no
  MAM/archive retrieval exists.
- The connection requires verified TLS and offers no plaintext or
  certificate-check bypass; transport encryption is not end-to-end
  encryption (the `slixmpp` SDK pinned in the provider's package
  metadata). Authentication rejection requires operator action.
- Outbound success is reported for local TLS transport writes, separately
  from recipient delivery/read state; the adapter claims no delivery or
  read receipts.
- Destination conversations are restricted to the configured `rooms` and
  `peers`; the adapter rejects destinations absent from that allowlist and
  does not infer rooms from server names. Oversized output is rejected
  without silent clipping (wire size measured with the pinned Slixmpp
  serializer).

### 2.6 Chatmail

- The native core owns SMTP/IMAP, cryptography, protocol retries, and
  account storage (the `deltachat-rpc-client` /
  `deltachat-rpc-server` SDK pair pinned). Attachments, native
  edits/deletes/reactions, forwarded read receipts, typing notifications,
  and puppeting are not advertised (`medre-adapter-chatmail` README at
  the inspected pin).
- Outbound success means admission to the provider's local queue; SMTP
  acceptance is reported only after the native core reports its sent
  state, and neither claims recipient delivery or reading.
- First use baselines existing history without relaying it; later starts
  resume from the last durable checkpoint, committed only after canonical
  admission. Partial text messages stay pending; self echoes, blocked
  contacts, informational controls, edits, and unsupported media are
  excluded from fresh-message ingress.
- Native replies use Delta Chat's quote UI only when core uniquely binds
  an earlier message to a native reference in the same private profile
  and chat; otherwise a readable inline reply is retained. Threads always
  use inline fallback.
- Unsent or uncertain work recovered across a restart is abandoned for
  inspection before an explicit retry; correlated native jobs are
  recovered without resending. Encryption/signature observations are
  exposed in native metadata — confidentiality across the bridge and
  identity puppeting are not implied.
- Conversation identity is the chat ID local to the profile; contact
  addresses are not used to guess conversation identity. The profile is
  dedicated to one adapter instance and locked exclusively by MEDRE
  commands.

### 2.7 SimpleX Chat

- The provider pins the official `simplex-chat` SDK (native library
  downloaded on first initialization). The SDK supports Linux, macOS, and
  Windows on its published architectures; CI qualifies Linux x86-64 only.
  The official SDK and native library are AGPL-3.0-licensed; this
  adapter's code is MIT (`medre-adapter-simplexchat` README at the
  inspected pin).
- Text is rendered with relay attribution only. Native quotes,
  attachments, edits, deletions, reactions, typing, and puppeting are not
  advertised: reply/thread fallbacks are plain text, and the native-quote
  candidate path remains capability `fallback` pending independent
  recipient-side qualification
  (`medre-adapter-simplexchat/docs/qualification/native-quoted-replies.md`).
- Received control items, sent echoes, blocked members, file captions,
  live partial messages, and private support chats are excluded from
  ingress.
- The first successful scan baselines history without replaying it.
  Restart recovery walks retained native history pages against a local
  processed-ID ledger; group history is timestamp-ordered, so late
  messages can carry higher IDs at earlier positions and recovery must
  not stop at a fully retained page. Startup cost and retained-ID storage
  grow with native history size; native history deleted or expired before
  recovery cannot be reconstructed.
- Live events use a native buffer bounded to 256 items and 1 MiB; overflow
  stops the adapter, and restart recovery re-reads retained messages from
  the native profile.
- Outbound acceptance confirms only the native local queue, not remote
  delivery. A conflicting attempt, uncertain native outcome, or different
  attempt for the same outbox requires inspection of the native profile;
  the adapter refuses blind resubmission. A rejection before any command
  is written releases the unsent reservation; a native rejection after
  submission is not proof of no handoff and remains uncertain.
- Health describes the local native session, not peer reachability. Group,
  member, and contact IDs are local to the dedicated profile — they are
  not global SimpleX identities — and unrelated SimpleX clients do not
  honor the profile's exclusive MEDRE lock.

## 3. Fire-and-Forget Model

No transport guarantees end-to-end delivery confirmation. An outbound
`deliver()` call that returns success confirms only that the message was
handed off to the local transport layer. It does not mean the message was
received by any remote party.

| Transport    | `success=True` means                                  |
| ------------ | ----------------------------------------------------- |
| Meshtastic   | Local radio accepted the packet (no acknowledgement is requested; see §2.2). |
| MeshCore     | Local radio accepted the packet.                      |
| LXMF         | Message was handed to the LXMRouter in the worker process. |
| Matrix       | Homeserver persisted the event and returned event_id. |
| XMPP         | Stanza was written to the verified TLS stream.        |
| Chatmail     | Item was admitted to the provider's local queue.      |
| SimpleX Chat | Native local queue accepted the submission.           |

This is an honest model. MEDRE reports what it knows (local handoff succeeded)
and does not pretend to know what it cannot verify (remote receipt).

## 4. Startup Backlog Suppression

| Transport    | Status      | Notes                                                                                                |
| ------------ | ----------- | ---------------------------------------------------------------------------------------------------- |
| Meshtastic   | Implemented | `startup_backlog_suppress_seconds` (default 5.0s in the provider config), `rxTime`-based, best-effort |
| MeshCore     | Deferred    | No message history, no store-and-forward. Suppressing live events would risk dropping fresh packets. |
| Matrix       | Excluded    | Sync protocol handles message ordering and gap detection.                                            |
| LXMF         | Deferred    | No reliable receive-time timestamps suitable for suppression.                                        |
| XMPP         | Excluded    | MUC joins request zero history; no offline-message or archive recovery exists to suppress.            |
| Chatmail     | Excluded    | First use baselines existing history; later starts resume from the last durable checkpoint.           |
| SimpleX Chat | Excluded    | First scan baselines history; restart recovery re-walks retained native history pages against a local processed-ID ledger. |

## 5. Protocol-Neutral Abstractions

The following abstractions are genuinely transport-neutral:

- `source_transport_id` as a string
- `NativeMetadata.data` dict
- `max_text_bytes` / `max_text_chars` capability declarations
- Adapter-owned pacing queues
- `AdapterHandoffResult` with transport-provided native ID extraction
- `AdapterRole` enum
- `IdentityResolver` native-to-canonical mapping

The following carry accidental assumptions from protocols with native reply
mechanisms:

- `EventRelation.target_native_ref` assumes the protocol carries a reply
  reference (true for Matrix, Meshtastic, and XMPP; Chatmail binds a native
  quote only when core uniquely verifies the earlier message in the same
  profile and chat; LXMF carries relations in the MEDRE `0xFD` envelope
  fields rather than a native reply reference; SimpleX Chat renders
  reply/thread fallbacks without advertising native quotes; false for
  MeshCore). Relations
  are capability-gated via `CapabilityDecisionResolver`; adapters that lack
  native support for a relation type produce `capability_suppressed` delivery
  outcomes.

## 6. Capability Semantics Known Gaps

1. **Thread fallback lacks live endpoint evidence.** Built-in profiles declare
   `threads="fallback"` and exercise the normal planner fallback path, but no
   `live_service` or `hardware`
   live-service/hardware scenario validates degraded thread output end-to-end.
   Native thread emission is intentionally not advertised.

2. **No hardware or live validation of capability suppression.** Most capability
   suppression, fallback rendering, and budget enforcement coverage remains
   synthetic even though the same production planner path is exercised.

3. **Replay same-run admission is atomic, but transport delivery is not exactly-once.**
   A non-empty replay run ID durably claims one outbox generation per delivery
   identity, so concurrent executions sharing one database cannot create sibling
   same-run dispatches. Distinct/empty run IDs remain repeatable, and ambiguous
   transport handoff followed by retry/recovery can still redispatch.

4. **Some replay pre-filter evidence remains in-memory only.** Same-run duplicate
   execution is represented by the durable outbox claim plus the replay result, not
   by a synthetic lifecycle receipt. Capability-filter diagnostics that never create
   a target receipt likewise remain part of the replay result only.

5. **Delivery evidence strength differs by transport.** Matrix synchronous hand-off
   normally proves homeserver acceptance. LXMF initial hand-off proves local
   LXMRouter acceptance inside the worker process, while later
   callback-emitted terminal provider states are persisted separately as
   post-handoff delivery observations. Neither mechanism implies
   end-to-end recipient acknowledgement; evidence levels are normative in
   [routing-delivery.md](../routing-delivery.md) §13.
