# Known Limitations

Limitations surfaced from test annotations, conformance `xfail`s, and
`skip` markers that would otherwise be invisible to operators. Each entry
points at the test file that records it and explains the current
behaviour.

> **Status.** This appendix is a living inventory. Entries here are honest
> limitations — not bugs awaiting fix, and not deprecated behaviour. When
> an entry changes status (because the underlying code path ships, or
> because the limitation is replaced by a different constraint), update
> this appendix in the same change.

## Evidence provenance and scope

Adapter-provenance claims in this index are checked against the **seven
transport pins recorded in `.github/adapter-compatibility.json`** — that
manifest is the pin authority — plus the MEDRE core revision that carries this
appendix. These are repository-relative paths, not files in the MEDRE core
checkout:

| Repository                  | Inspected commit                           |
| --------------------------- | ------------------------------------------ |
| `medre`                     | `094deabe60864f10ebb4f0fc9cf0cf9e995b7e02` |
| `medre-adapter-matrix`      | `4579d350ae9b70fb8562b3339f5c6e475170acd7` |
| `medre-adapter-meshtastic`  | `150774f04d8f4741ea01f6551da40badcd758b04` |
| `medre-adapter-meshcore`    | `040cbfe39eead9bdc18a784abb7eae07706034cd` |
| `medre-adapter-lxmf`        | `438a1497849977fb13218199c6a57f5b99afb3db` |
| `medre-adapter-xmpp`        | `64a6bf934d32fbd53759d26c8c7cfa5bddb28d83` |
| `medre-adapter-chatmail`    | `4d98eb9fbd58669a462d83dca022a95a74c4afc6` |
| `medre-adapter-simplexchat` | `b60e55ea08ad09d7c148493353ae51efbbe3bcff` |

Native SDK pins are declared by each provider's own package metadata at the
inspected commit (`mindroom-nio[e2e]`, `mtjk`, `meshcore`, `lxmf` with `rns`,
`slixmpp`, `deltachat-rpc-client` with `deltachat-rpc-server`,
`simplex-chat`); that metadata is the version authority, so claims below name
the SDK package rather than repeat its pin.

Core-owned test references use `tests/...`; provider-owned references name the
repository explicitly. A **test path records where a claim is exercised**, not
proof it passed on these commits. Historical pass counts and hardware
observations below are dated evidence, _not_ an automatic current-tree native
qualification. Re-run the relevant opt-in tiers with the exact provider pins
and record environment, SDK revisions, and raw evidence for a new claim. Where
a claim cannot be verified from the pinned provider repositories alone, the
entry says so explicitly.

## 1. Meshtastic → Matrix Live Inbound Evidence

The Meshtastic-to-Matrix inbound path has an opt-in hardware harness, but it is
not a default CI gate and current release readiness does not record physical-radio
execution evidence for the current tree.

- **Source test:**
  `tests/test_live_matrix_radio_bridge.py::test_radio_to_matrix_three_legs_relayed_encrypted`
  exercises a real Meshtastic peer through the production runtime and verifies
  canonical admission plus Matrix delivery evidence when the required hardware
  and credentials are available.
- **Default CI:** the hardware-marked harness is skipped unless its explicit live
  prerequisites are configured. Synthetic and Docker/local-integration coverage
  do not promote this row to physical live validation.
- **Operator action today:** use the live/hardware harness and the procedures in
  `docs/ops/diagnostics-and-evidence.md` when recording physical-radio evidence.

## 2. Delivery-Stage Policy Reserved Extension Point

The pipeline conforms to a model where delivery-stage policy is reserved
for future implementation. There is no current code path that evaluates
policy inside the `DELIVER` phase.

- **Source test:**
  `tests/test_pipeline_conformance.py`, test
  `TestPolicyEvaluation::test_delivery_policy_suppresses_delivery`
  (`@pytest.mark.xfail` records that delivery-stage policy remains a reserved
  extension point with zero current implementation).
- **Spec anchor:** `docs/spec/conformance.md` §3.2 item 6 explicitly
  states that delivery-stage policy is a reserved extension point with
  zero current implementation.
- **Direction:** Until implemented, delivery suppression paths rely on
  upstream stages (route policy, capability decisions) and the
  `capability_suppressed` outcome.

## 3. LXMF Paper-Mode State Reports `UNMAPPED` by Design

