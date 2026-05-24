# License Governance

> Contract version: 2
> Last updated: 2026-05-12
> Track: 10 (License Governance + Beta Governance Formalization)
> Supersedes: Version 1 (2026-05-10). License decision finalized: GPL-3.0-or-later.
> Status: Governance. Records license direction, dependency pressure, open questions, and decision constraints. License decision: **GPL-3.0-or-later** (effective 2026-05-12).

## 1. Purpose

This document governs the intended license direction for medre. It records
the dependency license landscape, the copyleft pressures those dependencies
create, the architectural factors that affect licensing analysis, and the
open questions that remain unresolved.

No metadata changes (`pyproject.toml`, `LICENSE` file) are made by this
document. The governance record must be complete and reviewed before any
license field changes.

This is not legal advice. License decisions affect downstream users and
contributors. When in doubt, consult qualified legal counsel.

## 2. Declared License

| Field                        | Value                                                                                  |
| ---------------------------- | -------------------------------------------------------------------------------------- |
| `pyproject.toml license`     | `GPL-3.0-or-later`                                                                     |
| `LICENSE` file               | Present. Standard FSF GPLv3 text with copyright holder placeholder.                    |
| `README.md`                  | License section updated to reflect GPL-3.0-or-later.                                   |
| `pyproject.toml classifiers` | Includes `License :: OSI Approved :: GNU General Public License v3 or later (GPLv3+)`. |
| `Development Status`         | Updated to `3 - Alpha`. Pre-release; no stable public API.                             |

The license was changed from MIT to GPL-3.0-or-later on 2026-05-12 by project
governance decision. The decision reflects the dependency reality documented in
section 4 and contract 41: medre's in-tree adapters import GPL-licensed
dependencies (Meshtastic via `mtjk`, Reticulum via `rns`/`lxmf`), making MIT
incompatible with the actual distribution.

## 3. Project Architecture Relevant to Licensing

medre has two architectural layers with different licensing implications.

### 3.1 Toolkit layer

The toolkit layer is the stable contract: adapters, configs, result types,
codecs, renderers, metadata types, and error types. Consumers import these
directly and wire them into their own code. The toolkit layer has no runtime
lifecycle management. It is stateless where possible.

This layer is what downstream users consume as an importable library.

### 3.2 Runtime/framework layer

The runtime layer (sessions, reconnect logic, queue management, lifecycle
coordination) is convenience code. It works in unit tests against mocks. It
has not been validated under sustained operation. The runtime layer is not a
stable commitment at this stage.

### 3.3 Why this distinction matters for licensing

An importable toolkit and a runtime framework have different licensing
profiles. A toolkit consumer who never uses the runtime layer should not
inherit copyleft obligations from framework decisions they don't use. A
runtime consumer who pulls in the full stack may face different obligations.

The architectural boundary is real (core never imports adapter code; adapters
never import each other), but the code ships in a single distribution
(package `medre`). How package boundaries interact with copyleft is a
meaningful open question.

## 4. Dependency License Landscape

### 4.1 Core dependency

| Package   | Version    | License      | Required | Pressure          |
| --------- | ---------- | ------------ | -------- | ----------------- |
| `msgspec` | `==0.21.1` | BSD-3-Clause | Yes      | None. Permissive. |

### 4.2 Optional transport dependencies

| Package                          | Version floor | License                        | Optional group | Pressure                                                                                   |
| -------------------------------- | ------------- | ------------------------------ | -------------- | ------------------------------------------------------------------------------------------ |
| `mtjk` (meshtastic-python fork)  | `>=2.7.8`     | **GPL-3.0-or-later**           | `[meshtastic]` | **Strong copyleft.** The highest-pressure dependency.                                      |
| `mindroom-nio` (matrix-nio fork) | `>=0.25.3`    | ISC                            | `[matrix]`     | None. Permissive.                                                                          |
| `meshcore`                       | `>=2.3.7`     | MIT                            | `[meshcore]`   | None. Permissive.                                                                          |
| `lxmf`                           | `>=0.9.6`     | **Reticulum License** (custom) | `[lxmf]`       | **Ambiguous.** Non-standard, non-OSI. Restricts AI training data and certain applications. |

