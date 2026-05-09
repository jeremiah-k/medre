# Matrix Alpha Operation Runbook

> Last updated: 2026-05-09
> Scope: Real Matrix Operation Alpha (Track 7)
> Status: Alpha. Not production. Not hardened. Not complete.

This runbook describes how to run MEDRE against a real Matrix homeserver in alpha mode. Alpha mode means the MatrixAdapter connects to a real homeserver using real credentials, syncs real rooms, sends real messages, and receives real events. It does not mean the system is ready for anything beyond a single operator on a local or test homeserver.

Everything in this document is conservative. If something has not been tested against a real homeserver and confirmed working, this document says so. If something is known to be broken or missing, this document says that too.


## 1. Purpose

Alpha operation validates that the MEDRE Matrix adapter works end to end against a real Matrix homeserver with real network calls. This is the first time the adapter leaves mock and fake territory.

Scope boundaries:

- One transport: Matrix. No other transports are in scope for this runbook.
- One operator: a single person running against a local or test homeserver.
- Plain text messages and replies only. Nothing else.
- No production deployment, no scaling, no monitoring, no alerting.
- No claims about reliability, durability, or correctness beyond what manual testing confirms.

This runbook complements `docs/runbooks/matrix-live-smoke.md`. The smoke test validates adapter methods in isolation. Alpha operation validates the full wiring: config, adapter, codec, inbound, outbound, and health, running together.


## 2. Prerequisites

| Requirement | Details |
|------------|---------|
| Matrix homeserver | Synapse or Conduit, local or reachable over the network |
| Bot account | A dedicated Matrix user, not your personal account |
| Python | 3.11 or later |
| Package install | `pip install -e ".[matrix]"` (installs `mindroom-nio`) |
| Access token | Obtained via login API or Element UI |
| A test room | Unencrypted, bot has joined it |
| Network access | Your machine can reach the homeserver's HTTP(S) port |

You do not need Docker. You do not need a domain name. You do not need federation. A local homeserver on localhost is sufficient.


## 3. Homeserver Setup

You need a Matrix homeserver that the MEDRE process can reach. For alpha, local is fine.

### 3.1 Synapse via pip (recommended)

```bash
pip install matrix-synapse

python -m synapse.app.homeserver \
  --server-name localhost \
  --config-path homeserver.yaml \
  --generate-config \
  --report-stats=no

python -m synapse.app.homeserver --config-path homeserver.yaml
```

Synapse starts on port 8008 by default.

### 3.2 Conduit (lightweight alternative)

Download a binary from conduit.rs or build from source. Conduit starts on port 6167 by default. Registration happens through any Matrix client pointed at it.

### 3.3 Docker (optional)

```bash
docker run -d --name synapse -p 8008:8008 \
  -e SYNAPSE_SERVER_NAME=localhost \
  -e SYNAPSE_REPORT_STATS=no \
  matrixdotorg/synapse:latest
```

Docker is not required. It is listed here because some people prefer it. The primary path is pip (Synapse) or native binary (Conduit).


## 4. Token Generation

The adapter authenticates with a long-lived access token. There are two ways to get one.

### 4.1 Login API (curl)

```bash
# Register a bot user first (Synapse only)
register_new_matrix_user \
  -c homeserver.yaml \
  -u bot -p secret \
  http://localhost:8008

# Get a token
curl -s -X POST \
  -d '{"type":"m.login.password","user":"bot","password":"secret"}' \
  http://localhost:8008/_matrix/client/v3/login
```

The response JSON includes an `access_token` field. Copy that value.

### 4.2 Element UI

Open Element, log in as the bot user, go to Settings, Help and About, and copy the access token.

### 4.3 Token handling

Do not commit the token. Do not log the token. Do not paste it into chat. Set it as an environment variable and leave it there. The `MatrixConfig.__repr__` method redacts the token in log output, but you are responsible for not leaking it yourself.


## 5. Room Setup

