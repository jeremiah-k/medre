# Diagnostics and Evidence

How to collect, interpret, and reason about MEDRE pipeline evidence before and after a run.

## Evidence Provenance

Bridge behavior is validated at six fidelity levels. Each level adds constraints and reduces the gap between test and production:

| Level               | Environment                  | Transport        | What it proves                                                                                                      |
| ------------------- | ---------------------------- | ---------------- | ------------------------------------------------------------------------------------------------------------------- |
| **Synthetic**       | In-memory, fake adapters     | Simulated        | Pipeline routing, rendering, receipts, accounting, loop prevention                                                  |
| **Conformance**     | Deterministic fixtures       | Real codecs      | Behavioral contracts with real codecs/renderers/services using fixed JSON inputs                                    |
| **Adapter-wrapper** | Unit test, real adapter code | Mocked transport | Adapter codec, renderer, session logic                                                                              |
| **Docker**          | Container, real deps         | Loopback         | Dependency resolution, config loading, adapter lifecycle, real SDK boundary, pipeline routing through real adapters |
| **Live service**    | Real endpoints               | Real transport   | Actual connectivity, protocol compliance against a real external service                                            |
| **Hardware**        | Physical device              | Real RF          | Physical radio operation, firmware interaction, send/receive against actual hardware                                |

Do not treat Docker evidence as equivalent to live service or hardware evidence. Docker validates SDK integration and adapter wiring. Live service validates network connectivity and real server behavior. Hardware validates physical radio operation. Each boundary validates different properties.

Storage-only evidence (receipts and outbox rows in SQLite) records what the runtime observed and stored. Stored data alone does not constitute live service or hardware validation.

## Transport Evidence Matrix

| Adapter    | Synthetic | Conformance | Adapter-wrapper |  Docker  |       Live service       |  Hardware   |
| ---------- | :-------: | :---------: | :-------------: | :------: | :----------------------: | :---------: |
| Matrix     |  proven   |   proven    |     proven      |  proven  |       not claimed        | not claimed |
| Meshtastic |  proven   |   proven    |     proven      |  proven  | unconfirmed (2nd client) | not claimed |
| MeshCore   |  proven   |   proven    |     proven      | no setup |       not claimed        | not claimed |
| LXMF       |  proven   |   proven    |     proven      | no setup |       not claimed        | not claimed |

The table above is the authoritative per-transport, per-tier evidence matrix. "not claimed" means no evidence at that tier has been recorded. Docker validation demonstrates SDK/container integration. Docker validation is not live_service evidence. Docker validation is not hardware evidence. Conformance tests validate pipeline behavioral contracts using deterministic fixtures and real codec implementations. Conformance evidence applies to all transports equally.

## Quick Bundle Collection

Run these commands in order. Each writes JSON to stdout. Redirect to files for the bundle archive.

### Ephemeral Smoke (no files left behind)

```bash
PYTHONPATH=src medre smoke --json > bundle-smoke.json
# Exit code: 0 = passed, 1 = failed
```

### Persistent Smoke (inspectable after exit)

```bash
PYTHONPATH=src medre smoke --storage-path /tmp/medre-smoke.db --json > bundle-smoke-persist.json
```

### Failure Drills (all available drills)

```bash
for drill in renderer_failure adapter_permanent_failure \
  adapter_transient_failure capacity_rejection shutdown_rejection \
  replay_duplicate_risk degraded_live_health; do
  PYTHONPATH=src medre smoke --drill "$drill" \
    --storage-path /tmp/medre-smoke.db --json \
    >> bundle-drills.jsonl
done
```

### Pre-Runtime Drills (config and startup failures)

```bash
for drill in bad_route_config all_adapters_build_fail \
  partial_degraded_startup all_adapters_start_fail; do
  PYTHONPATH=src medre smoke --drill "$drill" \
    --storage-path /tmp/medre-smoke.db --json \
    >> bundle-preruntime.jsonl
done
```

### Evidence Command (single-command bundle)

```bash
# Basic bundle: config summary + route validation + diagnostics snapshot + storage
PYTHONPATH=src medre evidence --config my-bridge.toml --json > bundle-full.json

# Targeted: smoke a specific event through the pipeline
PYTHONPATH=src medre evidence --config my-bridge.toml --event <event_id> --json > bundle-event.json

# Include live health refresh (starts real adapters)
PYTHONPATH=src medre evidence --config my-bridge.toml --include-refresh-health --json > bundle-with-health.json
```

## Post-Run Inspection

For day-to-day investigation, start with `medre inspect` (the preferred operator path).

```bash
# Using --storage-path (read-only, no config needed):
medre inspect event <event_id> --storage-path /tmp/medre-smoke.db
medre inspect receipts --event <event_id> --storage-path /tmp/medre-smoke.db

# Inspect with timeline:
medre inspect event <event_id> --timeline --storage-path /tmp/medre-smoke.db

# Inspect with evidence:
medre inspect event <event_id> --evidence --storage-path /tmp/medre-smoke.db

# Inspect with recovery guidance:
medre inspect event <event_id> --recovery --storage-path /tmp/medre-smoke.db

# Other inspect subcommands:
medre inspect native-ref --adapter <name> --message <native_id> --storage-path /tmp/medre-smoke.db
medre inspect receipts --replay-run <run_id> --storage-path /tmp/medre-smoke.db

# Using --config (reads storage path from config):
medre inspect event <event_id> --config my-bridge.toml
medre inspect receipts --event <event_id> --config my-bridge.toml
```

All `inspect` subcommands (`event`, `receipts`, `native-ref`, and `receipts --replay-run`) support `--storage-path` for direct read-only access to a SQLite database.

The `replay` and `recover` commands require `--config`. Use `inspect` as your first investigation step.

## Command Reference

