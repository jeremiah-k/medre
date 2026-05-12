# Contributor Governance

> Contract version: 2
> Last updated: 2026-05-12
> Track: 3 (Project Governance)
> Supersedes: Version 1 (2026-05-10). License updated to GPL-3.0-or-later.
> Status: Governance contract. Records contributor expectations, licensing posture, and relicensing constraints while the project is single-author pre-beta.

This document records the contributor governance posture for medre. It
covers what happens when someone submits a pull request, how licensing works
today, who owns what copyright, and what constraints appear if the project
license ever changes. It is written for the current moment: a single-author
project at version 0.1.0, not yet at beta, with no external contributions
received.

This is a governance document. It does not add CLA tooling, DCO enforcement,
CI checks, or legal automation. It records decisions and expectations so they
exist before they are needed.


## 1. Current State

medre is a single-author project. All code to date was written by the project
maintainer. No external contributions have been merged. The project has no CLA
(C Contributor License Agreement), no DCO (Developer Certificate of Origin)
policy, and no contributor onboarding process, because none of these things
have been necessary yet.

The project is licensed GPL-3.0-or-later, as declared in `pyproject.toml`. A
top-level `LICENSE` file with the standard FSF GPLv3 text is present (added
2026-05-12). The license was changed from MIT to GPL-3.0-or-later to align with
the dependency reality (see contracts 40, 41).


## 2. Inbound Contribution Expectations

When the project starts accepting external contributions, the following
expectations apply.

### 2.1 License grant

By submitting a contribution (pull request, patch, issue with code, or any
other form), the contributor confirms that:

1. The contribution is their original work, or they have the right to submit
   it under the project's license.
2. The contribution is offered under the same GPL-3.0-or-later license that
   governs the project. If the project license changes in the future, the
   contribution is subject to the terms described in section 5 of this document.
3. The contributor retains their copyright. The project does not require
   copyright assignment.

This is not a CLA. It is a statement of expectation. There is no signing
process, no form to fill out, and no automation to enforce it. The
expectation is that contributors understand and accept these terms by the act
of submitting.

### 2.2 What counts as a contribution

A contribution is any intentional submission of code, documentation, tests,
configuration, or design input that the project maintainer merges or applies.
Casual suggestions in issue threads ("you could try X") are not contributions
unless the submitter explicitly offers them as such and the maintainer
integrates them.

### 2.3 Contribution quality

medre uses contracts (this directory) and runbooks (`docs/runbooks/`) as
operational specifications. Contributors should read relevant contracts before
submitting changes to adapter code, diagnostics, or delivery semantics. Pull
requests that contradict documented contracts will be asked to align with the
contract or propose a contract change alongside the code change.

The test suite is the baseline. Contributions that break the test suite will
not be merged. New functionality should include tests. Bug fixes should include
regression tests.


## 3. No CLA, No DCO

The project does not use a CLA or DCO at this time. This is a deliberate
choice, not an oversight.

A CLA gives the project explicit legal permission to use and relicense
contributions. A DCO provides a lighter-weight sign-off that the contributor
has the right to submit under the project's license. Both solve real problems.
Neither is worth the overhead for a pre-beta project with zero external
contributors.

If the project grows a contributor base, a DCO or CLA may become necessary.
Section 5 describes the relicensing constraints that exist without one.


## 4. Copyright Ownership

### 4.1 Current code

All existing code is copyright the project maintainer. No copyright
assignment has been made to any organization, foundation, or corporate entity.

### 4.2 Contributions

Contributors retain copyright on their own work. The project does not
require copyright assignment. The GPL-3.0-or-later license applied to the
project grants permissions to all recipients under copyleft terms, including the
maintainer, without requiring assignment.

### 4.3 No entity ownership

No corporation, foundation, or legal entity owns medre. It is a personal
project. If this changes, this section will be updated to reflect the new
ownership structure, and a CLA or DCO will almost certainly be needed at that
point.


## 5. Relicensing

This section exists because relicensing is the area where governance decisions
made now have consequences later. The goal is to avoid confusion.

