# Manage MEDRE and adapter packages

Use the `sources` commands to inspect and switch installed packages. Fresh
index-only installations need uv; Git is also required for Git-backed requests,
including requests retained when upgrading an existing installation.
If your installed CLI does not recognize a command, update the original
installation or use native uv to manage it; `medre version` reports the installed
core and `medre sources show` reports package provenance.

Install one persistent command, then select the adapters you need. Matrix
requires Python 3.12 or later:

```bash
uv tool install --python 3.12 medre
medre init
medre adapters sources
medre adapters install matrix meshtastic
medre adapters
medre config bridge --output bridge.yaml
```

These commands use uv's isolated tool environment. New adapter requests default
to package-index requirements (PyPI unless uv is configured otherwise). uv selects
published versions; no fallback to Git occurs if a package cannot resolve.
Existing catalogs and installed source requests remain intact. If `medre` is
unavailable after installation, run `uv tool update-shell` and follow its guidance.

Use an exact index requirement or reviewed Git commit when pinning a deployment.
Inspect source choices separately from installed provenance:

```bash
medre sources show
medre sources show --json
```

The inventory reads package metadata without importing adapters or contacting
an index. It separates configured catalog requirements, uv's saved request, the
installed version, and direct-source metadata such as a resolved Git commit or
editable directory. Outside a persistent tool environment it reports the
available metadata and explains the missing tool receipt. An index installation
without direct-source metadata does not independently establish which index
served it. The inventory is not a qualification record or lockfile.

## Add, change, or remove adapters

```bash
medre adapters install 'matrix[e2e]' --dry-run
medre adapters install 'matrix[e2e]'
medre adapters remove meshtastic
```

`install` adds or replaces an explicit adapter requirement and its extras.
Installing `matrix` after `matrix[e2e]` requests Matrix without the E2EE extra.
An explicit Python request is preserved; otherwise the running minor version is
requested without pinning a patch-specific interpreter path.
Other explicitly requested packages, extras, markers, and the MEDRE source
remain in the tool request. `remove` drops the selected explicit requirement;
uv retains packages another installed distribution still depends on.

`--dry-run` displays the uv request without changing files or packages. MEDRE
redacts URL userinfo and query strings from its own output; a redacted preview
is not a copyable command. Use credential helpers or native uv management
if necessary. uv streams its own output; do not embed secrets in requirements.
Package-changing commands stream uv's progress and return its failure status.
On Windows, they print the uv preview and exit 2 without changing packages;
run an unredacted command in PowerShell after `medre.exe` exits so its executable
is not locked. If the preview is redacted, manage the request with uv directly. `--dry-run` remains a successful preview on every platform.
Stop running bridges before changing packages and restart them afterward.
An empty `medre adapters` inventory explains the active environment and points
to source inspection and installation before generating a sample.
Adapter discovery is fixed for the lifetime of a MEDRE process. Check
`medre adapters` and `medre config check --config bridge.yaml` before restarting.

The helpers verify that the executing MEDRE belongs to the persistent tool
named `medre` in `uv tool dir`. A project virtualenv, pip installation, or
transient `uvx` invocation receives guidance instead of modifying another
installation. Use `uv add`, `uv remove`, and `uv sync --locked` for deployment
projects; see [Getting Started](getting-started.md).

## Switch installed package sources

Use **index**, **Git**, or **local checkout** to describe where a package comes
from. `pypi` is accepted as an alias for `index`; it respects uv's configured
indexes rather than overriding private-index policy. Source kind and release
version are separate choices. Git branches, tags, immutable commits and forks
use the same Git source kind.

```bash
# Preview and switch the installed first-party stack to index releases.
medre sources switch --all --from index --dry-run
medre sources switch --all --from index

# Select an index source, or follow upstream development.
medre sources switch core --from index --dry-run
medre sources switch matrix --from git --ref main --dry-run

# Fork branch; omit --ref to follow a custom repository's default HEAD.
medre sources switch core --from git --git-url https://github.com/EXAMPLE/medre.git --ref feature/source-work --dry-run

# Edit a checkout directly, or omit --editable for an installed snapshot.
medre sources switch core --from local --path /work/medre --editable --dry-run
medre sources switch matrix --from local --path /work/matrix --editable --dry-run

# Standard named requirements retain ranges and direct archive URLs.
medre sources switch core --requirement 'medre>=0.1,<0.2' --dry-run
```

Remove `--dry-run` to execute a reviewed command. A preview validates the
selection and prints the request (redacting URL credentials and query strings); it does not resolve versions or
promise that a package is available. Select core as `core` or `medre`; select
adapters by catalog alias or explicit installed distribution name (an exact
installed name takes precedence over a conflicting alias). Optional
extras such as `matrix[e2e]` are retained and can be added. Switching never
installs an absent distribution; use `adapters install` first. Inline index
constraints in the form `ALIAS[EXTRAS]==VERSION` override the alias's source
and select the chosen released version. Replace `VERSION` with the version you
intend to deploy. For source switching, `--version` selects an exact release;
use it only when deliberately pinning that package.

