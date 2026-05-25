# Bridge Operation Runbook

> Last updated: 2026-05-16
> Scope: Delivery-state discipline for cross-transport bridge operation
> Status: Pre-beta. Not production. Operational model is accurate to code; live bridge validation is not claimed. Docker SDK-boundary bridge tests prove real SDK lifecycle against containerized services, including cross-adapter Matrix→Meshtastic routing.

This runbook documents how delivery state works when MEDRE bridges events across transports. It covers what each transport can honestly report, where retry boundaries fall, how the pipeline records results, and what operators should expect when routing events through a multi-transport bridge.

## 1. Core Principle: Adapters Own Transport Delivery

MEDRE separates two concerns:

- **Adapters own transport delivery.** Each adapter owns its connection lifecycle, its retry budget, its reconnect policy, and the truth of what the external system reported back. When an adapter's `deliver()` returns an `AdapterDeliveryResult`, that result contains exactly what the platform returned — a Matrix `event_id`, a Meshtastic packet ID, or nothing if the transport does not confirm. The adapter does not fabricate confirmation that the transport did not provide.

- **The runtime owns routing attribution and orchestration.** The router matches events to routes. The pipeline orchestrates ingress → store → route → plan → deliver → receipt. The runtime records `DeliveryReceipt` objects tracking the progression of each outbound delivery through status states. The runtime never claims final delivery — it records what the adapter reported, honestly.

This boundary is architectural. Nothing outside an adapter touches the transport connection. Nothing inside an adapter decides which events to route where.

## 2. Per-Transport Delivery Semantics

Each transport has fundamentally different delivery guarantees. Operators must understand these differences to interpret receipt states and diagnose delivery issues correctly.

### Matrix

| Property               | Value                                                                                                                                                        |
| ---------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| Transport type         | Persistent async TCP (long-poll or WebSocket sync)                                                                                                           |
| Server acknowledgment  | Yes — Synapse returns an `event_id` on successful `room_send`                                                                                                |
| Delivery confirmation  | Server-level. The message reached the homeserver. Not per-recipient read receipts.                                                                           |
| Retry semantics        | Meaningful. Connection loss is detectable; reconnect and retry will attempt redelivery.                                                                      |
| Duplicate risk         | Low on normal paths. Retries after connection loss may produce duplicates if the first send succeeded but the response was lost.                             |
| Receipt interpretation | `sent` with a populated `adapter_message_id` means the homeserver accepted the event. This is the strongest confirmation MEDRE can report for any transport. |

Matrix is the only MEDRE transport where `sent` implies server-verified persistence. Even so, this is server-level only — it does not mean any recipient has read the message.

### Meshtastic

| Property               | Value                                                                                                                                                              |
| ---------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| Transport type         | LoRa radio (serial/TCP connection to a local node)                                                                                                                 |
| Server acknowledgment  | None. The local node queues the packet for radio transmission. No mesh-wide ACK exists.                                                                            |
| Delivery confirmation  | None beyond local-node acceptance. Whether any remote node received the packet is unknown.                                                                         |
| Retry semantics        | Limited. The adapter can retry if the local node connection fails, but cannot retry based on remote-node receipt.                                                  |
| Duplicate risk         | High. Radio environments cause packet loss. Operators routinely send duplicate messages to increase delivery probability. This is by design in LoRa mesh networks. |
| Receipt interpretation | `sent` means the local node accepted the packet for transmission. It does not mean any other node received it.                                                     |

Meshtastic delivery is best-effort fire-and-forget at the radio layer. Expect packet loss. Expect to resend. Do not treat `sent` as delivered.

**Outbound gate (`outbound_mode`):** The Meshtastic adapter supports `outbound_mode` with values `"enabled"` (default) and `"listen_only"`. When `outbound_mode = "listen_only"`, the adapter connects normally and receives inbound packets, but suppresses all outbound delivery — `deliver()` rejects payloads as non-retryable failures with detail `outbound suppressed: listen_only mode`. This is an intentional operator-configured gate for receive-only monitoring. Enable via TOML or `MEDRE_ADAPTER__RADIO__OUTBOUND_MODE=listen_only`.

**Shutdown queue abandonment:** Items remaining in the Meshtastic adapter's in-memory outbound queue at shutdown are not persisted, not requeued, and not recovered on restart. The adapter-local queue is non-durable. However, the `delivery_outbox` table provides durable operational tracking: a `queued` outbox row may survive if committed before the crash (such rows are ambiguous after restart and are not automatically retried). Operators requiring delivery assurance must ensure the queue is drained before shutdown.

### MeshCore

| Property               | Value                                                                                              |
| ---------------------- | -------------------------------------------------------------------------------------------------- |
| Transport type         | MeshCore radio (TCP/serial/BLE connection to a local node)                                         |
| Server acknowledgment  | None beyond local-node acceptance. No mesh-wide ACK.                                               |
| Delivery confirmation  | None. Same radio best-effort reality as Meshtastic.                                                |
| Retry semantics        | Same as Meshtastic — retryable at the local-node connection level, not at the mesh delivery level. |
| Duplicate risk         | High. Same radio environment considerations.                                                       |
| Receipt interpretation | `sent` means the local node accepted the packet. Nothing more.                                     |

MeshCore and Meshtastic share the same delivery discipline: radio best-effort, no confirmation, duplicates are normal operational reality.

### LXMF (Reticulum)

| Property               | Value                                                                                                                                                                |
| ---------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Transport type         | Store-and-forward over Reticulum (multi-hop mesh)                                                                                                                    |
| Server acknowledgment  | No single-server ACK. Reticulum uses link-level delivery with propagation delays.                                                                                    |
| Delivery confirmation  | Eventual. LXMF messages propagate across the Reticulum network over seconds to hours depending on path length and transport type.                                    |
| Retry semantics        | Reticulum handles propagation internally. The adapter delivers to the local `LXMRouter` and trusts the network. Adapter-level retry covers local failures only.      |
| Duplicate risk         | Low for well-behaved senders. Reticulum's delivery mechanism handles deduplication at the protocol level.                                                            |
| Receipt interpretation | `sent` means the local `LXMRouter` accepted the message for propagation. Delivery to the destination may take significant time. Do not assume instantaneous receipt. |

LXMF is the only transport where `sent` means "accepted for eventual delivery" with a potentially long propagation window. The time between `sent` and actual destination receipt can range from seconds to hours depending on network topology.

## 3. Delivery Receipt States

The pipeline records a `DeliveryReceipt` for each outbound delivery attempt. Receipts progress through these states:

```text
accepted → queued → sent → confirmed
                  ↘ failed → dead_lettered
```

| Status          | Meaning                                                                                                                                              |
| --------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------- |
| `accepted`      | Pipeline has accepted the event for delivery. No transport contact yet.                                                                              |
| `queued`        | Delivery plan created, waiting for adapter execution.                                                                                                |
| `sent`          | Adapter reported successful handoff to the transport. **This is not final delivery.** See per-transport table above for what `sent` actually means.  |
| `confirmed`     | Adapter reported positive confirmation from the external system. Only Matrix currently reaches this state. Radio transports never reach `confirmed`. |
| `failed`        | Adapter reported a delivery failure. Classified by `DeliveryFailureKind`.                                                                            |
| `dead_lettered` | Delivery exhausted all retries and fallback strategies. Permanently failed.                                                                          |

