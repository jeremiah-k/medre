# SPDX + Metadata Hygiene Audit

> Contract version: 2
> Last updated: 2026-05-12
> Track: 5 (SPDX + Metadata Hygiene)
> Status: Audit report. License updated to GPL-3.0-or-later. LICENSE file added. Metadata changes applied.

This document is the deliverable for Track 5: a full audit of pyproject
metadata, SPDX identifiers, LICENSE/COPYING presence, source header strategy,
and classifier correctness. It records the current state (GPL-3.0-or-later),
identifies resolved findings, and tracks remaining deferred items.


## 1. Findings Summary

| # | Item | Current State | Status | Blocking? |
|---|------|---------------|--------|-----------|
| F1 | `pyproject.toml` `license` field | `license = "GPL-3.0-or-later"` | ✅ Updated from MIT to GPL-3.0-or-later (2026-05-12). Consistent with dependency reality (contracts 40, 41). | No |
| F2 | LICENSE file | ✅ Present. Standard FSF GPLv3 text with copyright holder placeholder. | Resolved. | No |
| F3 | License classifier | `License :: OSI Approved :: GNU General Public License v3 or later (GPLv3+)` | ✅ Added. | No |
| F4 | Source file headers | **None.** Zero .py files have copyright, SPDX, or license headers. | Acceptable for now. Per-file headers may be added post-beta for stronger copyleft enforcement. | No — post-beta |
| F5 | README license section | ✅ Updated to reflect GPL-3.0-or-later with links to LICENSE file and governance docs. | Done. | No |
| F6 | PKG-INFO `License-Expression` | `License-Expression: GPL-3.0-or-later` | Consistent with pyproject.toml. | No |
| F7 | Other metadata fields (`authors`, `urls`) | **Missing.** Known gap, tracked in contract 38 §7.2 F5/F6. | Not license-related. | No |


## 2. pyproject.toml Metadata Detail

```
[project]
name = "medre"
version = "0.1.0"
description = "Modular event communications runtime"
readme = "README.md"
requires-python = ">=3.11"
license = "GPL-3.0-or-later"              # <-- F1: updated from MIT (2026-05-12)
classifiers = [
    "Development Status :: 4 - Beta",
    "Intended Audience :: Developers",
    "License :: OSI Approved :: GNU General Public License v3 or later (GPLv3+)",  # <-- F3: added
    "Programming Language :: Python :: 3",
    "Programming Language :: Python :: 3.11",
    "Programming Language :: Python :: 3.12",
    "Topic :: Communications",
    "Typing :: Typed",
]
```

### Assessment

- `license = "GPL-3.0-or-later"` is a valid PEP 639 SPDX expression.
- The `License ::` classifier is present alongside the `license` field. Note:
  PEP 639 (setuptools >= 80) uses `license` field as `License-Expression` and
  may reject a `License ::` classifier alongside it in future setuptools versions.
  The current configuration works with setuptools >= 68.
- `authors` and `urls` are missing but not license-related (tracked in contract 38).


## 3. LICENSE File (F2 — Resolved)

### Resolution

A top-level `LICENSE` file with the standard FSF GPLv3 text (including copyright
holder placeholder) was added on 2026-05-12 as part of the GPL-3.0-or-later
license transition. The file is present and consistent with `pyproject.toml`.

- `python -m build` produces sdist/wheel with license text included.
- `pip install medre` gives consumers the GPLv3 license text.
- PyPI landing page shows `License-Expression: GPL-3.0-or-later` with the file
  to read.

This finding is resolved. No further action needed.


## 4. Source Header Strategy (F4)

### Current state

No source files contain any of:
- `# SPDX-License-Identifier: ...`
- `# Copyright (c) ...`
- `# Licensed under ...`

### Assessment

For GPL-3.0-or-later: headers are strongly recommended for copyleft enforcement.
The SPDX identifier `SPDX-License-Identifier: GPL-3.0-or-later` in each file
makes the license unambiguous for automated scanning tools and provides legal
clarity if files are copied in isolation.

### Recommendation

Do not add headers until:
1. The project has more than one contributor (headers matter more then).

Apply headers to all `.py` files in a single commit when ready. Template:

```python
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (c) 2025-2026 medre contributors
```

This is a post-beta task. Do not act on it now.


## 5. Docs Referencing MIT (Updated 2026-05-12)

These docs previously referenced the project's own MIT license. Status after
GPL-3.0-or-later transition:

