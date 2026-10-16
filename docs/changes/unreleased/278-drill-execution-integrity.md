# Failure drill execution and evidence integrity

Configured failure drills reject enabled real adapters before runtime assembly,
matching ordinary fake smoke and run sessions. A drill fails when any evidence
check fails, including missing suppression receipts, native-reference lookup
errors, unsuccessful recovery, or incomplete adapter cleanup.

Drill reports retain shutdown errors and expose `shutdown_status` for runtimes
they start. Cancellation closes the runtime before propagating. Synthetic
startup/configuration drills use temporary state roots that are removed when
the drill exits and preserve the caller's `MEDRE_HOME` and XDG environment.
Programmatic path resolution accepts an explicit environment mapping for this
isolation.
