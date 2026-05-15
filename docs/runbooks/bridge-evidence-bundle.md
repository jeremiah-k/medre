# Bridge Evidence Bundle

> Last updated: 2026-05-14
> Scope: Operator workflow for collecting, inspecting, and interpreting
>         pre-runtime evidence before committing to a live bridge.
> Status: Pre-beta. All commands use fake adapters unless noted.
> Prerequisites: medre installed with `[dev]` extras, no Docker or live endpoints
> needed for the basic bundle.

This runbook describes how to collect an evidence bundle: a set of CLI-produced
artifacts that prove pipeline correctness at the fake-bridge and drill levels
before an operator starts a live runtime. The bundle is also the recommended
attachment format for bug reports.

**What the bundle proves:**

- The runtime pipeline correctly routes events between adapters.
- DeliveryReceipts are persisted for every outbound delivery attempt.
- NativeMessageRefs are persisted when adapters return native IDs.
- RuntimeAccounting counters reflect actual flow.
- RouteStats track per-route delivery counts.
- Failure drills produce the expected receipt status and failure kind.

**What the bundle does NOT prove:**

- Real transport connectivity (no network involved unless `--refresh-health`
  is used with real adapters).
- Delivery confirmation beyond local adapter acceptance.
- Sustained throughput, reconnection resilience, or multi-hop delivery.
- Radio transports fire-and-forget. `sent` means the local node accepted the
  packet, not that any remote node received it.

See [Fake Bridge Evidence Criteria](fake-bridge-evidence-criteria.md) for the
full assertion-level criteria per flow type.


## 1. Quick Bundle Collection

Run these commands in order. Each writes JSON to stdout. Redirect to files for
the bundle archive.

### 1.1 Ephemeral Smoke (no files left behind)

```bash
PYTHONPATH=src medre smoke --json > bundle-smoke.json
# Exit code: 0 = pass, 1 = fail
```

### 1.2 Persistent Smoke (inspectable after exit)

```bash
PYTHONPATH=src medre smoke --storage-path /tmp/medre-smoke.db --json > bundle-smoke-persist.json
# After exit, the SQLite database at /tmp/medre-smoke.db can be inspected.
```

### 1.3 Failure Drills (all available drills)

```bash
for drill in renderer_failure adapter_permanent_failure \
  adapter_transient_failure capacity_rejection shutdown_rejection \
  replay_duplicate_risk degraded_live_health; do
  PYTHONPATH=src medre smoke --drill "$drill" \
    --storage-path /tmp/medre-smoke.db --json \
    >> bundle-drills.jsonl
done
```

### 1.4 Pre-Runtime Drills (config and startup failures)

```bash
for drill in bad_route_config all_adapters_build_fail \
  partial_degraded_startup all_adapters_start_fail; do
  PYTHONPATH=src medre smoke --drill "$drill" \
    --storage-path /tmp/medre-smoke.db --json \
    >> bundle-preruntime.jsonl
done
```

### 1.5 Evidence Command (single-command bundle)

```bash
# Basic bundle: config summary + route validation + diagnostics snapshot + storage
PYTHONPATH=src medre evidence --config my-bridge.toml --json > bundle-full.json

# Targeted: smoke a specific event through the pipeline
PYTHONPATH=src medre evidence --config my-bridge.toml --event <event_id> --json > bundle-event.json

# Include live health refresh (starts real adapters)
PYTHONPATH=src medre evidence --config my-bridge.toml --include-refresh-health --json > bundle-with-health.json
```

### 1.6 Post-Run Inspection

```bash
# Requires a previous run with --storage-path or [storage] backend = "sqlite"
PYTHONPATH=src medre inspect event <event_id> --config my-bridge.toml
PYTHONPATH=src medre inspect receipts --event <event_id> --config my-bridge.toml
PYTHONPATH=src medre inspect native-ref --adapter <name> --message <native_id> --config my-bridge.toml
PYTHONPATH=src medre inspect receipts --replay-run <run_id> --config my-bridge.toml
```


## 2. Command Reference

