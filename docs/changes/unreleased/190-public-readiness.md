# 190: Public readiness — operator path, pinned SDKs, recovery surface

User-visible changes in this buildout, ahead of a first release:

- **MeshCore inbound identity** is now derived deterministically, so
  messages no longer collide when they arrive within the same second.
  Identical payloads from the same node in the same second remain ambiguous:
  stable deduplication keys cannot tell a true duplicate from a re-send in
  that window.
- **Adapter identifiers** follow one portable path-safe rule (start with a
  letter or digit; then letters, digits, dots, hyphens, underscores; no
  trailing period or Windows-reserved device basename). Invalid IDs,
  duplicates across transports, and environment-override token collisions
  now fail at `medre config check` or early startup with a clear error,
  before any state directory is created.
- **Sample config fix**: the generated sample's active route no longer pins
  a Matrix `source_room` and now accepts `message.created` and
  `message.text`. Previously the documented installed-package journey
  (`medre config sample` → `medre smoke --config <sample> --json`) could
  never pass because the sample policy suppressed every smoke delivery.
- **Recovery surface**: `medre recover` now reports each logical delivery's
  current outcome by durable receipt append sequence — a later successful
  retry or executed replay supersedes an earlier failure of the same
  delivery even when attempt numbers are irregular. Output is paginated (`--limit`,
  keyset `--cursor`) and can be bounded by `--since` (inclusive; an
  explicit UTC offset is required). The nonfunctional `--failed-only` and
  `recover --dry-run` flags are gone; previewing a replay uses
  `medre replay --mode dry_run --config <config>`, which records no
  receipts. Recovery pages are a live view, not a snapshot. Read-only scans
  do not write the database, though SQLite may materialize `-wal`/`-shm`
  sidecars.
- **LXMF health scope**: adapter health reflects the local session and
  router (lifecycle, callbacks, storage); it cannot observe peer
  reachability. A two-process loopback probe at the declared pinned SDK
  versions proves real-router lifecycle, cross-process
  relations, and local health. No external mesh, hardware, or
  interoperability claim is made.
- **Install provenance**: optional transport SDKs are installed only
  through the pinned extras (`pip install -e ".[lxmf]"` and siblings).
  `pyproject.toml` is the single declared SDK-version authority; current
  docs/source/tests derive expected versions from it instead of copying pin
  literals. Earlier unpinned `pip install rnspure lxmf` guidance is removed.
  A new repeatable artifact check —
  `python scripts/check_installed_package.py` from a source checkout
  (optional `--wheel PATH` for an already-built wheel) — verifies the wheel
  build, a clean core install, and the installed package. With build isolation
  disabled, the proof first verifies the installed build backend against the
  exact `build-system.requires` declaration and removes stale setuptools
  source-build outputs before building; CI runs the proof once after
  coverage/test-result uploads.
- **Documentation**: the README now carries one reading path — core
  install, sample, check, smoke, then transport selection — and
  recovery/replay examples use the real command surface. Transport pages
  state current evidence tiers and keep dated historical records as
  history.

- **Recovery output safety**: sanitize persisted receipt error text at the
  `medre recover` operator-output boundary while preserving raw durable evidence.

- **Local LXMF isolation**: pin loopback probes to explicit per-instance
  Reticulum configuration directories before adapter startup.

Compatibility: storage schema versions are unchanged; no migration is
provided or implied. Historical evidence records keep their original dates
and scope.
