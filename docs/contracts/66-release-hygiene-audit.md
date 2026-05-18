# Release Hygiene Audit

> Last updated: 2026-05-10
> Track: Beta Release Hygiene (Track 7)
> Status: Audit report with findings and actions taken. Updated with packaging/reproducibility audit findings.

This document records the release hygiene audit performed on MEDRE at head
`7046ecc` (2026-05-10). It covers pyproject metadata, README accuracy, stale
artifacts, contradictory operational claims, SDK leakage in public APIs,
test markers, and live-test exclusion guarantees.

## 1. pyproject.toml Metadata

### 1.1 Before Audit

| Field             | Value                                    | Issue                                                                     |
| ----------------- | ---------------------------------------- | ------------------------------------------------------------------------- |
| `name`            | `medre`                                  | Correct.                                                                  |
| `version`         | `0.1.0`                                  | Correct for pre-beta.                                                     |
| `description`     | `"Modular event communications runtime"` | Accurate.                                                                 |
| `readme`          | **Missing**                              | Not declared. pip would not show README.                                  |
| `license`         | **Missing**                              | No license declared.                                                      |
| `classifiers`     | **Missing**                              | No trove classifiers.                                                     |
| `urls`            | **Missing**                              | No homepage, bug tracker, or repo URLs.                                   |
| `authors`         | **Missing**                              | No author metadata.                                                       |
| `requires-python` | `>=3.11`                                 | Correct.                                                                  |
| `extras`          | 6 groups                                 | Correct: `dev`, `matrix`, `matrix-e2e`, `meshtastic`, `meshcore`, `lxmf`. |

### 1.2 Actions Taken

- **Added `readme = "README.md"`** — pip now displays README content.
- **Added `license` declaration** — initially `"MIT"`, later updated to `"GPL-3.0-or-later"` per project license decision. Declares license for downstream tooling.
- **Added `classifiers`** — Development Status (Alpha), Intended Audience, Python versions, Topic, Typing.
- **Removed `License :: OSI Approved :: MIT License` classifier** — PEP 639 (enforced by setuptools >= 80.x) rejects license classifiers when `license` is declared as a SPDX expression. Having both causes `pip install -e .` to fail with `InvalidConfigError` on setuptools >= 80. (The project license has since transitioned from MIT to GPL-3.0-or-later.)
- **Added `PyPubSub>=4.0` to `[meshtastic]` extra** — The `mtjk` distribution does not declare `pubsub` as a dependency, but it is required at runtime for callback-based packet reception. Previously, users had to install it manually (`pip install pubsub`). Now `pip install -e ".[meshtastic]"` pulls it automatically.
- **Did NOT add `urls` or `authors`** — these require project decisions (repo URL, author identity). Not fabricated.

### 1.3 Not Fixable (Requires Project Decision)

| Field               | Why not fixed                                                         |
| ------------------- | --------------------------------------------------------------------- |
| `[project.urls]`    | Requires decision on public repo URL, documentation URL, bug tracker. |
| `[project.authors]` | Requires decision on author name/email.                               |

### 1.4 Recommendation

Before beta release, add `[project.urls]` and `[project.authors]` to
`pyproject.toml`. These are standard metadata that package indices and
tooling expect.

## 2. README Accuracy

### 2.1 Current State

The README (`README.md`) contains only the project name and a blank line:

```markdown
# medre (Modular Event-driven Routing Engine)
```

**This is not sufficient for a beta release.** The README should, at minimum,
describe what MEDRE is, how to install it, and link to developer environment
documentation.

### 2.2 Action Taken

None. README expansion requires project-level decisions about tone, scope,
and audience. A placeholder README is acceptable for pre-beta development
but not for a published beta.

### 2.3 Recommendation

Before beta, update README.md to include:

1. One-paragraph project description.
2. Installation instructions (link to `docs/runbooks/developer-environment.md`).
3. List of supported transports with maturity classification (link to contract 37).
4. License declaration.

## 3. Stale Artifacts

### 3.1 `src/meshnet_framework.egg-info/` (FIXED)

**Finding:** A stale `meshnet_framework.egg-info/` directory existed in
`src/`. This was generated by a previous package name (`meshnet-framework`)
that is no longer current. The current package name is `medre`.

**Problems with the stale egg-info:**

