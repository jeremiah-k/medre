# Troubleshooting

Failure categories, drill procedures, routing diagnostics, and common fixes.

MEDRE does not provide automated remediation, per-adapter restart, or self-healing. All diagnosis and action is operator-initiated.

## Failure Category Quick Reference

| Category              | Exit code | Receipt/Outcome status                                                                                                                    | Retry?                     | Where to inspect                                 |
| --------------------- | --------- | ----------------------------------------------------------------------------------------------------------------------------------------- | -------------------------- | ------------------------------------------------ |
| Config error          | 2         | None (no runtime)                                                                                                                         | No                         | stderr, `medre config check`                     |
| Build failure         | 3         | None (no delivery)                                                                                                                        | No                         | `startup.build_failures`, logs                   |
| Total startup failure | 4         | None (no delivery)                                                                                                                        | No                         | `startup.boot_summary`, logs                     |
| Degraded startup      | 0         | Partial                                                                                                                                   | Yes (for started adapters) | `failed_adapter_ids`, `routes.startup_readiness` |
| Renderer failure      | 0         | `failed` (RENDERER_FAILURE)                                                                                                               | No                         | `medre inspect receipts`, RouteStats             |
| Adapter permanent     | 0         | `failed` (ADAPTER_PERMANENT)                                                                                                              | No                         | receipt lineage, adapter `diagnostics()`         |
| Adapter transient     | 0         | `sent` (after retry) or `failed`                                                                                                          | Yes (up to max_attempts)   | receipt `attempt_number`, `parent_receipt_id`    |
| Capacity exceeded     | 0         | `failed` (delivery_capacity_exceeded)                                                                                                     | No                         | `capacity_rejections` counter, logs              |
| Deadline exceeded     | 0         | `failed` (DEADLINE_EXCEEDED)                                                                                                              | No                         | delivery plan timestamps                         |
| Shutdown rejection    | 0         | `DeliveryReceipt`: `suppressed` (`failure_kind="shutdown_rejection"`, `error="shutdown_drain_timeout"` or `"delivery_rejected_shutdown"`) | No                         | `outbound_failed` counter                        |
| Replay capacity       | 0         | `error` (replay_capacity_exceeded)                                                                                                        | No                         | `capacity_rejections` counter                    |
| Replay duplicate      | 0         | `sent` (multiple receipts, source=replay)                                                                                                 | N/A (by design)            | receipt `replay_run_id`                          |
| Capability suppressed | 0         | `skipped` + `suppressed` receipt for routed events                                                                                        | No                         | receipt `failure_kind`, `error` field            |
| Loop prevented        | 0         | `suppressed` receipt persisted                                                                                                            | No                         | `loop_prevented` counter, RouteStats             |
| Degraded live health  | 0         | N/A                                                                                                                                       | No                         | `health.live_health.adapters[]`                  |
| Failed live health    | 0         | N/A                                                                                                                                       | No                         | `health.live_health.adapters[]`, `.error`        |

## Config Failure Drills

Config errors are caught before any adapter construction or I/O. The runtime never starts.

### Bad TOML Syntax

```bash
cat > /tmp/bad-syntax.toml <<'EOF'
[runtime]
name = "test
EOF
PYTHONPATH=src medre config check --config /tmp/bad-syntax.toml
```

Expected: exit code **2**, human-readable TOML parse error on stderr. Fix the syntax and re-run.

### Unknown Adapter Ref in Route

```bash
cat > /tmp/bad-route.toml <<'EOF'
[runtime]
name = "bad-route"

[storage]
backend = "memory"

[adapters.matrix.bot]
enabled = true
adapter_kind = "fake"

[routes.bad]
source_adapters = ["bot"]
dest_adapters = ["nonexistent_adapter"]
directionality = "source_to_dest"
enabled = true
EOF
PYTHONPATH=src medre routes validate --config /tmp/bad-route.toml
```

Expected: exit code **2**, `RouteValidationError` naming the unknown adapter.

Fix: verify `dest_adapters` references match adapter IDs in `[adapters.*]`.

Or run the drill:

```bash
PYTHONPATH=src medre smoke --drill bad_route_config --json
```

