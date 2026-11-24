# Developer Documentation

Set up your [development environment](environment.md), then use the
[testing guide](testing.md) to choose the checks for your change.

| Document                                                                                             | Purpose                                                                               |
| ---------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------- |
| [implicit-state-machines.md](implicit-state-machines.md)                                             | Source-based state-machine ownership audit and disposition                            |
| [content-lifecycle.md](content-lifecycle.md)                                                         | Content expiry, reply context and bounded consumer/plugin holds                       |
| [development-priorities.md](development-priorities.md)                                               | Coordinated recovery, conversation fidelity and qualification work                    |
| [contract-gap-status.md](contract-gap-status.md)                                                     | Living register of open contract gaps, their status and next work                     |
| [environment.md](environment.md)                                                                     | Checkout setup, uv/pip workflows, extras, dependency updates, builds                  |
| [installation-sources.md](installation-sources.md)                                                   | Published, Git and editable sources; qualification and channel planning               |
| [testing.md](testing.md)                                                                             | Test suite structure, patterns, tiers, live-test harness                              |
| [live-validation/](live-validation/)                                                                 | Native campaign procedures, per-transport smoke tests, and the Docker ecosystem lab   |
| [static-typing.md](static-typing.md)                                                                 | Strict-mypy gate scope, SDK import exemptions, and typing status                      |
| [adapter-authoring.md](adapter-authoring.md)                                                         | How to write a new transport adapter                                                  |
| [transport-internals/](transport-internals/README.md)                                                | Per-transport adapter internals behind the operator setup guides                      |
| [adapter-registration.md](adapter-registration.md)                                                   | `AdapterSpec` fields and the `medre.adapters` entry-point contract                    |
| [adapter-sdk-parity.md](adapter-sdk-parity.md)                                                       | Installed-SDK contract tiers and open SDK parity gaps                                 |
| [external-adapters.md](external-adapters.md)                                                         | External adapter distributions, entry-point discovery, and CI ownership               |
| [coordinated-changes.md](coordinated-changes.md)                                                     | Compatibility manifest rules for core and adapter merges                              |
| [resource-lifecycle.md](resource-lifecycle.md)                                                       | Runtime resource ownership, creation, teardown                                        |
| [reference-repos.md](reference-repos.md)                                                             | External reference implementations and copy boundaries                                |
| [mmrelay-behavior-reference.md](mmrelay-behavior-reference.md)                                       | Live mmrelay interop behavior reference                                               |
| [documentation-style.md](documentation-style.md)                                                     | Conventions for writing MEDRE documentation                                           |
| [licensing.md](licensing.md)                                                                         | Own-source licensing, dependency obligations, publication checks                      |
| [change-process.md](change-process.md)                                                               | How to propose and track documentation changes                                        |
| [messenger-interoperability.md](messenger-interoperability.md)                                       | XMPP reference contracts, rendering, activity, security, and freeze evidence          |
| [attribution-status.md](attribution-status.md)                                                       | Native sender attribution claims and their authority limits                           |
| [consistency-delivery-status.md](consistency-delivery-status.md)                                     | Consistency, recovery, and delivery audit mapped to tests and evidence                |
| [provider-retention-inventory.md](provider-retention-inventory.md)                                   | Per-transport store ownership, preservation, and erase-ability inventory behind G06    |
| [qualified-stack-export.md](qualified-stack-export.md)                                               | Offline exact-pin source bundles selected by core                                     |
| [source-export-verification.md](source-export-verification.md)                                       | Reproducible exported-stack bundles and offline pin verification                      |
| [four-target-recovery.md](four-target-recovery.md)                                                   | Durable fanout close/reopen regression and its native qualification boundary          |
| [sqlite-measurement.md](sqlite-measurement.md)                                                       | Disposable synthetic SQLite append/reopen baseline measurement                        |
| [ceiling-sweep-pressure-measurement-2026-11-23.md](ceiling-sweep-pressure-measurement-2026-11-23.md) | Content-ceiling sweep pressure loads, frozen acceptance budgets, and measured results |
| [native-evidence-index.json](native-evidence-index.json)                                             | Recorded native qualification runs with exact source revisions                        |

Follow the [documentation style guide](documentation-style.md) when editing
docs and the [versioning policy](versioning-policy.md) when changing a
published contract.

## Repository skills

If you use a coding agent, link it to the relevant skill in your request.

- [Configure MEDRE](../../skills/medre-configure/SKILL.md) for drafting,
  previewing, and troubleshooting bridge routes.
- [Develop MEDRE core](../../skills/medre-core-development/SKILL.md) for
  tracing behavior to its owner and choosing the right regression and
  compatibility checks.
