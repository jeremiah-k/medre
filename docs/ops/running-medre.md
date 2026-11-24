# Running MEDRE

Starting, stopping, and operating the MEDRE runtime — lifecycle, exit codes, health, shutdown, logs, and what to expect while it runs.

## Starting MEDRE

```bash
medre run --config config.yaml
```

The runtime loads the config, validates it, creates required directories, and starts all enabled adapters. Adapters start in deterministic order: sorted by `(transport, adapter_id)`.

To verify config without starting:

```bash
medre config check
```

To preview the expanded route topology (route legs, `context_map`
expansion, origin-label provenance, fan-in decisions) without starting
any transport:

```bash
medre routes plan --config config.yaml
```

`medre routes plan` is an offline dry-run — no adapter is started, no
SDK is imported, no network or hardware I/O is performed. Run it before
`medre run` to confirm the router will build the shape you expect,
especially for `context_map` routes whose per-context legs are
otherwise invisible in `routes validate` / `routes topology`.

### Expected Startup Output

Startup prints one minimal start line, per-adapter progress lines, then —
once every adapter has settled — one consolidated banner followed by a
single readiness line:

```console
2026-10-18T20:00:00-0500 [INFO] medre: MEDRE starting (bridge) with 2 adapter(s): matrix, radio
2026-10-18T20:00:00-0500 [INFO] medre.runtime.app: Starting adapter matrix.matrix
2026-10-18T20:00:00-0500 [INFO] medre.runtime.app: Starting adapter meshtastic.radio
2026-10-18T20:00:01-0500 [INFO] medre.runtime.app: Adapter matrix.matrix started in 312ms (health=healthy)
2026-10-18T20:00:01-0500 [INFO] medre.runtime.app: Adapter meshtastic.radio started in 145ms (health=healthy)
2026-10-18T20:00:01-0500 [INFO] medre: MEDRE bridge — startup (2 adapter(s))
2026-10-18T20:00:01-0500 [INFO] medre:   Config:  config.yaml
2026-10-18T20:00:01-0500 [INFO] medre:   State:   /var/lib/medre
2026-10-18T20:00:01-0500 [INFO] medre:   Core:    medre <installed-version> (pypi)
2026-10-18T20:00:01-0500 [INFO] medre:   Python:  <python-version> (Linux x86_64)
2026-10-18T20:00:01-0500 [INFO] medre:   Adapter: matrix <installed-version> (github:jeremiah-k/medre-adapter-matrix@<commit>)
2026-10-18T20:00:01-0500 [INFO] medre:   Adapter: meshtastic <installed-version> (github:jeremiah-k/medre-adapter-meshtastic@<commit>)
2026-10-18T20:00:01-0500 [INFO] medre:   Limits:  max_inflight_deliveries=100, max_inflight_replay_events=100, drain_timeout=10s
2026-10-18T20:00:01-0500 [INFO] medre:
2026-10-18T20:00:01-0500 [INFO] medre: matrix.matrix      ready  312ms
2026-10-18T20:00:01-0500 [INFO] medre:   user id @bot:example.com · device id <device>
2026-10-18T20:00:01-0500 [INFO] medre: meshtastic.radio   ready  145ms
2026-10-18T20:00:01-0500 [INFO] medre:   node name Ridge Relay · firmware 2.7.15
2026-10-18T20:00:01-0500 [INFO] medre:
2026-10-18T20:00:01-0500 [INFO] medre:   Routes:  1 enabled, 0 disabled (1 configured)
2026-10-18T20:00:01-0500 [INFO] medre:   Routing: 2 live-eligible, 0 startup-blocked (2 build-registered)
2026-10-18T20:00:01-0500 [INFO] medre:   Events:  71 persisted
2026-10-18T20:00:01-0500 [INFO] medre:   Storage: sqlite (/var/lib/medre/medre.sqlite)
2026-10-18T20:00:01-0500 [INFO] medre: All adapters ready — MEDRE runtime running
```