`--all` selects core and explicitly installed first-party adapters, retaining
community and unrelated requests. Repository, ref, version, path and explicit
requirement options select exactly one package. For a community Git adapter,
supply its repository explicitly; MEDRE does not invent an upstream URL.

uv stores successful choices in its tool receipt. Ordinary `medre upgrade`
retains them. Catalog files are not edited: a later `adapters install ALIAS` or
explicit `--sync-sources` deliberately applies that catalog's choice instead.
Use `sources show` to see both authorities. Existing catalog overrides are not
automatically migrated to index defaults.

Switching preserves installed extras, conditional selection and unrelated
requests. Scoped changes refuse unselected moving Git or local sources that
uv could refresh. Switch those packages together or select immutable unselected
sources first. For a deliberate mixed development environment, pass
`--allow-refresh` to explicitly permit unselected moving sources to refresh;
the preview states that permission. This preserves requested sources, not the
previously resolved working-tree bytes or commits. Unsupported custom
receipt settings are refused rather than discarded. Editable mode survives
supported request replay, and an explicit switch away from editable mode removes
it. Source switching does not roll back databases; check state compatibility
before downgrading. Windows prints a preview for execution after MEDRE exits;
manage redacted requests using uv directly.
A core release predating these commands also removes their CLI surface when
selected. Use uv directly if that older CLI cannot perform the next switch;
uv's tool receipt retains the installed request independently of CLI version.

For project deployments, use `uv add`, `[tool.uv.sources]`, `uv lock`, and
`uv sync --locked`; these helpers manage persistent uv tools only. For core
source development, `medre dev sync` keeps the selected checkout editable.

## Choose your own sources

`medre adapters sources` prints the effective source catalog as YAML, with a
comment showing the default override path. URLs containing userinfo or query
strings are redacted; edit the original catalog rather than copying a redacted
display. The default is `sources.yaml` next
to MEDRE's runtime config: `$XDG_CONFIG_HOME/medre/sources.yaml` (falling back
to `~/.config/medre/sources.yaml`), or `$MEDRE_HOME/sources.yaml`.

Run `medre init` to create that file, then edit it, or pass `--sources PATH` to
`init`, `sources`, `install`, or `remove`. Initialization keeps an existing valid
catalog and never installs packages or generates a runtime bridge configuration.
Ordinary upgrades use uv's recorded core source. An optional core requirement in
this catalog is applied only by an explicit --sync-sources upgrade. Ordinary
commands do not create source files automatically.
Overrides merge with the seven first-party package-index defaults. Set an alias to `null` to remove it from the catalog.

```yaml
# Optional: deliberately choose this core source with upgrade --sync-sources.
core: medre @ git+https://github.com/jeremiah-k/medre.git@main
adapters:
  matrix: medre-adapter-matrix[e2e] @ git+https://github.com/jeremiah-k/medre-adapter-matrix.git@FULL_COMMIT_SHA
  meshtastic: medre-adapter-meshtastic==0.1.0
  community: community-radio @ git+ssh://git@example.org/radio.git@stable
  local: my-radio @ file:///absolute/path/to/radio
  meshcore: null
```

The index example requires that version to be published on your configured
index. Requirements must include the distribution name; aliases are convenience
names, independent of runtime transport names and Python import paths. To change
a source, edit the catalog and run `medre adapters install ALIAS` again. Removing
an adapter matches its distribution name, so retain the alias's distribution
until removal completes. Additional aliases may name any trusted adapter;
this catalog is not a discovery allowlist.

Use SSH agents, credential helpers, and uv's own index configuration for private
sources. Keep credentials out of requirement URLs. The source file is deployment
configuration, separate from bridge YAML, and is read only by explicit management
commands. Loading a bridge config or starting MEDRE never installs packages.

## Test a branch and return to main

Select a development branch in a separate source catalog:

```yaml
adapters:
  matrix: medre-adapter-matrix[integration] @ git+https://github.com/jeremiah-k/medre-adapter-matrix.git@integration/medre-matrix-continuity
```

Select only extras actually provided by that branch. Full commit SHAs freeze a
qualified combination; moving branch names follow subsequent branch updates.
Install the selection explicitly:

```bash
medre adapters install 'matrix[integration]' --sources candidate-sources.yaml --dry-run
medre adapters install 'matrix[integration]' --sources candidate-sources.yaml
medre update
```

The installation records the branch in uv's request, so ordinary updates follow
it. Editing the catalog alone does not change installed sources. Return selected
adapters, or every installed first-party adapter, to their default main sources:

```bash
medre adapters reset matrix --dry-run
medre adapters reset matrix
medre adapters reset matrix meshtastic
medre adapters reset
```