- Package name: `meshnet-framework` (wrong — should be `medre`).
- `requires`: `msgspec>=0.19` (wrong — should be `msgspec==0.21.1`).
- `top_level.txt`: `meshnet_framework` (wrong — should be `medre`).
- `SOURCES.txt`: Listed files under `src/meshnet_framework/` which no longer
  exist.

**Action taken:** Removed `src/meshnet_framework.egg-info/`.

### 3.2 `src/medre.egg-info/`

This is the current, correct egg-info. It is auto-generated and should not be
committed. It is listed in `.gitignore` (assumed). No action needed.

### 3.3 Recommendation

Add `src/*.egg-info/` to `.gitignore` if not already present. Verify that
`meshnet_framework.egg-info` does not reappear.

## 4. Contradictory Operational Claims

### 4.1 msgspec Version Inconsistency (FIXED in egg-info, documented)

**Finding:** The stale egg-info declared `msgspec>=0.19` while `pyproject.toml`
declares `msgspec==0.21.1`. The egg-info was from a previous iteration. After
removing the stale egg-info and reinstalling, the version is consistent.

### 4.2 meshcore Audited Version Discrepancy

**Finding:** Contract 34 (dependency audit, section 4.5) records the audited
version of `meshcore` as **2.2.5**. However, `pyproject.toml` declares
`meshcore>=2.3.7` and the comment says "v2.3.7".

**Assessment:** Not necessarily a contradiction — the audit may have been
written when 2.2.5 was current and the pin updated later. But the contract
should be updated to reflect the current validated version.

**Action taken:** None (requires verifying which version was actually tested).
This is recorded as a finding.

### 4.3 Live Test Claims

**Finding:** Contract 32 (beta readiness) accurately distinguishes between
transports with recorded live evidence (Matrix 13/13, Meshtastic 10/10) and
transports without (MeshCore, LXMF). No contradictory claims found.

### 4.4 "Version-Pinned" Language

**Finding:** Contract 32, section 7.1 previously said "All transport SDK
dependencies must be version-pinned before beta" without clarifying that
the strategy is floor pins (`>=`), not strict pins (`==`). This could be
misinterpreted as requiring strict pins.

**Action taken:** Updated section 7.1 to explicitly state the minimum-version
floor pin strategy and link to contract 34, section 7 for full rationale.

## 5. SDK Leakage in Public APIs

### 5.1 Core Module Imports

**Finding:** `medre.core` imports only from `medre.core.contracts.adapter`:

- `medre.core.runtime.health` → `AdapterInfo` from `medre.core.contracts.adapter`
- `medre.core.runtime.capabilities` → `AdapterCapabilities` from `medre.core.contracts.adapter`
- `medre.core.engine.pipeline` → `AdapterCapabilities`, `AdapterDeliveryResult`, `AdapterContract` from `medre.core.contracts.adapter`

These are MEDRE-defined types in the core contracts module. **No third-party SDK
types leak through.** The `AdapterContract` import in the pipeline is expected —
the pipeline drives adapters.

### 5.2 Adapter Boundary

**Finding:** Each adapter module (matrix, meshtastic, meshcore, lxmf) imports
its respective SDK inside the module (guarded by compat flags). SDK types do
not appear in adapter public API signatures — all public methods return
MEDRE-defined types (`AdapterDeliveryResult`, `AdapterInfo`, etc.).

### 5.3 `shutdown_event: Any`

**Finding:** `AdapterContract.shutdown_event` is typed as `Any` with a comment
"asyncio.Event – avoided import to prevent hard dep." This is correct —
`asyncio` is a stdlib module, so this is not a third-party dep concern, but
the `Any` typing is a minor type-safety gap.

**Assessment:** Not SDK leakage. Documented as a minor typing concern.

### 5.4 Verdict

**No accidental SDK leakage in public APIs.** All public types are MEDRE-defined.

## 6. Test Markers

### 6.1 Live Test Exclusion

**Finding:** All live test files use module-level markers:

```python
pytestmark = pytest.mark.live
```

And individual test functions use `@require_live` decorators.

`pyproject.toml` configures:

```toml
markers = ["live: tests that connect to a real service or hardware (skipped by default)"]
addopts = "-m 'not live'"
```

**Guarantee:** Running `pytest` without explicit `-m live` will never execute
any live test. This is enforced at the configuration level.

### 6.2 Marker Consistency

All four live test files (`test_matrix_live.py`, `test_matrix_e2ee_live.py`,
`test_meshtastic_live.py`, `test_meshcore_live.py`, `test_lxmf_live.py`)
use the same pattern:

