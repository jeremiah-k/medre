# Track contract gap status in a living register

`docs/dev/contract-gap-status.md` records each open contract gap with a
closed status vocabulary, current evidence, and next work. Rows move in the
same change as the work that moves them, and a gap closes only when its
behavior is implemented, specified under `docs/spec/`, and qualified on
`main`; historical evidence never closes a row.

The register is seeded from the 2026-11-02 whole-stack source
reconciliation (core plus the seven independently distributed adapters) and
links in-flight change requests inline. The exact-destination recovery work
stream is broken out per stage beneath the main register. The developer
documentation index and the development priorities page link to the
register.
