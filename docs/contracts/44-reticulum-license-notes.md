# Reticulum / LXMF License Notes

> Contract version: 2
> Last updated: 2026-05-12
> Track: 6 (Licensing & Compliance Observations)
> Supersedes: Nothing. New track.
> Status: Observation only. No legal conclusions. No code or packaging changes proposed.

This document records what the Reticulum License actually says, where ambiguity
exists, what that ambiguity means for MEDRE's operational use, and what
mitigation paths might exist in the future. It does not provide legal advice,
render compatibility opinions, or propose runtime changes.


## 1. Scope

- Confirmed text of the Reticulum License as applied to RNS and LXMF.
- Identification of restriction clauses and their surface-level scope.
- Operational implications for MEDRE as a downstream consumer.
- Open questions that remain unresolved.
- Possible future mitigation directions (speculative, not current work).

## 2. Non-goals

- Rendering legal conclusions about GPL compatibility, enforceability, or
  license classification.
- Changing MEDRE's own license, packaging, adapter structure, or runtime
  architecture.
- Implementing subprocess boundaries, adapter extraction, or feature work.
- Contacting upstream maintainers or negotiating license terms.


## 3. Confirmed License Text Observations

All observations in this section are verified against local copies of the
LICENSE files in `/home/jeremiah/dev/Reticulum` and `/home/jeremiah/dev/LXMF`.

### 3.1 License form

Both RNS and LXMF ship under the **Reticulum License**, authored by
Mark Qvist. The text is structurally derived from the MIT license (same grant
language, same warranty disclaimer) with two additional restriction clauses
inserted between the grant and the attribution requirement.

Key structural facts:

| Property | RNS (Reticulum) | LXMF |
|----------|-----------------|------|
| **License name** | Reticulum License | Reticulum License |
| **Copyright holder** | Mark Qvist (2016-2026) | Mark Qvist (2020-2025) |
| **setup.py `license` field** | `"Reticulum License"` | `"Reticulum License"` |
| **OSI approval** | Not listed on OSI approved list | Same |
| **SPDX identifier** | None assigned | Same |
| **Identical text** | Yes | Yes (same two restrictions) |

The license is not approved by the Open Source Initiative (OSI). It does not
appear in the SPDX license list. No standard classifier (e.g., `License :: 
OSI Approved :: MIT`) is used in either `setup.py`; both omit license
classifiers entirely.

### 3.2 Restriction clause 1: Harm to humans

> "The Software shall not be used in any kind of system which includes amongst
> its functions the ability to purposefully do harm to human beings."

Confirmed text. Present in both RNS and LXMF LICENSE files (lines 12-13 of
each).

The Zen of Reticulum (`Zen of Reticulum.md`, section "The Harm Principle",
lines 268-287) frames this as a philosophical commitment: the restriction is
described as encoding a "moral compass" into the legal terms, explicitly
preventing use in weapon systems, drone controllers, and surveillance tools.

### 3.3 Restriction clause 2: AI/ML training data exclusion

> "The Software shall not be used, directly or indirectly, in the creation of
> an artificial intelligence, machine learning or language model training
> dataset, including but not limited to any use that contributes to the
> training or development of such a model or algorithm."

Confirmed text. Present in both LICENSE files (lines 15-18 of each).

