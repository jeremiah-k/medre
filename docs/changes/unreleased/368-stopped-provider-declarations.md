# Derive stopped-state inventories from provider declarations

Add `medre.maintenance.stopped_declare`: the trust boundary and consumers for
provider-owned stopped-state declarations. Each adapter distribution ships a
declaration module — SDK-free, configured-paths-only, never opening a profile
or starting a process — referenced from its adapter spec through the new
optional `stopped_state_declaration` symbol. Core resolves the reference
lazily and keeps its own core database declaration in core.

The new maintenance `plan` subcommand loads the configuration (including
environment overrides), asks each configured provider to resolve its own
paths, and writes a version-1 inventory; the `check` subcommand acquires each
provider's ownership primitive, refuses if already owned, and releases it when
the command exits. Retaining exclusion across a capture requires one process
using the library API, so
no local owner can start between the check and the capture.

Selections stay closed and honest: required files must live inside the common
state root, conflicting ownership of one resolved file is rejected, and a
distribution that ships no declaration — or one that fails to load — reports
as unsupported instead of being silently omitted. Providers whose state
cannot be captured by files — encrypted native profiles, non-enumerable blob
stores, device firmware, credentials outside the root — are represented as
externally managed, device-attached, or stateless with explanatory notes
rather than invented files. Locally held lock bytes are never treated as a
live lock after restore, and machine-local exclusion is never advertised as
proof that another host with the same native identity is inactive. Capture,
verify, and restore remain maintenance operations without sends, and
uncertain native handoff states are preserved byte-exactly across a restore.

Inventory publication preserves a destination created during staging. SQLite
exclusion checks require an existing regular lease and do not create missing
ownership files. Releasing a primitive also clears its retained-exclusion claim.
