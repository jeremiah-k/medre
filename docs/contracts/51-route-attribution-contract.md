# Contract 51 — Route Attribution Contract

**Status:** Active
**Scope:** Authoritative specification for how route attribution is attached, stored, and propagated through the MEDRE pipeline — covering live delivery, delivery receipts, and replay.
**Audience:** Runtime builders, adapter authors, replay engine authors, test harnesses, operators.
**References:** Contract 49 (Routing and Bridge), Contract 50 (Runtime Topology), Contract 31 (Session Boundary).

Every agent or document that references route attribution, route trace metadata, delivery receipt attribution, or replay route attribution must defer to this contract.

## 1. Attribution Fields

Every routed delivery carries the following attribution data:

| Field            | Type            | Required | Description                                                                   |
| ---------------- | --------------- | -------- | ----------------------------------------------------------------------------- |
| `route_id`       | `str`           | Yes      | Unique identifier of the route that matched.                                  |
| `source_adapter` | `str`           | Yes      | Adapter ID of the event's origin. Drawn from `CanonicalEvent.source_adapter`. |
| `dest_adapter`   | `str`           | Yes      | Adapter ID of the delivery target.                                            |
| `policy_id`      | `str` or `None` | No       | Bridge policy identifier, if a policy is attached to the route.               |
| `event_id`       | `str`           | Yes      | Original canonical event ID (`CanonicalEvent.event_id`).                      |
| `replay_run_id`  | `str` or `None` | No       | Replay run identifier, present only during replay attribution.                |

## 2. Where Attribution Lives

Route attribution is stored in three locations, each serving a different lifecycle stage:

### 2.1 `RoutingMetadata.route_trace` (on CanonicalEvent metadata)

After route matching, the pipeline populates `RoutingMetadata` on the event with both matched route IDs and a bounded historical route trace:

```text
RoutingMetadata(
    matched_routes=("route_a", "route_b"),   # routes matched during this pass
    route_trace=("route_a", "route_b"),      # bounded historical traversal
)
```

- `matched_routes` records which routes matched during the **current** routing pass.
- `route_trace` is the **bounded historical** traversal trace (capped at 16 entries). It appends `matched_routes` to any prior trace from earlier routing passes (e.g. during replay).

This is the live-routing attribution path. The pipeline uses `msgspec.structs.replace` to create a new `CanonicalEvent` with the updated metadata. The original event is never mutated in place.

**Immutability guarantee:** `CanonicalEvent` is `frozen=True`. Attribution is added by constructing a replacement event with an updated `RoutingMetadata` namespace. The event schema itself is never altered.

### 2.2 `DeliveryReceipt.route_id` (on persisted receipts)

Every `DeliveryReceipt` carries a `route_id` field. This persists the route attribution into storage via SQLite alongside the delivery outcome:

```text
DeliveryReceipt(
    event_id="evt_001",
    target_adapter="longfast",
    route_id="matrix_to_radio",
    status="sent",
    ...
)
```

Receipts form the durable audit trail. The `route_id` on a receipt is the authoritative source for "which route was this delivery attributed to" in any post-hoc analysis.

**Note on storage timing:** The source canonical event is persisted _before_ routing metadata is populated. Therefore the stored canonical event record may not carry `route_trace`. Delivery receipts are the definitive persisted attribution record.

### 2.3 `ReplayRouteAttribution` (on replay results)

During replay, route attribution is captured in `ReplayRouteAttribution` and stored on `ReplayResult.route_attribution`. This preserves replay-specific metadata (replay mode, loop warnings) without altering the canonical event schema:

```text
ReplayRouteAttribution(
    route_ids=("matrix_to_radio",),
    source_adapter="bot1",
    target_adapters=("longfast",),
    replay_mode="RE_ROUTE",
    is_replay=True,
    loop_warnings=(),
)
```

Replay attribution is ephemeral — it exists on the `ReplayResult` and is not written back to stored events.

## 3. CanonicalEvent Is Never Mutated for Attribution

This is an architectural invariant. `CanonicalEvent` is a frozen msgspec struct. Route attribution is added exclusively through:

1. **Metadata namespace replacement:** `RoutingMetadata.route_trace` is set via `msgspec.structs.replace`, producing a new `CanonicalEvent` with the attribution embedded in metadata.
2. **Result wrappers:** `DeliveryReceipt` and `DeliveryOutcome` carry `route_id` independently of the event.
3. **Replay wrappers:** `ReplayRouteAttribution` carries attribution on the replay result, not on the stored event.

No code path may set an attribution field directly on a `CanonicalEvent` instance after construction.

## 4. Deterministic Attribution Guarantee

For the same canonical event and the same route configuration, route attribution is identical across runs. This holds for both live routing and replay.

Determinism is ensured by:

- Route registration order matches `RouteConfigSet` order (TOML declaration order).
- Route matching is performed by the in-memory `Router.match()` which iterates registered routes in registration order.
- `route_trace` preserves the order routes were matched.
- Replay attribution uses the same `Router.match()` path as live routing.

## 5. Attribution Examples

### 5.1 Simple Route (One-to-One)

```json
[routes.matrix_to_radio]
source_adapters = ["bot1"]
dest_adapters = ["longfast"]
```

Event from `bot1` matches `matrix_to_radio`:

| Location                      | Attribution            |
| ----------------------------- | ---------------------- |
| `RoutingMetadata.route_trace` | `("matrix_to_radio",)` |
| `DeliveryReceipt.route_id`    | `"matrix_to_radio"`    |
| `DeliveryOutcome.route_id`    | `"matrix_to_radio"`    |

### 5.2 One-to-Many Route (Fan-Out)

```json
[routes.hub]
source_adapters = ["bot1"]
dest_adapters = ["radio_a", "radio_b"]
```

Event from `bot1` matches `hub`, producing two delivery targets:

| Target    | `DeliveryReceipt.route_id` | `DeliveryOutcome.route_id` |
| --------- | -------------------------- | -------------------------- |
| `radio_a` | `"hub"`                    | `"hub"`                    |
| `radio_b` | `"hub"`                    | `"hub"`                    |

`RoutingMetadata.route_trace` = `("hub",)` — one entry because one route matched.

Each target gets an independent receipt and outcome. A failure on `radio_b` does not affect the receipt for `radio_a`.

### 5.3 Replay Route

Replaying a historical event through `RE_ROUTE` mode:

| Location                                 | Attribution            |
| ---------------------------------------- | ---------------------- |
| `ReplayRouteAttribution.route_ids`       | `("matrix_to_radio",)` |
| `ReplayRouteAttribution.source_adapter`  | `"bot1"`               |
| `ReplayRouteAttribution.target_adapters` | `("longfast",)`        |
| `ReplayRouteAttribution.is_replay`       | `True`                 |
| `ReplayRouteAttribution.replay_mode`     | `"RE_ROUTE"`           |

The stored `CanonicalEvent` is not modified. Attribution lives on the `ReplayResult`.

## 6. Attribution Boundary Rules

### 6.1 Adapters Do Not See Attribution Metadata

Adapters receive `CanonicalEvent` and `DeliveryPlan` for delivery. They do not consume `RoutingMetadata.route_trace`, `DeliveryReceipt.route_id`, or `ReplayRouteAttribution`. Attribution is orchestration-layer metadata, not adapter-facing data.

### 6.2 RouteStats Does Not Import Adapters

`RouteStats` and `RouteCounters` (in `medre.core.routing.stats`) record per-route delivery counters. They do not import SDKs, concrete adapter packages, or adapter modules. They accept `route_id: str` and `error: str` — plain strings only.

### 6.3 Attribution Does Not Cross Process Boundaries

Route attribution is local to the MEDRE process. It is not propagated to external systems, radio packets, or Matrix rooms. Attribution is an internal observability and audit mechanism.

## 7. Explicit Non-Guarantees

### 7.1 No Exactly-Once Delivery

MEDRE explicitly does **not** provide exactly-once delivery semantics. Replay modes (especially `BEST_EFFORT`) may intentionally re-deliver events that were previously delivered. The `route_trace` mechanism prevents re-routing through the **same** route within a bounded window (16 entries), but it does not prevent the same event from producing multiple deliveries to the same adapter through different routes or different replay invocations.

### 7.2 Replay May Intentionally Redeliver

`BEST_EFFORT` replay exists to re-deliver historical events. This is intentional. Use `DRY_RUN` or `RE_ROUTE` to preview matching without side effects.

### 7.3 `DeliveryReceipt.route_id` Persists Through Replay

When replay produces a new delivery (in `BEST_EFFORT` mode), the resulting `DeliveryReceipt` carries the same `route_id` as the original delivery. This allows operators to correlate replay receipts with the original route, even though the receipt itself is a new record with a new `attempt_number`.

### 7.4 `ReplayRouteAttribution` Is a Filtered Set

`ReplayRouteAttribution.route_ids` contains only routes that **survived** loop prevention filtering. Routes that would produce self-loops or were already present in `route_trace` are excluded from the attribution set and recorded in `loop_warnings` instead.

### 7.5 `route_trace` Semantics

- `route_trace` is an ordered tuple of route IDs that were matched during routing passes, preserved in match order.
- It is **bounded to 16 entries**. When the trace exceeds 16, older entries are dropped.
- Loop prevention checks `route.id in event.metadata.routing.route_trace` before each delivery. This uses counting semantics: each route ID appears at most once per trace window, and the bound of 16 prevents unbounded trace growth.
- `matched_routes` records routes matched in the **current** routing pass. `route_trace` is the cumulative bounded history across passes (e.g. live + replay).
