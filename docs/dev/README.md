# Developer Documentation

This directory contains documentation for contributors, adapter authors, and
anyone extending the MEDRE runtime.

## Documents

| Document                                        | Purpose                                         |
| ----------------------------------------------- | ----------------------------------------------- |
| `testing.md`                                    | Test suite structure, patterns, and conventions |
| `adapter-authoring.md`                          | How to write a new transport adapter            |
| `resource-lifecycle.md`                         | Runtime resource ownership, creation, teardown  |
| `source-audits.md`                              | Audit evidence and review notes                 |
| `relay-prefix-attribution-audit.md`             | Relay prefix and sender-provenance audit        |
| `transport-native-identity-enrichment-audit.md` | Per-transport sender-identity projection audit  |
| `reference-repos.md`                            | External reference implementations              |
| `documentation-style.md`                        | Conventions for writing MEDRE documentation     |
| `change-process.md`                             | How to propose and track documentation changes  |
| `lifecycle-authority-audit.md`                  | Lifecycle status vocabulary audit guide         |

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

MEDRE is pre-first-release. The documentation structure is being consolidated.
If you find conflicting information, `docs/spec/` is the authority.
