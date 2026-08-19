# Transport Realism Ladder

MEDRE is pre-release, but transport claims still require evidence at the layer
being claimed. Unit fakes are necessary and intentionally insufficient for
SDK, endpoint, hardware, and endurance claims.

## 1. Required ladder

Each transport SHOULD progress through the following layers in order. A later
layer does not remove the need for earlier deterministic coverage.

| Layer | Test label                              | Required boundary                                           | What it may claim                                            |
| ----- | --------------------------------------- | ----------------------------------------------------------- | ------------------------------------------------------------ |
| 1     | `fake_pipeline` / `wrapper_callback`    | MEDRE code + synthetic SDK boundary                         | MEDRE logic and callback wiring                              |
| 2     | `sdk_contract`                          | exact pinned optional SDK installed                         | SDK constructors, enums, callback and return-value contracts |
| 3     | `local_integration`                     | MEDRE + exact SDK + deterministic local endpoint/emulator   | SDK/session interaction without external service or hardware |
| 4     | `docker`, `live`, or `hardware`           | containerized service, external service, or physical device | behavior of the exercised endpoint class                     |
| 5     | `soak` overlay                          | repeated/extended execution at one of the layers above      | bounded endurance at that same evidence layer                |

`local_integration` is a test-layer label, not a runtime evidence tier. A local
integration using an in-process endpoint remains `conformance` evidence. A
container-backed local service is `docker` evidence. Only external services or
physical devices produce `live_service` or `hardware` evidence.

## 2. Scenario requirements

The following scenarios MUST have executable coverage at the strongest practical
non-live layer for each adapter. Live/hardware validation SHOULD repeat them when
an endpoint is available and safe to manipulate.

1. repeated start/stop;
2. startup failure after partial initialization;
3. connection loss during inbound traffic;
4. connection loss during outbound traffic;
5. restart with persisted transport/core state where the transport owns state;
6. malformed SDK callback payloads;
7. callback arriving after shutdown starts;
8. stalled send;
9. cancellation;
10. backpressure or serialization under concurrent sends;
11. duplicate native messages;
12. remote/local connection-state disagreement.

A scenario MAY be satisfied by a lower layer when a higher layer cannot safely
or deterministically produce the condition. The test or audit MUST state that
limitation instead of relabeling synthetic evidence as integration/live proof.

## 3. Current transport ladder

### Matrix

- Synthetic/wrapper: extensive.
- Installed SDK: pinned `mindroom-nio` contract job.
- Local integration: Docker Synapse, including E2EE SDK-boundary coverage.
- External live: harness exists; current-commit external evidence remains an
  explicit release gate.
- Soak: synthetic runtime soak exists; external-service soak is not yet current
  evidence.

### Meshtastic

- Synthetic/wrapper: extensive.
- Installed SDK: pinned `mtjk` contract matrix.
- Local integration: Docker `meshtasticd` proves lifecycle and outbound SDK
  handoff; simulation mode does not prove inbound pubsub relay.
- Hardware: opt-in MEDRE live harness is marked `hardware`; historical radio
  observations do not automatically become current-commit evidence.
- Soak: opt-in hardware lifecycle soak exists and is `NOT EXECUTED` until run
  against a physical radio at the current commit.

### MeshCore

- Synthetic/wrapper: extensive.
- Installed SDK: pinned `meshcore==2.3.8` contract matrix.
- Local integration: deterministic TCP companion-protocol endpoint drives the
  real SDK through MEDRE lifecycle, inbound, malformed-frame recovery, outbound
  disconnect/retry, reconnect, cancellation, and serialization paths.
- Hardware: opt-in live harness exists; previously recorded hardware runs are
  historical unless rerun at the current commit.
- Soak: deterministic local-integration repeated lifecycle/send test exists;
  hardware soak remains an operator-run activity.

### LXMF

- Synthetic/wrapper: extensive.
- Installed SDK: pinned `LXMF==1.1.1` / `RNS==1.4.2` contract matrix.
- Local integration: process-isolated real Reticulum/LXMRouter lifecycle proves
  repeated startup/shutdown, identity persistence, malformed/late callback
  containment, and partial-start cleanup.
- External live: opt-in Reticulum harness exists; multi-hop/remote receipt
  requires an external peer and remains a release gate.
- Soak: process-isolated repeated real-router lifecycle is available as a manual
  local-integration soak.

## 4. CI policy

The default unit suite MUST exclude `local_integration`, `soak`, `live`,
`hardware`, Docker, and installed-SDK markers. Dedicated SDK-contract jobs MUST
run exact pins. Deterministic local-integration jobs SHOULD run on pull requests
for transports that can provide a bounded local endpoint. Soak jobs MAY be
manual when their duration would materially slow ordinary CI.

Hardware/live tests MUST be opt-in and MUST never transmit merely because the
normal test suite is invoked.
