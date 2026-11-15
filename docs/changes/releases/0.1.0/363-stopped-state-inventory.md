# Add explicit stopped-state inventory and maintenance commands

Require a closed operator-declared file selection for stopped-installation
snapshots. The separate maintenance command captures SQLite WAL state,
verifies selected native and journal files, and restores only to a new
installation path. Providers and role declarations are checked, including
one core database. File omission, native process death and restore-time
identity ownership remain operator responsibilities; no runtime or SDK is
started and no delivery attempt is authorized.

Inventory loading shares the snapshot reader's bounded regular-file checks and
refuses replacement symlinks at open time where the platform supports no-follow
opens. The versioned inventory schema documents selection and ownership rules
without certifying SDK completeness.
