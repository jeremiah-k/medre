# Fake Bridge Smoke Runbook

> Last updated: 2026-05-14
> Scope: Proving cross-adapter bridge behavior with fake adapters
> Status: Pre-beta. Fake bridge proven; live bridge not yet validated.

This runbook describes how to prove that the MEDRE runtime correctly bridges
events between adapters using fake adapters and in-memory storage. It covers
what each test proves, how to run the tests, and what the results mean.


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
