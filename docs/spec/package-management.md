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
installation rather than silent loss. MEDRE MUST NOT write uv's receipt itself.
Updates MUST delegate to `uv tool upgrade medre`, retaining uv's recorded source
constraints and installer settings. Package changes require a process restart
before adapter discovery reflects them.

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
No package source or runtime YAML changes are implied by an update.
