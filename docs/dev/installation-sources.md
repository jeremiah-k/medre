# Installation sources and channel planning

A package version, a source choice, and a qualified stack are different facts.
Core and adapters can all report `0.1.0` while editable and Git installations
contain changes beyond that release. Keep requested sources and resolved
provenance separate when reporting a run.

## Supported source choices

| Choice                | Existing mechanism                           | Reproducibility                                                  |
| --------------------- | -------------------------------------------- | ---------------------------------------------------------------- |
| Released packages     | Named index requirements in a source catalog | Exact versions plus a deployment lockfile and artifact hashes    |
| Upstream development  | Git requirements on `main`                   | Moving intent; record the resolved full commit                   |
| Candidate or fork     | A Git URL with a full commit, tag, or branch | Exact commits are reproducible; branches move                    |
| Local development     | `medre dev sync --editable ALIAS=PATH`       | Working tree, commit, dirty state and interpreter matter         |
| Curated compatibility | Committed adapter compatibility manifest     | Reviewed merged adapter commits against the selected core commit |

The [package manager](../ops/package-management.md#choose-your-own-sources)
already accepts index, Git and local requirements. A source catalog can set an
explicit core requirement; only an explicit `upgrade --sync-sources` applies it.
Adapter installation uses the selected catalog requirement. Editing a catalog
does not change an environment until an installation command applies it.

For source development, core stays editable at the checkout used by `medre dev
sync`. A local profile can point adapters at arbitrary sibling or fork
checkouts. These selections do not edit CI compatibility pins. See the
[development environment](environment.md) for profile and test commands.

## Released packages

Install the published core into a persistent tool environment with an explicit
version, for example `uv tool install 'medre==0.1.0'`. Select published adapter
versions in a separate source catalog rather than assuming built-in aliases
select PyPI. Built-in adapter defaults and `adapters reset` currently select
upstream Git `main`, even when core itself came from PyPI.

```yaml
core: medre==0.1.0
adapters:
  matrix: medre-adapter-matrix[e2e]==0.1.0
  meshtastic: medre-adapter-meshtastic==0.1.0
  lxmf: medre-adapter-lxmf==0.1.0
```

This partial catalog replaces only the listed aliases; other built-in aliases
retain their defaults. Select the named adapters explicitly. Do not describe
a mixed index/Git environment as an entirely released stack. Only select
versions actually published on the configured index. Stop bridges before
changing sources and inspect the management command's dry run first.

## Channel work to plan

Named channels can be conveniences over source catalogs, not a second package
resolver or runtime configuration. A future released channel should select
published versions only and explain missing providers. A development channel
should preserve moving-source intent while recording exact resolved commits.
Fork URLs and local editable paths remain explicit per-package overrides.

Switching should preview the complete core/adapter request, preserve deliberate
extras and unrelated packages, and show which requirements change. Switching
sources is not a state rollback; database migration compatibility and backups
remain separate concerns. Preserve the existing uv-tool ownership checks and
Windows handoff when extending management commands.

Qualification should record requested requirements, resolved distribution
versions, Git URLs and commits or index artifact hashes, editable locations and
dirty state, interpreter, SDK versions and the relevant lockfile. A channel
name alone is not qualification evidence. Core's curated compatibility manifest
continues to contain merged Git revisions; it is not a list of released wheels.

No named-channel flag or automatic default-source change is implemented by
this tooling update. The next product change should build on source catalogs,
add source-switching regressions, and qualify the actual published artifact set.
