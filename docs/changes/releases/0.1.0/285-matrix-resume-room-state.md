# Matrix room state after cursor resume

Fresh Matrix sessions request full room state while resuming their saved event
cursor. This restores membership display names and encryption state after a
process restart without replaying admitted messages. Subsequent syncs on the
initialized client remain incremental.

The Matrix adapter compatibility pin includes the fix and focused checkpoint,
recovery, and restart-state regression tests.
