# Operate MEDRE

New here? Start with **[Getting Started](getting-started.md)**: install from Git,
run an offline smoke, and configure a real two-way route. For repeated use,
keep MEDRE and its adapters in one locked uv project. The examples below show
`medre` directly; in a project without an activated environment, prefix it
with `uv run --locked`.

## Find the right guide

| I want to… | Read |
| --- | --- |
| Install from Git, PyPI, or local paths | [Installation](install.md) and [adapter sources](adapter-installation.md) |
| Build and preview a bridge interactively | [Guided configuration](guided-configuration.md) |
| Configure routes, contexts, or policies | [Configuration](configuration.md) |
| Start, stop, and monitor a bridge | [Running MEDRE](running-medre.md) |
| Inspect message evidence or collect diagnostics | [Operator workflows](operator-workflows.md) and [diagnostics](diagnostics-and-evidence.md) |
| Investigate pending sends or replay safely | [Recovery and replay](recovery-and-replay.md) |
| Connect a specific network or device | [Transport setup](transport-setup/) |
| Validate native behavior with hardware | [Live validation](live-validation/short-iterations.md) |
| Diagnose a failure | [Troubleshooting](troubleshooting.md) |

## Before testing a new revision

MEDRE is a working pre-release, not a stable storage-format promise. Keep a
copy of your configuration and commit-pinned dependency lockfile. Stop MEDRE
and back up its private state before changing revisions. A schema change may
require **starting with a new disposable test database** rather than reusing
old SQLite state; do not erase a database with pending messages or evidence.
See [configuration](configuration.md) for state paths and
[recovery](recovery-and-replay.md) for investigating existing deliveries.

A `sent` receipt is transport-specific and does not always prove that a remote
radio received a packet. See [running MEDRE](running-medre.md) for receipt
semantics. Share only sanitized diagnostics when reporting problems.

Operator docs cover **procedures**. Normative runtime behavior lives in
[the specification](../spec/README.md); contributor guidance belongs in
[developer documentation](../dev/README.md).
