# Change Fragments

This directory tracks incremental changes to MEDRE documentation and
specification.

## Structure

```text
changes/
  README.md          ← This file
  unreleased/        ← Active change fragments
```

## Adding a Fragment

1. Create a file in `unreleased/` named `NNN-brief-description.md`.
2. Use the template:

```markdown
## Brief Description

One-line summary.

### Changed

- Item changed.
```

3. Commit the fragment alongside the documentation change it describes.

## During Release

1. Collect all fragments from `unreleased/`.
2. Consolidate into release notes.
3. Remove the processed fragments.