Reset uses MEDRE's built-in first-party source defaults, independently of saved
catalog overrides. It removes the development-only `integration` extra while
retaining other extras such as `e2e`. It selects only installed adapters and
retains the core request, community adapters and unrelated package requests.
Community adapters have no host-owned default: select their source explicitly
with `install`. uv may re-resolve shared dependencies and other moving sources
when rebuilding the complete tool request.

If uv cannot fetch a deleted core branch, adapter reset still retains that
saved core requirement. Explicitly restore core to main as well:

```bash
medre adapters reset --include-core --dry-run
medre adapters reset --include-core
```

This preserves core extras, unrelated package requests, and saved catalogs.
Omit `--include-core` to retain a deliberate core pin. On older installations
without this option, use `medre upgrade --sync-sources --sources PATH` with a
catalog whose `core` requirement explicitly names main; inspect its dry-run first.

Saved source catalogs remain available for later experiments. An explicit
`--sync-sources` can apply their overrides again; ordinary `medre update` follows
the newly recorded main sources. Package source changes do not restore older
persistent state. Back up state and stop the bridge before switching sources,
then validate the configuration and restart. The Windows command handoff and
uv-tool ownership checks described above also apply to reset.

For source checkout development, use `medre dev sync --sources PATH` or
`--editable ALIAS=PATH`. That manages a checkout's environment separately from
the persistent CLI installation.

## Upgrade core and adapters

```bash
medre upgrade --dry-run
medre upgrade                      # Core, installed adapters and tool dependencies.
medre update                       # The same command.
medre upgrade --core-only
medre upgrade --adapters-only
medre adapters upgrade             # Same as --adapters-only.
medre adapters update matrix       # Upgrade an installed adapter.
medre upgrade matrix meshcore      # Select adapters by alias or distribution name.
```

Bulk upgrades delegate to uv's native tool upgrade, preserving saved installer
settings and requirements. Scoped upgrades rebuild the complete supported request
with uv's selective upgrade flags. Other explicit requests, extras, markers and
source constraints remain present; uv can adjust shared dependencies to satisfy
compatibility. uv can also re-resolve unselected floating Git sources and local
source directories or archives. Scoped commands refuse those requests before
changing anything: pin unselected Git sources to full commits, replace mutable
local sources with immutable package requirements, or use a whole-tool upgrade.
This preserves the meaning of core-only/adapter-only without leaving
hidden pins in the receipt. Custom receipt settings that cannot be replayed cause scoped
updates to refuse with guidance to use uv directly or a native bulk upgrade.

Adapters are identified from installed distribution metadata, including third-party
adapters, without importing their SDKs. A broken adapter can be updated or
removed without loading its CLI hooks. No installed adapters is a successful
adapter-only no-op. A selection never installs an absent adapter. Core-only
updates do not inspect adapter discovery metadata.

Existing pins stay pinned. Floating Git branches can advance; exact commits and
version constraints remain authoritative. Editing sources.yaml alone does not
change installed sources. To apply catalog changes deliberately:

```bash
medre upgrade --sync-sources --dry-run
medre upgrade --sync-sources
medre adapters upgrade matrix --sync-sources --sources /private/sources.yaml
```

An optional core requirement selects a different core repository, revision or
index constraint during source synchronization. Adapter-only commands retain the
installed core request. Catalog requirements apply to selected installed
packages with matching distribution names, preserving installed extras such as
Matrix E2EE. Unrelated requests remain unchanged. If an adapter changes its
distribution name, install the replacement explicitly and remove the previous
explicit requirement by its distribution name:

```bash
medre adapters install replacement
medre adapters remove previous-adapter
```

Distribution names still work after an alias is removed. A null alias never
uninstalls anything; update does not delete profiles or guess package replacements.

Preview commands without package/configuration writes using --dry-run. Real
updates stream uv's output and preserve failure exit status. Windows retains the
explicit command handoff described above. Source checkouts, uv deployment projects,
pipx and ordinary pip environments receive ownership-specific guidance instead of
having another installation modified. The updater never pulls a developer's Git
checkout, edits deployment locks, restarts services, or changes bridge YAML.

Tool installations do not provide a deployment uv.lock. For reproducible live
bridges, prefer the [locked deployment workflow](adapter-installation.md).
Back up persistent state before updating.

## Custom uv installations

The helper reads uv's receipt without modifying it. Adapter changes support
ordinary index, Git, URL, and absolute local-path requirements. They refuse
unsupported receipt shapes, ambiguous URL fragment/subdirectory combinations,
editable packages, extra exported executables, and custom saved resolver
settings or constraints rather than discarding them.
Use uv directly for those adapter changes. `medre update` delegates receipt
handling to uv and retains those settings.

uv owns package resolution and installation. Resolver failures leave the helper
without a success message; inspect uv's error and `uv tool list --show-with`
before retrying. The helper does not implement its own resolver, environment
backup, rollback, or automatic service restart.

See [uv's tool documentation](https://docs.astral.sh/uv/concepts/tools/) for
installation, upgrades, source constraints, and the distinction between tool
installations and temporary execution.
