# Required Docker-tier gate for routed Matrix encrypted outbound

MEDRE can now prove, as a required and self-refreshing CI qualification, that
an encrypted message it sends is decrypted and observed by an independent
second client. The new core-owned module
`tests/integration/test_synapse_e2ee_outbound_acceptance.py` extends the
existing Docker Synapse E2EE harness: it routes a canonical `message.created`
event through durable admission, the durable outbox and the retry worker to
the real MatrixAdapter Megolm send path, and requires a dedicated peer
account — its own Matrix account, a fresh server device per test, and its own
nio crypto store — to decrypt the exact plaintext under the native event id
bound to the durable outbox generation, the attempt receipt with
`remote_service` confirmation, and the outbound native reference. The proof
asserts the full chain with no xfail or plaintext fallback path. The
module has a prerequisite skip marker for local runs without E2EE support;
required CI rejects a skipped or missing case.

Negative cases in the same module prove the verdict cannot be reached
dishonestly: a recipient without the room key, an observer in a different
encrypted room, a wrong native event id, and a stale (foreign-event) attempt
each refuse with named reasons, and opt-in strict verification refuses an
unverified complete recipient view before any native send. Adapter restart on
the same key store is separately asserted to keep the device identity, keep
delivering, and keep the earlier generation readable. The required proof also
carries a tripwire asserting the default quiet-permissive policy performs no
peer-device trust scan.

In `.github/workflows/docker-integration.yml`, the module runs explicitly at
the pinned Synapse image and adapter revision with a junit report, and
`scripts/ci/enforce_test_execution.py` fails the job when any case is
skipped, errors, fails, or when fewer tests executed than the module defines;
the whole-directory Docker lane deselects the module to avoid double
execution. Inbound decrypt convergence remains the separate, honest-xfail
property of `tests/integration/test_synapse_e2ee_smoke.py` and is unchanged.

G05 registers now record the encrypted Matrix outbound observer as a
current-pin Docker-tier execution: `docs/dev/contract-gap-status.md`,
`docs/dev/native-evidence-campaign.md`, `docs/dev/native-evidence-index.json`,
and `docs/spec/appendices/transport-limitations.md`. The tier claims no
federation, external network, cross-signing ceremony, or recipient-side user
action; strict peer-device verification stays opt-in and the default stays
quiet permissive.
