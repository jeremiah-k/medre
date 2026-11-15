# Reject conflicting smoke execution modes and options

`medre smoke` now rejects `--drill` combined with `--run-session`, rather than
silently choosing one. Mode-specific arguments fail before runtime startup:
`--message`, `--source-adapter`, and `--source-context` require normal smoke;
`--scenario` and `--snapshot-dir` require `--run-session`. Shared configuration,
storage, and JSON options remain valid in all modes. This prevents a successful
report for a different diagnostic than the one the operator requested.
