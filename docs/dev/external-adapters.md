# External adapter ownership and CI

MEDRE discovers installed transports through the `medre.adapters` Python
entry-point group. Any installable distribution can supply a zero-argument
factory returning `AdapterSpec`, from PyPI, Git, another index, a wheel, or a
local checkout. Discovery is process-static and never installs packages.

## Ownership

Core owns canonical events, generic adapter contracts, configuration
orchestration, routing/planning, storage, evidence, and runtime lifecycle.
Adapters own transport models, optional runtime wrappers, fakes, native
protocol behavior, SDK dependencies, native address validators, and protocol
and SDK tests. Core's only optional extra is `dev`; its lockfile contains no
transport SDK graph. Import adapter-owned wrappers directly; core has no
legacy transport-named runtime wrapper facade.

The four first-party distributions retain their original Python namespaces
through `pkgutil.extend_path`. Community providers may use a separate top-level
package instead; namespace layout is not part of discovery. The adapter's
`medre` dependency range states the core interface versions it supports.
No radio category is required: chat services, webhooks, sidecars, and other
platforms use the same contracts. Normal installers resolve compatibility; runtime does not repair dependency
skew. Source histories and original attribution remain in each repository.

Structured destination kinds are extensible through `DestinationSpec`. Core
owns named `channel` addressing; adapters own native kinds and pure validation.
The static runtime/routing schemas validate common structure and accept community
transport/addressing names. Installed-provider checks and native field rules
are enforced by configuration loading. Transport-specific schema snapshots in
MEDRE's documentation describe first-party examples, not an installation list.

## Test layers

1. **Core independence:** the CI core job installs zero adapter distributions
   and exercises generic event/routing/fake contracts, configuration, discovery,
   a metadata-backed community provider, and installed core-wheel behavior.
   It runs on Linux, macOS, and Windows independently of adapter availability.
2. **Adapter ownership:** each repository runs its own protocol tests and SDK
   contracts against MEDRE core. Its fake job omits SDK dependencies; its SDK
   job installs the distribution's own pins. LXMF and MeshCore also own their
   process-isolated/local endpoint integration tests and opt-in soak tier.
   Suite helpers are local test code, outside the shipped package.
3. **Cross-adapter compatibility:** MEDRE retains multi-transport routing,
   lifecycle, configuration orchestration, operator workflows, and architecture
   checks. This suite explicitly installs the curated provider set without SDKs.
   Core CI also runs each selected adapter suite against the proposed core
   revision, including its native SDK contract and local integration tier.
4. **Installed artifacts:** `scripts/check_installed_package.py` proves the
   core wheel without providers. Repeated `--adapter-wheel` arguments additionally
   prove the sample bridge with external Matrix and Meshtastic wheels, outside
   all source checkouts and without SDKs.

The generic default suite in this checkout includes cross-adapter tests. After
`uv sync --locked --extra dev`, explicitly install the adapter checkouts with
`uv pip install --no-deps -e ../medre-adapter-matrix ...` before collecting it.
Run an adapter suite from its own repository with the shared interpreter, for
example `../medre/.venv/bin/python -m pytest -q`. No adapter imports MEDRE's
private `tests` package. Gated SDK/service/hardware/soak tiers stay explicit.

## Dynamic compatibility selection

`.github/adapter-compatibility.json` is the source of truth for the **curated CI
test set**. It records each repository/ref, optional SDK extra, and whether a
local integration tier exists. Runtime discovery does not consult this file;
third-party adapters need no manifest entry to work.

`scripts/ci/adapter_matrix.py --resolve` resolves explicit branch/tag refs to
full commit SHAs once, before matrix jobs run. Annotated tags are peeled to
commits. The plan prints the exact snapshot; all compatibility jobs consume it,
including installations through standard pip-compatible Git requirements.
The Docker workflow creates its own per-run snapshot through the same helper.
No job independently follows a moving adapter branch halfway through a run.

The manifest pins reviewed adapter commits. After publishing a compatible
adapter change, update its `ref` to the new full commit SHA to make
compatibility checks stable across runs.
Updating the manifest then becomes an explicit compatibility-change review.
Adapter CI defaults to core `main`. During coordinated development, set the
adapter repository's `MEDRE_CORE_REF` Actions variable to the proposed core
branch or commit; the manual `core_ref` input overrides it. Clear the variable
after the compatible core change merges. No commits or publication are
performed by these scripts.

Publish matching core and adapter revisions before expecting cross-repository
CI to test this extraction: remote repositories cannot see local working-tree
changes. Core-only CI remains meaningful throughout that coordination.

See [adapter installation](../ops/adapter-installation.md) for deployments and
[adapter authoring](adapter-authoring.md) for registration and lifecycle rules.