| Command | Storage | Starts adapters | Output | Exit codes |
|---------|---------|----------------|--------|------------|
| `medre smoke --json` | In-memory | Fake only | pass/fail JSON | 0=pass, 1=fail |
| `medre smoke --storage-path <db> --json` | SQLite | Fake only | pass/fail JSON + DB | 0=pass, 1=fail |
| `medre smoke --drill <name> --json` | In-memory | Fake only | Drill report JSON | 0=pass, 1=fail |
| `medre smoke --drill <name> --storage-path <db> --json` | SQLite | Fake only | Drill report JSON + DB | 0=pass, 1=fail |
| `medre evidence --config <path> --json` | Per config (memory or SQLite) | Fake only (or real with `--include-refresh-health`) | Full bundle JSON | 0=ok/partial, 2=config error |
| `medre evidence --config <path> --event <id> --json` | Per config (memory or SQLite) | No | Bundle with event/receipt lookup | 0=ok/partial, 2=config error |
| `medre evidence --config <path> --include-refresh-health --json` | Per config (memory or SQLite) | Yes (real or fake) | Full bundle + live health JSON | 0=ok/partial, 2=config error |
| `medre inspect event <id> --config <path>` | Opens SQLite (RO) | No | Event JSON | 0=found, 2=no SQLite |
| `medre inspect receipts --event <id> --config <path>` | Opens SQLite (RO) | No | Receipt array JSON | 0=found, 2=no SQLite |
| `medre inspect receipts --replay-run <id> --config <path>` | Opens SQLite (RO) | No | Receipt array JSON | 0=found, 2=no SQLite |
| `medre inspect native-ref --adapter <name> --message <id> --config <path>` | Opens SQLite (RO) | No | Ref JSON | 0=found, 2=no SQLite |
| `medre diagnostics --config <path>` | None | No | Build-time snapshot JSON | 0=ok, 2=config, 3=build |
| `medre diagnostics --refresh-health --config <path>` | None | Yes (real or fake) | Live health snapshot JSON | 0=ok, 2=config, 3=build, 4=startup |
| `medre run --config <path> --snapshot-on-shutdown` | Per config (SQLite or memory) | Yes (real or fake) | Logs + writes final JSON snapshot after graceful shutdown to `{state_dir}/shutdown-snapshot.json` | 0=clean shutdown, 2=config, 3=build, 4=startup |


## 3. Report Shapes

### 3.1 Smoke Report

```json
{
  "status": "pass",
  "evidence_level": "fake_bridge",
  "scenario_category": "smoke",
  "simulated": true,
  "command": "smoke",
  "commands_argv": ["medre", "smoke", "--json"],
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
  "native_refs": [
    {
      "adapter": "radio",
      "channel": "general",
      "native_message_id": "fake_123",
      "canonical_event_id": "evt_abc123"
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
  "snapshot": {
    "schema_version": "0.1.0",
    "lifecycle": { "runtime_state": "running" },
    "routes": { "stats": {} },
    "accounting": {}
  },
  "limitations": [
    "Fake adapters only \u2014 no real transport connectivity proven",
    "In-memory storage \u2014 no persistence or crash-recovery proof",
    "No live codec verification for real packet formats",
    "No reconnection resilience or retry-against-live proof",
    "Fire-and-forget delivery model for radio transports"
  ]
}
```

When the smoke **fails**, the report adds a `fail_reasons` array:

```json
{
  "status": "fail",
  "evidence_level": "fake_bridge",
  "scenario_category": "smoke",
  "simulated": true,
  "fail_reasons": [
    "No receipt with status 'sent'",
    "Accounting outbound_delivered < 1"
  ]
}
```

When `--storage-path` is provided, the report includes:

```json
{
  "storage_backend": "sqlite",
  "storage_path": "/tmp/medre-smoke.db"
}
```

### 3.2 Drill Report

```json
{
  "status": "pass",
  "evidence_level": "drill",
  "scenario_category": "drill",
  "simulated": true,
  "simulation_method": "failure_injection",
  "command": "smoke",
  "commands_argv": ["medre", "smoke", "--drill", "renderer_failure", "--json"],
  "commands_text": "medre smoke --drill renderer_failure --json",
  "drill_name": "renderer_failure",
  "timestamp": "2026-05-14T10:30:00+00:00",
  "config_source": "default",
  "storage_backend": "memory",
  "drill_steps": [
    { "step": "inject_unrenderable_event", "result": "ok" },
    { "step": "assert_renderer_failure_receipt", "result": "ok" }
  ],
  "event_id": "evt_xyz",
  "source_adapter": "bot",
  "target_adapters": [],
  "limitations": [
    "Drill uses fake adapters \u2014 no real transport failure proven",
    "Failure injection is synchronous and deterministic",
    "No background retry scheduler exercised",
    "No sustained failure or cascading failure proof"
  ]
}
```

