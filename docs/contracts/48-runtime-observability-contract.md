# Contract 48 — Runtime Observability Contract

> **Status:** Active
> **Classification:** Normative
> **Authority:** Authoritative specification for MEDRE runtime logging, diagnostics, metrics, and observability policies
> **Last reviewed:** 2026-05-24
>
> **Scope:** Authoritative specification for MEDRE runtime logging, diagnostics, metrics, and observability policies.
> **Audience:** Runtime builders, adapter authors, test harnesses, operators.
> **References:** Contract 46 (Runtime Storage and Path Model), Contract 47 (Runtime Assembly), Contract 29 (Diagnostics).

Every agent or document that references MEDRE runtime logging, structured log output, adapter lifecycle events, or diagnostics snapshots must defer to this contract.

## 1. Structured Adapter Lifecycle Logs

### 1.1 Consistent Fields

All adapter lifecycle log entries must include:

- `adapter_id` — the adapter's unique identifier.
- `transport` — the transport type (`matrix`, `meshtastic`, `meshcore`, `lxmf`).

These fields appear on every lifecycle log message: start, stop, connect, disconnect, reconnect, health change, and failure events.

### 1.2 Lifecycle Events

The following adapter lifecycle events are always logged at INFO level:

| Event                  | When                             | Required Fields                          |
| ---------------------- | -------------------------------- | ---------------------------------------- |
| `adapter_starting`     | Before adapter start begins      | `adapter_id`, `transport`                |
| `adapter_started`      | After adapter start succeeds     | `adapter_id`, `transport`, `duration_ms` |
| `adapter_stopping`     | Before adapter stop begins       | `adapter_id`, `transport`                |
| `adapter_stopped`      | After adapter stop completes     | `adapter_id`, `transport`, `duration_ms` |
| `adapter_connected`    | Transport connection established | `adapter_id`, `transport`                |
| `adapter_disconnected` | Transport connection lost        | `adapter_id`, `transport`                |
| `adapter_failed`       | Unrecoverable adapter failure    | `adapter_id`, `transport`, error summary |

Recoverable errors (transient send failures, reconnect attempts) are logged at WARNING level. Internal state transitions are logged at DEBUG level.

## 2. Runtime Startup Logs

### 2.1 Startup Summary

At INFO level, the runtime emits:

1. **"Starting N adapters"** — before the first adapter starts, where N is the count of enabled adapters.
2. **Per-adapter timing** — each adapter's start duration is logged.
3. **Final summary** — after all adapters have attempted start:
   - Count of successfully started adapters.
   - Count of failed adapters (if any).
   - Total assembly duration.

### 2.2 Startup Log Sequence Example

```console
INFO  medre.runtime: Starting 3 adapters
INFO  medre.adapters.matrix.bot1: adapter_starting transport=matrix adapter_id=bot1
INFO  medre.adapters.matrix.bot1: adapter_started transport=matrix adapter_id=bot1 duration_ms=234
INFO  medre.adapters.matrix.bot2: adapter_starting transport=matrix adapter_id=bot2
INFO  medre.adapters.matrix.bot2: adapter_started transport=matrix adapter_id=bot2 duration_ms=189
INFO  medre.adapters.meshtastic.radio: adapter_starting transport=meshtastic adapter_id=radio
INFO  medre.adapters.meshtastic.radio: adapter_started transport=meshtastic adapter_id=radio duration_ms=102
INFO  medre.runtime: Assembly complete: 3/3 adapters started in 525ms
```

## 3. Runtime Shutdown Logs

### 3.1 Shutdown Sequence

At INFO level, the runtime emits per-adapter stop entries in reverse start order:

```console
INFO  medre.runtime: Shutting down 3 adapters
INFO  medre.adapters.meshtastic.radio: adapter_stopping transport=meshtastic adapter_id=radio
INFO  medre.adapters.meshtastic.radio: adapter_stopped transport=meshtastic adapter_id=radio duration_ms=45
INFO  medre.adapters.matrix.bot2: adapter_stopping transport=matrix adapter_id=bot2
INFO  medre.adapters.matrix.bot2: adapter_stopped transport=matrix adapter_id=bot2 duration_ms=30
INFO  medre.adapters.matrix.bot1: adapter_stopping transport=matrix adapter_id=bot1
INFO  medre.adapters.matrix.bot1: adapter_stopped transport=matrix adapter_id=bot1 duration_ms=28
INFO  medre.runtime: Shutdown complete in 103ms
```

### 3.2 Shutdown Timeout Warning

If an adapter does not stop within the global shutdown timeout, a WARNING is logged:

```console
WARNING  medre.runtime: adapter_id=radio did not stop within shutdown_timeout_seconds=10
```

## 4. Startup Duration Metrics

### 4.1 Per-Adapter Timing

Each adapter's start duration is recorded in milliseconds (`duration_ms`). This metric appears in:

- The adapter's `adapter_started` log entry.
- The diagnostics snapshot (see § 7).

### 4.2 Total Assembly Duration

The total runtime assembly duration is measured from the first adapter start to the last adapter completion (success or failure). This appears in the final summary log.

## 5. Reconnect and Retry State Visibility

### 5.1 Reconnect Events

Reconnect attempts are visible at the runtime layer. When a session enters its reconnect loop (see Contract 31 § 3.1):

- First reconnect attempt: logged at WARNING level with `adapter_id`, `transport`, `reconnect_attempt`.
- Subsequent attempts: logged at WARNING level.
- Successful reconnect: logged at INFO level with total reconnect duration.
- Exhausted retries (max 10): logged at ERROR level.

### 5.2 Retry State in Diagnostics

The current reconnect state is visible in the adapter diagnostics snapshot:

- `reconnecting: bool`
- `reconnect_attempts: int` (bounded to max 10)

These fields are specified in Contract 29 § 3.

## 6. Adapter Health Snapshots

### 6.1 Health States

Each adapter exposes a health state via `health_check()`. The possible states:

| State      | Meaning                                                                           |
| ---------- | --------------------------------------------------------------------------------- |
| `healthy`  | Adapter is connected and operating normally.                                      |
| `degraded` | Adapter is connected but experiencing transient errors (retries, slow responses). |
| `failed`   | Adapter is in a non-recoverable failure state. Not connected.                     |
| `stopped`  | Adapter has been stopped and is no longer running.                                |

### 6.2 Health State Transitions

Health state transitions are logged at INFO level:

```console
INFO  medre.adapters.matrix.bot1: health_change transport=matrix adapter_id=bot1 old=starting new=healthy
INFO  medre.adapters.meshtastic.radio: health_change transport=meshtastic adapter_id=radio old=healthy new=degraded
```

### 6.3 Diagnostic Snapshots

Diagnostics snapshots are per-adapter. Each adapter's `diagnostics()` method returns a plain dict containing transport-specific state. Cross-adapter isolation is maintained: one adapter's diagnostics never include data from another adapter.

See Contract 29 for the complete diagnostics schema.

## 7. Strict No-Secrets Policy

### 7.1 Prohibited Content

The following must **never** appear in logs, diagnostics output, or error messages:

| Category          | Examples                                                           |
| ----------------- | ------------------------------------------------------------------ |
| Access tokens     | Matrix `syt_...` tokens, OAuth tokens, API keys                    |
| Device keys       | Matrix device keys, ed25519/Curve25519 key data                    |
| Crypto material   | Olm/Megolm session keys, pickle data, key bytes                    |
| Raw SDK dumps     | nio sync responses, Meshtastic protobuf objects, Reticulum objects |
| Protobuf objects  | Serialized or deserialized Meshtastic protobuf payloads            |
| Reticulum objects | RNS Identity, Link, or Destination object representations          |

### 7.2 Safe Representations

When referencing secrets or sensitive data:

- **Access tokens:** Never logged. If a token-related error occurs, log the error type only (e.g., "authentication failed for adapter_id=bot1").
- **Device IDs:** Log the device ID string (e.g., `ABCD1234`) but never device key material.
- **Connection endpoints:** Hostnames and ports are safe to log. Full URLs with embedded tokens are not.
- **Error messages:** SDK error messages must be sanitized before logging. If the SDK's error string may contain sensitive data, log only the error type.

### 7.3 Enforcement

This policy applies to all log levels, including DEBUG. Turning up log verbosity must not leak secrets. Adapter authors must ensure that transport SDK debug output is not forwarded to MEDRE logs without sanitization.

## 8. Log Levels

### 8.1 Level Assignment

| Level     | Purpose                                                               | Examples                                                                |
| --------- | --------------------------------------------------------------------- | ----------------------------------------------------------------------- |
| `INFO`    | Lifecycle events, state transitions, startup/shutdown sequences       | Adapter started, connected, stopped, health change                      |
| `DEBUG`   | Internal state, message processing details, SDK interaction summaries | Codec decode details, routing decisions, session state                  |
| `WARNING` | Recoverable errors, degraded conditions, retry attempts               | Transient send failure, reconnect attempt, slow response                |
| `ERROR`   | Failures that affect adapter operation                                | Adapter start failure, exhausted retries, unrecoverable transport error |

### 8.2 CRITICAL Usage

`CRITICAL` is not used by MEDRE components. Process-level failures (OOM, segfault) are outside MEDRE's logging scope.

## 9. Structured Logger Names

### 9.1 Naming Convention

Logger names follow a hierarchical dotted convention:

| Logger                                    | Scope                                               |
| ----------------------------------------- | --------------------------------------------------- |
| `medre.runtime`                           | Runtime assembly, startup, shutdown, global events  |
| `medre.adapters.{transport}.{adapter_id}` | Per-adapter lifecycle and transport events          |
| `medre.cli`                               | CLI command execution, argument parsing             |
| `medre.core.{module}`                     | Core library components (codec, event bus, routing) |

### 9.2 Examples

```text
medre.adapters.matrix.bot1
medre.adapters.matrix.bot2
medre.adapters.meshtastic.longfast
medre.adapters.meshtastic.shortturbo
medre.adapters.lxmf.local
medre.adapters.meshcore.radio
medre.runtime
medre.cli
```

### 9.3 Purpose

Structured logger names enable:

- **Per-adapter log filtering:** Filter to a single adapter by logger name.
- **Transport-level filtering:** Filter all Matrix adapters with `medre.adapters.matrix.*`.
- **Runtime vs. adapter separation:** Distinguish runtime lifecycle from adapter events.
- **CLI vs. runtime separation:** Distinguish CLI command output from runtime events.

## 10. Log Output

### 10.1 Log File Location

All log output is written to a single global log file:

```json
{state}/logs/medre.log
```

See Contract 46 § 3.2.

### 10.2 Per-Adapter Log Files

Per-adapter log files do not exist today. This is a future consideration. When introduced, per-adapter log files must not replace the global log file — the global log remains the authoritative source for runtime-wide observability.

### 10.3 Log Format

The log format is controlled by the `[logging]` configuration section:

- `format = "text"` — human-readable, suitable for terminals and development.
- `format = "json"` — structured JSON, suitable for log aggregation and production monitoring.

Both formats include the logger name, level, timestamp, and message. Structured fields (`adapter_id`, `transport`, `duration_ms`) are included as key-value pairs in both formats.

## 11. Observability Invariants

1. **Adapter lifecycle events are never silent.** Every start, stop, connect, disconnect, and failure is logged.
2. **No secrets in any log level.** DEBUG logs are as safe as INFO logs.
3. **Logger names are deterministic.** The same configuration always produces the same logger names.
4. **Diagnostics are read-only.** Calling `diagnostics()` never changes adapter state.
5. **Cross-adapter isolation.** One adapter's log output never includes another adapter's data.
6. **Shutdown is always logged.** Even if shutdown is triggered by a signal, the shutdown sequence is logged before exit.