LXMF defines a `PAPER` delivery **method** alongside `DIRECT`,
`OPPORTUNISTIC`, and `PROPAGATED`. The delivery **state** vocabulary
(`LxmfDeliveryState` in the LXMF provider’s
`medre-adapter-lxmf/src/medre/adapters/lxmf/session.py`) has no
paper-specific entry.

- **Source:** `medre-adapter-lxmf/tests/test_lxmf_state_mapping.py` and
  `medre-adapter-lxmf/tests/test_lxmf_session.py` assert that any LXMF delivery state value
  the runtime does not recognise maps to `LxmfDeliveryState.UNMAPPED`.
- **Current behaviour:** If LXMF reports a state value outside the
  eight-name map (`GENERATING`, `OUTBOUND`, `SENDING`, `SENT`,
  `DELIVERED`, `FAILED`, `REJECTED`, `CANCELLED`), the state mapping
  returns `UNMAPPED`. Paper-mode state reports fall in this gap by
  design: the pinned SDK's `LXMessage.PAPER` (`0x05`) is a delivery
  METHOD constant that `__mark_paper_generated` assigns to `state`, and
  because it is not in `LXMessage.states` the provider deliberately maps
  it to `UNMAPPED` (see the state-map note in
  `medre-adapter-lxmf/src/medre/adapters/lxmf/session.py`). Paper mode
  is a config-selectable default delivery method in the provider's
  configuration model; it is not a delivery state.
- **Direction:** When paper-mode delivery adds its own state reporting,
  the state map must be extended. Until then, paper-mode delivery is
  distinguished only at the method level (see
  `_map_delivery_method()`).

## 4. MMRelay Compatibility Surface Removed

The Matrix adapter's opt-in MMRelay presentation-compatibility path is
**removed** at the pinned Matrix adapter revision (commit `bdee94e`, an
ancestor of the inspected pin): the renderer no longer reads
`mmrelay_compatibility`, emits flat `meshtastic_*` wire keys, or consults
the `native.interop.mmrelay` metadata fallback, and the retired core
`medre.interop` module is deleted. The frozen 0.1.0 native contract never
included that namespace. Flat MMRelay-style content keys that still arrive
in Matrix room events are untrusted input: they are ignored when
normalizing authored text, sender metadata, and reactions, and they never
override the message body or invent reactions.

- **Source tests:** `tests/test_matrix_reaction_mmrelay.py` (flat content
  hints do not override the body or invent reactions) and
  `tests/test_mmrelay_isolation.py` (AST ban on importing the retired
  `medre.interop` namespace; Matrix `displayname` trust boundary).
- **Direction:** There is no MMRelay presentation-compatibility surface to
  configure. MEDRE 0.1.0 does not claim MMRelay 1.5 parity for shared-room
  relays; peer relays exchange the canonical `content.medre` envelope. Any
  future compatibility surface would be a new reviewed contract, not a
  revival of the deleted renderer path.

## 5. Diagnostics Granularity Gaps

### 5.1 LXMF health is local-scope only

`LxmfAdapter.health_check()` reports local session/router liveness —
`healthy` only while the adapter is started and the owned session
reports its router running and connected, `degraded` while a worker
status probe is delayed (recovered native status returns it to
`healthy`), `failed` when that local session is missing or torn down,
`unknown` when not started. It is not a peer-reachability claim: the
pinned LXMF/RNS SDKs expose no supported peer-liveness API, and MEDRE
never probes merely to answer health, so `diagnostics()` reports
`peer_reachability="unknown"` explicitly and separately
(`health_scope="local_session_and_router"`). Reticulum also exposes no
transport-down event, so a silently lost transport is first noticed on
the next outbound send. Operators MUST NOT read `healthy` as peer
reachability.

- **Source:** `medre-adapter-lxmf/src/medre/adapters/lxmf/adapter.py::health_check`
  and `docs/spec/native-worker.md` ("Native health notifications") at the
  inspected pin; `medre-adapter-lxmf/tests/test_lxmf_health_lifecycle.py`.

### 5.2 Only Meshtastic exposes outbound queue evidence

The outbound-queue diagnostic keys — `queue_pending` plus the
`queue_total_*` family (`sent`, `failed`, `enqueued`, `dequeued`,
`rejected`, `requeued`, `exhausted`, `permanent_failed`) and the
`queue_pressure_state`/`queue_utilization_pct` health fields — exist
only on the Meshtastic adapter. Matrix, MeshCore, XMPP, Chatmail, and
SimpleX Chat diagnostics expose no equivalent queue-depth surface at
their pins (Chatmail and SimpleX Chat do keep bounded native/local
queues; their diagnostics report other operational counters).

