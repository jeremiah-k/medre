"""Tests for LXMF _send_real retry-loop exhaustion behaviour.

When the bounded retry loop exhausts all attempts due to transient
failures (e.g. router transport errors), the resulting LxmfSendError
MUST carry ``transient=True`` so the outer pipeline's durable-retry
mechanism can take over.  Exhausting the adapter-level fast-retry loop
does NOT make the failure permanent.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from medre.adapters.lxmf.errors import LxmfSendError
from medre.adapters.lxmf.session import LxmfSession
from medre.config.adapters.lxmf import LxmfConfig


def _make_config(**overrides: Any) -> LxmfConfig:
    defaults: dict[str, Any] = dict(adapter_id="lxmf-test")
    defaults.update(overrides)
    if (
        defaults.get("connection_type") == "reticulum"
        and "storage_path" not in defaults
    ):
        defaults["storage_path"] = "/tmp/medre-test-lxmf-router"
    return LxmfConfig(**defaults)


def _make_session(**config_overrides: Any) -> LxmfSession:
    config = _make_config(**config_overrides)
    return LxmfSession(
        config=config,
        adapter_id=config.adapter_id,
    )


# ===================================================================
# Retry-loop exhaustion stays transient
# ===================================================================


class TestRetryExhaustionTransient:
    """Exhausting the bounded retry loop raises LxmfSendError(transient=True)."""

    async def test_transient_exhaustion_stays_transient(self) -> None:
        """After _SEND_MAX_RETRIES transient failures, error is still transient."""
        session = _make_session(connection_type="fake")
        await session.start()
        session._config = _make_config(connection_type="reticulum")
        session._diag.connected = True

        recalled_identity = MagicMock()

        class FakeDestination:
            OUT = "out"
            SINGLE = "single"
            hash = b"\x00" * 16

            def __init__(self, identity, *args, **kwargs):
                pass

        class FakeLXMessage:
            OUTBOUND = 1

            def __init__(self, dest, router, content, **kwargs):
                pass

            def register_delivery_callback(self, cb):
                pass

        mock_rns = MagicMock()
        mock_rns.Identity.recall.return_value = recalled_identity
        mock_rns.Destination = FakeDestination

        mock_lxmf = MagicMock()
        mock_lxmf.LXMessage = FakeLXMessage

        mock_router = MagicMock()
        # handle_outbound raises on every attempt, exhausting the retry loop.
        mock_router.handle_outbound.side_effect = RuntimeError("transport glitch")

        session._identity = MagicMock()
        session._router = mock_router

        with patch(
            "medre.adapters.lxmf.session._require_lxmf",
            return_value=(mock_rns, mock_lxmf),
        ):
            with pytest.raises(
                LxmfSendError,
                match=r"Send failed after 3 attempts",
            ) as exc_info:
                await session._send_real(
                    destination_hash="ab" * 16,
                    content="hello",
                )

        assert (
            exc_info.value.transient is True
        ), "Retry exhaustion must remain transient for outer pipeline retry"

        # 3 transient increments inside the loop + 1 at exhaustion = 4
        assert session._diag.transient_delivery_failures == 4
        assert session._diag.permanent_delivery_failures == 0
        await session.stop()
