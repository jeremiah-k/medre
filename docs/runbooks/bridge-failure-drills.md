# Bridge Failure Drills Runbook

> Last updated: 2026-05-14
> Scope: Operator guide for interpreting and reproducing failure scenarios
> Status: Pre-beta. Not production. All drills use fake adapters unless noted.
> Prerequisites: medre installed with `[dev]` extras, no Docker or live endpoints needed.

This runbook describes how to reproduce, interpret, and diagnose each failure
category in the MEDRE runtime. Each drill provides:

- A **command** to trigger or observe the failure.
- The **expected pass/fail report** (exit code, JSON fields, or log output).
- **What the operator should inspect next.**
- **Caveats** specific to the drill.

This is an interpretation guide for operators. MEDRE does not provide automated
remediation, per-adapter restart, or self-healing. Operators diagnose and act
manually.

Pre-runtime drills (`bad_route_config`, `all_adapters_build_fail`,
`partial_degraded_startup`, `all_adapters_start_fail`) can be run as a batch
via `medre evidence --config <path> --json` or individually via
`medre smoke --drill <name> --json`. See
[Bridge Evidence Bundle](bridge-evidence-bundle.md) for the full collection
workflow.

For failure categories not covered here (route topology),
see [Routing Correctness](routing-correctness.md) and
[Bridge Operation](bridge-operation.md).


## 1. Failure Category Quick Reference

| Category | Exit code | Receipt status | Retry? | Where to inspect |
|----------|-----------|---------------|--------|------------------|
| Config error | 2 | None (no runtime) | No | stderr, `medre config check` |
| Build failure | 3 | None (no delivery) | No | `startup.build_failures`, logs |
| Total startup failure | 4 | None (no delivery) | No | `startup.boot_summary`, logs |
| Degraded startup | 0 | Partial | Yes (for started adapters) | `failed_adapter_ids`, `routes.startup_readiness` |
| Renderer failure | 0 | `failed` (RENDERER_FAILURE) | No | `medre inspect receipts`, RouteStats |
| Adapter permanent | 0 | `failed` (ADAPTER_PERMANENT) | No | receipt lineage, adapter `diagnostics()` |
| Adapter transient | 0 | `sent` (after retry) or `failed` | Yes (up to max_attempts) | receipt `attempt_number`, `parent_receipt_id` |
| Capacity exceeded | 0 | `failed` (delivery_capacity_exceeded) | No | `capacity_rejections` counter, logs |
| Deadline exceeded | 0 | `failed` (DEADLINE_EXCEEDED) | No | delivery plan timestamps |
| Shutdown rejection | 0 | `failed` (delivery_rejected_shutdown) | No | `outbound_failed` counter |
| Replay capacity | 0 | `error` (replay_capacity_exceeded) | No | `capacity_rejections` counter |
| Replay duplicate | 0 | `sent` (multiple receipts, source=replay) | N/A (by design) | receipt `replay_run_id` |
| Loop prevented | 0 | `skipped` (no receipt) | No | `loop_prevented` counter, RouteStats |
| Degraded live health | 0 (command succeeds) | N/A | No | `health.live_health.adapters[]` |
| Failed live health | 0 (command succeeds) | N/A | No | `health.live_health.adapters[]`, `.error` |


## 2. Smoke and Inspect: Persistence Boundary

``medre smoke`` uses in-memory storage by default. When the process exits,
all stored evidence is released. The JSON report printed to stdout is the only
surviving record.

Pass ``--storage-path <path>`` to persist evidence to a SQLite database that
``medre inspect`` can query afterward.

``medre inspect`` subcommands are read-only and require a persistent SQLite
database. They exit with code 2 if the config uses ``backend = "memory"``:

```
Error: storage backend is 'memory' — no persistent data to inspect.
```

To inspect stored evidence after a run, use ``medre run`` with SQLite storage:

```toml
[storage]
backend = "sqlite"
```

Then inspect:

```bash
medre inspect event <event_id> --config my-bridge.toml
medre inspect receipts --event <event_id> --config my-bridge.toml
medre inspect receipts --replay-run <run_id> --config my-bridge.toml
medre inspect native-ref --adapter <name> --message <native_id> --config my-bridge.toml
```

See [Fake Bridge Smoke Runbook](fake-bridge-smoke-runbook.md#smoke-persistence-caveat)
and [Runtime Persistence](runtime-operation.md#persistence-and-crash-semantics).


## 3. Config Failure Drills

Config errors are caught before any adapter construction or I/O. The runtime
never starts.

### 3.1 Bad TOML Syntax

**Command:**

```bash
cat > /tmp/bad-syntax.toml <<'EOF'
[runtime]
name = "test
EOF
PYTHONPATH=src medre config check --config /tmp/bad-syntax.toml
```

**Expected FAIL:**

- Exit code: **2** (`EXIT_CONFIG`)
- stderr: human-readable TOML parse error
- No JSON output

**Inspect next:**

1. Fix the TOML syntax error.
2. Re-run `medre config check --config /tmp/bad-syntax.toml`.
3. Repeat until exit code 0.

**Caveat:** Config errors block all downstream operations. No adapter
construction, no storage initialization, no network I/O.

### 3.2 Unknown Adapter Ref in Route

**Command (config validation):**

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

**Command (drill):**

```bash
PYTHONPATH=src medre smoke --drill bad_route_config --json
```

**Expected FAIL:**

- Route validation: exit code **2** (`EXIT_CONFIG`) — the runtime would reject this config.
- stderr: `RouteValidationError` naming the unknown adapter
- No adapter starts
- Drill report: `status == "pass"` — the drill itself exits 0 because the expected error was correctly observed

**Inspect next:**

1. Verify `dest_adapters` references match adapter IDs in `[adapters.*]`.
2. Re-run `medre routes validate`.

**Caveat:** Disabled routes (`enabled = false`) are not validated against
adapter refs. They may reference nonexistent adapters without error.

### 3.3 Duplicate Route ID

**Command:**

```bash
cat > /tmp/dup-route.toml <<'EOF'
[runtime]
name = "dup-route"

[storage]
backend = "memory"

[adapters.matrix.bot]
enabled = true
adapter_kind = "fake"

[adapters.meshtastic.radio]
enabled = true
adapter_kind = "fake"

[routes.dup]
source_adapters = ["bot"]
dest_adapters = ["radio"]
directionality = "source_to_dest"
enabled = true
EOF
PYTHONPATH=src medre routes validate --config /tmp/dup-route.toml
```

**Expected FAIL:**

- Exit code: **2**
- stderr: duplicate route ID error

**Inspect next:** Rename one of the routes to a unique ID.


## 4. Build Failure Drills

Build failures occur during adapter construction — after config parsing but
before adapter startup. The runtime does not start.

### 4.0 Drill Command (All Build Failures)

Run the pre-runtime build failure drill:

```bash
PYTHONPATH=src medre smoke --drill all_adapters_build_fail --json
```

**Expected pass (drill catches the error correctly):**

- Exit code: **0** (drill itself succeeds)
- Drill report: `status == "pass"`
- Drill report includes `simulation_method` (e.g. `"config_injection"`,
  `"failure_injection"`, `"fake_adapter"`) documenting how the failure
  scenario was produced.
- Drill report includes `simulated: true` and `scenario_category: "drill"`.
- Drill steps include config construction, build attempt, and exit code
  verification
- The drill proves that the runtime exits with code 3 when all adapters fail
  to build

### 4.1 Missing SDK Dependency

**Command:**

```bash
# Create a config requesting a real adapter without its SDK installed.
# Ensure mindroom-nio is NOT installed first.
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

**Expected FAIL:**

- If **all** adapters fail to build: exit code **3** (`EXIT_BUILD`).
- If **some** adapters build and others don't: exit code **0** with
  `startup_health == "degraded"`.
- `startup.build_failures` in snapshot contains failed adapter ID and error.

**Inspect next:**

1. Check `startup.build_failures` in `medre diagnostics` JSON output.
2. Install the missing SDK: `pip install -e ".[matrix]"`.
3. Re-run `medre diagnostics`.

**Caveat:** A single adapter failing to build with others succeeding results
in degraded health (exit 0), not a build failure exit code. The runtime keeps
running with whatever adapters succeeded.

### 4.2 Invalid Storage Path

**Command:**

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

**Expected FAIL:**

- Exit code: **3** (`EXIT_BUILD`) — storage initialization failure.
- stderr: error about directory creation or file open failure.

**Inspect next:**

1. Verify the storage path is on a writable filesystem.
2. Check disk space and directory permissions.
3. Use `medre paths` to see resolved paths for the config.


## 5. Startup Failure Drills

Startup failures occur after build succeeds but before adapters enter running
state.

### 5.0 Pre-Runtime Drill Commands

Run startup failure drills as a batch:

```bash
PYTHONPATH=src medre smoke --drill all_adapters_start_fail --json
PYTHONPATH=src medre smoke --drill partial_degraded_startup --json
```

Or collect both as part of a full evidence bundle:

```bash
PYTHONPATH=src medre evidence --config my-bridge.toml --json > bundle.json
```

### 5.1 Total Startup Failure (Exit 4)

All adapters failed to start. The runtime does not enter RUNNING state. This is
difficult to trigger from config alone with fake adapters. Observe behavior via
the test suite:

**Command (test-based):**

```bash
PYTHONPATH=src pytest tests/test_runtime_startup_classification.py -v -k "total_failure"
```

**Expected FAIL indicators:**

- Exit code: **4** (`EXIT_STARTUP`)
- `startup.boot_summary.startup_outcome == "total_failure"`
- `startup.boot_summary.adapters_started == 0`
- `startup.boot_summary.adapters_failed > 0`
- `lifecycle.runtime_state == "failed"`

**Inspect next:**

1. `startup.boot_summary.failed_adapter_ids` — which adapters failed.
2. `diagnostics.runtime_events` — `adapter_start_failed` events.
3. Logs for the specific error per adapter.

**JSON snippet (illustrative):**

```json
{
  "startup": {
    "startup_health": "failed",
    "boot_summary": {
      "startup_outcome": "total_failure",
      "adapters_started": 0,
      "adapters_failed": 2,
      "adapters_total": 2,
      "started_adapter_ids": [],
      "failed_adapter_ids": ["bot", "radio"],
      "build_failure_ids": [],
      "route_count": 0
    }
  }
}
```

**Caveat:** Total startup failure means zero adapters started. The runtime does
not enter RUNNING state and cannot process events.

### 5.2 Degraded Startup (Exit 0)

Some adapters started, others failed. The runtime enters RUNNING with degraded
health and continues operating.

**Command:**

```bash
# Use a mixed config where at least one adapter can start.
# See runtime-operation.md for diagnosing degraded startup.
PYTHONPATH=src medre run --config /tmp/mixed-degraded.toml
```

**Expected PASS (degraded):**

- Exit code: **0** — runtime continues operating.
- Log: `"Assembly complete: N/M adapters started, K failed"`.
- `startup.boot_summary.startup_outcome == "partial"`.
- `startup.boot_summary.runtime_health == "degraded"`.

**Inspect next:**

1. `startup.boot_summary.failed_adapter_ids` — which adapters failed.
2. `startup.boot_summary.started_adapter_ids` — which adapters are running.
3. `routes.startup_readiness` — routes marked `degraded` (some targets
   failed) or `skipped` (source adapter failed).

**JSON snippet (illustrative):**

```json
{
  "startup": {
    "startup_health": "degraded",
    "boot_summary": {
      "startup_outcome": "partial",
      "adapters_started": 1,
      "adapters_failed": 1,
      "adapters_total": 2,
      "started_adapter_ids": ["bot"],
      "failed_adapter_ids": ["radio"],
      "build_failure_ids": [],
      "route_count": 1
    }
  },
  "routes": {
    "startup_readiness": [
      {
        "route_id": "bot-to-radio",
        "readiness": "degraded",
        "failed_adapter_ids": ["radio"]
      }
    ]
  }
}
```

**Caveat:** Degraded startup does NOT exit. The runtime keeps running with
whatever adapters succeeded. Routes referencing only failed adapters are
skipped entirely. Routes with some failed targets operate in degraded mode —
events are delivered to available targets only.


## 6. Runtime Delivery Failure Drills

Delivery failures occur while the runtime is running. The runtime stays up;
individual deliveries fail.

### 6.1 Renderer Failure (Permanent)

An event with an `event_kind` that no renderer handles produces a permanent
rendering failure. The event is stored but delivery is not attempted.

**Command (test-based):**

```bash
PYTHONPATH=src pytest tests/test_fake_bridge_smoke.py::TestRenderingContract -v
```

**Expected FAIL:**

- Runtime stays running (no exit).
- `DeliveryReceipt`: `status == "failed"`, `failure_kind == "RENDERER_FAILURE"`.
- No retry — renderer failures are deterministic and permanent.
- `RouteStats`: `failed` counter incremented for the route.

**Inspect next (with SQLite storage):**

```bash
medre inspect receipts --event <event_id> --config my-bridge.toml
```

Look for `failure_kind == "RENDERER_FAILURE"`. The event is stored (ingestion
succeeded); only delivery failed.

**Caveat:** Renderer failures are permanent. No amount of retrying will fix
them. The fix is to either correct the event kind or add a renderer that
handles it.

### 6.2 Adapter Permanent Failure

An adapter reports a non-recoverable error. The delivery fails permanently.

**Command (test-based):**

```bash
PYTHONPATH=src pytest tests/ -k "permanent" -v --co -q
```

**Expected FAIL:**

- `DeliveryReceipt`: `status == "failed"`, `failure_kind == "ADAPTER_PERMANENT"`.
- `attempt_number == 1`, `parent_receipt_id == None` (no retry chain).
- No retry — permanent failures are not retried.

**Inspect next:**

1. `medre inspect receipts --event <event_id>` — receipt with failure details.
2. Adapter `diagnostics()` — current adapter state.
3. Logs for the specific error from the adapter.

**Caveat:** `ADAPTER_PERMANENT` means the adapter determined the failure is
not recoverable (e.g., authentication failure, invalid channel, permission
denied). Fixing the underlying cause and retrying requires operator action.

### 6.3 Adapter Transient Failure (With Retry)

An adapter raises a transient error (timeout, connection reset). The pipeline
retries per `RetryPolicy`.

**Command (test-based):**

```bash
PYTHONPATH=src pytest tests/ -k "transient" -v --co -q
```

**Expected behavior:**

- First attempt: `DeliveryReceipt` with `status == "failed"`,
  `failure_kind == "ADAPTER_TRANSIENT"`, `attempt_number == 1`.
- Pipeline schedules retry with exponential backoff.
- Subsequent attempt: `DeliveryReceipt` with `attempt_number == 2`,
  `parent_receipt_id == <first-receipt-id>`.
- If retry succeeds: final receipt `status == "sent"`.
- If all retries exhausted: final receipt `status == "failed"`.

**Inspect next:**

1. Full receipt lineage via `parent_receipt_id` chain.
2. `attempt_number` on each receipt — how many retries occurred.
3. `RetryPolicy` configuration for `max_attempts` and backoff settings.

**JSON snippet (receipt chain, illustrative):**

```json
[
  {
    "receipt_id": "rcpt-001",
    "event_id": "evt-abc",
    "target_adapter": "radio",
    "status": "failed",
    "failure_kind": "ADAPTER_TRANSIENT",
    "attempt_number": 1,
    "parent_receipt_id": null
  },
  {
    "receipt_id": "rcpt-002",
    "event_id": "evt-abc",
    "target_adapter": "radio",
    "status": "sent",
    "failure_kind": null,
    "attempt_number": 2,
    "parent_receipt_id": "rcpt-001"
  }
]
```

**Caveat:** Transient retries may produce duplicate deliveries on transports
that don't support idempotent sends (Matrix). See
[Bridge Operation > Duplicate Send Realities](bridge-operation.md#5-duplicate-send-realities).

### 6.4 Capacity Exceeded

More concurrent deliveries than `max_inflight_deliveries` are attempted.
The delivery is permanently failed as a backpressure signal.

**Command (test-based):**

```bash
PYTHONPATH=src pytest tests/test_soak_harness.py -v
```

**Expected FAIL:**

- `DeliveryOutcome`: `status == "permanent_failure"`,
  `error == "delivery_capacity_exceeded"`.
- `capacity_rejections` counter in `CapacityController` snapshot growing.
- No retry — capacity timeout is a backpressure signal.

**Inspect next:**

1. `CapacityController.snapshot()` — `delivery_current` vs `delivery_limit`.
2. `capacity_rejections` — sustained growth means the limit is too low.
3. Increase `max_inflight_deliveries` in `[runtime.limits]` if memory allows.
4. Reduce active routes or source event rate.

**Caveat:** Capacity rejection is by design — it prevents unbounded memory
accumulation. It does not mean the routing was wrong. The event was correctly
matched and planned; the failure is at the delivery execution stage.

### 6.5 Deadline Exceeded

A delivery plan's absolute deadline passes before the adapter completes.

**Expected FAIL:**

- `DeliveryReceipt`: `status == "failed"`,
  `failure_kind == "DEADLINE_EXCEEDED"`.
- No retry — the deadline has passed.

**Inspect next:**

1. Check the delivery plan's deadline configuration.
2. Check adapter latency — is it slower than expected?
3. Check for transport-level issues (radio congestion, network latency).


## 7. Shutdown Failure Drills

Shutdown failures occur when the runtime is stopping and deliveries or replays
are still in progress.

### 7.1 Delivery Rejected During Shutdown

In-flight deliveries when shutdown begins are rejected, not drained.

**Expected FAIL:**

- `DeliveryOutcome`: `error == "delivery_rejected_shutdown"`.
- `outbound_failed` counter incremented.

**Inspect next:**

1. `outbound_failed` counter in capacity snapshot.
2. Logs showing which deliveries were in-flight at shutdown time.
3. If these deliveries are important, replay the corresponding events after
   restart.

    **Caveat:** In-flight deliveries cancelled on shutdown are lost. There is no
persistent in-flight recovery. See
[Runtime Operation > Shutdown](runtime-operation.md#shutdown-behavior)
and [Bridge Recovery](bridge-recovery.md) for crash recovery procedures.

### 7.2 Replay Rejected During Shutdown

Replay events in progress when shutdown begins are rejected.

**Expected FAIL:**

- Replay result: `status == "error"`,
  `error == "replay_rejected_shutdown"`.
- `outbound_failed` counter incremented. (Replay rejection tracks the same counter category as delivery rejection.)

**Inspect next:** Re-initiate replay after restart with the same parameters.


## 8. Replay Failure Drills

### 8.1 BEST_EFFORT Duplicate Delivery

BEST_EFFORT replay re-delivers events that match current routes. Each replay
run produces new outbound messages. Replay does not deduplicate.

**Command:**

```bash
PYTHONPATH=src pytest tests/test_replay_pipeline_integration.py -v
```

**Expected behavior (not a failure, but operator must understand it):**

- Multiple `DeliveryReceipt` records with `source == "replay"`.
- Each receipt has a different `replay_run_id`.
- The same event produces separate deliveries for each BEST_EFFORT replay run.

**Inspect next:**

```bash
# After running with SQLite storage:
medre inspect receipts --replay-run <run_id> --config my-bridge.toml
```

Look for `source == "replay"` and `replay_run_id` on each receipt. Count total
outbound messages to understand the duplication scope.

**Caveat:** Replay deduplication is explicitly not provided. Each BEST_EFFORT
replay produces new outbound messages on all matched targets — including radio
transports where duplicates are normal. Use `DRY_RUN` first to preview route
matching without side effects. See [Replay Operation](replay-operation.md) for
the full replay workflow and [Event Tracing](event-tracing.md) for tracing
replay runs.

### 8.2 Replay Capacity Exceeded

More concurrent replay deliveries than `max_inflight_replay_events`.

**Expected FAIL:**

- Replay result: `status == "error"`,
  `error == "replay_capacity_exceeded"`.
- `capacity_rejections` counter growing.

**Inspect next:**

1. `CapacityController.snapshot()` — `replay_current` vs `replay_limit`.
2. Increase `max_inflight_replay_events` in `[runtime.limits]`.
3. Reduce replay batch size.


## 9. Live Health Failure Drills

### 9.1 Degraded Health Refresh

The runtime starts, adapters connect, but some adapters report degraded or
failed health. The `medre diagnostics --refresh-health` command succeeds (exit
0) even when health is not `healthy`.

**Command:**

```bash
PYTHONPATH=src medre diagnostics --refresh-health --config my-bridge.toml
```

**Expected PASS (command succeeds, runtime degraded):**

- Exit code: **0**
- `health.live_health.runtime_health == "degraded"`.
- `health.scope == "live"`.
- `health.live_refresh == true`.

**JSON snippet (illustrative):**

```json
{
  "health": {
    "scope": "live",
    "live_refresh": true,
    "live_health": {
      "poll_count": 1,
      "runtime_health": "degraded",
      "adapters": [
        {
          "adapter_id": "bot",
          "health": "healthy",
          "fake_or_live": "fake",
          "poll_timestamp_wall": "2026-05-14T10:30:00Z",
          "error": null
        },
        {
          "adapter_id": "radio",
          "health": "failed",
          "fake_or_live": "fake",
          "poll_timestamp_wall": "2026-05-14T10:30:00Z",
          "error": "connection refused"
        }
      ]
    }
  },
  "startup": {
    "startup_health": "healthy"
  }
}
```

**Inspect next:**

1. `health.live_health.adapters[].error` for each failing adapter.
2. `health.live_health.adapters[].health` — one of: `healthy`, `degraded`,
   `failed`, `unknown`, `starting`, `stopping`.
3. Verify transport connectivity (network, serial device, credentials).
4. Fix the environment or config, then re-run
   `medre diagnostics --refresh-health`.

**Caveat:** `startup_health` is frozen at startup time. `live_health` reflects
the moment of the refresh. They can differ — an adapter that started
successfully may fail its live health check. No automatic remediation occurs.

### 9.2 Total Health Failure

All adapters report failed health. The command still succeeds (exit 0).

**Expected PASS (command succeeds, health failed):**

- Exit code: **0**
- `health.live_health.runtime_health == "failed"`.
- All adapters report `health == "failed"`.

**Inspect next:**

1. Check all adapter `.error` fields.
2. Check logs for common cause (e.g., network outage, disk full).
3. Verify all transport credentials and connectivity.


## 10. Loop Prevention Drills

### 10.1 Self-Loop Guard

A route tries to deliver an event back to its source adapter.

**Command (test-based):**

```bash
PYTHONPATH=src pytest tests/test_fake_bridge_smoke.py::TestLoopPrevention -v
```

**Expected PASS (loop prevented correctly):**

- `DeliveryOutcome`: `status == "skipped"`,
  `error` contains `"loop_prevented"`.
- Target adapter has zero delivered payloads.
- No `DeliveryReceipt` persisted for the skipped delivery.
- `accounting.loop_prevented >= 1`.
- `RouteStats.snapshot()["route_id"]["loop_prevented"] >= 1`.
- The inbound event IS stored — ingestion succeeded, only delivery prevented.

**Inspect next:**

1. `RouteStats` — `loop_prevented` counter.
2. `accounting.snapshot()["loop_prevented"]`.
3. Event in storage — confirm it was stored despite loop prevention.

**Caveat:** Self-loop guard fires at runtime on every delivery. It catches
cases where `target_adapter == event.source_adapter`. Config-level bidirectional
routes expand into two separate routes, each with correct source/target
orientation, so the self-loop guard typically catches misconfiguration rather
than normal bidirectional operation.

### 10.2 Route-Trace Loop Prevention

An event would be re-routed through a route whose ID already appears in its
route trace.

**Expected PASS:**

- `DeliveryOutcome`: `status == "skipped"`,
  `error == "loop_prevented: route already in route_trace"`.
- The route ID appears in the event's `route_trace` more than once.

**Inspect next:**

1. `RoutingMetadata.route_trace` on the event — bounded to 16 entries.
2. Route configuration — look for overlapping source/dest specs.
3. `check_route_loops` warnings in startup logs.

**Caveat:** Route-trace is bounded to 16 entries. In deeply multi-hop
topologies, entries may be evicted, allowing re-traversal. MEDRE does not
provide cross-instance loop prevention. See
[Routing Correctness > Loop Prevention](routing-correctness.md#2-loop-prevention).


## 11. Incident Workflow Cross-Check

Each drill in this runbook can feed into the incident workflow described in
[Bridge Recovery §0](bridge-recovery.md#0-complete-incident-workflow-end-to-end).
When drills use `--storage-path`, the resulting SQLite database enables
post-drill tracing and evidence collection:

```bash
# Step 1: Run a drill with persistent storage
PYTHONPATH=src medre smoke --drill adapter_transient_failure \
  --storage-path /tmp/medre-drill.db --json

# Step 2: Trace the drill's event through its lifecycle
medre trace event <event_id> --config my-bridge.toml

# Step 3: Inspect receipts for retry chains, failure kinds
medre inspect receipts --event <event_id> --config my-bridge.toml

# Step 4: Collect full evidence for the drill
medre evidence --event <event_id> --config my-bridge.toml --json \
  > drill-evidence.json
```

This cross-check workflow applies to all drills in this runbook:

| Drill | What to trace after | Key receipt fields to check |
|-------|---------------------|-----------------------------|
| `renderer_failure` | Event ingestion, no delivery | `failure_kind == "RENDERER_FAILURE"`, `status == "failed"` |
| `adapter_permanent_failure` | Delivery attempt | `failure_kind == "ADAPTER_PERMANENT"`, no retry chain |
| `adapter_transient_failure` | Retry chain | `attempt_number`, `parent_receipt_id` progression |
| `capacity_rejection` | No receipt (permanent failure) | `capacity_rejections` counter (process-local, lost on restart) |
| `shutdown_rejection` | No receipt (rejected) | `outbound_failed` counter (process-local) |
| `replay_duplicate_risk` | Live vs. replay receipts | `source` field, `replay_run_id` grouping |
| `degraded_live_health` | Health snapshot | `health.live_health.adapters[].health`, `.error` |

**Caveat:** Drill trace data uses fake adapters — it proves pipeline
correctness, not real transport behavior. Traceability is not deduplication.
BEST_EFFORT replay sends real messages. There is no final ACK guarantee for
radio transports. There is no active retry scheduler. Runtime events and
counters are process-local and reset on restart.

See [Bridge Recovery](bridge-recovery.md) for the complete incident workflow,
[Event Tracing](event-tracing.md) for trace command details, and
[Bridge Evidence Bundle](bridge-evidence-bundle.md) for the full evidence
report shape.


## 12. Inspect Follow-Up Quick Reference

| After this failure... | Run this to inspect |
|----------------------|-------------------|
| Config error (exit 2) | `medre config check --config <path>` |
| Config error (drill) | `medre smoke --drill bad_route_config --json` |
| Build failure (exit 3) | `medre diagnostics --config <path>` → `startup.build_failures` |
| Build failure (drill) | `medre smoke --drill all_adapters_build_fail --json` |
| Total startup failure (exit 4) | `medre diagnostics --config <path>` → `startup.boot_summary` |
| Total startup failure (drill) | `medre smoke --drill all_adapters_start_fail --json` |
| Degraded startup | `medre diagnostics --refresh-health` → `health.live_health` |
| Degraded startup (drill) | `medre smoke --drill partial_degraded_startup --json` |
| Renderer failure | `medre inspect receipts --event <id> --config <path>` |
| Adapter permanent | `medre inspect receipts --event <id>` + adapter `diagnostics()` |
| Adapter transient | Full receipt chain via `parent_receipt_id` |
| Capacity exceeded | `capacity_rejections` counter in logs; tune `max_inflight_deliveries` |
| Deadline exceeded | Delivery plan timestamps vs. actual adapter latency |
| Shutdown rejection | `outbound_failed` counter; replay orphaned events after restart |
| Replay duplicate | `medre inspect receipts --replay-run <id> --config <path>` |
| Replay capacity | `capacity_rejections` counter; tune `max_inflight_replay_events` |
| Live health degraded | `medre diagnostics --refresh-health` → per-adapter `.error` |
| Loop prevented | `RouteStats` → `loop_prevented`; `accounting.snapshot()` |

For persistent inspection, use ``[storage] backend = "sqlite"`` and query:

```bash
# All failed deliveries for a route
medre inspect receipts --event <event_id> --config my-bridge.toml

# Orphaned events (stored but never delivered) — use SQL:
# SELECT e.event_id FROM canonical_events e
# LEFT JOIN delivery_receipts r ON e.event_id = r.event_id
# WHERE r.event_id IS NULL;
```


## 13. Caveats

1. **No live-network claims.** These drills use fake adapters and in-memory
   storage unless explicitly noted. They prove pipeline correctness, not real
   transport behavior.

2. **In-memory storage is ephemeral.** ``medre smoke`` and most test fixtures
   use in-memory storage. Evidence vanishes on process exit. For durable
   inspection, use ``backend = "sqlite"`` and ``medre inspect``.

3. **``medre inspect`` requires SQLite.** The inspect subcommands open an
   existing SQLite database in read-only mode. They exit with code 2 if the
   config uses ``backend = "memory"`` or the database file does not exist.

 4. **Counters reset on restart.** ``capacity_rejections``, ``outbound_failed``,
    ``RouteStats``, and ``CapacityController`` gauges are process-local. They
   reset to zero on every startup.

5. **No automated remediation.** MEDRE does not restart adapters, routes, or
   the runtime based on failure state. All remediation is operator-initiated.

6. **No per-adapter restart.** Only full runtime stop/start is supported.

7. **No distributed coordination.** Failure state, receipts, and loop
   prevention are local to the process. Cross-instance loops are not detected.

8. **Replay does not deduplicate.** Multiple BEST_EFFORT replays of the same
   event produce additional outbound messages each time.

9. **Radio transports are probabilistic.** With real adapters, ``sent`` does not
   mean delivered. This runbook's fake-adapter drills do not exercise radio
   unreliability. See [Bridge Operation > Per-Transport Delivery
   Semantics](bridge-operation.md#2-per-transport-delivery-semantics).

10. **Smoke and diagnostics are one-shot.** ``medre smoke`` and
    ``medre diagnostics --refresh-health`` start and stop the runtime. They do
    not provide ongoing monitoring.

     11. **Pre-beta.** Exit codes, receipt schemas, and diagnostic shapes may change
      before beta. Always verify against the current code.

    12. **Evidence bundle workflow.** For a structured approach to collecting
     smoke output, drill reports, and inspect results as a pre-runtime
     evidence package, see [Bridge Evidence Bundle](bridge-evidence-bundle.md).

    13. **Recovery and tracing.** For crash recovery procedures and orphan
    detection, see [Bridge Recovery](bridge-recovery.md). For event tracing
    through the pipeline lifecycle, see [Event Tracing](event-tracing.md).
    For the full replay workflow, see [Replay Operation](replay-operation.md).
