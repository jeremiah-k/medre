# medre Beta Candidate Notes

> Version: 0.1.0 (beta candidate)
> Date: 2026-05-12
> Status: Beta candidate. Not a release. Not production-ready.

This document records the beta candidate state of medre as of 2026-05-12. It is
an honest assessment of what works, what is validated, what is not, and what
remains unresolved. It does not overclaim.

## Quick Start

```bash
# Clone and install with dev dependencies
git clone <repo-url> && cd medre
pip install -e ".[dev]"

# Run the full unit test suite (no network, no hardware)
PYTHONPATH=src pytest -q
# Expected: 3200+ passed, live tests skipped by default

# Compile check
python -m compileall -q src tests
# Expected: no output

# Start the runtime with a sample config (uses fake adapters)
medre config sample > /tmp/medre-test.toml
# Edit adapters to use fake transports, then:
PYTHONPATH=src medre run --config /tmp/medre-test.toml
```

For a specific transport:

```bash
pip install -e ".[matrix]"       # Matrix (requires mindroom-nio)
pip install -e ".[meshtastic]"   # Meshtastic (requires mtjk + PyPubSub)
pip install -e ".[meshcore]"     # MeshCore (requires meshcore)
pip install -e ".[lxmf]"         # LXMF / Reticulum (requires lxmf + rns)
pip install -e ".[matrix-e2e]"   # Matrix with E2EE (adds vodozemac, Rust)
```

## Transport Maturity

| Transport      | Live-validated | Maturity                              | Unit tests           | Caveats                                                                                                                                                                                                                                              |
| -------------- | -------------- | ------------------------------------- | -------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Matrix**     | Yes            | Beta-candidate                        | 2,903 LOC (10 files) | Fork dependency (`mindroom-nio`). E2EE requires Rust. Third-party inbound xfail (no second sender available). Federation validated (sk.community → matrix.org rooms).                                                                                |
| **Meshtastic** | Yes            | Beta-candidate                        | 3,429 LOC (8 files)  | Fire-and-forget delivery. Duplicate-send risk from retries. Fork dependency (`mtjk`).                                                                                                                                                                |
| **MeshCore**   | **No**         | Alpha-operational                     | 2,321 LOC (7 files)  | Unit-tested only. Hardware probe: CP2104 `/dev/ttyUSB0` identified (likely T-Beam, no serial chatter). Firmware flash pending follow-up validation. Maturity: Alpha (Tier 2) per Contract 62 — cannot promote until hardware-validated.              |
| **LXMF**       | **No**         | Alpha-operational (experimental risk) | 3,381 LOC (8 files)  | Unit-tested only. Local source repos available. Reticulum live path pending follow-up validation. Delivery state model unvalidated. Experimental downgrade risk per Contract 62 §5.4 if live path proves non-viable. Reticulum non-standard license. |

**Live-validated means smoke-tested once against a real endpoint.** It does not
mean sustained, reliable, or production-tested.

### Live Test Evidence

| Transport   | Date       | Result                                                | Endpoint                                                                                                          |
| ----------- | ---------- | ----------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------- |
| Matrix      | 2026-05-12 | 12/12 passed, 1 xfailed                               | sk.community homeserver (federated to matrix.org rooms)                                                           |
| Matrix      | 2026-05-10 | 13/13 passed                                          | matrix.org homeserver                                                                                             |
| Matrix E2EE | 2026-05-10 | 7/7 passed                                            | Encrypted room on matrix.org                                                                                      |
| Meshtastic  | 2026-05-12 | CLI validation: device info, 1 outbound, 4 reconnects | Serial `/dev/ttyACM0` (CH9102F, T-LoRa V2.1-1.6), firmware 2.7.19                                                 |
| MeshCore    | —          | Not run                                               | CP2104 `/dev/ttyUSB0` (likely T-Beam) identified. No serial chatter. Firmware flash pending follow-up validation. |
| LXMF        | —          | Not run                                               | Local source repos at `/home/jeremiah/dev`. Reticulum live path setup pending follow-up validation.               |

**Evidence lifecycle** (per Contract 61 §8):

| Transport           | evidence_type | confidence | verified_at | verification_scope                                                                                            | environment                                                   |
| ------------------- | ------------- | ---------- | ----------- | ------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------- |
| Matrix              | tested        | medium     | 2026-05-12  | Live smoke test, single homeserver (sk.community), 12/12 pass + 1 xfail. No soak, no sustained operation.     | Dev laptop, sk.community → matrix.org federation, Python 3.12 |
| Matrix (matrix.org) | tested        | low        | 2026-05-10  | Historical live smoke test, 13/13 passed. Stale (3 days, not re-confirmed at current commit).                 | Dev laptop, matrix.org homeserver                             |
| Matrix E2EE         | tested        | low        | 2026-05-10  | Historical live E2EE smoke, 7/7 passed. Stale. No cross-session crypto store validation.                      | Dev laptop, encrypted room on matrix.org                      |
| Meshtastic          | observed      | medium     | 2026-05-12  | Manual CLI serial validation: device discovery, 1 outbound, 4 reconnects. NOT MEDRE adapter (CLI-level only). | Dev laptop, serial `/dev/ttyACM0`, T-LoRa V2.1-1.6, fw 2.7.19 |
| MeshCore            | planned       | low        | never       | Hardware probe only (CP2104 identified, no serial chatter). No firmware, no live test.                        | N/A                                                           |
| LXMF                | planned       | low        | never       | Local source repos available. No Reticulum network, no live test.                                             | N/A                                                           |

