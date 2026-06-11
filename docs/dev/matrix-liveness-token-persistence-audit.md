# Matrix Liveness & Token Persistence Audit

Date: 2026-06-10
Branch: `adapter-diagnostics-sdk-parity`

## Scope

Focused design pass for Matrix adapter liveness detection, sync-token
persistence, stale-sync watchdog, and shutdown behaviour. This audit
does **not** cover E2EE key management (covered separately in
`test_matrix_session_e2ee.py`) or Meshtastic/LXMF adapters.

## Current State

### Sync loop

`MatrixSession._sync_with_reconnect()` runs a manual sync loop (not
`sync_forever`) with bounded exponential-backoff reconnect (1 s – 60 s,
max 10 attempts). On each successful sync, `_last_successful_sync` is
set to `time.monotonic()`. The session tracks `_live_sync_started`
(after first successful sync) for history suppression.

### Sync token

nio's `AsyncClient` holds the `next_batch` token internally for the
duration of a session. When the session stops (client closed), the
token is **lost**. On restart, nio performs an initial sync without a
token, receiving full room state. This is correct for the current
pre-release but means:

- Restart causes a brief "catch-up" window where the adapter reprocesses
  all visible room events.
- History suppression (`is_live`) prevents these from being published
  inbound.
- No `next_batch` token is persisted to storage.

### Health lifecycle

`_last_health` is `None` before the first `health_check()` call and is
cleared to `None` on every `start()` and `stop()`. This means
`diagnostics()["health"]` correctly shows `None` before any health check
in each lifecycle, matching the protocol lifecycle rule.

### Stale-sync watchdog (implemented)

`health_check()` downgrades `"healthy"` to `"degraded"` when
`last_successful_sync` is older than `_SYNC_STALE_THRESHOLD_SECONDS`
(300 s by default). A `None` timestamp (no sync completed yet) is
intentionally **not** treated as stale — the adapter just started and
has not had a chance to complete its first sync loop iteration. This
uses a fakeable clock (`adapter._clock`) for deterministic testing.

## Design Decisions

### 1. Token persistence — NOT implemented now

**Decision**: Do not persist `next_batch` tokens to storage in this pass.

**Rationale**:

- Token persistence requires storage-layer integration (SQLite column or
  dedicated state table), schema migration considerations, and careful
  handling of multi-adapter state directories.
- The current history suppression mechanism (`is_live` boundary) already
  prevents duplicate inbound processing after restart.
- Token persistence is a performance optimisation (avoids re-downloading
  room state on restart) rather than a correctness issue.
- Recommended for a future PR after the schema stabilises post-prerelease.

**Future implementation sketch**:

```python
# In MatrixSession.stop():
if self._client and hasattr(self._client, "next_batch"):
    await self._config.save_sync_token(self._client.next_batch)

# In MatrixSession._start_plaintext():
saved_token = await self._config.load_sync_token()
if saved_token:
    self._client.next_batch = saved_token
```

### 2. Stale-sync watchdog — implemented

**Mechanism**: `health_check()` compares `time.monotonic() - last_successful_sync`
against `_SYNC_STALE_THRESHOLD_SECONDS` (300 s). Only downgrades
`"healthy"` → `"degraded"`; does not override `"failed"` or `"unknown"`.
A `None` timestamp (no sync yet) is intentionally not treated as stale.

**Why 300 seconds**: Matrix long-polling uses 30 s timeouts. Five
minutes represents 10 missed sync cycles, which is a clear signal that
something is wrong without being overly aggressive.

**Clock**: `adapter._clock` defaults to `time.monotonic` but can be
overridden for deterministic tests. This avoids `time.monotonic()` in
test assertions.

### 3. Shutdown behaviour

Current shutdown sequence is correct:

1. `adapter.stop()` calls `session.stop(timeout=5.0)`
2. `session.stop()` sets `_stop_requested=True`, cancels sync task,
   cancels outstanding join tasks, closes the nio client, yields to
   event loop for aiohttp cleanup, sets `_client=None`, `_closed=True`.
3. `adapter.stop()` captures `_sync_failure_stored` before stopping,
   clears `_last_health=None`.

