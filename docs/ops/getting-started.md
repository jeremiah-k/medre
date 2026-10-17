# Getting Started

MEDRE connects conversations on Matrix, Meshtastic, MeshCore, and LXMF. You
install the providers you need, choose two adapter instances and their
conversation addresses, then preview the route **before starting a service**.

**New to MEDRE?** Follow the walkthrough to validate the pipeline before you
provision accounts or attach hardware, then connect the same installation to
your real platforms. For a bridge you keep running, use a separate,
commit-pinned [deployment project](#set-up-a-locked-deployment).

> **Working pre-release; testers welcome, not production-ready.** Transport
> behavior has been exercised, but config, event, and SQLite layouts may change
> between revisions. Use disposable test state initially. Before upgrading,
> stop MEDRE and back up configuration, credentials, and the **complete** SQLite
> database including its WAL sidecars. Never delete a database containing
> pending deliveries as an upgrade workaround. See
> [pre-release storage handling](operator-workflows.md#pre-release-storage-reset).

## 1. Install MEDRE and two providers

Install [uv](https://docs.astral.sh/uv/getting-started/installation/) and Git.
The simplest way to start is a persistent command installed from Git:

```bash
uv tool install 'git+https://github.com/jeremiah-k/medre.git@main'
medre adapters install matrix meshtastic
medre adapters
```

If the `medre` command is not found, run `uv tool update-shell` and open a new
shell. On **Windows**, `medre adapters install` prints a uv command instead of
replacing a running `medre.exe`; run the printed command from PowerShell after
MEDRE exits. This is an intentional handoff, not an installation success.

The tool manages its **own** Python environment. Providers installed into a
separate project or virtualenv will not be visible to it. Use
[package management](package-management.md) to inspect sources, change extras,
or remove providers. For longer-running bridges, prefer the
[locked deployment](#set-up-a-locked-deployment) below.

## 2. Verify routing without credentials

Generate a configuration from the installed providers and run one fake event
through the pipeline:

```bash
medre config sample > sample.yaml
medre config check --config sample.yaml
medre smoke --config sample.yaml --storage-path smoke.sqlite3 --json
```

**Expected:** the smoke report says `passed` and names the source, target, and
persisted delivery evidence. A fake `sent` receipt verifies local routing and
storage, **not** Matrix server acceptance or radio reception. Smoke covers one
source event. The generated sample is a **one-way diagnostic route**, not a
ready-to-run two-way bridge; construct a bidirectional route in step 3. Repeat
smoke with `--source-adapter INSTANCE_ID` for each eligible route source, and
`--source-context CONTEXT` for explicit context mappings. If no installed
provider contributes a fake sample, the generated config contains no route:
install a sample-capable provider before running smoke.

Keep `sample.yaml` and `smoke.sqlite3` out of source control. Inspect the result
using the reported event ID:

```bash
medre inspect event EVENT_ID --storage-path smoke.sqlite3
medre inspect receipts --event EVENT_ID --storage-path smoke.sqlite3
```

## 3. Build a route using the wizard

```bash
medre config bridge --output bridge.yaml
```

Choose the source and destination adapter **instance IDs** from the numbered
menu (for example, `matrix_1` and `meshtastic_1`). Choose one-way or
bidirectional forwarding, then map their **conversation contexts**: a Matrix
room ID like `!room:server` and a Meshtastic channel index like `0`. The
prompts show address examples; use your real addresses when connecting native
services. Review the compiled forward and reverse legs before confirming.

A bidirectional route usually produces a topology-cycle warning: that is a
prompt to review loop controls, not proof of a delivery loop. The builder saves
a **new** YAML file and never overwrites your input configuration.

After saving, use the exact validation and context-specific smoke commands
printed by the wizard. You can also review the plan directly:

```bash
medre config check --config bridge.yaml
medre routes plan --config bridge.yaml
```

A successful preview proves configuration and route-plan consistency; it does
not start adapters or test live delivery. See
[guided configuration](guided-configuration.md) for repeated `--map` pairs,
noninteractive use, source contexts, and existing native connections.

## 4. Connect real services or devices

Use the transport-specific setup instructions for credentials, identity
storage, device permissions, encryption, and native context formats:

| Provider | Configure | Conversation example |
| --- | --- | --- |
| [Matrix](transport-setup/matrix.md) | Account, room membership, optional E2EE | Room ID `!room:server` (not a `#` alias) |
| [Meshtastic](transport-setup/meshtastic.md) | Node connection and RF provisioning | Channel index `0` |
| [MeshCore](transport-setup/meshcore.md) | Companion connection, contacts and channels | Provider-declared contact or channel |
| [LXMF](transport-setup/lxmf.md) | Reticulum identity, RNS configuration and destination | Native LXMF destination |

Put native adapter instances in `connections.yaml`, then run the same wizard
without exposing secrets at its prompts:

```bash
medre config bridge --config connections.yaml --output bridge.yaml
medre adapters check --config bridge.yaml
medre config check --config bridge.yaml
medre routes plan --config bridge.yaml
```

Configure tokens through environment variables or a service manager rather
than pasting them into shared files. Test delivery **in each direction from an
independent recipient**; a local send receipt is not proof of remote receipt.
For operation, monitoring, and recovery, see [running MEDRE](running-medre.md)
and [operator workflows](operator-workflows.md).

## Set up a locked deployment

For a bridge you intend to keep running, create a uv project and pin the
reviewed MEDRE and provider Git commits. `uv.lock` captures the resolved
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

For a private state directory in Bash:

```bash
mkdir -p .medre
chmod 700 .medre
export MEDRE_HOME="$PWD/.medre"
printf '.medre/\n' >> .gitignore
```

In PowerShell, create `.medre`, restrict its ACL to the bridge operator, then
set `$env:MEDRE_HOME = Join-Path $PWD '.medre'`. Set the same absolute path in
your service environment; also exclude YAML secrets and SQLite evidence from
Git. See [path configuration](configuration.md#medre_home-single-directory-mode).

## Need help?

[AI-assisted setup](ai-assisted-setup.md) is optional. An assistant can explain
prompts and help review a config without ever seeing your keys or database.
For command or SDK failures, use [troubleshooting](troubleshooting.md) and
share sanitized diagnostics with your exact core/adapter revisions.
