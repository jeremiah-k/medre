# Release Candidate Criteria

> Contract version: 1
> Last updated: 2026-05-10
> Track: Beta Release Hygiene (Track 5)
> Supersedes: Contract 32 (beta-readiness-checklist) sections 1, 2, 4. Refines and extends for RC gate.
> Status: Criteria document. Defines what must be true before a release candidate tag.

This document defines the criteria for promoting medre from pre-beta
development to a tagged release candidate. It is a gate, not a roadmap. Every
item must be satisfied before an RC tag is cut. Items are organized by domain.

A release candidate is a specific commit tagged with a version number (e.g.
`v0.2.0rc1`) that the project asserts is suitable for external beta testers to
evaluate. It is not a claim of production readiness. It is a claim of
operational honesty: the software does what the documentation says it does, the
known limitations are documented, and the install path works from a clean
environment.


## 1. Readiness Definition

A release candidate must satisfy all of the following:

1. **All must-have items from contract 32 are resolved** — no blockers remain.
2. **All four transports have live evidence recorded** — or transports without
   live evidence are explicitly labeled "alpha-operational, not live-validated"
   in the README and release notes.
3. **The full unit test suite passes from a clean install** — `PYTHONPATH=src
   pytest -q` produces zero failures.
4. **The README accurately describes current state** — including maturity
   tiers, known limitations, and beta expectations.
5. **pyproject.toml metadata is complete** — including authors, URLs, and
   correct development status classifier.
6. **No contradictory operational claims exist anywhere in the codebase** — no
   document claims production readiness for any transport.

These are non-negotiable. If any one is false, there is no RC.


## 2. Operational Evidence Requirements

### 2.1 Unit Test Evidence

| Requirement | Evidence | Pass Criteria |
|------------|----------|---------------|
| Full suite passes | `PYTHONPATH=src pytest -q` output | 0 failed, 0 errors |
| Compile check clean | `python -m compileall -q src tests` | No output |
| No import errors | `python -c "import medre"` | Clean import |
| All extras installable | `pip install -e ".[dev,matrix,matrix-e2e,meshtastic,meshcore,lxmf]"` in clean venv | Successful install |

### 2.2 Live Test Evidence

Each transport must have recorded evidence in
`docs/runbooks/operational-evidence.md`:

| Transport | Minimum Live Evidence | Current Status |
|-----------|----------------------|----------------|
| Matrix plaintext | Lifecycle + send + receive + diagnostics | ✅ 13/13 recorded 2026-05-10 |
| Matrix E2EE | Encrypted room send/receive | ✅ 7/7 recorded 2026-05-10 |
| Meshtastic | Lifecycle + send + diagnostics against real radio | ✅ 10/10 recorded 2026-05-10 |
| MeshCore | Lifecycle + send + diagnostics against real hardware | ⛔ Not run — requires radio hardware |
| LXMF | Lifecycle + send + diagnostics against real Reticulum | ⛔ Not run — requires Reticulum instance |
| Matrix inbound (third-party) | Inbound message from second account | ⛔ Not confirmed |

**RC gate:** Either all transports have live evidence, or transports without it
are explicitly excluded from the RC scope with documented rationale.

### 2.3 Soak Evidence

Soak testing is not required for RC but is required for any subsequent stable
release. The following soak criteria are recorded here for completeness:

| Criterion | Description | Required For |
|-----------|-------------|--------------|
| Sustained send | Send 100+ messages over 10+ minutes without error | Stable |
| Reconnect resilience | Adapter recovers from intentional network disconnection | Stable |
| Memory stability | No unbounded growth over 1-hour run | Stable |
| Session restart | Stop + start produces clean state, no stale callbacks | Stable |

RC does not require soak evidence. RC requires acknowledgment that soak
evidence does not yet exist.


## 3. Live Validation Gate

### 3.1 What a passing live test proves

- The adapter can start against a real endpoint.
- The adapter can send at least one message and return an
  `AdapterDeliveryResult` with `success=True`.
- The adapter can report diagnostics with no secrets and no SDK object leakage.
- The adapter can stop cleanly.

### 3.2 What a passing live test does NOT prove

- Sustained throughput.
- Reliability under failure conditions.
- Multi-hop delivery for radio transports.
- Concurrent operation.
- Performance characteristics.

### 3.3 Third-party inbound confirmation

At least one transport must confirm inbound message reception from a source
that is not the medre instance itself. Currently Matrix is the candidate for
this confirmation (M16 in contract 32). This is a must-have for RC.


## 4. Transport-Specific RC Blockers

### 4.1 Matrix

| Blocker | Severity | Status | Resolution |
|---------|----------|--------|------------|
| Third-party inbound not confirmed | Must | ⛔ | Run test with second Matrix account, record evidence |
| Access token plain string | Should | Unresolved | Document secure handling in runbook |
| Fork maintenance (`mindroom-nio`) | Should | Unresolved | Document dependency, pin version |
| E2EE requires `ignore_unverified_devices` | Should | Documented | Document in README and release notes |
| No cross-signed device trust | Should | Documented | Document as deliberate trade-off |

### 4.2 Meshtastic

| Blocker | Severity | Status | Resolution |
|---------|----------|--------|------------|
| `deliver()` returns `None` (queued) | Should | Unresolved | Document limitation |
| Duplicate-send risk from retries | Should | Documented | Document in README |
| BLE mode untested | Should | Unresolved | Test or document as unsupported |
| Fork maintenance (`mtjk`) | Should | Unresolved | Document dependency, pin version |

### 4.3 MeshCore

