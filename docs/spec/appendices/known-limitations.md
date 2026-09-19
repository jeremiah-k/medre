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

## 1. Meshtastic → Matrix Automated Inbound Bridge

The automated Meshtastic-to-Matrix inbound bridge test class is
**permanently skipped**. Reliability is not yet sufficient for CI.

- **Source test:**
  `tests/test_live_matrix_meshtastic_bridge.py::TestMeshtasticToMatrix`
  (`@pytest.mark.skip(reason="Meshtastic → Matrix automated inbound not yet reliable")`).
- **Operator action today:** manual testing via the operator runbook in
  `docs/ops/diagnostics-and-evidence.md`.
- **Direction:** The live matrix/meshtastic bridge module exists
  (`tests/operational/test_matrix_meshtastic_relations.py` and friends)
  but the inbound bridge is not currently exercised in CI.

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
(`LxmfDeliveryState` in `src/medre/adapters/lxmf/session.py`) has no
paper-specific entry.

- **Source:** `tests/test_lxmf_state_mapping.py` and
  `tests/test_lxmf_session.py` assert that any LXMF delivery state value
  the runtime does not recognise maps to `LxmfDeliveryState.UNMAPPED`.
- **Current behaviour:** If LXMF reports a state value outside the
  eight-name map (`GENERATING`, `OUTBOUND`, `SENDING`, `SENT`,
  `DELIVERED`, `FAILED`, `REJECTED`, `CANCELLED`), the state mapping
  returns `UNMAPPED`. Paper-mode state reports fall in this gap by
  design — paper-mode is a delivery method, not a delivery state.
- **Direction:** When paper-mode delivery adds its own state reporting,
  the state map must be extended. Until then, paper-mode delivery is
  distinguished only at the method level (see
  `_map_delivery_method()`).

## 4. MMRelay Interop Caveats

The Matrix adapter emits an mmrelay-compatibility metadata block under
`native.interop.mmrelay` to preserve wire compatibility with
mmrelay/MeshCore bridges. The current shape deliberately differs from
upstream mmrelay in four known ways:

1. **`mmrelay_suppress` flag is not honoured.** MEDRE does not consume
   the upstream `mmrelay_suppress` flag. Operators that rely on it to
   suppress mmrelay-emitted messages must configure MEDRE directly.

2. **Per-index template variables (`{displayN}`, `{longN}`, `{meshN}`)
   are unsupported.** MEDRE only renders generic variables such as
   `{sender}` and `{sender_short}`. Per-index aliases that mmrelay
   consumers may emit are intentionally not interpolated. See
   `src/medre/adapters/matrix/renderer.py` for the supported template
   vocabulary.

3. **Reaction body byte-format differences vs upstream MMR.** MEDRE
   emits the mmrelay reaction body with the local encoding. The
   byte-for-byte reaction payload may differ from upstream mmrelay;
   consumers must accept the MEDRE shape rather than expect identical
   bytes.

4. **Hardcoded `TEXT_MESSAGE_APP` portnum in mmrelay-compat Matrix
   metadata.** The Matrix adapter embeds a hardcoded
   `"TEXT_MESSAGE_APP"` port number in the mmrelay-compatibility
   metadata. See `src/medre/adapters/matrix/renderer.py` (`embed_*`)
   for the injection site. Multi-portnum or non-text packet types are
   not currently exposed through this metadata block.

- **Source tests:** `tests/test_matrix_reaction_mmrelay.py` and the
  `tests/conformance/fixtures/meshtastic/*.json` fixtures, which
  declare `"portnum": "TEXT_MESSAGE_APP"`.
- **Direction:** When the upstream mmrelay contract changes, MEDRE's
  compatibility block must be reviewed alongside the live mmrelay
  reference at `docs/dev/mmrelay-behavior-reference.md`.

## 5. Diagnostics Granularity Gaps

### 5.1 LXMF health is local-scope only

`LxmfAdapter.health_check()` reports local session/router liveness —
`healthy` only while the adapter is started and the owned session
reports its router running and connected, `failed` when that local
session is missing or torn down, `unknown` when not started. It is not
a peer-reachability claim: the pinned LXMF/RNS SDKs expose no supported
peer-liveness API, and MEDRE never probes merely to answer health, so
`diagnostics()` reports `peer_reachability="unknown"` explicitly and
separately (`health_scope="local_session_and_router"`). Reticulum also
exposes no transport-down event, so a silently lost transport is first
noticed on the next outbound send. Operators MUST NOT read `healthy`
as peer reachability.

- **Source:** `src/medre/adapters/lxmf/adapter.py::health_check`;
  `tests/test_lxmf_health_lifecycle.py`.

### 5.2 MeshCore and Matrix expose no outbound queue evidence

The outbound-queue diagnostic keys (`queue_pending`, `queue_total_sent`,
`queue_total_failed`) exist only on the Meshtastic adapter. MeshCore and
Matrix diagnostics have no equivalent queue-depth surface.

- **Source:** `src/medre/adapters/meshtastic/adapter.py` (keys present);
  Matrix/MeshCore adapter diagnostics (keys absent).

## 6. LXMF Envelope Relation Fidelity

Inbound LXMF relations are reconstructed at decode time from the `0xFD`
(`FIELD_CUSTOM_META`) management-envelope fields via
`_reconstruct_relations`. A reproducible, fully local two-instance
regression harness now covers relation preservation across a real
pinned-SDK hop: instance A renders a relation-bearing event through
`LxmfRenderer` and its real `LxmfAdapter`, and distinct process B
decodes through the real inbound adapter/codec path over a loopback
Reticulum `UDPInterface` pair
(`tests/integration/test_lxmf_local_integration.py::test_relation_preserved_across_two_local_instances`).
That harness passed on the declared pinned SDKs in a locked disposable venv
at the 2026-09-18 gate: 3/3 local-integration tests were green, and
`PYTHONPATH=src python -m tests.helpers.lxmf_local_probe relation <tmpdir>`
exited 0 with every verdict true and the runtime-resolved SDK versions recorded
in the result payload. The passing run is still
local-SDK loopback evidence only: external interoperability (real
multi-hop meshes, other MEDRE deployments, live hardware) remains
unproven, and no live-network or hardware bridge proof is claimed from
a local SDK relation test.

- **Source:** `src/medre/adapters/lxmf/codec.py`;
  `tests/integration/test_lxmf_local_integration.py`.

## See also

- [`transport-limitations.md`](transport-limitations.md) — transport-specific
  limitations.
- [`transport-realism.md`](transport-realism.md) — required evidence
  ladder and current transport coverage.
- `tests/test_pipeline_conformance.py`, `tests/test_lxmf_state_mapping.py`,
  `tests/test_live_matrix_meshtastic_bridge.py`,
  `tests/test_matrix_reaction_mmrelay.py` — the test modules that record
  the limitations above.