> **Note:** `verified_at` dates are historical as of 2026-05-13. Confidence reflects both scope limits and age. Matrix 2026-05-10 entries are H-tier (historical) — not re-confirmed against the current codebase. MeshCore and LXMF have no evidence of any tier beyond S (unit tests).

Per-transport maturity definitions:
[docs/contracts/37-transport-maturity-classification.md](docs/contracts/37-transport-maturity-classification.md)

Cross-adapter operational maturity matrix (evidence-backed):
[docs/contracts/62-adapter-operational-maturity-matrix.md](docs/contracts/62-adapter-operational-maturity-matrix.md)

### Follow-Up Validation (Pending)

The following live validations require future hardware/software operations. They are NOT blocking beta release for transports labeled alpha-operational.

| Operation                                  | Transport  | Prerequisite                               | Status  |
| ------------------------------------------ | ---------- | ------------------------------------------ | ------- |
| `esptool chip_id` on CP2104 `/dev/ttyUSB0` | MeshCore   | Physical access to device                  | Pending |
| MeshCore firmware flash from local source  | MeshCore   | Confirm chip type, build firmware binary   | Pending |
| MeshCore live smoke test                   | MeshCore   | MeshCore firmware running on CP2104 device | Pending |
| Reticulum install from local source        | LXMF       | Configure transport, generate identity     | Pending |
| LXMF live smoke test                       | LXMF       | Running Reticulum instance                 | Pending |
| Matrix current-tranche live re-run         | Matrix     | Valid credentials (token or password)      | Pending |
| Meshtastic adapter live re-run             | Meshtastic | `mtjk` in project venv                     | Pending |

## Known Limitations

These are not bugs. They are honest boundaries of what medre covers today.

- **Fire-and-forget radio delivery.** Meshtastic, MeshCore, and LXMF report
  `success=True` when the local radio or router accepts the message. Remote
  receipt is not confirmed. This is inherent to the protocols.
- **Duplicate sends.** Meshtastic and MeshCore sessions retry transient failures
  (up to 3 attempts). Duplicates are possible. Consumers must deduplicate.
- **No exactly-once delivery.** No transport guarantees exactly-once delivery.
  No radio ACK confirmation is plumbed through.
- **No sustained throughput testing.** Live tests are smoke tests (send a
  message, verify it arrives). No load testing exists.
- **No reconnect resilience testing.** No live test exercises adapter behavior
  during real network failures.
- **Text only.** No reactions, edits, deletes, attachments, media, or rich
  message types.
- **Two transports lack live evidence.** MeshCore and LXMF are unit-tested only. MeshCore: CP2104 device at `/dev/ttyUSB0` identified (hardware probe, likely T-Beam) but no serial chatter — firmware flash required. LXMF: local source repos available, Reticulum live path setup pending. Both are Alpha (Tier 2) per Contract 62. LXMF has experimental downgrade risk if Reticulum live path proves non-viable. They may work perfectly or may have fundamental issues with real hardware/software — the gap is specific and documented, not vague.
- **Third-party Matrix inbound xfail.** Live test `test_inbound_message_received`
  xfails because no second Matrix account was available to send during the 30s
  window. Self-echo suppression verified; third-party inbound exercised only in
  unit tests.
- **Federation-tested homeserver (sk.community).** Live validation on 2026-05-12
  used sk.community (Synapse 1.152.1) federated to matrix.org rooms. The
  `.well-known` redirects to `matrix.sk.community`; `MATRIX_HOMESERVER` must be
  set to the resolved base URL. The `/logout/all` endpoint is disabled on
  sk.community (single-device `/logout` works).
- **Fork dependencies.** Matrix uses `mindroom-nio` (fork of `matrix-nio`).
  Meshtastic uses `mtjk` (fork of `meshtastic-python`). Fork maintenance is an
  ongoing responsibility.
- **No admin APIs, webhooks, HTTP endpoints, or deployment tooling.**
- **Runtime is early.** The config-driven runtime (`medre run`) works in tests
  but has not been exercised under sustained load or real operational conditions.

## License

medre is licensed under **GPL-3.0-or-later**. See the `LICENSE` file for the
full license text.

