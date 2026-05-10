# SPDX + Metadata Hygiene Audit

> Contract version: 1
> Last updated: 2026-05-10
> Track: 5 (SPDX + Metadata Hygiene)
> Status: Audit report. Records findings and governance-blocked actions. No metadata changes finalized — all actions pending governance decision.

This document is the deliverable for Track 5: a full audit of pyproject
metadata, SPDX identifiers, LICENSE/COPYING presence, source header strategy,
and classifier correctness. It records the current state, identifies
inconsistencies, and lists actions blocked on the governance license decision.


## 1. Findings Summary

| # | Item | Current State | Status | Blocking? |
|---|------|---------------|--------|-----------|
| F1 | `pyproject.toml` `license` field | `license = "MIT"` | Consistent with contract 42 §5.1 | No — but see F2 |
| F2 | LICENSE file | **Missing.** No LICENSE, LICENSE.txt, COPYING, or COPYING.md anywhere in repo. Neither sdist nor wheel contains license text. | **Harmful.** MIT §2 requires license text in distributions. PyPI consumers get no license text. | **Yes** — blocked on final license choice |
| F3 | License classifier | None. Removed in prior fix (contract 38 F8) because PEP 639 makes it redundant with `license` field. | Clean for PEP 639 setuptools >= 80. | No |
| F4 | Source file headers | **None.** Zero .py files have copyright, SPDX, or license headers. | Acceptable for MIT. If license changes to GPL/LGPL, per-file headers become important for copyleft enforcement. | No — post-governance |
| F5 | README license section | "Currently MIT." Updated to note governance review. | Now documented. | No |
| F6 | PKG-INFO `License-Expression` | `License-Expression: MIT` | Consistent with pyproject.toml. | No |
| F7 | Other metadata fields (`authors`, `urls`) | **Missing.** Known gap, tracked in contract 38 §7.2 F5/F6. | Not license-related. | No |


## 2. pyproject.toml Metadata Detail

```
[project]
name = "medre"
version = "0.1.0"
description = "Modular event communications runtime"
readme = "README.md"
requires-python = ">=3.11"
license = "MIT"                          # <-- F1: governance-pending comment added
classifiers = [                          # <-- F3: no License classifier (correct for PEP 639)
    "Development Status :: 3 - Alpha",
    "Intended Audience :: Developers",
    "Programming Language :: Python :: 3",
    "Programming Language :: Python :: 3.11",
    "Programming Language :: Python :: 3.12",
    "Topic :: Communications",
    "Typing :: Typed",
]
```

### Assessment

- `license = "MIT"` is a valid PEP 639 SPDX expression.
- No `License ::` classifier is present. This is correct: PEP 639 (setuptools >= 80)
  uses `license` field as `License-Expression` and rejects a `License ::` classifier
  alongside it. The prior fix (contract 38 F8) correctly removed the classifier.
- `authors` and `urls` are missing but not license-related (tracked in contract 38).


## 3. LICENSE File Gap (F2 — Harmful)

### Why this matters

MIT license §2 requires: "The above copyright notice and this permission notice
shall be included in all copies or substantial portions of the Software."

Without a LICENSE file:
- `python -m build` produces sdist/wheel without license text.
- `pip install medre` gives consumers no license text.
- PyPI landing page shows `License-Expression: MIT` but no file to read.

### Why not fixed now

The correct LICENSE file content depends on the final license choice:

| Final License | Required File | SPDX in pyproject |
|---------------|---------------|-------------------|
| Stay MIT | LICENSE (MIT text) | `license = "MIT"` |
| GPLv3-or-later | LICENSE (GPLv3 text) | `license = "GPL-3.0-or-later"` |
| LGPLv3-or-later | LICENSE (LGPLv3 text) | `license = "LGPL-3.0-or-later"` |

Creating a LICENSE file with MIT text now and then replacing it later is
wasteful and risks shipping an inconsistent state. The gap is documented
(contract 42 §8, this document §3) and blocked on governance.


## 4. Source Header Strategy (F4)

### Current state

No source files contain any of:
- `# SPDX-License-Identifier: ...`
- `# Copyright (c) ...`
- `# Licensed under ...`

### Assessment

For MIT: headers are optional. The project-level LICENSE file is sufficient.

For GPL/LGPL: headers are strongly recommended for copyleft enforcement. The
SPDX identifier `SPDX-License-Identifier: GPL-3.0-or-later` (or LGPL-3.0-or-later)
in each file makes the license unambiguous for automated scanning tools and
provides legal clarity if files are copied in isolation.

### Recommendation

Do not add headers until:
1. The final license is decided.
2. The project has more than one contributor (headers matter more then).