| Blocker | Severity | Status | Resolution |
|---------|----------|--------|------------|
| No live evidence | Must | ⛔ | Run harness against real hardware |
| Low session test count (18 functions) | Should | Unresolved | Increase to 40+ |
| BLE mode untested | Should | Unresolved | Test or document as unsupported |
| SDK maturity (`meshcore_py` small community) | Should | Unresolved | Pin version, document risk |

### 4.4 LXMF

| Blocker | Severity | Status | Resolution |
|---------|----------|--------|------------|
| No live evidence | Must | ⛔ | Run harness against real Reticulum |
| Delivery state progression unvalidated | Should | Unresolved | Add state progression test |
| Identity file security (64-byte raw key) | Should | Unresolved | Document file permission requirements |
| Reticulum daemon dependency | Should | Unresolved | Document setup requirements |
| Non-standard license (Reticulum License) | Should | Documented | Document for downstream consumers |

### 4.5 RC scoping options

If live evidence cannot be obtained for MeshCore or LXMF before the RC window:

1. **Tag RC with only Matrix + Meshtastic in scope.** MeshCore and LXMF remain
   alpha-operational. This is the honest option.
2. **Delay RC until all four have live evidence.** This is the complete option.
3. **Do not tag RC.** Continue development until gate is met.

Option 1 is acceptable. Option 2 is preferable. Option 3 is conservative.


## 5. Documentation State Requirements

### 5.1 Must-exist documents

| Document | Content | Status |
|----------|---------|--------|
| README.md | Project description, transports, limitations, philosophy, install, beta expectations | ✅ (contract 38, Track 5) |
| `docs/runbooks/developer-environment.md` | Full setup guide | ✅ |
| `docs/runbooks/operational-evidence.md` | Live test results per transport | ✅ (Matrix, Meshtastic recorded) |
| `docs/runbooks/secure-credentials.md` | Credential handling | ✅ |
| `docs/contracts/37-transport-maturity-classification.md` | Per-transport maturity | ✅ |
| `docs/contracts/32-beta-readiness-checklist.md` | Beta criteria | ✅ |
| `docs/contracts/36-radio-limitations.md` | Fire-and-forget model | ✅ |

### 5.2 Must-not-exist claims

No document in the repository may claim:

- Production readiness for any transport.
- Guaranteed delivery for any radio transport.
- E2EE support beyond Matrix text in encrypted rooms.
- Support for reactions, media, attachments, or rich messages.
- Deployment, scaling, or operational support.

### 5.3 README accuracy check

Before RC, verify README against this checklist:

- [ ] First sentence says "pre-beta" or equivalent.
- [ ] Transport table lists all four with correct maturity tiers.
- [ ] Live-validated status is accurate per `operational-evidence.md`.
- [ ] Known limitations section matches contracts 33, 34, 35, 36.
- [ ] E2EE scope is stated accurately (Matrix text only).
- [ ] Installation commands work from a clean virtualenv.
- [ ] No hype language ("production-ready", "enterprise", "scalable", "robust").


## 6. Dependency and Reproducibility Requirements

### 6.1 Dependency pinning

| Requirement | Current State | RC Gate |
|------------|---------------|---------|
| Core dep pinned exactly | `msgspec==0.21.1` | ✅ |
| Transport deps have floor pins | `>=` constraints in pyproject.toml | ✅ |
| Fork dependencies documented | `mindroom-nio`, `mtjk` noted in comments | ✅ |
| Optional deps guarded by compat checks | `HAS_NIO`, `HAS_MESHTASTIC`, etc. | ✅ |

### 6.2 Clean install reproducibility

Before RC:

1. `python -m venv /tmp/medre-rc-test && source /tmp/medre-rc-test/bin/activate`
2. `pip install -e ".[dev]"`
3. `PYTHONPATH=src pytest -q` — must pass.
4. Repeat with each transport extra: `pip install -e ".[matrix]"`, run matrix
   unit tests, etc.
5. Verify no import errors for any installed extra.

### 6.3 pyproject.toml completeness

| Field | Current | RC Gate |
|-------|---------|---------|
| `name` | `medre` | ✅ |
| `version` | `0.1.0` | Must be bumped for RC (e.g. `0.2.0rc1`) |
| `description` | Present | ✅ |
| `readme` | `README.md` | ✅ |
| `license` | `MIT` | ✅ |
| `classifiers` | Present | ✅ |
| `requires-python` | `>=3.11` | ✅ |
| `authors` | **Missing** | ⛔ Must add before RC |
| `urls` | **Missing** | ⛔ Must add before RC (requires project decision) |

### 6.4 Version bump

RC requires a version bump in `pyproject.toml`. Recommended: `0.2.0rc1`.
Development Status classifier should be updated to `4 - Beta` at RC time.


## 7. RC Checklist Summary

| # | Item | Status |
|---|------|--------|
| RC1 | All must-have items from contract 32 resolved | 3 blockers remaining (M14, M15, M16) |
| RC2 | All four transports have live evidence OR scope is explicit | 2 transports pending |
| RC3 | Full unit suite passes from clean install | ✅ 2127/2127 |
| RC4 | README accurate | ✅ (this tranche) |
| RC5 | pyproject.toml metadata complete | Authors + URLs pending |
| RC6 | No contradictory claims | Audit needed at RC time |
| RC7 | Third-party inbound confirmed for at least one transport | ⛔ (Matrix M16) |
| RC8 | Clean install reproducibility verified | Verify at RC time |
| RC9 | Version bumped | Pending |
| RC10 | Release notes drafted | Pending |

**Current assessment: Not ready for RC.** Three must-have blockers (M14, M15,
M16) and two metadata gaps (authors, URLs) remain. The honest path is to
resolve these or scope the RC accordingly.
