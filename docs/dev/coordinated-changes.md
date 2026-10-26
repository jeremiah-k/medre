# Coordinated core and adapter changes

Keep the committed compatibility manifest on full commits that have landed in
each adapter repository's default branch. Candidate-pair testing is useful
evidence, but it does not replace qualification against the defaults that will
exist after a merge.

## Remove unnecessary dependencies first

Put a contract test with the code that owns the behavior. Core tests own sample
instance naming, route expansion, and generic CLI behavior. Adapter tests own
their sample fields, protocol behavior, and whether their emitted configuration
can deliver between distinct instances. An adapter smoke test should discover
instance IDs from that configuration instead of asserting core's spelling.

This allows the same adapter test to run against both core `main` and a proposed
core revision. Do not make new adapter tests require an unrelated core UI change.
Keep core-only tests SDK-free; declare provider-dependent integration tests as
`medre-test-tier: compatibility` and exercise both layers when a contract changes.

For generated commands, verify shell argument round trips rather than imposing
one platform's quoting style on every platform. Keep behavior tests runnable
without providers, then use the paired compatibility suite for native contracts.
Change tests with their owning behavior in the same commit; do not move an
unrelated assertion into a dependent repository just to make a candidate pass.

## Qualify a candidate pair without persistent overrides

Commit and push each candidate so remote CI can fetch its exact revision. Keep
`MEDRE_CORE_REF` unset: a repository-wide variable silently changes the baseline
of unrelated adapter runs. Use an explicit, full core SHA for a single dispatch:

```bash
gh workflow run test.yml --repo OWNER/ADAPTER --ref ADAPTER_BRANCH \
  -f core_ref=FULL_CORE_COMMIT_SHA
```

Record both SHAs with the result. Adapter push and PR workflows still need to
pass against core `main`. A candidate dispatch qualifies only its recorded pair;
it is not evidence that an adapter can merge before its dependency.

For local experiments, copy `.github/adapter-compatibility.json` to a file under
`/tmp`, put candidate full SHAs there, and pass `--manifest /tmp/pair.json` to
`scripts/ci/adapter_matrix.py`. Keep candidate manifests and their generated
requirements outside Git. Use a separate environment for each pair, install the
snapshot after syncing core, and do not sync an environment while tests run in it.
`--requirements` produces normal pip-compatible requirements from resolved SHAs.

## Merge dependencies before consumers

1. Make the adapter change compatible with current core `main`. For a genuinely
   breaking interface, split the work into an additive core change, the adapter
   migration, then removal of the old interface. This breaks the dependency
   cycle without a temporary branch becoming a deployment dependency.
2. Test each adapter PR against core `main` and the explicit candidate core SHA.
   Inspect the latest exact-head checks and reviews, then merge the adapter PRs.
3. Update core's compatibility manifest to the adapters' landed commits. A squash
   merge creates a new SHA; a green candidate SHA is not a substitute. Review
   the resulting diff. If using `--sync-local`, first update clean sibling
   checkouts to those default-branch commits.
4. Run the ancestry gate and core's compatibility tests against that snapshot:

   ```bash
   python scripts/ci/adapter_matrix.py --check-merged
   ```

   This uses authenticated `gh api` access and fails closed on lookup errors.
   Both branch/tag refs and SHAs belonging only to unmerged branches are rejected.
   The **Merged adapter compatibility pins** CI job runs this on the committed
   manifest, alongside the existing core, provider, SDK, and integration jobs.

5. Merge core only after its final revision passes every gate. Re-run adapter
   default-branch workflows against the new core default to verify the resulting
   consumer state. Keep hardware/live/soak qualification explicit.

When a change cannot pass against an already-merged dependency, split it rather
than approving red baseline checks. Do not solve the cycle with a moving branch
pin, a persistent CI variable, skipped assertions, or a promise to fix main later.

`Merge readiness` aggregates every core, adapter, and local-integration result,
including the ancestry gate. It fails when a required tier fails, is cancelled,
or is unexpectedly skipped. Require `Merge readiness` and the separate
`docker-integration` check in branch protection. GitHub merge protection is
configured in repository settings, not by this file.
