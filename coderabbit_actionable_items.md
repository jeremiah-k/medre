<!-- trunk-ignore-all -->
<!-- markdownlint-disable -->

# CodeRabbit IDE/Linting Comments - Actionable Items

> Total items: 1
> Generated: 2026-09-25T12:27:20.844782

## Summary by Severity

| Severity | Count |
| -------- | ----- |
| Minor    | 1     |

## MINOR (1 items)

### `docs/spec/diagnostics-evidence.md:759`

**Type:** actionable

**Update the stale compatibility sentence in §15.2.**

Lines 770-772 were not changed. They still say that delayed native-ref and delivery-observation records "retain scalar-only compatibility for custom/legacy adapters". This PR makes those records reject construction when `attempt_provenance` is missing. `conformance.md` §9.4 and the changelog now state that every asynchronous callback record requires the envelope. Replace the stale sentence with the mandatory-envelope rule. Also change the "Missing terminal `attempt_provenance`" row so it covers all callback records.

---
