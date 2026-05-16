# Operator Command Surface

> Last updated: 2026-05-16
> Scope: Intended CLI command surface for first release
> Status: Architecture definition. No CLI code changes.

This document defines the intended operator command surface before first
release. It inventories every CLI command that exists today, groups them by
operator role, and classifies each as keep, consolidate, fold into another
command, or developer tooling.

**No deprecations or compatibility shims are needed.** medre has not been
publicly released. All consolidation can happen cleanly before any user depends
on the current command layout.


## Command groups

The operator surface has three tiers:

| Group | Audience | Purpose |
|-------|----------|---------|
| Product operation | Bridge operators | Daily runtime management, health checks, incident investigation, recovery |
| Developer/local validation | Developers, CI | Pre-release validation, alpha smoke testing, pipeline verification |
| Internal/test-only | Codebase internals | `run_session`, drill helpers, evidence helpers, trace/timeline assembly |

A command's group determines its stability expectations, documentation home,
and whether it appears in operator-oriented help text.


## Current command inventory

Every command registered in `src/medre/cli/main.py` as of this writing.

| Command | Subcommand(s) | Purpose | Implemented in |
|---------|---------------|---------|----------------|
| `medre run` | | Start the MEDRE runtime | `cli/run_commands.py` |
| `medre config` | `check`, `sample` | Config validation and generation | `cli/config_commands.py` |
| `medre paths` | | Print resolved MEDRE paths | `cli/config_commands.py` |
| `medre version` | | Print version, Python, platform | `cli/main.py` |
| `medre adapters` | | List available and configured adapters | `cli/config_commands.py` |
| `medre diagnostics` | | Print runtime snapshot JSON (no server) | `cli/diagnostics_commands.py` |
| `medre routes` | `validate`, `topology`, `list` | Route management | `cli/route_commands.py` |
| `medre smoke` | (flags: `--drill`, `--run-session`, `--scenario`) | Fake bridge smoke test | `cli/smoke_commands.py` |
| `medre evidence` | | Collect evidence bundle for support | `cli/evidence_commands.py` |
| `medre inspect` | `event`, `receipts`, `native-ref`, `replay` | Read-only storage inspection | `cli/inspect_commands.py` |
| `medre trace` | `event`, `replay` | Chronological timeline assembly | `cli/trace_commands.py` |
| `medre replay` | | Execute a replay operation | `cli/replay_commands.py` |
| `medre recover` | | Analyze failed deliveries, generate recovery runbook | `cli/recover_commands.py` |


## Per-command classification

### Product operation (keep on operator surface)

These are the commands a daily bridge operator runs. The target product surface
shrinks toward these six command families: `run`, `config`, `routes`,
`diagnostics`, `inspect`, `replay`.

| Command | Classification | Rationale |
|---------|---------------|-----------|
| `medre run` | **Keep** | Primary operator command. Starts the runtime. |
| `medre config check` | **Keep** | Pre-flight validation before `run`. |
| `medre config sample` | **Keep** | Onboarding. Generates a starter TOML file. |
| `medre routes validate` | **Keep** | Pre-flight route validation. |
| `medre routes topology` | **Keep** | Operator visualization of route graph. |
| `medre routes list` | **Keep** | Quick route inventory. |
| `medre diagnostics` | **Keep** | Health check and runtime snapshot without starting a server. |
| `medre inspect event` | **Keep** | Read-only canonical event lookup from storage. |
| `medre inspect receipts` | **Keep** | Delivery receipt query by event or replay run. |
| `medre inspect native-ref` | **Keep** | Reverse lookup from transport-native ID to canonical event. |
| `medre inspect replay` | **Keep** | Read-only replay run timeline inspection. |
| `medre replay` | **Keep** | Recovery action. Re-delivers historical events through current routes. |
| `medre version` | **Keep** | Standard CLI convention. Every tool has this. |

### Consolidate into product commands

| Command | Classification | Target | Rationale |
|---------|---------------|--------|-----------|
| `medre paths` | **Consolidate** | `medre config paths` | Path resolution is a config concern. Lives in `config_commands.py` already. Becomes a subcommand of `config`. |
| `medre adapters` | **Consolidate** | `medre config adapters` or fold into `diagnostics` | Adapter inventory is config/diagnostic information, not a standalone operation. Currently lives in `config_commands.py`. |

### Candidates to fold into `inspect`

`trace`, `evidence`, and `recover` remain supported today. They are candidates
to consolidate under `medre inspect` before first release because they share the
same pattern: read-only queries against storage, producing structured output for
incident investigation. No decision is made here about timing. The current
commands work. The classification records the direction.

