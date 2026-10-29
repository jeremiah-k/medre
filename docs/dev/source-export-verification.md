# Reproducible source export verification

An adapter checkout on `main` is not proof that the exact core compatibility
pin is present in an exported Git bundle. Include every exact pinned Git
object in the bundle so qualified installations can be reproduced offline.

After restoring `manifest.tsv` and the sibling checkouts, run:

```console
python scripts/verify_stack_sources.py /path/to/restored-stack
```

Every bundle named by `manifest.tsv` is verified and unpacked into an empty
isolated object store. Both the exported commit and compatibility pin, including
their reachable trees and blobs, must be complete there. Missing, corrupt or
incremental bundles fail even when a checkout or ambient object database contains
the missing objects. The verifier performs no network repair. Checkout cleanliness
follows Git's configured text normalization, including Windows line endings;
artifact proof continues to exclude ambient configuration and object databases.

The report distinguishes bundle verification, the pinned reference, recorded
export commit and checked-out `HEAD`. A checkout newer than the pin may be expected; it does not
change the pin. The command returns an error when a pinned Git object is
absent, a checkout is dirty, or any recorded checkout (including core) disagrees
with the manifest. Empty pin sets, malformed headers, duplicate rows and nested
directories masquerading as independent repositories are rejected. Optionally use
`--require-pinned-heads` when validating a collection of intentionally pinned
worktrees rather than the development checkouts.

This check does not qualify SDK installations, hardware, recipient receipts,
or the behavior of the checked-out head. It only confirms reproducible source
objects. CI must still install and test exactly the revisions advertised in
the compatibility manifest.

To create a new exact-pin artifact, use the
[qualified stack exporter](qualified-stack-export.md). Its `manifest.json`
and independently checked bundles are a separate format from the historical
`manifest.tsv` inputs accepted by this verifier; the exporter verifies its own
output before publication.