- **Source:** `medre-adapter-meshtastic/src/medre/adapters/meshtastic/adapter.py`
  (keys present); `medre-adapter-matrix/src/medre/adapters/matrix/adapter.py`,
  `medre-adapter-meshcore/src/medre/adapters/meshcore/adapter.py`,
  `medre-adapter-xmpp/src/medre/adapters/xmpp/adapter.py`,
  `medre-adapter-chatmail/src/medre/adapters/chatmail/adapter.py`, and
  `medre-adapter-simplexchat/src/medre/adapters/simplexchat/adapter.py`
  (the corresponding diagnostics do not expose those queue keys).

## 6. LXMF Envelope Relation Fidelity

Inbound LXMF relations are reconstructed at decode time from the `0xFD`
(`FIELD_CUSTOM_META`) management-envelope fields via
`_reconstruct_relations`. A reproducible, fully local two-instance
regression harness covers relation preservation across a real
pinned-SDK hop: instance A renders a relation-bearing event through
`LxmfRenderer` and its real `LxmfAdapter`, and distinct process B
decodes through the real inbound adapter/codec path over a loopback
Reticulum `UDPInterface` pair
(`medre-adapter-lxmf/tests/test_lxmf_local_integration.py::test_relation_preserved_across_two_local_instances`,
marked `local_integration` + `lxmf_sdk` at the inspected pin). That
harness is part of the provider's local-integration tier, which core's
required local-integration job runs against the pinned adapter
revision (`medre-adapter-lxmf` README at the inspected pin; the
compatibility manifest marks LXMF `local_integration: true`). A dated
pass exists from the 2026-09-18 gate: 3/3 local-integration tests were
green in a locked disposable venv on the then-declared pinned SDKs, and
the provider-owned
`medre-adapter-lxmf/tests/medre_lxmf_tests/helpers/lxmf_local_probe.py`
probe exited 0 with every verdict true and the runtime-resolved SDK
versions recorded in the result payload; that run remains dated
evidence, not a current-tree proof. Any passing run is still
local-SDK loopback evidence only: external interoperability (real
multi-hop meshes, other MEDRE deployments, live hardware) remains
unproven, and no live-network or hardware bridge proof is claimed from
a local SDK relation test.

- **Source:** `medre-adapter-lxmf/src/medre/adapters/lxmf/codec.py`;
  `medre-adapter-lxmf/tests/test_lxmf_local_integration.py`.

## 7. Encrypted-Transport Live-Tier xfails

The opt-in Synapse and live Matrix integration tiers record known runtime
limitations as `pytest.xfail` guards instead of hiding them:

- `tests/integration/test_synapse_e2ee_smoke.py:638` — the client-side
  Megolm-encrypted message was sent but the bot did not decrypt it within
  the 30 s timeout (outbound session creation / inbound key sharing did not
  complete in time); the encrypted room, crypto store, and diagnostics are
  all valid.
- `tests/integration/test_synapse_e2ee_smoke.py:649` — the second nio
  client could not be initialised or `room_send` failed, so the run fell
  back to a plaintext send and cannot prove Megolm decryption.
- `tests/integration/test_synapse_attachment_smoke.py:826` — the E2EE test
  client could not join the encrypted room, so the encrypted-attachment
  path is not exercised on that run.
- `tests/integration/test_synapse_attachment_smoke.py:1012` — the receiving
  client refused the unverified bot device; the ciphertext shape was observed,
  but neither receiving-client event nor attachment decryption was proved.
- `tests/integration/test_synapse_attachment_smoke.py:1035` — Megolm key exchange
  did not complete within the grace period; ciphertext-shape delivery was
  observed, but receiving-client decryption remained unproven.
- `medre-adapter-matrix/tests/test_matrix_live.py:962` — no inbound message
  from a third-party sender arrived within 30 s (missing
  `MATRIX_INBOUND_SENDER` prerequisite or missed manual send window), so
  inbound relay evidence stays inconclusive for that run.

## See also

- [`transport-limitations.md`](transport-limitations.md) — transport-specific
  limitations.
- [`transport-realism.md`](transport-realism.md) — required evidence
  ladder and current transport coverage.
- `tests/test_pipeline_conformance.py`,
  `tests/test_live_matrix_radio_bridge.py`, and
  `tests/test_matrix_reaction_mmrelay.py` (MEDRE core), and
  `medre-adapter-lxmf/tests/test_lxmf_state_mapping.py` (LXMF provider)
  — the test modules recording the limitations above.