Each receipt carries `attempt_number` and `parent_receipt_id` forming an explicit retry lineage. The first attempt is `attempt_number=1` with `parent_receipt_id=None`. Retries chain through the parent reference. The `source` column on each receipt distinguishes origin: `"live"` for original pipeline delivery, `"retry"` for RetryWorker-attempted delivery, and `"replay"` for operator-initiated replay delivery.

## 4. Retry Ownership Boundaries

Retry is **opt-in** — it is disabled by default. The `RetryWorker` only runs when a `RetryPolicy` is configured on the route or delivery plan. Without a `RetryPolicy`, transient failures are not automatically retried; they remain as `failed` receipts until an operator initiates manual replay.

Retry responsibility falls to different components depending on where the failure occurs:

| Failure kind        | Who owns retry       | Notes                                                                                              |
| ------------------- | -------------------- | -------------------------------------------------------------------------------------------------- |
| `PLANNER_FAILURE`   | No retry — permanent | Config error                                                                                       |
| `RENDERER_FAILURE`  | No retry — permanent | Deterministic error                                                                                |
| `ADAPTER_TRANSIENT` | RetryWorker (opt-in) | Requires `RetryPolicy`; bounded by max attempts and backoff; retry receipts carry `source="retry"` |
| `ADAPTER_PERMANENT` | No retry — permanent | Adapter determined unrecoverable                                                                   |
| `DEADLINE_EXCEEDED` | No retry             | Plan deadline passed                                                                               |

**Frozen target semantics:** Retry uses the `target_adapter` and `target_channel` from the original failed receipt, not the current route config. Route targets, channel assignments, and adapter mappings may change between the original failure and a retry attempt, but the retry continues to target the originally recorded adapter and channel. Before executing the retry, the RetryWorker validates that the target adapter still exists at runtime. If the adapter has been removed from the configuration, the retry is not attempted and the receipt is dead-lettered. This ensures route config changes do not silently redirect in-flight retries while still guarding against retrying to a non-existent adapter.

**Retry policy persistence:** The first failure receipt captures the `RetryPolicy` parameters as `retry_max_attempts`, `retry_backoff_base`, `retry_max_delay`, and `retry_jitter` columns. The RetryWorker reads these values from the stored receipt, not from the current route configuration. Route or policy changes after the original failure do not affect in-flight retry behavior. The policy is frozen at first failure.

Adapters own their internal reconnect logic (e.g., Matrix sync reconnection, Meshtastic node reconnection). The RetryWorker owns retry scheduling for transient delivery failures. These are separate mechanisms operating at different layers. **Retry does NOT restart adapters.** Adapter lifecycle is independent — adapters reconnect on their own schedule. The RetryWorker only re-attempts the delivery through the same planning pipeline.

**Retry does NOT guarantee final delivery ACK.** The adapter confirms transport acceptance only. Whether the remote side actually received the message depends on the transport (see Section 2). A `sent` receipt from a retry means the same thing as a `sent` receipt from the original delivery.

**Retry receipt attribution:** Each retry attempt produces a new `DeliveryReceipt` with `source="retry"`, linked to the original failure via `parent_receipt_id` and carrying an incremented `attempt_number`. This makes retry receipts distinguishable from live deliveries (`source="live"`) and replay deliveries (`source="replay"`) at the storage layer. Operators can filter by `source` to isolate retry outcomes from normal delivery and replay.

**Capacity rejection semantics:** If the RetryWorker cannot acquire delivery capacity (the delivery semaphore is full), it emits a `retry_failed` event and reschedules the receipt for the next worker interval. No durable receipt is created for capacity rejection — the original failed receipt remains due with its `next_retry_at` updated to the next interval. The RetryWorker retries capacity acquisition on its next cycle; it does not dead-letter on capacity rejection.

## 5. Duplicate-Send Realities

Duplicate sends are an operational fact in bridge scenarios, not a bug:

- **Radio transports (Meshtastic, MeshCore):** Duplicate sends are expected and often intentional. Packet loss is high in LoRa environments. Operators routinely send the same message multiple times to increase the probability of at least one copy arriving. The bridge does not deduplicate at the radio layer because deduplication is not the bridge's job — it is the application's job on the receiving side.

- **Matrix:** Duplicates are rare but possible when a send succeeds but the response is lost, triggering a retry that sends the same content again. Matrix event IDs will differ for each attempt.

- **LXMF:** Duplicates are low-probability due to Reticulum's protocol-level handling, but store-and-forward semantics mean a late duplicate from a slow propagation path is possible.

- **Bridge fan-out:** When a single inbound event routes to multiple targets (e.g., one Matrix message bridged to both Meshtastic and MeshCore), each target gets an independent delivery. A failure on one target does not affect the other. A success on one target does not guarantee the other.

The runtime does not suppress duplicate sends. It delivers what the routes specify, to the targets the routes specify, and records what happens honestly.

## 6. Runtime Routing and Delivery Honesty

The runtime's routing layer — the `Router` and `RouteEngine` — is a pure in-memory matching engine. It performs no I/O. It matches events against route source specifications and resolves target adapters. It does not know or care about transport delivery semantics.

The pipeline records delivery results honestly:

- If the adapter returns a native message ID, the receipt records it.
- If the adapter returns nothing, the receipt records `sent` without an `adapter_message_id`.
- If the adapter raises, the receipt records `failed` with the error classification.

The runtime never upgrades a receipt state based on assumptions. A `sent` receipt for Meshtastic stays `sent`. It does not become `confirmed` because the runtime has no basis for that claim. This honesty principle is non-negotiable — the receipts must be trustworthy as an audit trail.

## 7. Replay and Route Attribution

The `ReplayEngine` supports re-processing historical events through pipeline stages. Two modes are relevant to bridge delivery state:

| Mode          | Route | Deliver | Side effects     | Use case                                                                                                                                           |
| ------------- | ----- | ------- | ---------------- | -------------------------------------------------------------------------------------------------------------------------------------------------- |
| `RE_ROUTE`    | Yes   | No      | None (read-only) | Re-evaluate which routes match historical events after a route config change. Useful for verifying that new routes would have matched past events. |
| `BEST_EFFORT` | Yes   | Yes     | Adapter delivery | Re-deliver historical events through current routes and adapters. Use with caution — this produces real outbound messages.                         |
| `DRY_RUN`     | Yes   | Skip    | None (read-only) | Route and render without actually delivering. Preview what would happen.                                                                           |

Replay route attribution records which routes matched each historical event. This attribution is metadata about routing decisions, not about delivery outcomes. A route attribution says "this route would have matched" — it does not say "this message was delivered."

**Operational implication:** When re-routing after a config change, use `RE_ROUTE` or `DRY_RUN` first to verify matching behavior. Only use `BEST_EFFORT` when you intend to re-deliver real messages. Re-delivery through `BEST_EFFORT` will produce new outbound messages on all matched targets — including radio transports where duplicates are normal.

**Test coverage note:** The replay pipeline integration path — including route matching, loop prevention via `_filter_replay_loops`, and `ReplayRouteAttribution` — is exercised by `test_replay_pipeline_integration.py` (which tests the real `PipelineRunner` replay path) and `test_replay_routing.py` (which covers route matching through the actual `Router`, `ReplayEngine`, and `_filter_replay_loops` code paths). Boundary tests in `test_architectural_boundaries.py` confirm that replay and routing modules remain free of transport SDK imports. Replay test purity is enforced: no replay test file imports live adapter packages or SDKs.

Replay receipts carry `source="replay"` and a populated `replay_run_id` for audit traceability. This distinguishes replay-originated receipts from live deliveries at the storage layer. Traceability supports audit but does not prevent duplicate delivery — multiple BEST_EFFORT replays of the same event produce additional receipt rows, each with a different `replay_run_id`.