| Command                                                                        | Storage           | Starts adapters                                     | Output                           | Exit codes                              |
| ------------------------------------------------------------------------------ | ----------------- | --------------------------------------------------- | -------------------------------- | --------------------------------------- |
| `medre smoke --json`                                                           | In-memory         | Fake only                                           | passed/failed JSON               | 0=passed, 1=failed                      |
| `medre smoke --storage-path <db> --json`                                       | SQLite            | Fake only                                           | passed/failed JSON + DB          | 0=passed, 1=failed                      |
| `medre smoke --drill <name> --json`                                            | In-memory         | Fake only                                           | Drill report JSON                | 0=passed, 1=failed                      |
| `medre smoke --drill <name> --storage-path <db> --json`                        | SQLite            | Fake only                                           | Drill report JSON + DB           | 0=passed, 1=failed                      |
| `medre evidence --config <path> --json`                                        | Per config        | Fake only (or real with `--include-refresh-health`) | Full bundle JSON                 | 0=passed/partial, 2=config error        |
| `medre evidence --config <path> --event <id> --json`                           | Per config        | No                                                  | Bundle with event/receipt lookup | 0=passed/partial, 2=config error        |
| `medre evidence --config <path> --include-refresh-health --json`               | Per config        | Yes (real or fake)                                  | Full bundle + live health JSON   | 0=passed/partial, 2=config error        |
| `medre inspect event <id> --config <path>`                                     | Opens SQLite (RO) | No                                                  | Event JSON                       | 0=found, 2=no SQLite                    |
| `medre inspect receipts --event <id> --config <path>`                          | Opens SQLite (RO) | No                                                  | Receipt array JSON               | 0=found, 2=no SQLite                    |
| `medre inspect receipts --replay-run <id> --storage-path <db>`                 | Opens SQLite (RO) | No                                                  | Receipt array JSON               | 0=found, 2=no SQLite                    |
| `medre inspect native-ref --adapter <name> --message <id> --storage-path <db>` | Opens SQLite (RO) | No                                                  | Ref JSON                         | 0=found, 2=no SQLite                    |
| `medre diagnostics --config <path>`                                            | None              | No                                                  | Build-time snapshot JSON         | 0=success, 2=config, 3=build            |
| `medre diagnostics --refresh-health --config <path>`                           | None              | Yes (real or fake)                                  | Live health snapshot JSON        | 0=success, 2=config, 3=build, 4=startup |

## Report Shapes

### Smoke Report

```json
{
  "status": "passed",
  "evidence_level": "fake_bridge",
  "scenario_category": "smoke",
  "simulated": true,
  "command": "smoke",
  "commands_text": "medre smoke --json",
  "timestamp": "2026-05-14T10:30:00+00:00",
  "config_source": "default",
  "storage_backend": "memory",
  "preflight": {
    "config_valid": true,
    "routes_valid": true
  },
  "source_adapter": "bot",
  "target_adapters": ["radio"],
  "event_id": "evt_abc123",
  "route_ids": ["bot-to-radio"],
  "delivery_receipts": [
    {
      "receipt_id": "rcpt_001",
      "target_adapter": "radio",
      "status": "sent",
      "source": "live",
      "route_id": "bot-to-radio"
    }
  ],
  "accounting": {
    "inbound_accepted": 1,
    "outbound_attempts": 1,
    "outbound_delivered": 1,
    "outbound_failed": 0
  },
  "route_stats": {
    "bot-to-radio": { "delivered": 1, "failed": 0, "skipped": 0 }
  },
  "limitations": [
    "Fake adapters only — no real transport connectivity proven",
    "In-memory storage — no persistence or crash-recovery proof",
    "Fire-and-forget delivery model for radio transports"
  ]
}
```

When smoke **fails**, the report adds a `fail_reasons` array:

```json
{
  "status": "failed",
  "fail_reasons": [
    "No receipt with status 'sent'",
    "Accounting outbound_delivered < 1"
  ]
}
```

### Smoke PASSED Criteria

All four conditions below are required for a passed smoke report:

1. Event stored in storage (`storage.get(event_id)` returns non-None).
2. At least one `DeliveryOutcome` with `status == "success"`.
3. At least one `DeliveryReceipt` with `status == "sent"`.
4. `accounting.outbound_delivered >= 1`.

### Drill Report

```json
{
  "status": "passed",
  "evidence_level": "drill",
  "scenario_category": "drill",
  "simulated": true,
  "simulation_method": "failure_injection",
  "drill_name": "renderer_failure",
  "drill_steps": [
    { "step": "inject_unrenderable_event", "result": "ok" },
    { "step": "assert_renderer_failure_receipt", "result": "ok" }
  ],
  "limitations": [
    "Drill uses fake adapters — no real transport failure proven",
    "Failure injection is synchronous and deterministic"
  ]
}
```

### Evidence Bundle Report

The `medre evidence` command produces a structured bundle with per-section status. Each section has its own `status` (`"passed"`, `"partial"`, `"error"`, `"skipped"`), `error` (string or null), and `data` (section-specific payload).

**Top-level fields:**

| Field             | Type        | Description                                                |
| ----------------- | ----------- | ---------------------------------------------------------- |
| `schema_version`  | `int`       | Bundle schema version                                      |
| `status`          | `str`       | Overall: `"passed"`, `"partial"`, or `"error"`             |
| `collected_at`    | `str`       | ISO-8601 UTC timestamp                                     |
| `medre_version`   | `str`       | Installed package version                                  |
| `config_source`   | `str`       | How the config file was found (`"cli_arg"`, `"xdg"`, etc.) |
| `runtime_started` | `bool`      | `true` only when `--include-refresh-health` was used       |
| `sections`        | `dict`      | Grouped evidence, each with its own status                 |
| `errors`          | `list[str]` | Flat list of error strings across all sections             |
| `limitations`     | `list[str]` | What the evidence does not prove                           |

**Sections:**

| Section                | Populated when                                  | Key data fields                                                                     |
| ---------------------- | ----------------------------------------------- | ----------------------------------------------------------------------------------- |
| `config_summary`       | Always (if config loads)                        | `adapters`, `routes`, `limits`, `storage_backend`, `storage_path`                   |
| `route_validation`     | Always (if config loads)                        | `route_count`, `valid`, `route_errors`                                              |
| `diagnostics_snapshot` | Always (if config loads)                        | Full `build_runtime_snapshot` output                                                |
| `live_health`          | Only with `--include-refresh-health`            | Full runtime snapshot with `health.live_health` populated                           |
| `storage`              | When config uses `sqlite` backend and DB exists | `db_exists`, `event_count`, `receipt_count`, `event`, `trace_event`, `trace_replay` |

## Interpreting the Bundle

### Status Values

