# Exact-pinned offline source exports

The compatibility manifest stored in the selected core commit is the single
selection authority. Hidden checkout edits cannot replace its provider pins.
Ordinary
stack exports can contain development checkout heads and omit selected commits;
that is useful for work-in-progress but is **not** an exact release build.

```bash
python scripts/export_qualified_stack.py /path/to/sibling-checkouts /private/new-export
```

The command requires a clean core checkout plus a clean checkout and exact pin
commit object for every provider in core's compatibility manifest.
Provider selections must be full immutable commit SHAs; branch and tag refs are
refused even when they currently resolve locally.
It creates independent bundles from the selected pinned objects even if a
checkout is on a newer branch, then verifies commit and tree identity using
fresh isolated Git stores. If any pin is absent, export fails without
publishing a partial destination. An existing output directory is never
overwritten. `manifest.json` records pinned and checkout SHAs separately and
SHA-256 digests of the bundles.

**Limits:** This is a source-code artifact, not an installed or qualified
runtime. It contains full reachable Git history (which may itself contain
secrets); keep the artifact private. It does not capture SDK wheels, private
profiles, Matrix E2EE identity, radio firmware, backups or native receipt
evidence. A separate native manifest must establish the installation and
peer-observation gates. Missing pinned objects cannot be filled from a
convenient branch head.

For large histories, set `--git-timeout SECONDS` to change the finite per-operation
limit for fetching, creating and verifying bundles (default: 600 seconds).
Metadata commands retain a 90-second bound. Bundle hashing streams the file
with bounded memory. Failed Git operations include their stderr in the refusal
so missing objects and local repository errors can be distinguished.
