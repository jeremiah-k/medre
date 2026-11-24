# Transport internals

Per-transport adapter internals behind the operator
[transport setup guides](../../ops/transport-setup/README.md): adapter
construction against internal APIs, startup and shutdown sequences, queue and
packet diagnostics, and test harnesses. Operator setup lives in the setup
guides; the normative contracts live in the
[transport profiles](../../spec/transport-profiles/README.md).

| Transport                   | Internals page                                                                                      |
| --------------------------- | --------------------------------------------------------------------------------------------------- |
| [Matrix](matrix.md)         | Device identity and crypto store, validation harnesses, Classic Sync durability                     |
| [Meshtastic](meshtastic.md) | Connection modes, manual adapter wiring, startup/shutdown, queue diagnostics, packet classification |
| [MeshCore](meshcore.md)     | BLE session handling, connection modes, manual wiring, startup/shutdown, outbound delivery          |
| [LXMF](lxmf.md)             | Reticulum object conversion, delivery threading, router shutdown ownership                          |

Transports without a page here keep all of their guidance in the operator
setup guide.