### 4.3 Transitive dependencies of note

| Package           | Pulled in by               | License           | Notes                                                                    |
| ----------------- | -------------------------- | ----------------- | ------------------------------------------------------------------------ |
| `rns` (Reticulum) | `lxmf`                     | Reticulum License | Same custom license as lxmf. Required at runtime when lxmf is installed. |
| `PyPubSub`        | Explicit in `[meshtastic]` | BSD-2-Clause      | Permissive. Added because mtjk doesn't declare it.                       |
| `bleak`           | `meshcore`                 | MIT               | Permissive.                                                              |
| `vodozemac`       | `mindroom-nio[e2e]`        | Apache-2.0        | Permissive.                                                              |

### 4.4 Development dependencies

| Package          | License    | Notes                     |
| ---------------- | ---------- | ------------------------- |
| `pytest`         | MIT        | Not shipped. No pressure. |
| `pytest-asyncio` | Apache-2.0 | Not shipped. No pressure. |

## 5. The Meshtastic GPL Pressure

This is the strongest copyleft factor in medre's dependency graph.

### 5.1 Facts

- Upstream `meshtastic-python` is GPL-3.0-or-later.
- `mtjk` is a fork of `meshtastic-python`. It inherits the GPL-3.0-or-later
  license from upstream. Forking does not change the license.
- medre's Meshtastic adapter (`medre.adapters.meshtastic`) directly imports
  `meshtastic` (the import name for mtjk) and calls its APIs: `TCPInterface`,
  `SerialInterface`, `sendText`, `sendData`, the pubsub callback system.
- The adapter lives in-tree, in the same distribution (`medre`), as all other
  code.
- The Meshtastic adapter is optional at install time. A user can `pip install
medre` without the `[meshtastic]` extra and never touch GPL code.

### 5.2 The copyleft question

GPL-3.0's copyleft applies when you distribute a work "based on" a GPL'd
work. The key question: does the Meshtastic adapter, which directly imports
and calls GPL-licensed code, make the combined distribution of medre a
GPL-derivative work?

Arguments that the combined work is GPL-obligated:

- The Meshtastic adapter is not a standalone program. It is a module inside
  the medre package. It imports mtjk (GPL) and calls its API. Python
  `import` of a GPL module into a non-GPL module creates a derivative work
  under the FSF's interpretation of dynamic linking.
- The adapter ships in the same distribution (same `pyproject.toml`, same
  pip-installable package). "Mere aggregation" defenses are weaker when code
  ships together in one package.
- Even though the dependency is optional, the adapter code itself is always
  present. The import only fails at runtime when mtjk isn't installed, but
  the code is distributed together.

Arguments that it might not be GPL-obligated:

- The adapter is never imported unless the user explicitly activates it. Core
  medre never imports from `medre.adapters.meshtastic`. The import chain is
  user-directed.
- The compat guard pattern (`HAS_MESHTASTIC = bool(importlib.util.find_spec("meshtastic"))`)
  means the GPL code is only ever loaded when the user opts in.
- An argument exists that optional plugin-style adapters are "aggregation"
  rather than derivation, particularly when the plugin interface is generic.

### 5.3 Assessment

The honest assessment is that the GPL obligation is likely triggered for the
Meshtastic adapter and potentially for the combined distribution when a user
installs the `[meshtastic]` extra. The "optional dependency" defense has
merit but is not universally accepted in GPL interpretation. The in-tree
distribution (same package) weakens the aggregation argument.

**This is not a settled legal question.** medre should not proceed on the
assumption that optional extras insulate it from GPL obligations.

### 5.4 What this means for MIT

If GPL obligations extend to the combined distribution, then medre cannot
accurately declare itself MIT-only. An MIT declaration would be misleading
to downstream users who install `medre[meshtastic]` and receive GPL-licensed
code in the same package.

The `pyproject.toml` now says `license = "GPL-3.0-or-later"`, reflecting the
Meshtastic GPL reality. The license was changed on 2026-05-12.

## 6. The Reticulum/LXMF License Ambiguity

### 6.1 Facts

- Reticulum (the `rns` package) uses the Reticulum License, a custom license
  written by Mark Qvist.
