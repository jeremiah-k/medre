# SDK Parity Opportunities Backlog

**Branch**: `development-1`
**Baseline**: Post `adapter-sdk-parity` / after #99
**Status**: Backlog for future work packages
**Date**: 2026-06-11
**Scope**: Runtime behavioral parity across all four adapters — Matrix, Meshtastic, MeshCore, LXMF
**Purpose**: Ranked backlog of SDK parity improvements that MEDRE should likely adopt from reference implementations, with rationale for every proposed parity move.

This document is a **ranked backlog** for future implementation waves. It identifies gaps and proposes actions; implementation belongs to later work packages.

---

## Methodology

Each item was identified by comparing MEDRE's current adapter runtime behavior against the following reference sources:

| Reference                                 | Version / Commit    | What was compared                                                                                                 |
| ----------------------------------------- | ------------------- | ----------------------------------------------------------------------------------------------------------------- |
| mmrelay (meshtastic-matrix-relay `1.3.8`) | `7b9efca`           | Meshtastic connection health, reconnect policy, Matrix sync lifecycle, queue management                           |
| meshtastic-python (mtjk)                  | v2.7.8.post3        | Pubsub callback lifecycle, `sendText` return shape, connection-lost detection                                     |
| meshcore_py                               | v2.3.7              | `send_msg` return shape (`expected_ack`, `suggested_timeout`), `EventType` subscriptions, `auto_message_fetching` |
| MeshCore firmware                         | snapshot 2026-04-28 | ACK protocol, APP_START command requirements                                                                      |
| LXMF                                      | v0.9.6              | `LXMessage` state model, announce mechanism, delivery callbacks                                                   |
| Reticulum docs                            | accessed 2026-06-10 | `RNS.Destination` constructor, identity recall, announce API                                                      |
| mindroom-nio                              | v0.25.3             | `sync_forever` key management loop, `restore_login` pattern, rate-limit handling                                  |
| MEDRE `docs/dev/adapter-reality-audit.md` | 2026-06-05          | Prior audit findings (R1–R10) and remaining risks                                                                 |
| MEDRE `docs/dev/reference-repos.md`       | current             | Boundary rules on what not to copy                                                                                |

Items are ranked by **operational value** — the impact on real deployment reliability when the gap manifests. Within the same operational value tier, items are ordered by estimated implementation risk (lower risk first).

### Gap Classification

- **Behavioral gap**: MEDRE's runtime code path behaves differently from the reference in a way that could cause operational failures, missed events, or resource leaks under real deployment conditions.
- **Declarative/capability gap**: MEDRE does not expose or use a capability that the SDK provides, but the absence does not cause incorrect runtime behavior.

---

## Backlog Items

### P-01. Meshtastic: No periodic connection health verification

**Rank**: 1 (highest operational value)

| Field     | Value                                       |
| --------- | ------------------------------------------- |
| Adapter   | Meshtastic                                  |
| Reference | mmrelay `meshtastic_utils.py` lines 909–987 |
| Gap type  | Behavioral                                  |

**Observed MEDRE behavior**: The Meshtastic session relies solely on the SDK's `meshtastic.receive` pubsub callback and an explicit `notify_connection_lost()` call. There is no periodic health check. If the TCP connection drops silently (no RST, no FIN — e.g., network middlebox timeout), MEDRE will never detect the loss and will stop receiving inbound packets.

**Reference behavior**: MMRelay runs a periodic health check (`check_meshtastic_connection`) that calls `_get_device_metadata(client)`. If metadata parsing fails, it falls back to `client.getMyNodeInfo()`. If both fail, `on_lost_meshtastic_connection()` triggers reconnect. The check runs on a configurable interval and is suppressed while reconnecting.

**Gap**: MEDRE has no equivalent of a liveness probe. A Meshtastic TCP connection that enters a half-open state will cause the bridge to silently stop relaying packets until a send attempt fails (which may be indefinitely if no outbound traffic exists).