### 5.1 Current license: GPL-3.0-or-later

The project is GPL-3.0-or-later licensed (changed from MIT on 2026-05-12). All
code in the repository is under GPL-3.0-or-later. The change was made by the
sole copyright holder (project maintainer) before any external contributions
were received.

### 5.2 What relicensing means

If the project maintainer decides to change the license (for example, to
Apache 2.0, GPLv3, or a dual-license arrangement), they can relicense their
own code unilaterally. They own the copyright and can offer it under different
terms.

### 5.3 The contributor constraint

If external contributions exist in the repository at the time of relicensing,
the situation changes. Each contributor owns copyright on their contributions.
The project can only relicense those contributions if:

- The contributor agrees to the new license.
- A CLA was in place at the time of contribution that grants relicensing
  rights.
- The contribution is removed or rewritten.

Without a CLA or DCO, relicensing requires individual contributor agreement
for every contribution ever received. This is manageable with three
contributors. It is painful with thirty. It is a serious problem with three
hundred.

### 5.4 Practical implication

This is the key point: **absent a CLA or DCO policy, accepting external
contributions constrains future relicensing.** Every merged PR from an
external contributor adds one more person who must agree to any future
license change.

Contributors should expect the project license terms to govern their
contributions unless a future policy (CLA, DCO, or written agreement) says
otherwise. If the project later adopts a CLA, it will apply prospectively to
new contributions, not retroactively to contributions already merged.

### 5.5 When to act

The right time to add a CLA or DCO is before accepting the first external
contribution, or shortly after the first few. Waiting until the project has
dozens of contributors makes relicensing harder. Acting before any
contributions exist is premature for a pre-beta project.

The trigger for adopting a CLA or DCO is: the project receives a pull request
from someone who is not the maintainer, and the maintainer intends to merge
it. At that point, this document should be updated and a formal policy
adopted before the merge.


## 6. Governance While Single-Author

### 6.1 Decision making

All technical and governance decisions are made by the project maintainer.
There is no steering committee, voting process, or governance board.

### 6.2 This is fine for now

Single-author governance is not a deficiency. It is the natural state of a
project that has not yet attracted contributors. Pretending to have governance
structures (maintainer teams, voting procedures, roadmaps by committee)
before they are needed creates bureaucracy without benefit.

### 6.3 When to change

Governance should expand when it needs to, not before. Triggers for expanding
governance include:

- Three or more active external contributors.
- A contribution that the maintainer wants to merge but is unsure about
  licensing implications.
- A fork or downstream use that raises questions about contribution
  provenance.
- Institutional interest (a company or foundation wanting to use or sponsor
  medre).

None of these have happened yet. When they do, this document gets updated.


## 7. Summary

| Topic | Current posture |
|-------|----------------|
| CLA | None. Not needed yet. |
| DCO | None. Not needed yet. |
| License | GPL-3.0-or-later (declared in pyproject.toml, LICENSE file present). |
| Copyright | Retained by each author. No assignment. |
| Relicensing | Maintainer can relicense own code. External contributions add constraints. |
| Contributor process | None formal. Expectation recorded in section 2. |
| Decision authority | Maintainer. Single-author project. |
| When to revisit | When the first external PR arrives. |


## 8. Actions Before First External Merge

This section records what should happen before the first external
contribution is merged. It is a checklist for the maintainer, not a policy
for contributors.

- [x] Add a top-level `LICENSE` file containing the GPLv3 license text.
- [ ] Decide whether to adopt a DCO (lighter weight, recommended first step)
      or a CLA (heavier, more protective).
- [ ] If adopting DCO: document the sign-off requirement in a `CONTRIBUTING.md`
      file.
- [ ] If adopting CLA: select or create a CLA, set up a signing process, and
      document it.
- [ ] Update this contract to reflect the adopted policy.
- [ ] Ensure the contribution expectation in section 2 is surfaced somewhere
      contributors will see it (README, CONTRIBUTING.md, or PR template).
