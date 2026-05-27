# Diagnostics and Evidence

How to collect, interpret, and reason about MEDRE pipeline evidence before and after a run.

## Evidence Provenance

Bridge behavior is validated at four fidelity levels. Each level adds constraints and reduces the gap between test and production:

| Level                   | Environment                  | Transport        | What it proves                                                                                                      |
| ----------------------- | ---------------------------- | ---------------- | ------------------------------------------------------------------------------------------------------------------- |
| **Fake bridge**         | In-memory, fake adapters     | Simulated        | Pipeline routing, rendering, receipts, accounting, loop prevention                                                  |
| **Adapter-wrapper**     | Unit test, real adapter code | Mocked transport | Adapter codec, renderer, session logic                                                                              |
| **Docker SDK-boundary** | Container, real deps         | Loopback         | Dependency resolution, config loading, adapter lifecycle, real SDK boundary, pipeline routing through real adapters |
| **Live network**        | Real endpoints               | Real transport   | Actual connectivity, protocol compliance                                                                            |

Each provenance level also carries an environment-boundary sub-class for real-live evidence:

| Sub-class               | Meaning                                                                                                      |
| ----------------------- | ------------------------------------------------------------------------------------------------------------ |
| **Docker SDK-boundary** | Local Docker container running the transport server (e.g. Synapse). SDK boundary test — no external network. |
| **External live**       | Real external server over the network.                                                                       |
| **Hardware**            | Physical radio hardware connected via serial/TCP/BLE. Real RF transmission or reception.                     |

Do not treat Docker SDK-boundary evidence as equivalent to external live or hardware evidence. Each boundary validates different properties: SDK-boundary validates SDK integration and adapter wiring; external live validates network connectivity and real server behavior; hardware validates physical radio operation.

## Transport Evidence Matrix

| Adapter    | Fake callback | Wrapper callback | Docker SDK-boundary (outbound) | Docker SDK-boundary (inbound) | Live network/radio |
| ---------- | :-----------: | :--------------: | :----------------------------: | :---------------------------: | :----------------: |
| Matrix     |    proven     |      proven      |             proven             |      proven (sync_loop)       |  proven (Synapse)  |
| Meshtastic |    proven     |      proven      |             proven             |   unconfirmed (2nd client)    |    not claimed     |
| MeshCore   |    proven     |      proven      |        no Docker setup         |          not claimed          |    not claimed     |
| LXMF       |    proven     |      proven      |        no Docker setup         |          not claimed          |    not claimed     |

The table above is the authoritative per-transport, per-tier evidence matrix.

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

| Drill name                  | What it proves                                                               |
| --------------------------- | ---------------------------------------------------------------------------- |
| `renderer_failure`          | Unhandled event kind produces `RENDERER_FAILURE` receipt, no retry           |
| `adapter_permanent_failure` | Non-recoverable adapter error produces `ADAPTER_PERMANENT` receipt, no retry |
| `adapter_transient_failure` | Transient error triggers retry with `ADAPTER_TRANSIENT` receipt chain        |
| `capacity_rejection`        | Delivery capacity exhaustion produces `delivery_capacity_exceeded`           |
| `shutdown_rejection`        | In-flight deliveries during shutdown produce `delivery_rejected_shutdown`    |
| `replay_duplicate_risk`     | BEST_EFFORT replay produces duplicate receipts per run                       |
| `degraded_live_health`      | Adapters can report degraded/failed health without runtime exit              |

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

## See Also

- [recovery-and-replay.md](recovery-and-replay.md) — crash recovery, orphan detection, replay modes
- [troubleshooting.md](troubleshooting.md) — failure drill interpretation, routing diagnostics
- [transport-setup/matrix.md](transport-setup/matrix.md) — Matrix adapter setup and validation
- [transport-setup/meshtastic.md](transport-setup/meshtastic.md) — Meshtastic adapter setup and validation