### 3.3 Evidence Bundle Report

The `medre evidence` command produces a structured bundle with per-section
status. Each section has its own `status` (`"ok"`, `"partial"`, `"error"`,
`"skipped"`), `error` (string or null), and `data` (section-specific payload).

**Top-level fields:**

| Field | Type | Description |
|-------|------|-------------|
| `schema_version` | `int` | Bundle schema version (frozen at `1` during pre-release) |
| `status` | `str` | Overall: `"ok"`, `"partial"`, or `"error"` |
| `collected_at` | `str` | ISO-8601 UTC timestamp |
| `medre_version` | `str` | Installed package version |
| `config_source` | `str` | How the config file was found (`"cli_arg"`, `"xdg"`, etc.) |
| `runtime_started` | `bool` | `true` only when `--include-refresh-health` was used and runtime started |
| `sections` | `dict` | Grouped evidence, each with its own status |
| `errors` | `list[str]` | Flat list of bounded error strings across all sections |
| `limitations` | `list[str]` | Honest list of what the evidence does not prove |

**Sections:**

| Section | Populated when | Key data fields |
|---------|---------------|-----------------|
| `config_summary` | Always (if config loads) | `adapters`, `routes`, `limits`, `storage_backend`, `storage_path`, `paths`, `env_overrides_applied` |
| `route_validation` | Always (if config loads) | `route_count`, `route_enabled`, `valid`, `route_errors` |
| `diagnostics_snapshot` | Always (if config loads) | Full `build_runtime_snapshot` output (no adapter start, no I/O) |
| `live_health` | Only with `--include-refresh-health` | Full runtime snapshot with `health.live_health` populated; otherwise `status: "skipped"` |
| `storage` | When config uses `sqlite` backend and DB exists | `db_exists`, `db_path`, `event_count`, `receipt_count`, `event` (if `--event`), `replay_run_receipts` (if `--replay-run`), `incident_summary` (if `--event` and event has failed receipts) |

**incident_summary (within storage section):**

When `--event <event_id>` is provided and the event has failed delivery
receipts, the `storage` section includes an `incident_summary` object. This is
a compact incident classification derived from the event's receipt history,
using the same failure-kind vocabulary as `medre recover` and `medre trace`.

| Field | Type | Description |
|-------|------|-------------|
| `classification` | `dict[str, list]` | Failed targets grouped by recovery category: `"retryable"`, `"permanent"`, `"operational"`, `"unknown"` |
| `recommended_commands` | `list[str]` | Suggested `medre` commands for next steps, keyed to the present failure categories |
| `first_failure_kind` | `str or null` | The failure-kind of the earliest failed receipt (e.g. `"adapter_transient"`, `"renderer_failure"`); `null` if no failures |

Failure-kind values are the same set used by `medre trace event` and
`medre recover`: `"adapter_transient"`, `"adapter_permanent"`,
`"adapter_missing"`, `"renderer_failure"`, `"planner_failure"`,
`"capacity_rejection"`, `"shutdown_rejection"`, `"deadline_exceeded"`,
`"unknown"`. See [Event Tracing](event-tracing.md) for the full vocabulary.

**Minimal bundle (no `--include-refresh-health`, memory storage):**