## 7a. Docker SDK-Boundary Bridge Validation

Docker SDK-boundary tests prove that real adapter SDKs work against
containerized services (Synapse for Matrix, meshtasticd for Meshtastic).
These tests validate:

- **Real SDK initialization** — adapter code loads and uses real SDK libraries.
- **Config-to-runtime path** — configs with real connection parameters build
  and start correctly.
- **Lifecycle correctness** — start, health check, deliver, stop all work
  through real SDK code paths.
- **SDK boundary integrity** — no SDK objects leak across the adapter boundary
  into diagnostics or snapshots.

Docker SDK-boundary tests do **not** prove live network behavior. Services
run on localhost via Docker containers. See `docs/runbooks/integration-testing.md`
for the full Docker test tier documentation and provenance levels.

The Synapse bridge smoke test (`test_synapse_bridge_smoke.py`) provides the
strongest evidence at this tier: it exercises real nio SDK inbound via sync
loop (with fallback to direct `_on_room_message` if sync does not deliver in
15 seconds), real `MatrixCodec` decode, real `PipelineRunner` routing to a
`FakeMatrixAdapter`, `DeliveryReceipt` persistence with genuine Synapse
event_ids, `NativeMessageRef` inbound mapping, and `RuntimeAccounting`
counter increments — all against a Docker-local Synapse homeserver. The
outbound target in this test is a fake adapter.

The `matrix_to_meshtastic` Docker bridge artifact run goes further: it
exercises the full cross-adapter path from real Matrix nio SDK ingress
through `PipelineRunner` routing to a real Meshtastic `mtjk` SDK outbound
delivery against a Docker-local meshtasticd instance. This proves real
SDK-to-SDK event flow: Matrix event arrives via the real sync loop,
`MatrixCodec` decodes it, the pipeline routes through `MeshtasticRenderer`,
and the real Meshtastic adapter enqueues the payload via `sendText` to
meshtasticd, returning a real packet ID. This is the strongest cross-adapter
evidence available without real radio hardware or external accounts.

**Proof boundaries for `matrix_to_meshtastic` cross-adapter:**

- Proven: real nio SDK ingress, PipelineRunner routing, real mtjk SDK
  outbound to meshtasticd.
- Not proven: automated queue draining, real LoRa radio, real pubsub
  Meshtastic inbound, sustained throughput.
- All services run on localhost via Docker containers. No external Matrix
  account, no real radio, no real network.

- **`ingress_path` tracking** — the Matrix Docker bridge test tracks whether
  inbound events arrived via the real nio `sync_forever` callback
  (`"sync_loop"`) or via direct `_on_room_message`
  (`"direct_on_room_message_fallback"`). Only `sync_loop` proves full
  Matrix adapter ingress through the real SDK sync path. When the fallback
  path fires, the test logs a warning and the report records it in
  `limitations`. See `test_synapse_bridge_smoke.py` for the `_wait_for_sync_or_fallback`
  helper and `IngressResult` class that encode this distinction.

- **Run-session test** — `test_synapse_run_session.py` exercises the full
  MEDRE runtime lifecycle against Docker Synapse (start adapters, send
  Matrix message, ingress through real sync path, canonical event persisted,
  delivery to fake target, receipt status="sent") and produces a report
  matching the `run_session` shape (status, event_id, receipts, native_refs,
  ingress_path).

| Provenance tier                               | Status          | What is proven                                                                                                                                                                                                                                  |
| --------------------------------------------- | --------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Fake bridge                                   | **Proven**      | Pipeline routing, rendering, receipts, accounting                                                                                                                                                                                               |
| Adapter-wrapper                               | **Proven**      | Per-transport adapter codec, renderer, session                                                                                                                                                                                                  |
| Docker SDK-boundary                           | **Proven**      | Real SDK lifecycle, config, dependency resolution                                                                                                                                                                                               |
| Docker SDK-boundary bridge smoke              | **Proven**      | Real Matrix SDK codec + pipeline routing + storage + accounting with genuine Synapse event_ids                                                                                                                                                  |
| Docker cross-adapter (`matrix_to_meshtastic`) | **Proven**      | Real Matrix nio SDK ingress + PipelineRunner routing + real Meshtastic mtjk SDK outbound to meshtasticd with real packet IDs                                                                                                                    |
| Docker cross-adapter (`meshtastic_to_matrix`) | **Deferred**    | Not proven at cross-adapter level. Meshtastic SDK lifecycle and outbound `sendText` proven, but inbound uses `simulate_inbound`/`wrapper_callback`. No real external Matrix target. Deferred until Matrix outbound is proven with real Synapse. |
| Live network                                  | **Not claimed** | No test against real external endpoints or real radio hardware                                                                                                                                                                                  |

### 7a.1 Docker Artifact Bundle

The Docker bridge artifact collector (`scripts/ci/run-docker-bridge-artifacts.sh`) writes a structured artifact bundle to `.ci-artifacts/docker-bridge-runs/<ISO-timestamp>/`. This bundle is the primary evidence record for Docker SDK-boundary bridge validation.

**Required files** (scenario-aware):

| File                | When required                                                   |
| ------------------- | --------------------------------------------------------------- |
| `summary.json`      | All scenarios                                                   |
| `run-metadata.json` | All scenarios                                                   |
| `config.toml`       | All scenarios                                                   |
| `synapse.log`       | `matrix_to_meshtastic`, `bidirectional`                         |
| `meshtasticd.log`   | `matrix_to_meshtastic`, `meshtastic_to_matrix`, `bidirectional` |

For `matrix_to_meshtastic`, both `synapse.log` and `meshtasticd.log` are required because the scenario exercises the full cross-adapter path: real Synapse ingress through the nio SDK, PipelineRunner routing, and real Meshtastic adapter outbound delivery to meshtasticd. Both container logs are necessary evidence of SDK-to-SDK event flow.

**Best-effort files** (present when the corresponding subsystem ran):

| File                    | Meaning                                                                                                                         | When present                               |
| ----------------------- | ------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------ |
| `medre.log`             | Runtime log. **Absent when PipelineRunner is used instead of full MedreApp** (the common case for Docker bridge artifact runs). | Full MedreApp runtime initialized.         |
| `receipts.json`         | Delivery receipt snapshot.                                                                                                      | At least one delivery attempted.           |
| `native-refs.json`      | Inbound native message refs.                                                                                                    | Inbound refs recorded.                     |
| `inspect-timeline.json` | Per-event pipeline timeline.                                                                                                    | Pipeline completed for at least one event. |
| `evidence.json`         | Full bridge evidence bundle.                                                                                                    | Evidence collection succeeded.             |
| `final-snapshot.json`   | Runtime shutdown snapshot. **Absent when PipelineRunner is used instead of full MedreApp**.                                     | Full MedreApp graceful shutdown.           |

Missing best-effort files are explained in `summary.json` under `limitations`. For example: `"receipts.json absent: no deliveries attempted before failure"`.

**Quick inspection:**

```bash
RUN_DIR=$(ls -td .ci-artifacts/docker-bridge-runs/*/ | head -1)

# Status and limitations
python -c "import json; s=json.load(open('${RUN_DIR}summary.json')); \
  print(f\"Status: {s['status']}\nScenario: {s['scenario']}\"); \
  print('Limitations:'); [print(f'  - {l}') for l in s['medre']['limitations']]"

# Review logs
less "${RUN_DIR}pytest-stderr.log"
less "${RUN_DIR}synapse.log"      # Matrix scenarios
less "${RUN_DIR}meshtasticd.log"  # Meshtastic scenarios
```