| Doc | Status | Notes |
|-----|--------|-------|
| `README.md` | ✅ Updated | GPL-3.0-or-later declared. LICENSE file linked. |
| `docs/contracts/40-license-governance.md` | ✅ Updated | Records GPL-3.0-or-later decision and rationale. |
| `docs/contracts/41-third-party-license-audit.md` | ✅ Updated | Compatibility notes updated to reference GPL-3.0-or-later. |
| `docs/contracts/42-contributor-governance.md` | ✅ Updated | License grant updated to GPL-3.0-or-later. |
| `docs/contracts/44-reticulum-license-notes.md` | ✅ Updated | MEDRE license references updated. |
| `docs/contracts/45-spdx-metadata-audit.md` | ✅ Updated | This document. |
| `docs/contracts/66-release-hygiene-audit.md` | Historical | Records "Added license = MIT" as historical fact. No change needed. |
| `docs/contracts/38-release-candidate-criteria.md` | ✅ Updated | RC gate license field updated to GPL-3.0-or-later (resolved). |

These docs referencing **dependency** licenses are not project-license issues:

| Doc | Reference | Notes |
|-----|-----------|-------|
| `docs/contracts/19-meshcore-connectivity-readiness.md` | meshcore: MIT | Dependency. Correct. |
| `docs/contracts/34-dependency-reality-audit.md` | meshcore: MIT | Dependency. Correct. |
| `docs/runbooks/lxmf-alpha-operation.md` | Reticulum: custom license | Dependency. Correct. |
| `docs/runbooks/developer-environment.md` | Reticulum: non-standard | Dependency. Correct. |
| `docs/runbooks/meshcore-live-smoke.md` | meshcore: MIT | Dependency. Correct. |


## 6. Governance Actions (Resolved 2026-05-12)

The license governance decision was finalized on 2026-05-12: GPL-3.0-or-later.
All previously governance-blocked actions have been completed:

| # | Action | Status |
|---|--------|--------|
| A1 | Create top-level LICENSE file with GPLv3 text | ✅ Done. Standard FSF GPLv3 text with copyright holder placeholder. |
| A2 | Update `pyproject.toml` `license` field to GPL-3.0-or-later | ✅ Done. |
| A3 | License classifier | ✅ Added `License :: OSI Approved :: GNU General Public License v3 or later (GPLv3+)`. |
| A4 | Update README.md license section | ✅ Done. GPL-3.0-or-later with links to LICENSE and governance docs. |
| A5 | Update contract 42 §5.1 with final license | ✅ Done. |
| A6 | Update contract 38 RC criteria §6.3 license row | Pending. Not blocking (RC gate, not beta gate). |
| A7 | Add SPDX headers to all .py files | Deferred. Post-beta task. Headers are recommended for GPL enforcement but not required for beta. |
| A8 | Verify dependency license compatibility | ✅ Done. Documented in contracts 40, 41. All dependencies are compatible with GPL-3.0-or-later (BSD, ISC, Apache-2.0 are permissive; GPL-3.0-only is compatible with GPL-3.0-or-later; Reticulum License ambiguity documented in contract 44). |


## 7. Changes Made This Audit

| File | Change | Rationale |
|------|--------|-----------|
| `pyproject.toml` | Updated `license = "GPL-3.0-or-later"` (from MIT) | Aligns with dependency reality (mtjk is GPL-3.0-only). |
| `pyproject.toml` | Added `License :: OSI Approved :: GNU General Public License v3 or later (GPLv3+)` classifier | Trove classifier for PyPI classification. |
| `LICENSE` | Created with standard FSF GPLv3 text | Compliant distribution requires license text. |
| `README.md` | Updated License section to GPL-3.0-or-later | Surface the license where consumers see it. |
| `docs/contracts/40-license-governance.md` | Updated to record GPL-3.0-or-later decision | Governance record. |
| `docs/contracts/41-third-party-license-audit.md` | Updated compatibility notes | Reflect GPL-3.0-or-later in all dependency assessments. |
| `docs/contracts/42-contributor-governance.md` | Updated license grant to GPL-3.0-or-later | Contributor expectations match project license. |
| `docs/contracts/44-reticulum-license-notes.md` | Updated MEDRE license references | Accurate cross-references. |
| `docs/contracts/45-spdx-metadata-audit.md` | This document updated | Reflect resolved state of all findings. |


## 8. Conclusion

The project's metadata is internally consistent. The license is GPL-3.0-or-later,
declared in `pyproject.toml`, reflected in the LICENSE file, documented in the
README, and recorded in governance contracts 40–45. The license classifier is
present. The only remaining action is A7 (SPDX source headers), which is
deferred to post-beta as a copyleft enforcement hardening measure, not a beta
requirement.
