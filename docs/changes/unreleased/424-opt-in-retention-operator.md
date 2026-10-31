# Add bounded, opt-in content collection command

`medre storage collect` retains content by default. `--mode relay-only` previews
up to 100 events in a read-only connection; `--apply --yes` performs separately
rechecked, exact-event logical collection. Reports distinguish removed logical
bytes from unmeasured provider, backup and WAL copies. No periodic scheduler.