The drill exits 0 (it caught the expected error correctly).

**Caveat:** Disabled routes (`enabled = false`) are not validated against adapter refs. They may reference nonexistent adapters without error.

### Duplicate Route ID

```bash
PYTHONPATH=src medre routes validate --config /tmp/dup-route.toml
```

Expected: exit code **2**, duplicate route ID error. Rename one of the routes.

## Build Failure Drills

Build failures occur during adapter construction — after config parsing but before adapter startup.

```bash
PYTHONPATH=src medre smoke --drill all_adapters_build_fail --json
```

Expected: drill exits 0 (catches the error correctly), report includes `simulation_method` and `simulated: true`.

### Missing SDK Dependency

```bash
# Create a config requesting a real adapter without its SDK installed.
cat > /tmp/missing-sdk.toml <<'EOF'
[runtime]
name = "missing-sdk"

[storage]
backend = "memory"

[adapters.matrix.bot]
enabled = true
adapter_kind = "real"
homeserver = "https://example.com"
user_id = "@bot:example.com"
access_token = "fake"
room_allowlist = ["!room:example.com"]
encryption_mode = "plaintext"
EOF
PYTHONPATH=src medre diagnostics --config /tmp/missing-sdk.toml
```

Expected: if **all** adapters fail to build: exit code **3**. If **some** succeed: exit code **0** with `startup_health == "degraded"`.

Fix: install the missing SDK (`pip install -e ".[matrix]"`), then re-run `medre diagnostics`.

**Caveat:** A single adapter failing to build with others succeeding results in degraded health (exit 0), not a build failure exit code.

### Invalid Storage Path

```bash
cat > /tmp/bad-storage.toml <<'EOF'
[runtime]
name = "bad-storage"

[storage]
backend = "sqlite"
path = "/nonexistent/readonly/path/medre.sqlite"
EOF
PYTHONPATH=src medre run --config /tmp/bad-storage.toml
```

Expected: exit code **3**, error about directory creation or file open failure.

Fix: verify the storage path is on a writable filesystem. Check disk space and directory permissions.

## Startup Failure Drills

Startup failures occur after build succeeds but before adapters enter running state.

```bash
PYTHONPATH=src medre smoke --drill all_adapters_start_fail --json
PYTHONPATH=src medre smoke --drill partial_degraded_startup --json
```

### Total Startup Failure (Exit 4)

All adapters failed to start. The runtime does not enter RUNNING state.

Expected: exit code **4**, `startup.boot_summary.startup_outcome == "total_failure"`, `adapters_started == 0`.

Inspect: `startup.boot_summary.failed_adapter_ids`, `diagnostics.runtime_events` for `adapter_start_failed` events.

### Degraded Startup (Exit 0)

Some adapters started, others failed. The runtime enters RUNNING with degraded health.

Expected: exit code **0**, `startup.boot_summary.startup_outcome == "partial"`, `runtime_health == "degraded"`.

Inspect: `startup.boot_summary.failed_adapter_ids`, `routes.startup_readiness` for routes marked `degraded` or `skipped`.

**Caveat:** Degraded startup does NOT exit. The runtime keeps running. Routes referencing only failed adapters are skipped entirely. Routes with some failed targets operate in degraded mode.

## Runtime Delivery Failure Drills

Delivery failures occur while the runtime is running. The runtime stays up; individual deliveries fail.

### Renderer Failure (Permanent)

An event with an `event_kind` that no renderer handles produces a permanent rendering failure.

```bash
PYTHONPATH=src pytest tests/test_fake_bridge_smoke.py::TestRenderingContract -v
```

Expected: runtime stays running. `DeliveryReceipt`: `status == "failed"`, `failure_kind == "RENDERER_FAILURE"`. No retry.

Inspect: `medre inspect receipts --event <event_id> --storage-path /path/to/medre.db`

**Caveat:** Renderer failures are permanent. Fix the event kind or add a renderer that handles it.

### Adapter Permanent Failure

An adapter reports a non-recoverable error (authentication failure, invalid channel, permission denied).

