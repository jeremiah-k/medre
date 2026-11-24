# Delivery Semantics

What a `sent` receipt means depends on the transport that accepted the handoff.
This page is the operator reference for per-transport delivery guarantees and
receipt interpretation. Normative per-transport behavior lives in
[the transport profiles](../spec/transport-profiles/README.md); the
[transport evidence matrix](diagnostics-and-evidence.md#transport-evidence-matrix)
records what has been validated for the current tree.

## Per-Transport Delivery Semantics

Each transport has fundamentally different delivery guarantees. Understanding these differences is essential for interpreting receipts.

### Matrix

| Property               | Value                                                                |
| ---------------------- | -------------------------------------------------------------------- |
| Server acknowledgment  | Yes — Synapse returns `event_id` on successful `room_send`           |
| Delivery confirmation  | Server-level only (not per-recipient)                                |
| Duplicate risk         | Low — retries after connection loss may produce duplicates           |
| Receipt interpretation | `sent` with `adapter_message_id` means homeserver accepted the event |

Matrix is the only transport where `sent` implies server-verified persistence.

### Meshtastic

| Property               | Value                                                                                  |
| ---------------------- | -------------------------------------------------------------------------------------- |
| Server acknowledgment  | None beyond local-node acceptance                                                      |
| Delivery confirmation  | None — whether any remote node received the packet is unknown                          |
| Duplicate risk         | High — radio environments cause packet loss; duplicates are normal practice            |
| Receipt interpretation | `sent` means local node accepted for transmission, not that any other node received it |

### MeshCore

Same discipline as Meshtastic — radio best-effort, no confirmation, duplicates are normal.

### LXMF (Reticulum)

| Property               | Value                                                                                                 |
| ---------------------- | ----------------------------------------------------------------------------------------------------- |
| Delivery confirmation  | Eventual — propagation delays from seconds to hours                                                   |
| Duplicate risk         | Low                                                                                                   |
| Receipt interpretation | `sent` means local router accepted for propagation; delivery to destination may take significant time |

## Delivery Receipt States

Receipts progress through these states:

```text
queued → sent
       ↘ failed → dead_lettered
       ↘ suppressed
```

| Status          | Meaning                                                                                                                                                                                                                                       |
| --------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `queued`        | Delivery plan created, waiting for adapter execution.                                                                                                                                                                                         |
| `sent`          | Adapter reported successful handoff to transport. See per-transport table for what this means.                                                                                                                                                |
| `failed`        | Adapter reported delivery failure. Classified by `failure_kind`.                                                                                                                                                                              |
| `dead_lettered` | Exhausted all retries. Permanently failed.                                                                                                                                                                                                    |
| `suppressed`    | Terminal for that delivery attempt. Policy/loop denials remain terminal; direct/replay capacity or shutdown rejection is also terminal for that attempt. Storage-backed live ingress instead defers its durable work row for a later attempt. |

Each receipt carries `attempt_number` and `parent_receipt_id` forming a retry lineage. The `source` column identifies the dispatch mechanism (`"live"`, `"replay"`, or `"retry"`); `replay_run_id` separately preserves named replay origin across later retries.

## See Also

- [Running MEDRE](running-medre.md) — start, monitor, and stop the runtime.
- [Recovery and replay](recovery-and-replay.md) — retry lineage, replay receipts, and duplicate risk assessment.
- [Explicit non-guarantees](running-medre.md#explicit-non-guarantees) — what MEDRE does not promise on top of transport behavior.