| Status             | Meaning                                                      | Operator action                                        |
| ------------------ | ------------------------------------------------------------ | ------------------------------------------------------ |
| `passed`           | All criteria met at the reported evidence level              | Proceed to live runtime with caution                   |
| `partial`          | Some adapters/routes/drills failed but the runtime stayed up | Inspect `fail_reasons` and per-adapter `.error` fields |
| `error` / `failed` | A required criterion was not met                             | Do not proceed. Fix config or environment, re-collect  |

### What `sent` Means Per Transport

| Transport  | `sent` means                                       | Remote receipt              |
| ---------- | -------------------------------------------------- | --------------------------- |
| Matrix     | Homeserver accepted the event (event_id returned)  | Not confirmed per-recipient |
| Meshtastic | Local node queued the packet for LoRa transmission | Unknown. Fire-and-forget.   |
| MeshCore   | Local node queued the packet                       | Unknown. Fire-and-forget.   |
| LXMF       | Local LXMRouter accepted for propagation           | Eventual, seconds to hours. |

### Why `--include-refresh-health` Starts Adapters

The `--include-refresh-health` flag causes `medre evidence` to build the runtime, start all enabled adapters, poll each adapter's `health_check()` once, capture the live health snapshot, and then stop the runtime cleanly. With fake adapters this is trivial. With real adapters, this opens real connections (Matrix TCP to homeserver, Meshtastic serial/TCP to local node, etc.).

### Inspect Output Interpretation

| `medre inspect` output                                               | What to look for                                                                                                      |
| -------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------- |
| `event` — source_adapter, event_kind, payload                        | Event was stored correctly before delivery                                                                            |
| `receipts` — status, failure_kind, attempt_number, parent_receipt_id | Full delivery lifecycle. `attempt_number > 1` with `parent_receipt_id` chain indicates retry.                         |
| `receipts` — route_id                                                | Which route triggered the delivery                                                                                    |
| `native-ref` — native_message_id, canonical_event_id                 | Maps transport-native IDs to canonical events                                                                         |
| `receipts --replay-run` — source="replay", replay_run_id             | Distinguishes replay from live. Multiple entries across different `replay_run_id` values = multiple BEST_EFFORT runs. |

## Fake Bridge Smoke: Running the Tests

```bash
# Full fake bridge test suite (no network, no hardware)
PYTHONPATH=src pytest tests/test_fake_bridge_smoke.py -v
# Expected: 30+ passed in under 30 seconds

# Specific test class
PYTHONPATH=src pytest tests/test_fake_bridge_smoke.py::TestMatrixToMeshtastic -v
```

### Test Coverage Matrix

| Test Class                       | Flow                            | Key Assertions                                                                        |
| -------------------------------- | ------------------------------- | ------------------------------------------------------------------------------------- |
| `TestMatrixToMeshtastic`         | Matrix -> Meshtastic            | Event stored, receipt sent, native ref, accounting, route stats, no duplicate         |
| `TestMeshtasticToMatrix`         | Meshtastic -> Matrix            | Event stored, receipt sent, inbound native ref, outbound native ref, accounting       |
| `TestBidirectionalBridge`        | Matrix <-> Meshtastic           | Both directions deliver, no cross-contamination, two receipts                         |
| `TestFanoutDelivery`             | Matrix -> Meshtastic + MeshCore | Both targets receive delivery, two receipts, two native refs, error isolation         |
| `TestLoopPrevention`             | Self-loop                       | Delivery skipped, loop_prevented counter incremented, `suppressed` receipt persisted  |
| `TestReplyRelationPreservation`  | Reply event bridge              | Relations preserved in storage, fallback text rendered correctly                      |
| `TestRenderingContract`          | Various                         | RenderingResult shape, empty payload handling, unsupported kind = failure, truncation |
| `TestSnapshotReflectsBridgeFlow` | After delivery                  | Accounting counters, route stats, JSON-safe snapshot                                  |
| `TestRouteConfigThroughRuntime`  | Config -> Routes                | Config route registers, bidirectional expands, policy filters, disabled skipped       |

### Operator Smoke Command

```bash
# Default: uses shipped fake-bridge-smoke.toml
PYTHONPATH=src medre smoke

# JSON report (machine-readable)
PYTHONPATH=src medre smoke --json

# Explicit config
PYTHONPATH=src medre smoke --config examples/configs/fake-bridge-smoke.toml

# Custom message text
PYTHONPATH=src medre smoke --message "operator check $(date -Iseconds)"

# Run a specific scenario (choices: happy_path, renderer_failure,
# adapter_permanent_failure, adapter_transient_failure, capacity_rejection,
# degraded_live_health)
PYTHONPATH=src medre smoke --scenario <name> --json
```

### Smoke Persistence Caveat

`medre smoke` uses in-memory storage by default. When the smoke process exits, all stored evidence is released. The JSON report printed to stdout is the only surviving record.

Pass `--storage-path <path>` to persist evidence to a SQLite database instead. When `--storage-path` is supplied, events, receipts, and native refs are written to the specified database file and can be inspected with `medre inspect` after the process exits.

`medre inspect` subcommands require persistent storage. Running `medre inspect` against a config with `[storage] backend = "memory"` produces:

```text
Error: storage backend is 'memory' — no persistent data to inspect.
```

To inspect stored evidence after a run, use `medre run` with SQLite storage:

```toml
[storage]
backend = "sqlite"
```

## Docker SDK-Boundary Tests

```bash
# Prerequisites: Docker daemon running, SDK extras installed
pip install -e ".[matrix,meshtastic,dev]"

# All Docker integration tests
PYTHONPATH=src pytest tests/integration/ -m docker -v

# Matrix (Synapse) only
PYTHONPATH=src pytest tests/integration/test_synapse_connectivity.py -m docker -v

# Meshtastic (meshtasticd) only
PYTHONPATH=src pytest tests/integration/test_meshtasticd_connectivity.py -m docker -v

# Synapse bridge smoke (full pipeline: real Matrix SDK -> PipelineRunner -> FakeMatrixAdapter)
PYTHONPATH=src pytest tests/integration/test_synapse_bridge_smoke.py -m docker -v
```

Docker tests are excluded from default runs via `addopts = "-m 'not live and not docker'"` in `pyproject.toml`. They are collected and skipped unless explicitly enabled.