Expected: `DeliveryReceipt`: `status == "failed"`, `failure_kind == "ADAPTER_PERMANENT"`. `attempt_number == 1`, `parent_receipt_id == None` (no retry chain).

Fix the underlying cause and replay the event. See [recovery-and-replay.md](recovery-and-replay.md).

### Adapter Transient Failure (With Retry)

An adapter raises a transient error (timeout, connection reset). The pipeline retries per `RetryPolicy`.

Expected receipt chain:

```json
[
  {
    "receipt_id": "rcpt-001",
    "status": "failed",
    "failure_kind": "ADAPTER_TRANSIENT",
    "attempt_number": 1,
    "parent_receipt_id": null
  },
  {
    "receipt_id": "rcpt-002",
    "status": "sent",
    "failure_kind": null,
    "attempt_number": 2,
    "parent_receipt_id": "rcpt-001"
  }
]
```

Inspect: full receipt lineage via `parent_receipt_id` chain, `attempt_number` on each receipt.

**Caveat:** Transient retries may produce duplicate deliveries on transports that don't support idempotent sends (Matrix).

### Capacity Exceeded

More concurrent deliveries than `max_inflight_deliveries` are attempted.

Expected: `DeliveryOutcome`: `error == "delivery_capacity_exceeded"`. No retry.

Fix: increase `max_inflight_deliveries` in `[runtime.limits]`, or reduce active routes / source event rate.

**Caveat:** Capacity rejection is by design — it prevents unbounded memory accumulation. The event was correctly matched and planned; the failure is at the delivery execution stage.

### Deadline Exceeded

A delivery plan's absolute deadline passes before the adapter completes.

Expected: `DeliveryReceipt`: `status == "failed"`, `failure_kind == "DEADLINE_EXCEEDED"`. No retry.

Fix: check the delivery plan's deadline configuration, adapter latency, and transport-level issues.

## Shutdown Failure Drills

### Delivery Rejected During Shutdown

In-flight adapter deliveries are drained up to `shutdown_drain_timeout_seconds`. Deliveries that do not complete within the drain period are abandoned with evidence persisted as `suppressed` receipts with `failure_kind="shutdown_rejection"` and `error="shutdown_drain_timeout"`. New deliveries attempted after the capacity controller stops accepting (step 1 of shutdown) are rejected with the same `failure_kind` but `error="delivery_rejected_shutdown"`.

Expected: `DeliveryReceipt`: `status == "suppressed"`, `failure_kind == "shutdown_rejection"`, `error == "shutdown_drain_timeout"` (drain timeout) or `"delivery_rejected_shutdown"` (new delivery during shutdown). `outbound_failed` counter incremented.

Fix: if these deliveries are important, replay the corresponding events after restart. Replay is manual and one-shot — each invocation processes stored events once and exits. Non-terminal outbox rows survive shutdown as resumable work and are reclaimed on next startup.

### Replay Rejected During Shutdown

Replay events in progress when shutdown begins are rejected.

Expected: replay result: `status == "error"`, `error == "replay_rejected_shutdown"`.

Fix: re-initiate replay after restart.

### Shutdown Hangs (Adapter or Worker Does Not Stop)

When the MEDRE process appears stuck during shutdown, the most likely cause is
an adapter or background worker that does not respond to the stop signal within
the configured timeout.

#### What the runtime does automatically

The runtime enforces a timeout (default 10 s) on every adapter stop call. If an
adapter does not return within the deadline, the runtime records the failure,
sets that adapter to `FAILED` state, and continues stopping the remaining
adapters, pipeline, and storage. A `RuntimeShutdownError` is raised at the end
with a summary of which adapters failed.

The RetryWorker follows the same pattern: it gets a configurable grace period
(default 10 seconds, sourced from `runtime.shutdown_timeout_seconds`) after
receiving the shutdown signal. If it does not finish within its configured
stop timeout, its background task is cancelled and a second bounded grace
period (also `stop_timeout_seconds`) is applied.