**Prerequisites:** The script checks optional imports per scenario and fails fast with a clear install instruction if any are missing. It does not silently install packages. Matrix scenarios require `import nio` (from `mindroom-nio`). Meshtastic scenarios require `import meshtastic` and `from pubsub import pub`. Install everything with:

```bash
pip install -e ".[matrix,meshtastic,dev]"
```

For full artifact documentation, see [Docker Bridge Artifacts](docker-bridge-artifacts.md).

## 8. How to Validate MEDRE Bridge Operation

This section provides concrete commands and expected outcomes for validating
MEDRE bridge behavior at three tiers of fidelity. Start at the lowest tier and
work upward. Each tier is a prerequisite for the next.

For day-to-day investigation of what happened during a bridge run, the
inspect-first product path is preferred: `medre inspect event` and
`medre inspect receipts` to understand current state. For deeper per-event
investigation, use `medre inspect event --timeline` (covers `trace event`),
`medre inspect event --evidence` (covers `evidence --event`), or
`medre inspect event --recovery` (covers `recover --event`). The specialized
`trace`, `evidence`, and `recover` commands remain available for standalone
output. Replay is a lower-level supported command reserved for recovery
scenarios.

The authoritative evidence matrix for all transports and tiers is in
[docs/architecture/transport-validation-matrix.md](../architecture/transport-validation-matrix.md).

### 8.1 Quick Validation (No Docker, No Hardware)

This validates the core pipeline, codec, rendering, receipts, accounting, and
loop prevention using fake adapters. No network, no Docker, no SDK dependencies.

**Run the [Fake adapter bridge] or [Wrapper callback] path from §8.4.**

For a full unit test suite that covers both tiers: `PYTHONPATH=src pytest -q`

**What passing proves:**

- Pipeline routing, rendering, receipts, accounting, and loop prevention work
  correctly with all four transports.
- Per-transport adapter codec and callback paths work with real adapter code
  (wrapper callback tests).
- The core is correct. Bugs are not in the pipeline.

**What passing does NOT prove:**

- Nothing about real SDK behavior, network I/O, or hardware.
- A passing fake bridge test gives no evidence that the real transport SDK
  connects to anything.

**What failing means:**

- `ImportError` for `msgspec` or `medre.core`: installation is broken. Run
  `pip install -e ".[dev]"` and verify `PYTHONPATH=src`.
- Test failures in fake bridge: core pipeline regression. Do not proceed to
  Docker or live testing until fake bridge tests pass clean.
- Failures in wrapper callback tests: adapter codec regression. The
  simulate_inbound → codec → pipeline path is broken for that adapter.

### 8.2 Docker Validation (Containerized Transports)

This validates real SDK libraries against containerized services. Requires
Docker, the relevant SDK dependency group, and network access to pull images.

**Prerequisites:**

```bash
pip install -e ".[matrix,meshtastic,dev]"
docker compose -f docker-compose.integration.yaml up -d
```

**Run the [Docker Matrix] or [Docker Meshtastic] path from §8.4.**

For all Docker integration tests together: `PYTHONPATH=src pytest tests/integration/ -m docker -v`

**Expected evidence report (Matrix):**

- `test_synapse_connectivity`: real nio SDK connects to Synapse, delivers a
  message, reports healthy, stops cleanly.
- `test_synapse_bridge_smoke`: real sync_loop delivers inbound event through
  pipeline. Receipts persisted with genuine Synapse `event_id`. Report shows
  `ingress_path == "sync_loop"`.
- `test_synapse_run_session`: full runtime lifecycle against Docker Synapse.

**Expected evidence report (Meshtastic):**

- `test_meshtasticd_connectivity`: real `mtjk` SDK creates `TCPInterface`,
  subscribes to pubsub, reports healthy, stops cleanly.
- `test_meshtasticd_sdk_bridge`: outbound delivery via real `sendText` returns
  real packet ID. SDK lifecycle proven. Inbound uses `simulate_inbound` (not
  real pubsub).

**What passing proves:**

- Real SDK libraries load, connect, and interact with containerized services.
- Config-to-runtime path works with real connection parameters.
- Adapter lifecycle (start, health, deliver, stop) works through real SDK code.
- For Matrix: inbound delivery through real nio sync_forever callback. Full
  codec → pipeline → receipt chain with genuine Synapse event IDs.
- For Meshtastic: outbound delivery through real `sendText`. SDK lifecycle and
  pubsub subscription.

**What passing does NOT prove:**

- Container runs on localhost. Not a real network environment.
- Meshtastic inbound through real pubsub callback is unconfirmed (see known
  gap: two-client relay).
- No MeshCore or LXMF Docker tests exist.
- No automated queue draining, sustained throughput, or reconnect resilience.
- No real LoRa radio. meshtasticd simulates radio behavior.
- No real external Matrix account beyond Docker-local Synapse.

**What failing means:**

- Docker not running: start Docker, pull images, verify containers are up.
- `ImportError` for `mindroom_nio` or `mtjk`: install the relevant dependency
  group (`pip install -e ".[matrix]"` or `pip install -e ".[meshtastic]"`).
- Connection refused: container not ready. Wait for health checks or check
  `docker compose logs`.
- Meshtastic tests fail with SDK errors: meshtasticd may not be running in
  simulation mode. Verify `docker-compose.integration.yaml` passes the `-s`
  flag to meshtasticd (it does by default).

### 8.3 Live Validation (Real Accounts/Devices)

This validates adapters against real endpoints. Requires real credentials,
accounts, or hardware. These tests are off by default and gated by environment
variables.

**Prerequisites:**

```bash
pip install -e ".[matrix,meshtastic,dev]"

# For Matrix live tests:
export MATRIX_HOMESERVER=https://your-homeserver.org
export MATRIX_USER_ID=@user:your-homeserver.org
export MATRIX_ACCESS_TOKEN=syt_...
export MATRIX_ROOM_ID='!roomid:your-homeserver.org'
```

**Run the [Live Matrix] path from §8.4.**

No live test files exist for Meshtastic, MeshCore, or LXMF.

**Expected evidence report (Matrix):**

- Adapter starts, sends a message, reports diagnostics, stops.
- A real `event_id` is returned by the homeserver.
- The message appears in the room (manual verification or check `event_id`
  against Synapse).

**What passing proves:**

- The Matrix adapter works against a real homeserver with real credentials.
- Basic connectivity and protocol compliance for Matrix.

**What passing does NOT prove:**

- Sustained reliability, throughput, or reconnect resilience.
- No live test exists for Meshtastic, MeshCore, or LXMF. Passing Matrix live
  says nothing about those transports.

**What failing means:**

- Tests skipped: environment variables not set. This is expected if you do not
  have real credentials.
- Connection refused / timeout: homeserver unreachable, credentials invalid,
  or room does not exist. Verify the `MATRIX_*` values.
- 401/403: access token expired or wrong user. Regenerate the token.

### 8.4 Happy Path per Evidence Level

For quick reference, each evidence level has **one** primary validation command.
Use the table below to find the right command for the fidelity you need.

