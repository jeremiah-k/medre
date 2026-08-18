"""Runtime cross-signing reconciliation behavior at the session boundary.

Covers how ``MatrixSession`` integrates ``MatrixCrossSigningService``
during ordinary runtime startup and stop:

- E2EE runtime start reconciles cross-signing without bootstrap/reset
  authority (no password, no rotation).
- An identity mismatch is non-fatal at boot and surfaces as
  diagnostics with ``reset_required``.
- Plaintext runtime never constructs the cross-signing service.
- Stop releases the service while retaining the last known
  cross-signing diagnostics.

Policy and authenticated-bootstrap coverage live in
``test_matrix_cross_signing_identity.py`` and
``test_matrix_cross_signing_auth.py``. General session lifecycle tests
live in ``test_matrix_session.py``.

No test requires mindroom-nio[e2e].
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

from medre.adapters.matrix.adapter import MatrixAdapter  # noqa: F401
from medre.adapters.matrix.identity import MatrixCrossSigningDiagnostics
from medre.adapters.matrix.session import MatrixSession
from tests.helpers.matrix_session import (
    make_matrix_config,
)
from tests.helpers.matrix_session import mock_nio as _mock_nio  # noqa: F401


async def test_e2ee_runtime_reconciles_cross_signing_without_rotation(
    mock_nio,
) -> None:
    """Runtime verification never supplies bootstrap/reset authority."""
    import medre.adapters.matrix.compat as compat

    diagnostics = MatrixCrossSigningDiagnostics(
        provider_supported=True,
        local_identity_present=True,
        server_identity_present=True,
        current_device_self_signed=True,
        chain_status="valid",
    )
    service = MagicMock()
    service.reconcile = AsyncMock(return_value="already_signed")
    service.diagnostics.return_value = diagnostics

    original = compat.HAS_E2EE
    try:
        compat.HAS_E2EE = True
        config = make_matrix_config(
            encryption_mode="e2ee_required",
            store_path="/tmp/store",
            device_id="DEV",
        )
        with patch(
            "medre.adapters.matrix.session.MatrixCrossSigningService",
            return_value=service,
        ) as service_cls:
            session = MatrixSession(config)
            try:
                await session.start()
                service_cls.assert_called_once_with(
                    session._client, logger=session._logger
                )
                service.reconcile.assert_awaited_once_with()
                diag = session.diagnostics()
                assert diag.cross_signing_chain_status == "valid"
                assert diag.cross_signing_current_device_self_signed is True
            finally:
                await session.stop()
    finally:
        compat.HAS_E2EE = original


async def test_e2ee_runtime_identity_mismatch_is_nonfatal(mock_nio) -> None:
    """An identity mismatch requires auth recovery but does not rotate at boot."""
    import medre.adapters.matrix.compat as compat

    diagnostics = MatrixCrossSigningDiagnostics(
        provider_supported=True,
        local_identity_present=True,
        server_identity_present=True,
        current_device_self_signed=False,
        chain_status="mismatch",
        reset_required=True,
        last_failure_category="identity_mismatch",
    )
    service = MagicMock()
    service.reconcile = AsyncMock(return_value=None)
    service.diagnostics.return_value = diagnostics

    original = compat.HAS_E2EE
    try:
        compat.HAS_E2EE = True
        config = make_matrix_config(
            encryption_mode="e2ee_required",
            store_path="/tmp/store",
            device_id="DEV",
        )
        with patch(
            "medre.adapters.matrix.session.MatrixCrossSigningService",
            return_value=service,
        ):
            session = MatrixSession(config)
            try:
                await session.start()
                assert session.connected is True
                diag = session.diagnostics()
                assert diag.cross_signing_chain_status == "mismatch"
                assert diag.cross_signing_reset_required is True
                assert (
                    diag.cross_signing_last_failure_category
                    == "identity_mismatch"
                )
            finally:
                await session.stop()
    finally:
        compat.HAS_E2EE = original


async def test_plaintext_runtime_does_not_create_cross_signing_service(
    mock_nio,
) -> None:
    config = make_matrix_config(encryption_mode="plaintext")
    with patch(
        "medre.adapters.matrix.session.MatrixCrossSigningService"
    ) as service_cls:
        session = MatrixSession(config)
        try:
            await session.start()
            service_cls.assert_not_called()
            assert session.diagnostics().cross_signing_chain_status == "unchecked"
        finally:
            await session.stop()


async def test_e2ee_stop_releases_service_and_retains_diagnostics(
    mock_nio,
) -> None:
    import medre.adapters.matrix.compat as compat

    diagnostics = MatrixCrossSigningDiagnostics(
        provider_supported=True,
        local_identity_present=True,
        server_identity_present=True,
        current_device_self_signed=True,
        chain_status="valid",
    )
    service = MagicMock()
    service.reconcile = AsyncMock(return_value="already_signed")
    service.diagnostics.return_value = diagnostics

    original = compat.HAS_E2EE
    try:
        compat.HAS_E2EE = True
        config = make_matrix_config(
            encryption_mode="e2ee_required",
            store_path="/tmp/store",
            device_id="DEV",
        )
        with patch(
            "medre.adapters.matrix.session.MatrixCrossSigningService",
            return_value=service,
        ):
            session = MatrixSession(config)
            await session.start()
            await session.stop()
            assert session._cross_signing_service is None
            assert session.diagnostics().cross_signing_chain_status == "valid"
    finally:
        compat.HAS_E2EE = original
