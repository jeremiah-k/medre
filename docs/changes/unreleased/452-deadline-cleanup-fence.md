# Deadline cleanup and native submission fence

Storage can retire expired, provably unstarted and unclaimed outbox generations
and refuse native submission past the effective deadline. Retirement preserves
existing delivery evidence and releases only the retired target's content
dependency. Production sweep wiring and bounded candidate examination remain
outstanding.