```json
{
  "schema_version": 1,
  "status": "ok",
  "collected_at": "2026-05-14T10:30:00+00:00",
  "medre_version": "0.1.0",
  "config_source": "cli_arg",
  "runtime_started": false,
  "sections": {
    "config_summary": {
      "status": "ok",
      "error": null,
      "data": {
        "runtime_name": "my-bridge",
        "adapters": [
          {
            "adapter_id": "bot",
            "adapter_kind": "fake",
            "enabled": true,
            "transport": "matrix"
          }
        ],
        "routes": [
          {
            "route_id": "bot-to-radio",
            "source_adapters": ["bot"],
            "dest_adapters": ["radio"],
            "directionality": "source_to_dest",
            "enabled": true
          }
        ],
        "limits": {
          "max_inflight_deliveries": 100,
          "max_inflight_replay_events": 100,
          "delivery_acquire_timeout_seconds": 1.0,
          "shutdown_drain_timeout_seconds": 10.0
        },
        "storage_backend": "memory",
        "storage_path": null,
        "logging_level": "INFO",
        "env_overrides_applied": [],
        "paths": { "...": "resolved MedrePaths" }
      }
    },
    "route_validation": {
      "status": "ok",
      "error": null,
      "data": {
        "route_count": 1,
        "route_enabled": 1,
        "valid": true,
        "route_errors": [],
        "route_warnings": []
      }
    },
    "diagnostics_snapshot": {
      "status": "ok",
      "error": null,
      "data": { "...": "build_runtime_snapshot output" }
    },
    "live_health": {
      "status": "skipped",
      "error": null,
      "data": null,
      "note": "Use --include-refresh-health to populate this section"
    },
    "storage": {
      "status": "skipped",
      "error": null,
      "data": null,
      "note": "Storage backend is 'memory' \u2014 no persistent data to inspect"
    }
  },
  "errors": [],
  "limitations": [
    "Evidence is a point-in-time snapshot, not continuous monitoring",
    "Diagnostics snapshot reflects build-time state unless --include-refresh-health is used",
    "Storage section requires an existing initialised database",
    "Fake adapters report synthetic health, not real transport connectivity",
    "No sustained throughput, reconnection resilience, or load evidence"
  ]
}
```

**With `--include-refresh-health` (SQLite storage, runtime started):**

The `live_health` section transitions from `"skipped"` to `"ok"` (or `"partial"`)
with a full runtime snapshot including `health.live_health`. The `runtime_started`
top-level field becomes `true`. The `storage` section reflects SQLite counts.

```json
{
  "schema_version": 1,
  "status": "ok",
  "collected_at": "2026-05-14T10:30:00+00:00",
  "medre_version": "0.1.0",
  "config_source": "cli_arg",
  "runtime_started": true,
  "sections": {
    "config_summary": { "status": "ok", "...": "..." },
    "route_validation": { "status": "ok", "...": "..." },
    "diagnostics_snapshot": { "status": "ok", "...": "..." },
    "live_health": {
      "status": "ok",
      "error": null,
      "data": {
        "health": {
          "scope": "live",
          "live_refresh": true,
          "live_health": {
            "poll_count": 1,
            "runtime_health": "healthy",
            "adapters": [
              {
                "adapter_id": "bot",
                "health": "healthy",
                "fake_or_live": "fake",
                "poll_timestamp_wall": "2026-05-14T10:30:00Z",
                "error": null
              }
            ]
          }
        },
        "startup": { "...": "..." }
      }
    },
    "storage": {
      "status": "ok",
      "error": null,
      "data": {
        "db_exists": true,
        "db_path": "/opt/medre/state/medre.sqlite",
        "event_count": 42,
        "receipt_count": 38,
        "event": null,
        "native_refs_for_event": null,
        "receipt_count": 38,
        "replay_run_receipts": null
      }
    }
  },
  "errors": [],
  "limitations": ["..."]
}
```

**Config load failure (entire bundle is error):**

```json
{
  "schema_version": 1,
  "status": "error",
  "collected_at": "2026-05-14T10:30:00+00:00",
  "config_source": null,
  "errors": ["TOML parse error at line 5: ..."],
  "limitations": ["..."],
  "medre_version": "0.1.0",
  "runtime_started": false,
  "sections": {},
  "status": "error"
}
```

**With `--event <event_id>` (targets a specific stored event):**

The `storage` section populates `event` with the canonical event data and
`native_refs_for_event` with any native message ref mappings for that event.
If the event is not found, the storage section status is `"partial"`.

When the event has delivery receipts, the `storage` section also includes
`trace_event` — the same enriched trace report produced by `medre trace event`.
When `--replay-run <run_id>` is provided, `trace_replay` contains the enriched
replay trace report produced by `medre trace replay`.

**Caveat:** Traceability is not deduplication. The trace sections show all
receipts including duplicates from replay, but cannot tell you which delivery
actually reached the remote side. BEST_EFFORT sends real messages. There is
no final ACK guarantee for radio transports. There is no active retry
scheduler. Runtime events and counters are process-local.

