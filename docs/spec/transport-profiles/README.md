# Transport profiles

Each supported adapter is separately installed and implements the same core
routing, lifecycle, health, diagnostics, and evidence contracts. A first-class
adapter does not imply that its network supports every message feature.

| Adapter                        | Native conversation                        | Capability declaration                |
| ------------------------------ | ------------------------------------------ | ------------------------------------- |
| [Matrix](matrix.md)            | Rooms                                      | [JSON](matrix-capabilities.json)      |
| [Meshtastic](meshtastic.md)    | Radio channels                             | [JSON](meshtastic-capabilities.json)  |
| [MeshCore](meshcore.md)        | Companion channels and configured contacts | [JSON](meshcore-capabilities.json)    |
| [LXMF](lxmf.md)                | Reticulum destinations                     | [JSON](lxmf-capabilities.json)        |
| [XMPP](xmpp.md)                | Multi-user rooms and direct peers          | [JSON](xmpp-capabilities.json)        |
| [Chatmail](chatmail.md)        | Accepted groups and direct chats           | [JSON](chatmail-capabilities.json)    |
| [SimpleX Chat](simplexchat.md) | Active groups and direct contacts          | [JSON](simplexchat-capabilities.json) |

Capability declarations describe default instances. Configuration can further
restrict behavior or change a text budget; the adapter's reported capabilities
MUST govern delivery planning. Unsupported features MUST retain the specified
fallback or refusal behavior rather than gaining capabilities from the source
transport. Native encryption MUST NOT imply end-to-end secrecy across a bridge.

Operator setup is documented in [transport setup](../../ops/transport-setup/README.md).
Native qualification and its limits are recorded in
[SDK parity](../../dev/adapter-sdk-parity.md). A local queue or transport handoff
MUST NOT be promoted to remote recipient delivery or read confirmation.