The RetryWorker stop timeout is wired from
`config.runtime.shutdown_timeout_seconds` (the `[runtime]` TOML section,
default `10`) — the same value that governs per-adapter stop deadlines.
It is not a `[retry]` config field. The standalone `RetryWorker`
constructor has its own default of `5.0` seconds, which only applies
when constructing the worker outside `MedreApp`; the app-managed
worker always uses the runtime config value above.

If a RetryWorker task is cancellation-resistant (e.g. a storage call
refuses to release), `stop()` returns within `2 * stop_timeout_seconds`
and emits a `retry_abandoned` event. See
`docs/dev/resource-lifecycle.md` for the full abandonment contract.

#### How to diagnose a shutdown hang

1. **Check the logs.** The runtime logs per-adapter stop progress at DEBUG
   level. Look for entries like:
   - `"Adapter <transport>.<id> stopping"` -- stop initiated
   - `"Timeout stopping adapter <transport>.<id> after Xs, cancelling"` -- adapter hung past cooperative grace
   - `"Adapter <transport>.<id> did not stop after cancel within Xs; abandoning"` -- adapter did not respond to cancel
   - `"Cancelled while stopping adapter <transport>.<id>"` -- adapter cancelled by external cancellation
   - `"RetryWorker task did not cancel within Xs; abandoning (_task is still referenced, state.abandoned=True)"` -- retry worker abandoned after cancel grace. No intermediate "cancelling" log; the polling loop moves directly from deadline expiry to `task.cancel()`.

2. **Check the final error.** If the runtime raises `RuntimeShutdownError`,
   the message lists each failed adapter or subsystem:

   ```text
   RuntimeShutdownError: Errors during shutdown; alpha: Timeout stopping adapter meshtastic.alpha after 10.0s
   ```

3. **Check adapter state after exit.** If the process eventually exits, the
   evidence bundle or diagnostics output shows which adapters ended in `FAILED`
   state rather than `STOPPED`.

4. **Check for stuck in-flight deliveries.** If the drain phase times out,
   abandoned deliveries are persisted as suppressed receipts:

   ```sql
   SELECT event_id, target_adapter, error, created_at
   FROM delivery_receipts
   WHERE failure_kind = 'shutdown_rejection'
     AND error = 'shutdown_drain_timeout'
   ORDER BY created_at DESC
   LIMIT 20;
   ```

5. **Send a SIGABRT for a stack trace** if the process is completely
   unresponsive (not just slow). Python dumps thread stacks to stderr on
   SIGABRT. This helps identify which code path is blocked:

   ```bash
   kill -ABRT <pid>
   ```

#### Common causes

| Symptom                                          | Likely cause                                                                  |
| ------------------------------------------------ | ----------------------------------------------------------------------------- |
| Adapter times out on every shutdown              | Transport SDK connection is in a blocking call with no timeout                |
| RetryWorker task may be cancelled then abandoned | Storage operation (claim/renew) is blocked or very slow                       |
| Process never exits after `RuntimeShutdownError` | A non-daemon thread created by a third-party SDK is keeping the process alive |
| Drain phase times out repeatedly                 | Slow or unresponsive adapter callbacks holding capacity semaphore             |

#### Tuning shutdown timeouts

```toml
[runtime]
shutdown_timeout_seconds = 15      # per-adapter stop deadline (default 10)
shutdown_drain_timeout_seconds = 20  # in-flight work drain deadline (default 10)
```

Increasing these values gives adapters more time to clean up, but also makes
shutdown slower. If an adapter genuinely cannot stop (hard lock in a C
extension), no timeout value will help, and the process needs a `SIGKILL`.

## Loop Prevention

MEDRE provides three layers of loop prevention at different stages.

### Self-Loop Guard (Runtime, Per-Delivery)

Catches: a route delivering an event back to its own `source_adapter`.

When it fires: every delivery attempt. Runtime guard, not config-time check.

Test:

```bash
PYTHONPATH=src pytest tests/test_fake_bridge_smoke.py::TestLoopPrevention -v
```

Expected: `DeliveryOutcome`: `status == "skipped"`, `error` contains `"loop_prevented"`. Target adapter has zero delivered payloads. `DeliveryReceipt` with `status="suppressed"` is persisted. `accounting.loop_prevented >= 1`.

