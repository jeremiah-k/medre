#!/usr/bin/env bash

set -euo pipefail

# =============================================================================
# MEDRE Docker Bridge Artifact Collector
# =============================================================================
#
# Opt-in script that runs Docker bridge integration tests for a given scenario
# and collects artifacts (logs, config snapshots, summary.json) into a
# timestamped directory under .ci-artifacts/docker-bridge-runs/.
#
# This script does NOT run by default in CI.  It is invoked explicitly:
#
#   ./scripts/ci/run-docker-bridge-artifacts.sh [scenario]
#
# Scenarios:
#   matrix_to_meshtastic   — Matrix inbound, Meshtastic outbound (default)
#   meshtastic_to_matrix   — Meshtastic inbound, Matrix outbound
#   bidirectional          — Both directions
#
# Environment variables (same as run-docker-integration.sh):
#   MEDRE_SYNAPSE_IMAGE        — Synapse Docker image
#   MEDRE_MESHTASTICD_IMAGE    — meshtasticd Docker image
#   MEDRE_SYNAPSE_PORT         — Synapse port (default: 8008)
#   MEDRE_MESHTASTICD_PORT     — meshtasticd port (default: 4403)
#   MEDRE_MESHTASTICD_HWID     — meshtasticd hardware ID (default: 11)
#   MEDRE_DOCKER_READY_TIMEOUT — seconds to wait per service (default: 120)
#   MEDRE_CI_ARTIFACT_DIR      — artifact base directory
#
# Artifacts are written to:
#   .ci-artifacts/docker-bridge-runs/<ISO-timestamp>/
#     summary.json         — structured evidence summary (always written)
#     pytest-stdout.log    — pytest stdout capture
#     pytest-stderr.log    — pytest stderr capture
#
# summary.json is always written, even on failure, with status "failed" or
# "partial" and populated limitations.
#
# IMPORTANT: This proves Docker SDK-boundary validation only.  No real external
# Matrix account, real radio, or live network behavior is claimed.  See
# docs/runbooks/docker-bridge-artifacts.md for full honesty requirements.
# =============================================================================

PYTHON="${PYTHON:-python}"
SCENARIO="${1:-matrix_to_meshtastic}"
TIMEOUT_MINUTES="${TIMEOUT_MINUTES:-15}"

echo "MEDRE Docker Bridge Artifact Collector"
echo "========================================"
echo "Scenario: ${SCENARIO}"
echo "Timeout:  ${TIMEOUT_MINUTES}m"
echo ""

# Validate scenario.
case "${SCENARIO}" in
    matrix_to_meshtastic|meshtastic_to_matrix|bidirectional)
        ;;
    *)
        echo "ERROR: Unknown scenario '${SCENARIO}'." >&2
        echo "Valid: matrix_to_meshtastic, meshtastic_to_matrix, bidirectional" >&2
        exit 1
        ;;
esac

# Verify Docker is available.
if ! command -v docker >/dev/null 2>&1; then
    echo "ERROR: docker is not installed or not in PATH." >&2
    exit 1
fi

if ! docker info >/dev/null 2>&1; then
    echo "ERROR: Docker daemon is not running." >&2
    exit 1
fi

# Verify Python is available.
if ! command -v "${PYTHON}" >/dev/null 2>&1; then
    echo "ERROR: Python runtime '${PYTHON}' is required." >&2
    exit 1
fi

# Ensure integration extras are installed.
echo "Checking MEDRE installation..."
INSTALLED=$("${PYTHON}" -c "import medre; print('ok')" 2>/dev/null || true)
if [[ "${INSTALLED}" != "ok" ]]; then
    echo "Installing MEDRE with matrix + meshtastic extras..."
    "${PYTHON}" -m pip install -e ".[matrix,meshtastic]" --quiet
fi

echo ""
echo "Running Docker bridge artifact collection..."
echo ""

# Run via the Python helper, which handles everything.
set +e
timeout --foreground "${TIMEOUT_MINUTES}m" \
    "${PYTHON}" -c "
import sys
import json
from medre.runtime.docker_bridge_artifacts import collect_docker_bridge_artifacts

summary = collect_docker_bridge_artifacts(
    scenario='${SCENARIO}',
    timeout_minutes=${TIMEOUT_MINUTES},
)

# Print summary to stdout.
print()
print('=== Docker Bridge Artifact Summary ===')
print(json.dumps(summary, indent=2, sort_keys=True, default=str))
print()
print(f'Run directory: {summary[\"run_directory\"]}')
print(f'Status: {summary[\"status\"]}')
print(f'Scenario: {summary[\"scenario\"]}')

sys.exit(0 if summary['status'] == 'passed' else 1)
"
EXIT_CODE=$?
set -e

echo ""
if [[ ${EXIT_CODE} -eq 0 ]]; then
    echo "Docker bridge artifact collection completed: PASSED"
else
    echo "Docker bridge artifact collection completed: FAILED/PARTIAL (exit ${EXIT_CODE})"
    echo "Check summary.json in the run directory for details."
fi

exit ${EXIT_CODE}
