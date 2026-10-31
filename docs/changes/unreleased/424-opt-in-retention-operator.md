# Add bounded, opt-in content collection command

`medre storage collect` retains content by default. `--mode relay-only` previews
up to 100 events in a read-only connection; `--apply --yes` performs separately
rechecked, exact-event logical collection. Reports distinguish removed logical
bytes from unmeasured provider, backup and WAL copies. No periodic scheduler.

Batch results are schema-validated, keep committed progress across per-event
refusals and show external-copy caveats even for retain/empty pages. Existing
empty files are refused without schema initialization; valid long event IDs
remain usable for pagination.
