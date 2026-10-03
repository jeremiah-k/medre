"""Real nio crypto-store ownership at the MEDRE lifecycle boundary."""

from pathlib import Path

import pytest

from medre.adapters.matrix.session import MatrixSession
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
