# Queued Delivery Outbox Correlation and Terminal Outcome Reporting

Add exact `outbox_id`/`attempt_number` correlation for async queued adapters,
stale callback protection, terminal queue outcome reporting, and remove
`delivery_plan_id=None` legacy fallback.

## Changed

- `src/medre/core/contracts/adapter.py` — added `QueueTerminalRecord`, `outbox_id`/`attempt_number` on `OutboundNativeRefRecord`, `record_outbound_terminal` callback on `AdapterContext`
- `src/medre/core/rendering/renderer.py` — added `outbox_id`, `attempt_number` to `RenderingResult`
- `src/medre/core/engine/pipeline/delivery_lifecycle.py` — rewritten 3-priority correlation: exact `outbox_id` (stale-safe) → `delivery_plan_id` fallback → no keys warn+return
- `src/medre/core/engine/pipeline/runner.py` — added `_record_outbound_terminal()` mapping 4 outcomes to receipt status + outbox transitions
- `src/medre/core/engine/pipeline/target_delivery.py` — stamps `outbox_id`/`attempt_number` onto `RenderingResult`
- `src/medre/core/engine/pipeline/receipt_factory.py` — `outbox_id` param on `build_delivery_receipt`
- `src/medre/adapters/meshtastic/queue.py` — `QueueTerminalResult` dataclass; `process_one` returns terminal results; `pop_cancelled_item()`, `drain_all()` methods
- `src/medre/adapters/meshtastic/adapter.py` — `_report_queue_terminal`, `_report_cancelled_and_drain`; passes `outbox_id` through enqueue and delayed callback
- `src/medre/runtime/retry.py` — passes `outbox_id=item.outbox_id` to `deliver_to_target`
- `src/medre/core/storage/sqlite/schema.py` — added `outbox_id TEXT` column to `delivery_receipts`
- `src/medre/core/storage/sqlite/statements.py`, `serde.py`, `_receipt.py` — updated for `outbox_id` column
- `src/medre/runtime/app.py` — wired `record_outbound_terminal` in `AdapterContext`
- `docs/spec/delivery-lifecycle.md` — added §3.4–3.6 (async queued correlation, stale protection, terminal outcomes)
- `docs/spec/transport-profiles/meshtastic.md` — updated queue semantics for terminal outcome reporting and correlation
