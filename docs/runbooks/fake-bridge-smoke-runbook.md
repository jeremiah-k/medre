# Fake Bridge Smoke Runbook

> Last updated: 2026-05-14
> Scope: Proving cross-adapter bridge behavior with fake adapters
> Status: Pre-beta. Fake bridge proven; Docker SDK-boundary proven; Docker SDK-boundary bridge smoke proven (Matrix → fake via real SDK); live bridge not claimed.

This runbook describes how to prove that the MEDRE runtime correctly bridges
events between adapters using fake adapters and in-memory storage. It covers
what each test proves, how to run the tests, and what the results mean.


## 0. Provenance Summary

| Tier | Status | What is proven |
|------|--------|---------------|
| **Fake bridge** | Proven | Full pipeline routing with fake adapters (this runbook) |
| **Adapter-wrapper** | Proven | Per-transport adapter internals with mocked transport |
| **Docker SDK-boundary** | Proven | Real SDK code paths against containerized Synapse/meshtasticd (see `integration-testing.md`) |
| **Docker SDK-boundary bridge smoke** | Proven | Real Matrix SDK codec + pipeline routing + storage + accounting with genuine Synapse event_ids (see `integration-testing.md`) |
| **Live network** | **Not claimed** | No cross-transport bridge test against real endpoints has been executed |

Fake bridge and Docker SDK-boundary are complementary. Fake bridge proves the
pipeline routing logic is correct. Docker SDK-boundary proves the real SDK
boundary works (config loading, dependency resolution, adapter lifecycle).
Docker SDK-boundary bridge smoke proves the real SDK's inbound path integrates
with the pipeline, storage, and accounting — but routes to a fake outbound
target. None proves live network behavior.


## 1. What "Fake Bridge Proven" Means

A fake bridge test exercises the **full runtime pipeline** without network or
hardware dependencies:

```
FakeAdapter.simulate_inbound(event)
  -> PipelineRunner.handle_ingress
    -> validate -> resolve_relations -> store -> route -> plan -> deliver
    -> DeliveryReceipt persisted -> NativeMessageRef persisted
    -> RuntimeAccounting incremented -> RouteStats updated
```

The fake adapters use the same `AdapterContext.publish_inbound` path as real
adapters. The pipeline runs the same routing, rendering, delivery, and receipt
code. The only difference is the transport layer is simulated.

**What this proves:**
- The runtime pipeline correctly routes events between adapters.
- DeliveryReceipts are persisted for every outbound delivery attempt.
- NativeMessageRefs are persisted when adapters return native IDs.
- RuntimeAccounting counters reflect actual flow.
- RouteStats track per-route delivery counts.
- Loop prevention guards fire correctly.
- Reply relations survive the full pipeline.

**What this does NOT prove:**
- Real transport connectivity (no network involved).
- Real adapter codec correctness for live packet formats.
- Real adapter session lifecycle (reconnection, retry against live endpoints).
- Delivery confirmation beyond local adapter acceptance.


## 2. Running the Tests

```bash
# Full fake bridge test suite (no network, no hardware)
PYTHONPATH=src pytest tests/test_fake_bridge_smoke.py -v

# Specific test class
PYTHONPATH=src pytest tests/test_fake_bridge_smoke.py::TestMatrixToMeshtastic -v

# With the existing runtime smoke tests for comparison
PYTHONPATH=src pytest tests/test_fake_runtime_smoke.py tests/test_fake_bridge_smoke.py -v
```

All tests should pass in under 30 seconds total.


## 3. Test Coverage Matrix

