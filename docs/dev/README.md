# Developer Documentation

This directory contains documentation for contributors, adapter authors, and
anyone extending the MEDRE runtime. Historical audit snapshots are not kept
here; Git history is the archive, and findings that remain current live in
the relevant spec/ops/dev page.

## Documents

| Document                                                         | Purpose                                                                      |
| ---------------------------------------------------------------- | ---------------------------------------------------------------------------- |
| [environment.md](environment.md)                                 | Checkout setup, uv/pip workflows, extras, dependency updates, builds         |
| [testing.md](testing.md)                                         | Test suite structure, patterns, tiers, live-test harness                     |
| [static-typing.md](static-typing.md)                             | Strict-mypy gate scope, SDK import exemptions, and typing status             |
| [adapter-authoring.md](adapter-authoring.md)                     | How to write a new transport adapter                                         |
| [adapter-registration.md](adapter-registration.md)               | `AdapterSpec` fields and the `medre.adapters` entry-point contract           |
| [adapter-sdk-parity.md](adapter-sdk-parity.md)                   | Installed-SDK contract tiers and open SDK parity gaps                        |
| [external-adapters.md](external-adapters.md)                     | External adapter distributions, entry-point discovery, and CI ownership      |
| [coordinated-changes.md](coordinated-changes.md)                 | Compatibility manifest rules for core and adapter merges                     |
| [resource-lifecycle.md](resource-lifecycle.md)                   | Runtime resource ownership, creation, teardown                               |
| [reference-repos.md](reference-repos.md)                         | External reference implementations and copy boundaries                       |
| [mmrelay-behavior-reference.md](mmrelay-behavior-reference.md)   | Live mmrelay interop behavior reference                                      |
| [documentation-style.md](documentation-style.md)                 | Conventions for writing MEDRE documentation                                  |
| [licensing.md](licensing.md)                                     | Own-source licensing, dependency obligations, publication checks             |
| [change-process.md](change-process.md)                           | How to propose and track documentation changes                               |
| [messenger-interoperability.md](messenger-interoperability.md)   | XMPP reference contracts, rendering, activity, security, and freeze evidence |
| [attribution-status.md](attribution-status.md)                   | Native sender attribution claims and their authority limits                  |
| [consistency-delivery-status.md](consistency-delivery-status.md) | Consistency, recovery, and delivery audit mapped to tests and evidence       |
| [source-export-verification.md](source-export-verification.md)   | Reproducible exported-stack bundles and offline pin verification             |
| [four-target-recovery.md](four-target-recovery.md)               | Durable fanout close/reopen regression and its native qualification boundary |
| [sqlite-measurement.md](sqlite-measurement.md)                   | Disposable synthetic SQLite append/reopen baseline measurement               |
| [native-evidence-index.json](native-evidence-index.json)         | Recorded native qualification runs with exact source revisions               |

## How to Add Documentation

1. **Spec semantics** (data models, contracts, guarantees) go into existing
   `docs/spec/` pages. Do not create new spec pages without a change fragment.
2. **Operator procedures** (commands, workflows, troubleshooting) go into
   `docs/ops/` pages.
3. **Developer references** (patterns, testing, adapter authoring) go into
   `docs/dev/` pages.
4. Do not create new top-level directories under `docs/` without explicit
   approval.
5. Do not create contract-style or runbook-style files. The old
   `docs/contracts/` and `docs/runbooks/` systems have been replaced.

## Pre-Release Note

MEDRE is pre-first-release. Begin with the development environment guide above
and use the testing guide for evidence tiers and scoped verification. If usage
documentation conflicts with runtime contracts, `docs/spec/` is the authority.