**Operational value**: **High**. Silent connection loss is the primary failure mode in TCP-based Meshtastic deployments (especially over VPNs, NATs, or long-running sessions). Without health checks, the bridge appears healthy in diagnostics but is actually deaf.

**Risk**: **Low–Medium**. Implementation requires a periodic task in the session or adapter that probes the SDK client's `myInfo` or sends a lightweight request. The main risk is interaction with the SDK's internal thread — MMRelay works around this with careful exception handling.

**Proposed next action**: Add a configurable `health_check_interval_seconds` (default 60) to `MeshtasticConfig`. In the session, start a periodic task that attempts `client.getMyNodeInfo()` with a bounded timeout. On failure, call `notify_connection_lost()`. Suppress checks while reconnecting.

---

### P-02. Meshtastic: No SDK connection-lost event subscription

**Rank**: 2

> **Resolved** (development-1): Code now subscribes to
> `meshtastic.connection.lost` in `MeshtasticSession._subscribe_callbacks()`
> via
> `pub.subscribe(self._on_connection_lost, "meshtastic.connection.lost")`.
> Unsubscribes in `_unsubscribe_callbacks()`. Flag
> `_subscribed_connection_lost` prevents duplicate subscriptions.

| Field     | Value                                       |
| --------- | ------------------------------------------- |
| Adapter   | Meshtastic                                  |
| Reference | mmrelay `meshtastic_utils.py` lines 398–408 |
| Gap type  | Behavioral                                  |

**Observed MEDRE behavior**: The session subscribes to `meshtastic.receive` only. It does not subscribe to any connection-lost or disconnect event from the SDK.

**Reference behavior**: MMRelay subscribes to `meshtastic.connection.lost` via `pub.subscribe(on_lost_meshtastic_connection, "meshtastic.connection.lost")` and tracks subscription state with a `subscribed_to_connection_lost` flag.

**Gap** (historical): When the Meshtastic SDK itself detects a connection loss (e.g., the node sends a disconnect, the TCP stream closes cleanly), the SDK fires a `meshtastic.connection.lost` pubsub event. MEDRE did not listen for this event. The session would only notice the loss when the next send attempt failed or when the next inbound packet didn't arrive — which is passive and unreliable.

**Operational value**: **High**. This is the primary connection-loss detection mechanism provided by the SDK. Without it, MEDRE misses the most authoritative signal that the connection is gone.

**Risk**: **Low**. Adding a pubsub subscription for `meshtastic.connection.lost` that calls `notify_connection_lost()` is straightforward. The callback fires on the SDK's reader thread, same as `_on_receive`, so the threading model is unchanged.

**Proposed next action**: _Completed._ In `MeshtasticSession._subscribe_callbacks()`, added `pub.subscribe(self._on_connection_lost, "meshtastic.connection.lost")`. Implemented `_on_connection_lost` to call `notify_connection_lost()`. Unsubscribe in `_unsubscribe_callbacks()`.

---

### P-03. Matrix: No sync token persistence across restarts

**Rank**: 3

| Field     | Value                                                                 |
| --------- | --------------------------------------------------------------------- |
| Adapter   | Matrix                                                                |
| Reference | mmrelay `matrix_utils.py` lines 815–820; nio `store_sync_tokens=True` |
| Gap type  | Behavioral                                                            |

**Observed MEDRE behavior**: MEDRE creates the nio `AsyncClient` with `store_path` for E2EE, but does not explicitly set `store_sync_tokens=True` in its `AsyncClientConfig`. After restart, the sync starts from the beginning (or from whatever `next_batch` token nio internally retains via its store). If the store path is not configured (plaintext mode), there is no sync token persistence at all.

**Reference behavior**: MMRelay explicitly passes `store_sync_tokens=True` to the nio client config (lines 817, 1090). This tells nio to persist the `next_batch` token in its store so that after a restart, the sync resumes from where it left off rather than replaying all events since the beginning of time.

**Gap**: In plaintext mode or when `store_path` is not set, MEDRE's Matrix session will perform a full initial sync on every restart. For accounts in many rooms or with long histories, this causes a burst of inbound events on startup, all of which are suppressed by the startup-backlog filter — but the sync itself is slow and resource-intensive.

