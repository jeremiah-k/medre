# Development Environment

Use uv for source checkouts and CI. The project keeps standard Python package
metadata and a setuptools build backend, so pip installs and built wheels remain
supported. Install uv using the [official installation guide](https://docs.astral.sh/uv/getting-started/installation/).

## Set Up a Checkout

From the repository root:

```bash
uv sync --locked --extra dev
uv run --no-sync medre version
uv run --no-sync medre smoke --json
uv run --no-sync pytest tests/test_fake_bridge_smoke.py -q
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

## Select Transport Extras

Request every extra needed by the environment on each sync:

```bash
# Developer environment with a real LXMF/RNS SDK
uv sync --locked --extra dev --extra lxmf
uv run --no-sync pytest tests/test_lxmf_sdk_contract.py -m lxmf_sdk -v

# Matrix and Meshtastic Docker boundary tests
uv sync --locked --extra dev --extra matrix --extra meshtastic
uv run --no-sync bash scripts/ci/run-docker-integration.sh

# All SDKs, including Matrix E2EE, when their native prerequisites are available
uv sync --locked --all-extras
```

`uv sync` reconciles the environment to the selected extras. A later core-only
sync removes SDKs and developer tools that were selected earlier. Include
`--extra dev` in a transport test environment and repeat all required transport
extras when adding another. The lockfile contains every extra, but sync installs
only the selected subset. Hardware, credentials, Docker services, and native
library prerequisites remain separate from package installation.

## Change Dependencies

Declare dependency requirements and exact transport SDK pins in `pyproject.toml`.
`uv.lock` records the resolved graph and artifacts. Commit metadata and lockfile
changes together:

```bash
# After editing a dependency requirement in pyproject.toml
uv lock
uv lock --check
uv sync --locked --extra dev --extra lxmf
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
python -m pip install -e ".[dev,lxmf]"
python -m pytest tests/test_lxmf_sdk_contract.py -m lxmf_sdk -v
```

pip uses package requirements, not `uv.lock`, so transitive versions can differ
from the locked checkout workflow. When switching an existing environment to uv,
let `uv sync --locked` reconcile `.venv` and select all required extras. Keep
unrelated applications in their own environments.