No changes needed.

### 4. Remote delivery / read receipts — NOT claimed

The Matrix adapter does **not** claim remote delivery confirmation or
read receipts. `delivery_receipts` in capabilities is `True` only in
the sense that the adapter receives a synchronous `event_id` from the
homeserver confirming the server accepted the event. This is server
ACK, not remote delivery to other clients. The adapter does not track
read receipts (MSC2285 / `.m.read` private receipts).

## Stale-Sync Watchdog Options (Evaluated)

| Option                       | Pros                                                          | Cons                                        | Chosen      |
| ---------------------------- | ------------------------------------------------------------- | ------------------------------------------- | ----------- |
| **A. health_check degraded** | Zero new tasks; piggybacks on existing health_check; testable | Only observed when health_check is called   | **Yes**     |
| B. Background watchdog task  | Continuous monitoring; can log proactively                    | New task lifecycle to manage; complexity    | No (future) |
| C. Sync-loop self-report     | Sync loop itself reports stale                                | Couples sync loop to health; harder to test | No          |
| D. External operator tooling | Operator-driven; no code impact                               | Not automated                               | No          |

## What Is NOT Overclaimed

1. **Remote delivery**: The adapter confirms the homeserver accepted the
   event (`event_id` in response). It does not know if other clients
   received or displayed the message.
2. **Read receipts**: Not tracked, not claimed.
3. **E2EE reliability**: `undecryptable_event_count` is tracked for
   diagnostics but does not affect health status. E2EE failures are
   informational.
4. **Sync token persistence**: Not implemented. Tokens are lost on
   restart; history suppression handles the catch-up window.

## Test Coverage

| Test                                              | File                              | What it proves                    |
| ------------------------------------------------- | --------------------------------- | --------------------------------- |
| `test_diagnostics_health_none_before_first_check` | `test_matrix_health_lifecycle.py` | Health is None before first check |
| `test_diagnostics_health_set_after_check`         | `test_matrix_health_lifecycle.py` | Health is set after check         |
| `test_diagnostics_health_none_after_stop`         | `test_matrix_health_lifecycle.py` | Stop clears health                |
| `test_diagnostics_health_none_after_start`        | `test_matrix_health_lifecycle.py` | Start clears health               |
| `test_restart_clears_health`                      | `test_matrix_health_lifecycle.py` | Restart cycle clears health       |
| `test_stale_sync_reports_degraded`                | `test_matrix_health_lifecycle.py` | Stale sync → degraded             |
| `test_fresh_sync_reports_healthy`                 | `test_matrix_health_lifecycle.py` | Fresh sync → healthy              |
| `test_no_sync_yet_preserves_healthy`              | `test_matrix_health_lifecycle.py` | None sync preserves healthy       |
| `test_stale_does_not_override_failed`             | `test_matrix_health_lifecycle.py` | Failed takes priority             |
| `test_stale_does_not_override_unknown`            | `test_matrix_health_lifecycle.py` | Unknown takes priority            |
| `test_fakeable_clock_controls_degradation`        | `test_matrix_health_lifecycle.py` | Clock faking works                |
| `test_diagnostics_json_safe`                      | `test_matrix_health_lifecycle.py` | Diagnostics serialise cleanly     |

## Changed Files

- `src/medre/adapters/matrix/adapter.py` — Added `_clock` slot,
  `_SYNC_STALE_THRESHOLD_SECONDS` constant, stale-sync downgrade in
  `health_check()`.
- `tests/test_matrix_health_lifecycle.py` — New test file.
- `docs/dev/matrix-liveness-token-persistence-audit.md` — This document.

## SDK Parity Backlog Note

The following wording should be added to `docs/dev/sdk-parity-backlog.md`
under the Matrix section by a separate PR (per task constraint):

> **Sync token persistence**: The Matrix adapter does not persist
> `next_batch` tokens to storage. On restart, nio performs a full
> initial sync. History suppression (`is_live` boundary) prevents
> duplicate inbound processing. Token persistence is recommended as a
> future performance optimisation once the storage schema stabilises.
