# Transport Realism Audit

This audit maps failure-oriented transport scenarios to the strongest MEDRE test
layer currently available. It is an implementation crosswalk for
`docs/spec/appendices/transport-realism.md`, not a source of runtime semantics.

## Evidence ladder crosswalk

| Transport  | Fake/wrapper                     | Exact SDK                                   | Local integration                                | External/hardware                      | Soak                                              |
| ---------- | -------------------------------- | ------------------------------------------- | ------------------------------------------------ | -------------------------------------- | ------------------------------------------------- |
| Matrix     | extensive adapter/session suites | `test_matrix_sync_recovery_sdk_contract.py` | Docker Synapse integration                       | opt-in live harness                    | synthetic runtime soak; external soak pending     |
| Meshtastic | extensive adapter/session suites | `test_meshtastic_sdk_contract.py`           | Docker `meshtasticd` lifecycle/outbound          | `test_meshtastic_live.py` (`hardware`) | `test_meshtastic_hardware_soak.py` (opt-in)       |
| MeshCore   | extensive adapter/session suites | `test_meshcore_pinned_sdk_contract.py`      | `integration/test_meshcore_local_integration.py` | `test_meshcore_live.py`                | local-integration soak in same integration module |
| LXMF       | extensive adapter/session suites | `test_lxmf_sdk_contract.py`                 | `integration/test_lxmf_local_integration.py`     | `test_lxmf_live.py`                    | process-isolated local-integration soak           |

## Failure-scenario coverage

The goal is not to force every failure through hardware. The strongest bounded,
deterministic layer is preferred for destructive or timing-sensitive failures.

| Scenario                     | Matrix                          | Meshtastic                 | MeshCore                                                        | LXMF                                                                                                  |
| ---------------------------- | ------------------------------- | -------------------------- | --------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------- |
| repeated start/stop          | live + unit                     | live harness + unit        | local integration                                               | local integration                                                                                     |
| partial startup failure      | unit/provider integration       | unit                       | local integration APPSTART error                                | local integration invalid router storage                                                              |
| loss during inbound          | sync/reconnect unit             | generation-race unit       | local TCP disconnect after inbound                              | callback/teardown unit; external peer needed for link-loss realism                                    |
| loss during outbound         | send/reconnect unit             | queue/reconnect unit       | local TCP drop during real SDK send + MEDRE reconnect/retry     | bounded retry unit; remote link-loss needs external peer                                              |
| restart with persisted state | durable Matrix checkpoint tests | storage/native-ref tests   | SDK state is node-owned; MEDRE restarts cleanly                 | local integration reuses persisted identity/router storage                                            |
| malformed SDK callback       | codec/session tests             | callback/classifier tests  | local oversized-frame resynchronization through real SDK reader | local integration malformed callback containment                                                      |
| callback after shutdown      | session recovery tests          | stale-generation tests     | lifecycle tests                                                 | local integration late callback containment                                                           |
| stalled send                 | bounded SDK/runtime tests       | queue timeout/retry tests  | real SDK stalled command cancellation                           | synchronous LXMRouter handoff cannot be safely pre-empted; unit cancellation covers pre-handoff waits |
| cancellation                 | explicit async tests            | queue/session tests        | local real-SDK stalled-send cancellation                        | explicit send/announce cancellation tests                                                             |
| backpressure                 | delivery/outbox tests           | bounded queue tests        | local real-SDK send-lock serialization                          | send-lock unit tests                                                                                  |
| duplicate native message     | durable admission/dedup         | adapter dedup              | adapter dedup                                                   | adapter dedup                                                                                         |
| remote/local disagreement    | sync/token ownership            | SDK `isConnected` fallback | disconnect/health tests                                         | router/session diagnostic tests                                                                       |

## New local endpoints

### MeshCore

`tests/helpers/meshcore_local_node.py` implements only the companion-protocol
subset MEDRE consumes: APPSTART/SELF_INFO, GET_MSG/NO_MORE_MSGS, direct send
MSG_SENT, channel-send OK, channel-message injection, malformed-frame
resynchronization, and TCP disconnect during inbound or outbound activity. The
real pinned `meshcore` SDK owns framing, command waiting, event dispatch,
subscription, and reconnect notification. The helper is deliberately not a
full firmware simulator.

### LXMF

Reticulum and LXMRouter are process-global/threaded enough that local integration
is isolated in a subprocess. The probe uses the real pinned RNS/LXMF packages,
a persisted generated identity, temporary router storage, and MEDRE's real
session lifecycle. Isolation prevents one test's Reticulum singleton and signal
handlers from becoming another test's hidden prerequisite.

## Remaining realism gaps

- Matrix: external homeserver/federation soak and live multi-room concurrency.
- Meshtastic: current-commit physical-radio lifecycle/reconnect/send/receive
  evidence; Docker simulation still cannot prove inbound radio pubsub behavior.
- MeshCore: current-commit physical-node TCP/serial/BLE evidence and RF receipt.
- LXMF: remote-peer delivery-state progression, link loss during real outbound,
  and multi-hop observation.

These gaps remain explicit release gates. Do not relabel a passing local
integration test as external or hardware validation.
