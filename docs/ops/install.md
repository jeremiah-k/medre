# Installation Reference

For a first real bridge, use [Getting Started](getting-started.md). It includes
Package installation, a local smoke, credentials, and a two-way route. This page
covers the **other installation methods** and how to verify them, without
maintaining a second copy of the operator tutorial.

MEDRE requires Python **3.11 or newer** and is licensed **MIT** (MEDRE-owned core source; adapters and SDKs have separate terms). Its
console entry point is `medre.cli:main` (the command is `medre`). No transport
SDK is included in core; adapters are separately installable distributions.

## Which installation method?

| Goal                                           | Use                                                | Repeatability                                                        |
| ---------------------------------------------- | -------------------------------------------------- | -------------------------------------------------------------------- |
| Operate a bridge                               | [Locked uv deployment project](#locked-deployment) | Pin Git commits and commit `uv.lock`                                 |
| Keep a short `medre` command without a project | `uv tool install` below                            | Exact index versions or Git revisions, without a deployment lockfile |
| Try help or version once                       | `uvx --from ... medre version`                     | Temporary isolated tool environment                                  |
| Develop MEDRE                                  | Editable source checkout below                     | Core `uv.lock`                                                       |
| Use a built artifact or pip                    | Virtualenv with wheel / direct Git requirement     | Depends on your requirements lock                                    |

Adapters install from **Git, local paths, or package indexes**; no PyPI
publication is required. See [adapter installation](adapter-installation.md)
for source syntax, private repositories, and pinning. Installing a distribution
registers its `medre.adapters` entry point; MEDRE never downloads code during
startup.

## Persistent command without `uv run`

A locked deployment project's `.venv` is the recommended way to keep the core
and all adapters reproducible. After `uv sync --locked`, you can invoke plain
`medre` by activating that environment:

```bash
source .venv/bin/activate
medre version
medre adapters
```

For PowerShell use `.\.venv\Scripts\Activate.ps1`; for unattended services
invoke the project's `.venv/bin/medre` (Windows: `.venv\Scripts\medre.exe`)
with the same `MEDRE_HOME` and environment variables used during setup.

If you prefer a globally available **isolated CLI** rather than a deployment
project, install core once, then add adapters to the **same tool environment**. Select Python 3.12 or later for Matrix:

```bash
uv tool install --python 3.12 medre
medre adapters install matrix
medre version
medre adapters
```

Adapter installation defaults to package-index requirements. Existing
Git-backed requests retain their source and require Git. See
[package management](package-management.md) for source inspection and switching.
Native uv `--with` can also install adapters into the same tool environment. The adapter's Python requirement
applies to the entire tool environment; for an existing Python 3.11 MEDRE tool,
reinstall through native uv with `--python 3.12` and retain the complete request
(including other adapters, extras and sources).

Use `medre sources show` to distinguish installed sources from catalog choices,
`medre sources switch` to select releases, Git revisions or local checkouts,
`medre adapters sources` to inspect the YAML source catalog,
`medre adapters remove ALIAS` to remove an adapter requirement, and
`medre update` to update the tool within its recorded source constraints.
Add `--dry-run` to preview a package change. See
[package management](package-management.md) for private/local sources, extras,
and environment detection.

At startup, adapters that supply startup facts display their account or node
identity and any useful hardware details under the current lifecycle state.
`medre adapters` shows the last-known facts with a `(cached)` marker. Its
`--json` output includes the structured record; use
`medre adapters --config PATH --json` to inspect a selected configuration.
Cached facts describe the last successful observation for that adapter state
location, not a current connection or readiness check.
See [adapter startup info](../spec/adapter-startup-info.md).

Git branches are convenient for trials; use reviewed commit SHAs or exact
index versions for repeatability. `uv tool install` does **not** create a deployment
`uv.lock`, and its environment is separate from your project's `.venv`. Use
`uv tool list` to inspect installed tools. If `medre` is not on your `PATH`,
follow uv's `uv tool update-shell` guidance.

### Run adapter SDK tools in the same environment

For an activated deployment project, use its `python` and dependency
executables directly. For a persistent uv tool installation, locate the
installed MEDRE environment with `uv tool dir`. Dependency executables are
not normally exposed on the global `PATH`. Invoke them or the interpreter
inside that environment instead of using an unrelated shell interpreter:

```bash
"$(uv tool dir)/medre/bin/python" -c "import meshcore; print(meshcore.__all__)"
"$(uv tool dir)/medre/bin/meshtastic" --help
```

On Windows PowerShell, use the corresponding Scripts directory:

```powershell
& (Join-Path (uv tool dir) 'medre/Scripts/python.exe') -c "import meshcore; print(meshcore.__all__)"
& (Join-Path (uv tool dir) 'medre/Scripts/meshtastic.exe') --help
```

These examples require the corresponding adapter installed alongside MEDRE.
Do not use `uvx` to inspect an installed SDK: it can resolve another environment.

`uvx --from 'git+https://github.com/jeremiah-k/medre.git@main'
medre version` is fine for one-off commands, but a temporary uvx environment
cannot see adapters installed elsewhere. Prefer a project for live services.

## Docker

Use the [Docker deployment guide](docker.md) for the all-adapter image,
persistent state, source-selected builds and hardware access.

## Source checkout (development)

```bash
git clone --branch main https://github.com/jeremiah-k/medre.git
cd medre
uv sync --locked --extra dev
uv run --no-sync medre version
```

This creates `.venv`, installs core in editable mode, and installs test/build
tools via the `dev` extra. For adapter work, use Git sources or editable checkouts at any location;
see the [development environment guide](../dev/environment.md). A later core `uv sync` can remove separately
installed adapters. See [development environment](../dev/environment.md).

Standard pip remains supported in a standalone virtualenv:

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
python -m pip install -e ".[dev]"
python -m pip install 'medre-adapter-matrix @ git+https://github.com/jeremiah-k/medre-adapter-matrix.git@main'
```

For an existing environment or CI artifact, use
`python -m pip install PATH_TO_WHEEL`, or a Git direct URL
requirement as documented in [adapter installation](adapter-installation.md).
Pip does not consume `uv.lock`; use a separate pinned requirements/constraints
workflow if you need reproducibility with pip.

## Verify the installation

Run in whichever environment contains **both core and your adapters**:

```bash
medre version
medre paths
medre adapters
medre config sample > sample.yaml
medre adapters check --config sample.yaml
medre config check --config sample.yaml
medre smoke --config sample.yaml --storage-path smoke.sqlite3 --json
```

A sample contributing adapter creates SDK-free sample instances and a route
without connecting to a service or radio. If none of your installed adapters
contributes a sample, the generated config is empty: install an adapter before
expecting smoke delivery. The smoke result reports a source, receipts and an
event ID. Inspect durable evidence with:

```bash
medre inspect event EVENT_ID --storage-path smoke.sqlite3
medre inspect receipts --event EVENT_ID --storage-path smoke.sqlite3
```

The CLI's `--storage-path` flag temporarily selects SQLite for smoke, even if
the sample config uses in-memory storage. A fake `sent` receipt proves local
pipeline and adapter-fake behavior, **not network or RF reception**. For live
credentials and native confirmation, follow the [transport setup
pages](README.md#find-the-right-guide) and [operator
workflows](operator-workflows.md).

## Package and test validation

From a development checkout with its `dev` extra installed:

```bash
uv run --no-sync python scripts/check_installed_package.py
uv run --no-sync python -m pytest tests/ --core-only -q
```

The wheel proof builds/installs the core wheel into an isolated environment.
Adapter SDK tests and live hardware experiments belong to their owning
repositories; the core-only suite requires none of them. Full instructions,
markers and locked dependency updates belong to [testing](../dev/testing.md)
and [development environment](../dev/environment.md), respectively.

## Troubleshooting

Missing adapter? `medre adapters check --config bridge.yaml` distinguishes
missing/broken entry points; then run `medre config check` for full validation.
An installed adapter may still lack its optional SDK. Check `medre adapters`
and the adapter's setup guide. Environment or permissions errors with devices,
Matrix crypto or native identity files need native verification rather than a
fake smoke. See [troubleshooting](troubleshooting.md).

## Locked deployment

Before changing versions, stop MEDRE and use a coherent backup of its private
state. WAL sidecars can contain committed data; copying an active SQLite main
file alone is not a complete backup. See
[stopped-state snapshots](stopped-state-snapshots.md).

For a bridge you intend to keep running, create a uv project and pin the
reviewed MEDRE and adapter Git commits. `uv.lock` captures the resolved
packages, and `uv run --locked` executes against that project's environment.
The following is a **starting example** using moving branches; replace each
`@main` with the full reviewed commit SHA and keep the resulting lockfile.

```bash
uv init --bare --python 3.13 my-medre-bridge
cd my-medre-bridge
uv add \
  'medre @ git+https://github.com/jeremiah-k/medre.git@main' \
  'medre-adapter-matrix @ git+https://github.com/jeremiah-k/medre-adapter-matrix.git@main' \
  'medre-adapter-meshtastic @ git+https://github.com/jeremiah-k/medre-adapter-meshtastic.git@main'
uv sync --locked
uv run --locked medre version
uv run --locked medre adapters
```

You can now prefix the tutorial's `medre` commands with `uv run --locked` from
this project, or activate `.venv` and run plain `medre`. Never mix `uv tool`
package changes with a project's dependency graph; use `uv add`, `uv remove`,
and `uv sync --locked` instead. The full
[adapter-source reference](adapter-installation.md) covers local checkouts,
private Git servers, package indexes, and extras such as Matrix E2EE.

Before generating bridge configuration or accepting credentials in a Git
project, ignore the wizard outputs **as well as** the private runtime state.
`uv.lock` is safe to commit, but generated YAML can contain account credentials.
Run this before following the first-run commands (or store configs outside the
repository entirely):

```bash
mkdir -p .medre
chmod 700 .medre
export MEDRE_HOME="$PWD/.medre"
cat >> .gitignore <<'EOF'
.medre/
# Local configurations and smoke evidence (not sanitized examples)
/sample.yaml
/matrix.yaml
/connections.yaml
/bridge.yaml
/smoke.sqlite3*
EOF
```

Do not rely on `.gitignore` to untrack files already committed: check with
`git status` before committing, and remove any previously tracked secret
configuration from Git's index and history using your normal secret-rotation
procedure.

In PowerShell, create `.medre`, restrict its ACL to the bridge operator, then
set `$env:MEDRE_HOME = Join-Path $PWD '.medre'`. Add the same ignore patterns
to `.gitignore` before creating configuration files. Set the same absolute
`MEDRE_HOME` in your service environment. See
[path configuration](configuration.md#medre_home-single-directory-mode).
