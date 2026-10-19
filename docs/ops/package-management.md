# Manage MEDRE and adapter packages

Install one persistent command, then select the adapters you need:

```bash
uv tool install 'git+https://github.com/jeremiah-k/medre.git@main'
medre init
medre adapters sources
medre adapters install matrix meshtastic
medre adapters
medre config bridge --output bridge.yaml
```

These commands use uv's isolated tool environment. No source checkout or PyPI
publication is required. If `medre` is unavailable after installation, run
`uv tool update-shell` and follow its instructions. Restart your shell if needed.

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

`--dry-run` displays the full uv command without changing files or packages.
Package-changing commands stream uv's progress and return its failure status.
On Windows, they print the uv command and exit 2 without changing packages;
run that command in PowerShell after `medre.exe` exits so its executable is not
locked. `--dry-run` remains a successful preview on every platform.
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

## Choose your own sources

`medre adapters sources` prints the effective source catalog as YAML, with a
comment showing the default override path. The default is `sources.yaml` next
to MEDRE's runtime config: `$XDG_CONFIG_HOME/medre/sources.yaml` (falling back
to `~/.config/medre/sources.yaml`), or `$MEDRE_HOME/sources.yaml`.

Run `medre init` to create that file, then edit it, or pass `--sources PATH` to
`init`, `sources`, `install`, or `remove`. Initialization keeps an existing valid
catalog and never installs packages or generates a runtime bridge configuration.
Ordinary upgrades use uv's recorded core source. An optional core requirement in
this catalog is applied only by an explicit --sync-sources upgrade. Ordinary
commands do not create source files automatically.
Overrides merge with the seven defaults, which point to `jeremiah-k`'s adapter
repositories on `main`. Set an alias to `null` to remove it from the catalog.

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
until removal completes. Additional aliases may name any trusted provider;
this catalog is not a discovery allowlist.

Use SSH agents, credential helpers, and uv's own index configuration for private
sources. Keep credentials out of requirement URLs. The source file is deployment
configuration, separate from bridge YAML, and is read only by explicit management
commands. Loading a bridge config or starting MEDRE never installs packages.

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
source directories. Scoped commands refuse those requests before changing
anything: pin the unselected sources to full commits first, or use a whole-tool
upgrade. This preserves the meaning of core-only/adapter-only without leaving
hidden pins in the receipt. Custom receipt settings that cannot be replayed cause scoped
updates to refuse with guidance to use uv directly or a native bulk upgrade.

Adapters are identified from installed distribution metadata, including third-party
providers, without importing their SDKs. A broken provider can be updated or
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
Matrix E2EE. Unrelated requests remain unchanged. If a provider changes its
distribution name, install the replacement explicitly and remove the previous
explicit requirement by its distribution name:

```bash
medre adapters install replacement
medre adapters remove previous-provider
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
Back up persistent state before updating; MEDRE's pre-release schemas can change.

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
