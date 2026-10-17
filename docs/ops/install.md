# Installation Reference

For a first real bridge, use [Getting Started](getting-started.md). It includes
Git installation, a local smoke, credentials, and a two-way route. This page
covers the **other installation methods** and how to verify them, without
maintaining a second copy of the operator tutorial.

MEDRE requires Python **3.11 or newer**, is **pre-release** (package classifier
`Development Status :: 3 - Alpha`), and is licensed **MIT** (MEDRE-owned core source; adapters and SDKs have separate terms). Its
console entry point is `medre.cli:main` (the command is `medre`). No transport
SDK is included in core; adapters are separately installable distributions.

## Which installation method?

| Goal                                           | Use                                                                         | Repeatability                                      |
| ---------------------------------------------- | --------------------------------------------------------------------------- | -------------------------------------------------- |
| Operate a bridge                               | [Locked uv deployment project](getting-started.md#set-up-a-locked-deployment) | Pin Git commits and commit `uv.lock`               |
| Keep a short `medre` command without a project | `uv tool install` below                                                     | Explicit Git revisions, but no deployment lockfile |
| Try help or version once                       | `uvx --from ... medre version`                                              | Temporary isolated tool environment                |
| Develop MEDRE                                  | Editable source checkout below                                              | Core `uv.lock`                                     |
| Use a built artifact or pip                    | Virtualenv with wheel / direct Git requirement                              | Depends on your requirements lock                  |

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
project, install core once, then add providers to the **same tool environment**:

```bash
uv tool install 'git+https://github.com/jeremiah-k/medre.git@main'
medre adapters install matrix
medre version
medre adapters
```

Use `medre adapters sources` to inspect or customize the YAML source catalog,
`medre adapters remove ALIAS` to remove a provider requirement, and
`medre update` to update the tool within its recorded source constraints.
Add `--dry-run` to preview a package change. See
[package management](package-management.md) for private/local sources, extras,
and environment detection.

These branches are convenient for trials; replace them with reviewed commit
SHAs for repeatability. `uv tool install` does **not** create a deployment
`uv.lock`, and its environment is separate from your project's `.venv`. Use
`uv tool list` to inspect installed tools. If `medre` is not on your `PATH`,
follow uv's `uv tool update-shell` guidance.

`uvx --from 'git+https://github.com/jeremiah-k/medre.git@main'
medre version` is fine for one-off commands, but a temporary uvx environment
cannot see providers installed elsewhere. Prefer a project for live services.

## Source checkout (development)

```bash
git clone --branch main https://github.com/jeremiah-k/medre.git
cd medre
uv sync --locked --extra dev
uv run --no-sync medre version
```

This creates `.venv`, installs core in editable mode, and installs test/build
tools via the `dev` extra. For adapter work, use sibling checkouts and install
providers after the core sync, or create a separate locked deployment project
with local editable sources. A later core `uv sync` can remove separately
installed providers. See [development environment](../dev/environment.md).

Standard pip remains supported in a standalone virtualenv:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
python -m pip install -e ".[dev]"
python -m pip install -e ../medre-adapter-matrix
```

For an existing environment or CI artifact, use
`python -m pip install ./dist/medre-0.1.0-py3-none-any.whl`, or a Git direct URL
requirement as documented in [adapter installation](adapter-installation.md).
Pip does not consume `uv.lock`; use a separate pinned requirements/constraints
workflow if you need reproducibility with pip.

## Verify the installation

Run in whichever environment contains **both core and your providers**:

```bash
medre version
medre paths
medre adapters
medre config sample > sample.yaml
medre adapters check --config sample.yaml
medre config check --config sample.yaml
medre smoke --config sample.yaml --storage-path smoke.sqlite3 --json
```

A sample contributing provider creates offline sample instances and a route
without connecting to a service or radio. If none of your installed providers
contributes a sample, the generated config is empty: install a provider before
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

Missing provider? `medre adapters check --config bridge.yaml` distinguishes
missing/broken entry points; then run `medre config check` for full validation.
An installed provider may still lack its optional SDK. Check `medre adapters`
and the provider's setup guide. Environment or permissions errors with devices,
Matrix crypto or native identity files need native verification rather than a
fake smoke. See [troubleshooting](troubleshooting.md).
