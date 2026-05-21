# Operator Workflows

> Last updated: 2026-05-21
> Scope: Day-to-day MEDRE operation for a single operator
> Status: **Alpha. Not production. Not hardened. Not complete.** Everything here is subject to change without notice.

This runbook covers the practical side of running MEDRE: installing it, running a quick smoke test, validating against a real Matrix homeserver, collecting evidence when something goes wrong, and reading diagnostic output. It is written for a single operator on a single machine. It does not cover deployment, scaling, monitoring, or multi-node setups, because none of those exist yet.

If something has not been tested and confirmed working, this document says so. If something is known to be broken or missing, this document says that too.

Test developers should read `docs/dev/live-test-harness.md` for the live test patterns and conventions. This document is for operators.

## 1. Prerequisites

| Requirement | Details |
|---|---|
| Python | 3.11 or later |
| pip | Recent enough to handle extras (`pip >= 21.3`) |
| Git | For cloning the repo |
| Matrix homeserver (optional) | Synapse or Conduit, local or reachable. Only needed for live Matrix sessions. |

You do not need Docker for the basic workflow. You do not need any Matrix credentials to run the fake local smoke test.

## 2. Setup

### 2.1 Install

```bash
git clone <repo-url> && cd medre
pip install -e ".[dev]"
```

This installs MEDRE and all dev dependencies. For Matrix live sessions, you also need the Matrix extra:

```bash
pip install -e ".[matrix]"
```

For E2EE text alpha (encrypted rooms, text only):

```bash
pip install -e ".[matrix-e2e]"
```

### 2.2 Verify the install

```bash
medre --help
```

If this prints a help message, the install worked. If it prints `command not found`, check that your virtualenv is active and the install completed without errors.

### 2.3 Environment variables

MEDRE reads configuration from environment variables. The variables you need depend on what you are doing.

| Mode | Required env vars | Notes |
|---|---|---|
| Fake local smoke | None | Runs entirely with fake adapters, no network |
| Matrix live session | `MATRIX_HOMESERVER`, `MATRIX_USER_ID`, `MATRIX_ACCESS_TOKEN`, `MATRIX_ROOM_ALLOWLIST` | See `docs/runbooks/matrix-alpha-operation.md` for full details |
| E2EE text alpha | All Matrix vars plus `MATRIX_ENCRYPTION_MODE` | Set to `e2ee_required` or `e2ee_optional` |
| Meshtastic live | `MESHTASTIC_*` vars | Not covered in this runbook. See `docs/runbooks/meshtastic-alpha-operation.md` |

The transport-prefixed convention is consistent: Matrix vars start with `MATRIX_`, Meshtastic vars start with `MESHTASTIC_`, and so on.

## 3. Fake Local Run Session

The fastest way to confirm MEDRE works on your machine. No network, no credentials, no external services.

### 3.1 Run the smoke test

```bash
PYTHONPATH=src medre smoke
```

This builds a pipeline with fake adapters, runs a message through it, and prints a summary. You should see output like:

```
Smoke test: PASSED
  Evidence level: fake_bridge
  Events processed: 1
  ...
```

For machine-readable output:

```bash
PYTHONPATH=src medre smoke --json
```

The `evidence_level` field will say `fake_bridge`. That is honest. A fake smoke test proves the pipeline wiring works. It does not prove the Matrix adapter talks to a real homeserver.

### 3.2 What the smoke test validates

1. The pipeline builds and starts without errors.
2. A canonical event flows through the codec, renderer, and session stages.
3. The fake adapter receives and acknowledges the event.
4. Storage round-trips correctly.
5. Diagnostics are collected and reported.

### 3.3 What the smoke test does NOT validate

1. Any real network communication.
2. Matrix SDK behavior against a real homeserver.
3. E2EE crypto operations.
4. Meshtastic radio or serial communication.
5. Anything beyond a single event on a single fake adapter.

## 4. Matrix Live Run Session

If you have `MATRIX_*` environment variables set, you can validate MEDRE against a real homeserver. This section assumes you have already set up a homeserver and bot account. If you have not, the full setup instructions are in `docs/runbooks/matrix-alpha-operation.md`.

### 4.1 Set environment variables

```bash
export MATRIX_HOMESERVER="http://localhost:8008"
export MATRIX_USER_ID="@bot:localhost"
export MATRIX_ACCESS_TOKEN="syt_xxxxxxxxxxxxx"
export MATRIX_ROOM_ALLOWLIST="!abc123:localhost"
```

Do not commit these. Do not paste them into chat. Do not log them. They are credentials.

### 4.2 Start the runner

```bash
PYTHONPATH=src medre run
```

You should see startup log lines confirming config loaded, pipeline started, and adapter connected. The key line is:

```
Matrix Operation Alpha running — awaiting shutdown signal
```

If you see that, the runner validated all env vars, initialized storage, started the pipeline, connected to the homeserver, and began the sync loop. Press Ctrl+C to stop.

### 4.3 Quick validation checklist

