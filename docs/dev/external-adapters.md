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

First-party distributions retain their original Python namespaces
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
   It runs on Linux, macOS, and Windows independently of adapter availability,
   selecting every module without a compatibility declaration through
   `pytest tests/ --core-only`. Import dependency guards reject undeclared
   coupling, including compatibility helpers.
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

The default suite includes cross-adapter tests. After the initial core setup,
run `uv run --no-sync medre dev sync` to install the CI-pinned providers without
SDK dependencies. Use a local development profile or `--editable ALIAS=PATH` for
arbitrarily located checkouts, then run `medre dev test ALIAS -- -q` through
`uv run --no-sync`. The helper uses the adapter's own working directory and the
core interpreter. See [development environment](environment.md#install-external-adapters-for-development).
No adapter imports MEDRE's private `tests` package. Gated SDK/service/hardware/soak
tiers stay explicit.

## Dynamic compatibility selection

`.github/adapter-compatibility.json` is the source of truth for the **curated CI
test set**. It records each repository/ref, distribution identity, optional SDK extra, and whether a
local integration tier exists. Runtime discovery does not consult this file;
third-party adapters need no manifest entry to work.

`scripts/ci/adapter_matrix.py --resolve` resolves explicit branch/tag refs to
full commit SHAs once, before matrix jobs run. Annotated tags are peeled to
commits. The plan prints the exact snapshot; all compatibility jobs consume it,
including installations through standard pip-compatible Git requirements.
The Docker workflow creates its own per-run snapshot through the same helper.
No job independently follows a moving adapter branch halfway through a run.

The manifest pins reviewed adapter commits. To prevent locally tested sibling
checkouts from silently diverging from the revisions CI will install, run:

```bash
python scripts/ci/adapter_matrix.py --check-local
```

This compares each manifest SHA with the clean sibling repository HEAD and
validates the adapter entry point and optional `distribution` expectation (when
recorded in the manifest). Distribution comparisons normalize case and runs
of `-`, `_`, and `.` according to Python package naming rules. Repository names
need not equal distribution names. The check does not fetch or install anything. Once **all** adapter changes are committed in their own
repositories, use `--sync-local` to update every `ref` atomically, review the
diff, then publish those adapter commits before the updated MEDRE manifest.
These commands require sibling repositories in the default `../medre-adapter-*`
layout; `--repo-parent` overrides the containing directory. The update fails
if any checkout is dirty or missing, rather than pinning half a coordinated
change. A failed write preserves the original manifest and removes its
temporary file. Existing full-commit refs remain valid for disconnected CI runs.
Updating the manifest is always an explicit compatibility-change review.
Adapter CI defaults to core `main`. During coordinated development, dispatch a
single run with `core_ref` set to the full proposed core SHA. Keep the persistent
`MEDRE_CORE_REF` variable unset. Before core merges, `--check-merged` verifies that
each committed adapter pin has landed in its repository's default branch. See
[coordinated changes](coordinated-changes.md) for candidate-pair testing, contract
test ownership, and the dependency-first merge order. No commits or publication
are performed by these scripts.

The scheduled **Adapter Pin Drift Canary** workflow runs `--check-local` against
fresh clones of every manifest repository's default branch daily at 08:30 UTC,
after the adapter-side default-branch canaries, and on every manual dispatch.
On confirmed pin drift the job summary names the two legitimate resolutions — a Renovate pin bump once cross-adapter CI qualifies
the pair, or a coordinated core-first flow where the pin waits on an unmerged
core PR. The canary only reports; it never updates the manifest.

The pin-drift canary clones unique repositories under owner/repository paths,
requires immutable manifest SHAs, and queues overlapping checks. Setup and
validation failures are reported separately from confirmed pin drift.

Publish matching core and adapter revisions before expecting cross-repository
CI to test this extraction: remote repositories cannot see local working-tree
changes. Core-only CI remains meaningful throughout that coordination.

See [adapter installation](../ops/adapter-installation.md) for deployments and
[adapter authoring](adapter-authoring.md) for registration and lifecycle rules.

## Smoke and SDK readiness

Normal `medre smoke` injects one event through one started fake source that
provides the optional synchronous `make_event` helper. Use `--source-adapter`
with a configured instance ID to exercise another source; the report discloses
`source_candidates` and `coverage_scope: single_source`. A fake without the
helper can still serve as a target. Every configured route and target pair
matched by the event must have successful delivery evidence for a pass, even
when runtime degradation removes an unavailable target. Event-factory errors
produce a failed report and trigger runtime cleanup. Adapter-failure drills use
the router's actual channel and event-kind matching to select a target.

`medre adapters check` inspects installed provider metadata without loading SDKs.
`medre adapters` uses each provider's declared dependency probe for SDK readiness;
a partial SDK installation cannot satisfy a probe requiring multiple modules.
See [multi-source smoke coverage](testing.md#multi-source-fake-smoke-coverage).

Standalone adapter jobs record the resolved core/adapter SHAs. Their SDK install
uses `scripts/ci/install_checkout_adapter.py` from the selected core baseline,
so qualification extras share the manifest used by core and Docker CI. SDK
versions remain in the owning adapter's package metadata. Core's pinned pair
job is authoritative; standalone results qualify the recorded pair only.

## Chatmail and SimpleX qualification

The curated compatibility snapshot includes Chatmail and SimpleX Chat at their
merged default-branch commits. Both provider suites run with their released
native SDKs across Python 3.11–3.14. SimpleX native assets are prepared explicitly
in a runner-local cache before the provider suite starts.

Chatmail native contracts cover process crash/restart and exclusive profile
ownership. Its opt-in two-profile group test also checks native reply references,
RPC reconnect, offline recovery, SMTP acceptance and independent peer receipt.
SimpleX native contracts check official group history, local queue persistence,
restart deduplication and profile reuse after worker failure. Remote SimpleX peer
delivery remains an explicit live qualification; local queue acceptance does not
establish that receipt.
