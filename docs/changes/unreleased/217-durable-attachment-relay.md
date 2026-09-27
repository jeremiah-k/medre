# Durable attachment relay: neutral descriptors, content-addressed storage, bounded Matrix media

MEDRE now carries primary attachments as transport-neutral descriptors with
opt-in durable byte retention: adapters admit honest `message.file` payload
descriptors, core stores verified plaintext bytes content-addressed in SQLite
under explicit quotas, and Matrix gains bounded authenticated media ingress
(with encrypted-media verification) plus native `send_media` egress that
always uploads fresh bytes to the destination — while restart/replay reads
only retained bytes and disabling the policy stops all transfer, including of
previously stored files.

- New canonical attachment descriptor (module
  `medre.core.events.attachments`): one validated descriptor per
  `message.file` event under the payload key `attachment`, with `kind`
  (`image`/`audio`/`video`/`file`), filename, MIME type, optional
  dimensions/duration, and exactly one exclusive state — **retained**
  (`content_ref = sha256:<64 lowercase hex>`, `size_bytes` = measured
  length) or **unavailable** (one of ten stable, secret-free reason codes:
  `policy_disabled`, `history_suppressed`, `oversized`, `malformed_source`,
  `integrity_failed`, `unsupported_source`, `quota_exceeded`, `not_retained`,
  `content_missing`, `fetch_exhausted`). A third, wire-**declared** form exists
  only in flight between adapter decode and admission and never persists. Transport
  provenance (MXC locators, wire `file` objects, encrypted-media key
  material) never enters the descriptor; adapters keep it in their own
  versioned `metadata.native.data` namespace.
- Content-addressed SQLite storage: `attachment_blobs` (one row per unique
  retained byte string; `INSERT OR IGNORE` dedup) and
  `event_attachment_associations` (one primary attachment per event, the
  only load authority). Admission of event + native ref + ingress work +
  blob + association is one atomic transaction. Content identity
  (SHA-256) and length are always computed from the supplied bytes —
  declared sizes are evidence, never authority. Quotas count unique
  retained bytes only; dedup never double-counts. Oversized and
  quota-exceeded content still admits the event with an honest unavailable
  descriptor; nothing is evicted — there is no eviction, GC, or TTL, and
  raising `max_retained_bytes` is an operator action. Loading is
  association-scoped and re-verifies digest + size on every read
  (`association_missing` / `content_missing` / `integrity_failed`);
  failures never refetch, substitute, or fall back. Existing
  schema-version-1 databases gain the tables automatically via
  `CREATE TABLE IF NOT EXISTS` (they are intentionally absent from the
  pre-release shape guard); no rows are backfilled.
- New configuration section `attachments` (`AttachmentConfig`,
  `MEDRE_ATTACHMENTS__<FIELD>` env overrides), **disabled by default**:
  `enabled=false`, `max_attachment_bytes=10485760` (10 MiB),
  `max_retained_bytes=268435456` (256 MiB),
  `max_concurrent_transfers=2`, `transfer_timeout_seconds=60.0`.
  Validation rejects non-positive or non-finite numbers, booleans posing as
  integers, per-attachment caps above the retained budget, and concurrency
  below 1. The disabled policy also blocks outbound transfers of previously
  stored files.
- Durable binary ingress: adapters supply bounded, verified (and, for
  encrypted sources, decrypted) plaintext through `InboundAttachmentContent`,
  separate from the persisted envelope. Transient acquisition failures stay
  retryable through `DurableIngressDeferredError`; permanent media problems
  admit descriptor-only without poisoning the sync cursor. Restart/replay
  reconstructs outbound transfers from retained bytes only — there is no
  source refetch path.
- Matrix media ingress is bounded and authenticated: decision gates run
  before any network request, the locator is validated before use, and the
  download hits the authenticated media endpoint of the configured
  homeserver only — token in the `Authorization` header, redirects never
  followed, stream capped at `max_attachment_bytes`. Encrypted media are
  structure-validated (`v2`/JWK/`A256CTR`) and ciphertext-SHA-256-verified
  with the pinned SDK before decryption; no new cipher code exists.
  Upload keys/IVs never enter canonical events, receipts, outbox, logs,
  diagnostics, or envelopes; retained bytes are plaintext at rest, and
  `content_ref` is a local integrity digest only.
- Matrix media egress renders a closed `send_media` operation
  (`_matrix_operation`) carrying the destination wire template and the
  event's `content_ref` — no bytes, no keys, and never a forwarded source
  MXC. Delivery loads retained bytes through the association-scoped seam
  under one runtime-wide transfer permit and uploads fresh bytes to the
  destination homeserver: ordinary upload for plaintext rooms,
  client-side encryption for encrypted rooms (transient keys discarded
  after the one wire event). Unavailable descriptors fail closed with
  `attachment_unavailable:<reason>`; a crash between upload and send may
  orphan an unused remote upload (no upload-idempotency state, by design).
- Media edits are rejected: an edit whose content is a media msgtype, or
  whose bound target is a stored `message.file` original, fails closed with
  the stable `attachment_edit_unsupported` reason. Native edits remain
  text-only.
- Runtime seam: `AdapterContext.attachments` bundles the immutable policy
  snapshot, runtime-wide transfer permits (`max_concurrent_transfers`,
  closed at shutdown so in-flight transfers finish under their deadline and
  no new acquisition starts), and association-scoped content access.
- Hardening from adversarial review: transient Matrix attachment acquisition is
  capped at three attempts using a restart-persistent counter keyed by stable
  native Matrix identity; exhaustion admits descriptor-only as
  `fetch_exhausted` instead of leaving one event pending forever. Malformed
  encrypted-media key/IV/hash values classify as `malformed_source`; disconnected
  Matrix media fetches stay inside that same transient budget; retry-state
  cleanup cannot replace the original admission failure; fake Matrix delivery
  rejects an explicitly disabled attachment policy; and oversized integer
  transfer deadlines fail configuration validation instead of leaking an
  `OverflowError`.
- Hardening from adversarial review: quota-rejected bytes are never written or
  associated behind an unavailable descriptor; zero-byte attachments retain
  normally with measured size `0`; a transfer waiter that has not acquired a
  permit before shutdown cannot start after the gate closes; and Matrix media
  upload HTTP 5xx responses plus exhausted network failures remain transient
  even when nio uses `M_UNKNOWN` or raises a transport exception.
- The Matrix capability profile now advertises `attachments: true`
  (spec table updated to match) with the policy gate, bounded-download
  model, and edit rejection documented next to the claim. Other transports
  keep their existing behavior.

Explicit exclusions, all documented in the specs: LXMF and other
non-Matrix transports gain no attachment handling; only one primary
attachment per event is modeled (no multi-file); media edits remain
unsupported; there is no transcoding, thumbnailing, or thumbnail transfer;
and retained bytes have no TTL, GC, or eviction — quota capacity changes
are operator actions. Verification for this change is recorded in the
implementation handoff.
