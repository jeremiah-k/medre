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
#   uv sync --locked --extra dev --extra matrix --extra meshtastic
#   uv run --no-sync bash scripts/ci/run-docker-integration.sh
#
# Environment variables:
#   MEDRE_SYNAPSE_IMAGE       — Synapse Docker image (default: matrixdotorg/synapse:v1.162.0)
#   MEDRE_MESHTASTICD_IMAGE   — meshtasticd Docker image (default: meshtastic/meshtasticd:2.7.26)
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

# Check the selected environment without mutating it. A uv-created venv need
# not contain pip, and importing core MEDRE does not prove SDK availability.
echo "Checking MEDRE and integration dependencies..."
if ! "${PYTHON}" -c "import medre, pytest, nio, meshtastic, pubsub"; then
	echo "ERROR: the selected Python environment lacks integration dependencies." >&2
	echo "Prepare the environment, then run this script through uv:" >&2
	echo '  uv sync --locked --extra dev --extra matrix --extra meshtastic' >&2
	echo '  uv run --no-sync bash scripts/ci/run-docker-integration.sh' >&2
	echo 'For an existing pip environment: python -m pip install -e ".[matrix,meshtastic,dev]"' >&2
	exit 1
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