The evidence bundle includes replay receipts alongside live receipts when
applicable. The `replay_run_id` field distinguishes which replay run produced
which receipts. Multiple BEST_EFFORT runs produce duplicate receipts — the
evidence bundle reflects actual delivery attempts, not a deduplicated view.

```json
{
  "storage": {
    "status": "ok",
    "error": null,
    "data": {
      "db_exists": true,
      "db_path": "/opt/medre/state/medre.sqlite",
      "event_count": 42,
      "receipt_count": 38,
      "event": {
        "event_id": "evt_abc123",
        "source_adapter": "bot",
        "event_kind": "message.text",
        "payload": { "text": "hello" },
        "created_at": "2026-05-14T10:30:00Z"
      },
      "native_refs_for_event": [
        {
          "native_message_id": "fake_123",
          "native_channel_id": "general",
          "canonical_event_id": "evt_abc123",
          "adapter": "radio",
          "direction": "outbound"
        }
      ],
      "trace_event": {
        "event": { "...": "canonical event data" },
        "receipts": [
          {
            "receipt_id": "rcpt_001",
            "target_adapter": "radio",
            "route_id": "bot-to-radio",
            "status": "sent",
            "failure_kind": null,
            "attempt_number": 1,
            "parent_receipt_id": null,
            "source": "live",
            "replay_run_id": null,
            "created_at": "2026-05-14T10:30:00.050Z"
          },
          {
            "receipt_id": "rcpt_r1",
            "target_adapter": "radio",
            "route_id": "bot-to-radio",
            "status": "sent",
            "failure_kind": null,
            "attempt_number": 1,
            "parent_receipt_id": null,
            "source": "replay",
            "replay_run_id": "replay_xyz789",
            "created_at": "2026-05-14T11:00:00Z"
          }
        ],
        "native_refs": [
          {
            "native_message_id": "fake_123",
            "native_channel_id": "general",
            "canonical_event_id": "evt_abc123",
            "adapter": "radio",
            "direction": "outbound"
          }
        ],
        "timeline": [
          {
            "timestamp": "2026-05-14T10:30:00.000Z",
            "phase": "ingestion",
            "description": "Event stored from source adapter 'bot'"
          },
          {
            "timestamp": "2026-05-14T10:30:00.050Z",
            "phase": "delivery",
            "description": "Delivery to adapter 'radio': status=sent"
          },
          {
            "timestamp": "2026-05-14T11:00:00.000Z",
            "phase": "replay",
            "description": "Event evt_abc123 re-delivered via replay run replay_xyz789 to adapter 'radio': status=sent"
          }
        ]
      },
      "trace_replay": null,
      "replay_run_receipts": null
    }
  }
}
```

**With `--replay-run <run_id>` (targets a specific replay run):**

The `storage` section populates `trace_replay` with the enriched replay trace
report — the same data produced by `medre trace replay <run_id>`. The
`replay_run_receipts` array lists all receipts for that run. Each receipt has
`source='replay'` and the matching `replay_run_id`.

```json
{
  "storage": {
    "status": "ok",
    "error": null,
    "data": {
      "db_exists": true,
      "db_path": "/opt/medre/state/medre.sqlite",
      "event_count": 42,
      "receipt_count": 45,
      "event": null,
      "native_refs_for_event": null,
      "trace_event": null,
      "trace_replay": {
        "replay_run_id": "replay_xyz789",
        "receipts": [
          {
            "receipt_id": "rcpt_r1",
            "event_id": "evt_abc123",
            "target_adapter": "radio",
            "route_id": "bot-to-radio",
            "status": "sent",
            "source": "replay",
            "replay_run_id": "replay_xyz789",
            "attempt_number": 1,
            "created_at": "2026-05-14T11:00:00Z"
          }
        ],
        "summary": {
          "total_receipts": 1,
          "sent": 1,
          "failed": 0,
          "events_covered": 1,
          "duplicate_risk": "possible — check source='replay' receipts against live receipts for same events"
        },
        "timeline": [
          {
            "timestamp": "2026-05-14T11:00:00.000Z",
            "phase": "replay",
            "description": "Event evt_abc123 re-delivered via replay run replay_xyz789 to adapter 'radio': status=sent"
          }
        ]
      },
      "replay_run_receipts": [
        {
          "receipt_id": "rcpt_r1",
          "event_id": "evt_abc123",
          "target_adapter": "radio",
          "status": "sent",
          "source": "replay",
          "replay_run_id": "replay_xyz789"
        }
      ]
    }
  }
}
```

