# Matrix Adapter start() Lifecycle Cleanup

Roll back Matrix adapter lifecycle fields on failed start; move started log
after completion.

## Changed

- `src/medre/adapters/matrix/adapter.py` (`start()`): Removed early
  `_mark_started(ctx)` call so `_start_time` is only set when start completes
  fully. Added `self.ctx = None` cleanup on all three failure paths (no-nio,
  session start failure, auto-join failure) to prevent stale context from
  surviving a failed startup. Moved "MatrixAdapter started" log to after
  `_started = True` and `_mark_started(ctx)`, ensuring it only fires on full
  success. Added optional debug log "Matrix session connected; joining
  configured rooms" before auto-join when `auto_join_rooms` is configured.

## Added

- `tests/test_matrix_adapter_startup.py` (`TestStartLifecycleCleanup`): seven
  test cases covering HAS_NIO=False state cleanup, session start failure
  cleanup, auto-join failure cleanup, started-log suppression on failure,
  successful start log ordering, post-failed-start event guard, and
  `_start_time` set only on full success.
