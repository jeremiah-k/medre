# Adapter Reuse

Which adapter modules you can import standalone, and what the boundaries are.

MEDRE's adapter layer is split so that codec and renderer modules work without
the full runtime. Other tools, scripts, or test harnesses can decode packets,
render events, or inspect wire formats without pulling in the application
server, storage, or protocol SDKs. This document describes what's reusable,
what isn't, and the rules that keep it that way.

See [Module Boundaries](module-boundaries.md) for the full import graph and
package ownership tables.

## Intended Layers

Each transport adapter (Matrix, Meshtastic, MeshCore, LXMF) is built from four
cooperating layers, each with a single responsibility:

**codec**
Decodes native transport events into `CanonicalEvent` instances. Operates
on raw dicts or SDK objects. Produces canonical events. Does not send
anything.

**renderer**
Encodes `CanonicalEvent` instances into native transport payloads (Matrix
`m.room.message` dicts, Meshtastic text payloads, etc.). Pure transform,
no I/O.

**session**
Owns the protocol client lifecycle. Connects, reconnects, subscribes to
transport events, and sends rendered payloads. This is the only layer
that imports the heavy protocol SDK (nio, meshtastic, etc.).

**adapter**
Runtime integration wrapper. Ties codec, renderer, session, and adapter
config together into a single object that `MedreApp` manages. Imports
everything, wires lifecycle hooks, and participates in the pipeline.

Each transport package also contains helpers alongside these four: packet
classifiers, error types, compatibility guards, metadata envelopes, and so on.

## Reusable Modules

These modules are designed for standalone import. They don't require the MEDRE
runtime, storage, or CLI infrastructure.

```text
medre.adapters.matrix.codec           MatrixCodec            (nio-free)
medre.adapters.matrix.renderer        MatrixRenderer
medre.adapters.matrix.session         MatrixSession          (requires nio at runtime)

medre.adapters.meshtastic.codec       MeshtasticCodec        (protobuf-free)
medre.adapters.meshtastic.renderer    MeshtasticRenderer
medre.adapters.meshtastic.session     MeshtasticSession      (requires meshtastic pkg)

medre.adapters.meshcore.codec         MeshCoreCodec
medre.adapters.meshcore.renderer      MeshCoreRenderer
medre.adapters.meshcore.session       MeshCoreSession

medre.adapters.lxmf.codec             LxmfCodec
medre.adapters.lxmf.renderer          LxmfRenderer
medre.adapters.lxmf.session           LxmfSession

medre.interop.mmrelay                 Wire-format constants (KEY_ID, KEY_LONGNAME, etc.)
```

Codec and renderer modules are SDK-free. They operate on plain dicts and
`CanonicalEvent` objects, so they're importable without optional dependencies.
Session modules require their protocol SDK but defer the import inside methods
rather than at module level.

`medre.interop.mmrelay` defines the MMRelay wire-format key names and protocol
values. These live outside any single adapter package because they represent a
cross-adapter wire contract consumed by both Matrix and Meshtastic code.

## Runtime-Specific Modules

These modules are MEDRE-application internals. They depend on the runtime
lifecycle, storage, or CLI infrastructure and are not intended for standalone
reuse.

```text
medre.adapters.<transport>.adapter     runtime integration wrapper
medre.runtime.builder                  RuntimeBuilder (dynamic adapter construction)
medre.runtime.route_engine             route expansion and registration
medre.core.engine.pipeline             PipelineRunner
medre.core.storage.*                   persistence layer
medre.cli.*                            CLI commands
medre.runtime.app                      MedreApp top-level orchestrator
medre.runtime.capacity                 capacity controller
```

## Examples

### Example 1: Decode a Meshtastic packet without running MEDRE

```python
from medre.adapters.meshtastic.codec import MeshtasticCodec
from types import SimpleNamespace

config = SimpleNamespace()
codec = MeshtasticCodec(adapter_id="my-tool", config=config)

packet = {
    "from": 123,
    "fromId": "!abcd",
    "to": 0xFFFFFFFF,
    "channel": 0,
    "id": 42,
    "decoded": {"portnum": "TEXT_MESSAGE_APP", "text": "hello"},
}
event = codec.decode(packet)
print(event.payload["body"])
```

