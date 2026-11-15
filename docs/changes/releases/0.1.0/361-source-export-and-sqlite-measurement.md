# Verify exact exported sources and measure SQLite reopening

Verify exported bundles in empty object stores, including pinned and exported
commits and their complete object graphs, without ambient objects or network
fallback. Check checkout identity independently of artifact completeness.
Reject empty or ambiguous manifests, dirty source and mismatched core exports.
Measure bounded synthetic SQLite append and reopen behavior without native I/O
or environment-specific performance thresholds.
