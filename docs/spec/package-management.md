# Explicit package management

MEDRE's package-management CLI is a deployment surface separate from runtime
configuration, provider discovery, and adapter contracts.

## Ownership and execution

Package-changing helpers MUST verify that `sys.prefix` identifies the persistent
`medre` environment beneath `uv tool dir`. They MUST NOT mutate a project,
ordinary virtual environment, pip installation, or temporary uvx environment.
They MUST invoke uv through an argument vector without a shell, stream its
output, and report unsuccessful execution without reporting success. On Windows,
helpers MUST instead print a preview and exit 2 without changing packages;
the operator runs the command after the MEDRE executable exits to release its
Windows file lock. If URL credentials or query strings are redacted, the preview
MUST NOT be represented as copyable; use direct uv management instead. A dry-run preview MUST exit successfully on every platform.
`--dry-run` MUST perform no package or configuration writes.

Adapter changes MUST preserve the core requirement unless the operator explicitly
selects core through `sources switch` or `adapters reset --include-core`
(see below). They MUST preserve every
unrelated explicit requirement from uv's receipt, including supported extras, markers, source refs,
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
for compatibility. Scoped updates MUST refuse unselected mutable Git, local
file, and directory requests that uv would re-resolve; they MUST NOT silently
advance them or leave synthetic overrides in the receipt. Unsupported saved
settings MUST cause scoped
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
Absent default files use built-in package-index requirements for first-party
adapters. Existing catalogs MUST NOT be migrated or overwritten implicitly.
An unresolved index request MUST fail through uv, not fall back to Git. Missing explicit files,
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

## Explicit source reset

`adapters reset [SELECTION ...]` MUST restore only selected installed first-party
adapters to built-in main sources. Without selections it MUST select all installed
first-party adapters. It MUST remove only the `integration` extra and preserve
other selected-adapter extras. Saved catalogs MUST remain unchanged; reset MUST
NOT consult them. Unknown and community selections MUST fail before execution.

`--include-core` MUST be accepted only with `adapters reset`. It MUST explicitly
replace the saved core requirement with MEDRE's built-in main source, preserving
core extras and unrelated requests. This opt-in MUST work without installed
first-party adapters. Without it, reset MUST preserve the recorded core source,
including a retired branch; a failed reset MUST report the explicit recovery
command without retrying or rewriting uv's request. Both forms MUST retain uv's
failure exit status (negative subprocess statuses map to 1), dry-run behavior,
and Windows executable handoff. Neither form guarantees that unselected mutable
sources remain at their previously resolved commits.

## Explicit source switching and inspection

`sources switch` MUST select only explicitly installed distributions from the
owned tool request. It MUST accept core independently of adapter discovery,
resolve adapter aliases through the catalog, and preserve selected extras,
conditional selection and all unrelated explicit requests. `--all` MUST select
core and explicitly requested first-party adapters, leaving community requests
unchanged. Missing packages, duplicate selections and incompatible selector
options MUST fail before installation. Per-project source options MUST select
exactly one distribution.

Index sources MUST delegate version selection and availability to uv. `pypi`
is an alias for index and MUST NOT override uv's configured index policy.
Git sources MUST support an explicit repository and revision; only built-in
first-party distributions may infer an upstream repository. Local sources MUST
support explicit snapshot or editable installation. Supported editable receipt
entries MUST retain their mode during unchanged request replay; an explicit
source replacement MUST NOT silently retain unwanted editable mode.

Successful switches MUST let uv record the installed request. MEDRE MUST NOT
rewrite source catalogs, uv receipts, project lockfiles, compatibility pins or
runtime configuration as a side effect. Reinstalling an alias or requesting
`--sync-sources` MAY deliberately reapply catalog choices. Source switching
MUST retain the ownership, scoped-source, failure, dry-run and Windows rules
above. Only an explicit `sources switch --allow-refresh` MAY permit uv to
refresh unselected moving sources; its preview MUST disclose that permission.
This opt-in MUST NOT bypass receipt-shape or ownership checks. A source change
MUST NOT be described as persisted-state rollback.

`sources show` MUST distinguish configured requirements, requested tool sources
and installed versions/direct-source metadata without importing providers or
contacting an index. The human-readable inventory MUST render each installed
source with the same compact provenance label as the startup summary; the
`--full` view MUST instead print the complete recorded source URL with the
full commit revision and editable marker, for hosts and layouts the compact
label abbreviates; the
`--json` inventory MUST retain the structured direct-source record for
scripts. `sources show` MUST remain usable outside an owned tool environment
and explain unavailable receipt evidence. An index installation without direct
source metadata MUST NOT imply independently verified index provenance.
A displayed Git commit or local path MUST NOT imply native qualification or
working-tree cleanliness. URL credentials and query secrets MUST be redacted
from the source inventory, catalog display, MEDRE-generated plan notes and
command previews. Redaction MUST NOT alter the arguments supplied to uv. uv
output is owned by uv; operators SHOULD use credential helpers rather than
embedding secrets in requirements. Installed provider metadata MUST be included
even when no catalog alias or supported tool receipt exists; an unreadable
provider metadata inventory MUST be reported without discarding known catalog
and receipt rows. An explicit installed distribution name takes precedence
over a conflicting catalog alias during source switching.

## Startup installation summary

`medre run` MUST report a best-effort installation summary at INFO in the
startup header, after the config and state paths and before runtime construction.
It includes the core version and source, Python version/system/architecture,
and a deterministic list of installed adapter entry-point transport/distribution
pairs (not just configured or enabled adapter instances). Core-owned entries
MUST be excluded by normalized distribution name; nonstandard distribution names
MUST be shown in brackets. The summary MUST NOT load provider implementations,
query an index, or change deployment state.

The source label reflects installed metadata only, as one compact token per
package: `pypi` means no direct URL record was found, `source` means core
distribution metadata is absent, Git labels name the recorded repository and
abbreviated commit (`github:owner/repo@`, `gitlab:group/repo@` or
`git:host/path@` for non-forge hosts), `hg:` and `svn:` show recorded
non-Git revisions, `editable:` shows a recorded local directory, `local:`
shows a recorded local snapshot, and `url:` shows a direct source location.
Invalid, inconsistent or unsupported metadata
MUST yield `unknown`; a missing, unreadable or unsafe-to-display version
MUST yield `?` independently. The source-tree core version fallback is not an installed version.
These labels establish neither index identity, source-tree cleanliness, exact
installed bytes, nor qualification. URL userinfo, query values and fragments
MUST NOT appear in the startup summary, including for non-HTTP schemes; Git
labels are derived from the validated URL's host and path only. Paths
and hostnames remain visible, so operators MUST NOT embed credentials in paths
and SHOULD review logs before sharing them. Invalid metadata MUST NOT be logged
verbatim or prevent startup; an incomplete provider inventory MUST be indicated
without suppressing healthy entries already discovered. This text is not a
versioned evidence or snapshot schema.

`medre adapters` MUST list the same installed identities (transport, version
and provenance label) whenever adapter distributions are installed, alongside
the transport/SDK availability catalog. `medre version` MUST include the core
provenance label. All three surfaces share one label formatter; none of them
MUST import provider implementations or contact an index to produce it.
