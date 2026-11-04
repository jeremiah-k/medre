# Reproducible native qualification

Native qualifications preserve the distinction between a real SDK call, a
controlled protocol endpoint, a native provider observation, and a recipient
observation. They use existing core replay and ledger authority. No test record
or observation licenses another send.

## Current proof boundaries

| Scenario | Observer | Claim | CI policy |
| --- | --- | --- | --- |
| MeshCore core replay | Provider-owned deterministic TCP companion endpoint, independently reading real SDK command frames | Selected-channel protocol acceptance, exact ledger binding, sibling exclusion; guarded multipart uncertainty remains blocked across restart | Required MeshCore local-integration job; 12 executed cases and two positive records |
| LXMF core replay | Separate raw-SDK recipient process, also its loopback propagation node | One core-authorized replay observed by that local recipient after clean teardown | Opt-in native-qualification workflow; one executed case and one positive record |

MeshCore's `local_integration` marker and typed tier do not promote its modeled
endpoint to a physical companion: runtime evidence remains conformance. Channel
responses supply no native packet identity. Timeout/cancellation/restart coverage
does not prove non-submission; the checkpoint holds uncertainty. The guarded
multipart send owner disables internal retries, while the ordinary single-packet
session path retains its existing retry semantics outside this qualification.

The native-evidence and exact-target specifications define the authority and
mapping. This page describes how to reproduce and retain the test artifacts.

## Record contract

`scripts/ci/native_qualification.py` constructs and verifies the closed test-data
format in `docs/schemas/native-qualification.schema.json`. Records contain:

- Actual core Git revision and an explicit dirty-source flag.
- Manifest-authorized provider repository/revision, installed editable checkout,
  and actual versions of its declared SDK dependencies, including RNS for LXMF.
- Python/platform and GitHub execution ID/attempt when running in CI.
- Core and provider harness paths and SHA-256 digests of their actual bytes.
- Exact delivery identity, outbox/generation/receipt, native ID when one exists,
  claim scope/tier/result, evidence fingerprint, and current/exact ledger binding.
- Observer kind, witness count and digests; no raw command/message bodies.
- A digest over the canonical JSON statement excluding `record_sha256` itself.

Emission follows successful native assertions and teardown. Verification checks
the schema, digest, current source pair, SDK versions, harness bytes, observer
scope and execution identity. CI rejects dirty source, missing/duplicate records,
missing tests, skips and failed collection. SDK-less tests attack these guards
independently of the native scenarios.

These digests are integrity checks, not signatures or observer authentication.
Identifiers are selected from synthetic test traffic; this format is not a
secret scrubber. It excludes arbitrary error text and metadata, and it is not a
new production observation API. Existing fingerprints remain statements rather
than send permits.

## Focused local execution

Use a clean checkout of the provider at its compatibility-manifest SHA. Select
the native SDK explicitly; default core tests continue to deselect native tiers.
From the core checkout, for a MeshCore provider at `/path/to/meshcore`:

```bash
uv sync --locked --extra dev
uv run --no-sync medre dev sync --sdk meshcore --editable meshcore=/path/to/meshcore
MEDRE_MESHCORE_PROVIDER_ROOT=/path/to/meshcore uv run --no-sync python -m pytest \
  tests/native_qualification/test_meshcore_exact_target_replay.py \
  -m 'local_integration and meshcore_sdk' -q \
  --basetemp=/tmp/meshcore-proof --junitxml=/tmp/meshcore-proof-junit.xml
uv run --no-sync python scripts/ci/enforce_test_execution.py \
  --junit /tmp/meshcore-proof-junit.xml --expect-tests 12
uv run --no-sync python scripts/ci/native_qualification.py \
  '/tmp/meshcore-proof/test_core_replay_companion_acc[01]/qualification.json' \
  --provider /path/to/meshcore --expect-records 2
```

For local uncommitted development, `--allow-dirty` verifies integrity while
retaining the dirty flag. Such records cannot be promoted to clean CI evidence.
The provider source remains required to match its clean manifest revision.
LXMF uses the existing workflow and test, with `MEDRE_LXMF_PROVIDER_ROOT` available
for an explicit local provider checkout. It also needs the Matrix monitor
distribution; the opt-in workflow installs both manifest-selected providers.

## Retention and release review

Verified proof artifacts are retained for 90 days; failure diagnostics retain
their seven-day window. Verification precedes the success-only proof upload.
Raw recipient payloads, SDK profiles, SQLite state and radio identities are not
included. A failed or skipped job cannot produce verified qualification evidence.

CI retention is not permanent archival. Before relying on a run for a release,
retain its content-free records and associated selected verdicts with the
release evidence, record their digests and run URL, and verify against the named
source pair. Historical runs without this format remain historical observations;
do not backfill missing attempt identities or provenance by inference. Update the
gap register according to the exact observed boundary, without closing RF,
external-peer or uncertain-handoff recovery gaps from these local results.
