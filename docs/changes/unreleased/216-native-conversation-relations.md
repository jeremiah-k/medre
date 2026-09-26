# Native conversation relations: edits, deletes, threads

MEDRE now binds relation targets to destination-local native identities through
one platform-neutral authority and uses it to drive native Matrix message
edits, redactions of MEDRE-owned copies, and native threads — while preserving
existing native replies/reactions and MMRelay fallback behavior.

- New core authority `medre.core.planning.relation_binding.RelationBindingAuthority`
  resolves a canonical relation target into the exact destination adapter
  instance + destination context. Binding accepts exactly one distinct stored
  native tuple after deduplication; multiple distinct copies (retry/replay
  duplicates) are `ambiguous` and never guessed. Two rooms on one adapter, and
  two instances of the same platform, are strictly isolated; native-only
  relations cannot bypass scope guards. Canonical event IDs remain canonical
  and stored originals/relations stay immutable.
- `EventRelation` gains one core-computed field, `target_fact`
  (`RelationTargetFact`): an immutable resolved fact with statuses
  `bound` / `bound_owned` / `unresolved_target` / `out_of_scope` / `ambiguous` /
  `not_authorized` / `binding_unavailable`. Facts are derived exclusively from
  stored canonical events and `NativeMessageRef` records — never from
  user-supplied relation metadata (e.g. `original_sender`) or Matrix-native
  keys interpreted by core. Codecs never populate it; it is never persisted
  into `canonical_events`.
- Mutation eligibility (edits/deletes) fails closed. A native mutation is
  authorized only with: a resolved stored original canonical event; identical,
  non-empty original source adapter / actor identity / source context between
  original and mutation; and exactly one distinct stored OUTBOUND native copy
  in the actual destination adapter/context. Inbound originals, cross-actor,
  cross-origin, wrong-context, missing, or ambiguous targets — and any storage
  read failure (`binding_unavailable`) — produce explicit non-success evidence
  with stable reason codes (`relation_target_not_bindable:<status>`): no
  transport mutation call, no fallback ordinary message pretending to be an
  edit/delete, and no successful sent receipt or native ref. Replay/retry
  re-binds at execution time and can never retarget an operation.
- Matrix outbound lifecycle is now a closed envelope: every native render
  emits a `MatrixOutboundOperation` (`send_event` / `redact_event`) under the
  single adapter-local `_matrix_operation` payload key, strictly validated by
  the adapter; the old `_matrix_event_type` magic key is removed. The renderer
  owns protocol request construction, the adapter dispatches the closed
  operation, and the session remains the sole SDK owner.
- Native edits: spec-valid `m.replace` + `m.new_content` (escaped formatted
  body, exactly-once relay attribution, `"* "` fallback body) targeting the
  ORIGINAL message's destination copy — never the previous edit's event ID.
  Each edit's returned native ID is recorded on its own canonical mutation
  event. Bound reply/thread semantics are preserved in `m.new_content` rather
  than edited away. Edits are text-only; attachments remain unsupported.
- Native deletes: a real pinned-SDK `room_redact` request
  (pinned `mindroom-nio`) against the proven owned destination copy, with
  deterministic transaction identity (operation kind + redaction target folded
  into the txn derivation), shared rate-limit/cooldown/permanent-error
  ownership, and shared handoff validation. A redaction is an append-only
  fact; stored originals are never erased. Source-author deletion of a
  relayed reaction targets the relayed reaction's owned copy.
- Native threads: `m.thread` rooted at the correctly bound destination root
  with spec-compliant fallback-parent semantics (`is_falling_back`), explicit
  reply-in-thread parents resolved separately from the root, order-independent
  rendering, and honest degradation (plain message, no `m.relates_to`) when
  the root cannot be bound. Inbound Matrix thread events now carry both a
  `thread` relation (root) and a `reply` relation (explicit parent) at the
  codec seam; graph root-selection rules are unchanged.
- Matrix capability profile now advertises `edits: native`, `deletes: native`,
  `threads: native` with mutation-eligibility and unresolved-target behavior
  documented next to the claims; attachments remain unsupported. Other
  transports keep their existing fallback/unsupported outcomes. m.room.redaction
  and m.reaction are intentionally plaintext event types; edits follow normal
  room-message encryption (no plaintext leak into encrypted rooms). No crypto
  secrets are persisted.

General cross-origin moderation/ACL federation is explicitly out of scope and
documented: a redaction-shaped source payload from another actor never
authorizes mutation. No persisted storage schema change was required.
