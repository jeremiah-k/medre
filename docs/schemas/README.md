# JSON Schemas

Machine-readable JSON Schema definitions for MEDRE data structures and stable
adapter-owned serialization contracts. Core schemas mirror source models; adapter
contract schemas mirror their projection helpers and normative specifications.

## Schema Files

| Schema                                           | Source Type                                                     | Description                                                                                                                   |
| ------------------------------------------------ | --------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------- |
| `canonical-event.schema.json`                    | `CanonicalEvent` (`src/medre/core/events/canonical.py`)         | Core event record flowing through the pipeline                                                                                |
| `content-inventory.schema.json`                  | `SQLiteStorage.inspect_content_inventory`                       | Read-only logical bytes and consumers without message values                                                                  |
| `content-retention-preview.schema.json`          | `SQLiteStorage.preview_content_retention`                       | Bounded aggregate preview with outcome counts and one optional (created_at, event_id) continuation; the CLI omits that cursor |
| `storage-column-classification.schema.json`      | Declared registry (`src/medre/core/storage/classification.py`)  | Versioned export of the column-level ownership classes over persistent storage tables and columns                             |
| `event-content-release-assessment.schema.json`   | `SQLiteStorage.assess_event_content_release`                    | Metadata-only, non-authorizing per-event release assessment                                                                   |
| `canonical-event.schema.yaml`                    | YAML mirror of `canonical-event.schema.json`                    | YAML representation of the canonical event schema; not an instance document                                                   |
| `styled-text.schema.json`                        | `StyledText` / `TextSpan`                                       | Optional semantic presentation over canonical plain text                                                                      |
| `attachment.schema.json`                         | `AttachmentDescriptor`                                          | Retained/unavailable descriptors and retention-pending input                                                                  |
| `delivery-receipt.schema.json`                   | `DeliveryReceipt` (`src/medre/core/events/canonical.py`)        | Append-only delivery status record                                                                                            |
| `delivery-observation.schema.json`               | `DeliveryObservation` (`src/medre/core/events/canonical.py`)    | Append-only post-handoff transport evidence                                                                                   |
| `delivery-result.schema.json`                    | `AdapterHandoffResult` (`src/medre/core/contracts/delivery.py`) | Successful synchronous adapter hand-off fact                                                                                  |
| `radio-text-plan.schema.json`                    | `RadioTextPlan` (`src/medre/core/rendering/radio_text.py`)      | Immutable multipart radio text wire plan                                                                                      |
| `radio-local-handoff.schema.json`                | `RadioLocalHandoff` (`src/medre/core/storage/radio_handoff.py`) | Synchronous radio local handoff reference scoped to one packet                                                                |
| `radio-attempt-progress.schema.json`             | `StoredRadioAttempt` (`src/medre/core/storage/radio.py`)        | Attempt-bound radio handoff progress retained per outbox attempt                                                              |
| `radio-attempt-inspection.schema.json`           | Dict shape (`medre.core.recovery.radio_assessment`)             | Read-only exact radio attempt inspection with continuation assessment                                                         |
| `bridge-context.schema.json`                     | Dict shape (`medre.core.rendering.bridge_context`)              | Cross-platform relay context claims retained on canonical events                                                              |
| `lifecycle-drill-report.schema.json`             | Dict shape (`medre.runtime.drill.run_drill`)                    | Synthetic configuration/startup drill assignment and coverage evidence                                                        |
| `drill-step.schema.json`                         | Dict shape (`medre.runtime.drill`)                              | Single ordered evidence step within a lifecycle drill report                                                                  |
| `runtime-snapshot.schema.json`                   | `RuntimeSnapshot` (`src/medre/core/supervision/diagnostics.py`) | Point-in-time runtime state snapshot                                                                                          |
| `diagnostics.schema.json`                        | Dict shape (`capture_runtime_snapshot().to_dict()`)             | Diagnostics collector output                                                                                                  |
| `evidence-bundle.schema.json`                    | Dict shape                                                      | `medre evidence` bundle structure                                                                                             |
| `adapter-config.schema.json`                     | Per-transport configs                                           | Adapter configuration shapes                                                                                                  |
| `routing-config.schema.json`                     | `RouteConfig`, `BridgePolicy`                                   | Route matching configuration shapes                                                                                           |
| `runtime-config.schema.json`                     | `RuntimeConfig` (`src/medre/config/model.py`)                   | Shared runtime configuration across runtime, logging, storage, retry, limits, adapters, and routes                            |
| `dev-environment.schema.json`                    | `DevEnvironment` (`src/medre/dev_environment.py`)               | Checkout `.medre-dev.yaml` preferences for adapter selection and SDK extras                                                   |
| `package-sources.schema.json`                    | Deployment source catalog                                       | Adapter distribution aliases and source overrides                                                                             |
| `native-bridge-context-expectations.schema.json` | Dict shape (native campaign report)                             | Optional bridge-context destination expectations in native campaign reports                                                   |
| `stopped-state-inventory.schema.json`            | Dict shape (`medre.maintenance.stopped_inventory`)              | Operator-declared stopped installation inventory for capture/verify/restore                                                   |
| `stopped-state-snapshot.schema.json`             | Dict shape (`medre.maintenance.stopped_snapshot`)               | Captured stopped-state snapshot manifest checked during verify and restore                                                    |
| `operator-decision-inspection.schema.json`       | `medre inspect decisions`                                       | Versioned, read-only cancellation audit with unauthenticated actor provenance                                                 |
| `recovery-action.schema.json`                    | `RecoveryAction` (`medre.core.observability.recovery`)          | Current destination ownership and read-only recovery guidance                                                                 |
| `live-health.schema.json`                        | `LiveHealthSnapshot` (`medre.core.supervision.health`)          | Current manual samples and native health notifications                                                                        |

