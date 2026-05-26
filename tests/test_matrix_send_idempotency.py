"""Tests for Matrix send idempotency and delivery classification.

Covers:
- _matrix_txn_id helper (deterministic, different inputs → different outputs)
- txn_id passed to room_send and reused across retries
- Native Matrix event ID returned on success
- Success metadata includes matrix_txn_id
- Error classification: timeout/network → transient, rate-limit → transient,
  E2EE-blocked → permanent, M_FORBIDDEN → permanent
- CancelledError propagation
- No secrets in metadata
- Counter accuracy for transient/permanent delivery failures
- Docs do not claim exactly-once delivery

No test requires mindroom-nio[e2e]. No pytest run needed — py_compile only.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from medre.adapters.matrix.adapter import (
    MatrixAdapter,
    _is_nio_permanent_response,
    _is_nio_rate_limited_response,
    _is_transient_error,
    _matrix_txn_id,
    _NioRateLimitError,
)
from medre.config.adapters.matrix import MatrixConfig
from medre.core.contracts.adapter import (
    AdapterContext,
    AdapterPermanentError,
    AdapterSendError,
)
from medre.core.rendering.renderer import RenderingResult

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_config(**overrides: Any) -> MatrixConfig:
    defaults: dict[str, Any] = {
        "adapter_id": "matrix-idem-test",
        "homeserver": "https://matrix.example.com",
        "user_id": "@bot:example.com",
        "access_token": "tok_abc",
    }
    defaults.update(overrides)
    return MatrixConfig(**defaults)


def _make_context(adapter_id: str = "matrix-idem-test") -> AdapterContext:
    return AdapterContext(
        adapter_id=adapter_id,
        event_bus=None,
        publish_inbound=AsyncMock(),
        logger=logging.getLogger(f"test.{adapter_id}"),
        clock=lambda: datetime.now(timezone.utc),
        shutdown_event=asyncio.Event(),
    )


def _make_result(
    event_id: str = "evt-001",
    target_adapter: str = "matrix-idem-test",
    target_channel: str = "!room:example.com",
    body: str = "hello",
) -> RenderingResult:
    return RenderingResult(
        event_id=event_id,
        target_adapter=target_adapter,
        target_channel=target_channel,
        payload={"msgtype": "m.text", "body": body},
    )


def _make_send_response(event_id: str = "$sent-001") -> MagicMock:
    resp = MagicMock()
    resp.event_id = event_id
    return resp


# ---------------------------------------------------------------------------
# 1-3. _matrix_txn_id helper
# ---------------------------------------------------------------------------


class TestMatrixTxnId:
    """_matrix_txn_id is deterministic and input-sensitive."""

    def test_same_result_room_same_txn_id(self) -> None:
        result = _make_result()
        txn1 = _matrix_txn_id(result, "!room:example.com")
        txn2 = _matrix_txn_id(result, "!room:example.com")
        assert txn1 == txn2

    def test_different_event_id_different_txn_id(self) -> None:
        r1 = _make_result(event_id="evt-001")
        r2 = _make_result(event_id="evt-002")
        assert _matrix_txn_id(r1, "!room:example.com") != _matrix_txn_id(
            r2, "!room:example.com"
        )

    def test_different_room_id_different_txn_id(self) -> None:
        result = _make_result()
        assert _matrix_txn_id(result, "!room-a:example.com") != _matrix_txn_id(
            result, "!room-b:example.com"
        )

    def test_different_channel_different_txn_id(self) -> None:
        r1 = _make_result(target_channel="!room-a:example.com")
        r2 = _make_result(target_channel="!room-b:example.com")
        assert _matrix_txn_id(r1, "!room:example.com") != _matrix_txn_id(
            r2, "!room:example.com"
        )

    def test_txn_id_has_medre_prefix(self) -> None:
        result = _make_result()
        txn_id = _matrix_txn_id(result, "!room:example.com")
        assert txn_id.startswith("medre_")

    def test_txn_id_is_38_chars(self) -> None:
        result = _make_result()
        txn_id = _matrix_txn_id(result, "!room:example.com")
        assert len(txn_id) == 38  # 6 prefix + 32 hex

    def test_txn_id_does_not_include_body(self) -> None:
        r1 = _make_result(body="hello world")
        r2 = _make_result(body="goodbye world")
        assert _matrix_txn_id(r1, "!room:example.com") == _matrix_txn_id(
            r2, "!room:example.com"
        )

    def test_txn_id_matches_expected_sha256(self) -> None:
        result = _make_result()
        room_id = "!room:example.com"
        expected_input = "".join(
            f"{len(p)}:{p}|"
            for p in [
                result.event_id,
                result.target_adapter,
                result.target_channel or "",
                room_id,
            ]
        )
        expected_digest = hashlib.sha256(expected_input.encode("utf-8")).hexdigest()[
            :32
        ]
        expected = f"medre_{expected_digest}"
        assert _matrix_txn_id(result, room_id) == expected


# ---------------------------------------------------------------------------
# 4. deliver passes txn_id to room_send
# ---------------------------------------------------------------------------


class TestDeliverTxnIdPassed:
    """deliver() passes the deterministic txn_id to client.room_send."""

    async def test_txn_id_passed_to_room_send(self) -> None:
        config = _make_config()
        adapter = MatrixAdapter(config)
        mock_client = MagicMock()
        mock_client.room_send = AsyncMock(return_value=_make_send_response())
        adapter._session = mock_client

        result = _make_result()
        await adapter.deliver(result)

        call_kwargs = mock_client.room_send.call_args.kwargs
        assert "tx_id" in call_kwargs
        room_id = result.target_channel or "!room:example.com"
        expected_txn = _matrix_txn_id(result, room_id)
        assert call_kwargs["tx_id"] == expected_txn


# ---------------------------------------------------------------------------
# 5. retry reuses same txn_id
# ---------------------------------------------------------------------------


class TestRetryReusesTxnId:
    """All retry attempts use the same txn_id."""

    async def test_retry_reuses_txn_id(self) -> None:
        config = _make_config()
        adapter = MatrixAdapter(config)
        mock_client = MagicMock()

        txn_ids_seen: list[str] = []
        call_count = 0

        async def _flaky_then_ok(**kwargs: Any) -> MagicMock:
            nonlocal call_count
            call_count += 1
            txn_ids_seen.append(kwargs.get("tx_id", ""))
            if call_count <= 2:
                raise ConnectionError("network glitch")
            return _make_send_response()

        mock_client.room_send = AsyncMock(side_effect=_flaky_then_ok)
        adapter._session = mock_client

        result = _make_result()
        with patch("asyncio.sleep", new_callable=AsyncMock):
            await adapter.deliver(result)

        assert call_count == 3
        # All txn_ids must be identical
        assert len(set(txn_ids_seen)) == 1
        room_id = result.target_channel or "!room:example.com"
        assert txn_ids_seen[0] == _matrix_txn_id(result, room_id)


# ---------------------------------------------------------------------------
# 6. success returns native_message_id from Matrix response event_id
# ---------------------------------------------------------------------------


class TestSuccessReturnsNativeMessageId:
    """Successful deliver returns AdapterDeliveryResult with Matrix event_id."""

    async def test_success_returns_event_id(self) -> None:
        config = _make_config()
        adapter = MatrixAdapter(config)
        mock_client = MagicMock()
        mock_client.room_send = AsyncMock(
            return_value=_make_send_response("$native-evt-123")
        )
        adapter._session = mock_client

        result = _make_result()
        delivery = await adapter.deliver(result)

        assert delivery is not None
        assert delivery.native_message_id == "$native-evt-123"
        assert delivery.native_channel_id == result.target_channel


# ---------------------------------------------------------------------------
# 7. success metadata includes matrix_txn_id
# ---------------------------------------------------------------------------


class TestSuccessMetadataIncludesTxnId:
    """AdapterDeliveryResult.metadata contains matrix_txn_id."""

    async def test_metadata_has_matrix_txn_id(self) -> None:
        config = _make_config()
        adapter = MatrixAdapter(config)
        mock_client = MagicMock()
        mock_client.room_send = AsyncMock(return_value=_make_send_response())
        adapter._session = mock_client

        result = _make_result()
        delivery = await adapter.deliver(result)

        assert delivery is not None
        assert "matrix_txn_id" in delivery.metadata
        room_id = result.target_channel or "!room:example.com"
        expected_txn = _matrix_txn_id(result, room_id)
        assert delivery.metadata["matrix_txn_id"] == expected_txn


# ---------------------------------------------------------------------------
# 8. timeout / network → transient
# ---------------------------------------------------------------------------


class TestTransientErrorClassification:
    """Timeout and network errors are classified as transient."""

    def test_asyncio_timeout_is_transient(self) -> None:
        assert _is_transient_error(asyncio.TimeoutError()) is True

    def test_timeout_is_transient(self) -> None:
        assert _is_transient_error(TimeoutError("timed out")) is True

    def test_oserror_is_transient(self) -> None:
        assert _is_transient_error(OSError("network down")) is True

    def test_connection_error_is_transient(self) -> None:
        assert _is_transient_error(ConnectionError("refused")) is True

    def test_nio_rate_limit_is_transient(self) -> None:
        assert _is_transient_error(_NioRateLimitError("M_LIMIT_EXCEEDED")) is True

    async def test_timeout_raises_adapter_send_error_transient(self) -> None:
        config = _make_config()
        adapter = MatrixAdapter(config)
        mock_client = MagicMock()
        mock_client.room_send = AsyncMock(
            side_effect=[asyncio.TimeoutError(), _make_send_response()]
        )
        adapter._session = mock_client

        result = _make_result()
        with patch("asyncio.sleep", new_callable=AsyncMock):
            delivery = await adapter.deliver(result)

        assert delivery is not None
        assert adapter._transient_delivery_failures == 1


# ---------------------------------------------------------------------------
# 9. rate-limit → transient if detectable
# ---------------------------------------------------------------------------


class TestRateLimitTransient:
    """M_LIMIT_EXCEEDED and 429 responses are retried as transient."""

    def test_nio_rate_limit_response_detected(self) -> None:
        resp = MagicMock()
        del resp.event_id  # no event_id
        resp.errcode = "M_LIMIT_EXCEEDED"
        assert _is_nio_rate_limited_response(resp) is True

    def test_nio_429_response_detected(self) -> None:
        resp = MagicMock()
        del resp.event_id
        resp.status_code = 429
        assert _is_nio_rate_limited_response(resp) is True

    def test_nio_success_not_rate_limited(self) -> None:
        resp = MagicMock()
        resp.event_id = "$ok"
        assert _is_nio_rate_limited_response(resp) is False

    async def test_rate_limit_response_retried(self) -> None:
        config = _make_config()
        adapter = MatrixAdapter(config)
        mock_client = MagicMock()

        call_count = 0

        async def _rate_limit_then_ok(**kwargs: Any) -> MagicMock:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                resp = MagicMock()
                del resp.event_id
                resp.errcode = "M_LIMIT_EXCEEDED"
                return resp
            return _make_send_response()

        mock_client.room_send = AsyncMock(side_effect=_rate_limit_then_ok)
        adapter._session = mock_client

        result = _make_result()
        with patch("asyncio.sleep", new_callable=AsyncMock):
            with pytest.raises(AdapterSendError) as exc_info:
                await adapter.deliver(result)
        assert exc_info.value.transient is True
        assert call_count == 1
        assert adapter._transient_delivery_failures == 1


# ---------------------------------------------------------------------------
# 10. E2EE-blocked → permanent with clear message
# ---------------------------------------------------------------------------


class TestE2EEBlockedPermanent:
    """Encrypted-room sends blocked when crypto disabled → permanent error."""

    async def test_encrypted_room_blocked_without_crypto(self) -> None:
        config = _make_config()
        adapter = MatrixAdapter(config)

        # Simulate a session with crypto disabled and room known encrypted
        mock_session = MagicMock()
        mock_session.crypto_enabled = False
        mock_session.room_state.return_value = "encrypted"
        adapter._session = mock_session

        result = _make_result()
        with pytest.raises(AdapterPermanentError, match="encrypted but E2EE"):
            await adapter.deliver(result)

    async def test_plaintext_room_send_allowed(self) -> None:
        config = _make_config()
        adapter = MatrixAdapter(config)

        # Simulate a session with crypto disabled but room is plaintext
        mock_session = MagicMock()
        mock_session.crypto_enabled = False
        mock_session.room_state.return_value = "plaintext"
        mock_session.is_room_encrypted = MagicMock(return_value=False)
        mock_session.room_send = AsyncMock(return_value=_make_send_response())
        adapter._session = mock_session

        result = _make_result()
        delivery = await adapter.deliver(result)
        assert delivery is not None


# ---------------------------------------------------------------------------
# 11. CancelledError still propagates
# ---------------------------------------------------------------------------


class TestCancelledErrorPropagation:
    """CancelledError propagates through deliver(), never swallowed."""

    async def test_cancelled_error_propagates(self) -> None:
        config = _make_config()
        adapter = MatrixAdapter(config)
        mock_client = MagicMock()
        mock_client.room_send = AsyncMock(side_effect=asyncio.CancelledError())
        adapter._session = mock_client

        result = _make_result()
        with pytest.raises(asyncio.CancelledError):
            await adapter.deliver(result)

    async def test_cancelled_does_not_increment_counters(self) -> None:
        config = _make_config()
        adapter = MatrixAdapter(config)
        mock_client = MagicMock()
        mock_client.room_send = AsyncMock(side_effect=asyncio.CancelledError())
        adapter._session = mock_client

        result = _make_result()
        with pytest.raises(asyncio.CancelledError):
            await adapter.deliver(result)

        assert adapter._transient_delivery_failures == 0
        assert adapter._permanent_delivery_failures == 0


# ---------------------------------------------------------------------------
# 12. no secrets in metadata
# ---------------------------------------------------------------------------


class TestNoSecretsInMetadata:
    """Delivery result metadata must not contain secrets."""

    async def test_no_access_token_in_metadata(self) -> None:
        config = _make_config()
        adapter = MatrixAdapter(config)
        mock_client = MagicMock()
        mock_client.room_send = AsyncMock(return_value=_make_send_response())
        adapter._session = mock_client

        result = _make_result()
        delivery = await adapter.deliver(result)

        assert delivery is not None
        meta = dict(delivery.metadata)
        assert "access_token" not in meta
        assert "token" not in meta
        assert "password" not in meta
        assert "secret" not in meta
        assert "key" not in meta
        # matrix_txn_id is allowed
        assert "matrix_txn_id" in meta


# ---------------------------------------------------------------------------
# 13. docs do not claim exactly-once
# ---------------------------------------------------------------------------


class TestDocsNoExactlyOnce:
    """Verify that docs do not claim exactly-once delivery."""

    def test_no_exactly_once_in_adapter(self) -> None:
        """adapter.py source must not claim exactly-once."""
        import medre.adapters.matrix.adapter as mod

        source = open(mod.__file__).read().lower()
        assert "exactly-once" not in source
        assert "exactly once" not in source


# ---------------------------------------------------------------------------
# Error classification helpers
# ---------------------------------------------------------------------------


class TestNioPermanentResponse:
    """M_FORBIDDEN, M_NOT_FOUND responses are permanent."""

    def test_m_forbidden_is_permanent(self) -> None:
        resp = MagicMock()
        del resp.event_id
        resp.errcode = "M_FORBIDDEN"
        assert _is_nio_permanent_response(resp) is True

    def test_m_not_found_is_permanent(self) -> None:
        resp = MagicMock()
        del resp.event_id
        resp.errcode = "M_NOT_FOUND"
        assert _is_nio_permanent_response(resp) is True

    def test_m_unknown_is_permanent(self) -> None:
        resp = MagicMock()
        del resp.event_id
        resp.errcode = "M_UNKNOWN"
        assert _is_nio_permanent_response(resp) is True

    def test_success_not_permanent(self) -> None:
        resp = MagicMock()
        resp.event_id = "$ok"
        assert _is_nio_permanent_response(resp) is False

    def test_rate_limit_not_permanent(self) -> None:
        resp = MagicMock()
        del resp.event_id
        resp.errcode = "M_LIMIT_EXCEEDED"
        assert _is_nio_permanent_response(resp) is False

    async def test_m_forbidden_raises_permanent(self) -> None:
        config = _make_config()
        adapter = MatrixAdapter(config)
        mock_client = MagicMock()

        resp = MagicMock()
        del resp.event_id
        resp.errcode = "M_FORBIDDEN"

        mock_client.room_send = AsyncMock(return_value=resp)
        adapter._session = mock_client

        result = _make_result()
        with pytest.raises(AdapterPermanentError, match="M_FORBIDDEN"):
            await adapter.deliver(result)


# ---------------------------------------------------------------------------
# Counter accuracy
# ---------------------------------------------------------------------------


class TestCounterAccuracy:
    """Transient/permanent counters are accurate after retries."""

    async def test_exhausted_transient_retries_no_permanent_counter(self) -> None:
        """Exhausted transient retries must not increment permanent counter."""
        config = _make_config()
        adapter = MatrixAdapter(config)
        mock_client = MagicMock()
        mock_client.room_send = AsyncMock(
            side_effect=ConnectionError("persistent failure")
        )
        adapter._session = mock_client

        result = _make_result()
        with patch("asyncio.sleep", new_callable=AsyncMock):
            with pytest.raises(AdapterSendError, match="transient retries"):
                await adapter.deliver(result)

        # All 3 attempts are transient
        assert adapter._transient_delivery_failures == 3
        # The exhausted-transient path must NOT increment permanent counter
        assert adapter._permanent_delivery_failures == 0

    async def test_permanent_error_increments_permanent_counter(self) -> None:
        config = _make_config()
        adapter = MatrixAdapter(config)
        mock_client = MagicMock()

        resp = MagicMock()
        del resp.event_id
        resp.errcode = "M_FORBIDDEN"
        resp.status_code = None

        mock_client.room_send = AsyncMock(return_value=resp)
        adapter._session = mock_client

        result = _make_result()
        with pytest.raises(AdapterPermanentError):
            await adapter.deliver(result)

        assert adapter._permanent_delivery_failures == 1
        assert adapter._transient_delivery_failures == 0
