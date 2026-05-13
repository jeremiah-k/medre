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

### 1.1 Blocker severity definitions

All blockers in this document use the following severity taxonomy. These are not
suggestions. They are gates that must be satisfied or explicitly acknowledged.

| Severity | Meaning | RC Implication |
|----------|---------|----------------|
| **Must** | Required for RC. Absence prevents tagging. | No RC without resolution. |
| **Should** | Expected for RC. Absence requires documented rationale in release notes. | RC can ship, but the gap is a known limitation. |
| **Deferred** | Intentionally post-RC. Acknowledged in risk register. | No action at RC. Track in contract 39. |

The distinction matters. A "Must" item is a hard gate: the RC tag does not go
out until it is resolved. A "Should" item is a soft gate: the RC tag can go out
with the item listed as a known limitation in the release notes, provided the
limitation is also recorded in the operational risk register (contract 39).
"Deferred" items are explicitly out of scope for RC and must have an entry in
the risk register explaining the deferral rationale.

A "Should" that cannot be documented honestly should be promoted to "Must." If
the known limitation cannot be stated clearly, the gap is a blocker, not an
advisory.


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

### 2.2.1 Evidence quality thresholds

Recorded evidence must meet the following quality bar to count as "live
validated." Evidence that does not meet these thresholds is treated as missing
evidence.

**Lifecycle evidence** must include: adapter start command, clean stop with no
lingering tasks or exceptions, and confirmation that the adapter enters
`AdapterState.STOPPED`.

**Send evidence** must include: at least one message sent through the adapter's
`deliver()` method, a returned `AdapterDeliveryResult` with `success=True`, and
confirmation that `success=True` reflects actual transport acknowledgment (not
just "we queued it internally"). For radio transports where only local handoff
is confirmable, the evidence must explicitly state that `success` means local
radio acceptance, not end-to-end delivery.

**Receive evidence** must include: at least one inbound message observed through
the adapter's callback mechanism, with the message content matching what was
sent.

**Diagnostics evidence** must include: a `diagnostics()` call that returns
structured data with no secrets leaked, no SDK objects exposed, and transport
state reported.

Partial evidence (e.g., lifecycle works but send fails) does not count as live
validated. The evidence must be complete for the declared scope. If a transport
can start but cannot send, the honest status is "partial lifecycle, not
live-validated."

### 2.2.2 Evidence recording requirements

Each evidence entry in `docs/runbooks/operational-evidence.md` must include:

- Date and time of the test run.
- Python version and OS.
- Transport SDK version (e.g., `mtjk==2.7.8`, `mindroom-nio==0.25.3`).
- Hardware or service endpoint details (without secrets).
- The command that was run.
- The output or summary of results.
- Any deviations from expected behavior.

Evidence that lacks these fields is informal, not recorded. It may be useful
for development but does not satisfy the RC gate.

### 2.3 Soak Evidence

Soak testing is not required for RC but is required for any subsequent stable
release. The following soak criteria are recorded here for completeness and to
set expectations for what "stable" actually demands.

| Criterion | Description | Required For |
|-----------|-------------|--------------|
| Sustained send | Send 100+ messages over 10+ minutes without error | Stable |
| Reconnect resilience | Adapter recovers from intentional network disconnection | Stable |
| Memory stability | No unbounded growth over 1-hour run | Stable |
| Session restart | Stop + start produces clean state, no stale callbacks | Stable |
| Queue drainage | Outbound queue drains after reconnect without message loss or duplication | Stable |
| Long-run diagnostics | Diagnostics remain accurate after 1-hour continuous operation | Stable |

RC does not require soak evidence. RC requires acknowledgment that soak
evidence does not yet exist.

#### 2.3.1 Soak reproducibility

When soak testing is performed for stable release:

1. The test must be runnable from the repository without manual setup beyond
   what is described in the developer environment runbook.
2. The test must produce deterministic pass/fail output (not "seemed fine").
3. The test must log memory usage at regular intervals, not just at start/end.
4. The test must be re-runnable. A soak test that cannot be reproduced by
   another developer does not count as evidence.

#### 2.3.2 RC acknowledgment

Any RC release notes must include a statement of the form:

> Soak testing has not been performed. Sustained throughput, memory stability
> under load, and reconnect resilience under real network conditions are
> unknown. Do not use this RC for workloads requiring sustained reliability.


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
this confirmation (M14 in contract 32). This is a must-have for RC.


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

### 4.6 Transport parity honesty

No document may imply that all four transports have equivalent maturity. Matrix
and Meshtastic are live-validated (beta-candidate). MeshCore and LXMF are
unit-tested only (alpha-operational). This is not a parity gap to close through
documentation wording. It is a maturity difference that reflects actual evidence.
The transport table in the README must show this distinction clearly, and no
release notes or contract may describe all four transports as equally validated.


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
- That the license selection is final or settled.

### 5.3 README accuracy check

Before RC, verify README against this checklist:

- [ ] First sentence says "pre-beta" or equivalent.
- [ ] Transport table lists all four with correct maturity tiers.
- [ ] Live-validated status is accurate per `operational-evidence.md`.
- [ ] Known limitations section matches contracts 33, 34, 35, 36.
- [ ] E2EE scope is stated accurately (Matrix text only).
- [ ] Installation commands work from a clean virtualenv.
- [ ] No hype language ("production-ready", "enterprise", "scalable", "robust").
- [ ] License section honestly describes governance status (under review, not final).


