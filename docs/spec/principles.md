# Design Principles

This document defines the foundational design principles of the MEDRE runtime.
Every behavioral claim in other spec documents derives from these principles.

See also: [architecture.md](architecture.md), [event-model.md](event-model.md),
[conformance.md](conformance.md).

---

## 1. Event-First, Not Message-First

Messages (text, images, files) are one `event_kind` among many. The pipeline
handles telemetry, metrics, presence, system signals, and plugin events
identically. Adapters decide how to render each event kind for their platform.

No event kind is privileged. A `telemetry` event flows through the same
ingress, storage, enrichment, transform, policy, routing, and delivery stages
as a `message.text` event.

## 2. Immutability

Canonical events are append-only records. Enrichment, transformation, and
policy evaluation create new derived events that reference their parent via
`parent_event_id`. No code path modifies an event after creation.

Derived events carry a `lineage` list that traces back to the original source
event. The original is always recoverable by walking the lineage chain.

## 3. Transport Agnostic

No adapter is special-cased in core. No single radio protocol or chat platform
is central to the design. Meshtastic is not the "source of truth." Matrix is
not the "primary interface." Both are adapters with defined roles.

Code that needs to answer "what protocol does this adapter speak?" MUST use the
`platform` string, not the `adapter_id` instance name.

## 4. Pipeline over Callback

Events flow through explicit stages: ingress, store, enrichment, transform,
policy, routing, delivery planning, rendering, adapter execution, receipts.
Each stage is inspectable, testable, and replaceable. No adapter directly
calls another adapter. All inter-adapter communication goes through the
pipeline.

## 5. Schema Evolution over Schema Lock

Unknown fields in events are preserved, not stripped. Known fields keep their
meaning. Deprecation follows time-bounded windows. Adapters declare the schema
version they understand. Schema versions are monotonically increasing integers.

## 6. Storage Authoritative

The canonical event log in storage is the single source of truth. Metadata
embedded in external platforms (Matrix custom content fields, LXMF fields
dicts) is secondary and may be lost due to redaction, pruning, or platform API
changes. Any feature that needs reliable metadata (replay, correlation,
identity resolution) MUST read from storage, not from external platforms.

## 7. Replayable

The canonical event log supports reprocessing events through the pipeline for
plugin changes, routing changes, and debugging. Replay does not modify existing
events; it creates new derived events and receipts.

## 8. Observable

Every stage of the pipeline emits structured telemetry. Structured logging
covers all pipeline stages with timestamps, stage names, event IDs, adapter
names, durations, and outcomes. Metrics track ingress counts, delivery
outcomes, latencies, and adapter health.

## 9. Explicit Boundaries

Adapters, plugins, and pipeline stages communicate through well-defined
interfaces. Adapters receive an `AdapterContext` with scoped access to runtime
services. Plugins receive a `PluginContext` gated by declared capabilities.
No component reaches into another component's internals.

Import rules enforce boundaries at the package level:

- `core/` MUST NOT import from `adapters/`, `config/`, `cli/`, or `runtime/`.
- `config/` MUST NOT import from `adapters/` or `runtime/`.
- Adapters MUST NOT import from other adapter packages or from `runtime/`.
