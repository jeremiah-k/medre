"""Real nio crypto-store ownership at the MEDRE lifecycle boundary."""

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

import medre.adapters.matrix.session as session_module
from medre.adapters.matrix.session import MatrixSession
from tests.helpers.async_utils import wait_until
from tests.helpers.matrix_session import make_matrix_config
from tests.helpers.sdk_contract import assert_installed_extra_matches_declared_pins


@pytest.mark.matrix_sdk
async def test_session_stop_closes_real_crypto_database(tmp_path: Path) -> None:
    nio = pytest.importorskip("nio")
    crypto = pytest.importorskip("nio.crypto")
    if not crypto.ENCRYPTION_ENABLED:
        pytest.skip("Matrix E2EE extra is required")
    assert_installed_extra_matches_declared_pins("matrix-e2e", ("mindroom-nio",))
    config = make_matrix_config()
    client = nio.AsyncClient(
        config.homeserver,
        config.user_id,
        device_id="BENCH",
        store_path=str(tmp_path),
        config=nio.AsyncClientConfig(encryption_enabled=True),
    )
    try:
        client.restore_login(config.user_id, "BENCH", config.access_token)
        identity_keys = client.olm.account.identity_keys
        database = client.store.database
        assert not database.is_closed()
        session = MatrixSession(config)
        session._client = client
        await session.stop()
        assert database.is_closed(), (
            "MEDRE released the client with its crypto database open"
        )
        await session.stop()
    finally:
        try:
            await client.close()
        finally:
            if client.store is not None and not client.store.database.is_closed():
                client.store.database.close()

    reopened = nio.AsyncClient(
        config.homeserver,
        config.user_id,
        device_id="BENCH",
        store_path=str(tmp_path),
        config=nio.AsyncClientConfig(encryption_enabled=True),
    )
    try:
        reopened.restore_login(config.user_id, "BENCH", config.access_token)
        session._client = reopened
        assert reopened.olm.account.identity_keys == identity_keys
        await session.stop()
        assert reopened.store.database.is_closed()
    finally:
        try:
            await reopened.close()
        finally:
            if reopened.store is not None and not reopened.store.database.is_closed():
                reopened.store.database.close()


@pytest.mark.matrix_sdk
@pytest.mark.parametrize("cancel_phase", ["close_wait", "post_deadline_yield"])
async def test_cancelled_stop_retry_retains_store_until_recovery_drains(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cancel_phase: str,
) -> None:
    nio = pytest.importorskip("nio")
    crypto = pytest.importorskip("nio.crypto")
    if not crypto.ENCRYPTION_ENABLED:
        pytest.skip("Matrix E2EE extra is required")
    assert_installed_extra_matches_declared_pins("matrix-e2e", ("mindroom-nio",))
    config = make_matrix_config()
    client = nio.AsyncClient(
        config.homeserver,
        config.user_id,
        device_id="BENCH",
        store_path=str(tmp_path),
        config=nio.AsyncClientConfig(
            encryption_enabled=True, backfill_limited_timelines=True,
        ),
    )
    release = asyncio.Event()
    started = asyncio.Event()

    async def callback() -> None:
        started.set()
        await release.wait()

    recovery = asyncio.create_task(callback())
    # Pin the SDK ownership seam: close must finish an active recovery
    # callback even when its caller is cancelled. No homeserver is needed.
    client._recovery._active_dispatches[("!room", "event", "timeline")] = recovery
    session = MatrixSession(config)
    stop_task = None
    yielded = asyncio.Event()

    async def stop_yield(delay: float) -> None:
        if asyncio.current_task() is stop_task:
            yielded.set()
            await asyncio.Event().wait()
        else:
            await asyncio.sleep(delay)

    if cancel_phase == "post_deadline_yield":
        # Intercept only this module's final teardown yield, leaving the SDK
        # and test runner's asyncio module untouched.
        monkeypatch.setattr(
            session_module, "asyncio",
            SimpleNamespace(**(vars(asyncio) | {"sleep": stop_yield})),
        )
    try:
        client.restore_login(config.user_id, "BENCH", config.access_token)
        database = client.store.database
        session._client = client
        await asyncio.wait_for(started.wait(), 1)
        timeout = 0.03 if cancel_phase == "post_deadline_yield" else 5.0
        stop_task = asyncio.create_task(session.stop(timeout=timeout))
        assert await wait_until(client._sync_response_lock.locked, timeout=1)
        if cancel_phase == "post_deadline_yield":
            await asyncio.wait_for(yielded.wait(), 1)
        stop_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await stop_task
        assert not database.is_closed()

        await session.stop(timeout=0.03)
        assert not recovery.done(), "shutdown retry cancelled active recovery"
        assert not database.is_closed(), "store closed before recovery settled"
        release.set()
        assert await wait_until(database.is_closed, timeout=1)
    finally:
        release.set()
        await asyncio.gather(recovery, return_exceptions=True)
        if stop_task is not None:
            await asyncio.gather(stop_task, return_exceptions=True)
        await session.stop()
        await client.close()
        if client.store is not None and not client.store.database.is_closed():
            client.store.database.close()
