# Beta Entry Validation Runbook

> Last updated: 2026-05-11
> Status: Evidence requirements for beta entry. Defines the minimum gate.
> Related: `docs/contracts/32-beta-readiness-checklist.md`

This document specifies the **minimum evidence required** before the MEDRE
project can enter beta. It is a gate checklist: every item must be satisfied
or explicitly deferred with a recorded reason.

Beta entry means the project is safe for external operators to evaluate on
their own infrastructure. It does **not** mean production-ready.


## 1. Deterministic Evidence (Required, No Hardware)

These must pass in a clean environment without any transport hardware or
credentials. Follow the procedure in
`docs/runbooks/developer-environment.md` §8.

| # | Evidence | Command | Pass criteria |
|---|----------|---------|---------------|
| D1 | Compile check | `python -m compileall -q src tests` | No output (clean compilation) |
| D2 | Unit test suite | `pytest -q` | All passed, 0 failed |
| D3 | Live tests excluded | `pytest -m live --co -q 2>/dev/null \| wc -l` | Live tests collected but not run by default |
| D4 | Console script installed | `medre version` | Prints version string |
| D5 | Config sample generation | `medre config sample` | Prints valid TOML |
| D6 | Config validation (fake) | `medre config check --config examples/configs/fake-multi-adapter.toml` | Reports valid |
| D7 | Config validation (matrix) | `medre config check --config examples/configs/matrix.toml` | Reports valid |
| D8 | Config validation (meshtastic) | `medre config check --config examples/configs/meshtastic-serial.toml` | Reports valid |
| D9 | Config validation (mixed) | `medre config check --config examples/configs/mixed-matrix-meshtastic.toml` | Reports valid |
| D10 | Paths resolution | `medre paths` | Prints resolved path directories |
| D11 | Adapter listing | `medre adapters` | Lists adapter types; fake adapters show as available |

All D1–D11 must pass. These are non-negotiable.


## 2. Live Smoke Evidence (At Least One Transport)

At least **one** transport must have live smoke test evidence recorded in
`docs/runbooks/operational-evidence.md`. The evidence must include:

| Field | Required |
|-------|----------|
| Test file path | Yes |
| Execution date | Yes |
| Executor (human/agent) | Yes |
| Transport SDK version | Yes |
| Python version | Yes |
| MEDRE commit or version | Yes |
| Total tests run | Yes |
| Passed / Failed / Skipped | Yes |
| Adapter start | Yes |
| Health check → healthy | Yes |
| Outbound send | Yes |
| Stop → clean teardown | Yes |
| Caveats observed | Yes (can be "none") |
| Reconnect observations | Yes (can be "not tested") |

Transports without live evidence must be recorded as **NOT EXECUTED** with
the reason (e.g., "no hardware available", "no credentials provisioned").
See `docs/runbooks/operational-evidence.md` for the current state.


## 3. Soak Evidence (Desired, Not Blocking)

Soak tests prove sustained operation. They are **desired** for beta entry
but not hard-blocking if the transport has passed live smoke.

| Level | Duration | Required for beta |
|-------|----------|-------------------|
| Short CI dry run | Default harness (50 iterations) | Desired, not blocking |
| Manual soak | 30–300 seconds with real endpoint | Desired for smoke-tested transports |
| Extended soak | >300 seconds | Not required for beta |

If soak tests have not been executed, record **NOT EXECUTED** in
`docs/runbooks/operational-evidence.md` soak sections with the reason.

See `docs/runbooks/soak-testing.md` for procedures.


## 4. Evidence Honesty Requirements

All evidence must follow these rules:

1. **No fabrication.** Do not invent, simulate, or extrapolate live test
   results. If a test was not executed, record **NOT EXECUTED**.

2. **No credential assumptions.** Do not assume credentials, tokens, or
   hardware exist. If the environment was not available, say so.

3. **Record the reason.** Every **NOT EXECUTED** entry must include:
   - Why it was not executed (e.g., "no MeshCore hardware available")
   - What command *would* be run (e.g., `pytest tests/test_meshcore_live.py -m live -v`)
   - What environment variables *would* be needed

4. **Distinguish deterministic from live.** Unit test passes do **not**
   constitute live evidence. Record them separately.

5. **Include exact versions.** SDK version, Python version, firmware version,
   and MEDRE commit must be recorded for every live execution.


## 5. Beta Entry Decision Checklist

Before declaring beta:

- [ ] All D1–D11 deterministic checks pass in a clean environment
- [ ] At least one transport has live smoke evidence recorded
- [ ] All transports without live evidence have **NOT EXECUTED** with reasons
- [ ] Operational evidence document is up to date
- [ ] Soak evidence recorded or deferred with reason
- [ ] No known crashes or data-loss bugs open
- [ ] `docs/runbooks/developer-environment.md` clean-env procedure is current
- [ ] `docs/runbooks/operational-evidence.md` reflects actual state
- [ ] `docs/runbooks/soak-testing.md` procedures are documented

### Deferred items

If any item is deferred, record it here:

| Item | Reason | Expected resolution |
|------|--------|---------------------|
| (example) MeshCore live smoke | No hardware available | Operator with MeshCore device executes |
| (example) LXMF live soak | No Reticulum network | Operator with Reticulum setup executes |


## 6. Relationship to Other Documents

| Document | Relationship |
|----------|-------------|
| `docs/runbooks/developer-environment.md` | Clean-env procedure (§8) satisfies D1–D11 |
| `docs/runbooks/operational-evidence.md` | Records live evidence and NOT EXECUTED status |
| `docs/runbooks/soak-testing.md` | Procedures for soak evidence |
| `docs/contracts/32-beta-readiness-checklist.md` | Contract-level beta criteria (references this runbook) |
