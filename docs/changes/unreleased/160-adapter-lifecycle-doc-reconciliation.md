# Adapter lifecycle doc reconciliation

- Realign `docs/spec/adapter-runtime.md` §8 with the eight-state
  `AdapterState` enum defined in
  `src/medre/core/lifecycle/states.py`: `INITIALIZING`, `READY`,
  `DEGRADED`, `BACKPRESSURED`, `DISCONNECTED`, `STOPPING`, `FAILED`,
  `STOPPED`.
- Transcribe `VALID_TRANSITIONS` faithfully into the spec, including the
  three states the previous five-state summary omitted
  (`BACKPRESSURED`, `DISCONNECTED`, `FAILED`) and renaming `RUNNING` to
  `READY` and `DRAINING` to `STOPPING` to match the code vocabulary.
- Update the simplified-vocabulary mapping table to cover all eight
  states, keeping consistency with the operator evidence labels in
  `docs/spec/diagnostics-evidence.md` §18 (`connected` -> `READY`;
  `degraded` -> `DEGRADED` or `BACKPRESSURED`; `unavailable` ->
  `DISCONNECTED`; `stopping` -> `STOPPING`; `failed` -> `FAILED`;
  `stopped` -> `STOPPED`).
- Update stale `RUNNING`/`DRAINING` references in `docs/ops/running-medre.md`,
  `docs/ops/troubleshooting.md`, `docs/ops/recovery-and-replay.md`,
  `docs/ops/diagnostics-and-evidence.md`, and
  `docs/dev/adapter-authoring.md` to use the current state names.

No runtime behavior changes — this change is documentation only.