### Route-Trace Loop Prevention (Runtime, Per-Delivery)

Catches: an event being re-routed through a route whose ID already appears in its `route_trace`.

The `route_trace` is bounded to 16 entries — older entries are dropped when the trace exceeds the limit.

When it fires: every delivery attempt, before the self-loop guard.

Expected: `DeliveryOutcome`: `status == "skipped"`, `error == "loop_prevented: route already in route_trace"`.

### Config-Time Detection (Startup)

**Direct loop detection:** `check_route_loops` builds edge pairs from all enabled routes. If both `(A, B)` and `(B, A)` edges exist, a warning is logged. Startup continues.

**Multi-hop DFS cycle detection:** `check_route_loops` runs DFS on the adapter adjacency graph. Back-edges produce cycle path warnings. Startup continues.

Both config-time checks produce warnings only. They do not block startup.

### Loop Prevention Scope

| Context     | Mechanism                          | Blocks startup?   | Blocks delivery? |
| ----------- | ---------------------------------- | ----------------- | ---------------- |
| Config-time | `check_route_loops` (direct + DFS) | No (warning only) | N/A              |
| Runtime     | Route-trace loop prevention        | N/A               | Yes              |
| Runtime     | Self-loop guard                    | N/A               | Yes              |
| Replay      | `_filter_replay_loops`             | N/A               | Yes              |

### Capability Suppression

MEDRE suppresses delivery when the target adapter lacks capability for the event's relation type (reactions, edits, deletes, replies) or event kind (attachments, presence).

#### Pre-outbox Capability Skip

When the `CapabilityDecisionResolver` determines the capability level is `"unsupported"`, the delivery is skipped before rendering and adapter invocation. For stored routed events, a `DeliveryReceipt(status="suppressed")` is persisted with `failure_kind="capability_suppressed"`. The outcome also carries `failure_kind="capability_suppressed"`.

#### Post-planning Suppressed Receipt

When suppression occurs through the direct target-delivery defense-in-depth path, a `DeliveryReceipt(status="suppressed")` is also persisted with `failure_kind="capability_suppressed"`. The `error` field contains the suppression reason (e.g. `"capability_suppressed: reactions unsupported"`).

#### Diagnosis

```bash
# Check for capability-suppressed receipts
sqlite3 {state}/medre.sqlite "
  SELECT event_id, target_adapter, failure_kind, error, created_at
  FROM delivery_receipts
  WHERE failure_kind = 'capability_suppressed'
  ORDER BY created_at DESC
  LIMIT 20;
"

# Check the evidence bundle for capability context
medre inspect event <event_id> --evidence --storage-path /path/to/medre.db
```

Look for `suppression_reason`, `capability_field`, and `capability_level` in the `delivery_state_by_target` section of the evidence output. These identify which capability caused the suppression and at what level.

#### Resolution

1. Check the target adapter's transport profile capability declarations.
2. If the adapter should support the capability, update its profile JSON and adapter code.
3. If the adapter genuinely cannot support the capability, this suppression is correct behavior. No action needed.
4. Re-evaluate by replaying the event after capability changes.

## Route Lifecycle

A route progresses through: Configuration -> Validation -> Registration -> Matching -> Delivery.

### Configuration

Routes are defined in TOML under `[routes.<id>]` sections:

```toml
[routes.matrix_to_radio]
source_adapters = ["bot1"]
dest_adapters = ["longfast"]
directionality = "source_to_dest"
enabled = true

[routes.matrix_to_radio.policy]
allowed_event_types = ["message.created"]
```

### Validation

At startup, `validate_route_adapter_refs` checks that every enabled route's `source_adapters` and `dest_adapters` reference adapter IDs that exist in the assembled runtime. Invalid references produce `RouteValidationError`.

### Registration

1. `build_runtime_routes` converts TOML configs into `Route` objects. Bidirectional routes expand into two internal routes.
2. `check_route_loops` runs DFS cycle detection.
3. Routes register on the `Router` in TOML declaration order.
4. Registration is all-or-nothing.

### Matching

At runtime, when an event enters the pipeline:

