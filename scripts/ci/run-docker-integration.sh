#!/usr/bin/env bash

set -euo pipefail

# =============================================================================
# MEDRE Docker Integration Test Runner
# =============================================================================
#
# Runs the MEDRE Docker integration test suite.  This script is the CI
# entry point called from .github/workflows/docker-integration.yml and can
# also be used for local runs.
#
# Usage:
#   ./scripts/ci/run-docker-integration.sh
#
# Environment variables:
#   MEDRE_SYNAPSE_IMAGE       — Synapse Docker image (default: matrixdotorg/synapse:v1.155.0)
#   MEDRE_MESHTASTICD_IMAGE   — meshtasticd Docker image (default: meshtastic/meshtasticd:2.7.15)
#   MEDRE_SYNAPSE_PORT        — Synapse port (default: 8008)
#   MEDRE_MESHTASTICD_PORT    — meshtasticd port (default: 4403)
#   MEDRE_DOCKER_READY_TIMEOUT — seconds to wait per service (default: 120)
#   MEDRE_CI_ARTIFACT_DIR     — artifact directory (default: .ci-artifacts/docker-integration)
# =============================================================================

PYTHON="${PYTHON:-python}"
TIMEOUT_MINUTES="${TIMEOUT_MINUTES:-13}"

echo "MEDRE Docker Integration Tests"
echo "================================"
echo ""

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
if [[ ${INSTALLED} != "ok" ]]; then
	echo "Installing MEDRE with matrix + meshtastic extras..."
	"${PYTHON}" -m pip install -e ".[matrix,meshtastic]" --quiet
fi

echo ""
echo "Running integration tests (timeout: ${TIMEOUT_MINUTES}m)..."
echo ""

# Run the docker-marked tests via pytest.
# The conftest.py handles Docker container lifecycle.
set +e
timeout --foreground "${TIMEOUT_MINUTES}m" \
	"${PYTHON}" -m pytest \
	tests/integration/ \
	-m docker \
	-v \
	--tb=short \
	--timeout=300 \
	"${PYTEST_EXTRA_ARGS-}"
TEST_EXIT=$?
set -e

echo ""
if [[ ${TEST_EXIT} -eq 0 ]]; then
	echo "All integration tests passed."
else
	echo "Integration tests FAILED (exit code ${TEST_EXIT})."
fi

exit "${TEST_EXIT}"
