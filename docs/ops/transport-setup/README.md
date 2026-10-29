# Transport setup

Install each provider in the same environment as MEDRE. A persistent tool uses
`medre adapters install ALIAS`; a deployment project uses its locked dependency
selection. All seven adapters use the same configuration and route builder.
Native accounts, devices, profiles, and delivery evidence remain transport-specific.

| Adapter                        | Prepare                                         | Conversation address                           |
| ------------------------------ | ----------------------------------------------- | ---------------------------------------------- |
| [Matrix](matrix.md)            | Dedicated account, joined rooms, optional E2EE  | Canonical room ID, such as `!room:example.org` |
| [Meshtastic](meshtastic.md)    | Serial, TCP or BLE node and radio channels      | Channel index, such as `0`                     |
| [MeshCore](meshcore.md)        | Companion node, contacts and channels           | Configured channel or contact                  |
| [LXMF](lxmf.md)                | Reticulum configuration and persistent identity | LXMF destination hash                          |
| [XMPP](xmpp.md)                | TLS account, allowed MUC rooms and peers        | Bare room or contact JID                       |
| [Chatmail](chatmail.md)        | Dedicated account and accepted chats            | Profile-local decimal chat ID                  |
| [SimpleX Chat](simplexchat.md) | Dedicated profile and active conversations      | Profile-local `group:ID` or `contact:ID`       |

Use `medre adapters` to inspect the installed providers. A generated
`medre config sample` includes their SDK-free fakes. A fake smoke test checks
routing and evidence locally; validate native delivery from an independent
recipient before relying on a bridge. See the
[transport profiles](../../spec/transport-profiles/README.md) for capabilities
and [SDK parity](../../dev/adapter-sdk-parity.md) for qualification boundaries.
