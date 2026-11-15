# Align developer docs with implemented content authority

- `docs/dev/development-priorities.md` records terminal-owner settlement, the
  read-only release assessment and guarded opt-in relay-only collection as
  implemented, keeps automatic and periodic collection disabled, and replaces
  the blanket migration prohibition with the allowlisted-additive policy for
  the `expired_at` grant field and the collection-receipt table.
- `docs/dev/content-lifecycle.md` replaces the inline-payload description with
  the implemented canonical content separation, transactional settlement,
  owner-scoped reads and isolated-content collection, keeping retention
  profiles and plugin holds as the remaining design scope.