If GPL/LGPL is chosen, apply headers to all `.py` files in a single commit.
Template:

```python
# SPDX-License-Identifier: GPL-3.0-or-later  # (or LGPL-3.0-or-later)
# Copyright (c) 2025-2026 medre contributors
```

This is a post-governance task. Do not act on it now.


## 5. Docs Referencing MIT

These docs reference the project's own MIT license (not dependency licenses):

| Doc | Line | Content | Action Needed |
|-----|------|---------|---------------|
| `README.md` | 270 | License section | Updated this audit. Will need final update post-governance. |
| `docs/contracts/38-release-hygiene-audit.md` | 236 | F1: "Added license = MIT" | Historical record. No change. |
| `docs/contracts/38-release-candidate-criteria.md` | 342 | `license \| MIT \| ✅` | Update RC gate once governance decides. |
| `docs/contracts/42-contributor-governance.md` | 29-32, 121-123, 209 | "MIT licensed" / "License: MIT" | Primary governance doc. Update with final decision. |

These docs referencing dependency licenses are **not** project-license issues:

| Doc | Reference | Notes |
|-----|-----------|-------|
| `docs/contracts/19-meshcore-connectivity-readiness.md` | meshcore: MIT | Dependency. Correct. |
| `docs/contracts/34-dependency-reality-audit.md` | meshcore: MIT | Dependency. Correct. |
| `docs/runbooks/lxmf-alpha-operation.md` | Reticulum: custom license | Dependency. Correct. |
| `docs/runbooks/developer-environment.md` | Reticulum: non-standard | Dependency. Correct. |
| `docs/runbooks/meshcore-live-smoke.md` | meshcore: MIT | Dependency. Correct. |


## 6. Governance-Blocked Actions

These actions are correct and necessary but cannot proceed until the
maintainer finalizes the license direction (MIT / GPL-3.0-or-later /
LGPL-3.0-or-later):

| # | Action | Prerequisite | Priority |
|---|--------|--------------|----------|
| A1 | Create top-level LICENSE file with correct license text | Final license decision | **High** — blocks compliant distribution |
| A2 | Update `pyproject.toml` `license` field to final SPDX expression | Final license decision | **High** — package metadata |
| A3 | Add `License :: OSI Approved :: ...` classifier if not using PEP 639 `license` field | Final license decision | Medium — PEP 639 makes this optional |
| A4 | Update README.md license section with final license name and link | Final license decision | Medium |
| A5 | Update contract 42 §5.1 with final license | Final license decision | Medium |
| A6 | Update contract 38 RC criteria §6.3 license row | Final license decision | Low — RC gate |
| A7 | If GPL/LGPL: add SPDX headers to all .py files | Final license decision | Low — enforcement tool |
| A8 | If GPL/LGPL: verify dependency license compatibility | Final license decision | **High** — Reticulum is non-OSI, mindroom-nio license unknown |

### A8 is critical if copyleft is chosen

If the project moves to GPL or LGPL, dependency compatibility must be verified:

| Dependency | License | GPL-compatible? | LGPL-compatible? |
|-----------|---------|-----------------|------------------|
| msgspec | BSD-3-Clause | Yes | Yes |
| mindroom-nio | Unknown (fork of matrix-nio / ISC) | Likely yes | Likely yes |
| mtjk (Meshtastic) | Unknown (fork) | Unknown | Unknown |
| meshcore | MIT | Yes | Yes |
| lxmf | Reticulum License (custom) | **Unknown** | **Unknown** |
| Reticulum (rns) | Reticulum License (custom, non-OSI) | **Unknown** | **Unknown** |

The Reticulum License is non-OSI and has restrictions on AI training and certain
applications. Its compatibility with GPL/LGPL has not been analyzed. This must
be resolved before any copyleft license change.


## 7. Changes Made This Audit

| File | Change | Rationale |
|------|--------|-----------|
| `pyproject.toml` | Added governance-pending comment above `license = "MIT"` | Documents that the field is not finalized, prevents accidental "it says MIT so it's decided" assumption |
| `README.md` | Updated License section to note governance review and missing LICENSE file | Surface the governance state where contributors/consumers will see it |
| `docs/contracts/45-spdx-metadata-audit.md` | This document | Complete audit trail, governance-blocked action list |

No license metadata was changed. No source headers were added. No LICENSE file
was created.


## 8. Conclusion

The project's metadata is internally consistent (everything says MIT) and
technically correct for PEP 639. The one harmful gap — missing LICENSE file —
is documented and blocked on the governance decision. Once the maintainer
chooses between MIT, GPL-3.0-or-later, and LGPL-3.0-or-later, the action
items in §6 can proceed in a single pass.
