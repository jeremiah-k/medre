# Short Hardware Validation Iterations

Use this sequence to qualify a firmware or MEDRE change with two Meshtastic
radios, two MeshCore companions, two RNodes, and a local Docker Matrix server.
Each stage has a bounded observation window. Run the affected stage after a
fix, then broaden to the bridges and convergence case. Endurance runs are a
separate final check.

## Prepare the Bench

Install the locked environment with all extras needed for the campaign. Every
subsequent sync needs the same extras; uv removes extras omitted from a sync.

```bash
uv sync --locked --extra dev --extra meshtastic --extra meshcore \
  --extra lxmf --extra matrix-e2e
uv run --no-sync medre smoke --json
```

Use an owned private channel on each mesh. Assign device A to MEDRE and device
B to the independent native SDK peer. Set these endpoint variables in a private
shell file outside the checkout, then source it:

```bash
export MESHTASTIC_MEDRE_SERIAL_PORT="/dev/serial/by-id/<meshtastic-a>"
export MESHTASTIC_PEER_SERIAL_PORT="/dev/serial/by-id/<meshtastic-b>"
export MESHTASTIC_CONNECTION_TYPE=serial
export MESHTASTIC_SERIAL_PORT="$MESHTASTIC_MEDRE_SERIAL_PORT"
export MESHTASTIC_LIVE_SEND=1

export MESHCORE_PAIR=1
export MESHCORE_MEDRE_BLE_ADDRESS="<meshcore-a>"
export MESHCORE_PEER_BLE_ADDRESS="<meshcore-b>"
export MESHCORE_MEDRE_NODE_NAME="<actual-a-name>"
export MESHCORE_PEER_NODE_NAME="<actual-b-name>"

export LXMF_PAIR=1
export LXMF_MEDRE_RNS_CONFIG="/private/lab/rns-a"
export LXMF_PEER_RNS_CONFIG="/private/lab/rns-b"
export LXMF_MEDRE_IDENTITY="/private/lab/identities/a.identity"
export LXMF_PEER_IDENTITY="/private/lab/identities/b.identity"

export MEDRE_MC_BRIDGE=1
export MEDRE_LX_BRIDGE=1
export MEDRE_RADIO_MATRIX=1
```

Check these prerequisites before sending:

- Record the Git commit, dirty diff, Python and installed package versions,
  device model, firmware readback, connection type, and radio preset. A label
  such as “latest nightly” is insufficient to reproduce a result.
- Check matching frequency, bandwidth, spreading factor, coding rate, channel,
  and key fingerprints between peers. Keep raw keys and credentials private.
- Re-sync both MeshCore clocks through the companion SDK after a power cycle.
  The T-Beam RTC can lose its time, causing replay protection to reject traffic.
- Use the existing MeshCore BLE bonds. Each board accepts one central; release
  a stale owned connection before starting another client.
- Give each RNS process its own config directory and identity. Configure only
  its assigned RNode interface, with `enabled = yes`, `share_instance = No`,
  and `enable_transport = No`. A LAN or TCP fallback would invalidate RF proof.
- Reserve these config directories for testing. The pair fixture deletes their
  RNS storage caches to establish freshness; identities live separately.
- Give each physical serial port and BLE connection exactly one owner. Run
  suites sequentially. Do not run a native diagnostic client beside MEDRE on
  the same device.
- Verify Docker works. The bridge tests create an isolated Synapse instance
  with throwaway accounts; production Matrix credentials are unnecessary.

The radio-matrix attribution assertions currently use this bench's peer labels:
Meshtastic `d662`, MeshCore `MEDRE-MC-B`, and LXMF `lx-b-peer`. The MeshCore
node-name environment variables configure the pair suite, not those hardcoded
matrix expectations. On a different bench, adapt the matrix expectations to
the actual peer labels before claiming its rendering and provenance results.
Do not rename device owners merely to satisfy an assertion.

See the transport pages for [Meshtastic](meshtastic.md),
[MeshCore](meshcore.md), [LXMF](lxmf.md), and [Matrix](matrix.md) setup.

## Keep Evidence for Every Iteration

Create a new evidence directory for each invocation. Pytest can delete an
existing `--basetemp` directory, so never reuse a failed run's path.

```bash
export MEDRE_EVIDENCE_ROOT="/private/lab/evidence"
mkdir -p "$MEDRE_EVIDENCE_ROOT"

run_stage() {
  local stage="$1"
  shift
  local evidence_dir
  evidence_dir=$(mktemp -d "$MEDRE_EVIDENCE_ROOT/${stage}.XXXXXX") || return
  git rev-parse HEAD > "$evidence_dir/commit.txt"
  git diff HEAD > "$evidence_dir/working-tree.patch"
  git status --short > "$evidence_dir/working-tree-status.txt"
  uv pip list --format json > "$evidence_dir/packages.json"
  local result=0
  uv run --no-sync pytest "$@" -v --tb=short -rA \
    --basetemp="$evidence_dir/state" \
    --junitxml="$evidence_dir/results.xml" \
    > "$evidence_dir/pytest.log" 2>&1 || result=$?
  cat "$evidence_dir/pytest.log"
  printf 'Evidence: %s\n' "$evidence_dir"
  return "$result"
}
```

The evidence directory includes SQLite state and logs that can contain message
content and identifiers. Keep it private; publish a sanitized account of the
result rather than the directory itself. The convergence case additionally
writes `peer-observations.json` under its testcase directory before checking
its RF floor, retaining source acceptance and observed texts even on failure.

