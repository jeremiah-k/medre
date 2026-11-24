# Native recovery campaigns

Use this campaign to qualify the actual MEDRE runtime across process death and
destination outages. It reuses the existing Matrix and XMPP native peers, with
their independent accounts and conversations, and retains one SQLite database
across all scenarios. Separate runtime generations preserve the bridge's durable
ingress checkpoints rather than re-ingesting room history into an empty store.

The runtime runs in an owned subprocess. Crash scenarios kill that subprocess
at a harness-controlled delivery barrier; restarted processes use the same
database and ordinary recovery workers. The harness reads storage through its
read-only interface. It does not shorten leases, edit checkpoints, create
successful receipts, or change outbox rows to accelerate recovery.

## Docker setup

Prepare and start the [Docker ecosystem lab](docker-ecosystem.md), then run:

```console
lab_dir=/private/bench/campaign-001
python -m scripts.adapter_campaign.lab test --directory "$lab_dir" --stage oauth
python -m scripts.adapter_campaign.lab test --directory "$lab_dir" --stage provision
python -m scripts.adapter_campaign.lab test --directory "$lab_dir" --stage resilience
python -m scripts.adapter_campaign.lab test --directory "$lab_dir" --stage logs
```

The grouped `native` and `all` runs include `resilience`. The existing `recovery`
stage tests authentication logout/re-login; it remains separate from process and
transport recovery. Always finish with `lab down`, including after failures.

This tier uses no radios. Its TCP gate listens only on container loopback and
interrupts the selected XMPP adapter's connection. Native peers connect directly
to the server and remain independent witnesses. TLS bytes pass through unchanged;
certificate verification and account authentication remain active. Host network
configuration and Bluetooth services are outside the fault controls.

## Scenario contracts

| Scenario           | Fault and required evidence                                                                                                                                                                                                              |
| ------------------ | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Before handoff     | Kill after the outbox claim but before invoking the native adapter. No native receipt precedes the crash. A fresh runtime reclaims the same work after normal lease expiry and delivers through attempt 2.                               |
| After handoff      | Observe the native message, then kill before the adapter returns its handoff result to core. Recovery retains the same event/outbox identity. One or two native deliveries are accepted and counted explicitly; uncertainty is retained. |
| Completed delivery | Observe both native receipt and terminal durable evidence, kill, and restart. The event, receipt history, and terminal work remain unchanged; the observer sees one delivery.                                                            |
| Transient outage   | Interrupt only the destination connection, establish degraded health, and send from Matrix. Restore connectivity after a failed attempt. The same durable work reaches sent through a bounded retry.                                     |
| Exhausted retries  | Hold the destination down through all three configured attempts. Require dead-letter evidence without a fourth attempt. Restore connectivity and deliver a fresh canary; exhausted work remains terminal and undelivered.                |

The campaign enables the ordinary retry worker and configures three attempts,
ten-second base backoff, twenty-second maximum backoff, and no jitter. This
allows a bounded reconnection window before retries exhaust; the exhaustion
case holds the gate closed through all three attempts. In-flight leases expire
normally, so each crash scenario can take about a minute.
Each scenario has a 150-second deadline; a timeout fails qualification. Late
duplicates are checked during a bounded observation window and again against
the complete peer streams after all scenarios and peer shutdown.

Successful local handoff is distinct from native receipt. An XMPP receipt can
honestly report local transport handoff while an independent peer supplies the
evidence that the message reached the conversation. The ambiguous crash case
does not establish exactly-once transport delivery.

## Use an existing private fixture

The standalone runner accepts the existing
[campaign configuration](adapter-campaign.md). It selects its Matrix and XMPP
entries, preserving fixture-authored sender, channel, and presentation
expectations. Use dedicated disposable accounts and conversations.
The XMPP runtime entry supplies an explicit host and port for the TCP gate;
the adapter still authenticates the configured XMPP server identity through TLS.

Preview without starting a runtime, connecting, or sending:

```console
python -m scripts.adapter_campaign.recovery --config /private/bench/campaign.yaml
```

Permit native sends and intentional crashes of the owned runtime:

```console
python -m scripts.adapter_campaign.recovery \
  --config /private/bench/campaign.yaml \
  --output /private/bench/recovery-001 --run
```

The output directory is fresh and private. Process-death qualification uses
POSIX process groups; the offline controls and evidence tests also run in
ordinary cross-platform CI. To use pytest, set MEDRE_RECOVERY_CAMPAIGN and
optionally MEDRE_RECOVERY_OUTPUT, then explicitly select
`tests/test_native_recovery_campaign_live.py -m live`.

## Retained evidence

Each scenario records its runtime generations, intentional crash timing,
native source ID, pre-fault storage snapshot, and final storage snapshot. The
campaign retains independent peer JSONL streams, the source fixture's expected
identities and prefixes, and cleanup outcomes. Raw logs and SQLite data remain
private and can contain messages, conversation identifiers, and credentials.

Analyze a completed run again without contacting the services:

```console
python -m scripts.analyze_recovery_campaign /private/bench/recovery-001
```

The analyzer reads the retained database and independent peer streams. A
passing label, a copied sent receipt, or two consistently incorrect destination
fields do not substitute for the configured conversation and committed outbox
receipt pointer. Missing coverage, an empty nonce, rewritten history, excess
attempts, and failed cleanup invalidate the result.

The lab's `logs` stage requires a control stream and both stdout and stderr
captures for every runtime generation in all five scenarios. Missing evidence
fails qualification. Managed stdout records require timestamps; stderr may be
empty or contain SDK diagnostics, but resource warnings, unhandled task/future
failures, and unawaited coroutines in either stream fail the review. Summaries
retain counts and line numbers rather than raw log contents.

This qualifies Matrix-source/XMPP-destination text recovery. Deferred radio
queues, other native transports, attachments, relations, encrypted rooms, and
extended outages require their own explicit scenarios and witnesses.
