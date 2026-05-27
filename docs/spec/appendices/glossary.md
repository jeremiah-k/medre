# Glossary

Key terms used across the MEDRE specification.

---

| Term                    | Definition                                                                                            |
| ----------------------- | ----------------------------------------------------------------------------------------------------- |
| **Adapter**             | A component that moves events between the MEDRE pipeline and an external transport or platform.       |
| **AdapterCodec**        | The format-conversion interface within an adapter: native protocol data to/from canonical events.     |
| **AdapterContext**      | The scoped runtime services object passed to each adapter on initialization.                         |
| **CanonicalActor**      | A resolved identity within MEDRE that may link multiple native identities across transports.          |
| **CanonicalEvent**      | The immutable, transport-agnostic event record that flows through the pipeline.                       |
| **Codec**               | See AdapterCodec.                                                                                     |
| **Constrained Transport** | A transport with tight payload limits (e.g., Meshtastic ~227 bytes, MeshCore 184 bytes).           |
| **Dead Letter**         | A delivery that has exhausted all retry attempts and is permanently undelivered.                      |
| **Delivery Plan**       | A constructed plan for delivering an event to a specific adapter target, including fallback chain.    |
| **Delivery Receipt**    | An append-only record of a single delivery attempt, including status and outcome.                     |
| **Derived Event**       | An event produced by enrichment, transformation, or policy stages, referencing a parent event.        |
| **Embedding Mode**      | The operator-configurable level of metadata embedded in outbound native payloads (`off`/`minimal`/`safe`/`full`). |
| **Envelope**            | MEDRE metadata embedded in an outbound native payload on an external platform.                        |
| **Event Bus**           | The central async pub/sub mechanism through which events flow between pipeline stages.                |
| **Event Kind**          | A string identifying the type of an event (e.g., `message.text`, `telemetry`, `presence`).            |
| **EventRelation**       | A first-class relation between events (reply, reaction, edit, delete, thread).                        |
| **Fan-out**             | A single event being delivered to multiple adapters or destinations.                                  |
| **Fake Adapter**        | A test adapter that exercises the full pipeline without real hardware or network.                     |
| **Ingress**             | The process of receiving raw data from a transport and converting it to a canonical event.            |
| **Lineage**             | The chain of event IDs from the original source event to the current derived event.                   |
| **Live Validation**     | Testing against a real transport endpoint (real homeserver, real radio, real network).                |
| **MXID**                | Matrix user identifier (e.g., `@alice:matrix.org`).                                                   |
| **NativeIdentity**      | Identity as it exists on a specific transport, scoped to an adapter instance.                         |
| **NativeMessageRef**    | The mapping between a canonical event and a native protocol message identifier.                       |
| **NativeRef**           | A structured reference to a native message on an adapter (adapter, channel, message, thread IDs).     |
| **Never-Embed List**    | The set of fields that must never appear in outbound envelopes, regardless of embedding mode.         |
| **Pipeline**            | The ordered sequence of stages through which every event flows.                                       |
| **Platform**            | The protocol family an adapter speaks (e.g., `"meshtastic"`, `"meshcore"`, `"matrix"`).               |
| **Plugin**              | A component that observes or emits events through the plugin API, within capability boundaries.       |
| **Presentation Adapter** | An adapter that presents events to human users (e.g., Matrix, Discord).                              |
| **Receipt**             | See Delivery Receipt.                                                                                 |
| **Rendering**           | The process of converting a canonical event into a target-specific payload for delivery.              |
| **Replay**              | Reprocessing historical events through the pipeline for plugin, routing, or debugging changes.        |
| **Route**               | A configured mapping from a source (adapter, event kinds, channel) to one or more targets.            |
| **Route Policy**        | Rules evaluated after routing but before delivery, controlling which routes proceed.                  |
| **Schema Version**      | A monotonically increasing integer identifying the event schema revision.                             |
| **Source Event**        | The initial canonical event produced by an adapter codec from raw native data.                        |
| **source_transport_id** | The native sender identity from the source transport, stored as a string on CanonicalEvent.           |
| **source_channel_id**   | The native channel, room, or topic where the event originated.                                        |
| **Storage Authoritative**| The principle that the canonical event log is the single source of truth, overriding embedded metadata. |
| **Suppressed**          | A delivery that was denied by policy evaluation before reaching the adapter.                          |
| **Transport Adapter**   | An adapter that moves data to/from a physical or logical transport layer (e.g., Meshtastic, LXMF).    |
| **Verification Status** | The trust level of an identity mapping: verified, manual, auto, or unverified.                        |
| **XDG**                 | XDG Base Directory Specification, used for MEDRE path resolution.                                     |
