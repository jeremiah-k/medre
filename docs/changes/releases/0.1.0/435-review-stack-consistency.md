# Align stacked context ownership, retry pacing, and snapshot fakes

- Recover optional receipt capability facts through one exact-owner snapshot
  reader across deferred failure and handoff completion. Retain the historical
  inline metadata precedence; log snapshot read failures with their causes
  while still recording outcomes already accepted by a transport.
- Defer an adapter lane after a transient snapshot-read failure, preserving the
  unstarted claim release without repeatedly reclaiming the locked row.
- Keep in-memory lifecycle test storage aligned with SQLite snapshot creation,
  exact-identity reads, terminal retirement, and rollback of rejected creation.
  Accept a deferred completion with no native message ID, as permitted by the
  production finalization contract.
- Pin both staged and direct snapshot inventories, split reconstruction and
  capability receipt tests into a separate behavioral module, and clarify the
  final post-stack blocker baseline in historical change fragments.
- Ignore transient CodeRabbit review output rather than tracking local
  analysis artifacts at repository root.
