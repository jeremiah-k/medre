# Next Release Milestone

MEDRE's first-party adapters are independent distributions, with the external
adapter migration merged to `main`. Core CI is adapter-free; provider and SDK
contracts are tested separately. The architecture is sufficiently settled for
pre-release qualification. **Prioritize reliability and current evidence over
adding new transport features.** This document is a short ordering of work;
the [release-readiness appendix](../spec/appendices/release-readiness.md) owns
capability status and test-evidence definitions.

## Sequence and acceptance gates

| Priority | Work                                   | Observable completion gate                                                                                                                                                    |
| -------- | -------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| P0       | Licensing and pinned release candidate | Approved provenance; matching source, wheel and documented licenses; one core and four adapter commits; independent locked installation                                       |
| P1       | Reliability and recovery               | Deterministic restart, reconnect, duplicate ingress, fanout and outbox recovery checks; no bypassed backoff, reset attempt identities or unexplained duplicate dispatch       |
| P2       | Native transport qualification         | Current-candidate evidence for real Matrix services, Meshtastic/MeshCore devices and LXMF paths; correlate native receptions with durable evidence and retain failed attempts |
| P3       | Operator experience                    | Repeatable Git installation, missing/broken adapter diagnostics, redacted evidence, recovery workflows and tested upgrade/reset instructions                                  |
| P4       | First prerelease                       | Published support matrix matches proven capabilities, identifies exact revisions and records unsupported or unqualified scenarios                                             |

With licensing and the pinned candidate established, prioritize P1. Use deterministic
fault injection to close core ownership and recovery gaps before repeating live
qualification. P2 then tests the resulting candidate against native services and
hardware. P3 can progress alongside both where operator diagnostics are needed to
explain a reproduced failure; it does not replace P1 or P2 evidence.

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
- A separate LXMF process _solely_ as a purported license firewall. Consider
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