- LXMF uses the same license.
- The Reticulum License is not OSI-approved. It is not a standard open-source
  license.
- The license includes restrictions that standard permissive and copyleft
  licenses do not:
  - Restrictions on use of the software for AI training data.
  - Restrictions on certain application types (the license text should be read
    directly for specifics).
- `pip install medre[lxmf]` installs both `lxmf` and `rns` as transitive
  dependencies.

### 6.2 The ambiguity

Because the Reticulum License is non-standard, its compatibility with other
licenses is not well-established. It is not clearly permissive (it has use
restrictions). It is not clearly copyleft (it does not use standard copyleft
mechanisms). Its restrictions on AI training data and certain applications
make it incompatible with strict open-source definitions.

### 6.3 Impact on medre

- medre's LXMF adapter imports `lxmf`, which pulls in `rns`. Both use this
  custom license.
- Downstream users who install the `[lxmf]` extra receive software under a
  license they may not have reviewed.
- The non-standard nature means medre cannot make assumptions about what
  downstream users can or cannot do with LXMF/Reticulum.
- This is not a copyleft pressure (the Reticulum License does not have
  standard copyleft terms), but it is a downstream clarity issue.

### 6.4 Governance position

The Reticulum License ambiguity does not force a medre license change in the
way the Meshtastic GPL does. It does require:

1. Clear documentation that the `[lxmf]` extra includes non-OSI-licensed
   software.
2. A note in downstream expectations (section 11) about reviewing the
   Reticulum License before using the LXMF adapter.
3. No pretense that medre controls or understands the full implications of
   the Reticulum License.

## 7. Optional Extras vs. Combined Works

### 7.1 How medre structures optional dependencies

medre uses `pip install medre[meshtastic]` extras. Each extra is a group in
`[project.optional-dependencies]`. The base install (`pip install medre`)
pulls in only `msgspec` (BSD-3-Clause).

Each transport adapter has a compat guard:

```python
# medre.adapters.meshtastic.compat
HAS_MESHTASTIC = bool(importlib.util.find_spec("meshtastic"))
```

Core medre never imports from any adapter package. Adapters never import from
each other. The user or runtime layer decides which adapters to load.

### 7.2 What this means in practice

A user who installs only the base package receives MIT-declared code with only
permissive dependencies. No GPL or custom-licensed code is present.

A user who installs `medre[meshtastic]` receives GPL-3.0-or-later code (mtjk)
in the same Python environment as medre's Meshtastic adapter code.

A user who installs `medre[lxmf]` receives custom-licensed code (Reticulum
License) in the same Python environment.

A user who installs `medre[matrix]` or `medre[meshcore]` receives only
permissive-licensed dependencies (ISC and MIT respectively).

### 7.3 The combined-work analysis

The critical question: is `pip install medre[meshtastic]` a single combined
work, or is it two separate works (medre + mtjk) that happen to be installed
together?

Under GPL, the answer depends on how closely the works are integrated. medre's
Meshtastic adapter:

- Calls mtjk's public API.
- Receives callbacks from mtjk via pubsub.
- Is not a fork or modification of mtjk.
- Uses mtjk as a library, not as a modified version.

This is the "use a GPL library" pattern, not the "modify a GPL program"
pattern. Whether using a GPL library as a dependency makes your work a
derivative is the core contested question in GPL interpretation.

### 7.4 Governance position

medre should not rely on the optional-extra structure as a definitive shield
against GPL obligations. The structure is a mitigating factor, not a
guarantee. The governance position is that the GPL pressure from mtjk is real
and must be addressed honestly in the project's license selection.

## 8. GPL-3.0-or-later vs. LGPL-3.0-or-later

This section evaluates two candidate licenses for medre. No selection is
finalized here. The evaluation is recorded so that the decision, when made,
has documented reasoning.

### 8.1 GPL-3.0-or-later

**What it does:** Copyleft. Anyone who distributes medre (or a work based on
it) must distribute the source under GPL-3.0-or-later terms. Downstream users
cannot incorporate medre into proprietary software without complying with
copyleft.

**Alignment with Meshtastic adapter:**

