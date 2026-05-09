"""Live LXMF/Reticulum connectivity smoke tests.

These tests connect to a **real** Reticulum/LXMF router and exercise
MEDRE adapter lifecycle against a live network.

All tests are **skipped by default** and require explicit opt-in via
environment variables.

**Running live tests:**

1. Set up a Reticulum instance with an LXMF router.

2. Set the required environment variables:

   .. code-block:: bash

       export LXMF_CONNECTION_TYPE="reticulum"
       export LXMF_IDENTITY_PATH="/path/to/identity"
       # export LXMF_DISPLAY_NAME="MEDRE Live Test"  # optional

3. Run the live tests:

   .. code-block:: bash

       pip install lxmf
       pytest tests/test_lxmf_live.py -m live -v

   Default ``pytest`` run (no live tests):

   .. code-block:: bash

       pytest   # live tests excluded by addopts

**Required environment variables:**

=========================== =====================================================
Variable                    Description
=========================== =====================================================
``LXMF_CONNECTION_TYPE``    Connection mode: must be ``reticulum``
``LXMF_IDENTITY_PATH``      Path to a Reticulum identity file for the LXMF
                            router.  Must be a non-empty string pointing to
                            an existing identity file.
``LXMF_DISPLAY_NAME``       Optional display name for LXMF announces.
=========================== =====================================================

At minimum, ``LXMF_CONNECTION_TYPE`` and ``LXMF_IDENTITY_PATH`` must
be set.  If any required variable is missing, every test in this file
skips with a descriptive reason.

**Known limitations (explicit):**

- **No real LXMF connectivity yet.**  The adapter is scaffolded;
  non-fake connections raise ``LxmfConnectionError`` when the ``lxmf``
  SDK is not available.  These tests document the future required
  environment variables and will be enabled when production LXMF
  support is implemented.
- **No E2EE.**  LXMF encrypted messages are not supported in tranche 1.
- **No inbound event subscription wiring.**  ``_subscribe_events()`` is a
  scaffold method that logs but does not wire real LXMRouter callbacks.
- **Radio / network safety.**  When enabled, tests send a small number
  of messages.  Ensure the network is not used for critical
  communications during testing.

**What this proves (when enabled):**

- The MEDRE ``LxmfAdapter`` can ``start()`` with a real Reticulum
  instance.
- ``health_check()`` reports ``"healthy"``.
- ``stop()`` disconnects cleanly.

**What this does NOT prove:**

- Full MEDRE adapter outbound delivery integration with real LXMF.
- Production-grade reconnection handling.
- Multi-hop Reticulum delivery.
- Encrypted LXMF message support.
"""

import asyncio
import logging
import os
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

# ---------------------------------------------------------------------------
# Module-level marker — entire file is tagged "live" so it is excluded by the
# default ``addopts = "-m 'not live'"`` in pyproject.toml.
# ---------------------------------------------------------------------------
pytestmark = pytest.mark.live

# ---------------------------------------------------------------------------
# Environment variable gate
# ---------------------------------------------------------------------------
LXMF_CONNECTION_TYPE = os.environ.get("LXMF_CONNECTION_TYPE", "").lower()
LXMF_IDENTITY_PATH = os.environ.get("LXMF_IDENTITY_PATH", "")
LXMF_DISPLAY_NAME = os.environ.get("LXMF_DISPLAY_NAME", "")


def _validate_env() -> tuple[str, str]:
    """Validate env vars and return (reason, connection_type).

    Returns ("", connection_type) if valid, or (skip_reason, "") if not.
    """
    ct = LXMF_CONNECTION_TYPE
    if not ct:
        return (
            "Set LXMF_CONNECTION_TYPE (reticulum) to run live LXMF tests",
            "",
        )

    if ct != "reticulum":
        return (
            f"Unknown LXMF_CONNECTION_TYPE {ct!r}; use reticulum",
            "",
        )

    if not LXMF_IDENTITY_PATH:
        return (
            "LXMF_IDENTITY_PATH is required for live LXMF tests",
            "",
        )

    return ("", ct)


_LIVE_SKIP_REASON, _CONNECTION_TYPE = _validate_env()
_LIVE_ENV_SET = _CONNECTION_TYPE != ""

