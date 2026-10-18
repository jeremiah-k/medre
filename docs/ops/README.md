# Operate MEDRE

New here? Start with **[Getting Started](getting-started.md)**: install from Git,
verify the pipeline without credentials, and build a first route with the
wizard. A persistent uv
tool is convenient for exploration; a locked uv project is preferable for
long-running bridges. All examples assume `medre` is on your PATH. Within a
project that is not activated, use `uv run --locked medre ...` instead.

## Find the right guide

| I want to… | Read |
| --- | --- |
| Install from Git, local paths, or published indexes | [Installation](install.md) and [adapter sources](adapter-installation.md) |
| Manage a persistent CLI and adapter packages | [Package management](package-management.md) |
| Create native connections, change bridges, and explain routing | [Guided configuration](guided-configuration.md) |
| Get optional AI help with setup | [AI-assisted setup](ai-assisted-setup.md) |
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
