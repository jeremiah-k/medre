# OutboxManager Extraction

Extract outbox lifecycle operations from `PipelineRunner` into a dedicated
`OutboxManager` module. Pure refactoring — no behavior changes.

## Changed

- `src/medre/core/engine/pipeline/runner.py` — removed `_create_outbox_for_delivery`, `_start_outbox_lease_renewal`, `_finalize_outbox_outcome`, `_record_outbound_terminal` methods; removed `_OUTBOX_RENEWAL_INTERVAL_SECONDS` and `_OUTBOX_RENEWAL_DURATION_SECONDS` constants; delegates to `OutboxManager` instance
- `src/medre/runtime/app.py` — updated `record_outbound_terminal` callback wiring from `self.pipeline_runner._record_outbound_terminal` to `self.pipeline_runner._outbox_manager.record_terminal`

## Added

- `src/medre/core/engine/pipeline/outbox_manager.py` — `OutboxManager` class with `create_for_delivery()`, `start_lease_renewal()`, `cancel_renewal()`, `finalize_outcome()`, `record_terminal()`; `OutboxContext` frozen dataclass; lease renewal constants