| Test Class | Flow | Key Assertions |
|------------|------|----------------|
| `TestMatrixToMeshtastic` | Matrix -> Meshtastic | Event stored, receipt sent, native ref, accounting, route stats, no duplicate |
| `TestMeshtasticToMatrix` | Meshtastic -> Matrix | Event stored, receipt sent, inbound native ref, outbound native ref, accounting |
| `TestBidirectionalBridge` | Matrix <-> Meshtastic | Both directions deliver, no cross-contamination, two receipts |
| `TestFanoutDelivery` | Matrix -> Meshtastic + MeshCore | Both targets receive delivery, two receipts, two native refs, error isolation |
| `TestLoopPrevention` | Self-loop | Delivery skipped, loop_prevented counter incremented, no receipt |
| `TestReplyRelationPreservation` | Reply event bridge | Relations preserved in storage, fallback text rendered correctly |
| `TestRenderingContract` | Various | RenderingResult shape, empty payload handling, unsupported kind = failure, truncation |
| `TestSnapshotReflectsBridgeFlow` | After delivery | Accounting counters, route stats, JSON-safe snapshot |
| `TestRouteConfigThroughRuntime` | Config -> Routes | Config route registers, bidirectional expands, policy filters, disabled skipped |


## 4. Step-by-Step: Proving a New Bridge Flow

To add a new fake bridge test:

1. **Build the config** using `RuntimeConfig` with fake adapters:
   ```python
   config = RuntimeConfig(
       runtime=RuntimeOptions(name="test-name"),
       storage=StorageConfig(backend="memory"),
       adapters=AdapterConfigSet(
           matrix={"mx": MatrixRuntimeConfig(adapter_id="mx", adapter_kind="fake")},
           meshtastic={"mesh": MeshtasticRuntimeConfig(adapter_id="mesh", adapter_kind="fake")},
       ),
   )
   ```

2. **Register routes** either via `RouteConfigSet` (config-based) or `app.router.add_route()` (manual).

3. **Build and start** the runtime:
   ```python
   app = await _build_and_start(config, tmp_paths)
   ```

4. **Inject an inbound event** via `adapter.simulate_inbound(event)`.

5. **Assert the full chain**:
   - Event stored: `await storage.get(event_id)`
   - Receipt persisted: `await storage.list_receipts_for_event(event_id)`
   - Native ref: `await storage.resolve_native_ref(adapter, channel, native_id)`
   - Target received delivery: `target_adapter.delivered_payloads`
   - Accounting: `app._runtime_accounting.snapshot()`
   - Route stats: `app.route_stats.snapshot()`

6. **Clean stop**: `await _clean_stop(app)`


## 5. Rendering Note

The `TextRenderer` reads `event.payload.get("text", "")`. Fake adapters store
body text under the `"body"` key by default. To get non-empty rendered output,
include a `"text"` key in the event payload:

```python
event = adapter.make_event("hello", text="hello")  # extra_payload: text="hello"
```

This is a known gap between the fake adapter convention (`"body"`) and the
renderer expectation (`"text"`). The rendering contract tests document this
behavior honestly.


## 6. Loop Prevention: What Is Proven

The pipeline has two loop prevention mechanisms:

1. **Self-loop guard**: Skips delivery when `target_adapter == event.source_adapter`.
   Tested in `TestLoopPrevention::test_self_loop_guard_skips_delivery`.

2. **Route-trace guard**: Skips delivery when a route ID appears more than once
   in `event.metadata.routing.route_trace`. This prevents re-traversal after a
   round-trip. Config-level validation (`RouteConfig`) rejects overlapping
   source/dest adapters, so the self-loop guard is the primary runtime
   mechanism for fake bridges.

What is NOT tested at the integration level: multi-hop cycles (A -> B -> C -> A)
through the runtime. The route engine's `check_route_loops()` detects these
statically at startup, but exercising them through the full pipeline requires
three or more adapters with cyclic routes.


## 7. Commands Reference

### Fake bridge tests (no network, no Docker, no SDKs)

```bash
# Full fake bridge test suite
PYTHONPATH=src pytest tests/test_fake_bridge_smoke.py -v

# Specific bridge flow
PYTHONPATH=src pytest tests/test_fake_bridge_smoke.py::TestMatrixToMeshtastic -v

# Replay pipeline integration (proves replay with real PipelineRunner)
PYTHONPATH=src pytest tests/test_replay_pipeline_integration.py -v

# Runtime snapshot (proves JSON safety, provenance, health fields)
PYTHONPATH=src pytest tests/test_runtime_snapshot.py -v

# Example config validation (proves all shipped configs parse/build)
PYTHONPATH=src pytest tests/test_example_configs.py -v
```

