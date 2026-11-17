# Versioning boundaries

MEDRE keeps three independent identities. Avoid translating one into another.

| Identity                    | Example                               | Owner                                 | Meaning                                                                              |
| --------------------------- | ------------------------------------- | ------------------------------------- | ------------------------------------------------------------------------------------ |
| Python distribution version | `0.1.0`, then a newer PEP 440 release | Each core/adapter/plugin distribution | PEP 440 packaging, dependency resolution, releases and tags.                         |
| Adapter API version         | `AdapterSpec.api_version = 1`         | MEDRE core; declared by each provider | Major version of the SDK-free provider interface: registration and runtime contract. |
| SQLite schema version       | `schema_version = 1`                  | MEDRE storage                         | Compatibility of persisted database layout and data.                                 |

The adapter API version is an **integer major compatibility generation**, not a
monotonic counter of every new field or feature. Backwards-compatible additive
provider capabilities can remain within API version 1 when they have explicit
optional defaults and are safe for an older version-1 provider. A new provider
which uses an additive field may still require a **newer core package release**:
its distribution must raise its minimum `medre` dependency to the first core
release providing the field. The major API version alone cannot encode that
minimum feature level. A breaking
interface change requires a coordinated API-version change and qualifying
providers. Exact cross-repository commit pins remain the integration test
source of truth; an API-version match is necessary, never sufficient.

Provider registration must declare the literal API version it implements.
It must not read the current core constant to populate that declaration: that
would hide mismatches instead of reporting them. Incompatible declarations fail
SDK-free discovery before native modules are imported. Omitted, malformed or
unsupported declarations are refused without an implicit default. Separate plugins should
adopt their own explicitly named compatibility contract when the plugin runtime
is implemented; do not reuse the adapter API version by accident.

Package tags need not be minted to establish the adapter API version. Core
and some providers have published `0.1.0`; publication remains independent
per distribution. Follow PEP 440 and use monotonically increasing distribution
versions. Core's release workflows accept canonical public three-component
versions (for example `0.1.1`, `0.1.1rc1` or `0.1.1.post1`), optionally prefixed
by `v` on the tag, matching `project.version` exactly. Mark prerelease versions
as prereleases in GitHub; development image tags use a separate `dev`
namespace. An already-published `0.1.0` must not be followed by `0.1.0a1`
(which sorts earlier under PEP 440). The exact-commit manifest supplies
source reproducibility; no broad package-version constraint is a compatibility
proof.

**Persisted databases:** The tagged 0.1.0 SQL snapshot identifies the released
baseline. The current writable initializer recognizes only explicitly declared,
structurally exact additive predecessors. Other incompatible development
catalogs are refused with an operator recovery or reset decision. See the
[storage policy](../spec/storage.md#10-database-compatibility-policy). Future
supported database layout changes identify their source and target catalogs
and include an explicit forward-only migration with data-preservation,
failure/rollback and restart tests. Unchanged catalogs reopen without
transformation. Compatibility review defines each change's supported upgrade
and recovery paths; it does not make arbitrary development layouts supported.
Schema version increments track supported persisted-state changes, not Git
commits, adapter API changes, or package releases.

## 0.1.0 schema compatibility baseline

The first **tagged** core `0.1.0` release defines the supported canonical
event, operational JSON/YAML, and SQL schema snapshots named in
`docs/schemas/v0.1.0.freeze.json`. Independent adapters freeze their native
metadata shapes under their own distributions; the Matrix namespace remains
`matrix.schema_version = 1` and is not the core `CanonicalEvent.schema_version`
or a distribution version. A new release that deliberately changes any public
schema must document whether it is additive, requires a new schema generation,
or requires a migration. The snapshots guard releases; they must not be edited
to make an unexpected CI failure disappear.

The current checkout can still report `0.1.0` while containing post-tag
changes. Distribution metadata alone does not identify a published artifact
or qualify a mixed source stack. Preserve the frozen released baseline and
review differences against exact tagged artifacts. Abandoned experimental
shapes from before the first release are not automatically supported migration
baselines.

The current source configuration schema lives under `docs/schemas/current/`;
the released top-level snapshots remain immutable. The initial current adapter
schema adds the optional Meshtastic startup drain setting without changing
canonical event, native metadata or persisted-state generations. Source drift
is checked against that current schema, and release packaging retains both
the baseline and current artifacts.