```bash
# Default: Docker tests collected but not run
PYTHONPATH=src pytest -q

# Explicitly skip Docker
MEDRE_SKIP_DOCKER=1 pytest tests/integration/ -v

# Run everything including Docker + live
pytest -m "" -v
```

### Failure Interpretation

| Symptom                                             | Likely cause                    | Action                                             |
| --------------------------------------------------- | ------------------------------- | -------------------------------------------------- |
| Docker tests skip with "Docker not available"       | Docker daemon not running       | Start Docker: `docker info`                        |
| Docker tests skip with "mtjk not installed"         | Meshtastic SDK not installed    | `pip install -e ".[meshtastic]"`                   |
| Docker tests skip with "mindroom-nio not installed" | Matrix SDK not installed        | `pip install -e ".[matrix]"`                       |
| Config validation exits 2                           | TOML syntax or credential error | `medre config check --config <path>`               |
| Routes validate exits 2                             | Unknown adapter ref in route    | Check adapter IDs in routes match adapters section |

## Diagnostics Commands

```bash
# Build-time snapshot (no adapter start, no I/O)
PYTHONPATH=src medre diagnostics --config examples/configs/fake-bridge-smoke.toml

# Live health refresh (starts adapters, polls health, stops)
PYTHONPATH=src medre diagnostics --refresh-health --config examples/configs/fake-multi-adapter.toml
```

## Evidence Assertion Criteria

### Unidirectional Bridge (A -> B)

Required assertions:

1. **Inbound event persisted**: `storage.get(event_id)` returns the canonical event with correct `source_adapter`, `event_kind`, and payload.
2. **Route selected**: The pipeline outcome has `route_id` matching the configured route. `route_stats.snapshot()` shows `delivered >= 1`.
3. **Rendered outbound payload**: Target adapter's `delivered_payloads` contains a `RenderingResult` with `target_adapter` matching the target.
4. **DeliveryReceipt persisted**: `storage.list_receipts_for_event(event_id)` returns at least one receipt with `status == "sent"`, `target_adapter` matching, `route_id` matching, `source == "live"`.
5. **NativeMessageRef persisted**: `storage.resolve_native_ref(adapter, channel, native_id)` returns the canonical `event_id`. Only when the adapter returns a `native_message_id`.
6. **Runtime accounting**: `accounting.snapshot()` shows `inbound_accepted == 1`, `outbound_attempts >= 1`, `outbound_delivered >= 1`.
7. **No duplicate delivery**: `len(target_adapter.delivered_payloads) == 1` exactly.

### Bidirectional Bridge (A <-> B)

In addition to unidirectional criteria for each direction:

1. **Config expansion**: Bidirectional `RouteConfig` produces exactly two registered `Route` objects (forward + reverse).
2. **Both directions deliver**: Events from A deliver to B, and events from B deliver to A, without cross-contamination.
3. **Independent receipts**: Each direction produces its own receipt with the correct `target_adapter`.
4. **Accounting reflects both**: `inbound_accepted == 2`, `outbound_delivered == 2` after one event in each direction.

### Fanout (A -> B, C)

1. **All targets receive**: Each target adapter has `len(delivered_payloads) == 1`.
2. **Multiple receipts**: `storage.list_receipts_for_event(event_id)` returns one receipt per target.
3. **Multiple native refs**: One `NativeMessageRef` per target that returns a native ID.
4. **Accounting**: `outbound_attempts == N`, `outbound_delivered == N`.
5. **Error isolation**: When one target fails, other targets still receive delivery.

### Loop Prevention

1. **Self-loop guard**: When `target_adapter == source_adapter`, the pipeline returns `outcome.status == "skipped"` with error containing "loop_prevented".
2. **No delivery**: The target adapter has zero delivered payloads.
3. **Suppressed receipt**: A `DeliveryReceipt` with `status="suppressed"` is persisted.
4. **Accounting**: `loop_prevented >= 1`.
5. **Event still stored**: The inbound event is persisted even though delivery was skipped.

## Available Drills

### Runtime Failure Drills

| Drill name                  | What it proves                                                                                                                         |
| --------------------------- | -------------------------------------------------------------------------------------------------------------------------------------- |
| `renderer_failure`          | Unhandled event kind produces `RENDERER_FAILURE` receipt, no retry                                                                     |
| `adapter_permanent_failure` | Non-recoverable adapter error produces `ADAPTER_PERMANENT` receipt, no retry                                                           |
| `adapter_transient_failure` | Transient error triggers retry with `ADAPTER_TRANSIENT` receipt chain                                                                  |
| `capacity_rejection`        | Delivery capacity exhaustion produces `delivery_capacity_exceeded`                                                                     |
| `shutdown_rejection`        | In-flight deliveries during shutdown produce `shutdown_rejection` with either `shutdown_drain_timeout` or `delivery_rejected_shutdown` |
| `replay_duplicate_risk`     | BEST_EFFORT replay produces duplicate receipts per run                                                                                 |
| `degraded_live_health`      | Adapters can report degraded/failed health without runtime exit                                                                        |

### Pre-Runtime Drills

| Drill name                 | What it proves                                                             |
| -------------------------- | -------------------------------------------------------------------------- |
| `bad_route_config`         | Unknown adapter ref in route causes `RouteValidationError`                 |
| `all_adapters_build_fail`  | Total build failure causes all adapters to fail construction               |
| `partial_degraded_startup` | Partial adapter start allows runtime to enter RUNNING with degraded health |
| `all_adapters_start_fail`  | Total startup failure prevents RUNNING state                               |

Run pre-runtime drills with:

```bash
PYTHONPATH=src medre smoke --drill <drill_name> --storage-path /tmp/medre-smoke.db --json
```

Each drill **exits 0** when the expected failure is correctly observed. The drill report documents what exit code and error the runtime would produce if run independently.

## Bug Report Artifacts

When filing a bug against MEDRE evidence, delivery, or runtime behavior, attach:

1. **`medre evidence --config <path> --json`** output.
2. **`medre evidence --config <path> --include-refresh-health --json`** output if the issue involves adapter health or connectivity.
3. **`medre inspect` outputs** showing the specific receipt, event, or ref in question.
4. **Config file** with secrets redacted.
5. **`medre version`** output.
6. **`medre paths`** output (for path-related issues).

Name files descriptively: `bundle-<date>.json`, `bundle-health-<date>.json`, `inspect-receipts-<event_id>.json`.

