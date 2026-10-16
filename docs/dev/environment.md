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

Core's only optional extra is `dev`. Adapter distributions own their SDK pins,
transport configs, and test fakes. Repositories may be installed from Git,
local checkouts, or PyPI. For a reproducible deployment use a separate uv
project and source-pinned `uv.lock` (see
[adapter installation](../ops/adapter-installation.md)).

```bash
uv sync --locked --extra dev
uv pip install --no-deps -e ../medre-adapter-matrix -e ../medre-adapter-meshtastic \
  -e ../medre-adapter-meshcore -e ../medre-adapter-lxmf
uv run --no-sync python -m pytest tests/test_adapter_registry.py -q
uv run --no-sync medre smoke --json
```

For SDK-backed jobs omit `--no-deps` when installing an adapter. To test the
core/registry with adapters but without native SDKs, use `uv pip install
--no-deps -e ../medre-adapter-matrix` (repeat for each needed adapter).
`uv sync` reconciles installed packages to the locked core state; install
adapters _after_ syncing or use a locked deployment project to avoid removal.

## Change Dependencies

Declare core dependencies in MEDRE, and transport SDK pins in each adapter
`pyproject.toml`.
`uv.lock` records the resolved graph and artifacts. Commit metadata and lockfile
changes together:

```bash
# After editing a dependency requirement in pyproject.toml
uv lock
uv lock --check
uv sync --locked --extra dev
uv pip install -e ../medre-adapter-lxmf
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
python -m pip install -e ".[dev]" -e ../medre-adapter-lxmf
cd ../medre-adapter-lxmf
python -m pytest ../medre-adapter-lxmf/tests/test_lxmf_sdk_contract.py -m lxmf_sdk -v
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