1. `Router.match(event)` iterates registered routes in registration order.
2. A route matches if the event's `source_adapter` is in the route's source spec.
3. Matched route IDs are recorded in `RoutingMetadata.route_trace`.

### Delivery

For each matched route's targets:

1. Route-trace loop prevention check.
2. Self-loop guard check.
3. Event is rendered and delivered.
4. `DeliveryReceipt` is recorded with `route_id`.
5. `RouteStats` counters are updated.

## Route Attribution Visibility

Every routed delivery carries attribution data:

| Location          | Field                         | Persisted?                |
| ----------------- | ----------------------------- | ------------------------- |
| Event metadata    | `RoutingMetadata.route_trace` | No (ephemeral)            |
| Delivery receipts | `DeliveryReceipt.route_id`    | Yes (SQLite)              |
| Delivery outcomes | `DeliveryOutcome.route_id`    | No (consumed by pipeline) |
| RouteStats        | `RouteStats.snapshot()`       | No (process-local)        |
| Replay results    | `ReplayRouteAttribution`      | No (in-memory)            |

Route attribution is internal to MEDRE. It does not appear in radio packets, Matrix messages, LXMF messages, or any external output.

## Common Troubleshooting

### "Unknown adapter" Startup Failure

**Symptom:** `RouteValidationError` during startup.

**Cause:** An enabled route references an adapter ID not present in the runtime configuration.

**Fix:** Check the route's `source_adapters` and `dest_adapters` against the `[adapters.*]` sections. Either add the missing adapter or update the route references.

### Cycle Warning at Startup

**Symptom:** Log messages like `"Direct routing loop detected"` or `"Route cycle detected"`.

**Cause:** Routes form a cycle in the adapter adjacency graph.

**Effect:** Startup continues. The self-loop guard at runtime prevents immediate self-loops. Multi-hop cycles are not automatically prevented at delivery time.

**Fix:** Remove or disable one of the routes in the cycle, or restructure to break the cycle (e.g., use hub-and-spoke instead of ring).

### Disabled Routes Not Matching

**Symptom:** A route is defined in TOML but events never match it.

**Cause:** The route has `enabled = false`.

**Fix:** Set `enabled = true` and restart.

### Stale Route Configuration

**Symptom:** Route behavior doesn't match expectations after a TOML edit.

**Cause:** Route configuration is loaded at startup. Runtime changes to TOML files are not picked up until restart.

**Fix:** Restart the MEDRE process after route configuration changes.

### Self-Loop Guard Firing Unexpectedly

**Symptom:** Deliveries showing `status="skipped"` with `error="loop_prevented"`.

**Cause:** A route's destination adapter matches the source adapter of the event.

**Fix:** Check the route's `dest_adapters` list. If the source adapter appears in it, remove it or restructure the route.

### Route Matches But No Delivery

**Symptom:** `route_trace` is populated but no delivery occurs.

**Possible causes:**

- The adapter is not started or has no active connection.
- The delivery plan has a `DEADLINE_EXCEEDED` failure.
- The renderer failed to produce output.

**Fix:** Check delivery receipts for the event. Check adapter health. Check `RouteStats` for error details.

## Inspect Follow-Up Quick Reference

Read-only inspection commands require `--storage-path` for direct SQLite access.

| After this failure...          | Run this to inspect                                                                                         |
| ------------------------------ | ----------------------------------------------------------------------------------------------------------- |
| Config error (exit 2)          | `medre config check --config <path>`                                                                        |
| Config error (drill)           | `medre smoke --drill bad_route_config --json`                                                               |
| Build failure (exit 3)         | `medre diagnostics --config <path>` → `startup.build_failures`                                              |
| Total startup failure (exit 4) | `medre diagnostics --config <path>` → `startup.boot_summary`                                                |
| Degraded startup               | `medre diagnostics --refresh-health` → `health.live_health`                                                 |
| Renderer failure               | `medre inspect receipts --event <id> --storage-path <db>`                                                   |
| Adapter permanent              | `medre inspect receipts --event <id>` + adapter `diagnostics()`                                             |
| Adapter transient              | Full receipt chain via `parent_receipt_id`                                                                  |
| Capacity exceeded              | `capacity_rejections` counter in logs; tune `max_inflight_deliveries`                                       |
| Deadline exceeded              | Delivery plan timestamps vs. actual adapter latency                                                         |
| Shutdown rejection             | `outbound_failed` counter; replay orphaned events after restart                                             |
| Replay duplicate               | `medre inspect receipts --replay-run <id> --storage-path <db>`                                              |
| Live health degraded           | `medre diagnostics --refresh-health` → per-adapter `.error`                                                 |
| Loop prevented                 | `RouteStats` → `loop_prevented`; `accounting.snapshot()`                                                    |
| Capability suppressed          | `failure_kind="capability_suppressed"` in receipts; `suppression_reason`, `capability_field` in report dict |