| Evidence level           | Primary command                                                                 | What it proves                                                                         | Limits                                                 |
| ------------------------ | ------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------- | ------------------------------------------------------ |
| **Fake adapter bridge**  | `PYTHONPATH=src medre smoke --json`                                             | Full pipeline routing with zero dependencies                                           | No real transports                                     |
| **Wrapper callback**     | `pytest tests/test_matrix_wrapper_ingress.py -v` (or MeshCore/LXMF equivalents) | Adapter callback → pipeline → fake outbound                                            | SDK is mocked; no real transport boundary              |
| **Docker Matrix**        | `pytest tests/integration/test_synapse_bridge_smoke.py -m docker -v`            | Real nio SDK against Synapse; sync-loop ingress tracked                                | Docker only; live Matrix claims not proven             |
| **Docker Meshtastic**    | `pytest tests/integration/test_meshtasticd_sdk_bridge.py -m docker -v`          | Real mtjk SDK lifecycle and outbound enqueue                                           | Inbound pubsub unconfirmed; no real radio              |
| **Docker cross-adapter** | `./scripts/ci/run-docker-bridge-artifacts.sh matrix_to_meshtastic`              | Real Matrix nio ingress + PipelineRunner + real Meshtastic SDK outbound to meshtasticd | Docker loopback only; no real radio or external Matrix |
| **Live Matrix**          | `MEDRE_* env vars set; pytest tests/test_matrix_live.py -m live -v`             | Real homeserver connectivity                                                           | Smoke only; not sustained throughput                   |
| **Live Meshtastic**      | Manual: requires real node                                                      | Not yet tested through MEDRE                                                           | No live radio evidence claimed                         |

**Source-tree note:** The smoke command above relies on
`examples/configs/fake-bridge-smoke.toml` from the source checkout (found
automatically when `PYTHONPATH=src` is set). Installed-package users must pass
`--config` explicitly with a config file generated by `medre config sample`
or written to match the smoke framework's adapter IDs and route shape.

For the full list of validation scenarios, see
[docs/architecture/transport-validation-matrix.md](../architecture/transport-validation-matrix.md).

The sections below (§8.1–§8.3) describe each tier in depth — what passing
proves, what failing means, and how to interpret results. The table above
provides the single command to run for each level.

## 9. Operational Checklist

When operating a multi-transport bridge:

1. **Inspect first.** When something seems wrong, start with `medre inspect
event` and `medre inspect receipts` to understand what happened. These
   read-only commands use `--storage-path` and need no config. For deeper
   investigation, use `medre inspect event --timeline` (covers `trace
event`), `medre inspect event --evidence` (covers `evidence --event`),
   or `medre inspect event --recovery` (covers `recover --event`). Reach
   for the specialized `trace`, `evidence`, and `recover` commands when
   you need standalone output or features beyond inspect flags.

2. **Read receipts in transport context.** A `sent` receipt means different things for Matrix vs. Meshtastic vs. LXMF. Consult the per-transport table in section 2.

3. **Expect radio packet loss.** Meshtastic and MeshCore targets will silently lose messages. This is normal. Monitor `sent` receipt counts, not delivery confirmations that do not exist.

4. **Do not over-retry radio transports.** Retrying a Meshtastic send five times does not guarantee delivery. It increases probability, but each retry adds radio congestion. Tune `RetryPolicy` per transport.

5. **Account for LXMF propagation delay.** An LXMF `sent` receipt does not mean the destination has the message. Do not alert on "sent but no response" for LXMF targets.

6. **Distinguish retry layers.** Adapter reconnect is not the same as pipeline delivery retry. A Meshtastic adapter reconnecting to its local node is independent of the pipeline retrying a failed delivery.

7. **Use replay carefully.** Replay is a lower-level supported command for recovery scenarios. `BEST_EFFORT` replay produces real messages. Always verify route matching with `RE_ROUTE` or `DRY_RUN` first.

8. **Trust receipt lineage.** The `attempt_number` and `parent_receipt_id` chain on receipts provides a complete audit trail. Use it to reconstruct what happened, not to assume what should have happened.

## 10. Route Attribution in Delivery Receipts

Every `DeliveryReceipt` now carries a `route_id` field identifying which route was responsible for the delivery attempt. This provides direct attribution from receipt back to route configuration.

**What this means for operators:**

- When inspecting receipts, the `route_id` field tells you which route triggered this delivery. If `route_id` is empty, the delivery was not routed (e.g., direct adapter-to-adapter delivery without route matching).
- In fan-out scenarios (one event routed to multiple targets), each target's receipt carries the same `route_id`. This lets you group all deliveries from a single route invocation.
- Failed receipts also carry `route_id`. You can query all failed deliveries for a specific route to identify systematic issues.

**Where else attribution appears:**

| Location                           | Field             | Lifecycle                                               |
| ---------------------------------- | ----------------- | ------------------------------------------------------- |
| `RoutingMetadata.route_trace`      | `tuple[str, ...]` | Ephemeral — on the in-flight event after route matching |
| `DeliveryReceipt.route_id`         | `str`             | Persisted — stored with the receipt in storage          |
| `DeliveryOutcome.route_id`         | `str`             | Ephemeral — pipeline-internal result, not persisted     |
| `ReplayRouteAttribution.route_ids` | `tuple[str, ...]` | Replay result only — not persisted to events            |

**Attribution does not cross adapter boundaries.** Adapters do not receive or consume route attribution metadata. Attribution is orchestration-layer information for observability and audit.

See: Contract 51 (Route Attribution), Contract 52 (Routed Delivery Result).

## 11. Route Loop Prevention

MEDRE detects and prevents routing loops at multiple layers. This section describes what operators should know about loop behavior in bridge scenarios.

### 11.1 Direct Loop Detection (Startup)

At startup, `check_route_loops` detects two forms of loops in route configuration:

- **Direct loops:** Two routes forming an immediate A↔B cycle (e.g., route X: `bot1 → longfast` and route Y: `longfast → bot1`).
- **Multi-hop cycles:** Routes forming a cycle through three or more adapters via DFS traversal (e.g., `alpha → beta → gamma → alpha`).

Both are logged as warnings. Startup is **not blocked**. The operator should review and fix cycle-inducing routes.

### 11.2 Self-Loop Guard (Runtime, Per-Delivery)

During delivery execution, the pipeline checks each target: if `target_adapter == event.source_adapter`, the delivery is skipped. The outcome records `status="skipped"` with `error="loop_prevented"`. No adapter call is made. `RouteStats` records the prevention.

This guard fires on every delivery attempt. It catches runtime self-loops that configuration-level detection may not prevent (e.g., a bidirectional route where a single adapter appears in both source and destination after expansion).

### 11.2a Native-Ref Duplicate Suppression (Runtime, Per-Ingress)

At pipeline Stage 1.5, `PipelineRunner.handle_ingress` checks each inbound event's `source_native_ref` against previously stored native message references. If a matching ref is found, the event is dropped before routing. This prevents echo when a radio transport re-delivers the same packet (e.g., MeshCore session retries or LXMF store-and-forward redelivery).

Native-ref dedup requires stable adapter-provided native IDs. Which adapters provide them:

| Adapter    | Native ID source                  | Stability                                                                                                            |
| ---------- | --------------------------------- | -------------------------------------------------------------------------------------------------------------------- |
| Matrix     | `event_id`                        | Stable. Synapse-assigned, globally unique per event.                                                                 |
| Meshtastic | `packet_id`                       | Stable per packet, but may collide under high churn (IDs are small integers reused across sessions).                 |
| MeshCore   | `sender_timestamp`                | Stable per node. Distinguishes messages from the same sender but is not globally unique across nodes.                |
| LXMF       | `source_hash + nonce` combination | May vary depending on codec implementation. LXMF does not guarantee a globally unique, stable ID in all codec paths. |

