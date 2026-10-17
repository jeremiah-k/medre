# Development Environment

Use uv for source checkouts and CI. The project keeps standard Python package
metadata and a setuptools build backend, so pip installs and built wheels remain
supported. Install uv using the [official installation guide](https://docs.astral.sh/uv/getting-started/installation/).

## Set Up a Checkout

From the repository root:

```bash
uv sync --locked --extra dev
uv run --no-sync medre version
uv run --no-sync pytest tests/test_core_independence.py -q
```

uv creates `.venv` and installs MEDRE in editable mode. An existing compatible
Python can be used; uv can also provision one. Choose an interpreter explicitly
with `uv sync --locked --extra dev --python 3.13` when reproducing a CI leg.
CI tests CPython 3.11–3.14 and pins its uv tool version in `.tool-versions`.
`pyproject.toml` declares the minimum supported uv version.

MEDRE's `dev` dependencies are an optional extra, not a dependency group.
Use `--extra dev`; `--dev` alone does not select those tools. This preserves the
existing `pip install -e ".[dev]"` interface.

Commands following a successful sync use `uv run --no-sync` to run in that
environment without changing its packages or lockfile. After pulling dependency
changes, sync again before using `--no-sync`. Alternatively, activate the
environment with `source .venv/bin/activate` and invoke `medre`, `python`, or
`pytest` directly. An editable install and pytest's configured source path make
manual `PYTHONPATH=src` unnecessary.

If sync fails or is interrupted, rerun it with the same required extras and wait
for a successful exit before using `--no-sync` or the activated environment.
Finish synchronization before running commands in that environment; do not sync
and execute against the same `.venv` concurrently. Separate worktrees or projects
should use separate environments.

## Install external adapters for development

After the initial core sync, use one command for the SDK-free compatibility set:

```bash
uv run --no-sync medre dev sync
uv run --no-sync pytest -q
uv run --no-sync medre smoke --json
```

`medre dev sync` first syncs locked core `dev` dependencies, then installs the
adapter commits from the checkout's CI compatibility snapshot without native SDK
runtime dependencies. It targets this checkout's `.venv` explicitly, regardless
of an activated environment or `UV_PROJECT_ENVIRONMENT`. Use a standalone
checkout outside a parent uv workspace, whose shared lock/environment ownership
is deliberately refused. When running printed uv commands manually, unset
`UV_PROJECT_ENVIRONMENT` and `UV_PROJECT` first. No adapter clones are
needed. Running the same command again restores the selection after a core sync.
It does not edit `pyproject.toml`, `uv.lock`, or compatibility pins.

Choose a subset, an interpreter, or native SDKs explicitly:

```bash
uv run --no-sync medre dev sync matrix meshcore --python 3.13
uv run --no-sync medre dev sync --sdk 'matrix[e2e]' meshcore
uv run --no-sync medre dev sync --dry-run
```

SDK mode exports constraints from the locked core graph before installing the
providers and their declared dependencies. A conflicting SDK/core requirement
fails resolution instead of replacing the editable core or its pinned shared
dependencies. SDK-free mode omits adapter runtime dependencies; installing SDKs
and then requesting an SDK-free sync removes them through uv's exact core sync.
Stop tests and bridges using this environment before syncing. A failed or
interrupted installation can leave a partial environment; correct the cause and
repeat the same sync before testing. On Windows, a command running from the target
`.venv` prints the commands for execution after `medre.exe` exits.

### Work on adapters at any location

Use `--editable ALIAS=PATH` to replace a selected Git source with a local checkout:

```bash
uv run --no-sync medre dev sync matrix --editable matrix=/work/providers/matrix
uv run --no-sync medre dev test --editable matrix=/work/providers/matrix matrix -- -q
```

`dev test` uses the core environment's Python and runs pytest from the adapter
checkout, preserving that repository's pytest configuration and helpers. Put
MEDRE options before the adapter name; arguments after `--` belong to pytest.
Before pytest runs, it verifies the installed core and provider are editable at
the selected paths; a stale or different installation requires repeating `dev sync`.
It does not install packages. Select SDKs with `dev sync --sdk` before running SDK
contracts, for example `dev test matrix -- -m matrix_sdk -v`.

For bench evidence, use `dev test --evidence-dir /private/run ALIAS -- ...` to
record that adapter's Git path, commit, binary dirty diff and working-tree status
before pytest starts. The destination can contain other stage artifacts; adapter
evidence files are exclusively created. Git lookup failures stop execution.
Keep untracked source files separately if the status lists them.

To remember a selection, create `.medre-dev.yaml` in the core checkout; Git ignores
this local profile. Paths inside it are relative to the profile, so different
repository layouts do not affect commands:

```yaml
adapters: [matrix, meshcore]
sdk: false
editable:
  matrix: /work/providers/matrix
  meshcore: /another/location/meshcore
# sources: /private/dev-sources.yaml
```

On Windows, use forward slashes such as `C:/work/providers/matrix`, or
single-quote backslash paths in YAML so backslashes are not parsed as escapes.

Then run `uv run --no-sync medre dev sync` and
`uv run --no-sync medre dev test matrix -- -q`. CLI selections override the
profile's adapter list; `--sdk` / `--no-sdk` override its mode, and `--editable`
overrides its paths. `adapters: []` selects a core-only environment. A standalone
uv-tool installation can also run `medre dev sync --project /work/medre`; the
checkout environment is separate from the tool environment. `--profile PATH`
selects a profile outside the checkout; the default local profile is never
created implicitly. The profile shape is published in
[dev-environment.schema.json](../schemas/dev-environment.schema.json).

For custom Git revisions or third-party providers, pass `--sources PATH` (or set
`sources` in the profile) using the [source catalog format](../ops/package-management.md#choose-your-own-sources).
Only this explicitly selected catalog overrides the CI snapshot defaults;
operator source preferences do not silently affect compatibility tests. A null
alias removes it. Full SHAs retain repeatability; candidate branches and local
edits are experiments, not evidence for the committed CI pair. Every editable
checkout is validated against its selected distribution before synchronization.

For a reproducible bridge deployment, use a separate
[locked deployment project](../ops/adapter-installation.md). `dev sync` does not
create a combined adapter lock; native SDK versions come from provider metadata.
Use the coordinated-change policy below to qualify and land exact candidate pairs.

## Change Dependencies

For changes spanning core and adapters, follow
[coordinated changes](coordinated-changes.md): qualify candidate pairs with
explicit SHAs, land compatible dependencies first, then refresh the committed
snapshot and run `python scripts/ci/adapter_matrix.py --check-merged`.

Declare core dependencies in MEDRE, and transport SDK pins in each adapter
`pyproject.toml`.
`uv.lock` records the resolved graph and artifacts. Commit metadata and lockfile
changes together:

```bash
# After editing a dependency requirement in pyproject.toml
uv lock
uv lock --check
uv sync --locked --extra dev
uv run --no-sync medre dev sync --sdk lxmf
```

For an intentional transitive update, use `uv lock --upgrade-package <package>`
and inspect the resulting graph. Avoid a blanket upgrade as part of an unrelated
change. Exact project pins constrain upgrades, so changing an SDK version also
requires changing its declared requirement. Run that SDK's contract tests and
the affected behavioral tests; see [testing.md](testing.md).

CI uses `uv sync --locked` to reject stale or missing lockfiles. `--frozen` skips
the freshness check and is not the CI installation policy. `uv pip install`
uses uv's pip-compatible interface and does not consume the project lockfile.
See uv's [locking and syncing documentation](https://docs.astral.sh/uv/concepts/projects/sync/)
for those distinctions.

## Build and Verify Packages

```bash
uv sync --locked --extra dev
uv build
uv run --no-sync python scripts/check_installed_package.py

# Reuse a built wheel instead of building it again
uv run --no-sync python scripts/check_installed_package.py \
  --wheel dist/medre-0.1.0-py3-none-any.whl
```

`uv build` uses the declared setuptools backend; it does not change the package
format. The proof helper uses the pinned build frontend/backend in the developer
environment, then installs only the wheel and core dependencies with pip into a
separate temporary environment. Keeping that consumer path independent of uv
checks that the package works outside its checkout and that optional SDKs do not
leak into a core install. The proof requires network access.

## Existing pip Environments

An activated pip virtual environment can continue to use:

```bash
python -m pip install -e ".[dev]" -e /path/to/lxmf-provider
cd /path/to/lxmf-provider
python -m pytest tests/test_lxmf_sdk_contract.py -m lxmf_sdk -v
```

pip uses package requirements, not `uv.lock`, so transitive versions can differ
from the locked checkout workflow. When switching an existing environment to uv,
let `uv sync --locked` reconcile `.venv` and select the dev extra, then install the required adapter distributions. Keep
unrelated applications in their own environments.

## Independent test ownership

Run protocol and SDK tests from the owning adapter checkout with the same
Python environment. Each repository has its own pytest settings and helpers.
Core keeps cross-adapter routing/lifecycle checks. See
[external adapters](external-adapters.md) for the CI compatibility snapshot.
The installed-package proof defaults to core alone. Supply repeated
`--adapter-wheel PATH` arguments, including Matrix and Meshtastic wheels, to
also exercise the sample bridge entirely from installed artifacts.
