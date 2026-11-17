# Operate MEDRE

New here? Start with **[Getting Started](getting-started.md)**: install released packages,
verify the pipeline without credentials, and build a first route with the
wizard. A persistent uv
tool is convenient for exploration; a locked uv project is preferable for
long-running bridges. All examples assume `medre` is on your PATH. Within a
project that is not activated, use `uv run --locked medre ...` instead.

## Find the right guide

| I want to…                                                     | Read                                                                                                                                      |
| -------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------- |
| Install from Git, local paths, or published indexes            | [Installation](install.md) and [adapter sources](adapter-installation.md)                                                                 |
| Manage a persistent CLI and adapter packages                   | [Package management](package-management.md)                                                                                               |
| Deploy with containers, image builds, and hardware access      | [Docker](docker.md)                                                                                                                       |
| Create native connections, change bridges, and explain routing | [Guided configuration](guided-configuration.md)                                                                                           |
| Get optional AI help with setup                                | [AI-assisted setup](ai-assisted-setup.md)                                                                                                 |
| Customize message layout or understand puppeting               | [Message presentation](message-presentation.md)                                                                                           |
| Understand message storage and privacy choices                 | [Privacy and message storage](message-storage.md)                                                                                         |
| Configure routes, contexts, or policies                        | [Configuration](configuration.md)                                                                                                         |
| Preview connections, channel mappings, or Mermaid route graphs | [Route visualization](route-visualization.md)                                                                                             |
| Start, stop, and monitor a bridge                              | [Running MEDRE](running-medre.md)                                                                                                         |
| Inspect message evidence or collect diagnostics                | [Operator workflows](operator-workflows.md) and [diagnostics](diagnostics-and-evidence.md)                                                |
| Inspect exact cancellation audit evidence                      | [Operator decisions](operator-decisions.md)                                                                                               |
| Cancel one unstarted destination locally                       | [Exact cancellation](exact-cancellation.md)                                                                                               |
| Record a resume/resolve decision on one exact target           | [Exact-target decisions](exact-target-decisions.md)                                                                                       |
| Investigate pending sends or replay safely                     | [Recovery and replay](recovery-and-replay.md)                                                                                             |
| Capture or restore stopped state coherently                    | [Stopped-state snapshots](stopped-state-snapshots.md) and [mixed-installation qualification](mixed-installation-restore-qualification.md) |
| Derive a stopped-state inventory from provider declarations    | [Provider stopped-state declarations](stopped-state-provider-inventory.md)                                                                |
| Connect a specific network or device                           | [Transport setup](transport-setup/README.md)                                                                                              |
| Validate native behavior with hardware                         | [Live validation](live-validation/short-iterations.md)                                                                                    |
| Diagnose a failure                                             | [Troubleshooting](troubleshooting.md)                                                                                                     |

## Updates and backups

Keep your configuration and dependency lockfile with your deployment. Before
updating, stop MEDRE and back up its private state using the
[operator workflows](operator-workflows.md). See [recovery and replay](recovery-and-replay.md)
for investigating queued deliveries, and [configuration](configuration.md) for
state paths.

Operator docs cover **procedures**. Normative runtime behavior lives in
[the specification](../spec/README.md); contributor guidance belongs in
[developer documentation](../dev/README.md).