## Operator Traceability Questions

The evidence bundle and report dicts answer common operator questions without needing to read source code or logs. Here is a reference for what to look for:

### "Was this event processed?"

Check `event_summary` in the evidence bundle. If present, the event was stored. If `None`, the event was not found in storage.

```bash
medre inspect event <event_id> --storage-path /path/to/medre.sqlite
```

### "Which route matched?"

Check `route_id` on the receipt or in the `delivery_state_by_target` entry. The `route_id` identifies which route configuration rule triggered the delivery.

### "Which target was selected?"

Check `target_adapter` and `target_channel` in the receipt. The composite key `(delivery_plan_id, route_id, target_adapter, target_channel)` uniquely identifies a delivery target.

### "What plan ID was assigned?"

Check `delivery_plan_id` on the receipt. Plan IDs are deterministic — the same event with the same route configuration produces the same plan ID regardless of whether it was a live delivery or a replay run. This means repeated replays produce predictable plan IDs.

### "What strategy was chosen?"

Check `delivery_strategy` in the report dict. This is derived from the receipt's `rendering_evidence` JSON or error text. Values: `"direct"` (native delivery), `"fallback_text"` (degraded rendering), or `"skip"` (suppressed before delivery).

### "What capability field drove the decision?"

Check `capability_field` in the report dict. This identifies which adapter capability field (e.g. `reactions`, `replies`, `text`) caused the strategy decision. It is `None` for loop-suppressed or policy-suppressed deliveries (those are driven by guards, not capabilities).

### "What is the delivery status?"

Check `status` on the receipt:

| Status          | Meaning                                            |
| --------------- | -------------------------------------------------- |
| `sent`          | Adapter accepted the delivery                      |
| `queued`        | Delivery enqueued, awaiting adapter confirmation   |
| `suppressed`    | Delivery suppressed by a guard (no adapter call)   |
| `failed`        | Delivery failed, may be retryable                  |
| `dead_lettered` | All retries exhausted, delivery permanently failed |

Suppressed receipts (`status="suppressed"`) are distinct from failed receipts (`status="failed"`). Suppressed means a guard fired before the adapter was called. Failed means the adapter was called and returned an error.

### "Why did delivery fail?"

Check these fields together:

- `failure_kind` — the high-level category (e.g. `capability_suppressed`, `loop_suppressed`, `adapter_transient`)
- `failure_kind_detail` — more specific (e.g. `e2ee_blocked`, `meshtastic_queue_rejected`)
- `suppression_reason` — human-readable reason parsed from the error text
- `error` — the raw error message

### "Was this a live delivery or replay?"

Check `source` on the receipt: `"live"` or `"replay"`. For replay, `replay_run_id` identifies the specific replay run.

### "How many retry attempts occurred?"

Check `attempt_number` on the receipt chain. Each retry produces a new receipt with an incremented `attempt_number`, linked via `parent_receipt_id`. The highest `attempt_number` represents the latest attempt. A `dead_lettered` receipt means retries were exhausted.

```sql
SELECT receipt_id, status, attempt_number, failure_kind, next_retry_at
FROM delivery_receipts
WHERE delivery_plan_id = '<plan_id>'
ORDER BY attempt_number;
```

### "Were suppressed deliveries retried?"

No. Suppressed deliveries (status `suppressed`) do not enter the retry queue. Their `next_retry_at` is always `None`. Suppression indicates a guard prevented delivery entirely — it is not a transient condition that retrying would resolve.

## Adapter Status Lifecycle

Adapters present one of the following operator-visible statuses, derived from configuration, runtime state, and health checks:

| Status           | When you see it                                                 | What it means                                                       |
| ---------------- | --------------------------------------------------------------- | ------------------------------------------------------------------- |
| `disabled`       | Config has `enabled = false`                                    | Adapter is present in config but intentionally excluded.            |
| `not_configured` | No adapter entry for this transport/ID in config                | No configuration exists. No adapter object is constructed.          |
| `configured`     | Valid config entry exists, runtime has not started              | Config is valid but the adapter has not been built or started.      |
| `starting`       | Runtime is in the startup phase                                 | The adapter is between build and start. Transient, visible in logs. |
| `connected`      | Health check reports `connected == True`, health OK             | Adapter is connected and operating normally.                        |
| `unavailable`    | Health check reports `connected == False`, no error             | Transport endpoint is not reachable right now.                      |
| `failed`         | Health check reports `health == "failed"`, or build/start error | Non-recoverable failure. Adapter is not connected.                  |
| `stopped`        | Runtime shutdown completed                                      | Adapter was running and has been stopped cleanly.                   |
| `degraded`       | Health check reports `health == "degraded"`                     | Adapter is connected but experiencing transient errors.             |

These statuses are not a state machine enforced in code. They are evidence labels that operators can observe through `medre diagnostics`, snapshot output, and log entries. An adapter can go from `failed` to `connected` across a runtime restart if the underlying cause is resolved.

Check adapter status at any time:

```bash
# Build-time snapshot (no adapter start)
medre diagnostics --config my-bridge.toml

# Live health refresh (starts and polls adapters)
medre diagnostics --refresh-health --config my-bridge.toml
```

## Shutdown Delivery Evidence

When the runtime shuts down, the delivery evidence system records what happened to in-flight work:

| Scenario                                           | Evidence produced                                                                             |
| -------------------------------------------------- | --------------------------------------------------------------------------------------------- |
| In-flight delivery completes during drain period   | Normal receipt with final status (`sent` or `failed`)                                         |
| In-flight delivery abandoned after drain timeout   | Suppressed receipt with failure_kind `shutdown_rejection`, error `shutdown_drain_timeout`     |
| New delivery rejected because shutdown is underway | Suppressed receipt with failure_kind `shutdown_rejection`, error `delivery_rejected_shutdown` |
| Pending retry receipt at shutdown                  | No change; receipt stays in storage for next startup                                          |
| Pending outbox item at shutdown                    | No change; outbox row stays for next startup                                                  |