- Strong alignment. medre's Meshtastic adapter uses GPL code (mtjk). If medre
  is GPL-3.0-or-later, there is no license conflict. Both are the same
  license family. No copyleft analysis needed for that adapter.
- The Meshtastic ecosystem (firmware, protocol specs, client libraries) has
  strong copyleft culture. Aligning with GPL respects that ecosystem.

**Impact on toolkit usability:**

- Downstream users who import medre adapters into their own code must license
  their code under GPL-3.0-or-later (or a compatible license) if they
  distribute the result.
- This is a significant constraint for an "importable toolkit." Commercial
  users, proprietary applications, and projects with non-GPL-compatible
  licenses cannot use medre without accepting copyleft.
- The toolkit layer (section 3.1) becomes copyleft-encumbered for all
  adapters, not just Meshtastic. A user who only wants the Matrix adapter
  (ISC dependency) still receives GPL-licensed medre code.

**Impact on runtime/framework layer:**

- The runtime layer is already a convenience layer with limited production
  validation. Copyleft on the runtime is less impactful because downstream
  users are less likely to embed it in proprietary products.

**Contributor implications:**

- Contributors must license their contributions under GPL-3.0-or-later.
- This is standard for copyleft projects. No unusual burden.

**Philosophical alignment:**

- GPL-3.0-or-later is the strongest copyleft option. It ensures medre and all
  derivative works remain free software. This aligns with the Meshtastic
  community's values and with a broader copyleft philosophy for
  communications infrastructure.

### 8.2 LGPL-3.0-or-later

**What it does:** Lesser copyleft. Allows medre to be used as a library
without copyleft infecting the consuming application. Copyleft applies only
to modifications of medre itself, not to code that merely uses medre's API.

**Alignment with Meshtastic adapter:**

- Complex. The Meshtastic adapter imports GPL code (mtjk). If medre is
  LGPL, the adapter module itself still links to GPL code. The LGPL does not
  resolve the GPL compliance question for the Meshtastic adapter. The GPL
  obligations from mtjk remain.
- The LGPL would protect downstream users of the Matrix, MeshCore, and LXMF
  adapters (none of which use GPL dependencies) from copyleft obligations.
  But the Meshtastic adapter would still create GPL exposure.
- A mixed scenario: LGPL for medre overall, but the Meshtastic adapter
  effectively GPL-encumbered by its dependency. This is honest but complex
  to communicate.

**Impact on toolkit usability:**

