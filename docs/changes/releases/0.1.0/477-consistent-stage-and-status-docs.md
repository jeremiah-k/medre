# Reconcile implemented-stage and operator-status documentation

`docs/spec/principles.md` described enrich, transform, and event policy as
stages executing for every event, while the `PipelinePhase` enum implements
six phases (ingress, dedup, resolve_relations, store, route, deliver) and
`docs/spec/architecture.md` already marked those three stages as reserved
future extension points with no code path. `principles.md` now states the
implemented phases from the enum and labels the reserved stages as future,
and a docs guard test pins the implemented-phase lists in `principles.md`
and `architecture.md` against the enum.

`docs/dev/development-priorities.md` still directed the next measure to be
producer-side radio ingress pressure; that work is merged, so the execution
section records the landed baseline instead: bounded callback admission with
per-source fair shares, durable pre-admission pressure evidence with the
content-free producer report sink, the sustained multi-source contention
suite, bounded outage retention with accounting-gap records, and
process-lifetime qualification of the producer pressure collector.

`docs/dev/contract-gap-status.md` G11 claimed the simplified first-run
journey was absent; `docs/ops/getting-started.md` with the guided `medre
config native` and `medre config bridge` commands supplies a real but
incomplete journey — native setup forms cover three of seven providers and
readiness and evidence visibility remain open — so the row states that
precise remaining coverage. G12 acknowledges bounded MeshCore callback
admission beside Meshtastic.