### Config validation (no runtime start)

```bash
# Validate config without starting
PYTHONPATH=src medre config check --config examples/configs/fake-bridge-smoke.toml

# Validate routes
PYTHONPATH=src medre routes validate --config examples/configs/fake-bridge-smoke.toml
```

### Diagnostics (no runtime start)

```bash
# Build-time snapshot (no adapter start, no I/O)
PYTHONPATH=src medre diagnostics --config examples/configs/fake-bridge-smoke.toml

# Live health refresh (starts adapters, polls health, stops)
PYTHONPATH=src medre diagnostics --refresh-health --config examples/configs/fake-multi-adapter.toml
```

### Docker SDK-boundary tests

```bash
# Prerequisites: Docker daemon running, SDK extras installed
pip install -e ".[matrix,meshtastic,dev]"

# All Docker integration tests
PYTHONPATH=src pytest tests/integration/ -m docker -v

# Matrix (Synapse) only
PYTHONPATH=src pytest tests/integration/test_synapse_connectivity.py -m docker -v

# Meshtastic (meshtasticd) only
PYTHONPATH=src pytest tests/integration/test_meshtasticd_connectivity.py -m docker -v

# Synapse bridge smoke (full pipeline: real Matrix SDK → PipelineRunner → FakeMatrixAdapter)
PYTHONPATH=src pytest tests/integration/test_synapse_bridge_smoke.py -m docker -v
```

Docker tests are **excluded from default runs** via `addopts = "-m 'not live and not docker'"`
in `pyproject.toml`. They are collected and skipped unless explicitly enabled.

### Skip behavior

```bash
# Default: Docker tests collected but not run
PYTHONPATH=src pytest -q
# Expected: all non-Docker tests pass, Docker tests shown as skipped/deselected

# Explicitly skip Docker (redundant with default but explicit)
MEDRE_SKIP_DOCKER=1 pytest tests/integration/ -v

# Run everything including Docker + live
pytest -m "" -v
```

### Failure interpretation

| Symptom | Likely cause | Action |
|---------|-------------|--------|
| Docker tests skip with "Docker not available" | Docker daemon not running | Start Docker: `docker info` |
| Docker tests skip with "mtjk not installed" | Meshtastic SDK not installed | `pip install -e ".[meshtastic]"` |
| Docker tests skip with "mindroom-nio not installed" | Matrix SDK not installed | `pip install -e ".[matrix]"` |
| Config validation exits 2 | TOML syntax or credential error | `medre config check --config <path>` |
| Routes validate exits 2 | Unknown adapter ref in route | Check adapter IDs in routes match adapters section |

### ResourceWarning (optional, CI hardening)

```bash
# Enable ResourceWarning as error to catch unclosed resources:
PYTHONPATH=src pytest -W error::ResourceWarning -q
```


## 7.5 Operator Smoke Command

The ``medre smoke`` command provides a single-command Docker-free bridge
validation.  It loads a config (default: ``examples/configs/fake-bridge-smoke.toml``),
builds and starts the runtime with fake adapters, injects one ``message.text``
event through the full pipeline, inspects every evidence surface, stops
cleanly, and prints a compact PASS/FAIL report.

```bash
# Default: uses shipped fake-bridge-smoke.toml
PYTHONPATH=src medre smoke

# JSON report (machine-readable)
PYTHONPATH=src medre smoke --json

# Explicit config
PYTHONPATH=src medre smoke --config examples/configs/fake-bridge-smoke.toml

# Custom message text
PYTHONPATH=src medre smoke --message "operator check $(date -Iseconds)"

# Exit codes: 0 = PASS, 1 = FAIL
PYTHONPATH=src medre smoke --json; echo "exit: $?"
```

**Report fields:**

