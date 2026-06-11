# Adapter Startup Lifecycle Cleanup

Harden start-failure cleanup across MeshCore, LXMF, and Meshtastic adapters
to match the Matrix pattern.

## Changed

- `src/medre/adapters/meshcore/adapter.py`: defer `_mark_started(ctx)` past
  `session.start()` success; clear `ctx`, `_session`, and `_started` on
  failure; best-effort `session.stop()` on failed start.
- `src/medre/adapters/lxmf/adapter.py`: defer `_mark_started(ctx)` past
  `session.start()` and dependency checks; clear `ctx` and `_started` on
  `HAS_LXMF` and session-start failures; best-effort `session.stop()` on
  failed start.
- `src/medre/adapters/meshtastic/adapter.py`: defer `_mark_started(ctx)` past
  `session.start()`, event loop, and drain-task creation; clear `ctx` on
  session-start failure.
- `docs/spec/diagnostics-evidence.md`: correct MeshCore diagnostic key counts
  (adapter-level 3→17, session sub-dict 13→15); remove false statement that
  health key is unimplemented; document three previously-omitted session
  sub-dict keys.

## Added

- `tests/test_meshcore_adapter_startup.py`: start-failure cleanup, post-stop
  ingress, and diagnostics fallback coverage.
- `tests/test_lxmf_adapter_startup.py`: start-failure cleanup, post-stop
  ingress, and lifecycle field coverage.
- `tests/test_meshtastic_adapter_startup.py`: start-failure cleanup, post-stop
  ingress, and infrastructure-creation coverage.
