"""Integration tests for MEDRE transport boundaries.

This package contains both Docker-backed service integration and deterministic
local real-SDK integration. Docker tests use ``pytest.mark.docker``; local
endpoint/emulator tests use ``pytest.mark.local_integration`` plus the adapter's
``*_sdk`` marker. Both are excluded from the default test run by ``pyproject``.

Examples::

    pytest tests/integration/ -m docker -v
    pytest tests/integration/ -m local_integration -v
"""
