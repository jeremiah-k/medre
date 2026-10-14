# Installing external adapters: PyPI, Git, and local sources

**Adapter discovery is source-agnostic.** MEDRE loads installed Python
`medre.adapters` entry points. An adapter does not need to be on PyPI, hosted
on GitHub, or maintained by the MEDRE organization. Any trusted distribution
that installs a valid entry point is eligible.

MEDRE **does not** clone repositories, run a package manager, or install
third-party code while loading a configuration or starting the runtime.
Package installation is an explicit **deployment** operation, separate from
runtime YAML. This is intentional: unattended installations would run arbitrary
build code with the operator's permissions and change a running environment.

## Recommended: one locked deployment project per bridge

Declare the core and the adapters your bridge needs in a deployment
`pyproject.toml` and commit that file **and its `uv.lock`**. The distribution
name belongs in dependencies; the source belongs in `tool.uv.sources`.

A bridge using MEDRE from Git, Matrix from PyPI, and MeshCore from Git could
use (replace example commits with real **full commit SHAs**):

```toml
[project]
name = "my-medre-bridge"
version = "0.1.0"
requires-python = ">=3.11"
dependencies = [
  "medre",
  "medre-adapter-matrix[e2e]",
  "medre-adapter-meshcore",
]

[tool.uv.sources]
medre = { git = "https://github.com/jeremiah-k/medre.git", rev = "FULL_MEDRE_COMMIT_SHA" }
medre-adapter-meshcore = { git = "https://github.com/jeremiah-k/medre-adapter-meshcore.git", rev = "FULL_MESHCORE_COMMIT_SHA" }
```

The Matrix distribution in this example is intentionally **not** assigned a
Git source: uv resolves it from the configured package index, if published.
To use Git instead, add:

```toml
[tool.uv.sources]
medre-adapter-matrix = { git = "https://github.com/jeremiah-k/medre-adapter-matrix.git", rev = "FULL_MATRIX_COMMIT_SHA" }
```

_Merge_ that entry into the existing `tool.uv.sources` table; do not create a
second table in one TOML file. To use a locally checked-out adapter instead:

```toml
[tool.uv.sources]
medre-adapter-matrix = { path = "../medre-adapter-matrix", editable = true }
```

Run `uv lock` to resolve the complete dependency graph, then `uv sync --locked`
for reproducible installs. `uv run --no-sync medre adapters check --config
bridge.yaml` reports missing or broken providers without opening a platform connection. Finish with `medre config check --config bridge.yaml`.

For a private Git repository, uv also accepts authenticated SSH Git URLs;
use your SSH agent or credential helper, not credentials embedded in committed
URLs. Avoid moving branches or tags as reproducibility boundaries: pin a full
commit SHA, review it, and commit the resolved `uv.lock`.

`uv sync` manages the deployment environment. Avoid following it with ad hoc
`pip install` or `uv pip install` commands: a later sync can remove unmanaged
packages. Developer checkouts may use `uv` editable path sources.

## Standard pip / existing virtual environments

No uv-specific feature is needed to install a Git adapter. Python installers
accept [direct URL requirements](https://pip.pypa.io/en/stable/topics/vcs-support/):

```bash
python -m pip install \
  'medre @ git+https://github.com/jeremiah-k/medre.git@FULL_MEDRE_COMMIT_SHA' \
  'medre-adapter-matrix[e2e] @ git+https://github.com/jeremiah-k/medre-adapter-matrix.git@FULL_MATRIX_COMMIT_SHA'
```

Or from a local checkout:

```bash
python -m pip install -e ../medre -e ../medre-adapter-matrix
```

When a package is on PyPI, `python -m pip install medre-adapter-matrix`
works exactly as for any other Python distribution. Other indexes and wheel
files work as well. For pip deployments, use a reviewed, pinned requirements
file with a constraints/lock workflow appropriate for your deployment.

The named provider must be installed **in the same Python environment as
MEDRE**. Restart MEDRE after changing installed distributions; adapter
discovery is process-static. This is ordinary Python packaging, not a MEDRE
repository-download protocol.

## Configuration and preflight

Runtime YAML declares **transport types and instances**, not package URLs:

```yaml
adapters:
  matrix:
    bot:
      enabled: true
      adapter_kind: real
      # ... Matrix credentials and options ...
  meshcore:
    radio:
      enabled: true
      adapter_kind: real
      # ... radio connection options ...
```

`medre config sample` includes SDK-free samples contributed by installed
providers. With one contribution it creates two fake instances; with none it
emits a loadable empty config with installation guidance. Install a provider
and regenerate to exercise routing. Providers may omit fake/sample support;
consult their documentation for real connection settings.

Broken entry points are reported as `broken`, separately from `missing`,
including the actual provider distribution name and entry-point reference.
Repair or uninstall those distributions and restart MEDRE. Help and unrelated
healthy providers remain usable; configuring a broken provider fails with a
configuration error.

MEDRE does not assume that the `meshcore` transport comes from a PyPI package
named `medre-adapter-meshcore`: the adapter author chooses their distribution
name and advertises the `meshcore` entry point. This also supports community
providers without a first-party allowlist or source registry.

```bash
medre adapters check --config bridge.yaml   # installed entry-point preflight
medre config check --config bridge.yaml     # full runtime config validation
medre run --config bridge.yaml              # no downloads at startup
```

The adapter preflight inspects declared transport groups, including disabled
instances (they still need config model providers). It does **not** validate
credentials, SDK availability, or the remainder of the YAML configuration.
Use `medre adapters` to inspect the available adapter-owned SDK dependency
probes. An installed provider can still report its SDK as unavailable (for
example, LXMF needs both the `LXMF` and `RNS` modules). `medre config check`
validates runtime configuration, and adapter startup verifies the live service.
A missing provider is reported without loading any transport SDK or fetching a
package. Invalid config is reported separately from an absent config without
echoing config values in the adapter inventory.

## Adapter author contract

A third-party Python distribution must declare:

```toml
[project]
name = "community-radio-adapter"
version = "0.1.0"
dependencies = ["medre>=0.1.0"]

[project.entry-points."medre.adapters"]
community_radio = "community_radio.plugin:adapter_spec"
```

`adapter_spec()` is a zero-argument factory returning MEDRE's `AdapterSpec`
with `transport="community_radio"`. Only the entry-point name must match the
runtime transport name; repository names, import names, and distributions are
independent. No global registry edit is needed. See
[adapter authoring](../dev/adapter-authoring.md) for the lifecycle and event
contracts. Providers are executable third-party code: install only packages
and revisions you trust.

## Why this differs from MMRelay community plugins

MMRelay historically clones configured repositories, validates refs, installs
requirements, keeps an install-state cache, and loads modules from plugin
folders during the startup workflow. MEDRE intentionally delegates that entire
dependency-management problem to mature Python tooling (`uv`, `pip`, package
metadata and lockfiles), then uses entry points for discovery. This avoids
inventing a second Git/package resolver and supports private Git servers,
package indexes, local workspaces, and community distribution conventions.

## Package ownership

MEDRE core has no transport SDK extras. Install adapter distributions from
PyPI, Git revisions, or local paths in a deployment project, then lock the
complete environment with uv. The adapter package's own dependencies bring
in its SDK. Install `medre-adapter-matrix[e2e]` when enabling Matrix encryption.

For development, install all five repos as editable packages with a local
`pyproject.toml` deployment manifest or run `uv pip install -e` for each after
`uv sync` of MEDRE's core `dev` dependencies. SDK-free fakes belong to the
corresponding adapter distribution and are not a feature of a core-only install.
