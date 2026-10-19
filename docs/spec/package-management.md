# Explicit package management

MEDRE's package-management CLI is a deployment surface separate from runtime
configuration, provider discovery, and adapter contracts.

## Ownership and execution

Package-changing helpers MUST verify that `sys.prefix` identifies the persistent
`medre` environment beneath `uv tool dir`. They MUST NOT mutate a project,
ordinary virtual environment, pip installation, or temporary uvx environment.
They MUST invoke uv through an argument vector without a shell, stream its
output, and report unsuccessful execution without reporting success. On Windows,
helpers MUST instead print the uv command and exit 2 without changing packages;
the operator runs the command after the MEDRE executable exits to release its
Windows file lock. A dry-run preview MUST exit successfully on every platform.
`--dry-run` MUST perform no package or configuration writes.

Adapter changes MUST preserve the core requirement and every unrelated explicit
requirement from uv's receipt, including supported extras, markers, source refs,
and version constraints. Unsupported receipt settings MUST cause refusal before
installation rather than silent loss. A URL source with an existing fragment and
a separately recorded subdirectory MUST be refused; two fragments can change
archive hash and installer semantics. MEDRE MUST NOT write uv's receipt itself.
Bulk updates MUST delegate to `uv tool upgrade medre`, retaining uv's recorded
source constraints and installer settings. `upgrade` and `update` MUST be aliases.
Scoped updates MUST replay the complete supported request through `uv tool install`
with `--upgrade-package` for selected distributions. They MUST NOT use the bulk
upgrade operation to claim selective behavior, mutate site-packages directly,
or persist temporary version/source pins. Resolution MAY adjust shared dependencies
for compatibility. Scoped updates MUST refuse unselected mutable Git/directory
requests that uv would re-resolve; they MUST NOT silently advance them or leave
synthetic overrides in the receipt. Unsupported saved settings MUST cause scoped
or source-changing operations to refuse before mutation; native bulk updates
remain available.

Adapter updates MUST select only installed provider distributions using entry-point
metadata without loading their implementations. Explicit selections MUST NOT
silently install another package. No installed adapters MUST produce a successful
no-op for adapter-only updates. Package-management dispatch MUST bypass adapter CLI
imports so a broken provider can be updated or removed. Package changes require
a process restart before adapter discovery reflects them.

## Source catalog

The catalog conforms to [the source schema](../schemas/package-sources.schema.json).
It is separate from bridge YAML. `sources.yaml` resolves beside MEDRE's config
file, honoring MEDRE_HOME and XDG paths, with an explicit `--sources` override.
Absent default files use built-in convenience sources. Missing explicit files,
unknown top-level fields, and duplicate YAML keys MUST be rejected.

Operator aliases override built-in aliases; null removes an alias. Aliases name
Python distribution requirements, not runtime transport names. Core MUST NOT be
selectable as an adapter. The catalog MUST NOT limit provider discovery, trigger
network access at startup, or be populated from CI's compatibility snapshot.
Ordinary updates MUST retain installed source intent. `--sync-sources` MUST be
explicit to apply catalog requirements to selected installed packages, retaining
installed extras and every unrelated explicit request. Optional `core` MUST name
the medre distribution and MUST NOT affect adapter-only operations. Conflicting
aliases for a selected distribution MUST be rejected. Catalog-only providers MUST
NOT be installed implicitly and removed aliases MUST NOT uninstall packages.
Removal by explicit installed distribution name MUST remain possible when its
alias is absent. No runtime YAML changes or automatic service restart are implied
by package updates.