| Field | Type | Description |
|-------|------|-------------|
| ``status`` | ``PASS`` / ``FAIL`` | Overall result |
| ``evidence_level`` | ``fake_bridge`` | What the report proves |
| ``source_adapter`` | str | Adapter that sourced the test event |
| ``target_adapters`` | list[str] | Adapters that received delivery |
| ``event_id`` | str | Canonical event ID |
| ``route_ids`` | list[str] | Routes that matched the event |
| ``delivery_receipts`` | list[dict] | Per-target receipt summaries |
| ``native_refs`` | list[dict] | Native message ref mappings |
| ``accounting`` | dict | RuntimeAccounting counters |
| ``route_stats`` | dict | Per-route delivery statistics |
| ``snapshot`` | dict | Abbreviated runtime snapshot |
| ``limitations`` | list[str] | What this does NOT prove |
| ``preflight`` | dict | Config/route validation summary |

**PASS criteria (all must be true):**

1. Event stored in storage (``storage.get(event_id)`` returns non-None).
2. At least one ``DeliveryOutcome`` with ``status == "success"``.
3. At least one ``DeliveryReceipt`` with ``status == "sent"``.
4. ``accounting.outbound_delivered >= 1``.

**What this proves:**

- The runtime pipeline correctly routes events between adapters.
- DeliveryReceipts are persisted for every outbound delivery attempt.
- NativeMessageRefs are persisted when adapters return native IDs.
- RuntimeAccounting counters reflect actual flow.
- RouteStats track per-route delivery counts.
- The full snapshot is JSON-safe.

**What this does NOT prove:**

- Real transport connectivity (no network involved).
- Real adapter codec correctness for live packet formats.
- Delivery confirmation beyond local adapter acceptance.
- Persistence or crash recovery (in-memory storage). The JSON report is the
  only surviving artifact. ``medre inspect`` requires SQLite storage; use
  ``medre run`` with ``[storage] backend = "sqlite"`` for durable inspection.

The underlying function ``run_fake_bridge_smoke()`` in
``medre.runtime.smoke`` can also be called programmatically:

```python
from medre.runtime.smoke import run_fake_bridge_smoke
report = await run_fake_bridge_smoke("path/to/config.toml")
assert report["status"] == "PASS"
```

**Note:** ``run_fake_bridge_smoke()`` also uses in-memory storage by default.
The returned report dict is the only surviving artifact. For durable inspection
of stored evidence, run the full runtime with ``[storage] backend = "sqlite"``
instead.


### Smoke persistence caveat

``medre smoke`` uses in-memory storage by default. When the smoke process exits,
all stored evidence — events, receipts, native refs, accounting counters — is
released. The JSON report (or human-readable output) printed to stdout is the
only surviving record.

Pass ``--storage-path <path>`` to persist evidence to a SQLite database instead.
When ``--storage-path`` is supplied, events, receipts, and native refs are
written to the specified database file and can be inspected with ``medre inspect``
after the process exits.

``medre inspect`` subcommands require persistent storage. Running
``medre inspect`` against a config with ``[storage] backend = "memory"``
produces:

```
Error: storage backend is 'memory' — no persistent data to inspect.
```

To inspect stored evidence after a run, use ``medre run`` with SQLite storage:

```toml
[storage]
backend = "sqlite"
```

```bash
# Run with persistent storage
PYTHONPATH=src medre run --config my-bridge.toml

# After runtime exits (or in a separate terminal), inspect stored evidence:
PYTHONPATH=src medre inspect event <event_id> --config my-bridge.toml
PYTHONPATH=src medre inspect receipts --event <event_id> --config my-bridge.toml
PYTHONPATH=src medre inspect native-ref --adapter <name> --message <native_id> --config my-bridge.toml
PYTHONPATH=src medre inspect receipts --replay-run <run_id> --config my-bridge.toml
```

All ``medre inspect`` commands are read-only. They open the SQLite database,
query the requested data, print it, and close. They do not modify state.