The banner renders after every adapter has settled, so adapter blocks
report confirmed facts rather than startup previews. Each adapter block
carries the instance name, its status (`ready`, `degraded`, `failed`, or
`build failed`) and startup attempt duration (where a start was attempted),
with the adapter's startup-info facts packed beneath in the shared
`label value · label value` grammar;
last-known cached facts keep their `(cached)` marker, and suppressed
rows keep the `… +N more` marker. Degraded and failed blocks render a
reason line. A failed start keeps its attempt duration, excluding cleanup;
a build failure has no startup duration. Failed instances may show last-known
cached facts, which do not imply connectivity or readiness. The final line
claims full readiness only when every attempted adapter is ready; otherwise
it reports the honest count, for
example `MEDRE runtime running — 1/2 adapter(s) ready, 1 failed`.
If the banner cannot be rendered, the CLI logs a warning and continues running
until shutdown.

This abbreviated text example omits some component logs.
The `Core`, `Python` and `Adapter` lines report the running installation:
adapter entries show installed versions and a compact provenance label —
`github:owner/repo@<commit>` (or `git:host/path@<commit>` for other hosts,
`hg:`/`svn:` revisions), `pypi` when no direct URL is recorded, `editable:`/
`local:` for local directories, or `url:` for a direct source location.
Community distributions appear with their normalized
package name in brackets. This is an inventory, not a list of enabled adapters
or proof of installed bytes or qualification. Unreadable metadata yields `?`,
`unknown`, or an incomplete-inventory notice without stopping startup.
URL userinfo, queries and fragments are redacted, but paths and hostnames may
be private; review logs before sharing. See the
[package-management spec](../spec/package-management.md#startup-installation-summary).
Adapter startup durations depend on the SDK and connection. `Routes` counts
configured routes; `Routing` separates current live eligibility from
build-time registration (the expanded-registration count itself logs at
DEBUG).

### Startup Checklist

| Element          | Where to look                                                                      | Healthy sign           | Problem sign                        |
| ---------------- | ---------------------------------------------------------------------------------- | ---------------------- | ----------------------------------- |
| Adapter count    | Console first line                                                                 | Matches enabled config | Unexpected enabled count            |
| Build failures   | Console `Build failures (N)` block                                                 | No block               | `✗` entries present                 |
| Routes           | Banner `Routes:` line                                                              | Expected count enabled | Zero enabled or errors              |
| Storage backend  | Banner `Storage:` line                                                             | `sqlite`               | `memory` (no persistence)           |
| Limits           | Banner `Limits:` line                                                              | As configured          | Unexpected defaults                 |
| Per-adapter logs | Banner adapter blocks (`ready`/`degraded`/`failed` tags with durations)            | All `ready`            | `failed` blocks or `degraded` tags  |
| Runtime summary  | Final line: `All adapters ready — MEDRE runtime running`, or the N-of-M count form | All adapters ready     | N-of-M wording with degraded/failed |

### Degraded Startup

If at least one adapter starts but others fail, the runtime enters `RUNNING` with degraded health and continues operating. Route enforcement after startup is scope- and reason-aware:

- **All targets of a route failed to start (LIVE)** — the route is removed from the router; planning into never-started adapters would dead-letter per event.
- **A route's source adapter failed to start (LIVE)** — the route stays registered. Routing stored canonical work keys off the event's recorded source adapter, not a live connection: already-admitted durable ingress still reaches surviving targets, and a source that never started cannot deliver fresh live ingress anyway. The route's `SKIPPED` readiness entry remains visible.
- **Replay scope** — startup skips never remove routes; replay executes stored events explicitly, and unavailable targets fail per-target in the replay result rather than collapsing into a false `no-route` outcome.
- **Some targets survive** — the route stays registered (DEGRADED) with honest per-target outcomes.

The retry and durable-ingress workers activate only after every adapter has settled, so due retry work is never claimed against an adapter that is still starting.

When the opt-in `relay-only` content-retention policy is activated, the bounded collection worker joins them against the same storage owner; `medre storage retention status` reports its activation epoch and heartbeat freshness. See [privacy and message storage](message-storage.md).

Diagnostic surfaces for degraded startup:

- **Console output** — logs which adapters started and which failed.
- **`startup.boot_summary`** — carries `startup_outcome: "partial"`, counts of started/failed adapters, lists of failed adapter IDs.
- **`startup.build_failures`** — bounded list of construction failures.
- **`routes.startup_readiness`** — shows routes that are degraded or skipped.
- **`diagnostics.runtime_events`** — event buffer recording adapter failures and route skips.

## Exit Codes

| Code | Constant       | Meaning                                                                                                                                            |
| ---- | -------------- | -------------------------------------------------------------------------------------------------------------------------------------------------- |
| 0    | `EXIT_OK`      | Successful run and clean shutdown.                                                                                                                 |
| 2    | `EXIT_CONFIG`  | Config not found, YAML parse error, validation error, or no adapters enabled.                                                                      |
| 3    | `EXIT_BUILD`   | Runtime build failure — missing SDK, invalid storage path, or adapter construction error. Exits with this code only if all adapters fail to build. |
| 4    | `EXIT_STARTUP` | Total startup failure — zero adapters started successfully.                                                                                        |

Degraded startup does not exit — the runtime continues with `DEGRADED` health.

### Exit Codes by Command

| Command                              | Config error | Build error | Startup error |
| ------------------------------------ | ------------ | ----------- | ------------- |
| `medre run`                          | 2            | 3           | 4             |
| `medre diagnostics`                  | 2            | 3           | n/a           |
| `medre diagnostics --refresh-health` | 2            | 3           | 4             |
| `medre config check`                 | 2            | n/a         | n/a           |
| `medre routes validate`              | 2            | n/a         | n/a           |

All commands print a human-readable error to stderr before exiting nonzero.

## Current health after startup

The N-of-M form (`MEDRE runtime running — 1/2 adapter(s) ready, …`) confirms
only that adapter owners started and settled; the all-ready form
(`All adapters ready — MEDRE runtime running`) also reflects healthy
startup observations. Matrix keeps its first sync inside
startup for up to `initial_sync_timeout_seconds` after room joins (45 seconds
by default), logging the wait as startup progress. An acknowledgement within
that budget permits healthy startup without an initial degradation warning.
A native sync failure or reconnect is reported immediately. On wait expiry,
Matrix starts with its actual degraded health and keeps native recovery active.
Set the wait to `0` for immediate classification. The runtime's per-adapter
startup deadline covers login, joins, and the wait together; allow a larger
runtime budget when needed. Later native notifications update current health automatically. Look for
`Adapter <id> health changed: degraded -> healthy` and
`Runtime current health: healthy`; a fresh manual probe is not required just
to acknowledge native recovery.

The startup record remains a historical observation. Runtime snapshots expose
later reports under live health without rewriting that record. A late callback
from an owner being stopped cannot make that owner ready again. Health describes
the adapter's native scope: a connected radio or running local worker does not
establish recipient delivery or remote peer reachability.

## Shutdown Behavior

MEDRE shuts down in reverse dependency order: adapters → pipeline runner → storage.

### Shutdown Sequence

1. **Quiesce replay and durable-ingress processing** within the shared drain deadline. Adapters remain available; newly admitted durable work can stay pending for the next run.
2. **Close delivery admission and stop retry claims**, then drain in-flight deliveries within the remaining deadline. Retain honest evidence for work that does not settle.
3. **Signal shutdown and stop native owners** in reverse adapter start order with bounded cleanup. A repeated signal does not interrupt an owner's cleanup.
4. **Close inbound admission after adapters stop**, then settle callbacks already crossing the admission boundary while pipeline/storage dependencies remain available.
5. **Stop the pipeline and close storage** after dependent owners settle. Incomplete cleanup is reported; a dirty startup marker or retained uncertain attempt is not converted into success.

### Expected Shutdown Output

```console
2026-10-27T06:14:34-0500 [INFO] medre: Runtime shutting down
2026-10-27T06:14:34-0500 [INFO] medre.runtime.app: Adapter meshtastic.radio stopped
2026-10-27T06:14:34-0500 [INFO] medre.runtime.app: Adapter matrix.bridge stopped
2026-10-27T06:14:34-0500 [INFO] medre:   drain completed (timeout=10s)
2026-10-27T06:14:35-0500 [INFO] medre:   Shutdown requested 1 time(s) observed so far; first request 1.2s before this line (at 2026-10-27T11:14:33.800000+00:00)
2026-10-27T06:14:35-0500 [INFO] medre: Runtime stopped (2 adapter(s))
```

### Shutdown Checklist

| Element            | Clean sign                                   | Problem sign                                |
| ------------------ | -------------------------------------------- | ------------------------------------------- |
| Adapter stop order | Reverse of startup                           | Missing stop lines                          |
| Drain outcome      | `completed`                                  | `timed out` with abandoned count            |
| Shutdown outcome   | `drain completed` and all adapter stop lines | Logged shutdown errors or a timed-out drain |

### What Gets Drained vs Preserved

| Category                                                               | Behaviour                                                                           |
| ---------------------------------------------------------------------- | ----------------------------------------------------------------------------------- |
| In-flight adapter deliveries                                           | Drained up to `shutdown_drain_timeout_seconds`, then abandoned                      |
| Abandoned in-flight deliveries                                         | Evidence persisted as `suppressed` receipts with error `shutdown_drain_timeout`     |
| New deliveries rejected during shutdown                                | Evidence persisted as `suppressed` receipts with error `delivery_rejected_shutdown` |
| Adapter receive loops                                                  | Stopped through bounded native-owner cleanup; uncertain outcomes remain explicit    |
| Replay events                                                          | Cancelled; completed receipts preserved                                             |
| Route statistics, diagnostic counters                                  | Lost — in-memory only                                                               |
| Pending `retry_wait` outbox items                                      | Preserved — remain resumable work, claimed on next startup when due                 |
| Retry receipt evidence                                                 | Preserved — remains immutable evidence; does not schedule work                      |
| Other pending outbox items                                             | Preserved — remain in storage, reclaimable on next startup                          |
| Non-terminal outbox (`pending`, `retry_wait`, `in_progress`, `queued`) | Preserved as resumable work. `ShutdownEvidence.resume_expected=True` when present.  |

Non-terminal outbox items survive shutdown in SQLite. On next startup, due retry work is claimed from the durable outbox by the RetryWorker (if retry is enabled). Pending and expired-lease outbox items are reclaimed through `claim_due_outbox_items()` or the normal dispatch path; `queued` rows are the durable confirmation-pending disposition and are never claimed or re-dispatched. Receipts remain immutable evidence rather than scheduling authority. Eligible work can be retried under a fresh claim. Radio recovery continues only a proven-unsent suffix; accepted parts are not replayed, and unknown or submitting native handoffs remain held. Non-terminal outbox work is intentionally preserved as resumable work, not cancelled. The `ShutdownEvidence` record reports `resume_expected=True` when pending work was left at shutdown, and `outbox_shutdown_policy="resumable"` signals that the resumable policy is active.

### Signal Handling

- **First Ctrl-C / SIGTERM**: Initiates graceful shutdown immediately, without waiting out the one-second fallback poll tick. Process exits with code 0 if clean.
- **Repeated Ctrl-C / SIGTERM**: Coalesced into the same graceful shutdown; never a hard kill and never an interruption of owned cleanup. The shutdown summary snapshots the shutdown requests and the age of the first observation. When MEDRE can exclusively own a wakeup pipe and its buffer has capacity, each delivered shutdown signal is counted: presses that arrive while the loop thread is un-serviced inside a C call coalesce into one Python handler invocation, but the pipe preserves their count. If a host already owns the process wakeup fd (or the platform cannot supply one), MEDRE preserves that owner and falls back to counting Python handler invocations; these can undercount coalesced deliveries. The first-observation timestamp is the first servicing round, which can trail the first delivery by the length of that window.
- **SIGKILL**: No graceful shutdown. No logs. SQLite on disk is preserved (WAL mode). Deliveries without outbox rows are lost.

Signal handlers are reset at the start of each `medre run` invocation.

### Shutdown Snapshot (`--snapshot-on-shutdown PATH`)

```bash
medre run --config config.yaml --snapshot-on-shutdown snapshot.json
```

Writes a runtime snapshot JSON to the specified PATH after graceful shutdown. Contains:

| Section                      | Content                                         |
| ---------------------------- | ----------------------------------------------- |
| `lifecycle`                  | Runtime and adapter lifecycle state at shutdown |
| `accounting`                 | Final delivery counters                         |
| `capacity`                   | Final capacity gauges                           |
| `startup.boot_summary`       | Frozen startup classification                   |
| `diagnostics.runtime_events` | Bounded event buffer from the run               |
| `routes.stats`               | Per-route delivery statistics                   |

Counters and events are process-local — without this flag, they are lost on exit.

## Support Bundles for Issue Reports

When filing an issue, collect an offline, redacted support bundle with:

```bash
medre support bundle --config config.yaml --output medre-support.zip
```

This is the recommended artifact for a bug report. The ZIP is built
offline from config discovery, `config check`, the route plan, the
adapter inventory, schema presence, environment metadata, and a
redacted copy of the parsed config — no adapters start, no live health
is refreshed, and no network or hardware I/O is performed.

The support bundle is **observational, not delivery evidence**. It does
not include live probe results, adapter health refresh, storage contents
(events, receipts, outbox rows), delivery evidence, runtime logs, or any
hardware/network checks. For storage-backed delivery evidence use
`medre inspect event --evidence` (per-event) or `medre evidence
--storage-path` (storage-backed evidence report) — these are separate
paths from the support bundle. See [troubleshooting.md](troubleshooting.md)
§ "Support Bundles" for the complete bundle contents, the full
not-included list, and the redaction scope.

## Delivery Semantics

Delivery guarantees and receipt interpretation are per-transport. The
per-transport semantics tables and the receipt-state reference live in
[delivery-semantics.md](delivery-semantics.md).

## Configuration Examples

Complete example configurations — a single Matrix adapter (plaintext and E2EE),
two Meshtastic radios, and a mixed Matrix + Meshtastic runtime — live in
[configuration.md](configuration.md#configuration-examples).

## Resource Limits

```yaml
runtime:
  limits:
    max_inflight_deliveries: 100
    max_inflight_replay_events: 100
    shutdown_drain_timeout_seconds: 10
    delivery_acquire_timeout_seconds: 1.0
```

The pipeline uses bounded worker pools plus capacity semaphores to bound concurrent
delivery and replay. Direct/replay work that cannot acquire a slot is rejected with
`error="delivery_capacity_exceeded"`. Durably admitted live ingress instead returns
to pending as an operational deferral without consuming its poison-work attempt
budget.

### Diagnostics Counters

| Counter               | Description                                |
| --------------------- | ------------------------------------------ |
| `inbound_accepted`    | Inbound events accepted                    |
| `outbound_delivered`  | Outbound successes                         |
| `outbound_failed`     | Outbound failures                          |
| `loop_prevented`      | Events blocked by self-loop guard          |
| `capacity_rejections` | Operations rejected by capacity controller |

### Example Configurations

**Conservative (low-resource device):**

```yaml
runtime:
  limits:
    max_inflight_deliveries: 8
    max_inflight_replay_events: 4
    delivery_acquire_timeout_seconds: 10.0
    shutdown_drain_timeout_seconds: 3
```

**High-throughput (server):**

```yaml
runtime:
  limits:
    max_inflight_deliveries: 128
    max_inflight_replay_events: 64
    delivery_acquire_timeout_seconds: 60.0
    shutdown_drain_timeout_seconds: 10
```

## Retry

Retry is **opt-in** — disabled by default. Enabling retry, its frozen target
semantics, and the retry-versus-replay comparison live in
[recovery-and-replay.md](recovery-and-replay.md#enabling-retry).

## Replay

Replay re-processes historical events through pipeline stages. It is manual and
operator-initiated; the replay modes, command shape, result interpretation, and
replay receipts live in
[recovery-and-replay.md](recovery-and-replay.md#replay-modes).

## Route Loop Prevention

Multiple layers prevent routing loops:

1. **Direct loop detection (startup)** — detects route cycles and warns; it does not block startup.
2. **Native-ref duplicate suppression (per-ingress)** — checks inbound `source_native_ref` against stored references before routing; this does not establish peer-relay trust.
3. **Route-trace guard (per-delivery)** — skips routing through a route already present in the event's trace.
4. **Self-loop guard (per-delivery)** — skips a target that matches the event's source adapter.

What loop prevention does not cover:

- Cross-instance loops (two separate MEDRE instances bridging the same transports).
- Application-level loops (user commands triggering replies — normal bidirectional operation).

Per-delivery loop drills, replay loop filtering, and the capability-suppression
matrix live in [troubleshooting](troubleshooting.md#loop-prevention).

## Persistence and Crash Semantics

Canonical events, delivery receipts, post-handoff delivery observations, native
refs, and the delivery outbox persist in local SQLite
(`{state}/medre.sqlite`); Matrix E2EE keys and LXMF
identities persist under `{state}/adapters/`. In-flight deliveries without an
outbox row are lost on termination; non-terminal outbox rows survive restart as
resumable work. Runtime counters, RouteStats, gauges, and the runtime event
buffer reset on startup (capture the buffer with `--snapshot-on-shutdown PATH`).

The authoritative survives/lost breakdown, the crash-recovery procedure, and
the orphaned-event query live in
[recovery-and-replay.md](recovery-and-replay.md#what-was-lost). MEDRE persists
to local SQLite and the local filesystem only — no replication, remote backup,
or distributed coordination; operators own backup, log rotation, and disk
monitoring.

## Docker Deployment

Container deployment has one authoritative guide: [Docker](docker.md).

## Explicit Non-Guarantees

MEDRE is best-effort. It explicitly does not provide:

1. **Exactly-once delivery** — radio transports are probabilistic, Matrix is at-least-once, LXMF is at-least-once with eventual delivery.
2. **Global replay deduplication** — different or empty replay run IDs remain
   repeatable. A non-empty run ID atomically owns one target generation in the
   shared storage database, but this does not make ambiguous transport retries or
   recovery exactly-once.
3. **Durable adapter-local queue** — Meshtastic outbound queue is in-memory, lost on shutdown.
4. **Per-adapter restart** — only full runtime stop/start.
5. **Distributed coordination** — state is local to the process.
6. **Transactional fan-out** — each target in a fan-out is independent; partial delivery is normal.
7. **Delivery completeness under pressure** — direct/replay work can be rejected
   when capacity is unavailable. Storage-backed live ingress is durably deferred
   instead, but eventual delivery still depends on later processing and transport
   behavior.

## Log Output and Capture

MEDRE writes runtime logs to stdout. It does not open a log file automatically.
A service manager or container logging driver captures that stream according to
its own retention policy. The resolved `log_dir` is a reserved directory; its
existence does not mean a `medre.log` file is being written.

For a direct shell run, choose a private capture path explicitly:

```bash
log_capture=/private/medre/medre.log
umask 077
touch -- "$log_capture"
chmod 600 -- "$log_capture"
medre run --config medre.yaml >> "$log_capture" 2>&1
```

Create the capture directory beforehand and restrict access to it (mode 0700).
The command runs in the foreground;
use another terminal to inspect the file or send the normal shutdown signal.
Config-loading and command-usage errors can appear on stderr before runtime
logging is configured. Captures can contain room IDs, message identifiers, and
SDK diagnostics; keep them private.

Choose `logging.format: text` for timestamped human-readable records or
`logging.format: json` for one JSON object per record. Text timestamps include
the local UTC offset; JSON timestamps represent record creation in UTC.
Lifecycle summaries use the same logger and level as runtime messages.

At INFO, startup renders the consolidated banner once every adapter has
settled, and total adapter startup failure is labeled
`Runtime startup failed` without reporting readiness. Individual expanded
route registrations, adapter construction, and duplicate component startup
details are available at DEBUG.
Delivery messages distinguish `Delivery queued` (deferred adapter work) from
`Handoff accepted` (synchronous adapter acceptance) and include the adapter,
event, attempt, and confirmation level. Neither label claims remote receipt.
JSON records retain the complete plan identifier and handoff disposition for
correlation without adding long generated identifiers to the text message.

MEDRE does not rotate logs internally. Use external log rotation (logrotate, Docker logging drivers, Kubernetes log management) for production deployments.

## Recovery Procedures

The canonical procedures live in [recovery-and-replay.md](recovery-and-replay.md):
the [complete incident workflow](recovery-and-replay.md#complete-incident-workflow),
[adapter failure recovery](recovery-and-replay.md#adapter-failure-recovery), and
[database corruption recovery](recovery-and-replay.md#database-corruption-recovery).
For config errors: run `medre config check` to identify the issue, fix the
config file, and restart the runtime; config failure drills live in
[troubleshooting](troubleshooting.md#config-failure-drills).

## Queue Discipline

### Capacity Bounding

The `CapacityController` manages three independent bounded capacity gates:

| Stream   | Config field                      | Default bound | What it limits                                            |
| -------- | --------------------------------- | ------------- | --------------------------------------------------------- |
| Delivery | `max_inflight_deliveries`         | 100           | Concurrent adapter `deliver()` calls across all adapters  |
| Replay   | `max_inflight_replay_events`      | 100           | Concurrent replay event deliveries                        |
| Inbound  | `max_inflight_inbound_admissions` | 100           | Concurrent durable ingress admissions across all adapters |

Inbound is admission-gated before durability: the radio transports schedule one
ingress coroutine per SDK callback with no transport-level bound, so every
inbound publish and protocol-provenance admission crosses the gate first.
Execution slots are global and work-conserving, but pending overload is split
into equal fair shares across the adapters that built successfully and is
granted round-robin by adapter ID. A single adapter can therefore use the full
execution limit while uncontended, but cannot consume every pending slot during
a multi-adapter burst.

The total wait queue remains bounded at the admission limit itself. Filling an
adapter's fair share or the global queue, waiting past
`inbound_admission_timeout_seconds` (default 5.0s), or arriving after inbound
acceptance closed is counted as a capacity rejection. Ordinary
`publish_inbound` callbacks receive `InboundAdmissionRejected` and may lose the
native event when their transport offers no redelivery contract. Cursor-aware
`admit_inbound` callbacks instead receive `DurableIngressDeferredError` so the
transport can leave its cursor unadvanced; Matrix uses that path and keeps the
native event pending. The capacity snapshot exposes aggregate pressure plus an
`inbound_sources` map with per-adapter `current`, `waiting`, `wait_limit`,
`rejections`, `timeouts`, and `oldest_wait_seconds` values. No event payloads or
transport secrets enter those counters.

During shutdown, inbound acceptance stays open while adapters stop so late
callbacks persist rows for the next runtime generation; it closes after the
last adapter, and outstanding admissions drain before storage closes.

When a direct delivery or replay event cannot acquire a slot within
`delivery_acquire_timeout_seconds` (default 1.0s), the operation is rejected and
diagnostics are incremented. A delivery spawned from durably admitted live ingress
is different: capacity/shutdown rejection defers the ingress row back to pending
without spending the processing-failure attempt budget. Delivery fan-out creates at
most `max_inflight_deliveries` worker tasks rather than allocating one waiting task
per target.

### Adapter-Level Queue Bounds

| Adapter    | Queue mechanism                           | Default bound | Overflow policy                                                          |
| ---------- | ----------------------------------------- | ------------- | ------------------------------------------------------------------------ |
| Meshtastic | Unbounded deque with explicit enqueue cap | 1024 items    | Explicit rejection when full, `queue_total_rejected` counter incremented |

Other adapters (Matrix, LXMF, MeshCore) rely on the runtime `CapacityController` gate and their transport's own flow control.

### Monitoring Queue Pressure

Capacity counters are available in the `capacity` section of the runtime snapshot. The `medre diagnostics` command produces a build-time snapshot where these counters are zero — it does not start adapters. To inspect live capacity, use `--snapshot-on-shutdown PATH` or the runtime snapshot from a running instance.

| Counter               | What it tells you                          |
| --------------------- | ------------------------------------------ |
| `delivery_current`    | In-flight deliveries right now             |
| `inbound_current`     | In-flight durable admissions right now     |
| `inbound_rejections`  | Admission arrivals rejected before commit  |
| `inbound_timeouts`    | Admission arrivals that timed out waiting  |
| `inbound_sources`     | Per-adapter admission pressure attribution |
| `inbound_accepted`    | Inbound events accepted                    |
| `outbound_delivered`  | Outbound successes                         |
| `outbound_failed`     | Outbound failures                          |
| `loop_prevented`      | Events blocked by self-loop guard          |
| `capacity_rejections` | Operations rejected by capacity controller |
| `replay_current`      | In-flight replay events                    |

Sustained growth in `capacity_rejections` indicates the runtime is under more pressure than its configured limits can handle. Inspect the per-stream counters first: tune `max_inflight_inbound_admissions` for admission pressure, `max_inflight_deliveries` for delivery pressure, or reduce the number/rate of active routes and sources.

## Bridged Message Appearance

How bridged messages render in each direction — renderer output, reply
threading support, and source-label placement — lives in
[message-presentation.md](message-presentation.md#bridged-message-appearance).

## Soak and Validation

The soak harness (`tests/test_soak_harness.py`) validates stability patterns within a bounded timeframe suitable for CI. It uses fake adapters, in-memory storage, and no wall-clock sleeps. Iteration count configurable via `SOAK_HARNESS_ITERATIONS` (default 50, max 200).

```bash
# Default: 50 iterations
pytest tests/test_soak_harness.py

# Deeper local run: 200 iterations
SOAK_HARNESS_ITERATIONS=200 pytest tests/test_soak_harness.py
```

What the soak harness validates: start/stop cycling, replay cycling, delivery under pressure, long-running stability.

What the soak harness does not validate: live transport behaviour, multi-hour stability, radio transport pressure, exactly-once delivery.

Soak runs are part of the developer test suite; the test-tier taxonomy lives
in [testing](../dev/testing.md).

## Operational Caveats

### Radio Transport Pressure

Radio transports (Meshtastic, MeshCore) are inherently constrained:

- Low bandwidth — LoRa PHY is extremely slow (hundreds of bytes per second on LongFast).
- No flow control from the mesh — the radio accepts packets as fast as the serial link allows, but the mesh itself may drop them.
- Serial write blocking — writing to a serial port blocks until the kernel buffer accepts the data.
- Packet loss is normal — monitor trends, not individual events.

Under sustained radio pressure, the Meshtastic outbound queue rejects new items when full. This is by design — the runtime prioritizes stability over delivery completeness.

Exactly-once delivery and transactional fan-out are absent by design; see
[Explicit Non-Guarantees](#explicit-non-guarantees).
