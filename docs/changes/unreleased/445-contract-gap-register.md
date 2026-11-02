# Track contract gap status in a living register

`docs/dev/contract-gap-status.md` records each open contract gap with a
closed status vocabulary, current evidence, and next work. Rows move in the
same change as the work that moves them, and a gap closes only when its
behavior is implemented, specified under `docs/spec/`, and qualified on
`main`; historical evidence never closes a row.

Developers can find the authoritative status of every known contract gap in
one place, including what is deliberately deferred and what a gap needs
before it closes.