## 6. Dependency and Reproducibility Requirements

### 6.1 Dependency pinning

| Requirement | Current State | RC Gate |
|------------|---------------|---------|
| Core dep pinned exactly | `msgspec==0.21.1` | ✅ |
| Transport deps have floor pins | `>=` constraints in pyproject.toml | ✅ |
| Fork dependencies documented | `mindroom-nio`, `mtjk` noted in comments | ✅ |
| Optional deps guarded by compat checks | `HAS_NIO`, `HAS_MESHTASTIC`, etc. | ✅ |

### 6.2 Clean install reproducibility

Before RC, the following clean-room verification must pass:

1. `python -m venv /tmp/medre-rc-test && source /tmp/medre-rc-test/bin/activate`
2. `pip install -e ".[dev]"`
3. `PYTHONPATH=src pytest -q` — must pass.
4. Repeat with each transport extra: `pip install -e ".[matrix]"`, run matrix
   unit tests, etc.
5. Verify no import errors for any installed extra.

#### 6.2.1 Reproducibility requirements

| Requirement | Detail |
|------------|--------|
| Python version | Must pass on Python 3.11+. Tested on the version listed in pyproject.toml `requires-python`. |
| OS | Must pass on at least one Linux distribution. macOS and Windows are not blocked but also not verified. |
| Dependency resolution | `pip install` must resolve without version conflicts. If dependency resolution fails, it is an RC blocker. |
| Clean venv | Verification must start from an empty virtualenv, not a developer's working environment. |
| No pre-built state | No `__pycache__`, `.pyc`, or build artifacts may exist before the test. Use `git clean -fdx` or equivalent. |

#### 6.2.2 Dependency reproducibility caveats

medre does not use a lock file. Dependency versions are pinned with floor
constraints (`>=`). This means:

- Exact dependency resolution may vary between RC and a later install.
- Binary wheel availability for `vodozemac` depends on platform.
- Fork dependencies (`mindroom-nio`, `mtjk`) are sourced from PyPI
  alternatives, not standard package indices.

For RC, this is acceptable. For stable, a lock file or hash-pinned requirements
file should be considered. The absence of a lock file at RC is documented, not
ignored.

### 6.3 pyproject.toml completeness

| Field | Current | RC Gate |
|-------|---------|---------|
| `name` | `medre` | ✅ |
| `version` | `0.1.0` | Must be bumped for RC (e.g. `0.2.0rc1`) |
| `description` | Present | ✅ |
| `readme` | `README.md` | ✅ |
| `license` | `MIT` | ⚠️ MIT declared; under governance review. GPL-3.0-or-later and LGPL-3.0-or-later under evaluation. See contract 42 §5, README §License. Final selection is deferred (contract 32 D17). |
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
| RC1 | All must-have items from contract 32 resolved | 1 blocker remaining (M14: Matrix third-party inbound) |
| RC2 | All four transports have live evidence OR scope is explicit | 2 transports pending |
| RC3 | Full unit suite passes from clean install | ✅ 2127/2127 |
| RC4 | README accurate | ✅ (this tranche) |
| RC4a | License governance consistently documented across README, risk register, contracts | ✅ (Track 7) |
| RC5 | pyproject.toml metadata complete | Authors + URLs pending; license governance noted |
| RC6 | No contradictory claims | Audit needed at RC time |
| RC7 | Third-party inbound confirmed for at least one transport | ⛔ (Matrix M14) |
| RC8 | Clean install reproducibility verified | Verify at RC time |
| RC9 | Version bumped | Pending |
| RC10 | Release notes drafted | Pending |

**Current assessment: Not ready for RC.** One must-have blocker (M14: Matrix third-party inbound) and two metadata gaps (authors, URLs) remain. License governance is now
consistently documented (README, risk register, contracts) but not resolved.
The honest path is to resolve blockers or scope the RC accordingly.


## 8. Architecture Scope and Risk Ownership

### 8.1 MEDRE as toolkit and optional runtime

MEDRE serves two roles: an importable toolkit (adapters, configs, results that
consumers use directly) and an optional runtime framework (session management,
reconnect logic, lifecycle coordination). These are not equivalent commitments.

The **toolkit** layer (adapters, configs, result types) is the stable contract.
Consumers who import individual adapters and manage their own lifecycle bear
their own operational risk. The adapter API is the commitment.

The **runtime framework** layer (sessions, reconnect, queue management) is
convenience code. It works in unit tests against mocks. It has not been
validated under sustained operation or real failure conditions. Consumers who
use the session framework inherit the operational risks documented in contract
39, sections O1, O2, R1, R2, LR1 through LR3, and Q1.

RC does not require the runtime framework to be production-grade. RC requires
that the distinction between toolkit and framework is documented so consumers
can make informed choices.

### 8.2 Risk ownership at RC

| Layer | Owned by | RC responsibility |
|-------|----------|-------------------|
| Adapter API contract | medre | Must be stable, tested, documented |
| Transport behavior | Upstream SDK/radio protocol | Documented as known limitation |
| Session lifecycle | medre (framework layer) | Unit-tested, soak-deferred |
| Credential management | Operator | Documented requirements |
| Reconnect resilience | medre (framework layer) | Mock-tested, live-deferred |
| Sustained throughput | Unknown | Soak-deferred, acknowledged |

This table says something specific: medre commits to the adapter API being
honest and tested. medre does not commit to the runtime framework being
reliable under sustained load. That commitment requires soak evidence that does
not exist yet.