**Operational value**: **High**. Full initial sync on every restart is the primary cause of slow Matrix adapter startup. Persisting the sync token allows the adapter to resume incremental sync immediately, reducing startup time from tens of seconds to sub-second in most cases.

**Risk**: **Low**. Adding `store_sync_tokens=True` to the `AsyncClientConfig` is a one-line change. The behavior is additive — even if the store is missing, nio falls back to a full sync. This has been proven in MMRelay's production deployment.

**Proposed next action**: In `MatrixSession._start_e2ee_required()` and `_start_plaintext()`, add `store_sync_tokens=True` to the `AsyncClientConfig` (or constructor kwargs). Verify that plaintext mode also receives a `store_path` for sync token storage, or add a lightweight token persistence mechanism.

---

### P-04. MeshCore: Unused `suggested_timeout` from SDK send result

**Rank**: 4

> **Partially resolved** (development-1): `_send_real()` now extracts
> `suggested_timeout` from dict results and `result.payload` dicts. Remaining
> gap: `result.attributes` dict is not yet checked. The retry delay uses the
> extracted timeout for DM retries with floor/ceiling clamping.
> `sdk_suggested_timeouts_used` counter incremented on valid extraction.

| Field     | Value                                                 |
| --------- | ----------------------------------------------------- |
| Adapter   | MeshCore                                              |
| Reference | meshcore_py v2.2.5 `MeshCore.send_msg()` return shape |
| Gap type  | Behavioral                                            |

**Observed MEDRE behavior**: The session extracts `expected_ack` from the `send_msg()` result (used as `native_message_id`) but discards `suggested_timeout`. The send retry uses a fixed linear backoff (`0.1 * attempt`) regardless of what the SDK recommends.

**Reference behavior**: meshcore_py's `send_msg()` returns an `Event` with `expected_ack` (4-byte hex) and `suggested_timeout` (integer, seconds). The `suggested_timeout` is the SDK's estimate of how long to wait for an ACK before considering the send failed. The firmware calculates this based on radio conditions and hop count.

**Gap** (partially resolved): `suggested_timeout` is now extracted from the send result's top-level dict and `result.payload` dict, and used for DM retry delays with floor/ceiling clamping. The `sdk_suggested_timeouts_used` diagnostic counter is incremented on valid extraction. **Remaining**: the `result.attributes` dict is not yet checked as a third extraction source. If the SDK returns timeout information exclusively through `result.attributes`, it will still be missed.

**Operational value**: **Medium–High**. On links with high latency (multi-hop MeshCore networks), MEDRE will incorrectly classify sends as transient failures and retry unnecessarily, generating duplicate messages. Using the SDK's timeout hint would eliminate false transient failures.

**Risk**: **Low**. The `suggested_timeout` is an integer in the result dict. Passing it to the retry delay calculation is a small code change. The SDK already provides this value; MEDRE just needs to use it.

**Proposed next action**: Extend `_send_real()` to also check `result.attributes` for `suggested_timeout`. With `result.payload` and top-level dict already covered, this completes the extraction across all known return shapes.

---

### P-05. MeshCore: No subscription to contact-list or self-info events

**Rank**: 5

> **Resolved** (development-1): `_subscribe_events()` now subscribes to
> `CONTACTS` and `SELF_INFO` event types (guarded by `hasattr` checks). Events
> update diagnostics (`known_contact_count`). `NEW_CONTACT` subscription is
> intentionally deferred — not needed for current routing.

| Field     | Value                                                |
| --------- | ---------------------------------------------------- |
| Adapter   | MeshCore                                             |
| Reference | meshcore_py v2.2.5 `MeshCore.__init__` lines 322–327 |
| Gap type  | Declarative/capability                               |

**Observed MEDRE behavior**: The session subscribes to three event types: `CONTACT_MSG_RECV`, `CHANNEL_MSG_RECV`, and `DISCONNECTED`. It does not subscribe to `CONTACTS`, `NEW_CONTACT`, `SELF_INFO`, `CURRENT_TIME`, `ADVERTISEMENT`, or `PATH_UPDATE`.