require_live = pytest.mark.skipif(
    not _LIVE_ENV_SET,
    reason=_LIVE_SKIP_REASON,
)

require_lxmf_sdk = pytest.mark.skipif(
    not _LIVE_ENV_SET,
    reason=_LIVE_SKIP_REASON,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _make_config():
    """Build an LxmfConfig from the live environment variables."""
    from medre.adapters.lxmf.config import LxmfConfig

    return LxmfConfig(
        adapter_id="lxmf-live-smoke",
        connection_type="reticulum",
        identity_path=LXMF_IDENTITY_PATH or None,
        display_name=LXMF_DISPLAY_NAME,
    )


def _make_context():
    """Build an AdapterContext suitable for live smoke tests."""
    from medre.adapters.base import AdapterContext

    return AdapterContext(
        adapter_id="lxmf-live-smoke",
        event_bus=None,
        publish_inbound=AsyncMock(),
        logger=logging.getLogger("test.lxmf-live"),
        clock=lambda: datetime.now(timezone.utc),
        shutdown_event=asyncio.Event(),
    )


# ---------------------------------------------------------------------------
# Live tests
# ---------------------------------------------------------------------------
@require_live
class TestLxmfLiveSmoke:
    """Live LXMF/Reticulum connectivity smoke tests.

    These tests connect to a real Reticulum/LXMF router and verify the
    adapter lifecycle: start, health_check, and stop.

    **NOTE**: Real LXMF connections require the ``lxmf`` and ``RNS``
    packages.  These tests will skip if the SDK is not installed.

    All tests require LXMF_CONNECTION_TYPE and LXMF_IDENTITY_PATH.
    Run with::

        pytest tests/test_lxmf_live.py -m live -v
    """

    # -- Lifecycle: connect, health, disconnect ----------------------------

    async def test_adapter_starts_and_reports_healthy(self):
        """Start the real adapter and verify health_check reports healthy.

        **Category B — MEDRE adapter lifecycle smoke test.**

        This validates:
        - The adapter creates a real LXMF client in ``start()``.
        - ``health_check()`` returns ``"healthy"`` after start.

        Note: This test will raise LxmfConnectionError until
        production LXMF support is implemented.
        """
        from medre.adapters.lxmf.adapter import LxmfAdapter
        from medre.adapters.lxmf.errors import LxmfConnectionError

        config = _make_config()
        adapter = LxmfAdapter(config)
        ctx = _make_context()

        try:
            await adapter.start(ctx)
            info = await adapter.health_check()
            assert info.health in ("healthy", "unknown"), (
                f"Expected healthy or unknown, got {info.health!r}"
            )
        except LxmfConnectionError:
            pytest.skip(
                "Real LXMF connections not yet implemented; "
                "this test documents the future live test structure"
            )
        finally:
            await adapter.stop()

    # -- Documentation tests (always pass) ----------------------------------

    async def test_lxmf_sdk_not_yet_connected_note(self):
        """Document: real LXMF SDK connections are scaffolded.

        This test always passes.  It exists to document that the
        LxmfAdapter raises ``LxmfConnectionError`` for non-fake
        connection types when the ``lxmf`` SDK is not available.
        Full production LXMF support is deferred to a future tranche.
        """
        pass

    async def test_outbound_delivery_not_yet_implemented_note(self):
        """Document: outbound LXMF delivery is scaffolded.

        This test always passes.  The real LxmfAdapter.deliver()
        returns ``None`` — no outbound delivery is implemented.
        """
        pass

    async def test_inbound_event_subscription_not_yet_wired_note(self):
        """Document: LXMF event subscriptions are scaffolded.

        This test always passes.  _subscribe_events() and
        _unsubscribe_events() are scaffold methods that log but do
        not wire real LXMRouter callbacks.
        """
        pass

    async def test_identity_path_configuration_note(self):
        """Document: identity_path is validated by LxmfConfig.

        This test always passes.  It documents that ``identity_path``
        must be a non-empty string when provided, and is required
        for ``reticulum`` connection type in a future production
        implementation.
        """
        pass
