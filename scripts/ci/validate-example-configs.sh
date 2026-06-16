#!/usr/bin/env bash

set -euo pipefail

# =============================================================================
# MEDRE Example Config Validation
# =============================================================================
#
# Focused pre-flight check for shipped example configs. Runs the three test
# files that gate example-config drift, adapter-kind validity, and runtime
# loader parity. Intended for CI and local pre-push runs — no network,
# Docker, or hardware required.
#
# Usage:
#   ./scripts/ci/validate-example-configs.sh
# =============================================================================

PYTHONPATH=src python -m pytest -q \
	tests/test_example_configs.py \
	tests/test_config_runtime_parity.py \
	tests/test_adapter_kind_validity.py