**Reference behavior**: The meshcore_py SDK itself subscribes to `CONTACTS`, `NEW_CONTACT`, `SELF_INFO`, `CURRENT_TIME`, `ADVERTISEMENT`, and `PATH_UPDATE` internally to maintain its own state. These events update the SDK's internal contact list, self-info, and path tables.

**Gap** (historical): Without subscribing to `CONTACTS` or `NEW_CONTACT`, MEDRE cannot detect when new contacts appear on the mesh. This matters for routing: if a new peer's public key becomes reachable, MEDRE has no way to learn about it proactively. The adapter can only send DMs to contacts that were already known at startup.

**Operational value**: **Medium**. For static deployments where all peers are known at startup, this gap has no impact. For dynamic mesh networks where peers join and leave, MEDRE's DM routing will be stale.

**Risk**: **Low**. Adding subscriptions to additional event types is straightforward. The events carry the same dict payload structure as existing subscriptions. The main design decision is whether to expose contact-list changes as MEDRE diagnostic data or as canonical events (currently no canonical event type covers mesh topology changes).

**Proposed next action**: _Mostly completed._ `CONTACTS` and `SELF_INFO` subscriptions are now active with diagnostic updates. `NEW_CONTACT` remains intentionally deferred. No canonical events for topology changes are emitted — that remains a separate capability decision.

---

### P-06. LXMF: No periodic announce for mesh path discovery

**Rank**: 6

> **Resolved** (development-1): `LxmfSession` now runs a periodic
> `_announce_loop()` when `announce_interval_seconds > 0`. Calls
> `router.announce()` with the delivery destination hash. Diagnostic counters:
> `announces_sent`, `announce_failures`, `last_announce_error`. Task cancelled
> on `stop()`. Config field: `announce_interval_seconds` (default 600,
> 0 = disabled).

| Field     | Value                                                      |
| --------- | ---------------------------------------------------------- |
| Adapter   | LXMF                                                       |
| Reference | LXMF v0.9.6 `LXMRouter.announce()`; RNS announce mechanism |
| Gap type  | Behavioral                                                 |

**Observed MEDRE behavior**: The session removed the announce callback handler (R9 from adapter-reality-audit.md) and does not send periodic announces. The `LXMRouter` is created with a `register_delivery_callback` for inbound messages, but no periodic `router.announce()` call is made.

**Reference behavior**: LXMF/Reticulum mesh networks rely on announces for path discovery. `LXMRouter.announce()` sends an announce packet that propagates through the Reticulum network, allowing peers to discover the router's identity and establish paths. Without announces, remote peers cannot initiate contact with the MEDRE LXMF instance — messages can only flow outbound (MEDRE → peer) or from peers that already have a cached path.

**Gap** (historical): In a real Reticulum deployment, if MEDRE's LXMF adapter never announces, remote peers that don't already have a path to MEDRE's identity will be unable to reach it. This means MEDRE's LXMF adapter will work for outbound messages and for inbound messages from already-known paths, but new peers discovering MEDRE will be blocked.

**Operational value**: **Medium**. For static deployments where all peer identities are pre-shared, this is not a problem. For any deployment where new peers may join the mesh, periodic announces are essential for reachability.

**Risk**: **Medium**. `LXMRouter.announce()` is a network-visible operation. The announce interval and display name need to be configurable to avoid flooding the mesh. The implementation needs to respect `_stop_requested` and cancel cleanly. The removed announce callback (R9) should be re-evaluated — the audit removed it because it was unused, but announces serve a different purpose than inbound-event processing.

**Proposed next action**: _Completed._ `LxmfSession._announce_loop()` calls `router.announce()` on a configurable interval (default 600 s, 0 = disabled). Diagnostic counters track sends, failures, and last error. Task is cancelled on `stop()`.

---

### P-07. Matrix: No periodic sync liveness check