- Strong positive. Downstream users can import medre adapters into proprietary
  code without triggering copyleft (for adapters that don't use GPL deps).
- This preserves the "importable toolkit" promise for Matrix, MeshCore, and
  LXMF transports.
- The toolkit layer remains usable by commercial and proprietary consumers
  for three of four transports.

**Impact on runtime/framework layer:**

- The runtime framework can be used without copyleft obligations, which
  matches its status as convenience code with limited validation.

**Contributor implications:**

- Contributors must license their modifications to medre under LGPL-3.0-or-
  later. Their own applications that use medre are not affected.

**Philosophical tension:**

- LGPL is a compromise. It provides copyleft protection for medre's own code
  (modifications must stay free) but does not extend copyleft to consumers.
- For a communications infrastructure toolkit, this may or may not align with
  the project's values. The Meshtastic ecosystem is GPL, but Matrix, MeshCore,
  and LXMF are not.
- The question is whether medre wants to enforce copyleft on downstream users
  (GPL) or only on modifications to medre itself (LGPL).

### 8.3 Comparison matrix

| Factor                                        | GPL-3.0-or-later                  | LGPL-3.0-or-later                       |
| --------------------------------------------- | --------------------------------- | --------------------------------------- |
| Meshtastic adapter conflict                   | None (same license family)        | Unresolved (adapter still imports GPL)  |
| Toolkit usability for non-Meshtastic adapters | Copyleft encumbers all adapters   | Copyleft only on modifications to medre |
| Downstream commercial use                     | Must comply with copyleft         | Free to use as a library                |
| Communicating license to users                | Simple: everything is GPL         | Complex: LGPL + Meshtastic GPL caveat   |
| Alignment with Meshtastic ecosystem           | Strong                            | Partial                                 |
| Alignment with importable-toolkit goal        | Tension: toolkit becomes copyleft | Better: toolkit remains usable          |
| Copyleft strength                             | Full: covers derivative works     | Lesser: covers modifications only       |

### 8.4 Open questions neither license resolves

1. **The Reticulum License question.** Neither GPL nor LGPL changes the fact
   that the `[lxmf]` extra pulls in non-OSI-licensed software. The downstream
   documentation requirement exists regardless.

2. **The "combined distribution" question.** Whether shipping all adapters in
   one pip package creates a combined work under GPL is independent of what
   license medre chooses for its own code. medre choosing GPL eliminates the
   conflict but does not answer the legal question of whether the conflict
   existed in the first place.

3. **The mtjk fork's license certainty.** mtjk is a fork of GPL-3.0-or-later
   software. The fork maintainer (jeremiah-k) is also medre's primary author.
   The fork's license is GPL-3.0-or-later by inheritance, but the governance
   document should not assume that the fork operator's dual role creates any
   special legal status. The GPL applies to downstream recipients regardless
   of who maintains the fork.

### 8.5 Position: GPL-3.0-or-later (decided 2026-05-12)

The license decision has been finalized:

- **MIT is no longer defensible** as the sole license for medre. The in-tree
  Meshtastic adapter imports GPL-licensed code. Declaring MIT was misleading.
- **GPL-3.0-or-later was selected** as the simplest honest option. It eliminates
  the copyleft conflict with the Meshtastic adapter. It aligns with the
  strongest copyleft dependency. It is easy to communicate.
- **LGPL-3.0-or-later was considered** as the most flexible option for
  downstream users. It would preserve the importable-toolkit promise for
  non-Meshtastic transports. But it does not resolve the Meshtastic GPL
  question and creates a mixed-license communication burden.
- **The decision was made before beta release.** Shipping a beta with an
  inaccurate MIT declaration would have been worse than shipping with a
  copyleft license that might surprise some users. Honesty about the license
  is more important than maximizing adoption.

## 9. Copyleft Philosophy

### 9.1 Why copyleft is on the table

medre operates in a space where multiple ecosystems have different licensing
cultures:

- **Meshtastic** is GPL-3.0-or-later (firmware, protocol, client libraries).
  The community values open hardware and open software.
- **Reticulum/LXMF** use a custom license with use restrictions, not standard
  copyleft.
- **Matrix** (matrix-nio) is ISC. Permissive.
- **MeshCore** is MIT. Permissive.

medre sits at the intersection of these ecosystems. Its license choice affects
not just its own users but how it can participate in each ecosystem.

### 9.2 The communications infrastructure argument

Communications infrastructure has a strong tradition of copyleft. The argument:
if the tools that carry people's messages are proprietary, the people who
depend on those messages have no control over the infrastructure. Copyleft
ensures that improvements to communications tools remain available to the
community that depends on them.

This argument favors GPL-3.0-or-later.

### 9.3 The toolkit argument

A toolkit that wants maximum adoption by downstream developers is better
served by permissive or lesser-copyleft licensing. The argument: if the goal
is to get medre adapters into as many applications as possible, copyleft is a
barrier for commercial and proprietary users who might otherwise adopt it.

This argument favors LGPL-3.0-or-later (or MIT, which is no longer viable).

### 9.4 Governance position

The project author made a values call on 2026-05-12: GPL-3.0-or-later. The
license is not just a legal mechanism. It is a statement about what the project
wants for its downstream ecosystem. This governance document recorded the
tradeoffs and the decision.

## 10. Importable-Toolkit Implications

### 10.1 What "importable toolkit" means for licensing

medre's primary consumption model is `import medre.adapters.xxx`. Users
import specific adapters, codecs, and types into their own applications. The
runtime framework is optional.

If medre is GPL-3.0-or-later, then any application that imports medre and is
distributed must itself be GPL-3.0-or-later (or GPL-compatible). This is the
standard copyleft consequence for libraries.

If medre is LGPL-3.0-or-later, applications can import medre without
copyleft obligations. Only modifications to medre itself trigger copyleft.

### 10.2 Per-adapter implications

| Adapter    | Dependency license | If medre is GPL                                              | If medre is LGPL                                                                       |
| ---------- | ------------------ | ------------------------------------------------------------ | -------------------------------------------------------------------------------------- |
| Matrix     | ISC                | Downstream must be GPL-compatible                            | Downstream is not copyleft-encumbered                                                  |
| Meshtastic | GPL-3.0-or-later   | No additional conflict beyond medre's own GPL                | Adapter still GPL-exposed via mtjk; downstream using this adapter likely GPL-obligated |
| MeshCore   | MIT                | Downstream must be GPL-compatible                            | Downstream is not copyleft-encumbered                                                  |
| LXMF       | Reticulum License  | Downstream must be GPL-compatible + review Reticulum License | Downstream is not copyleft-encumbered (by medre) but must review Reticulum License     |

### 10.3 What changes, what does not

Changing medre's license from MIT to GPL or LGPL does not change the license
of any dependency. It only changes the license of medre's own code. Users
still receive dependencies under their original licenses.

Changing the license also does not change the import experience. The compat
guard pattern, optional dependency structure, and adapter API all remain the
same.

## 11. Runtime/Framework Implications

### 11.1 Runtime layer licensing

The runtime layer (sessions, reconnect, queue management) is part of the same
distribution. It ships under whatever license medre chooses. There is no
separate licensing for the runtime layer.

If the runtime layer were ever extracted into a separate package, it could
carry a different license. But that extraction is not planned and should not
be assumed.

### 11.2 Framework vs. library distinction under LGPL

Under LGPL-3.0, the distinction between "using a library" and "creating a
combined work" is clearer than under GPL. Applications that use the medre
runtime as-is (without modifying medre's source) would not have copyleft
obligations. Applications that modify medre's runtime code would.

Under GPL-3.0, any distributed application that incorporates medre (including
the runtime) must comply with copyleft.

### 11.3 No runtime redesign for licensing reasons

The runtime layer's architecture is not being changed to accommodate any
particular license. The current architecture (single distribution, optional
extras, compat guards) is preserved. If the license choice creates tension
with the architecture, the governance position is to choose the license that
fits the architecture, not to restructure the architecture to fit a license.

## 12. Contributor Expectations

### 12.1 What contributors should know

1. medre's license is GPL-3.0-or-later (effective 2026-05-12). The license
   was changed from MIT to reflect the dependency reality documented in this
   governance record.

2. All contributions are licensed under GPL-3.0-or-later. By submitting a
   contribution, you agree that your code will be governed by that license.

3. Contributors retain their copyright. medre does not require a CLA.

4. Existing contributions made under the prior MIT declaration are compatible
   with GPL-3.0-or-later (MIT is GPL-compatible). No relicensing negotiation
   with past contributors was needed.

### 12.2 Contributor license agreement

medre does not use a CLA. Contributors license their work under the project
license (GPL-3.0-or-later) by submitting it. This is sufficient because the
GPL itself governs downstream distribution.

If the project ever considers a license change that is not upward-compatible
(for example, from GPL-3.0-or-later to a more restrictive or different
license), past contributor consent would be needed. This governance document
does not contemplate such a change.

## 13. Downstream Expectations

### 13.1 What downstream users should expect

1. **The license is GPL-3.0-or-later** (effective 2026-05-12). The project
   was previously MIT-licensed. The change was driven by the dependency reality
   documented in this governance record.

2. **The Meshtastic adapter is GPL-exposed.** Using the Meshtastic adapter
   means interacting with GPL-3.0-only code (mtjk). medre's GPL-3.0-or-later
   is compatible with this (GPL-3.0-or-later is a superset of GPL-3.0-only).

3. **The LXMF adapter is non-standard-license-exposed.** The Reticulum
   License is not OSI-approved. It has use restrictions (AI training data,
   certain applications). Read the license text before using the LXMF adapter
   in any context where these restrictions matter.

4. **The Matrix and MeshCore adapters have no additional copyleft pressure.** Their
   dependencies (ISC and MIT respectively) are permissive and compatible with
   GPL-3.0-or-later.

5. **License metadata is updated.** The `pyproject.toml` `license` field,
   LICENSE file, and README license section all reflect GPL-3.0-or-later.

### 13.2 What downstream users should not assume

1. Do not assume the optional-extra structure insulates you from GPL
   obligations when using the Meshtastic adapter.
2. Do not assume the Reticulum License is permissive. It is not.
3. Do not assume that because core medre never imports adapter code, you can
   use GPL-exposed adapters without GPL obligations. The adapter code is still
   distributed as part of the medre package.

### 13.3 Future flexibility

medre's license choice should not require a redesign of the current
architecture. The project should:

- Remain a single distribution with optional extras.
- Continue using compat guards for optional dependencies.
- Not split into separate packages for licensing reasons.
- Not extract adapters into separate distributions for licensing reasons.
- Not create subprocess/service isolation boundaries for licensing reasons.

If a future contributor wants to address the Meshtastic GPL pressure
architecturally (for example, by making the Meshtastic adapter a separate
package), that is a future decision, not a current commitment. The current
governance position is: choose a license that fits the architecture, not an
architecture that fits a license.

## 14. Action Items

| Item                                                   | Status   | Notes                                                                         |
| ------------------------------------------------------ | -------- | ----------------------------------------------------------------------------- |
| Review this governance document                        | ✅ Done  | Reviewed 2026-05-12.                                                          |
| Select license direction                               | ✅ Done  | GPL-3.0-or-later selected (2026-05-12).                                       |
| Update `pyproject.toml` `license` field                | ✅ Done  | `license = "GPL-3.0-or-later"`.                                               |
| Create `LICENSE` file in repo root                     | ✅ Done  | Standard FSF GPLv3 text.                                                      |
| Add license section to README.md                       | ✅ Done  | GPL-3.0-or-later with links.                                                  |
| Document Meshtastic GPL implications for downstream    | ✅ Done  | This document §5, contract 41 §7.                                             |
| Document Reticulum License implications for downstream | ✅ Done  | This document §6, contract 44.                                                |
| Update SPDX classifiers in pyproject.toml              | ✅ Done  | `License :: OSI Approved :: GNU General Public License v3 or later (GPLv3+)`. |
| Add SPDX headers to .py source files                   | Deferred | Post-beta hardening. See contract 45 §4.                                      |

All governance actions completed 2026-05-12.

## 15. Constraints

This document operates under the following constraints:

1. **No package splitting.** No adapter is extracted into a separate package.
2. **No runtime redesign.** No architectural changes for licensing reasons.
3. **No adapter redesign.** Adapter interfaces remain as-is.
4. **No transport extraction.** Transports stay in-tree.
5. **No subprocess/service isolation.** No process boundary changes.
6. **No new transports or features.** Licensing governance does not drive
   feature work.
7. **No legal advice.** This document records analysis and tradeoffs. It does
   not provide legal advice or guarantee legal compliance. Consult qualified
   legal counsel for definitive answers.
8. **No contributor communication via this document.** This document is an
   internal governance record. Contributor-facing communication happens via
   contract 42 and README.md.

## 16. References

| Document                                        | Relevance                                                          |
| ----------------------------------------------- | ------------------------------------------------------------------ |
| Contract 34 (Dependency Reality Audit)          | Install behavior, optional import mechanics, dependency versioning |
| Contract 09 (Meshtastic Tranche 1)              | Meshtastic adapter architecture, mtjk usage                        |
| Contract 10 (Meshtastic Source Audit)           | mtjk fork details, API surface                                     |
| Contract 11 (Meshtastic Connection Boundary)    | Adapter ownership boundaries                                       |
| Contract 14 (LXMF Tranche 1)                    | LXMF adapter architecture, Reticulum dependency                    |
| Contract 37 (Transport Maturity Classification) | Per-transport maturity ratings                                     |
| Contract 38 (Release Candidate Criteria)        | RC checklist, pyproject.toml metadata requirements                 |
| Contract 39 (Operational Risk Register)         | Operational risk context                                           |
| `pyproject.toml`                                | Current license declaration, dependency declarations               |
| `docs/runbooks/lxmf-alpha-operation.md` §13.3   | Reticulum License documentation                                    |
| `docs/runbooks/lxmf-live-smoke.md`              | Reticulum License authorship note                                  |