Pending retry receipts and outbox items are not cancelled during shutdown. They survive in SQLite and are processed on next startup by the RetryWorker (for due retry receipts) or by the normal outbox reclaim path (`claim_due_outbox_items`) for plain pending/queued/in_progress rows. This is an intentional design choice: non-terminal outbox work is preserved as resumable work, not implicitly transitioned to a cancelled state. The `ShutdownEvidence` record (in the evidence bundle) reports `resume_expected=True` when pending work was left at shutdown, and `outbox_shutdown_policy="resumable"` signals the resumable policy is active. Operators can inspect `pending_outbox_counts` in the shutdown evidence to see exactly which statuses and counts were preserved.

Use `--snapshot-on-shutdown PATH` to capture the final runtime state including counters, route stats, and the bounded event buffer:

```bash
medre run --config my-bridge.toml --snapshot-on-shutdown /tmp/shutdown-snapshot.json
```

## Live Validation Boundaries

MEDRE distinguishes three levels of real-endpoint validation. Each level validates different properties:

| Boundary     | Validates                                        | Does not validate                                           |
| ------------ | ------------------------------------------------ | ----------------------------------------------------------- |
| Docker       | SDK integration, adapter wiring, config loading  | External network, federation, hardware, real-world rates    |
| Live service | Network connectivity, protocol compliance, auth  | Hardware, RF behavior, physical device interaction          |
| Hardware     | Physical radio operation, firmware, send/receive | Network federation, server-side behavior, multi-device mesh |

Docker evidence is not hardware evidence. A Docker container running Synapse proves the Matrix SDK works; it does not prove the adapter can talk to a real homeserver over the internet. A physical Meshtastic radio connected via serial proves hardware interaction; it does not prove the Matrix integration.

No Matrix transport beyond Docker localhost has been validated. No Meshtastic, MeshCore, or LXMF hardware validation has been recorded. See "What Remains Unproven" below for the complete list.

## What Remains Unproven

| Capability                                     | Status     | Notes                                                         |
| ---------------------------------------------- | ---------- | ------------------------------------------------------------- |
| Live external Matrix (beyond Docker localhost) | Not proven | Docker tests use loopback Synapse only                        |
| Real radio hardware (Meshtastic/MeshCore/LXMF) | Not proven | No live hardware smoke test recorded                          |
| Final delivery ACK / remote receipt            | Not proven | Radio is fire-and-forget; Matrix is server-level only         |
| Replay deduplication                           | Not proven | Replay produces duplicates by design                          |
| Active restart / supervision                   | Not proven | No per-adapter restart, no auto-remediation                   |
| Background health polling                      | Not proven | Manual `--refresh-health` only                                |
| Sustained throughput                           | Not proven | All tests are smoke tests, not load tests                     |
| Network resilience / reconnection              | Not proven | No live failure/reconnect test                                |
| Cross-instance loop prevention                 | Not proven | Loop prevention is local-process only                         |
| Third-party Matrix inbound                     | Not proven | Bridge smoke uses HTTP API sender, not a second Matrix client |
| Full cross-transport relay                     | Not proven | Bridge smoke routes real Matrix to fake outbound              |

## Convergence Diagnostics

The evidence bundle includes a `convergence_summary` that classifies every delivery target's state by cross-referencing outbox and receipt statuses. When collected with an `event_id`, the summary is event-scoped; without, it provides the global view across all delivery targets.

### Reading Convergence Output

The convergence summary has three fields to check first:

| Field             | What to look for                                                                   |
| ----------------- | ---------------------------------------------------------------------------------- |
| `worst_severity`  | If `"inconsistent"`, investigate the targets with that severity.                   |
| `severity_counts` | How many targets at each level. Any non-zero `inconsistent` count needs attention. |
| `targets`         | Per-target details with `outbox_status`, `latest_receipt_status`, and `warnings`.  |

### Per-Target Details

Each target in the convergence summary includes:

- `delivery_plan_id`, `target_adapter`, `target_channel`: identifies the delivery target.
- `outbox_status`: the outbox item status (or `null` if no outbox item).
- `latest_receipt_status`: the highest-authority receipt status (or `null` if no receipt).
- `severity`: `safe`, `degraded`, or `inconsistent`.
- `warnings`: human-readable messages explaining why this severity was assigned.

### Operator Actions by Severity

#### `safe`

No action needed. Outbox and receipts agree on the delivery state.

#### `degraded`

Work is stalled or mid-flight. This is normal for recent events during startup recovery. If degraded persists well after startup, check whether:

- The RetryWorker is enabled and processing due retry receipts.
- `claim_due_outbox_items()` is reclaiming expired leases and stale queued items.
- The adapter is connected and accepting deliveries.

#### `inconsistent`

State mismatch that cannot be explained by normal flow. Investigate the specific target:

1. Check the receipt chain for the `delivery_plan_id`:

   ```sql
   SELECT receipt_id, status, attempt_number, failure_kind, created_at
   FROM delivery_receipts
   WHERE delivery_plan_id = '<plan_id>'
   ORDER BY attempt_number;
   ```

2. Check the outbox item status:

   ```sql
   SELECT outbox_id, status, attempt_number, updated_at
   FROM delivery_outbox
   WHERE delivery_plan_id = '<plan_id>';
   ```

3. Determine whether the outbox or receipt is the stale record. The outbox is the operational authority for current state; receipts are the immutable evidence trail.

4. If the outbox is stale (terminal but receipt shows non-terminal), consider replaying the event after resolving the underlying cause.

5. If the receipt is stale (non-terminal but outbox is terminal), the delivery likely completed successfully and the receipt chain may be incomplete.

### Orphan and Lineage Findings

The convergence system also detects orphaned and invalid-lineage records when supplied with an event catalogue:

| Finding kind                       | What it means                                               | What to do                                                                                      |
| ---------------------------------- | ----------------------------------------------------------- | ----------------------------------------------------------------------------------------------- |
| `orphaned_outbox`                  | Outbox item references an event that no longer exists.      | Cancel or abandon the orphaned outbox row if the event was intentionally deleted.               |
| `orphaned_parent_receipt`          | Receipt references a parent receipt that does not exist.    | Check for data loss. The receipt lineage is broken.                                             |
| `cross_plan_parent`                | Receipt's parent belongs to a different delivery plan.      | Retry lineage crossed plan boundaries. Investigate the delivery chain.                          |
| `cross_event_parent`               | Receipt's parent belongs to a different event.              | Retry lineage crossed event boundaries. Investigate the delivery chain.                         |
| `missing_delivery_plan_id`         | Retry receipt has no delivery plan ID.                      | The retry may resolve on its own. Check the original delivery.                                  |
| `dead_lettered_retryable_mismatch` | Outbox is dead-lettered but latest receipt is non-terminal. | The item might still be retryable. Consider replay if the underlying failure cause is resolved. |