1. Send a message in the allowlisted room from a second Matrix account (not the bot).
2. Confirm the adapter receives it (check logs for any errors).
3. Send a message through the adapter using `deliver()`.
4. Confirm it appears in the room via Element or another client.
5. Stop the runner with Ctrl+C. Confirm clean shutdown.

If all five steps pass, the Matrix live path is working. If any step fails, see section 8 for diagnosis and `docs/runbooks/matrix-alpha-operation.md` for troubleshooting.

## 5. Collect Evidence

When something goes wrong, the `evidence` command collects a bundle of diagnostic data. The bundle is safe to paste into a GitHub issue. It does not contain secrets.

### 5.1 Offline mode (from storage)

If you have a storage path (SQLite database file) from a previous run:

```bash
PYTHONPATH=src medre evidence --storage-path /path/to/medre.db
```

This opens the database in read-only mode, collects config, diagnostics, route information, and event data, and prints a summary.

For JSON output:

```bash
PYTHONPATH=src medre evidence --storage-path /path/to/medre.db --json
```

### 5.2 Live mode (from config)

If you have a config file that points to a running setup:

```bash
PYTHONPATH=src medre evidence --config /path/to/medre.yaml
```

This starts a runtime, collects a full evidence bundle including live health checks, and then shuts down. The `--include-refresh-health` flag forces a fresh health check (incompatible with `--storage-path`).

### 5.3 What is in the bundle

| Section | Contents |
|---|---|
| Config | Runtime configuration (secrets redacted) |
| Diagnostics | Adapter health, counters, connection state |
| Routes | Configured routes and their status |
| Events | Event data if `--event-id` is specified |
| Replay | Replay run data if `--replay-run-id` is specified |

Use `--event-id` to scope the bundle to a specific event (includes native refs, receipts, and incident summary):

```bash
PYTHONPATH=src medre evidence --storage-path /path/to/medre.db --event-id <event_id>
```

Use `--replay-run-id` to scope the bundle to a replay run (includes replay receipt analysis):

```bash
PYTHONPATH=src medre evidence --storage-path /path/to/medre.db --replay-run-id <replay_run_id>
```

The `config_source` field tells you whether the bundle came from a config file or a storage path. The `collected_at` timestamp tells you when.

### 5.4 Safety

The evidence bundle is designed to be safe to share:

- Access tokens and credentials are redacted before inclusion.
- The bundle does not contain message content. It contains metadata: event IDs, timestamps, adapter IDs, delivery status.
- No network addresses beyond what is in the config (homeserver URL, which is not secret).

If you are unsure, review the JSON output before pasting it into an issue. Look for any field containing your actual access token. If you find one, that is a bug. Report it.

## 6. Trace an Event

The `trace` command assembles a chronological timeline for a single event. It shows what happened to the event, when, and through which adapters.

### 6.1 Basic trace

```bash
PYTHONPATH=src medre trace event <event_id> --storage-path /path/to/medre.db
```

This prints a human-readable timeline. Each entry shows a timestamp, entry type, and relevant data.

For JSON output:

```bash
PYTHONPATH=src medre trace event <event_id> --storage-path /path/to/medre.db --json
```

### 6.2 Timeline entry types

| Entry type | What it shows |
|---|---|
| `event` | The canonical event itself (kind, source adapter, timestamp) |
| `relation` | Relations to other events (replies, reactions) |
| `native_ref` | Native transport references (Matrix event IDs, Meshtastic message IDs) |
| `receipt` | Delivery receipts (status, target adapter, attempt count) |

### 6.3 Interpreting the timeline

A healthy event trace looks something like this:

```
Event: evt_abc123 (message.created) from matrix-alpha
Timeline (4 entries):

  2026-05-21T10:00:00Z  [event] message.created from matrix-alpha
  2026-05-21T10:00:00Z  [native_ref] inbound via matrix-alpha: $mx_event_id
  2026-05-21T10:00:01Z  [receipt] delivered to meshtastic-alpha
  2026-05-21T10:00:01Z  [native_ref] outbound via meshtastic-alpha: msg_456
```

An event that failed delivery will show a receipt with a failure status and an error message. See section 8 for reading failure details.

## 7. Inspect Storage

The `inspect` command queries stored data directly from a SQLite database. It is read-only and never modifies the database.

### 7.1 Inspect a single event

```bash
PYTHONPATH=src medre inspect event <event_id> --storage-path /path/to/medre.db
```

This prints the canonical event as JSON. The output includes all fields: event ID, kind, source adapter, payload, timestamps, and metadata.

### 7.2 Inspect with timeline

```bash
PYTHONPATH=src medre inspect event <event_id> --storage-path /path/to/medre.db --timeline
```

Adds a `timeline` section to the output with all chronological entries.

### 7.3 Inspect with evidence

```bash
PYTHONPATH=src medre inspect event <event_id> --storage-path /path/to/medre.db --evidence
```

Adds an `evidence` section with a full evidence bundle scoped to that event.

### 7.4 Inspect with both

