# Change process

Merged PR descriptions and generated GitHub release notes are the primary
record. Routine changes do not require numbered fragments.

## Runtime changes

Update the relevant specification and regression tests in the same PR. Update
schemas only when the contract changes, respecting the published 0.1.0 freeze
and compatibility policy. A formatter upgrade is not authority to rewrite a
frozen artifact. State behavior, compatibility, operator impact and validation
in the PR description, with links to the relevant issues and documentation.

Core and adapters have independent releases. Identify affected projects and
coordinate their qualification and merge order. Record exact merged revisions
or published artifacts rather than treating a branch or channel name as proof.

## Documentation and tooling

Update the affected ops/dev page directly. Describe tooling behavior and checks
in the PR. Temporary audits, review ledgers and validation logs belong outside
tracked documentation, for example in ignored `.zcode/`.

## Optional release notes

Use [unreleased operator notes](../changes/unreleased.md) when operator actions
or several PRs need one coherent explanation. A separate note under
`docs/changes/unreleased/` is optional and uses a descriptive filename. Do not
allocate the next sequence number. Keep lasting procedures in the relevant
specification or operator guide rather than only in release notes.

## Release preparation

Generate release notes from merged PRs and review them for missing migration
instructions, compatibility changes and qualification limits. Publish the exact
tag and artifact identities. Fold useful unreleased notes into a versioned
summary, then clear or archive those individual notes.

The first published release's summary and 293 individual notes are retained in
[the 0.1.0 archive](../changes/0.1.0.md). They are historical records, not active
work. Schema compatibility decisions after publication require the documented
versioning review; the project is no longer pre-first-release.