All findings are detection-only. No automatic repair occurs.

## Lifecycle Convergence Findings

The evidence bundle also includes a `lifecycle_convergence_report`. When collected with an `event_id`, the report is event-scoped; without, it provides the global view across all delivery targets. This report contains specific findings about contradictions between outbox and receipt state machines, retry metadata problems, stalled plans, and sequence anomalies. It is separate from the `convergence_summary` (which gives overall target health) and the `orphan_report` (which detects broken lineage).

### Reading Lifecycle Convergence Output

The lifecycle convergence report has three fields to check first:

| Field             | What to look for                                                                      |
| ----------------- | ------------------------------------------------------------------------------------- |
| `worst_severity`  | If `"inconsistent"`, there are data-integrity contradictions that need investigation. |
| `severity_counts` | How many findings at each level. Any non-zero `inconsistent` count needs attention.   |
| `findings`        | Individual findings with `kind`, `record_id`, `details`, and `extra` context.         |

### Finding Kinds and Operator Actions

#### `terminal_receipt_nonterminal_outbox` (degraded)

The latest receipt says the delivery finished (sent, suppressed, or dead_lettered) but the outbox item is still non-terminal (pending, retry_wait, in_progress, queued).

This is typically a timing artifact — the outbox may not have caught up with the receipt yet. If the condition persists, it may indicate a stale outbox entry.

```sql
SELECT outbox_id, status, updated_at FROM delivery_outbox
WHERE delivery_plan_id = '<plan_id>';
SELECT receipt_id, status, attempt_number FROM delivery_receipts
WHERE delivery_plan_id = '<plan_id>' ORDER BY attempt_number;
```

Determine which record is stale. If the receipt is correct (delivery did complete), the outbox is stale. If the outbox is correct, the receipt may be from a stale correlation.

#### `terminal_outbox_nonterminal_receipt` (inconsistent)

The outbox has reached a terminal status but the latest receipt is still non-terminal (queued or failed).

This is also a data-integrity contradiction. The receipt should reflect the terminal outcome.

```sql
SELECT outbox_id, status FROM delivery_outbox
WHERE delivery_plan_id = '<plan_id>';
SELECT receipt_id, status FROM delivery_receipts
WHERE delivery_plan_id = '<plan_id>' ORDER BY attempt_number DESC LIMIT 1;
```

The delivery likely completed but the receipt chain may be incomplete. Check if the adapter callback was received and if a supplemental receipt was created.

#### `retry_wait_missing_next_retry` (inconsistent)

An outbox item is in `retry_wait` state but has no valid `next_attempt_at` timestamp. The retry scheduler cannot determine when to retry.

```sql
SELECT outbox_id, status, next_attempt_at FROM delivery_outbox
WHERE outbox_id = '<outbox_id>';
```

The retry metadata is corrupted or was never set. Consider replaying the event or manually correcting the `next_attempt_at` value.

#### `receipt_outbox_mismatch` (degraded)

Both receipt and outbox exist for a target but their statuses contradict normal flow in a way that does not indicate a terminal/non-terminal mismatch. For example, both are terminal but with different statuses, or both are non-terminal in an abnormal combination.

```sql
SELECT o.outbox_id, o.status AS outbox_status, r.receipt_id, r.status AS receipt_status
FROM delivery_outbox o JOIN delivery_receipts r
ON o.delivery_plan_id = r.delivery_plan_id
WHERE o.delivery_plan_id = '<plan_id>';
```

Check whether the statuses reflect a recent transition in progress or a persistent inconsistency.

#### `next_retry_in_past` (degraded)

An outbox item is in `retry_wait` but `next_attempt_at` is in the past. The retry should have already been attempted.

```sql
SELECT outbox_id, status, next_attempt_at, updated_at FROM delivery_outbox
WHERE outbox_id = '<outbox_id>';
```

The RetryWorker may be behind, or the retry scheduling logic produced an incorrect timestamp. Check that the RetryWorker is running and processing due items.

#### `retryable_without_retry_metadata` (degraded)

A failed receipt appears retryable (transient failure or matching non-terminal outbox) but is missing retry scheduling fields.

```sql
SELECT receipt_id, status, failure_kind, next_retry_at FROM delivery_receipts
WHERE receipt_id = '<receipt_id>';
```

The retry metadata was not populated when the receipt was created. If retry is enabled, the receipt may not be picked up by the RetryWorker. Consider replaying the event.

#### `stalled_delivery_plan` (degraded)

A non-terminal outbox item has not been updated for longer than the stall threshold (default 1 hour). The delivery appears stuck.

```sql
SELECT outbox_id, status, updated_at FROM delivery_outbox
WHERE outbox_id = '<outbox_id>';
```

Check whether the worker that claimed this item is still alive. Expired leases should be reclaimed by `claim_due_outbox_items()`. If the item remains stalled, the worker may have crashed without releasing the claim.

#### `attempt_count_regression` (inconsistent)

Within the same delivery target, a later receipt has a lower attempt number than an earlier receipt. Attempt numbers should monotonically increase within a retry chain.

```sql
SELECT receipt_id, attempt_number, created_at FROM delivery_receipts
WHERE delivery_plan_id = '<plan_id>' ORDER BY created_at;
```

This suggests a data integrity issue in the retry chain. The receipt chain should be audited for correctness.

#### `receipt_sequence_gap` (degraded)

Receipts for the same target have sequence numbers that skip by more than 1. Some intermediate receipts may be missing.

```sql
SELECT receipt_id, sequence, status, created_at FROM delivery_receipts
WHERE delivery_plan_id = '<plan_id>' ORDER BY sequence;
```

Gaps may indicate lost receipts or concurrent delivery attempts. Check whether receipts were created but not persisted.

### Important Constraints

- Lifecycle convergence diagnostics are **deterministic and read-only**. They never change retry scheduling, worker behavior, or storage state.
- No automatic repair occurs. All findings are for operator inspection only.
- Findings follow the `LifecycleConvergenceFinding` JSON Schema type with a closed `kind` enum. Each finding includes `kind`, `severity`, `record_id`, `record_type`, `details`, and `extra` fields.

