# Content-safe read mediation and exact native dispatch claims

- Ordinary canonical event, relation, timeline and replay-selection reads now
  expose metadata only. SQLite projects safe columns instead of decoding
  opaque payload/native metadata and discarding it afterward.
- Explicit replay reads require an existing live source hold and cannot
  renew retention or bypass expiry. Missing content skips dependent stages,
  and events admitted without body or excerpt bytes stay fully replayable.
- Relation enrichment quotes target content only while the target's
  admission hold is live and otherwise renders an honest missing-quote
  fallback.
- Evidence bundles withhold free-form rendering, error and capability text;
  authoritative persisted recovery state remains unchanged.
- No physical collection, database migration, release tag or adapter API
  version change is part of this prerelease slice.

All full canonical projections now have admission dependencies, including
opaque bodyless payload and metadata. Reply context has a dedicated typed read;
replay rechecks each content-dependent stage. Operator evidence, receipt,
native-ref, recovery and radio inspection withhold content in SQL. No owner backfill or automatic collection runs.