**Native-ref dedup is NOT replay dedupe.** Replay (section 7) produces independent canonical events and receipts. Replayed events get new `event_id` values and new `DeliveryReceipt` rows. Native-ref dedup prevents echo from transport-layer re-delivery of the same physical packet; it does not suppress replay-originated events. Multiple `BEST_EFFORT` replays of the same original event will produce additional deliveries.

### 11.2b Route-Trace Guard (Runtime, Per-Delivery)

During delivery execution, `PipelineRunner._execute_single_delivery` inspects the `route_trace` on the event's `RoutingMetadata`. The route_trace records which route IDs have already processed the event. If the current route ID appears more than once in the trace, the delivery is skipped. This catches multi-hop cycles that escape the self-loop guard (e.g., A→B→C→A where no single delivery targets the source adapter).

### 11.3 What Loop Prevention Does Not Cover

- **Cross-instance loops:** If two separate MEDRE instances bridge the same transports in opposite directions, neither instance detects the loop. Loop prevention is local-process only.
- **Application-level loops:** A user on Matrix commanding a bot to send a message to Meshtastic, and a Meshtastic user replying which triggers a message back to Matrix, is not a routing loop — it is normal bidirectional bridge operation. Loop prevention guards against the same event being routed back to its origin adapter, not against new events generated by users.
- **Duplicate suppression dependency on stable native IDs:** Duplicate suppression (Stage 1.5) depends on adapters providing stable, non-null `native_message_id` values via `source_native_ref`. Adapters that return `None` or an empty string for `native_message_id` bypass dedup entirely — every inbound event from that adapter is treated as novel. Operators should verify that the adapter's codec populates `native_message_id` for the relevant packet types. See Contract 49 §6.6.
- **Callback exception isolation:** Callback exceptions are isolated per callback. An exception in one adapter's inbound callback does not stop subsequent callbacks from processing. Each callback is wrapped in a try/except that logs the exception and continues. This isolation is at the callback-dispatch level — if the underlying SDK loop crashes, callbacks cease entirely.
- **Shutdown semantics under active ingress:** When the runtime initiates shutdown while ingress is active, existing deliveries complete or abort deterministically. New deliveries attempted after shutdown begins are rejected with `SHUTDOWN_REJECTION`. Events already persisted in SQLite survive shutdown. There is no ambiguous "maybe delivered" state — each in-flight delivery resolves to `sent`, `failed`, or is cancelled cleanly. In-flight deliveries that cannot complete are lost; they are not queued for retry after restart.

See: Contract 49 (Routing and Bridge), Routing Correctness Runbook.

## 12. Soak Harness and Queue Pressure

### Soak Harness Reference

The soak harness at `tests/test_soak_harness.py` provides a test-only harness for validating bridge stability patterns without live transports. It is **not** a multi-hour CI run — it exercises start/stop cycling, replay cycling, delivery under pressure, and long-running stability within seconds using fake adapters and in-memory storage.

Key characteristics:

- **Fake adapters only.** No real Matrix homeserver, radio, or Reticulum network.
- **In-memory storage.** No filesystem I/O beyond temp directories.
- **Deterministic.** No wall-clock sleeps. Iteration count configurable via `SOAK_HARNESS_ITERATIONS` (default 50, max 200).
- **Validates patterns, not completeness.** The harness verifies that the pipeline correctly routes, delivers, and reports outcomes under repeated cycling. It does not validate that every message reaches its destination (MEDRE does not provide this guarantee).

### Queue Pressure Expectations

When bridging events across transports with different speed profiles (e.g., Matrix → Meshtastic), the pipeline may experience queue pressure:

**Delivery capacity pressure:**

- The `CapacityController` bounds concurrent deliveries to `max_inflight_deliveries` (default 100).
- When the Meshtastic adapter's transport is slow (LoRa PHY, serial write blocking), delivery slots are held longer.
- Other adapters (Matrix, LXMF) compete for the same delivery semaphore pool.
- If delivery acquire times out (`delivery_acquire_timeout_seconds`, default 1.0s), the delivery is permanently failed with `error="delivery_capacity_exceeded"`.

**Meshtastic outbound queue pressure:**

- The Meshtastic adapter's `MeshtasticOutboundQueue` uses an unbounded deque with explicit enqueue-time capacity enforcement (default `max_queue_size=1024`).
- When the queue is full, `enqueue()` raises `MeshtasticSendError(transient=True)` instead of silently dropping items.
- `queue_total_rejected` tracks how many enqueue attempts were rejected.
- This is expected behavior for radio transports — the runtime prioritizes stability over completeness.

**Replay pressure:**

- Replay in `BEST_EFFORT` mode acquires a separate replay semaphore (`max_inflight_replay_events`, default 100).
- Replay does not starve real-time delivery — the semaphores are independent.
- If the replay semaphore is exhausted, replay events are rejected with `error="replay_capacity_exceeded"` (or `error="replay_rejected_shutdown"` during runtime shutdown).

### Monitoring Bridge Pressure

During bridge operation, monitor these signals:

| Signal                                 | Source               | Interpretation                                      |
| -------------------------------------- | -------------------- | --------------------------------------------------- |
| `capacity_rejections` growing          | `CapacityController` | Delivery concurrency is insufficient for the load   |
| `queue_total_rejected` growing         | Meshtastic adapter   | Outbound send rate cannot keep up with inbound rate |
| `capacity_rejections` growing (replay) | `CapacityController` | Replay concurrency is insufficient                  |
| High `delivery_current` sustained      | `CapacityController` | Adapters are slow to complete deliveries            |

**Remediation:**

- Increase `max_inflight_deliveries` if delivery timeouts are frequent and memory allows.
- Reduce active routes or source event rate if the bridge cannot keep up.
- For Meshtastic specifically, consider whether the channel configuration and radio settings can be optimized for throughput.

**Important:** MEDRE remains best-effort. Queue bounds prevent unbounded accumulation but do not prevent data loss under extreme pressure. No exactly-once guarantees. No transactional delivery guarantees. Radio transports remain probabilistic. The soak harness validates stability patterns for CI — it is not a substitute for operational monitoring with live transports.

## 13. Persistence of Bridge State

Bridge delivery state has a clear persistence boundary. This section describes what bridge operators can rely on and what is ephemeral. For the full contract, see Contract 55 (Runtime Persistence).

### What Persists Across Restarts

- **Delivery receipts** — every receipt written to SQLite survives crash and restart. Receipts include `route_id` attribution, `attempt_number`, retry lineage, and adapter-reported native IDs.
- **Canonical events** — every event that entered the pipeline was stored in SQLite before delivery began. These survive.
- **E2EE sessions** — Matrix crypto keys on disk survive restart. Bridging resumes without re-verification.
- **Logs** — all log entries written before the crash are in `{state}/logs/medre.log`.

### What Does NOT Persist

- **In-flight bridge deliveries** — if the runtime crashes while a Matrix-to-Meshtastic bridge delivery is in progress, the in-memory delivery is lost. The source event exists in SQLite (it was stored before delivery), and a `delivery_outbox` row with status `in_progress` may survive if the pipeline created it before the adapter call. An expired `in_progress` outbox row is reclaimable by the RetryWorker on restart. If no outbox row exists, the operator cannot distinguish "delivery was attempted but crashed" from "delivery was never attempted." Receipt absence alone is no longer authoritative — check `delivery_outbox` alongside `delivery_receipts` during crash analysis.
- **Runtime bridge counters** — `capacity_rejections`, `outbound_failed`, per-route delivery counts: all reset to zero on restart. There is no persistent metric store.
- **Active replay deliveries** — if a `BEST_EFFORT` replay was bridging historical events when the crash occurred, the replay run is lost. Completed deliveries from that replay run (those that produced receipts) are preserved. Remaining events must be re-replayed manually.

