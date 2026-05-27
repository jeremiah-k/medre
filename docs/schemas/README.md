# JSON Schemas

Machine-readable JSON Schema definitions for MEDRE data structures. These
schemas are derived from the `msgspec.Struct` types in the source code.

## Schema Files

| Schema                         | Source Type                   | Description                                    |
| ------------------------------ | ----------------------------- | ---------------------------------------------- |
| `canonical-event.schema.json`  | `CanonicalEvent`              | Core event record flowing through the pipeline |
| `delivery-receipt.schema.json` | `DeliveryReceipt`             | Append-only delivery status record             |
| `delivery-result.schema.json`  | `AdapterDeliveryResult`       | Per-adapter delivery outcome                   |
| `runtime-snapshot.schema.json` | `RuntimeSnapshot`             | Point-in-time runtime state snapshot           |
| `diagnostics.schema.json`      | Dict shape                    | Diagnostics collector output                   |
| `evidence-bundle.schema.json`  | Dict shape                    | `medre evidence` bundle structure              |
| `adapter-config.schema.json`   | Per-transport configs         | Adapter configuration shapes                   |
| `routing-config.schema.json`   | `RouteConfig`, `BridgePolicy` | Route matching configuration shapes            |

## Examples

The `examples/` directory contains representative JSON payloads validated
against these schemas.

## Generation

Schemas are hand-authored to match the current source types. When a `msgspec`
type changes, update the corresponding schema and run the schema validation
tests:

```bash
python -m pytest tests/test_docs_schema_examples.py -q
```

## Drift Detection

Tests compare schema required fields against example payloads and validate
examples against schemas. If a schema drifts from its example, the test fails
and both must be updated.