## Convergence Surfaces Overview

The evidence bundle exposes three distinct convergence/diagnostic surfaces. Each answers a different question:

| Surface                        | Answers                                                                     | Scope per event                                                                                                                 |
| ------------------------------ | --------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------- |
| `convergence_summary`          | "Is this target healthy overall?"                                           | Per-target classification (safe / degraded / inconsistent) with `outbox_status` and latest receipt status.                      |
| `orphan_report`                | "Are there broken lineages or recovery anomalies?"                          | Orphaned records, invalid parent chains, and four recovery-convergence finding kinds.                                           |
| `lifecycle_convergence_report` | "What specific lifecycle contradictions exist between outbox and receipts?" | Nine specific finding kinds covering status mismatches, retry anomalies, stalled plans, sequence gaps, and attempt regressions. |

These surfaces are complementary, not redundant. Start with `convergence_summary.worst_severity` to triage. If `inconsistent`, check `lifecycle_convergence_report.findings` for the specific contradiction. If `orphan_report.total_findings > 0`, check whether lineages or recovery ownership are broken.

All three surfaces are detection-only. None of them repair state, block startup, or change worker behavior.

## Recovery Ownership Evidence

The evidence bundle includes `recovery_summary` and `recovery_ledger` fields that document what happened to outbox items at startup recovery time. This is an accountability mechanism — it makes startup recovery behavior observable and provably race-safe.

### Recovery Ownership Statuses

Every outbox item is classified into exactly one recovery ownership status:

| Status                 | What it means                                                                                |
| ---------------------- | -------------------------------------------------------------------------------------------- |
| `recoverable`          | Non-terminal and not yet claimed for recovery.                                               |
| `claimed_for_recovery` | Moved to `in_progress` with a recovery context (lease set, worker identity assigned).        |
| `reclaimed`            | Previously in a resumable state (`pending` or `retry_wait`) and has been reclaimed.          |
| `abandoned`            | Previously `in_progress` but recovery was abandoned (e.g. drain timeout).                    |
| `unrecoverable`        | Terminal status — does not require recovery.                                                 |
| `skipped`              | Retry-eligible with future `next_attempt_at`, or `in_progress` with active lease — deferred. |

### Recovery Sources

Each ownership action carries a `recovery_source`:

| Source                  | When it fires                                                                                    |
| ----------------------- | ------------------------------------------------------------------------------------------------ |
| `startup_recovery`      | Outbox item reclaimed during runtime startup by the `RetryWorker` at boot.                       |
| `retry_worker_recovery` | Outbox item reclaimed during steady-state retry polling by the `RetryWorker`.                    |
| `snapshot_diagnostics`  | Diagnostic classification from stored snapshots — no runtime startup occurred, no live recovery. |
| `replay_execution`      | Reserved for future replay recovery. Not currently produced.                                     |

### Snapshot vs. Real Recovery — Important Distinction

Recovery evidence exists at three semantic levels that **operators should never conflate**:

1. **Actual startup recovery**: Tied to a real runtime startup cycle. Uses a real `recovery_run_id` from `BootSummary`. Produced by the runtime recovery path during boot. This is the only level that reflects genuine startup reclamation.

2. **Runtime evidence-bundle snapshot diagnostics**: Built from a storage snapshot at collection time. Uses a snapshot-scoped `recovery_run_id`. This is a **diagnostic reconstruction**, not proof that a startup recovery cycle occurred. The snapshot reflects outbox state as observed, not a live recovery transaction.

3. **Per-event recovery diagnostics**: Built from an event-scoped outbox snapshot by the `EvidenceCollector`. Uses `recovery_run_id=None` because the per-event collector has no `BootSummary` access. This is **classification only**, not proof of startup recovery.

**Operators should not interpret snapshot-derived `recovery_summary` and `recovery_ledger` values as proof that startup recovery actually ran.** They reflect stored outbox state at the time the snapshot was taken. The `recovery_source` field tells you which level produced each action:

- Actions with `recovery_source="snapshot_diagnostics"` are diagnostic reconstructions.
- Actions with `recovery_source="startup_recovery"` or `"retry_worker_recovery"` are from real runtime reclamation.
- The per-event collector always produces `"snapshot_diagnostics"` because it has no startup context.

### Reading Recovery Evidence

The `recovery_summary` has these fields to check:

| Field                 | What to look for                                                                                           |
| --------------------- | ---------------------------------------------------------------------------------------------------------- |
| `total_items`         | Total outbox items examined.                                                                               |
| `consistency_valid`   | Must be `true` — invariants hold (sum of categories equals total).                                         |
| `by_source`           | Count per `recovery_source`. If all are `"snapshot_diagnostics"`, this is a snapshot, not a real recovery. |
| `recoverable_items`   | Items available for reclaim at next startup.                                                               |
| `unrecoverable_items` | Terminal items (no recovery needed).                                                                       |

The `recovery_ledger` provides per-item detail. Each action has `ownership_action`, `prior_status`, `recovered_status`, `recovery_source`, and a human-readable `reason`.

### Recovery Convergence Findings

Four finding kinds extend the convergence diagnostics for recovery-specific anomalies. They appear in the `orphan_report`:

| Finding Kind               | Severity       | What it means                                                                                  |
| -------------------------- | -------------- | ---------------------------------------------------------------------------------------------- |
| `recovered_not_progressed` | `degraded`     | Outbox was recovered but latest receipt hasn't progressed since the previous shutdown.         |
| `repeatedly_reclaimed`     | `degraded`     | Same outbox item appears in multiple recovery ledgers with different `recovery_run_id` values. |
| `reclaimed_then_terminal`  | `inconsistent` | Outbox is terminal but latest receipt is non-terminal.                                         |
| `reclaimed_then_orphaned`  | `inconsistent` | Outbox was recovered but its `event_id` is absent from the known event catalogue.              |

## See Also

- [recovery-and-replay.md](recovery-and-replay.md) — crash recovery, orphan detection, replay modes
- [troubleshooting.md](troubleshooting.md) — failure drill interpretation, routing diagnostics
- [transport-setup/matrix.md](transport-setup/matrix.md) — Matrix adapter setup and validation
- [transport-setup/meshtastic.md](transport-setup/meshtastic.md) — Meshtastic adapter setup and validation
