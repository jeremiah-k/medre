# Installation sources and provenance

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

## Released packages and development sources

In a core build containing source switching, new first-party adapter requests
use index packages, with PyPI as uv's default index. Existing source catalogs
are preserved and may explicitly retain Git.
The legacy `adapters reset` command deliberately selects upstream Git `main`;
it is not a stable-release selector.

The published 0.1.0 core does not recognize `sources` or inline adapter version
constraints. Once this feature lands on main, install that Git revision (or a
later published release containing the feature) before following these examples.
Git is required for the development installation.

```bash
uv tool install 'git+https://github.com/jeremiah-k/medre.git@main'
medre adapters install 'matrix[e2e]==0.1.0'
medre sources show --json
medre sources switch --all --from index --dry-run
medre sources switch core --from git --ref main --dry-run
medre sources switch core --from local --path /work/medre --editable --dry-run
```

Use index, Git and local checkout as source kinds; a version or Git revision is
a separate selection. `pypi` is a convenience alias for index, respecting uv's
index configuration. A fork is an explicit Git URL, not a separate channel.
A local checkout can be an installed snapshot or editable. See
[source switching](../ops/package-management.md#switch-installed-package-sources)
for execution, scopes and complete examples.

## Requested intent and resolved state

Source catalogs configure aliases and explicit synchronization preferences.
A switch preserves them and delegates the installed request to uv's receipt.
Plain upgrades retain that receipt; reinstalling from a catalog or requesting
`--sync-sources` deliberately reapplies catalog intent. A successful switch is
not an update to deployment lockfiles or CI compatibility pins.

`medre sources show` distinguishes configured choices, the supported tool
receipt's requested requirements, and installed metadata. It works without a
managed tool receipt, imports no providers and performs no index query. For Git
installs, direct-source metadata exposes the resolved commit. For local
editables, the directory remains live and its contents can change after
installation; a source location is not proof of clean or qualified bytes.

Qualification must independently record requested requirements, installed
versions, resolved Git commits or index artifact hashes, editable working-tree
commit and dirty state, interpreter, SDK versions and the deployment lockfile.
A source label alone is not qualification evidence. Core's curated compatibility
manifest continues to contain merged Git revisions; it is not a list of
released wheels. uv owns locking for deployment projects, and MEDRE neither
writes `uv.lock` nor implements a second resolver or version-availability service.