1. Open a Matrix client (Element, or any other).
2. Create a new room. Give it any name.
3. Invite the bot user to the room.
4. Accept the invite from the bot account (log in as the bot in a second client session or via the join API).
5. Copy the room ID. It looks like `!opaquestring:localhost`. Room aliases (the `#name:server` form) will not work in the allowlist.
6. Confirm the room is unencrypted. E2EE is not supported. If the room has a lock icon in Element, it is encrypted and the adapter will not be able to read message content.


## 6. Allowlist Configuration

The adapter accepts an optional `room_allowlist`: a set of room IDs. When set, the inbound callback ignores messages from any room not in the set. When unset (None), the adapter accepts messages from all rooms.

In alpha mode, you should always set the allowlist to exactly the room(s) you intend to monitor. Running without an allowlist means the adapter will process every message from every room the bot has joined, which is almost certainly not what you want during testing.

The allowlist is configured through `MatrixConfig.room_allowlist`. It is a set of room ID strings:

```python
room_allowlist={"!abc123:localhost", "!def456:localhost"}
```

Or None to accept all rooms:

```python
room_allowlist=None
```

The environment variable convention for this is `MATRIX_ROOM_ALLOWLIST`, a comma-separated list of room IDs. If a runner module exists, it parses this into the set. If you are wiring the adapter manually, parse it yourself:

```python
import os

raw = os.environ.get("MATRIX_ROOM_ALLOWLIST", "")
allowlist = set(raw.split(",")) if raw.strip() else None
```


## 7. Running MEDRE in Alpha Mode

### 7.1 Environment variables

| Variable | Required | Example | Notes |
|----------|----------|---------|-------|
| `MATRIX_HOMESERVER` | Yes | `http://localhost:8008` | Full URL, no trailing slash |
| `MATRIX_USER_ID` | Yes | `@bot:localhost` | Must start with `@` |
| `MATRIX_ACCESS_TOKEN` | Yes | `syt_xxxxxxxxxxxxx` | Keep it secret |
| `MATRIX_ROOM_ALLOWLIST` | No | `!abc:localhost,!def:localhost` | Comma-separated room IDs. If unset, all rooms are accepted. |

### 7.2 Wiring the adapter

No runner module exists yet (as of this writing). The wiring pattern is straightforward:

```python
import asyncio
import os

from medre.adapters.matrix import MatrixAdapter, MatrixConfig

async def main():
    raw_allowlist = os.environ.get("MATRIX_ROOM_ALLOWLIST", "")
    allowlist = set(raw_allowlist.split(",")) if raw_allowlist.strip() else None

    config = MatrixConfig(
        adapter_id="matrix-alpha",
        homeserver=os.environ["MATRIX_HOMESERVER"],
        user_id=os.environ["MATRIX_USER_ID"],
        access_token=os.environ["MATRIX_ACCESS_TOKEN"],
        room_allowlist=allowlist,
    )

    adapter = MatrixAdapter(config)

    # Create a minimal context. The real runtime provides this.
    # For alpha testing, a stub context with a logger and publish_inbound
    # callback is sufficient.
    from medre.adapters.base import AdapterContext

    ctx = AdapterContext(
        logger=...,           # a logging.Logger or compatible object
        publish_inbound=...,  # an async callable accepting canonical events
        storage=...,          # a storage backend or None
    )

    await adapter.start(ctx)
    print("Adapter started. Press Ctrl+C to stop.")

    try:
        await asyncio.Event().wait()  # block forever
    except KeyboardInterrupt:
        pass
    finally:
        await adapter.stop()

asyncio.run(main())
```

If a runner module (`src/medre/runner.py`) has been created by the time you read this, it will handle the wiring above. Consult that module for the actual invocation command.

### 7.3 Expected startup output

On a successful start, you should see:

```
MatrixAdapter matrix-alpha started
```

That is the `ctx.logger.info` call at the end of `start()`. If you see that line, the adapter has:

1. Verified `mindroom-nio` is installed.
2. Created an `AsyncClient`.
3. Restored login with the access token.
4. Registered the inbound message callback.
5. Started the `sync_forever` background task.

If you do not see that line, check the failure modes in section 12.


## 8. Expected Logs and Diagnostics

### 8.1 Healthy startup

The adapter logs one line on start:

```
MatrixAdapter matrix-alpha started
```

After that, the sync loop runs silently. There is no periodic "still alive" log. Silence is normal. The sync loop is long-polling the homeserver, waiting for new events.

### 8.2 Inbound message received

When someone sends a message in an allowlisted room, the `_on_room_message` callback fires. On success, you will see nothing in the logs (the event is published to the context silently). On failure, you will see an exception traceback:

```
MatrixAdapter matrix-alpha: error processing inbound event
Traceback (most recent call last):
  ...
```

### 8.3 Self-message suppression

When the bot itself sends a message (including echoes of its own outbound messages via the sync loop), the adapter suppresses them. You will see:

```
MatrixAdapter matrix-alpha: suppressing self-message from @bot:localhost
```

This is logged at DEBUG level. If your logger is configured for INFO or higher, you will not see it.

### 8.4 Sync failure

If the sync loop crashes (network error, auth expiry, homeserver restart), the exception is captured internally. The next call to `health_check()` will return `health="failed"`. The error is logged:

```
MatrixAdapter matrix-alpha: sync task failed: <exception details>
```

### 8.5 Health states

| State | Meaning |
|-------|---------|
| `unknown` | Adapter has not started, or has been stopped |
| `healthy` | Client is connected and logged in |
| `failed` | Sync task has crashed, or client exists but is not logged in |

The adapter does not auto-reconnect. If the sync task fails, the adapter stays in `failed` until someone calls `stop()` and `start()` again.


## 9. Replay Validation Procedure

This section describes how to manually verify that the adapter behaves correctly in both directions.

### 9.1 Outbound validation

1. Start the adapter.
2. Trigger a `deliver()` call with a rendered text message targeting your test room.
3. Open the room in Element (or another client). Confirm the message appears.
4. Check the return value from `deliver()`. It should contain an `event_id` starting with `$` and the correct `room_id`.

### 9.2 Inbound validation

1. Start the adapter with the allowlist set to your test room.
2. From a second Matrix account (not the bot), send a plain text message in the test room.
3. Confirm that `publish_inbound()` is called with a canonical event.
4. Confirm the event's `source_transport_id` matches the sender's MXID, not the bot's.
5. Confirm the event's `channel_id` matches the room ID.

### 9.3 Self-message suppression validation

1. Start the adapter.
2. Send a message through the adapter using `deliver()`.
3. Wait for the sync loop to echo the message back.
4. Confirm that `publish_inbound()` is not called for the echoed message.

### 9.4 Allowlist validation

1. Start the adapter with `room_allowlist` set to room A.
2. Have someone send a message in room A. Confirm it is received.
3. Have someone send a message in room B (which the bot has also joined). Confirm it is not received.

### 9.5 Stop validation

1. Start the adapter. Confirm `health_check()` returns `"healthy"`.
2. Call `stop()`. Confirm `health_check()` returns `"unknown"`.
3. Confirm no lingering asyncio tasks. Use `asyncio.all_tasks()` before and after to check.


## 10. Known Limitations

This is an honest list. Everything here is real.

1. **No auto-reconnect.** If the sync task fails, the adapter stays dead until manually restarted. There is no exponential backoff, no retry loop, no watchdog. You have to call `stop()` then `start()` again yourself.

2. **No graceful shutdown signaling.** The adapter does not drain in-flight messages on stop. `stop()` cancels the sync task and closes the client. Anything in flight is lost.

3. **No inbound queue or persistence.** Inbound events are published directly via `context.publish_inbound()`. If that callback is slow or fails, the event is gone. There is no retry, no dead letter queue, no redelivery.