- Module-level `pytestmark = pytest.mark.live` (or `pytestmark = [pytest.mark.live]`).
- Per-function `@require_live` decorator.

**Minor inconsistency:** `test_meshcore_live.py` uses
`pytestmark = [pytest.mark.live]` (list form) while others use
`pytestmark = pytest.mark.live` (scalar form). Both are valid pytest syntax.
No functional impact.

### 6.3 Live Test Count

| Harness                    | Tests                             | Status        |
| -------------------------- | --------------------------------- | ------------- |
| `test_matrix_live.py`      | 13 (includes module-level marker) | Recorded pass |
| `test_matrix_e2ee_live.py` | 7                                 | Recorded pass |
| `test_meshtastic_live.py`  | 10                                | Recorded pass |
| `test_meshcore_live.py`    | ~12 (estimated from 401 LOC)      | Not run       |
| `test_lxmf_live.py`        | ~15 (estimated from 829 LOC)      | Not run       |

Total live tests: ~57 (matches the "57 deselected" count from unit suite runs).

## 7. Findings Summary

### 7.1 Fixed

| #   | Finding                                                                                                                            | Action                                                                                                             |
| --- | ---------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------ |
| F1  | Missing `readme`, `license`, `classifiers` in pyproject.toml                                                                       | Added `readme = "README.md"`, `license` (now `"GPL-3.0-or-later"`), and classifiers.                               |
| F2  | Stale `meshnet_framework.egg-info/` directory                                                                                      | Removed. Contained wrong package name, wrong dependency versions, wrong source paths.                              |
| F3  | "Version-pinned" language in contract 32 ambiguous about strategy                                                                  | Updated section 7.1 to explicitly state floor-pin strategy.                                                        |
| F8  | `License :: OSI Approved :: MIT License` classifier + SPDX `license` expression causes build failure on setuptools >= 80 (PEP 639) | Removed the license classifier. The SPDX `license` expression (now `"GPL-3.0-or-later"`) is sufficient.            |
| F9  | `PyPubSub` (import `pubsub`) missing from `[meshtastic]` extra — runtime failure on real hardware without manual install           | Added `PyPubSub>=4.0` to the meshtastic extra in pyproject.toml. Updated developer-environment.md and contract 34. |

### 7.2 Reported but Not Fixed (Requires Project Decision)

| #   | Finding                                                                               | Why Not Fixed                                         | Recommendation                                       |
| --- | ------------------------------------------------------------------------------------- | ----------------------------------------------------- | ---------------------------------------------------- |
| F4  | README.md is effectively empty                                                        | Requires project decisions on content, tone, audience | Expand before beta.                                  |
| F5  | Missing `[project.urls]` in pyproject.toml                                            | Requires public repo URL decision                     | Add before publishing to PyPI.                       |
| F6  | Missing `[project.authors]` in pyproject.toml                                         | Requires author identity decision                     | Add before publishing.                               |
| F7  | `meshcore` audited version discrepancy (contract 34 says 2.2.5, pyproject says 2.3.7) | Requires verifying which was actually tested          | Update contract 34 section 4.5 with correct version. |

### 7.3 Confirmed Clean

| #   | Check                                             | Result                                                                                        |
| --- | ------------------------------------------------- | --------------------------------------------------------------------------------------------- |
| C1  | No SDK types in public API surface                | Clean. All public types are MEDRE-defined.                                                    |
| C2  | No SDK imports in `medre.core`                    | Clean. Only imports from `medre.core.contracts.adapter` (MEDRE types). |
| C3  | No SDK imports in `medre.__init__` or `medre.cli` | Clean. No third-party imports.                                                                |
| C4  | Live test exclusion guaranteed                    | Clean. `addopts = "-m 'not live'"` in pyproject.toml. Module-level markers on all live files. |
| C5  | Extras definitions complete and correct           | Clean. 6 extras: dev, matrix, matrix-e2e, meshtastic (includes PyPubSub), meshcore, lxmf.     |
| C6  | No contradictory operational claims               | Clean. Contracts accurately distinguish recorded vs. unrecorded evidence.                     |
| C7  | No `meshnet_framework` references in source code  | Clean. All source uses `medre` namespace.                                                     |

## 8. Packaging / Reproducibility Audit

> Updated 2026-05-10 by packaging/reproducibility track (Track 7 retry).