```bash
PYTHONPATH=src medre inspect event <event_id> --storage-path /path/to/medre.db --timeline --evidence
```

This is the most detailed inspection available. It shows the event, its full timeline, and the evidence bundle. The output is deterministic JSON, suitable for diffing or pasting into reports.

### 7.5 What inspect does NOT do

- It does not start a runtime.
- It does not connect to any network service.
- It does not modify the database.
- It does not require any environment variables beyond the storage path.

## 8. Interpreting Delivery Failures

When a message fails to reach its destination, the evidence is in the receipts and timeline entries. Here is how to read them.

### 8.1 Receipt status values

| Status | Meaning |
|---|---|
| `delivered` | The target adapter confirmed successful delivery |
| `failed` | Delivery attempted and failed. Check the error message. |
| `pending` | Delivery has not been attempted yet |
| `skipped` | Delivery was not attempted (e.g., route was degraded or disabled) |

### 8.2 Reading a failure timeline

Trace the event (section 6) or inspect it with `--timeline --evidence` (section 7). Look for:

1. **The event entry.** Confirms the event exists and shows its kind and source.
2. **The receipt entries.** Show delivery attempts, statuses, and error messages.
3. **The evidence section.** Shows adapter health at the time of collection, which may reveal why delivery failed (adapter in `degraded` or `failed` state, for example).

### 8.3 Common failure patterns

| Pattern | Likely cause | What to check |
|---|---|---|
| Receipt says `failed` with `AdapterPermanentError` | Permanent delivery failure (bad room, forbidden, etc.) | Verify the target room/channel exists and the bot has access |
| Receipt says `failed` with `AdapterSendError` (transient) | Temporary network or homeserver error | Check network connectivity, homeserver health |
| No receipt at all | Event never reached delivery stage | Check routing config, adapter health, pipeline logs |
| Receipt says `skipped` | Route was degraded or disabled | Check route status in evidence bundle |
| Multiple receipts with alternating `failed` and `delivered` | Intermittent failures during retry | Check network stability, homeserver load |

### 8.4 Incident summary

When filing an issue or asking for help, include:

1. The event ID.
2. The `medre trace event` output (or `medre inspect event --timeline --evidence` output).
3. The approximate time the failure occurred.
4. What you expected to happen vs. what actually happened.

Do not include your access token, password, or any credential. The evidence bundle redacts them, but double-check before pasting.

## 9. Security

### 9.1 Token handling

- **Never print your access token.** The adapter's `__repr__` method redacts it. Your code might not.
- **Never commit credentials.** Not in `.env` files, not in scripts, not in config files checked into git.
- **Never paste credentials into chat or issues.** If you accidentally do, rotate the token immediately.
- **Unset env vars when done.** `unset MATRIX_ACCESS_TOKEN` after testing.

### 9.2 Safe-to-paste reports

The `evidence`, `trace`, and `inspect` commands produce output designed to be shareable. They redact secrets and include only metadata. But:

- Always review JSON output before sharing it. Look for anything that looks like a token or password.
- If you find a secret in the output, that is a bug. File an issue.
- The `collected_at` and `config_source` fields are metadata about the report itself, not secrets.

### 9.3 What is logged

The runner logs to stderr at INFO level. Logs include adapter IDs, room IDs, event IDs, and health status. They do not include access tokens (the adapter redacts them in `__repr__`). They may include error messages from the Matrix SDK, which could contain homeserver URLs but not credentials.

If you are sharing log output, review it first. Redact anything you are unsure about.

## 10. Alpha Status

This entire system is alpha software. Specific things that are not true:

1. **It is not production-ready.** Do not rely on it for anything important.
2. **It is not reliable.** Messages can be lost, duplicated, or silently dropped. There is no delivery guarantee.
3. **It is not hardened.** Error handling exists but is not comprehensive. Unexpected inputs may produce confusing errors or silent failures.
4. **It is not complete.** Many Matrix features are unsupported: reactions, edits, deletes, media, threads, presence, typing notifications, read receipts. See `docs/runbooks/matrix-alpha-operation.md` section 13 for the full list.
5. **It is not fast.** Performance has not been optimized. The sync loop is a single long-polling HTTP connection. Delivery is sequential within a single adapter.
6. **It is not documented completely.** This runbook covers the main workflows. Edge cases, error recovery, and advanced configuration are documented elsewhere or not at all.

If you find a bug, file an issue with the evidence bundle (section 5) and the event trace (section 6). Include what you expected and what actually happened.

## 11. Related Documentation

| Document | What it covers |
|---|---|
| `docs/runbooks/matrix-alpha-operation.md` | Full Matrix alpha operation guide (setup, validation, troubleshooting, E2EE) |
| `docs/runbooks/matrix-live-smoke.md` | Matrix live smoke test instructions |
| `docs/dev/live-test-harness.md` | Live test patterns and conventions for test developers |
| `docs/dev/TESTING_GUIDE.md` | General testing guide (tiers, style, patterns) |
