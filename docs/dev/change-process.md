# Change Process

This document describes how changes to MEDRE documentation are managed.

## Spec Changes

Changes that affect runtime semantics (data models, adapter contracts, routing
rules, storage guarantees) require all of the following:

1. Update the relevant `docs/spec/` page.
2. Update the corresponding JSON Schema files in `docs/schemas/`.
3. Add or update tests that validate the change.
4. Append an entry to `docs/changes/unreleased.md`.

All three artifacts (spec page, schema, test) must land in the same commit.

## Ops-Only Changes

Changes to operator documentation that do not alter runtime semantics (typos,
clarified instructions, new troubleshooting steps) update only the relevant
`docs/ops/` file. No schema or spec changes are required.

## Dev-Only Changes

Changes to developer documentation (testing patterns, adapter authoring guides)
update only the relevant `docs/dev/` file.

## Source-Audit Notes

Source audit notes document the results of code review. They are evidence of
review, not normative authority. If an audit reveals a spec inconsistency, the
spec page must be updated separately — the audit note alone does not change
semantics.

## Pre-Release Breaking Changes

MEDRE is pre-first-release. Breaking changes to the specification are
permitted when they simplify the model. When making a breaking change:

1. Update all affected spec pages.
2. Update all affected schemas.
3. Update all affected tests.
4. Append an entry to `docs/changes/unreleased.md` noting the break.
5. Run the full test suite to confirm nothing is missed.

## Changelog

Unreleased changes are tracked in a single file:
[`docs/changes/unreleased.md`](../changes/unreleased.md).

Append new entries to the bottom of that file. Do not create per-commit
fragment files.

Keep each entry to a single concise bullet or short paragraph focused on
operator or developer impact. Omit file-by-file change lists, test names,
audit-process narrative, and validation-command output — those belong in
the linked spec, audit, or PR description, not in release notes.
