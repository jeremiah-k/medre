# Local adapter compatibility pin commands

- Add offline `--check-local` and `--sync-local` compatibility manifest commands
  that validate clean sibling adapter checkouts, distribution identities, and
  entry-point providers before checking or updating all commit pins.
- Record the expected distribution name per manifest entry and add
  pinned-repository regression tests.