The LXMF adapter depends on Reticulum, which uses a non-OSI-approved license
(the Reticulum License). This is an unresolved ambiguity in the dependency
chain: medre's own license choice does not resolve the upstream license question
for downstream consumers who use the LXMF transport.

License governance: [docs/contracts/40-license-governance.md](docs/contracts/40-license-governance.md).
Third-party audit: [docs/contracts/41-third-party-license-audit.md](docs/contracts/41-third-party-license-audit.md).

## Beta Checklist Status

Summary from [docs/contracts/32-beta-readiness-checklist.md](docs/contracts/32-beta-readiness-checklist.md):

| Category                            | Satisfied | Blocked | Notes                                                                                                                                                    |
| ----------------------------------- | --------- | ------- | -------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Cross-transport must-haves (M1–M10) | 10/10     | 0       | All pass.                                                                                                                                                |
| Per-transport must-haves (M11–M14)  | 3/4       | 1       | M14 (Matrix third-party inbound) blocked on external resource (second account). MeshCore/LXMF live are deferred experimental (E1–E2), not beta-blocking. |
| Packaging (P1–P6)                   | 6/6       | 0       | All verified.                                                                                                                                            |
| License governance                  | Resolved  | 0       | GPL-3.0-or-later. LICENSE file present.                                                                                                                  |
| RuntimeWarning cleanup              | Resolved  | 0       | `coroutine 'run' was never awaited` fixed.                                                                                                               |

**3 items remain blocked** on external resources (hardware, network, second
account). Beta can ship with these documented as known limitations if the
transports in question are labeled alpha-operational.

## Architecture

medre ships as one Python package with two usage modes:

1. **Importable toolkit.** Adapters, codecs, sessions, renderers, and the event
   model are library components you import and wire into your own async Python
   application. No runtime dependency.

2. **Config-driven runtime.** `medre run` reads a TOML config file, assembles
   adapters, and manages a deterministic lifecycle with startup-derived health
   classification (not active supervision; see Contract 56 §4.1).
   This is convenience code, not a
   stable commitment.

Architecture layers:

- **Core** (`medre.core.*`): Frozen, schema-versioned `CanonicalEvent` (msgspec
  Struct), event bus, rendering pipeline, routing/planning, storage, identity.
  No transport dependencies.
- **Transport adapters**: Four adapters (Matrix, Meshtastic, MeshCore, LXMF),
  each with its own codec, renderer, session, config, error types, and compat
  guard. Each adapter owns its transport lifecycle entirely.
- **Runtime orchestration**: `RuntimeBuilder` assembles adapters from parsed
  TOML config, starts them in deterministic order, and provides classified
  lifecycle with deterministic startup health assessment.

See `README.md` for the full architecture description.

## What This Release Is Not

- Not production-ready.
- Not claiming exactly-once delivery.
- Not claiming radio ACK confirmation.
- Not claiming sustained throughput or reliability.
- Not providing admin APIs, webhook servers, or deployment tooling.
- Not a complete Matrix client (text messages only, no reactions/edits/media).
- Not a Meshtastic device management tool (message send/receive only).
- Not a Reticulum/LXMF network operator (single-node direct delivery only).
- Not security-audited by a third party.

## Changed Files (This Tranche)

### Track 5: License

- `pyproject.toml`: License updated to `GPL-3.0-or-later`, classifier added, dev status updated to Beta.
- `LICENSE`: Added. Standard FSF GPLv3 text with copyright holder placeholder.
- `README.md`: License section updated.
- `docs/contracts/40-license-governance.md`: Version 2. License decision recorded.
- `docs/contracts/41-third-party-license-audit.md`: Version 2. Updated to reflect GPL-3.0-or-later.
- `docs/contracts/42-contributor-governance.md`: Version 2. License updated, relicensing section updated.
- `docs/contracts/43-distribution-boundary-analysis.md`: Version 2. Updated date.
- `docs/contracts/44-reticulum-license-notes.md`: Version 2. Updated date.
- `docs/contracts/45-spdx-metadata-audit.md`: Version 2. All findings updated to resolved.

### Archived: runner.py deleted 2026-05-14

> **Note:** The following is historical only. `tests/test_runner.py` and `medre.runner` no longer exist. The runtime is now started via `medre.cli.run_commands`.

- `tests/test_runner.py` (DELETED 2026-05-14): Fixed `coroutine 'run' was never awaited` by adding `coro.close()` in mock `fake_asyncio_run`. Added regression test `test_main_no_unawaited_coroutine_warning`. The file was subsequently deleted when `medre.runner` was replaced by `medre.cli.run_commands`.

### Track 7: Beta Checklist

- `docs/contracts/32-beta-readiness-checklist.md`: Version 3. D17/D18 resolved. NB1 resolved. Classification summary updated. S6a/S6b/R5 updated to reflect GPL decision.

### Track 8: Release Notes

- `docs/releases/beta-candidate-notes.md`: This file. Created.