4. **No rate limiting.** The adapter sends as fast as you call `deliver()`. Matrix homeservers rate-limit by default. If you send too fast, the homeserver will reject messages and you will get `MatrixSendError`.

5. **No connection health monitoring.** The sync loop either runs or it does not. There is no heartbeat, no periodic ping, no health metric beyond the basic `health_check()` states.

6. **Single-room testing only.** Alpha mode has only been validated with one room at a time. Multi-room behavior (multiple allowlisted rooms with concurrent inbound) has not been tested against a real homeserver.

7. **No reconnection on token expiry.** If the access token is revoked or expires, the sync task fails and the adapter enters `failed` state. You need a new token and a manual restart.

8. **No structured logging.** The adapter uses `ctx.logger.info/debug/error` with format strings. There are no structured log fields, no trace IDs, no correlation across events.

9. **No metrics.** There is no Prometheus endpoint, no counters, no histograms. The only observability is the log output and the `health_check()` return value.

10. **No runner.** As of this writing, there is no `src/medre/runner.py` or CLI entry point. You have to wire the adapter yourself (see section 7.2). If a runner exists when you read this, it was created by a parallel work stream and this section may be outdated. Check the source.


## 11. Operational Risks

These are things that can go wrong. Read them before running the adapter.

### 11.1 Token leakage

The access token is a long-lived credential. Anyone who has it can impersonate the bot. Risks:

- Setting the token in a shell command that gets logged (e.g., `export MATRIX_ACCESS_TOKEN=syt_...` in a shared terminal).
- Committing `.env` files or scripts containing the token.
- Logging the token accidentally. The adapter redacts it in `__repr__`, but your own code might not.

Mitigation: use a dedicated bot account with minimal room membership. Rotate the token if you suspect it has been exposed. Unset the environment variable when you are done testing.

### 11.2 Sync interruption

The `sync_forever` loop can fail for many reasons: network blips, homeserver restarts, token expiry, resource exhaustion. When it fails, the adapter captures the exception but does not recover automatically. You have to notice the failure (via `health_check()` or the error log) and restart manually.

### 11.3 Missing reconnect logic

There is no reconnect logic. This is stated plainly because it is the most common question. If the connection drops, the adapter does not try again. This is a known limitation, not a bug. Reconnection with backoff and state recovery is future work.

### 11.4 Homeserver resource consumption

The long-polling sync loop holds an HTTP connection open to the homeserver. On Synapse, this is a `/_matrix/client/v3/sync` request with a 30-second timeout. On Conduit, behavior is similar. This is normal Matrix client behavior, but if you run many adapter instances against a small homeserver, you may see performance degradation.

### 11.5 Message ordering

The adapter does not guarantee ordering of outbound messages. If you call `deliver()` twice in rapid succession, the homeserver may process them in either order depending on its internal queueing. The adapter does not sequence or gate outbound sends.

### 11.6 Event duplication

If the adapter restarts after sending a message but before recording the event, the same message may be sent again on restart. The adapter has no deduplication logic for outbound messages. Inbound events may also be delivered twice if the sync token is not persisted between restarts.


## 12. Explicit Unsupported Features

The following features are not supported in alpha mode. Do not attempt to use them. They are listed here so you do not have to wonder.