Executable SQLite fragments for typed admission routing facts, frozen outbox
work ownership, and the durable content-retention activation epochs are
published as `event-routing-facts.schema.sql`,
`outbox-dispatch-snapshots.schema.sql`, and
`content-retention-epochs.schema.sql`. Their table constraints and declared
indexes are checked against the authoritative SQLite DDL.

## Examples

The `examples/` directory contains representative JSON payloads validated
against these schemas.

| Example                                            | Description                        |
| -------------------------------------------------- | ---------------------------------- |
| `examples/matrix-native-metadata-example.json`     | Matrix native metadata payload     |
| `examples/meshtastic-native-metadata-example.json` | Meshtastic native metadata payload |
| `examples/meshcore-native-metadata-example.json`   | MeshCore native metadata payload   |
| `examples/lxmf-native-metadata-example.json`       | LXMF native metadata payload       |
| `examples/delivery-observation-example.json`       | Post-handoff delivery observation  |

## Generation

Schemas are hand-authored to match the current source types or adapter contract.
When a source model or versioned adapter shape changes, update the corresponding
schema and run the schema validation tests:

```bash
python -m pytest tests/test_docs_schema_examples.py tests/test_docs_schema_drift.py -q
```

## Drift Detection

Tests validate examples against their schemas and check that schema required
fields align with example payloads. When example payloads or schemas change,
update both in the same commit.

For stable source models (`CanonicalEvent`, `DeliveryReceipt`,
`DeliveryObservation`, `AdapterHandoffResult`), tests also compare top-level
schema properties against source dataclass fields. If a source model adds or
renames a field without updating the schema, the test fails. These
source-drift tests live in `tests/test_docs_schema_drift.py`.

For built-in transport-native metadata,
`tests/test_prerelease_contract_guards.py` compares the source schema-version
constant with the corresponding JSON Schema `const` and example payload. Each
transport therefore has one version authority across source, machine schema,
and example data.

`AdapterCapabilities` are checked separately by
`tests/test_capability_conformance.py` against
`docs/spec/transport-profiles/*-capabilities.json`.

Runtime and routing schemas describe the shared configuration structure. They
accept community transport and destination kind names. `medre config check`
validates installed-provider availability and invokes adapter-owned native
address validators. Static schema validation alone cannot verify which Python
providers are installed in an operator's environment.

Native metadata schemas and validated examples belong to their adapter repositories; see the [metadata ownership contract](../spec/metadata.md#3-adapter-owned-native-metadata).

`content-collection-state.schema.json` defines the closed aggregate worker
blocks emitted by `MedreApp.diagnostic_snapshot()` and
`medre.runtime.snapshot.build_runtime_snapshot()`. These producers differ
from `core.supervision.capture_runtime_snapshot()`, whose existing
`runtime-snapshot.schema.json` and `diagnostics.schema.json` contracts remain
separate. Unknown worker fields, content, and event identifiers are refused.