The Zen of Reticulum (`Zen of Reticulum.md`, section "Preserving Human
Agency", lines 299-313) explains this clause as a defense against
"predatory extraction" of open-source commons for ML training.

### 3.4 Contributor License Agreement

From `Contributing.md` (lines 56-58):

> "By contributing code to this project, you agree that copyright for the code
> is transferred to the Reticulum maintainers and that the code is irrevocably
> placed under the Reticulum License."

This means all contributed code is owned by the maintainers and uniformly
licensed. There is no possibility of mixed-licensed contributions that might
carry different terms.

### 3.5 Protocol vs. implementation distinction

The Zen of Reticulum (lines 289-297) makes an explicit distinction:

- The **protocol** (mathematical rules of how Reticulum works) is **public
  domain**. Anyone can implement it independently.
- The **reference implementation** (the Python code in the RNS package) carries
  the Reticulum License restrictions.

This distinction matters for anyone considering a clean-room reimplementation
of the protocol, though doing so is a significant engineering undertaking
outside the scope of this document.

### 3.6 MEDRE's own license

MEDRE declares `license = "GPL-3.0-or-later"` in
`pyproject.toml` (updated from MIT 2026-05-12). MEDRE depends on RNS/LXMF as optional transport
dependencies, not as core dependencies. The RNS/LXMF code is not included in
MEDRE's distribution; it is fetched at install time via `pip install lxmf`.


## 4. Unresolved Questions

None of the following questions are answered here. They are recorded because
they represent the actual ambiguity that downstream consumers face.

### 4.1 GPL compatibility

**The question:** Is the Reticulum License compatible with the GPL?

**Why it matters:** MEDRE uses GPL-3.0-or-later, so the question of Reticulum
License compatibility with GPL is directly relevant. MEDRE's GPL-3.0-or-later
license must be compatible with the Reticulum License when both are present in
an installation that includes the `[lxmf]` extra.

- MIT, by itself, is GPL-compatible. GPL-3.0-or-later is compatible with
  standard permissive licenses.
- The GPL requires that any additional restrictions on the combined work also
  be compatible with the GPL's own terms (GPLv3, section 7).
- The harm clause and the AI/ML exclusion clause are "further restrictions"
  that go beyond what MIT or GPL alone would impose.
- Whether such ethical-use restrictions render the license GPL-incompatible is
  a matter of legal interpretation that has no settled consensus in the
  open-source community.

**Status:** Unresolved. This document does not answer it.

### 4.2 Scope of the harm clause

**The question:** What constitutes a "system which includes amongst its
functions the ability to purposefully do harm to human beings"?

**Why it matters:** The phrasing "includes amongst its functions" is broad. A
mesh networking stack used as one component in a larger system could
theoretically be argued to be part of a system that has other harmful
functions, even if the mesh networking component itself is benign. The word
"purposefully" provides some narrowing, but the boundary is not precise.

For MEDRE specifically: MEDRE is a messaging framework. Nothing in its code,
architecture, or stated purpose involves harm to humans. The practical risk
of the harm clause affecting MEDRE's use of RNS/LXMF appears low. But the
imprecision of the language means the boundary is a judgment call, not a
mechanical test.

**Status:** Unresolved. The clause's scope depends on interpretation.

### 4.3 Scope of the AI/ML training exclusion

**The question:** What counts as "indirectly" contributing to AI/ML training?

**Why it matters:** The clause prohibits both direct and indirect use in AI/ML
training datasets. MEDRE does not use RNS/LXMF for AI training purposes.
However, "indirectly" is open-ended. If MEDRE's message throughput were used
as a benchmark or data source that later fed into an ML pipeline, the
indirect chain might be argued to exist. This is a theoretical concern, not a
practical one for MEDRE's current use, but the language breadth is worth
noting.

**Status:** Unresolved. The clause's reach depends on interpretation.

### 4.4 OSI classification

**The question:** Can the Reticulum License be called "open source" under
OSI's definition?

**Why it matters:** OSI's Open Source Definition (section 6, "No
Discrimination Against Fields of Endeavor") states that licenses must not
restrict anyone from using the software in a specific field. The harm clause
restricts use in systems designed to harm humans. The AI/ML exclusion
restricts use in model training. Both could be argued to violate OSD section
6. If so, the license is not "open source" by OSI's definition, even though
it grants broad permissions.

This does not affect whether the software can be used. It affects how the
license is classified and discussed.

**Status:** Unresolved. This is a classification question, not a usability
question.

### 4.5 Downstream propagation

**The question:** When MEDRE lists `lxmf` as an optional dependency, what
obligations (if any) propagate to MEDRE's users?

**Why it matters:** MEDRE is GPL-3.0-or-later-licensed. Its optional dependency on
LXMF/RNS means that users who install with `pip install ".[lxmf]"` will
receive Reticulum-licensed code. The GPL-3.0-or-later license on MEDRE itself does not
cover those optional dependencies. Users must independently satisfy the
Reticulum License terms for the RNS/LXMF packages they install.

MEDRE's `setup.py`/`pyproject.toml` lists LXMF as an optional extra, not a
core dependency. Users who do not install the `lxmf` extra never receive
Reticulum-licensed code. This optional-dependency boundary provides a natural
separation.

**Status:** Unresolved in terms of formal legal opinion. The practical
observation is that optional dependency installation puts the onus on the
user, not on MEDRE.


## 5. Operational Implications for MEDRE

These are factual observations about how the license situation affects MEDRE
today, not legal conclusions.

### 5.1 Current impact: minimal

MEDRE uses RNS/LXMF as an optional transport. The code is not vendored, not
forked, not modified. It is fetched from PyPI at install time. MEDRE's
GPL-3.0-or-later license governs MEDRE's code. The Reticulum License governs the RNS/LXMF
packages that users optionally install.

No current MEDRE functionality violates the stated restrictions (no harm
systems, no AI training use).

### 5.2 Distribution consideration

If MEDRE is ever distributed as a pre-built package (Docker image, appliance,
bundled distribution), the RNS/LXMF packages included in that distribution
would need to carry their LICENSE files and comply with the Reticulum License
attribution requirement. This is a standard distribution hygiene concern, not
unique to this license.

### 5.3 The license is the same regardless of MEDRE's own license choice

The task context notes that this ambiguity exists regardless of whether MEDRE
chooses MIT, GPL, or LGPL for itself. This is correct. The Reticulum License
restrictions apply to the RNS/LXMF packages. MEDRE's own license governs only
MEDRE's code. The two licenses operate in parallel, and the Reticulum License
ambiguity exists independently of what MEDRE chooses for itself.

### 5.4 Documentation burden

Contracts 34, 37, and 38 already flag the non-standard license as a
"Should"-level concern. This document (contract 44) provides the detailed
observations behind those flags. Any future release criteria or distribution
documentation should reference this document rather than repeating the
analysis.


## 6. Possible Future Mitigation Directions

These are speculative paths that could reduce licensing uncertainty in the
future. None are proposed as current work. All would require legal review
before implementation. They are listed here as possible directions, not
recommendations.

### 6.1 Package split: runtime dependency vs. development dependency

If MEDRE's LXMF adapter code were split so that the adapter package itself
does not carry RNS/LXMF as a dependency but instead documents the user's
responsibility to install it separately, the licensing boundary becomes
cleaner. The adapter code (GPL-3.0-or-later) would be separate from the transport library
(Reticulum License). Users would install both and accept both licenses
independently.

This is a packaging and distribution decision, not a code change. It does not
alter runtime behavior. It would require updating `pyproject.toml`,
installation documentation, and possibly CI configuration.

### 6.2 Service/subprocess boundary

If RNS/LXMF were run as an external service (e.g., a standalone Reticulum
daemon process) rather than as an in-process Python import, the license
scope might be limited to that service boundary. MEDRE would communicate with
the service over a network socket or IPC mechanism.

This would be a significant architectural change. The current `LxmfSession`
imports `RNS` and `LXMF` directly (as seen in the compat guards). Moving to
a subprocess model would require adapter-level refactoring and new transport
infrastructure. It is not a near-term option.

### 6.3 External adapter isolation

MEDRE's plugin/adapter architecture already supports optional transports with
graceful degradation. If the LXMF adapter were extracted into a standalone
package (e.g., `medre-adapter-lxmf`) with its own license declaration, the
core MEDRE package would carry no Reticulum License exposure. Users who want
LXMF support would install the adapter package and accept the Reticulum
License separately.

This is a distribution structure change. It does not require runtime
redesign. It aligns with MEDRE's existing optional-adapter pattern but
would require packaging, testing, and documentation updates.

### 6.4 Upstream engagement

The Reticulum maintainer could be asked whether they consider their license
GPL-compatible, or whether they would be willing to add a compatibility
exception. This is a social/license negotiation path, not a technical one.
The Zen of Reticulum suggests the maintainer holds strong philosophical
convictions about the restrictions, so changes are unlikely.

### 6.5 Protocol-level reimplementation

As noted in section 3.5, the Reticulum protocol itself is public domain. A
clean-room implementation of the protocol (not derived from the RNS codebase)
would not be subject to the Reticulum License. This is a large engineering
effort that would need to replicate transport, identity, routing, and
encryption functionality without referencing the RNS source code.

This is a theoretical option with extremely high cost. It is mentioned for
completeness, not as a practical near-term direction.


## 7. Sources Consulted

| Source | Location | What was examined |
|--------|----------|-------------------|
| RNS LICENSE file | `/home/jeremiah/dev/Reticulum/LICENSE` | Full text (29 lines) |
| LXMF LICENSE file | `/home/jeremiah/dev/LXMF/LICENSE` | Full text (29 lines) |
| Zen of Reticulum (RNS) | `/home/jeremiah/dev/Reticulum/Zen of Reticulum.md` | Harm Principle (lines 268-287), Preserving Human Agency (lines 299-313), Public Domain Protocol (lines 289-297) |
| Zen of Reticulum (LXMF copy) | `/home/jeremiah/dev/LXMF/Zen of Reticulum.md` | Same sections, identical text |
| RNS Contributing.md | `/home/jeremiah/dev/Reticulum/Contributing.md` | CLA (lines 56-58), Generative AI Policy (lines 50-54) |
| RNS setup.py | `/home/jeremiah/dev/Reticulum/setup.py` | License field, classifiers |
| LXMF setup.py | `/home/jeremiah/dev/LXMF/setup.py` | License field, classifiers, dependency on `rns>=1.2.0` |
| MEDRE pyproject.toml | `pyproject.toml` | `license = "GPL-3.0-or-later"` |
| Contract 34 (Dependency Reality Audit) | `docs/contracts/34-dependency-reality-audit.md` | Prior Reticulum License flag (section 4.4) |
| Contract 37 (Transport Maturity Classification) | `docs/contracts/37-transport-maturity-classification.md` | Non-standard license risk flag (section 7.2) |
| Contract 38 (Release Candidate Criteria) | `docs/contracts/38-release-candidate-criteria.md` | License documentation blocker (section 4.4) |


## 8. Summary

The Reticulum License is a modified MIT license with two use restrictions:
one prohibiting use in systems designed to harm humans, and one prohibiting
use in AI/ML training datasets. Both restrictions create open questions about
GPL compatibility, OSI classification, and downstream propagation. None of
these questions are settled or answered here.

For MEDRE today, the impact is minimal: RNS/LXMF are optional dependencies,
MEDRE does not vendor or modify them, and MEDRE's use case (mesh messaging)
does not appear to conflict with either restriction. The license ambiguity is
a documentation and distribution hygiene concern, not a blocking operational
issue.

If the ambiguity becomes actionable (e.g., a downstream consumer requires GPL
compatibility assurance, or MEDRE moves to bundled distribution), the
mitigation directions in section 6 provide starting points. All would require
legal review and are future decisions, not current architecture changes.