| Command | Classification | Rationale |
|---------|---------------|-----------|
| `medre trace event` | **Fold into `inspect`** | Timeline assembly is a richer form of event inspection. `inspect event` shows the event; `inspect event --timeline` provides the full chronological trace. Same storage backend, same audience, same read-only contract. |
| `medre trace replay` | **Fold into `inspect`** | Replay timeline is now available as `inspect replay <run_id>`, providing the same chronological timeline for replay runs in a read-only subcommand. |
| `medre evidence` | **Fold into `inspect`** | Evidence collection aggregates storage queries (event, receipts, native refs) plus optional health refresh. Each piece is already an `inspect` subcommand. A future `inspect bundle` (or `inspect evidence`) could compose the same output. |
| `medre recover` | **Fold into `inspect`** | Recovery analysis queries storage for failed deliveries and produces a runbook. This is incident investigation, not a runtime operation. A future `inspect recover` (or `inspect failures`) could provide the same analysis under the inspection umbrella. |

These four commands share common traits that justify consolidation:

- All are read-only against storage. None modifies runtime state.
- All serve incident investigation, not daily operation.
- All produce structured output (JSON or human-readable reports).
- All share the `--config` / `--storage-path` dual-input pattern.
- The `inspect` command family already owns the read-only storage query contract.

Folding them would reduce the top-level command surface from 13 commands to 9
(`run`, `config`, `routes`, `diagnostics`, `inspect`, `replay`, `smoke`,
`version`, plus consolidated `paths` and `adapters`). After consolidation,
the target surface is 6 product commands plus `smoke` and `version`.

### Developer/local validation

| Command | Classification | Rationale |
|---------|---------------|-----------|
| `medre smoke` | **Keep as dev/validation tooling** | Alpha validation, not daily bridge operation. Exercises the full pipeline with fake adapters. Used in CI and pre-release verification. Remains a top-level command for now (Option A). |
| `medre smoke --drill` | **Keep as dev/validation tooling** | Failure drill mode. Part of smoke. |
| `medre smoke --run-session` | **Internal helper exposed via smoke** | Standardized session report. Primarily used by test infrastructure. Exposed through smoke for convenience. |

`smoke` is positioned as validation tooling, not a bridge operator command.
It uses fake adapters, produces test reports, and serves pre-release confidence.
It remains a top-level command because it has a distinct audience (developers
and CI) and a distinct contract (no real adapters, no persistent side effects
unless `--storage-path` is given).

No additional developer/validation commands are planned. If future dev tooling
is needed, it would follow the same pattern: fake adapters, structured reports,
no runtime dependency.


## Internal/test-only APIs

These are not top-level CLI commands. They are internal functions that support
the operator tooling surface. Documented here because they represent the
implementation layer behind the commands above.

| Function | Location | Called by | Purpose |
|----------|----------|-----------|---------|
| `_run_session` | `runtime/run_session.py` | `smoke --run-session` | Complete bridge session lifecycle: start, inject, poll, stop, snapshot, report |
| Drill helpers | `runtime/drill.py` | `smoke --drill` | Named failure injection against the pipeline |
| Evidence helpers | `runtime/evidence.py` | `evidence` | Aggregates smoke, drill, storage queries, diagnostics into a bundle |
| Trace/timeline assembly | `runtime/trace.py` | `trace event`, `trace replay` | Chronological timeline reconstruction from storage |
| Recover analysis | `runtime/recover.py` | `recover` | Failed delivery analysis and runbook generation |
| Smoke orchestration | `runtime/smoke.py` | `smoke` | Fake bridge pipeline exercise |

These modules live in `runtime/` today. They will move to `medre/operator/`
when the import graph allows. See [Operator Tooling Boundary](operator-tooling-boundary.md)
for the split criteria and import invariants.


## Summary

**Target product surface (6 command families):**

```
medre run              Start the runtime
medre config           Config management (check, sample, [paths], [adapters])
medre routes           Route management (validate, topology, list)
medre diagnostics      Health check and runtime snapshot
medre inspect          Read-only storage inspection (event, receipts, native-ref,
                          replay, [timeline], [evidence], [recover])
medre replay           Recovery action (re-deliver historical events)
```

**Developer tooling (1 command):**

```
medre smoke            Fake bridge validation (alpha/CI tooling)
```

**Standard CLI (1 command):**

```
medre version          Version and platform info
```

**Pre-release note:** No deprecations, no compatibility shims, no migration
paths. All consolidation is clean restructuring before any public release.
`trace`, `evidence`, and `recover` continue to work as top-level commands
until they are folded into `inspect`. The consolidation direction is recorded
here; the timing is a separate decision.