See [Event Tracing](event-tracing.md) for the full trace report shapes and
[Replay Operation](replay-operation.md) for replay receipt interpretation.


## 4. Interpreting the Bundle

### 4.1 Status Values

| Status | Meaning | Operator action |
|--------|---------|-----------------|
| `ok` / `pass` | All criteria met at the reported evidence level | Proceed to live runtime with caution. The bundle proves pipeline correctness, not live connectivity. |
| `partial` | Some adapters/routes/drills failed but the runtime stayed up | Inspect `fail_reasons` and per-adapter `.error` fields. Individual drill failures may be acceptable depending on the scenario (e.g., a degraded health drill expects failure). |
| `error` / `fail` | A required criterion was not met | Do not proceed to live runtime. Inspect `fail_reasons`, fix the config or environment, re-collect the bundle. |

### 4.2 Why `--include-refresh-health` Starts Adapters

The `--include-refresh-health` flag causes `medre evidence` to build the
runtime, **start all enabled adapters**, poll each adapter's `health_check()`
once, capture the live health snapshot, and then stop the runtime cleanly.

With fake adapters this is trivial. With real adapters, this opens real
connections (Matrix TCP to homeserver, Meshtastic serial/TCP to local node,
etc.). The purpose is to verify real transport connectivity as part of the
evidence bundle.

The snapshot is captured **before** the runtime stops, so `runtime_state`
appears as `"running"` in the output.

`startup_health` (from boot summary) is a separate frozen value from startup
time. `live_health` reflects the moment of the refresh. They can differ.

### 4.3 What `sent` Means Per Transport

| Transport | `sent` means | Remote receipt |
|-----------|-------------|----------------|
| Matrix | Homeserver accepted the event (event_id returned) | Not confirmed per-recipient |
| Meshtastic | Local node queued the packet for LoRa transmission | Unknown. Fire-and-forget. |
| MeshCore | Local node queued the packet | Unknown. Fire-and-forget. |
| LXMF | Local LXMRouter accepted for propagation | Eventual, seconds to hours. |

### 4.4 Inspect Output Interpretation

| `medre inspect` output | What to look for |
|------------------------|-------------------|
| `event` — source_adapter, event_kind, payload | Confirms the event was stored correctly before delivery |
| `receipts` — status, failure_kind, attempt_number, parent_receipt_id | Traces the full delivery lifecycle. `attempt_number > 1` with `parent_receipt_id` chain indicates retry. |
| `receipts` — route_id | Identifies which route triggered the delivery |
| `native-ref` — native_message_id, canonical_event_id | Maps transport-native IDs to canonical events for cross-referencing |
| `receipts --replay-run` — source="replay", replay_run_id | Distinguishes replay deliveries from live. Multiple entries for the same event across different `replay_run_id` values = multiple BEST_EFFORT runs. The evidence bundle reflects all actual delivery attempts. |


## 5. Bug Report Artifact

When filing a bug against MEDRE evidence, delivery, or runtime behavior,
attach the following:

1. **`medre evidence --config <path> --json`** output. If the `evidence`
   subcommand is unavailable in the current build, use the individual commands
   from §1 (smoke, drills, diagnostics) and concatenate the outputs.
2. **`medre evidence --config <path> --include-refresh-health --json`** output
   if the issue involves adapter health or connectivity.
3. **`medre inspect` outputs** showing the specific receipt, event, or ref in
   question (if using SQLite storage).
4. **Config file** with secrets redacted (`access_token`, passwords, etc.).
5. **`medre version`** output.
6. **`medre paths`** output (for path-related issues).

Name the files descriptively: `bundle-<date>.json`,
`bundle-health-<date>.json`, `inspect-receipts-<event_id>.json`.


## 6. Available Drills

### 6.1 Runtime Failure Drills

These drills exercise the delivery pipeline with injected failures:

| Drill name | What it proves |
|-----------|---------------|
| `renderer_failure` | Unhandled event kind produces `RENDERER_FAILURE` receipt, no retry |
| `adapter_permanent_failure` | Non-recoverable adapter error produces `ADAPTER_PERMANENT` receipt, no retry |
| `adapter_transient_failure` | Transient error triggers retry with `ADAPTER_TRANSIENT` receipt chain |
| `capacity_rejection` | Delivery capacity exhaustion produces `delivery_capacity_exceeded` |
| `shutdown_rejection` | In-flight deliveries during shutdown produce `delivery_rejected_shutdown` |
| `replay_duplicate_risk` | BEST_EFFORT replay produces duplicate receipts per run |
| `degraded_live_health` | Adapters can report degraded/failed health without runtime exit |