**Rank**: 7

> **Partially resolved** (development-1): `MatrixAdapter.health_check()` now
> detects stale sync via `_SYNC_STALE_THRESHOLD_SECONDS` (default 300s). Reports
> `"degraded"` health when last successful sync exceeds threshold. Remaining
> gap: sync token persistence (P-03) is still unresolved — the stale-sync
> watchdog detects the symptom but does not fix the root cause of full initial
> syncs on restart.

| Field     | Value                                                                                |
| --------- | ------------------------------------------------------------------------------------ |
| Adapter   | Matrix                                                                               |
| Reference | mmrelay implicit (sync_forever provides built-in liveness); MEDRE's manual sync loop |
| Gap type  | Behavioral                                                                           |

**Observed MEDRE behavior**: The Matrix session uses a manual sync loop (`_sync_with_reconnect`) instead of nio's `sync_forever`. If a sync call hangs indefinitely (e.g., the homeserver stops responding but doesn't close the TCP connection), the loop will never advance. The `_last_successful_sync` diagnostic is recorded, but nothing acts on a stale value.

**Reference behavior**: MMRelay uses nio's `sync_forever` with `sync_timeout_ms`, which sets a timeout on each long-poll cycle. If the sync times out, nio retries internally. MEDRE's manual loop does set `timeout=self._config.sync_timeout_ms` on each sync call, which should cause nio to return after the timeout — but there's no external watchdog that detects if the loop itself has stalled.

**Gap** (partially resolved): `health_check()` now detects stale sync and reports degraded status when the last successful sync exceeds `_SYNC_STALE_THRESHOLD_SECONDS`. **Remaining**: sync token persistence (P-03) is still unresolved. The stale-sync watchdog detects the symptom but does not fix the root cause of full initial syncs on restart.

**Operational value**: **Medium**. Sync stalls are rare but catastrophic when they occur — the bridge silently stops processing Matrix events. A liveness check that detects stale sync provides a self-healing mechanism.