No `meshtastic` package required. No runtime, no storage, no CLI.

### Example 2: Render a CanonicalEvent to Matrix content

```python
from medre.adapters.matrix.renderer import MatrixRenderer
from medre.core.events import CanonicalEvent, EventMetadata
from datetime import datetime, timezone

event = CanonicalEvent(
    event_id="evt-1",
    event_kind="message.created",
    schema_version=1,
    timestamp=datetime.now(timezone.utc),
    source_adapter="mesh-1",
    source_transport_id="!node1",
    source_channel_id="0",
    parent_event_id=None,
    lineage=(),
    relations=(),
    payload={"body": "hello from mesh"},
    metadata=EventMetadata(),
)

renderer = MatrixRenderer()
result = await renderer.render(event, target_adapter="matrix-1")
print(result.payload["msgtype"])  # "m.text"
print(result.payload["body"])     # "hello from mesh"
```

### Example 3: Render a CanonicalEvent to Meshtastic payload

```python
from medre.adapters.meshtastic.renderer import MeshtasticRenderer

renderer = MeshtasticRenderer()
result = await renderer.render(event, target_adapter="mesh-1", target_channel="3")
print(result.payload["text"])           # "hello from mesh"
print(result.payload["channel_index"])  # 3
```

### Example 4: Use MEDRE primitives in a custom daemon

```python
from medre.adapters.meshtastic.codec import MeshtasticCodec
from medre.adapters.matrix.renderer import MatrixRenderer
from medre.interop.mmrelay import KEY_ID, KEY_TEXT, PORTNUM_TEXT
from types import SimpleNamespace

# Decode mesh packets and render Matrix payloads without
# importing the runtime, storage, or any protocol SDK.
codec = MeshtasticCodec(adapter_id="relay", config=SimpleNamespace())
renderer = MatrixRenderer(mmrelay_compat=True, meshnet_name="my-mesh")
```

No runtime infrastructure needed. Just the codec/renderer primitives and the
interop wire constants.

## Boundary Rules

The following rules keep reusable modules clean and independent.

| Rule                                                                                                           | Rationale                                                            |
| -------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------- |
| Reusable modules (codec, renderer, interop) MUST NOT import runtime, builder, pipeline, storage, or CLI        | Keeps them importable in isolation                                   |
| `core/` MUST NOT import from `adapters/`                                                                       | Core is transport-agnostic by definition                             |
| Config model wraps adapter config dataclasses only, no adapter implementation imports                          | Config stays dependency-free                                         |
| Route engine may use platform strings (`"matrix"`, `"meshtastic"`) but MUST NOT import adapter implementations | Platform dispatch stays string-based                                 |
| Logging setup happens only through app/CLI bootstrap, never as an import side effect                           | Prevents handler duplication and test pollution                      |
| Codec and renderer modules MUST NOT import heavy protocol SDKs (nio, meshtastic, etc.)                         | Preserves SDK-free guarantee                                         |
| Session modules are the only place SDKs are imported, and imports are deferred inside methods                  | Allows graceful import-error handling when optional deps are missing |

See [Module Boundaries](module-boundaries.md) for the complete import-rule
table covering every package.

## Future Note

As more transports are added, `config/model.py` accumulates a dataclass and
registration entry per adapter. If this grows unwieldy, a small adapter config
registry could replace the manual wiring:

```text
transport name (str)          e.g. "meshtastic"
config dataclass (type)       e.g. MeshtasticConfig
runtime wrapper class (type)  e.g. MeshtasticAdapter
parser hook (callable)        e.g. add_meshtastic_args(parser)
```

Each adapter package would register itself, and the builder would look up
entries by transport name instead of importing everything centrally. This is
a future consideration, not something to implement now.