### Bridge Crash Recovery Example

After a hard crash during active Matrix-to-Meshtastic bridging, follow the
inspect-first product path. Start by looking at what happened, then decide
whether replay is warranted.

1. Restart the runtime. Both adapters reconnect.
2. **Inspect first.** Use read-only commands to understand the state:

   ```bash
   # Check a specific event (no config needed):
   medre inspect event <event_id> --storage-path /path/to/medre.sqlite

   # Check delivery receipts for that event:
   medre inspect receipts --event <event_id> --storage-path /path/to/medre.sqlite

   # For deeper per-event investigation (covers trace/evidence/recover output):
   medre inspect event <event_id> --storage-path /path/to/medre.sqlite --timeline
   medre inspect event <event_id> --storage-path /path/to/medre.sqlite --evidence
   medre inspect event <event_id> --storage-path /path/to/medre.sqlite --recovery
   ```

3. Find orphaned events (stored but not delivered) via SQL:
   ```sql
   SELECT e.event_id, e.source_adapter, e.created_at
   FROM canonical_events e
   LEFT JOIN delivery_receipts r ON e.event_id = r.event_id
   WHERE r.event_id IS NULL
     AND e.source_adapter = 'bridge'
   ORDER BY e.created_at DESC;
   ```
4. If deeper investigation is needed beyond the inspect flags, use the
   specialized commands: `medre trace event` for standalone timeline output,
   or `medre evidence` for a full bridge evidence bundle.
5. Decide whether to replay the orphaned events. Use `DRY_RUN` first to
   verify route matching, then `BEST_EFFORT` if re-delivery is warranted.
   Replay is a lower-level supported command that produces duplicate
   deliveries by design.
6. Expect possible duplicate deliveries — replay does not deduplicate.

For the full crash recovery workflow and decision tree, see
[Bridge Recovery](bridge-recovery.md). For the inspect-first product path,
see the [Alpha Walkthrough](alpha-walkthrough.md). For tracing events through
the pipeline, see [Event Tracing](event-tracing.md). For the replay workflow,
see [Replay Operation](replay-operation.md).

### Smoke Command Does Not Persist

The `medre smoke` command uses in-memory storage by default. Receipts,
events, and accounting data produced during a default smoke run are not written
to SQLite and are not inspectable with `medre inspect` after the process
exits. The JSON report printed to stdout is the only surviving record.

Pass `--storage-path <path>` to persist evidence to a SQLite database that
`medre inspect` can query afterward. When `--storage-path` is provided, all
events, receipts, and native refs are written to the specified database file.