**Risk**: **Low**. Add a periodic check (e.g., in the adapter's `health_check()` or a separate watchdog task) that compares `time.monotonic() - last_successful_sync` against a threshold (e.g., 2 × `sync_timeout_ms / 1000`). If exceeded, cancel the sync task and trigger a reconnect.

**Proposed next action**: Stale-sync detection is implemented in `health_check()`. The remaining work is sync token persistence (see P-03), which would prevent the full initial sync that triggers the stale condition on restart.

---

### P-08. Meshtastic: Reconnect backoff cap lower than proven value

**Rank**: 8

| Field     | Value                                               |
| --------- | --------------------------------------------------- |
| Adapter   | Meshtastic                                          |
| Reference | mmrelay `meshtastic_utils.py` reconnect (cap 300 s) |
| Gap type  | Behavioral                                          |

**Observed MEDRE behavior**: The Meshtastic session caps reconnect backoff at 30 seconds and gives up after 10 consecutive attempts (~5 minutes of total retry time).

**Reference behavior**: MMRelay caps reconnect backoff at 300 seconds (5 minutes) and continues indefinitely (no max attempts). This reflects the operational reality that Meshtastic radio nodes may be offline for extended periods (reboots, firmware updates, location changes).

**Gap**: MEDRE will permanently give up reconnecting after ~5 minutes of failed attempts. In real deployments, nodes can be offline for 10+ minutes during firmware updates or power cycles. After MEDRE gives up, the bridge requires a full restart to recover.

**Operational value**: **Medium**. The 10-attempt limit was noted in `adapter-reality-audit.md` §7 as "intentional for MEDRE's use case." Reconsidering: a bridge that permanently gives up on a radio connection is worse than one that keeps trying. The node will eventually come back online, and the bridge should be there when it does.

**Risk**: **Low**. Increasing `_BACKOFF_CAP` from 30 to 60–120 seconds and removing or significantly raising `_MAX_RECONNECT_ATTEMPTS` is a configuration change. The exponential backoff with jitter ensures the retry rate is bounded.

**Proposed next action**: Raise `_BACKOFF_CAP` to 120 seconds. Raise `_MAX_RECONNECT_ATTEMPTS` to 50 or remove the limit (rely on `_stop_requested` as the only termination condition). Document the rationale in the session module. This aligns with MMRelay's proven behavior.

---

### P-09. Meshtastic: Queue water-mark monitoring

**Rank**: 9

| Field     | Value                                    |
| --------- | ---------------------------------------- |
| Adapter   | Meshtastic                               |
| Reference | mmrelay `message_queue.py` lines 369–377 |
| Gap type  | Declarative/capability                   |

**Observed MEDRE behavior**: The `MeshtasticOutboundQueue` tracks queue depth, max size, and various counters (sent, failed, rejected, etc.) in diagnostics. However, there are no water-mark thresholds that trigger warnings when the queue is filling up.

**Reference behavior**: MMRelay's `MessageQueue._process_queue()` checks `queue_size > QUEUE_HIGH_WATER_MARK` and `QUEUE_MEDIUM_WATER_MARK`, logging warnings at each level. This provides early warning before the queue rejects messages.

**Gap**: MEDRE's queue will silently fill until it rejects messages with `MeshtasticSendError(transient=True)`. Operators have no early warning that the queue is approaching capacity.

**Operational value**: **Low–Medium**. This is an observability improvement, not a correctness fix. The rejection behavior is correct; the gap is in operator visibility.

**Risk**: **Low**. Add water-mark constants (e.g., 75% and 90% of max size) to the queue. In the drain task, check depth against water-marks and log warnings. No behavioral changes needed.

**Proposed next action**: Add `_QUEUE_HIGH_WATER_MARK_PCT = 0.75` and `_QUEUE_CRITICAL_WATER_MARK_PCT = 0.90` to the queue module. In `_process_queue`, check current depth against these thresholds and log at WARNING level. Expose `queue_water_mark` in diagnostics.

---

### P-10. MeshCore: Reconnect should re-issue appstart and re-subscribe to all events

**Rank**: 10

| Field     | Value                                            |
| --------- | ------------------------------------------------ |
| Adapter   | MeshCore                                         |
| Reference | meshcore_py v2.2.5 `send_appstart()` requirement |
| Gap type  | Behavioral (validation)                          |

**Observed MEDRE behavior**: The session's `_reconnect_loop()` calls `_connect_real()` on each attempt. `_connect_real()` subscribes to events, sends appstart, and starts auto-message-fetching. This path is shared between initial connect and reconnect — already correct.

**Reference behavior**: meshcore_py requires `send_appstart()` after every successful connect or reconnect. The firmware ignores subsequent commands until appstart is received.

**Validation**: MEDRE already handles this correctly. The reconnect path goes through `_connect_real()` which includes `_subscribe_events()`, `send_appstart()`, and `start_auto_message_fetching()`. This was verified and fixed in the adapter-reality-audit (R4).

**Gap**: **None identified**. This item is a validation confirmation, not a gap. Included for completeness because the audit backlog entry for R4 confirms this was previously broken and is now fixed.

**Operational value**: N/A (no gap).

**Risk**: N/A.

**Proposed next action**: No action needed. This entry exists to confirm that the `send_appstart()` parity move from the prior audit wave is complete and correct.

---

### P-11. LXMF: Outbound delivery tracking eviction should log evicted state

**Rank**: 11

| Field     | Value                                     |
| --------- | ----------------------------------------- |
| Adapter   | LXMF                                      |
| Reference | LXMF v0.9.6 `LXMRouter` delivery tracking |
| Gap type  | Declarative/capability                    |

**Observed MEDRE behavior**: When outbound delivery tracking hits the 1000-entry cap, oldest entries are evicted via FIFO. The eviction logs a warning with the count of evicted entries, but does not log the state of the evicted entries.

**Reference behavior**: No direct reference comparison — this is a MEDRE-internal observability gap. The LXMF SDK doesn't impose a tracking limit; MEDRE's limit is a self-imposed bound.

**Gap**: If evicted entries were in non-terminal states (e.g., `SENDING` or `SENT`), the adapter silently loses visibility into those deliveries. The delivery state callback will never fire for them because the tracking entry no longer exists. Operators cannot tell whether evicted entries were still in-flight.

**Operational value**: **Low**. The 1000-entry cap is generous; eviction only happens under extreme outbound volume. When it does happen, the impact is observability loss, not message loss (the messages are still in the LXMRouter's care).

**Risk**: **Low**. Before evicting, log the state of each evicted entry (e.g., `"Evicted OUTBOUND delivery abc123 after 45s in tracking"`). This adds one log line per evicted entry, which is bounded by the eviction batch size.

**Proposed next action**: In `_track_delivery()`, before evicting oldest entries, log each entry's `native_message_id` (first 16 chars), `state`, and time-since-creation. This provides forensic visibility into what was lost.

---

### P-12. Matrix: E2EE key request on undecryptable event should be rate-limited per session

**Rank**: 12

| Field     | Value                                     |
| --------- | ----------------------------------------- |
| Adapter   | Matrix                                    |
| Reference | nio `sync_forever` key management pattern |
| Gap type  | Behavioral                                |

**Observed MEDRE behavior**: When an undecryptable MegolmEvent is received, the session sends a room key request via `event.as_key_request()` followed by `client.to_device(key_request)`. There is a 60-second dedup window per room:session_id pair for logging, but the key request itself is sent every time a non-deduplicated event arrives.

**Reference behavior**: nio's `sync_forever` includes automatic key sharing protocols as part of each sync cycle. The key request mechanism is built into the sync loop's key management steps (`keys_query`, `keys_claim`, `send_to_device_messages`).

**Gap**: MEDRE's key request is sent inline in the Megolm event handler, outside the structured key management cycle. If many undecryptable events arrive in quick succession (e.g., after a long disconnection), MEDRE will send a burst of key requests. The 60-second logging dedup does not prevent repeated key requests for different session IDs within the same room.

**Operational value**: **Low**. Key requests are small to-device messages and are generally harmless. The gap is a minor efficiency issue, not a correctness problem.

**Risk**: **Low**. Move the key request logic to be gated by the same 60-second dedup window that already gates logging. Or, remove the explicit key request and rely on nio's built-in key management cycle (which MEDRE already runs in the sync loop).

**Proposed next action**: Evaluate whether the explicit `as_key_request` + `to_device` call in `_on_megolm_event` is still needed given that the sync loop already runs `keys_claim` and `send_to_device_messages`. If the built-in cycle is sufficient, remove the explicit key request to avoid duplication. If not, add the dedup key to the rate-limit gate.

---

## Summary Table

| Rank | ID   | Adapter    | Gap                                                 | Value    | Risk    | Type        | Status             |
| ---- | ---- | ---------- | --------------------------------------------------- | -------- | ------- | ----------- | ------------------ |
| 1    | P-01 | Meshtastic | No periodic connection health check                 | High     | Low–Med | Behavioral  | Open               |
| 2    | P-02 | Meshtastic | No SDK connection-lost event subscription           | High     | Low     | Behavioral  | Resolved           |
| 3    | P-03 | Matrix     | No sync token persistence across restarts           | High     | Low     | Behavioral  | Open               |
| 4    | P-04 | MeshCore   | Partial `suggested_timeout` extraction              | Med–High | Low     | Behavioral  | Partially resolved |
| 5    | P-05 | MeshCore   | No contact-list event subscriptions                 | Medium   | Low     | Declarative | Resolved (partial) |
| 6    | P-06 | LXMF       | No periodic announce for path discovery             | Medium   | Medium  | Behavioral  | Resolved           |
| 7    | P-07 | Matrix     | No sync liveness watchdog                           | Medium   | Low     | Behavioral  | Partially resolved |
| 8    | P-08 | Meshtastic | Reconnect backoff cap too low, gives up permanently | Medium   | Low     | Behavioral  | Open               |
| 9    | P-09 | Meshtastic | Queue water-mark monitoring                         | Low–Med  | Low     | Declarative | Open               |
| 10   | P-10 | MeshCore   | appstart on reconnect (validation — no gap)         | N/A      | N/A     | Validation  | No gap             |
| 11   | P-11 | LXMF       | Eviction logging lacks delivery state               | Low      | Low     | Declarative | Open               |
| 12   | P-12 | Matrix     | E2EE key request rate limiting                      | Low      | Low     | Behavioral  | Open               |

## Constraints Reiterated

- **No compatibility shims**: Items propose adopting SDK patterns, not wrapping them.
- **No facades**: Items propose using SDK APIs directly, not adding abstraction layers.
- **No public API commitments**: All proposals are internal to the adapter session/adapter layer.
- **No user-facing feature expansion**: Items improve reliability and observability of existing functionality.
- **Backlog status**: This document proposes actions for future work packages; implementation belongs to those packages.

## Relationship to Prior Audit

The adapter-reality-audit (R1–R10) addressed **correctness** gaps — places where MEDRE's assumptions were wrong or missing. This backlog addresses **runtime resilience** gaps — places where MEDRE's assumptions are correct but the operational behavior could be improved by adopting proven patterns from reference implementations.

Items from the prior audit's "Remaining Risks" (§7) that are **not** duplicated here:

- **R11 `delivery_receipts` capability**: Debated capability decision, not a runtime parity issue.
- **R12 `text_message_ack` portnum**: Cosmetic, no functional impact.
- **Meshtastic byte budget (B2)**: Capability/capacity decision, not a runtime parity issue. Requires user decision.
- **MeshCore hardcoded port 4000**: Intentional config behavior.
- **`M_UNKNOWN` HTTP status check**: Cosmetic classification, deferred.
- **LXMF "in production" destination nuance**: Complexity deferred, not a runtime gap.

## Testing Rules Reference

From `docs/dev/testing.md`, the following rules govern how SDK parity tests should be written when these backlog items are implemented:

1. **File size limit**: Target < 1,200 lines per test file. Hard ceiling: 1,500 lines. If a file approaches the cap, split by behavioral domain.
2. **Test style**: Use pytest function style (`async def test_...`), not `unittest.TestCase`.
3. **No fixed sleeps**: Use `wait_until()` from `tests/helpers/async_utils.py` or deterministic hooks (`asyncio.Event`, mock callbacks). Never `asyncio.sleep(0.3)`.
4. **Mock type matching**: If production code `await`s the callable, use `AsyncMock`. If not awaited, use `MagicMock`.
5. **Patch at lookup site**: Patch the canonical module where the object is **looked up**, not where it is defined.
6. **Evidence levels**: Label tests honestly. Tests using mock SDKs are `fake_pipeline` or `fake_adapter_callback` (tier 1–2), not "docker" or "live".
7. **Coroutine leak prevention**: Close passed coroutines in fake scheduler submissions.
8. **No compatibility shims in tests**: Tests and production code paths are identical.
9. **Agent execution discipline**: No timeout wrappers for routine pytest runs. Capture full output. Rerun at most once after a concrete fix.

Existing SDK parity tests (`tests/test_lxmf_session_sdk_parity.py`) demonstrate the pattern: mock the SDK at the module boundary, verify wiring behavior (stamp cost propagation, delivery method selection, pacing timing, callback invocation). New parity tests should follow this pattern.

## See Also

- [Adapter Reality Audit](adapter-reality-audit.md) — correctness gaps addressed in the prior wave
- [Reference Repos](reference-repos.md) — boundary rules for external reference use
- [Testing Guide](testing.md) — test authoring rules and conventions
- [Adapter Authoring Guide](adapter-authoring.md) — how to write and extend adapters
- Transport profiles: [matrix](../spec/transport-profiles/matrix.md), [meshtastic](../spec/transport-profiles/meshtastic.md), [meshcore](../spec/transport-profiles/meshcore.md), [lxmf](../spec/transport-profiles/lxmf.md)