| Feature | Status | Notes |
|---------|--------|-------|
| End-to-end encryption (E2EE) | Not supported | The adapter does not handle encrypted events. Messages in encrypted rooms will be ignored or produce errors. |
| Reactions | Not supported | The adapter registers callbacks for `RoomMessageText`, `RoomMessageNotice`, and `RoomMessageEmote` only. Reaction events are not processed. |
| Edits | Not supported | Edited messages appear as new messages. The adapter does not track `m.replace` relations. |
| Deletes / redactions | Not supported | Redacted messages, if received, are not handled. |
| Media / attachments / files | Not supported | The adapter handles text content only. No image, file, or audio events. |
| Threads | Not supported | Threaded messages are received but thread context is not preserved in canonical events. |
| Spaces | Not supported | Spaces are not relevant to the adapter's operation. |
| Webhooks | Not supported | Matrix does not use webhooks. MEDRE does not implement any webhook receiver for Matrix. |
| Admin APIs | Not supported | The adapter uses the client-server API only. No admin endpoints are called. |
| Non-Matrix transports | Not in scope | This runbook covers Matrix only. |
| Presence | Not supported | The adapter does not send or receive presence events. |
| Typing notifications | Not supported | The adapter does not send or receive typing notifications. |
| Read receipts | Not supported | The adapter does not send or track read receipts. |
| Room creation / management | Not supported | The adapter does not create rooms, set topics, or manage membership. |
| User profile operations | Not supported | The adapter does not set avatars, display names, or profile data. |


## 13. Troubleshooting

### 13.1 `MatrixConnectionError: mindroom-nio not installed`

You forgot to install the Matrix dependency.

```bash
pip install -e ".[matrix]"
```

### 13.2 `MatrixConnectionError: failed to authenticate as @bot:localhost`

The access token is wrong, expired, or the user ID does not match.

1. Verify the user ID format: it must be `@localpart:server` with the leading `@`.
2. Regenerate the token using the login API (section 4.1).
3. Confirm the homeserver URL is correct and reachable:
   ```bash
   curl -s http://localhost:8008/_matrix/client/versions
   ```
   This should return a JSON object with a `versions` array.

### 13.3 `MatrixConfigError: homeserver must start with 'http://' or 'https://'`

The `MATRIX_HOMESERVER` environment variable is missing the scheme. It must be a full URL.

Wrong: `localhost:8008`
Right: `http://localhost:8008`

### 13.4 `MatrixConfigError: user_id must start with '@'`

The `MATRIX_USER_ID` value is missing the leading `@`. Matrix user IDs always start with `@`.

Wrong: `bot:localhost`
Right: `@bot:localhost`

### 13.5 `MatrixSendError: no room_id in result`

The `deliver()` call did not include a `target_channel` and the payload did not contain a `room_id` key. The adapter needs to know which room to send to.

### 13.6 `MatrixSendError: homeserver returned empty/missing event_id`

The homeserver accepted the request but did not return an event ID. This is unusual. Check the homeserver logs. It could indicate a Synapse bug, a database issue, or a malformed request.

### 13.7 Adapter starts but no inbound messages are received

Check these things, in order:

1. Is the room ID in the allowlist? If `room_allowlist` is set and the room is not in it, messages are silently dropped.
2. Is someone other than the bot sending messages? The adapter suppresses self-messages.
3. Is the room encrypted? Encrypted rooms produce events the adapter cannot decode.
4. Is the bot actually joined to the room? Check via Element or the Matrix membership API.
5. Is the sync task still running? Call `health_check()`. If it returns `"failed"`, the sync loop crashed.

### 13.8 Adapter crashes on startup with `ImportError: nio`

The `compat.py` guard should catch this and raise `MatrixConnectionError` instead. If you see a raw `ImportError`, something is wrong with the guard. Check that `mindroom-nio` is installed and importable:

```python
python -c "import nio; print(nio.__version__)"
```

### 13.9 High CPU usage or spinning

The `sync_forever` loop should be idle most of the time (long-polling with a 30-second timeout). If you see high CPU, it might be rapidly reconnecting. Check `health_check()` and the logs for repeated sync failures.

### 13.10 Messages appear in Element but `deliver()` raises `MatrixSendError`

The homeserver rejected the message content. Check that the payload is a valid `m.room.message` content dict with a `msgtype` and `body`. The `MatrixRenderer` should produce this format, but if you are constructing payloads manually, double-check the structure.

### 13.11 `asyncio.CancelledError` warnings on shutdown

These are expected. The `stop()` method cancels the sync task, which raises `CancelledError` inside `sync_forever`. The adapter catches and suppresses it, but Python's runtime may still emit warnings. They are harmless.