### 6.2 Pre-Runtime Drills

These drills exercise config validation, build, and startup classification.
Each drill **exits 0** when the expected failure is correctly observed. The
drill report documents what exit code and error the runtime would produce
if run independently — the drill itself does not exit 2, 3, or 4.

| Drill name | What it proves |
|-----------|---------------|
| `bad_route_config` | Unknown adapter ref in route causes `RouteValidationError`; the runtime would exit with `EXIT_CONFIG` (code 2) |
| `all_adapters_build_fail` | Total build failure causes all adapters to fail construction; the runtime would exit with `EXIT_BUILD` (code 3) |
| `partial_degraded_startup` | Partial adapter start allows the runtime to enter `RUNNING` with degraded health (exit 0) |
| `all_adapters_start_fail` | Total startup failure prevents `RUNNING` state; the runtime would exit with `EXIT_STARTUP` (code 4) |

Run pre-runtime drills with:

```bash
PYTHONPATH=src medre smoke --drill <drill_name> --storage-path /tmp/medre-smoke.db --json
```


## 7. Caveats

1. **No final ACK.** Radio transports (Meshtastic, MeshCore) are
   fire-and-forget. `sent` means the local node accepted the packet, not that
   any remote node received it. Matrix `sent` means homeserver acceptance, not
   per-recipient read. No transport provides end-to-end delivery confirmation.

2. **No replay deduplication.** BEST_EFFORT replay produces new outbound
   messages each run. Multiple replays of the same event produce duplicates.
   This is by design.

3. **No active restart or supervision.** No per-adapter restart. No
   auto-remediation. Failed adapters stay failed until the operator restarts
   the entire runtime. `--include-refresh-health` is a manual one-shot check,
   not ongoing monitoring.

4. **Docker / local is not live-network.** Docker SDK-boundary tests prove
   real SDK lifecycle against containerized localhost services. They do not
   prove live network behavior, sustained throughput, reconnection resilience,
   or multi-hop delivery.

 5. **Counters reset on restart.** `capacity_rejections`, `outbound_failed`,
    `RouteStats`, `CapacityController` gauges are process-local. They reset to
    zero on every startup. Use `medre run --snapshot-on-shutdown` to capture
    these values to disk before the process exits.

6. **`medre inspect` requires SQLite.** `medre inspect` subcommands exit with
    code 2 if the config uses `backend = "memory"` or the database file does not
    exist.

7. **Shutdown snapshot is process-local.** The `--snapshot-on-shutdown` output
    captures runtime events and counters at shutdown time, but these are
    observations of process-local state. Runtime events do not survive the
    process and are not in SQLite. Replay is manual and duplicate-risky.

8. **Pre-beta.** Exit codes, receipt schemas, drill names, and report shapes
    may change before beta. Always verify against the current code.


## 8. Cross-References

- [Fake Bridge Smoke Runbook](fake-bridge-smoke-runbook.md) — smoke command
  details, PASS criteria, persistence semantics.
- [Bridge Failure Drills](bridge-failure-drills.md) — per-failure drill
  interpretation and inspect follow-up.
- [Fake Bridge Evidence Criteria](fake-bridge-evidence-criteria.md) —
  assertion-level evidence criteria per flow type.
- [Runtime Operation](runtime-operation.md) — diagnostics, inspect, exit codes,
  persistence semantics.
- [Bridge Operation](bridge-operation.md) — delivery-state discipline,
  per-transport semantics, persistence of bridge state.
- [Integration Testing](integration-testing.md) — Docker SDK-boundary tier
  documentation.
- [Event Tracing](event-tracing.md) — operator guide for tracing events through
  pipeline lifecycle, timeline reports, SQL queries.
- [Replay Operation](replay-operation.md) — replay modes, command shape, receipt
  interpretation, duplicate risk assessment.
- [Bridge Recovery](bridge-recovery.md) — crash recovery procedures, orphan
  detection, recovery decision tree.
