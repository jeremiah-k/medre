# Next Release Milestone

MEDRE's first-party adapters are independent distributions, with the external
adapter migration merged to `main`. Core CI is adapter-free; provider and SDK
contracts are tested separately. The architecture is sufficiently settled for
pre-release qualification. **Prioritize reliability and current evidence over
adding new transport features.** This document is a short ordering of work;
the [release-readiness appendix](../spec/appendices/release-readiness.md) owns
capability status and test-evidence definitions.

## Sequence and acceptance gates

| Priority | Work | Observable completion gate |
| --- | --- | --- |
| 0 | Complete copyright/provenance review and license transition | Each repository's LICENSE, wheel metadata and documentation agree; permissions and third-party notices checked; independent core install works without adapters |
| 1 | Establish one reproducible cross-repository release candidate | Pin core and four adapter commits, build five wheels, test a locked deployment from outside source checkouts, run adapter-free Linux/macOS/Windows core suites and adapter SDK contracts |
| 2 | Close live reliability/evidence gaps | Record current-tree evidence for restart, reconnect, duplicate ingress, target fanout and outbox recovery on Matrix, Meshtastic, MeshCore, LXMF; retain failures and limitations explicitly |
| 3 | Qualify missing live scenarios | External Matrix E2EE and multi-room runs; Meshtastic/MeshCore delivery reliability under node resets and sustained traffic; LXMF propagation/multi-hop tests and identity recovery |
| 4 | Harden the operator release path | Git-only locked deployment, adapter missing/conflict diagnostics, actionable transport health and recovery instructions, redacted support bundle, tested upgrade/reset procedure |
| 5 | Publish the prerelease | Release checklist proves source/wheel/license alignment, documents unsupported scenarios and supported platforms, and links evidence to exact Git commits |

The gating evidence must identify **the exact tree, environment, and evidence
tier**. Historical successful hardware or Docker runs are useful regression
references, not automatic passes for a different commit. Never treat a fake
receipt as proof of radio reception or crypto interoperability. A live campaign
must have durable outbound/inbound correlation and report partial failures.

## What to postpone

- Additional protocols, plugin installation managers, distributed deployment,
  and broad new Matrix event kinds until the qualification gates above pass.
- A new adapter API or compatibility mechanism merely to support an unshipped
  prerelease contract. Improve only contracts shown to be wrong by tests or
  cross-provider evidence.
- A separate LXMF process *solely* as a purported license firewall. Consider
  it later for failure isolation or deployment needs; separately review the
  upstream Reticulum License before distributing that integration.

## Definition of ready to branch toward new features

1. No unexplained failures in the five-repository contract matrix, including
   wheel-only and core-only environments.
2. Every declared delivery guarantee has a deterministic test and appropriate
   current-tree live evidence, or an explicit limitation.
3. Provenance and dependency-license decisions are approved for the actual
   distribution model, not inferred from the existence of separate repos.
4. The new-user path from Git install to a validated two-way route works with
   short, reproducible instructions on supported platforms.

After these gates, prioritize capabilities demonstrated by recorded user needs
(e.g. richer Matrix event semantics) rather than adding feature breadth to
compensate for incomplete release qualification.
