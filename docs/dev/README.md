# Developer Documentation

This directory contains documentation for contributors, adapter authors, and
anyone extending the MEDRE runtime. Historical audit snapshots are not kept
here; Git history is the archive, and findings that remain current live in
the relevant spec/ops/dev page.

## Documents

| Document                        | Purpose                                                  |
| ------------------------------- | -------------------------------------------------------- |
| `testing.md`                    | Test suite structure, patterns, tiers, live-test harness |
| `adapter-authoring.md`          | How to write a new transport adapter                     |
| `adapter-sdk-parity.md`         | Installed-SDK contract tiers and open SDK parity gaps    |
| `resource-lifecycle.md`         | Runtime resource ownership, creation, teardown           |
| `reference-repos.md`            | External reference implementations and copy boundaries   |
| `mmrelay-behavior-reference.md` | Live mmrelay interop behavior reference                  |
| `documentation-style.md`        | Conventions for writing MEDRE documentation              |
| `change-process.md`             | How to propose and track documentation changes           |

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