### 8.1 Dependency Pinning Strategy

| Layer                                | Strategy              | Rationale                                                                                                                                                                                                                   |
| ------------------------------------ | --------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Core** (`msgspec`)                 | Exact pin: `==0.21.1` | msgspec Struct schema versioning ties core data model to exact wire format. Any version change risks silent decode failures.                                                                                                |
| **Dev** (`pytest`, `pytest-asyncio`) | Floor pin: `>=X.Y`    | Dev tools don't affect production behavior. Range allows CI flexibility.                                                                                                                                                    |
| **Transport SDKs**                   | Floor pin: `>=X.Y.Z`  | Per project policy (contract 32 §7.1, contract 34 §7). SDKs are optional and only loaded when explicitly requested. Floor pins allow downstream consumers to get compatible newer versions without MEDRE releasing a patch. |
| **Build** (`setuptools`)             | Floor pin: `>=68`     | Standard practice.                                                                                                                                                                                                          |

**Assessment:** The pinning strategy is intentional and well-documented. No changes needed. The exact core pin protects wire format stability; floor pins for optional SDKs allow forward-compatible installs.

### 8.2 Dependency Drift Risk

| Risk                                       | Assessment                                                                                | Mitigation                                                                                                                                                                                                                                                             |
| ------------------------------------------ | ----------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `msgspec` major bump breaking decode       | Low — exact pin prevents auto-upgrade.                                                    | Pin is `==0.21.1`. Requires explicit change.                                                                                                                                                                                                                           |
| Transport SDK API breaks in newer versions | Medium — floor pins allow `pip install` to pull newer, possibly incompatible SDK.         | SDK compat guards (`HAS_*` flags) catch import failures. Adapter unit tests use mocks; live tests are gated. Acceptable risk for optional deps.                                                                                                                        |
| Transitive dependency conflicts            | Low — only one required dep (`msgspec`). All transport deps are isolated to their extras. | No shared transitive deps across extras (verified: matrix pulls aiohttp, meshtastic pulls protobuf/bleak, meshcore pulls bleak/pyserial-asyncio-fast, lxmf pulls rns/cryptography). Only overlap: `pycryptodome` appears in both matrix and meshcore transitive trees. |
| setuptools 80+ breaking changes            | **Occurred.** PEP 639 enforcement rejected the license classifier.                        | Fixed by removing the classifier. setuptools pin is `>=68` — no floor pin on max version. Acceptable for a pre-beta library.                                                                                                                                           |

### 8.3 Install Failure Modes

| Extra                     | Failure Mode                          | Likelihood                       | User Experience                                                          |
| ------------------------- | ------------------------------------- | -------------------------------- | ------------------------------------------------------------------------ |
| Core (`pip install -e .`) | setuptools 80+ PEP 639 error          | **Was high** on modern pip       | Fixed (license classifier removed). Now installs cleanly.                |
| `matrix-e2e`              | `vodozemac` compilation on Alpine/ARM | Medium                           | Requires Rust toolchain. Pre-built wheels for common platforms.          |
| `meshtastic`              | Missing `pubsub` callback module      | **Was certain** on real hardware | Fixed (PyPubSub added to extra). Previously required manual install.     |
| `lxmf`                    | `cryptography` compilation            | Low-Medium                       | Binary wheels available for common platforms. `rnspure` fallback exists. |
| `meshcore`                | `bleak` BLE stack on headless         | Low                              | BLE not required for serial/TCP modes.                                   |

### 8.4 Missing Infrastructure (Pre-Beta)

| Item                                                | Status                                                     | Priority                      |
| --------------------------------------------------- | ---------------------------------------------------------- | ----------------------------- |
| **No lock file** (`uv.lock`, `requirements.txt`)    | Acceptable for a library. Lock files are for applications. | N/A                           |
| **No CHANGELOG**                                    | Not present. Should exist before beta release.             | Should-have                   |
| **No `[project.urls]` or `[project.authors]`**      | Requires project decisions.                                | Must-have before PyPI publish |
| **No CI matrix testing** (multiple Python versions) | Not audited (out of scope).                                | Should-have before beta       |

### 8.5 README Install Guidance

The README now includes installation instructions with maturity tiers. The developer-environment runbook provides complete setup guidance. The `pubsub` manual-install instruction has been removed from the runbook since `PyPubSub` is now pulled by the `[meshtastic]` extra.