## Convergence State Troubleshooting

### "Convergence severity is inconsistent"

**Symptom:** The convergence summary for an event shows `worst_severity: "inconsistent"` for one or more targets.

**Cause:** The outbox item and latest receipt disagree on whether the delivery is terminal. For example, the outbox says `sent` but the latest receipt says `queued`.

**Investigation:**

```sql
-- Check the receipt chain for the affected delivery_plan_id
SELECT receipt_id, status, attempt_number, failure_kind, created_at
FROM delivery_receipts
WHERE delivery_plan_id = '<plan_id>'
ORDER BY attempt_number;

-- Check the outbox item
SELECT outbox_id, status, attempt_number, updated_at
FROM delivery_outbox
WHERE delivery_plan_id = '<plan_id>';
```

**Resolution:**

1. Determine which record is stale (outbox or receipt).
2. If the outbox is stale (already terminal but receipt is mid-flight), the delivery completed and the receipt may be from a stale queued correlation. No data loss.
3. If the receipt is stale (says `sent` but outbox is `pending`), the delivery likely completed but the outbox was not updated. Check if the adapter callback was received.
4. For true inconsistencies (data loss suspected), consider replaying the event.

### "Orphaned outbox items after crash"

**Symptom:** The orphan report shows `orphaned_outbox` findings for non-terminal outbox items referencing events not in the event catalogue.

**Cause:** The event was deleted or the event store was partially corrupted.

**Fix:** If the event was intentionally deleted, cancel or abandon the orphaned outbox row. If the event should exist, investigate the storage integrity.

### "Uncorrelated queued outbox items"

**Symptom:** The retry/outbox summary shows queued items with reason `"Queued, awaiting adapter callback (no outbox_id, no receipt linkage)"`.

**Cause:** The adapter callback has not yet supplied `outbox_id + attempt_number` linkage for the queued item.

**Fix:** Wait for the stale-grace reclaim timer (default 300 s) to reclaim the item. If the item remains uncorrelated after the grace period, check that the adapter is properly propagating `outbox_id` and `attempt_number` through its queue processing.

### "Replay-only callback warning"

**Symptom:** Logs show a warning about "only replay-sourced queued receipts found" during callback correlation, and no supplemental sent receipt is created.

**Cause:** A live adapter callback is arriving, but the only matching queued receipt(s) are from a replay run. `OutboundNativeRefRecord` carries no trusted replay provenance, so replay-only queued receipts are skipped to prevent live recovery state mutation.

**Fix:** Verify that the callback is from the replay run, not a live delivery. If a live delivery also occurred, the live receipt chain remains intact. The replay queued receipt stays uncorrelated. If this is a live callback that should have a matching live queued receipt, investigate whether the live delivery produced a queued receipt. This restriction may be relaxed in a future version when callback records carry trusted replay provenance.

## Lifecycle Convergence Finding Troubleshooting

Lifecycle convergence findings appear in the `lifecycle_convergence_report` section of the evidence bundle. They detect specific contradictions between outbox item states and delivery receipt states.

### "Terminal receipt but non-terminal outbox"

**Symptom:** The lifecycle convergence report shows `terminal_receipt_nonterminal_outbox` findings.

**Cause:** A delivery receipt with terminal status (sent, suppressed, dead_lettered) exists, but the corresponding outbox item is still in a non-terminal state.

