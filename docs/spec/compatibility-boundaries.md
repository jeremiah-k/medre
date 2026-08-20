# Compatibility Boundaries

MEDRE is pre-first-release. Internal development shapes are not compatibility
contracts. This specification distinguishes compatibility that preserves a real
external boundary from tolerance for abandoned MEDRE representations.

## 1. Policy

A production compatibility path is valid only when it serves one of these
boundaries:

1. an external protocol or wire convention;
2. a supported third-party SDK surface whose available versions differ;
3. an operator-visible runtime behavior that is intentionally part of the
   current contract; or
4. a bounded operational degradation path for current runtime evidence.

A reader, parser, constructor, or serializer MUST NOT accept an obsolete MEDRE
stored-event, native-metadata, configuration, report, or renderer shape solely
because an earlier development build produced it.

Breaking development-shape changes update source, schemas, examples, specs, and
tests together. There is no canonical-event migration registry and no automatic
SQLite schema migration. Incompatible pre-release databases are rejected with
an actionable error and must be recreated or handled explicitly by the operator.

## 2. Explicitly Retained External Interoperability

### 2.1 MMRelay

MMRelay interoperability is an external wire boundary, not an alternate MEDRE
event shape. MMRelay scalar fields are isolated under
`metadata.native.data["interop"]["mmrelay"]` when captured into canonical
metadata. Relation metadata may also retain the established MMRelay relation
fields required to reconstruct Meshtastic-compatible replies and reactions.

The owning code is limited to the Matrix codec/renderer and
`medre.interop.mmrelay`. Other core or adapter modules must not import MMRelay
wire constants directly.

### 2.2 Matrix / mindroom-nio

`medre.adapters.matrix.compat` and `MatrixSession` use feature and import guards
for the optional pinned mindroom-nio dependency. These guards adapt to actual SDK
availability; they do not interpret earlier MEDRE event shapes.

### 2.3 MeshCore SDK

`MeshCoreSession` retains bounded SDK-shape detection for optional event types,
self-info callback objects, and send-result fields. In particular,
`expected_ack` is authoritative for the pinned SDK while `message_id` remains a
defensive SDK-version fallback. This tolerance ends at the SDK boundary; stored
canonical metadata uses only the versioned `native.meshcore` contract.

### 2.4 Meshtastic SDK

`MeshtasticSession` tolerates SDK/fake-client capability differences such as an
absent `isConnected` event and optional callback fields. Packet protocol
fallbacks in the classifier are protocol vocabulary, not MEDRE event-shape
compatibility. Persisted metadata uses only `native.meshtastic`.

### 2.5 LXMF / Reticulum

LXMF session and codec normalization accepts the text/bytes and SDK state shapes
that the external libraries can produce. Persisted metadata uses only
`native.lxmf`; renderer constructors and attribution readers do not accept an
older MEDRE native-metadata representation.

## 3. Current MEDRE Wire Envelopes

MEDRE provenance carried over Matrix (`medre.envelope`) and LXMF
(`fields[0xFD]["medre"]`) is a current MEDRE wire contract. Readers require the
current explicit envelope schema version; missing or unsupported versions are
not interpreted as the current shape. This is a versioned current contract, not
a compatibility reader for earlier development envelopes.

## 4. Intentional Current-Runtime Convenience

The runtime may synthesize target renderer configuration for enabled fake
adapters used by tests and deterministic local integrations. This is a current
construction convenience, not a serialized compatibility contract.

The Docker integration artifact collector may derive reduced evidence from
pytest output when `run-metadata.json` is unavailable. That path is explicitly
lower-confidence operational degradation for the current runner; it does not
parse an obsolete persisted MEDRE event or configuration shape.

Optional fields in diagnostics and trace output may be omitted when unknown so
outputs remain sparse and truthful. Omission is not a promise to preserve an
older output schema.

### 4.1 Exception Ledger

The following compatibility/degradation seams are intentionally retained. This
table is exhaustive for production paths whose behavior adapts to an alternate
external/runtime shape rather than merely implementing normal capability
fallback semantics.

| Seam                                                 | Category                            | Code authority                                                           | Reason retained                                                                                       |
| ---------------------------------------------------- | ----------------------------------- | ------------------------------------------------------------------------ | ----------------------------------------------------------------------------------------------------- |
| MMRelay Matrix wire fields                           | external wire                       | `medre.interop.mmrelay`, Matrix codec/renderer                           | Interoperate with MMRelay's established Matrix/Meshtastic wire convention.                            |
| mindroom-nio optional event/import surfaces          | third-party SDK                     | `medre.adapters.matrix.compat`, `medre.adapters.matrix.session`          | Bound behavior to SDK features that are actually importable.                                          |
| MeshCore optional event/self-info/send-result fields | third-party SDK                     | `medre.adapters.meshcore.compat`, `medre.adapters.meshcore.session`      | Normalize SDK callback/result variants at the session boundary.                                       |
| Meshtastic optional SDK connection/callback surfaces | third-party SDK                     | `medre.adapters.meshtastic.session`                                      | Tolerate supported SDK/fake-client capability differences without changing persisted shape.           |
| Meshtastic numeric PortNum vocabulary                | external protocol                   | `medre.adapters.meshtastic.packet_classifier`                            | Preserve protocol-correct classification when the optional SDK enum is unavailable.                   |
| LXMF/Reticulum text, bytes, and state normalization  | third-party SDK/protocol            | `medre.adapters.lxmf.session`, `medre.adapters.lxmf.codec`               | Normalize values emitted by the supported external libraries.                                         |
| Replay stub `plan_delivery` / `deliver` path         | deterministic test seam             | `medre.core.engine.replay.planning`, `medre.core.engine.replay.delivery` | Exercise replay stages with structural test pipelines; real runtime replay uses `deliver_to_targets`. |
| Fake-adapter renderer configuration synthesis        | deterministic test/integration seam | `medre.runtime.builder`                                                  | Build current fake-adapter scenarios without inventing serialized compatibility formats.              |
| Docker stdout evidence derivation                    | bounded operational degradation     | `medre.runtime.docker_bridge_artifacts`                                  | Produce explicitly lower-confidence evidence when structured `run-metadata.json` is unavailable.      |

Capability levels named `fallback` (for example thread/reaction rendering) are
not compatibility exceptions. They are ordinary current delivery semantics and
are defined by the capability-planning specification.

## 5. Explicit Rejections

The current runtime rejects or ignores these abandoned development shapes:

- flat or dotted transport-native fields in canonical event metadata;
- unversioned built-in transport native metadata;
- bare-string `channel_room_map` entries;
- the old replay renderer `render_event` hook;
- the old `PipelineRunner.ingress_handler` callable;
- the old direct-constructor LXMF relay-prefix argument;
- flat smoke-session command report data; and
- incompatible SQLite schema fingerprints or schema versions.

The supported built-in canonical native shapes are versioned namespaces:
`native.matrix`, `native.meshtastic`, `native.meshcore`, and `native.lxmf`.
MMRelay interoperability, when present, is separately namespaced under
`native.interop.mmrelay`.

## 6. Review Rule

Any new use of terms such as `legacy`, `backward compatibility`, `deprecated`,
or `migration` in production code must be reviewed against this document. A new
compatibility branch must identify the external or operator contract that
requires it. Development history alone is not sufficient justification.
