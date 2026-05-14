# Fake Bridge Evidence Criteria

> Last updated: 2026-05-14
> Scope: Defining what assertions constitute proof of correct bridge behavior
> Status: Pre-beta. Criteria defined for fake bridge and Docker SDK-boundary levels.

This document defines evidence-level criteria for each bridge flow type. Each
criterion specifies what must be asserted to consider a flow "proven" at a given
level of fidelity.


## Provenance Levels

Bridge behavior can be proven at four levels of fidelity. Each level adds
constraints and reduces the gap between test and production:

| Level | Environment | Transport | What it proves |
|-------|-------------|-----------|----------------|
| **Fake bridge** | In-memory, fake adapters | Simulated | Pipeline routing, rendering, receipts, accounting, loop prevention |
| **Adapter-wrapper** | Unit test, real adapter code | Mocked transport | Adapter codec, renderer, session logic |
| **Docker SDK-boundary** | Container, real deps | Loopback | Dependency resolution, config loading, adapter lifecycle, real SDK boundary, pipeline routing through real adapters |
| **Live network** | Real endpoints | Real transport | Actual connectivity, protocol compliance |

This document covers **fake bridge** level criteria. Docker SDK-boundary criteria
are documented in `docs/runbooks/integration-testing.md`. Adapter-wrapper and
live-network criteria are tracked separately per transport.

### Honest Provenance Claims

| Tier | Status | What can be claimed |
|------|--------|-------------------|
| Fake bridge | **Proven** | Pipeline routing, rendering, receipts, accounting, loop prevention all work with fake adapters |
| Adapter-wrapper | **Proven** | Per-transport adapter codec, renderer, session logic work with mocked transport |
| Docker SDK-boundary | **Proven** | Real SDK code paths exercise against containerized Synapse/meshtasticd |
| Live network | **Not claimed** | No live cross-transport bridge test has been executed against real endpoints |


## Unidirectional Bridge (A -> B)

**Required assertions:**

1. **Inbound event persisted**: `storage.get(event_id)` returns the canonical
   event with correct `source_adapter`, `event_kind`, and payload.

2. **Route selected**: The pipeline outcome has `route_id` matching the
   configured route. `route_stats.snapshot()` shows `delivered >= 1`.

3. **Rendered outbound payload**: Target adapter's `delivered_payloads`
   contains a `RenderingResult` with `target_adapter` matching the target.

4. **DeliveryReceipt persisted**: `storage.list_receipts_for_event(event_id)`
   returns at least one receipt with `status == "sent"`, `target_adapter`
   matching, `route_id` matching, `source == "live"`.

5. **NativeMessageRef persisted**: `storage.resolve_native_ref(adapter,
   channel, native_id)` returns the canonical `event_id`. Only when the
   adapter returns a `native_message_id`.

6. **Runtime accounting**: `accounting.snapshot()` shows `inbound_accepted == 1`,
   `outbound_attempts >= 1`, `outbound_delivered >= 1`.

7. **No duplicate delivery**: `len(target_adapter.delivered_payloads) == 1`
   exactly.

**Optional but recommended:**

- Snapshot JSON-safe via `json.dumps(build_runtime_snapshot(app))`.
- RouteStats per-route counters match delivery count.


## Bidirectional Bridge (A <-> B)

**Required assertions** (in addition to unidirectional criteria for each
direction):

1. **Config expansion**: Bidirectional `RouteConfig` produces exactly two
   registered `Route` objects (forward + reverse).

2. **Both directions deliver**: Events from A deliver to B, and events from
   B deliver to A, without cross-contamination.

3. **Independent receipts**: Each direction produces its own receipt with the
   correct `target_adapter`.

4. **Accounting reflects both**: `inbound_accepted == 2`, `outbound_delivered == 2`
   after one event in each direction.


## Fanout (A -> B, C)

**Required assertions:**

1. **All targets receive**: Each target adapter has `len(delivered_payloads) == 1`.

2. **Multiple receipts**: `storage.list_receipts_for_event(event_id)` returns
   one receipt per target, each with correct `target_adapter`.

3. **Multiple native refs**: One `NativeMessageRef` per target that returns
   a native ID.

4. **Accounting**: `outbound_attempts == N`, `outbound_delivered == N` where
   N is the number of targets.

5. **Error isolation**: When one target fails, other targets still receive
   delivery. `outbound_delivered == N-1`, `outbound_failed == 1`.


## Loop Prevention

**Required assertions:**

1. **Self-loop guard**: When `target_adapter == source_adapter`, the pipeline
   returns `outcome.status == "skipped"` with error containing "loop_prevented".

2. **No delivery**: The target adapter has zero delivered payloads.

3. **No receipt**: No `DeliveryReceipt` is persisted for the skipped delivery.

4. **Accounting**: `loop_prevented >= 1`.

5. **RouteStats**: `route_stats.snapshot()["route_id"]["loop_prevented"] >= 1`.

6. **Event still stored**: The inbound event is persisted even though delivery
   was skipped -- ingestion succeeded, only delivery was prevented.


## Reply Relation Preservation

**Required assertions:**

1. **Relations stored**: The stored canonical event has `relations` tuple with
   the correct `relation_type` ("reply") and `target_event_id`.

2. **Rendering includes context**: The `RenderingResult` payload includes reply
   context (e.g., `[replying to: ...]` prefix when `fallback_text` is set).

3. **Bridge delivers**: The reply event is successfully delivered to the
   target adapter as with any other event.


## Rendering Contract

**Required assertions:**

1. **Deterministic shape**: `RenderingResult` has `event_id`, `target_adapter`,
   `payload` (dict with "text" key), `metadata` (dict with "renderer" and
   "original_length"), and `truncated` (bool).

2. **Empty payload safe**: Event with no renderable content produces empty
   text, not a delivery error.

3. **Unsupported kind fails as renderer failure**: Event kind not handled by
   any renderer produces `RENDERER_FAILURE`, not `ADAPTER_*` failure.

4. **Truncation**: Text exceeding 500 characters is truncated with
   `truncated=True` and metadata includes `original_length`.


## Config Route Validation

**Required assertions:**

1. **Parse**: `RouteConfigSet.from_toml_dict()` parses route TOML without error.

2. **Register**: `RuntimeBuilder.build()` registers routes on the Router.

3. **Validate**: Unknown adapter references raise `RouteValidationError`.

4. **Disable**: Routes with `enabled=false` are skipped during registration.

5. **Policy filter**: `allowed_event_types` maps to `RouteSource.event_kinds`
   and filters events correctly.

6. **Duplicate rejection**: `RouteConfigSet` rejects duplicate route IDs.
