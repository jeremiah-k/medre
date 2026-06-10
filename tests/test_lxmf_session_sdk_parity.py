"""LXMF session SDK-parity tests: stamp_cost wiring,
default_delivery_method wiring, message_delay_seconds pacing,
and delivery state callback verification.

Extracted from test_lxmf_session.py to keep file sizes manageable.
"""

from __future__ import annotations

import logging
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from medre.adapters.lxmf.session import (
    LxmfDeliveryState,
    LxmfSession,
)
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
# Stamp cost wiring
# ===================================================================


class TestStampCostWiring:
    """stamp_cost from LxmfConfig is propagated to LXMRouter on connect."""

    async def test_stamp_cost_set_on_connect(self) -> None:
        """When stamp_cost > 0, _connect_real calls
        router.set_inbound_stamp_cost(None, stamp_cost)."""
        session = _make_session(
            connection_type="reticulum",
            stamp_cost=12,
        )

        mock_rns = MagicMock()
        mock_lxmf = MagicMock()
        mock_router = MagicMock()
        mock_rns.Reticulum.get_instance.return_value = None
        mock_rns.Reticulum.return_value = MagicMock()
        mock_rns.Identity.return_value = MagicMock()
        mock_lxmf.LXMRouter.return_value = mock_router

        with (
            patch("medre.adapters.lxmf.session.HAS_LXMF", True),
            patch(
                "medre.adapters.lxmf.session._require_lxmf",
                return_value=(mock_rns, mock_lxmf),
            ),
        ):
            await session.start()

        mock_router.set_inbound_stamp_cost.assert_called_once_with(None, 12)
        await session.stop()

    async def test_stamp_cost_zero_is_noop(self) -> None:
        """When stamp_cost == 0, no stamp cost method is called on the
        router."""
        session = _make_session(
            connection_type="reticulum",
            stamp_cost=0,
        )

        mock_rns = MagicMock()
        mock_lxmf = MagicMock()
        mock_router = MagicMock()
        mock_rns.Reticulum.get_instance.return_value = None
        mock_rns.Reticulum.return_value = MagicMock()
        mock_rns.Identity.return_value = MagicMock()
        mock_lxmf.LXMRouter.return_value = mock_router

        with (
            patch("medre.adapters.lxmf.session.HAS_LXMF", True),
            patch(
                "medre.adapters.lxmf.session._require_lxmf",
                return_value=(mock_rns, mock_lxmf),
            ),
        ):
            await session.start()

        mock_router.set_inbound_stamp_cost.assert_not_called()
        await session.stop()

    async def test_stamp_cost_graceful_degradation(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """When router lacks set_inbound_stamp_cost, no crash occurs and a
        debug message is logged."""
        session = _make_session(
            connection_type="reticulum",
            stamp_cost=8,
        )

        mock_rns = MagicMock()
        mock_lxmf = MagicMock()
        mock_router = MagicMock()
        # Remove the stamp cost method to simulate older SDK.
        del mock_router.set_inbound_stamp_cost
        mock_rns.Reticulum.get_instance.return_value = None
        mock_rns.Reticulum.return_value = MagicMock()
        mock_rns.Identity.return_value = MagicMock()
        mock_lxmf.LXMRouter.return_value = mock_router

        with (
            patch("medre.adapters.lxmf.session.HAS_LXMF", True),
            patch(
                "medre.adapters.lxmf.session._require_lxmf",
                return_value=(mock_rns, mock_lxmf),
            ),
            caplog.at_level(logging.DEBUG),
        ):
            await session.start()

        # Session should still be connected — graceful degradation.
        assert session.connected is True
        assert any("stamp_cost" in r.message.lower() for r in caplog.records)
        await session.stop()


# ===================================================================
# Default delivery method wiring
# ===================================================================


class TestDeliveryMethodDefaultWiring:
    """default_delivery_method from config is used when no explicit override
    is provided, and overridden when one is."""

    async def test_default_delivery_method_used_when_no_explicit(self) -> None:
        """send_text with delivery_method=None uses config's
        default_delivery_method."""
        session = _make_session(
            connection_type="reticulum",
            default_delivery_method="propagated",
        )

        mock_rns = MagicMock()
        mock_lxmf = MagicMock()
        mock_router = MagicMock()
        mock_rns.Reticulum.get_instance.return_value = None
        mock_rns.Reticulum.return_value = MagicMock()
        mock_identity = MagicMock()
        mock_rns.Identity.return_value = mock_identity
        mock_lxmf.LXMRouter.return_value = mock_router

        # Set up LXMessage class with delivery method constants.
        mock_lxm_cls = MagicMock()
        mock_lxm_cls.PROPAGATED = "PROPAGATED_CONST"
        mock_lxm_cls.DIRECT = "DIRECT_CONST"
        mock_lxmf.LXMessage = mock_lxm_cls

        # Set up RNS.Identity.recall to return a valid identity.
        mock_rns.Identity.recall.return_value = mock_identity

        # Set up RNS.Destination constructor.
        mock_dest = MagicMock()
        mock_rns.Destination.return_value = mock_dest

        # Set up LXMessage constructor to return a mock message.
        mock_message = MagicMock()
        mock_message.hash = b"\xab" * 32
        mock_message.state = "outbound"
        mock_lxmf.LXMessage.return_value = mock_message

        with (
            patch("medre.adapters.lxmf.session.HAS_LXMF", True),
            patch(
                "medre.adapters.lxmf.session._require_lxmf",
                return_value=(mock_rns, mock_lxmf),
            ),
        ):
            await session.start()
            # send_text with delivery_method=None should use config default
            # ("propagated").
            await session.send_text("ab" * 16, "hello", delivery_method=None)

        # Verify LXMessage was constructed with PROPAGATED constant.
        mock_lxmf.LXMessage.assert_called_once()
        call_kwargs = mock_lxmf.LXMessage.call_args
        # The desired_method kwarg should be the PROPAGATED constant.
        assert call_kwargs.kwargs.get("desired_method") == "PROPAGATED_CONST"
        await session.stop()

    async def test_explicit_delivery_method_overrides_default(self) -> None:
        """send_text with explicit delivery_method overrides config default."""
        session = _make_session(
            connection_type="reticulum",
            default_delivery_method="direct",
        )

        mock_rns = MagicMock()
        mock_lxmf = MagicMock()
        mock_router = MagicMock()
        mock_rns.Reticulum.get_instance.return_value = None
        mock_rns.Reticulum.return_value = MagicMock()
        mock_identity = MagicMock()
        mock_rns.Identity.return_value = mock_identity
        mock_lxmf.LXMRouter.return_value = mock_router

        # Set up LXMessage class with delivery method constants.
        mock_lxm_cls = MagicMock()
        mock_lxm_cls.PROPAGATED = "PROPAGATED_CONST"
        mock_lxm_cls.DIRECT = "DIRECT_CONST"
        mock_lxmf.LXMessage = mock_lxm_cls

        # Set up RNS.Identity.recall.
        mock_rns.Identity.recall.return_value = mock_identity

        # Set up RNS.Destination.
        mock_dest = MagicMock()
        mock_rns.Destination.return_value = mock_dest

        # Set up LXMessage.
        mock_message = MagicMock()
        mock_message.hash = b"\xcd" * 32
        mock_message.state = "outbound"
        mock_lxmf.LXMessage.return_value = mock_message

        with (
            patch("medre.adapters.lxmf.session.HAS_LXMF", True),
            patch(
                "medre.adapters.lxmf.session._require_lxmf",
                return_value=(mock_rns, mock_lxmf),
            ),
        ):
            await session.start()
            # Explicit delivery_method="propagated" overrides config default
            # "direct".
            await session.send_text("ab" * 16, "hello", delivery_method="propagated")

        mock_lxmf.LXMessage.assert_called_once()
        call_kwargs = mock_lxmf.LXMessage.call_args
        # desired_method should be PROPAGATED, not DIRECT.
        assert call_kwargs.kwargs.get("desired_method") == "PROPAGATED_CONST"
        await session.stop()


# ===================================================================
# Message delay pacing
# ===================================================================


class TestMessageDelayPacing:
    """message_delay_seconds from LxmfConfig is honoured by _send_real()."""

    async def test_delay_applied_when_positive(self) -> None:
        """When message_delay_seconds > 0, asyncio.sleep is called with that
        value before the SDK send."""
        session = _make_session(
            connection_type="reticulum",
            message_delay_seconds=1.5,
        )

        mock_rns = MagicMock()
        mock_lxmf = MagicMock()
        mock_router = MagicMock()
        mock_rns.Reticulum.get_instance.return_value = None
        mock_rns.Reticulum.return_value = MagicMock()
        mock_identity = MagicMock()
        mock_rns.Identity.return_value = mock_identity
        mock_lxmf.LXMRouter.return_value = mock_router

        mock_lxm_cls = MagicMock()
        mock_lxm_cls.DIRECT = "DIRECT_CONST"
        mock_lxmf.LXMessage = mock_lxm_cls

        mock_rns.Identity.recall.return_value = mock_identity
        mock_rns.Destination.return_value = MagicMock()

        mock_message = MagicMock()
        mock_message.hash = b"\xaa" * 32
        mock_message.state = "outbound"
        mock_lxmf.LXMessage.return_value = mock_message

        with (
            patch("medre.adapters.lxmf.session.HAS_LXMF", True),
            patch(
                "medre.adapters.lxmf.session._require_lxmf",
                return_value=(mock_rns, mock_lxmf),
            ),
            patch(
                "medre.adapters.lxmf.session.asyncio.sleep", new_callable=AsyncMock
            ) as mock_sleep,
        ):
            await session.start()
            await session.send_text("ab" * 16, "hello")

        mock_sleep.assert_any_call(1.5)
        await session.stop()

    async def test_no_delay_when_zero(self) -> None:
        """When message_delay_seconds == 0, no pacing sleep occurs before
        the SDK send."""
        session = _make_session(
            connection_type="reticulum",
            message_delay_seconds=0,
        )

        mock_rns = MagicMock()
        mock_lxmf = MagicMock()
        mock_router = MagicMock()
        mock_rns.Reticulum.get_instance.return_value = None
        mock_rns.Reticulum.return_value = MagicMock()
        mock_identity = MagicMock()
        mock_rns.Identity.return_value = mock_identity
        mock_lxmf.LXMRouter.return_value = mock_router

        mock_lxm_cls = MagicMock()
        mock_lxm_cls.DIRECT = "DIRECT_CONST"
        mock_lxmf.LXMessage = mock_lxm_cls

        mock_rns.Identity.recall.return_value = mock_identity
        mock_rns.Destination.return_value = MagicMock()

        mock_message = MagicMock()
        mock_message.hash = b"\xbb" * 32
        mock_message.state = "outbound"
        mock_lxmf.LXMessage.return_value = mock_message

        with (
            patch("medre.adapters.lxmf.session.HAS_LXMF", True),
            patch(
                "medre.adapters.lxmf.session._require_lxmf",
                return_value=(mock_rns, mock_lxmf),
            ),
            patch(
                "medre.adapters.lxmf.session.asyncio.sleep", new_callable=AsyncMock
            ) as mock_sleep,
        ):
            await session.start()
            await session.send_text("ab" * 16, "hello")

        # Only retry backoff sleeps (0.1 * attempt) should appear, never a
        # pacing sleep of 0 — we guard with > 0 so sleep(0) is never called
        # for pacing.  Verify no call with the config value (0).
        for call in mock_sleep.call_args_list:
            assert call != ((0,),)
        await session.stop()

    async def test_sleep_duration_matches_config(self) -> None:
        """The exact config value is passed to asyncio.sleep — verifies
        duration precision, not just 'some sleep happened'."""
        session = _make_session(
            connection_type="reticulum",
            message_delay_seconds=2.5,
        )

        mock_rns = MagicMock()
        mock_lxmf = MagicMock()
        mock_router = MagicMock()
        mock_rns.Reticulum.get_instance.return_value = None
        mock_rns.Reticulum.return_value = MagicMock()
        mock_identity = MagicMock()
        mock_rns.Identity.return_value = mock_identity
        mock_lxmf.LXMRouter.return_value = mock_router

        mock_lxm_cls = MagicMock()
        mock_lxm_cls.DIRECT = "DIRECT_CONST"
        mock_lxmf.LXMessage = mock_lxm_cls

        mock_rns.Identity.recall.return_value = mock_identity
        mock_rns.Destination.return_value = MagicMock()

        mock_message = MagicMock()
        mock_message.hash = b"\xcc" * 32
        mock_message.state = "outbound"
        mock_lxmf.LXMessage.return_value = mock_message

        pacing_calls: list[float] = []

        async def _fake_sleep(delay: float) -> None:
            # Record only the pacing sleep (retry backoffs are < 1).
            if delay >= 1.0:
                pacing_calls.append(delay)

        with (
            patch("medre.adapters.lxmf.session.HAS_LXMF", True),
            patch(
                "medre.adapters.lxmf.session._require_lxmf",
                return_value=(mock_rns, mock_lxmf),
            ),
            patch(
                "medre.adapters.lxmf.session.asyncio.sleep",
                side_effect=_fake_sleep,
            ),
        ):
            await session.start()
            await session.send_text("ab" * 16, "hello")

        assert pacing_calls == [2.5]
        await session.stop()


# ===================================================================
# Delivery state callback
# ===================================================================


class TestDeliveryStateCallback:
    """Delivery state callback is invoked on terminal state transitions."""

    async def _start_fake_session(self) -> LxmfSession:
        """Create and start a session in fake mode."""
        session = _make_session(connection_type="fake")
        await session.start()
        return session

    async def test_callback_invoked_on_delivered(self) -> None:
        """Callback fires when delivery state transitions to DELIVERED."""
        session = await self._start_fake_session()

        # Manually seed an outbound delivery.
        msg_hash = "aa" * 32
        from medre.adapters.lxmf.session import _OutboundDelivery

        session._outbound_deliveries[msg_hash] = _OutboundDelivery(
            native_message_id=msg_hash,
            state=LxmfDeliveryState.SENT,
            destination_hash="bb" * 16,
        )

        callback = MagicMock()
        session.set_delivery_state_callback(callback)

        # Simulate delivery state update with DELIVERED state.
        message = MagicMock()
        message.hash = bytes.fromhex(msg_hash)
        message.state = LxmfDeliveryState.DELIVERED

        session._apply_delivery_state_update(message)

        callback.assert_called_once_with(msg_hash, "delivered")
        await session.stop()

    async def test_callback_invoked_on_failed(self) -> None:
        """Callback fires when delivery state transitions to FAILED."""
        session = await self._start_fake_session()

        msg_hash = "cc" * 32
        from medre.adapters.lxmf.session import _OutboundDelivery

        session._outbound_deliveries[msg_hash] = _OutboundDelivery(
            native_message_id=msg_hash,
            state=LxmfDeliveryState.SENDING,
            destination_hash="dd" * 16,
        )

        callback = MagicMock()
        session.set_delivery_state_callback(callback)

        message = MagicMock()
        message.hash = bytes.fromhex(msg_hash)
        message.state = LxmfDeliveryState.FAILED

        session._apply_delivery_state_update(message)

        callback.assert_called_once_with(msg_hash, "failed")
        await session.stop()

    async def test_callback_invoked_on_rejected(self) -> None:
        """Callback fires when delivery state transitions to REJECTED."""
        session = await self._start_fake_session()

        msg_hash = "ee" * 32
        from medre.adapters.lxmf.session import _OutboundDelivery

        session._outbound_deliveries[msg_hash] = _OutboundDelivery(
            native_message_id=msg_hash,
            state=LxmfDeliveryState.SENT,
            destination_hash="ff" * 16,
        )

        callback = MagicMock()
        session.set_delivery_state_callback(callback)

        message = MagicMock()
        message.hash = bytes.fromhex(msg_hash)
        message.state = LxmfDeliveryState.REJECTED

        session._apply_delivery_state_update(message)

        callback.assert_called_once_with(msg_hash, "rejected")
        await session.stop()

    async def test_callback_invoked_on_cancelled(self) -> None:
        """Callback fires when delivery state transitions to CANCELLED."""
        session = await self._start_fake_session()

        msg_hash = "11" * 32
        from medre.adapters.lxmf.session import _OutboundDelivery

        session._outbound_deliveries[msg_hash] = _OutboundDelivery(
            native_message_id=msg_hash,
            state=LxmfDeliveryState.SENDING,
            destination_hash="22" * 16,
        )

        callback = MagicMock()
        session.set_delivery_state_callback(callback)

        message = MagicMock()
        message.hash = bytes.fromhex(msg_hash)
        message.state = LxmfDeliveryState.CANCELLED

        session._apply_delivery_state_update(message)

        callback.assert_called_once_with(msg_hash, "cancelled")
        await session.stop()

    async def test_no_callback_for_intermediate_states(self) -> None:
        """Callback is NOT invoked for intermediate states (SENDING, SENT)."""
        session = await self._start_fake_session()

        msg_hash = "33" * 32
        from medre.adapters.lxmf.session import _OutboundDelivery

        session._outbound_deliveries[msg_hash] = _OutboundDelivery(
            native_message_id=msg_hash,
            state=LxmfDeliveryState.GENERATING,
            destination_hash="44" * 16,
        )

        callback = MagicMock()
        session.set_delivery_state_callback(callback)

        # SENDING — intermediate, should NOT trigger callback.
        message = MagicMock()
        message.hash = bytes.fromhex(msg_hash)
        message.state = LxmfDeliveryState.SENDING

        session._apply_delivery_state_update(message)
        callback.assert_not_called()

        # SENT — also intermediate, should NOT trigger callback.
        message2 = MagicMock()
        message2.hash = bytes.fromhex(msg_hash)
        message2.state = LxmfDeliveryState.SENT

        session._apply_delivery_state_update(message2)
        callback.assert_not_called()

        await session.stop()

    async def test_no_callback_when_not_registered(self) -> None:
        """Graceful handling when no callback is registered — no crash."""
        session = await self._start_fake_session()

        msg_hash = "55" * 32
        from medre.adapters.lxmf.session import _OutboundDelivery

        session._outbound_deliveries[msg_hash] = _OutboundDelivery(
            native_message_id=msg_hash,
            state=LxmfDeliveryState.SENT,
            destination_hash="66" * 16,
        )

        # No callback registered (default is None).
        message = MagicMock()
        message.hash = bytes.fromhex(msg_hash)
        message.state = LxmfDeliveryState.DELIVERED

        # Should not raise.
        session._apply_delivery_state_update(message)

        assert msg_hash not in session._outbound_deliveries
        await session.stop()

    async def test_callback_receives_correct_hash_and_state(self) -> None:
        """Callback arguments are the exact message hash and lowercase state."""
        session = await self._start_fake_session()

        msg_hash = "77" * 32
        from medre.adapters.lxmf.session import _OutboundDelivery

        session._outbound_deliveries[msg_hash] = _OutboundDelivery(
            native_message_id=msg_hash,
            state=LxmfDeliveryState.OUTBOUND,
            destination_hash="88" * 16,
        )

        captured_args: list[tuple[str, str]] = []
        session.set_delivery_state_callback(lambda h, s: captured_args.append((h, s)))

        message = MagicMock()
        message.hash = bytes.fromhex(msg_hash)
        message.state = LxmfDeliveryState.DELIVERED

        session._apply_delivery_state_update(message)

        assert len(captured_args) == 1
        assert captured_args[0] == (msg_hash, "delivered")
        await session.stop()

    async def test_callback_exception_does_not_crash(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A faulty callback does not prevent delivery tracking cleanup."""
        session = await self._start_fake_session()

        msg_hash = "99" * 32
        from medre.adapters.lxmf.session import _OutboundDelivery

        session._outbound_deliveries[msg_hash] = _OutboundDelivery(
            native_message_id=msg_hash,
            state=LxmfDeliveryState.SENT,
            destination_hash="aa" * 16,
        )

        def _bad_callback(h: str, s: str) -> None:
            raise RuntimeError("boom")

        session.set_delivery_state_callback(_bad_callback)

        message = MagicMock()
        message.hash = bytes.fromhex(msg_hash)
        message.state = LxmfDeliveryState.FAILED

        with caplog.at_level(logging.DEBUG):
            session._apply_delivery_state_update(message)

        # Delivery should still be untracked despite callback error.
        assert msg_hash not in session._outbound_deliveries
        assert any(
            "delivery state callback" in r.message.lower() for r in caplog.records
        )
        await session.stop()

    async def test_set_callback_none_clears(self) -> None:
        """Setting callback to None disables notifications."""
        session = await self._start_fake_session()

        msg_hash = "bb" * 32
        from medre.adapters.lxmf.session import _OutboundDelivery

        session._outbound_deliveries[msg_hash] = _OutboundDelivery(
            native_message_id=msg_hash,
            state=LxmfDeliveryState.SENT,
            destination_hash="cc" * 16,
        )

        callback = MagicMock()
        session.set_delivery_state_callback(callback)
        session.set_delivery_state_callback(None)

        message = MagicMock()
        message.hash = bytes.fromhex(msg_hash)
        message.state = LxmfDeliveryState.DELIVERED

        session._apply_delivery_state_update(message)
        callback.assert_not_called()
        await session.stop()