## Run From Pairs to Bridges

The following commands select the intended gated tier explicitly. A skipped
case is an unmet prerequisite, not successful hardware evidence. For an initial
fast connectivity check, set `MEDRE_LIVE_QUICK=1`; unset it for the full pair
cases below so boundaries, deduplication, and negative controls execute.

```bash
unset MEDRE_LIVE_QUICK
run_stage mt-pair tests/test_meshtastic_pair_live.py -m 'live and hardware'
run_stage mc-pair tests/test_meshcore_pair_live.py -m 'live and hardware'
run_stage lx-pair tests/test_lxmf_pair_live.py -m 'live and hardware'

run_stage radio-bridge tests/test_meshcore_meshtastic_bridge_live.py \
  -m 'live and hardware'
run_stage lx-bridges tests/test_lxmf_bridge_live.py -m 'live and hardware'

run_stage matrix-docker tests/integration/test_synapse_*.py -m docker

# A short paced volume iteration; increase the corpus only after this passes.
export MEDRE_MATRIX_TRAFFIC=12
run_stage radio-matrix tests/test_radio_message_matrix_live.py \
  -m 'live and hardware'
```

For a focused repeat, use `-k sustained_traffic_convergence` on the radio-matrix
module. The floor remains 90%; reducing it after a failure hides the evidence.
Retain a failing run, then compare it with a native-only control using the same
radio preset, payload shape, pacing, and fresh receiver window.

| Stage | Main assertions |
| --- | --- |
| Meshtastic pair | Native IDs, RF egress, Unicode/newlines, UTF-8 byte boundaries, distinct-message admission. |
| MeshCore pair | Clock-sensitive ingress, timestamp deduplication, wrong-key rejection and restored positive, peer egress, text boundaries. |
| LXMF pair | Native hash/source correlation, multi-frame payloads, distinct hashes for identical text, relation envelope reconstruction. |
| Radio bridge | Both directed routes, isolation, bounded echoes, scoped MeshCore stop/restart with Meshtastic remaining usable. |
| LXMF bridges | Four directed routes and fanout through real native peers. |
| Matrix Docker | Lifecycle, plaintext routing, encrypted text, crypto-store restart, attachment transfer and replay. |
| Radio matrix | Rendering, provenance, route isolation, paced convergence, and Docker Matrix relays through the three radio transports. |

After these pass, reverse the A/B endpoint assignments and rerun the relevant
pair cases to distinguish adapter behavior from a particular board. Change
both MeshCore names and both LXMF identity/config assignments with their roles.
Do not swap only one half of a configured endpoint.

## Distinguish Admission, Acceptance, and Delivery

Check three independent results for every routed nonce:

1. The correct canonical event is durably admitted, with native identity and
   deduplication matching the source protocol.
2. The receipt and outbox show the intended route and target. A `sent` receipt
   reports local SDK acceptance; it does not prove reception on the second radio.
3. The independent peer observes the expected nonce and wire text. Correlate its
   native message hash with the receipt where the transport supports that proof.

Do not infer RF delivery from local health. Health covers the local adapter
session; a powered but unreachable peer can coexist with a healthy adapter.
For a negative control, also require a fresh positive after restoration. An
old queued message arriving later cannot establish fresh recovery.

## Recovery and Longer Runs

Begin with short lifecycle cycles rather than an hours-long soak:

```bash
export MESHTASTIC_SOAK_CYCLES=5
run_stage mt-lifecycle tests/test_meshtastic_hardware_soak.py \
  -m 'live and hardware'
```

These cycles exercise local startup, health, and shutdown; they do not send RF
traffic or prove cable-loss recovery. The bridge module's scoped stop/restart
case similarly establishes runtime recovery, not unexpected physical loss.

For actual host-link disruption, record the connection state becoming offline,
its restoration, and a fresh payload received on the independent radio. Label
a serial proxy interruption as host-link loss: it leaves the radio powered and
does not prove radio reboot or power-loss recovery. RNS owns its RNode serial
reconnect; MEDRE local readiness and remote delivery remain separate evidence.

The LXMF power testcase is separately gated by `LXMF_PEER_HUB` and
`LXMF_PEER_HUB_PORT`. Enable it only after confirming that switching the mapped
port actually removes power from the owned peer radio. A hub VBUS status alone
does not establish that a battery-backed board is off. Without that verified
control, leave the power testcase skipped and still run the relation testcase.

Keep physical fault tests separate from ordinary functional stages. After a
fault, restore the connection or power, re-sync any lost clock, observe a fresh
delivery, and release all owned clients and temporary containers. Use a longer
soak only after the short functional and recovery iterations are understood.

## Turn Failures Into Focused Fixes

Preserve the failing evidence before making a change. Identify whether the
failure is admission, local acceptance, independent RF observation, teardown,
or a harness prerequisite. Reduce it to one route and one varying condition;
compare the direct SDK path when the adapter boundary is uncertain.

For a reproducible MEDRE defect, add a meaningful regression, make an atomic
fix with its specification and change fragment, rerun the affected stage, and
open a focused PR. Keep harness corrections and operational documentation in
their own concerns. Review fixes adversarially before publication. For CI,
locate the new run once and use `gh run watch --exit-status` to completion.

The [October 2026 campaign record](campaign-2026-10-03.md) records observed
results and unresolved limits from the first pass through this procedure.
