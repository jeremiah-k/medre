# Operate MEDRE

Follow [Getting Started](getting-started.md) to install MEDRE, try a simulated
route, and connect your first two conversations. In a locked uv project, run
`uv run --locked medre ...` or activate the project's environment.

## Find the right guide

| Task                                               | Guide                                                                                                                                                                                                                             |
| -------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Prepare an account or device                       | [Transport setup](transport-setup/README.md)                                                                                                                                                                                      |
| Build or explain a route                           | [Guided configuration](guided-configuration.md) and [route visualization](route-visualization.md)                                                                                                                                 |
| Look up YAML fields                                | [Configuration reference](configuration.md)                                                                                                                                                                                       |
| Change how relayed messages look                   | [Message presentation](message-presentation.md)                                                                                                                                                                                   |
| Understand what MEDRE stores                       | [Privacy and message storage](message-storage.md)                                                                                                                                                                                 |
| Install or update packages                         | [Installation](install.md), [adapter sources](adapter-installation.md), and [package management](package-management.md)                                                                                                           |
| Run or monitor a service                           | [Running MEDRE](running-medre.md) and [Docker](docker.md)                                                                                                                                                                         |
| Back up or restore state                           | [Operator workflows](operator-workflows.md), [snapshots](stopped-state-snapshots.md), [provider declarations](stopped-state-provider-inventory.md), and [mixed-installation restore](mixed-installation-restore-qualification.md) |
| Investigate a failure                              | [Troubleshooting](troubleshooting.md) and [diagnostics](diagnostics-and-evidence.md)                                                                                                                                              |
| Test with accounts or hardware                     | [Live validation](live-validation/short-iterations.md)                                                                                                                                                                            |
| Inspect or replay an outstanding delivery          | [Recovery and replay](recovery-and-replay.md)                                                                                                                                                                                     |
| Verify the logical purge of expired content        | [Ceiling audit](content-ceiling-audit.md)                                                                                                                                                                                         |
| Act on one destination or inspect its audit record | [Exact cancellation](exact-cancellation.md), [exact-target decisions](exact-target-decisions.md), and [operator decisions](operator-decisions.md)                                                                                 |

## Updates and backups

Keep your configuration and dependency lockfile with your deployment. Before
updating, stop MEDRE and back up its private state.

Operator docs cover **procedures**. Normative runtime behavior lives in
[the specification](../spec/README.md); contributor guidance belongs in
[developer documentation](../dev/README.md).