For durable inspection of bridge delivery state, use `medre run` with
`[storage] backend = "sqlite"` and inspect the database afterward. See the
[Fake Bridge Smoke Runbook](fake-bridge-smoke-runbook.md#smoke-persistence-caveat)
for details, the [Bridge Failure Drills](bridge-failure-drills.md) runbook
for failure interpretation guidance, and the
[Bridge Evidence Bundle](bridge-evidence-bundle.md) runbook for collecting
smoke, drill, and inspect outputs as a single pre-runtime evidence package
via `medre evidence`.

## 14. Explicit Non-Guarantees

The bridge operation layer explicitly does **not** provide:

1. **Replay deduplication.** Replay processes events without deduplication. Replayed events may be delivered again if they match current routes.

2. **Exactly-once delivery.** No transport in MEDRE provides exactly-once semantics. Radio transports are probabilistic. Matrix is at-least-once. LXMF is at-least-once with eventual delivery.

3. **No durable adapter-local queue.** Adapter-local outbound queues (e.g., Meshtastic outbound queue) are in-memory and non-durable — queue contents are lost on process termination. Durable `delivery_outbox` tracking rows (including `queued` rows committed before a crash) may survive in SQLite, but in-flight delivery execution state is ephemeral and cannot be resumed after restart. No persistent in-flight recovery beyond outbox lease reclamation. No replay resume.

4. **Per-adapter restart.** Only full runtime stop/start is supported. Individual adapters cannot be restarted independently.

5. **Distributed coordination.** Delivery state, receipts, and loop prevention are local to the process. There is no shared state between MEDRE instances.

6. **Exactly-once or transactional delivery.** MEDRE provides no exactly-once delivery, no transactional delivery guarantees, and no atomic fan-out. Partial delivery in fan-out scenarios is normal.

7. **Queue-bound delivery completeness.** Capacity semaphores and adapter-level queue bounds prevent unbounded memory accumulation but do not guarantee that every message is delivered. Under extreme pressure, messages are dropped or rejected to protect process stability.

8. **Persistent in-flight recovery.** In-flight delivery state does not survive as in-memory state. `in_progress` outbox rows with expired leases are reclaimable by RetryWorker; deliveries without outbox rows are lost. No replay resume after restart.

9. **Adapter-local outbound queue durability.** The Meshtastic adapter's outbound queue is in-memory and non-durable. Items remaining in the queue at process termination (graceful or ungraceful) are lost. The `delivery_outbox` table provides durable operational tracking — a `queued` outbox row may survive if committed before the crash — but adapter-local queue contents themselves are not persisted.

10. **Outbound gate suppression is non-retryable.** When `outbound_mode = "listen_only"` is configured on a Meshtastic adapter, suppressed deliveries are classified as non-retryable adapter failures. The pipeline does not retry them because the suppression is an intentional operator decision, not a transient transport error.

## 15. Shutdown Snapshot and Bridge Evidence

### Capturing Bridge State at Shutdown

For long-running bridge deployments, operators can capture the final runtime state after graceful shutdown using the `--snapshot-on-shutdown` flag:

```bash
medre run --config bridge.toml --snapshot-on-shutdown
```

This writes a snapshot JSON file to `{state_dir}/shutdown-snapshot.json` containing the runtime's final accounting counters, capacity gauges, adapter lifecycle state, route delivery statistics, and the bounded runtime events buffer. The snapshot is captured **after** graceful shutdown completes, so `lifecycle.runtime_state` will be `"stopped"`. Accounting counters are also printed to the console as a compact one-line summary at shutdown.

The shutdown snapshot is particularly valuable for bridge operators because it preserves:

- **Route delivery statistics** (`routes.stats`) — per-route delivery/failure/skip counts accumulated during the run.
- **Accounting counters** (`accounting`) — total inbound accepted, outbound attempts, outbound delivered, outbound failed.
- **Capacity gauges** (`capacity`) — delivery timeouts, rejections, and current concurrency at shutdown time.
- **Runtime events** (`diagnostics.runtime_events`) — process-local events (adapter failures, route skips, startup classifications) that are otherwise lost on process exit.

These values are process-local and non-durable. Without `--snapshot-on-shutdown`, they are lost when the process exits. With it, the snapshot file survives as a post-run artifact.

**Caveats:**

- The snapshot is a point-in-time capture, not a continuous log. It reflects the state after graceful shutdown completes (`lifecycle.runtime_state` will be `"stopped"`).
- The RetryWorker is an opt-in background task that polls for transient-failure receipts with `next_retry_at` set. Retry receipts carry `source="retry"`. No final ACK guarantee. Runtime events are process-local.
- Replay is manual and duplicate-risky. The snapshot may show replay receipts from earlier runs, but cannot tell you which delivery actually reached the remote side.

### Run-Time Evidence vs Post-Run Evidence

Bridge operators should distinguish between two categories of evidence:

**Run-time evidence** (available while `medre run` is active):

| Source                | How to access                                | Lifecycle                            |
| --------------------- | -------------------------------------------- | ------------------------------------ |
| Log output            | Console stdout/stderr, `{log_dir}/medre.log` | Written continuously during the run  |
| Runtime events buffer | `diagnostics.runtime_events` in snapshot     | Bounded, process-local, lost on exit |
| Accounting counters   | `accounting` in snapshot                     | Process-local, reset on startup      |
| Capacity gauges       | `capacity` in snapshot                       | Process-local, reset on startup      |
| Route delivery stats  | `routes.stats` in snapshot                   | Process-local, reset on startup      |

**Post-run evidence** (available after `medre run` exits):

| Source                  | How to access                                                   | Lifecycle                                         |
| ----------------------- | --------------------------------------------------------------- | ------------------------------------------------- |
| Delivery receipts       | `medre inspect receipts`                                        | Persisted in SQLite                               |
| Canonical events        | `medre inspect event`                                           | Persisted in SQLite                               |
| Event timelines         | `medre inspect event --timeline`                                | Persisted in SQLite                               |
| Event evidence bundles  | `medre inspect event --evidence`                                | Persisted in SQLite                               |
| Event recovery runbooks | `medre inspect event --recovery`                                | Persisted in SQLite                               |
| Native message refs     | `medre inspect native-ref`                                      | Persisted in SQLite                               |
| Replay receipts         | `medre inspect receipts --replay-run`                           | Persisted in SQLite                               |
| Shutdown snapshot       | `{state_dir}/shutdown-snapshot.json`                            | File on disk (only with `--snapshot-on-shutdown`) |
| Evidence bundle         | `medre evidence --config <path> --json`                         | Re-generated on demand from SQLite                |
| Event trace             | `medre inspect event <event_id> --timeline --storage-path <db>` | Re-generated on demand from SQLite                |

**Key distinction:** Run-time evidence (counters, gauges, events buffer) is process-local memory that is lost when the process exits unless captured via `--snapshot-on-shutdown`. Post-run evidence (receipts, events, refs) is persisted in SQLite and survives process termination.

For the full shutdown snapshot schema, see [Runtime Operation — Shutdown Snapshot](runtime-operation.md#shutdown-snapshot---snapshot-on-shutdown). For the evidence bundle report shape, see [Bridge Evidence Bundle](bridge-evidence-bundle.md).

## 16. Bridged Message Appearance

This section documents what a message actually looks like when bridged from one
transport to another. The rendering pipeline converts a `CanonicalEvent` into a
target-adapter-ready payload via transport-specific renderers. The format differs
per target transport.

### Matrix → Meshtastic

A message originating from Matrix is rendered by `MeshtasticRenderer` into a
plain-text payload:

```python
{
    "text": "<body text from Matrix event>",
    "channel_index": 0,          # parsed from target_channel or default 0
    "meshnet_name": "",          # placeholder (tranche 1)
}
```

The `text` field is extracted from the event payload's `body` key (falling back
to `text`). No Matrix formatting, HTML, or metadata is preserved in the
Meshtastic output. The source adapter label is **not** included in the radio
text. Truncation to Meshtastic's ~228-byte payload limit is **not** enforced in
tranche 1 (noted as TODO).

### Meshtastic → Matrix

A message originating from Meshtastic is rendered by `MatrixRenderer` into an
`m.room.message` content dict:

```python
{
    "msgtype": "m.text",
    "body": "<decoded text from Meshtastic packet>",
    "medre": {
        "envelope": {
            "schema_version": 1,
            "canonical_event_id": "<event_id>",
            "source_adapter": "<source adapter name>",
            "source_channel": "<source channel id>",
            "metadata_mode": "safe",
            ...
        }
    }
}
```

The `body` is the decoded text from the Meshtastic packet. A MEDRE provenance
envelope is embedded in the `medre.envelope` subtree recording the source
adapter and channel. If the event carries a reply relation, the rendered output
includes `m.relates_to` with `m.in_reply_to` referencing the original message
ID, and the body is formatted with a quoted fallback prefix
(`> <sender> original_text`).

### Source adapter label

The source adapter label (e.g. `"meshtastic-radio-1"`, `"matrix-src"`) is:

- **Included** in Matrix renderer output via the `medre.envelope.source_adapter`
  field.
- **Included** in LXMF renderer output via the fields envelope
  (`FIELD_MEDRE_ENVELOPE` / `0xFD`).
- **Not included** in Meshtastic or MeshCore renderer output (these renderers
  produce plain text payloads with no metadata envelope in tranche 1).

### Reply threading

Reply threading preservation depends on the target renderer:

| Target renderer | Reply support    | What happens                                                                      |
| --------------- | ---------------- | --------------------------------------------------------------------------------- |
| Matrix          | ✅ Supported     | `m.relates_to` with `m.in_reply_to` added; body includes quoted fallback          |
| Meshtastic      | ❌ Not supported | Reply relations are ignored; only body text is rendered                           |
| MeshCore        | ❌ Not supported | Same as Meshtastic — plain text only                                              |
| LXMF            | Partial          | Relations are recorded in the fields envelope but not used for display formatting |

Reply context that survives the bridge: Matrix ↔ Matrix (full `m.relates_to`),
any → Matrix (reply relation rendered with fallback text). Reply context that
does **not** survive: any → radio transport (Meshtastic, MeshCore) — the reply
relation is dropped at rendering time.

## 17. Opt-in Docker Bridge Artifact Collection

An opt-in artifact collection path is available for producing structured
evidence from Docker Matrix <-> Meshtastic bridge validation runs. This path
is **not** invoked by default CI — it requires explicit activation.

**What it does:**

1. Creates a timestamped run directory under
   `.ci-artifacts/docker-bridge-runs/<timestamp>/`.
2. Runs Docker integration tests for a given scenario
   (`matrix_to_meshtastic`, `meshtastic_to_matrix`, `bidirectional`).
3. Captures pytest stdout/stderr, config snapshots, and inspect artifacts.
4. Writes `summary.json` with structured evidence, **even on failure**.
5. Redacts tokens/passwords from all artifacts using
   `sanitize_for_log` and `sanitize_error`.

**Cross-adapter proof (`matrix_to_meshtastic`):**

The `matrix_to_meshtastic` scenario proves real cross-adapter event flow:
real Matrix nio SDK ingress through the sync loop, PipelineRunner routing
through MeshtasticRenderer, and real Meshtastic mtjk SDK outbound delivery
to meshtasticd returning a genuine packet ID. This is the strongest
SDK-to-SDK evidence available without real radio hardware or external
accounts.

**Usage:**

```bash
./scripts/ci/run-docker-bridge-artifacts.sh [scenario]
```

**Required `summary.json` fields:**
`status`, `scenario`, `matrix` (container/room/event_id/ingress_path),
`meshtastic` (daemon/inbound/outbound), `medre` (event_id/receipt/native_refs/runtime/limitations),
`errors`, `limitations`.

**Honesty requirements:**

- No real external Matrix account or real radio is proven.
- All services run on localhost in Docker containers (loopback only).
- No automated queue draining, real pubsub Meshtastic inbound, or sustained
  throughput is claimed.
- Limitations explicitly state localhost-only, container-local validation.
- On failure, `summary.json` is written with `status: "failed"` or
  `"partial"` and populated `limitations`.

For full artifact documentation, the manual inspection walkthrough, and
scenario-aware file lists, see [Docker Bridge Artifacts](docker-bridge-artifacts.md).