See [Runtime Operation > Persistence](runtime-operation.md#persistence-and-crash-semantics)
for what survives process termination, and
[Bridge Operation > Persistence of Bridge State](bridge-operation.md#12-persistence-of-bridge-state)
for bridge-specific persistence details.


### Evidence bundle workflow

For a structured approach to collecting smoke output, drill reports, and
inspect results as a single pre-runtime evidence package, use
`medre evidence`:

```bash
# Basic bundle: smoke + all drills
PYTHONPATH=src medre evidence --config my-bridge.toml --json > bundle.json

# Include live health refresh
PYTHONPATH=src medre evidence --config my-bridge.toml --include-refresh-health --json > bundle-health.json

# Target a specific stored event
PYTHONPATH=src medre evidence --config my-bridge.toml --event <event_id> --json > bundle-event.json
```

See [Bridge Evidence Bundle](bridge-evidence-bundle.md) for the full collection
workflow, report shapes, and bug report attachment guidance.

### Expected fake bridge PASS output

```bash
PYTHONPATH=src pytest tests/test_fake_bridge_smoke.py -v
# Expected: 30+ passed in under 30 seconds
# Key test classes that must PASS:
#   TestMatrixToMeshtastic, TestMeshtasticToMatrix,
#   TestBidirectionalBridge, TestFanoutDelivery,
#   TestLoopPrevention, TestReplyRelationPreservation,
#   TestRenderingContract, TestSnapshotReflectsBridgeFlow,
#   TestRouteConfigThroughRuntime
```

### Expected Docker bridge smoke PASS output

```bash
PYTHONPATH=src pytest tests/integration/test_synapse_bridge_smoke.py -m docker -v
# Expected: 3 passed
# Key assertions in test_inbound_via_sync_routes_to_fake_adapter:
#   - native_message_id starts with "$" (real Synapse event_id)
#   - fake_out.delivered_payloads >= 1 (pipeline routed successfully)
#   - receipt status == "sent" (DeliveryReceipt persisted)
#   - inbound NativeMessageRef maps real event_id to canonical ID
#   - RuntimeAccounting inbound_accepted >= 1, outbound_delivered >= 1
#   - No aiohttp ResourceWarnings after shutdown
```

### Inspecting stored evidence after fake bridge

```python
# After a fake bridge test with in-memory or SQLite storage:
# Event stored
event = await storage.get(event_id)
assert event.source_adapter == "expected-source"

# DeliveryReceipt
receipts = await storage.list_receipts_for_event(event_id)
assert receipts[0].status == "sent"

# NativeMessageRef (when adapter returns native_message_id)
ref = await storage.resolve_native_ref(adapter, channel, native_id)
assert ref is not None

# RuntimeAccounting
counters = app._runtime_accounting.snapshot()
assert counters["inbound_accepted"] >= 1

# RouteStats
stats = app.route_stats.snapshot()
assert stats["per_route"]["route-id"]["delivered"] >= 1
```


## 8. What Remains Unproven

| Capability | Status | Notes |
|-----------|--------|-------|
| Live external Matrix (beyond Docker localhost) | Not proven | Docker tests use loopback Synapse only |
| Real radio hardware (Meshtastic/MeshCore/LXMF) | Not proven | No live hardware smoke test recorded |
| Final delivery ACK / remote receipt | Not proven | Radio is fire-and-forget; Matrix is server-level only |
| Replay deduplication | Not proven | Replay produces duplicates by design. See [Replay Operation](replay-operation.md). |
| Active restart / supervision | Not proven | No per-adapter restart, no auto-remediation. See [Bridge Recovery](bridge-recovery.md). |
| Background health polling | Not proven | Manual `--refresh-health` only; no scheduler |
| Sustained throughput | Not proven | All tests are smoke tests, not load tests |
| Network resilience / reconnection | Not proven | No live failure/reconnect test |
| Cross-instance loop prevention | Not proven | Loop prevention is local-process only |
| Third-party Matrix inbound | Not proven | Bridge smoke uses HTTP API sender, not a second Matrix client |
| Full cross-transport relay | Not proven | Bridge smoke routes real Matrix to fake outbound, not to a second real adapter |

For event tracing after test runs, see
[Event Tracing](event-tracing.md). For replay workflows, see
[Replay Operation](replay-operation.md). For crash recovery procedures, see
[Bridge Recovery](bridge-recovery.md).
