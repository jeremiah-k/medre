# Versioning boundaries (pre-first-release)

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

Package tags need not be minted to establish the adapter API version. When
publishing first installable releases, follow PEP 440 and use independent,
monotonically increasing distribution versions. An already-published `0.1.0`
must not be followed by `0.1.0a1` (which sorts earlier under PEP 440).
Until then the exact-commit manifest supplies
reproducibility; no broad package-version constraint is a compatibility proof.

**Prerelease databases:** Until a first supported state baseline is declared,
a different SQLite catalog is unsupported development state and must be
explicitly replaced or exported by the operator. After a supported baseline,
forward-only additive database migrations may be introduced deliberately,
with tested failure/rollback behavior and clear support boundaries. There is
no obligation to migrate abandoned, unpublished prerelease shapes. Schema
version increments then track supported persisted-state changes, not Git
commits, adapter API changes, or package releases.
