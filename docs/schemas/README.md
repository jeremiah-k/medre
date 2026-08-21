# JSON Schemas

Machine-readable JSON Schema definitions for MEDRE data structures and stable
adapter-owned serialization contracts. Core schemas mirror source models; adapter
contract schemas mirror their projection helpers and normative specifications.

## Schema Files

| Schema                                   | Source Type                                                    | Description                                    |
| ---------------------------------------- | -------------------------------------------------------------- | ---------------------------------------------- |
| `canonical-event.schema.json`            | `CanonicalEvent` (`src/medre/core/events/canonical.py`)        | Core event record flowing through the pipeline |
| `canonical-event.schema.yaml`            | YAML mirror of `canonical-event.schema.json`                   | YAML representation of the canonical event schema; not an instance document |
| `delivery-receipt.schema.json`           | `DeliveryReceipt` (`src/medre/core/events/canonical.py`)      | Append-only delivery status record             |
| `delivery-result.schema.json`            | `AdapterDeliveryResult` (`src/medre/core/contracts/adapter.py`)| Per-adapter delivery outcome                   |
| `runtime-snapshot.schema.json`           | `RuntimeSnapshot` (`src/medre/core/supervision/diagnostics.py`)| Point-in-time runtime state snapshot           |
| `diagnostics.schema.json`                | Dict shape (`capture_runtime_snapshot().to_dict()`)            | Diagnostics collector output                   |
| `evidence-bundle.schema.json`            | Dict shape                                                     | `medre evidence` bundle structure              |
| `adapter-config.schema.json`             | Per-transport configs                                          | Adapter configuration shapes                   |
| `routing-config.schema.json`             | `RouteConfig`, `BridgePolicy`                                  | Route matching configuration shapes            |
| `matrix-native-metadata.schema.json`     | Matrix native metadata                                         | Versioned Matrix native namespace              |
| `meshtastic-native-metadata.schema.json` | Meshtastic native metadata                                     | Versioned Meshtastic native namespace          |
| `meshcore-native-metadata.schema.json`   | MeshCore native metadata                                       | Versioned MeshCore native namespace            |
| `lxmf-native-metadata.schema.json`       | LXMF native metadata                                           | Versioned LXMF native namespace                |

## Examples

The `examples/` directory contains representative JSON payloads validated
against these schemas.

| Example                                             | Description                        |
| --------------------------------------------------- | ---------------------------------- |
| `examples/matrix-native-metadata-example.json`      | Matrix native metadata payload     |
| `examples/meshtastic-native-metadata-example.json`  | Meshtastic native metadata payload |
| `examples/meshcore-native-metadata-example.json`    | MeshCore native metadata payload   |
| `examples/lxmf-native-metadata-example.json`        | LXMF native metadata payload       |

## Generation

Schemas are hand-authored to match the current source types or adapter contract.
When a source model or versioned adapter shape changes, update the corresponding
schema and run the schema validation tests:

```bash
python -m pytest tests/test_docs_schema_examples.py -q
```

## Drift Detection

Tests validate examples against their schemas and check that schema required
fields align with example payloads. When example payloads or schemas change,
update both in the same commit.

For stable source models (`CanonicalEvent`, `DeliveryReceipt`,
`AdapterDeliveryResult`), tests also compare top-level schema properties
against source dataclass fields. If a source model adds or renames a field
without updating the schema, the test fails.

For built-in transport-native metadata, the current-state inventory check
compares the source schema-version constant with the corresponding JSON Schema
`const` and example payload. Each transport therefore has one version authority
across source, machine schema, and example data.

`AdapterCapabilities` are checked separately by
`tests/test_capability_conformance.py` against
`docs/spec/transport-profiles/*-capabilities.json`.