**Investigation:**

```sql
SELECT outbox_id, status, updated_at FROM delivery_outbox
WHERE delivery_plan_id = '<plan_id>';
SELECT receipt_id, status, attempt_number FROM delivery_receipts
WHERE delivery_plan_id = '<plan_id>' ORDER BY attempt_number;
```

**Resolution:** Determine which record is stale. If the receipt is correct, the outbox was not transitioned. This may resolve on its own if the outbox is reclaimed. If persistent, the outbox row may need operator attention.

### "Terminal outbox but non-terminal receipt"

**Symptom:** The lifecycle convergence report shows `terminal_outbox_nonterminal_receipt` findings.

**Cause:** The outbox item has reached a terminal status, but the latest receipt is still non-terminal (queued or failed).

**Investigation:** Same SQL as above.

**Resolution:** The delivery likely completed but the receipt chain may be incomplete. Check whether the adapter callback was received and whether a supplemental sent receipt should have been created.

### "Retry wait without next retry timestamp"

**Symptom:** The lifecycle convergence report shows `retry_wait_missing_next_retry` findings.

**Cause:** An outbox item is in `retry_wait` state but has no valid `next_attempt_at` timestamp.

**Fix:** The retry scheduler cannot determine when to retry. Check whether the retry was set up correctly. Consider replaying the event or investigating why the timestamp was not populated.

### "Stalled delivery plan"

**Symptom:** The lifecycle convergence report shows `stalled_delivery_plan` findings.

**Cause:** A non-terminal outbox item has not been updated for longer than the stall threshold (default 1 hour).

**Fix:** Check whether the worker that claimed this item is still running. Expired leases should be reclaimed by `claim_due_outbox_items()`. If the item remains stalled, check the RetryWorker status and adapter health.

### "Attempt count regression"

**Symptom:** The lifecycle convergence report shows `attempt_count_regression` findings.

**Cause:** Within the same delivery target, a later receipt has a lower attempt number than an earlier receipt.

**Fix:** This is a data integrity issue. Audit the receipt chain for the affected `delivery_plan_id`. Attempt numbers should monotonically increase within a retry chain. If the data is incorrect, consider replaying the event.

### "Receipt sequence gap"

**Symptom:** The lifecycle convergence report shows `receipt_sequence_gap` findings.

**Cause:** Receipts for the same target have sequence numbers that skip by more than 1.

**Fix:** Gaps may indicate lost receipts or concurrent delivery attempts. Check whether receipts were created but not persisted, or whether multiple concurrent deliveries to the same target produced interleaved sequences.

### Important

Lifecycle convergence diagnostics are deterministic and read-only. They never change retry scheduling, worker behavior, or storage state. No automatic repair occurs based on these findings.

## Explicit Non-Guarantees

1. **Distributed loop prevention.** Loop detection is local to a single MEDRE process.
2. **Exactly-once delivery.** No transport provides exactly-once semantics. Radio is probabilistic. Matrix is at-least-once. LXMF is at-least-once with eventual delivery.
3. **Radio delivery confirmation.** Meshtastic and MeshCore cannot confirm remote receipt. `sent` means local acceptance.
4. **Cross-instance coordination.** Routes, attribution, and stats are local to the process.
5. **Automatic route reconfiguration.** Route changes require a restart.
6. **Delivery ordering guarantees.** Events are matched in route registration order, but async delivery means actual outbound ordering depends on transport latency.
7. **Replay deduplication.** Replayed events may be delivered again if they match current routes.
8. **Persistent queue.** Runtime execution state (counters, gauges, route stats) is in-memory only. SQLite receipt and outbox evidence persists across restarts. In-flight adapter deliveries abandoned after drain timeout produce `suppressed` receipts with `failure_kind="shutdown_rejection"` and `error="shutdown_drain_timeout"`; non-terminal outbox items survive shutdown as resumable work.

## See Also

- [diagnostics-and-evidence.md](diagnostics-and-evidence.md) — evidence provenance, bundle collection, report shapes
- [recovery-and-replay.md](recovery-and-replay.md) — crash recovery, orphan detection, replay modes
