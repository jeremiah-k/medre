"""Matrix cross-signing identity policy.

This module contains MEDRE's policy around the provider-owned cross-signing
identity.  It intentionally sits beside, rather than inside, the generic
Matrix session lifecycle so that identity rotation, repair, and diagnostics
remain explicit adapter concerns.

The active provider is treated as a capability boundary.  mindroom-nio 0.40
supplies the producer implementation (``ensure_cross_signing`` and the
persisted ``cross_signing_identity``); MEDRE decides when that producer may be
used and verifies the server-visible master -> self-signing -> current-device
chain before reporting success.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Protocol, cast

__all__ = [
    "MatrixCrossSigningDiagnostics",
    "MatrixCrossSigningService",
]

_OPERATION_TIMEOUT_SECONDS: float = 120.0
_VALID_PROVIDER_RESULTS: frozenset[str] = frozenset(
    {"already_signed", "device_signed", "uploaded_and_signed"}
)


class _MatrixHttpResponse(Protocol):
    status: int

    async def json(self, *, content_type: object = None) -> object: ...

    async def text(self) -> str: ...


class _VerifiableIdentity(Protocol):
    master_public_key: str
    self_signing_public_key: str

    def self_signing_key_payload(self) -> dict[str, object]: ...

    def signed_device_payload(
        self, device_keys: dict[str, object]
    ) -> dict[str, object]: ...


_SendRequest = Callable[[str, str, str, dict[str, str]], Awaitable[_MatrixHttpResponse]]
_EnsureCrossSigning = Callable[..., Awaitable[str]]
_UploadDeviceSignature = Callable[[object], Awaitable[None]]


@dataclass(frozen=True)
class MatrixCrossSigningDiagnostics:
    """Secret-free snapshot of MEDRE's Matrix identity state."""

    provider_supported: bool = False
    local_identity_present: bool = False
    server_identity_present: bool | None = None
    current_device_self_signed: bool | None = None
    chain_status: str = "unchecked"
    repair_required: bool = False
    reset_required: bool = False
    last_failure_category: str | None = None


@dataclass(frozen=True)
class _ServerChainCheck:
    status: str
    reason: str
    server_identity_present: bool
    current_device_self_signed: bool | None


class MatrixCrossSigningService:
    """Policy owner for one Matrix client's cross-signing identity.

    Ordinary runtime reconciliation never creates or rotates a missing
    identity.  Authenticated setup may opt into bootstrap, and identity
    rotation requires the explicit ``reset=True`` path plus a fresh password.
    """

    def __init__(
        self,
        client: object,
        *,
        logger: logging.Logger | None = None,
        operation_timeout_seconds: float = _OPERATION_TIMEOUT_SECONDS,
    ) -> None:
        self._client = client
        self._logger = logger or logging.getLogger(__name__)
        self._timeout = operation_timeout_seconds
        self._diagnostics = MatrixCrossSigningDiagnostics()

    def diagnostics(self) -> MatrixCrossSigningDiagnostics:
        return self._diagnostics

    async def reconcile(
        self,
        *,
        password: str | None = None,
        allow_bootstrap: bool = False,
        reset: bool = False,
    ) -> str | None:
        """Reconcile local and server-visible own-device cross-signing state.

        ``allow_bootstrap`` permits creation only when the homeserver has no
        existing master identity.  ``reset`` is stronger: it permits replacing
        server identity material and therefore requires ``password``.

        Expected provider/server failures are reflected in diagnostics and
        return ``None``.  Cancellation always propagates.
        """
        self._diagnostics = MatrixCrossSigningDiagnostics()
        try:
            async with asyncio.timeout(self._timeout):
                return await self._reconcile(
                    password=password,
                    allow_bootstrap=allow_bootstrap,
                    reset=reset,
                )
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            self._fail("operation_timeout", chain_status="error")
            self._logger.warning(
                "Matrix cross-signing reconciliation timed out after %.0f seconds",
                self._timeout,
            )
            return None
        except Exception as exc:
            self._fail("provider_error", chain_status="error")
            self._logger.warning("Matrix cross-signing reconciliation failed: %s", exc)
            self._logger.debug("Matrix cross-signing failure", exc_info=True)
            return None

    async def _reconcile(
        self,
        *,
        password: str | None,
        allow_bootstrap: bool,
        reset: bool,
    ) -> str | None:
        provider = self._inspect_provider()
        if provider is None:
            return None
        ensure_method, local_identity = provider

        self._diagnostics = replace(
            self._diagnostics,
            provider_supported=True,
            local_identity_present=local_identity is not None,
        )

        payload = await self._query_own_keys()
        server_has_identity = self._server_has_identity(payload)
        self._diagnostics = replace(
            self._diagnostics,
            server_identity_present=server_has_identity,
        )

        if reset:
            if not password:
                self._fail(
                    "reset_requires_password",
                    chain_status="reset_required",
                    reset_required=True,
                )
                return None
            return await self._reset_identity(
                ensure_method,
                password=password,
            )

        if local_identity is None:
            if server_has_identity:
                self._fail(
                    "local_identity_missing",
                    chain_status="reset_required",
                    reset_required=True,
                )
                self._logger.warning(
                    "Matrix has an existing cross-signing identity for %s but the "
                    "local identity sidecar is unavailable; preserving the server "
                    "identity. Restore the Matrix E2EE state or use the explicit "
                    "authenticated reset command.",
                    self._client_label("user_id"),
                )
                return None
            if not allow_bootstrap:
                self._fail(
                    "bootstrap_required",
                    chain_status="missing",
                    repair_required=True,
                )
                return None
            return await self._bootstrap_and_verify(
                ensure_method,
                password=password,
            )

        if server_has_identity:
            check = self._check_server_chain(payload, local_identity)
            return await self._apply_chain_check(check, local_identity)

        # Local identity exists but the homeserver does not expose a master
        # key. Re-uploading the persisted identity requires user-interactive
        # authentication (uploading master/self-signing keys is a UIA
        # operation), so it belongs to the authenticated bootstrap path only.
        # Runtime reconciliation reports the actionable state instead.
        if not allow_bootstrap:
            self._fail(
                "bootstrap_required",
                chain_status="missing",
                repair_required=True,
            )
            return None
        return await self._bootstrap_and_verify(
            ensure_method,
            password=password,
            existing_identity=True,
        )

    def _inspect_provider(self) -> tuple[_EnsureCrossSigning, object | None] | None:
        try:
            ensure_method = getattr(self._client, "ensure_cross_signing", None)
        except Exception as exc:
            self._fail("provider_inspection_failed", chain_status="error")
            self._logger.warning(
                "Could not inspect Matrix cross-signing provider support: %s", exc
            )
            return None
        if not callable(ensure_method):
            self._diagnostics = replace(
                self._diagnostics,
                provider_supported=False,
                chain_status="unsupported",
                last_failure_category="provider_unsupported",
            )
            return None
        try:
            identity = getattr(self._client, "cross_signing_identity", None)
        except Exception as exc:
            self._fail("local_identity_unreadable", chain_status="error")
            self._logger.warning(
                "Could not inspect the local Matrix cross-signing identity: %s", exc
            )
            return None
        return cast(_EnsureCrossSigning, ensure_method), identity

    async def _query_own_keys(self) -> dict[str, object]:
        user_id = getattr(self._client, "user_id", None)
        device_id = getattr(self._client, "device_id", None)
        access_token = getattr(self._client, "access_token", None)
        send = getattr(self._client, "send", None)
        if not isinstance(user_id, str) or not user_id:
            raise RuntimeError("Matrix user id unavailable for cross-signing query")
        if not isinstance(device_id, str) or not device_id:
            raise RuntimeError("Matrix device id unavailable for cross-signing query")
        if not isinstance(access_token, str) or not access_token:
            raise RuntimeError("Matrix access token unavailable for cross-signing query")
        if not callable(send):
            raise RuntimeError("Matrix provider lacks authenticated send()")

        response = await cast(_SendRequest, send)(
            "POST",
            "/_matrix/client/v3/keys/query",
            json.dumps(
                {"device_keys": {user_id: [device_id]}}, separators=(",", ":")
            ),
            {
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json",
            },
        )
        if response.status != 200:
            raise RuntimeError(f"Matrix keys/query failed with HTTP {response.status}")
        try:
            payload = await response.json(content_type=None)
        except (TypeError, ValueError) as exc:
            raise RuntimeError("Matrix keys/query returned invalid JSON") from exc
        if not isinstance(payload, dict):
            raise RuntimeError("Matrix keys/query returned a non-object response")
        failures = payload.get("failures")
        if isinstance(failures, dict) and failures:
            raise RuntimeError("Matrix keys/query reported homeserver failures")
        return cast(dict[str, object], payload)

    def _server_has_identity(self, payload: dict[str, object]) -> bool:
        user_id = self._required_client_string("user_id")
        master = _nested_dict(payload, "master_keys", user_id)
        return master is not None

    def _check_server_chain(
        self,
        payload: dict[str, object],
        identity: object,
    ) -> _ServerChainCheck:
        verifiable = _verifiable_identity(identity)
        if verifiable is None:
            return _ServerChainCheck(
                status="unverifiable",
                reason="provider identity lacks the verification surface",
                server_identity_present=True,
                current_device_self_signed=None,
            )

        user_id = self._required_client_string("user_id")
        device_id = self._required_client_string("device_id")
        master_public = verifiable.master_public_key
        self_signing_public = verifiable.self_signing_public_key
        master_key_id = f"ed25519:{master_public}"
        self_signing_key_id = f"ed25519:{self_signing_public}"

        master = _nested_dict(payload, "master_keys", user_id)
        if (
            master is None
            or master.get("user_id") != user_id
            or master.get("usage") != ["master"]
            or _nested_dict(master, "keys") != {master_key_id: master_public}
        ):
            return _ServerChainCheck(
                "mismatch",
                "server master key differs from the persisted identity",
                True,
                False,
            )

        self_signing = _nested_dict(payload, "self_signing_keys", user_id)
        if (
            self_signing is None
            or self_signing.get("user_id") != user_id
            or self_signing.get("usage") != ["self_signing"]
            or _nested_dict(self_signing, "keys")
            != {self_signing_key_id: self_signing_public}
        ):
            return _ServerChainCheck(
                "mismatch",
                "server self-signing key differs from the persisted identity",
                True,
                False,
            )

        expected_self_signing = verifiable.self_signing_key_payload()
        expected_master_signature = _signature_value(
            expected_self_signing,
            user_id=user_id,
            key_id=master_key_id,
        )
        observed_master_signature = _signature_value(
            self_signing,
            user_id=user_id,
            key_id=master_key_id,
        )
        if (
            expected_master_signature is None
            or observed_master_signature != expected_master_signature
        ):
            return _ServerChainCheck(
                "mismatch",
                "server self-signing key has an unexpected master signature",
                True,
                False,
            )

        device = _nested_dict(payload, "device_keys", user_id, device_id)
        if device is None:
            return _ServerChainCheck(
                "repairable",
                "server has no keys for the current device",
                True,
                False,
            )
        if device.get("user_id") != user_id or device.get("device_id") != device_id:
            return _ServerChainCheck(
                "mismatch",
                "server returned inconsistent current-device keys",
                True,
                False,
            )

        signable_device = {
            key: value
            for key, value in device.items()
            if key not in ("signatures", "unsigned")
        }
        expected_signed_device = verifiable.signed_device_payload(signable_device)
        expected_device_signature = _signature_value(
            expected_signed_device,
            user_id=user_id,
            key_id=self_signing_key_id,
        )
        observed_device_signature = _signature_value(
            device,
            user_id=user_id,
            key_id=self_signing_key_id,
        )
        if (
            expected_device_signature is None
            or observed_device_signature != expected_device_signature
        ):
            return _ServerChainCheck(
                "repairable",
                "current device lacks the expected owner self-signing signature",
                True,
                False,
            )
        return _ServerChainCheck(
            "valid",
            "server-visible cross-signing chain is valid",
            True,
            True,
        )

    async def _apply_chain_check(
        self,
        check: _ServerChainCheck,
        identity: object,
    ) -> str | None:
        self._diagnostics = replace(
            self._diagnostics,
            server_identity_present=check.server_identity_present,
            current_device_self_signed=check.current_device_self_signed,
            chain_status=check.status,
        )
        if check.status == "valid":
            self._diagnostics = replace(
                self._diagnostics,
                repair_required=False,
                reset_required=False,
                last_failure_category=None,
            )
            return "already_signed"
        if check.status == "unverifiable":
            self._fail("verification_unavailable", chain_status="unverifiable")
            return None
        if check.status == "mismatch":
            self._fail(
                "identity_mismatch",
                chain_status="mismatch",
                reset_required=True,
            )
            self._logger.warning(
                "Matrix cross-signing identity mismatch for device %s; refusing "
                "automatic master/self-signing rotation",
                self._client_label("device_id"),
            )
            return None

        self._diagnostics = replace(
            self._diagnostics,
            chain_status="repairable",
            repair_required=True,
            last_failure_category="device_signature_missing",
        )
        if not await self._repair_device_signature(identity):
            return None
        payload = await self._query_own_keys()
        repaired = self._check_server_chain(payload, identity)
        if repaired.status != "valid":
            self._fail(
                "device_signature_repair_failed",
                chain_status=repaired.status,
                repair_required=True,
            )
            return None
        self._diagnostics = replace(
            self._diagnostics,
            current_device_self_signed=True,
            chain_status="valid",
            repair_required=False,
            reset_required=False,
            last_failure_category=None,
        )
        return "device_signed"

    async def _repair_device_signature(self, identity: object) -> bool:
        upload = getattr(self._client, "_upload_own_device_signature", None)
        if not callable(upload):
            self._fail(
                "signature_repair_unsupported",
                chain_status="repairable",
                repair_required=True,
            )
            return False
        try:
            await cast(_UploadDeviceSignature, upload)(identity)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._fail(
                "device_signature_repair_failed",
                chain_status="repairable",
                repair_required=True,
            )
            self._logger.warning(
                "Could not repair Matrix self-signing for device %s: %s",
                self._client_label("device_id"),
                exc,
            )
            return False
        return True

    async def _bootstrap_and_verify(
        self,
        ensure_method: _EnsureCrossSigning,
        *,
        password: str | None,
        existing_identity: bool = False,
    ) -> str | None:
        try:
            result = await ensure_method(password=password)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._fail(
                "bootstrap_failed" if not existing_identity else "identity_repair_failed",
                chain_status="error",
                repair_required=True,
            )
            self._logger.warning(
                "Could not establish Matrix cross-signing for device %s: %s",
                self._client_label("device_id"),
                exc,
            )
            return None
        if result not in _VALID_PROVIDER_RESULTS:
            self._fail("unexpected_provider_result", chain_status="error")
            return None

        identity = getattr(self._client, "cross_signing_identity", None)
        if identity is None:
            self._fail("local_identity_missing_after_bootstrap", chain_status="error")
            return None
        self._diagnostics = replace(
            self._diagnostics,
            local_identity_present=True,
        )
        payload = await self._query_own_keys()
        check = self._check_server_chain(payload, identity)
        verified_result = await self._apply_chain_check(check, identity)
        return result if verified_result is not None else None

    async def _reset_identity(
        self,
        ensure_method: _EnsureCrossSigning,
        *,
        password: str,
    ) -> str | None:
        sidecar = self._provider_sidecar_path()
        if sidecar is None:
            self._fail(
                "reset_unsupported",
                chain_status="reset_required",
                reset_required=True,
            )
            return None

        backup = sidecar.with_name(f"{sidecar.name}.pre-reset")
        if backup.exists():
            self._fail(
                "reset_backup_exists",
                chain_status="reset_required",
                reset_required=True,
            )
            self._logger.warning(
                "Refusing Matrix cross-signing reset because a prior reset backup "
                "already exists in the E2EE store"
            )
            return None

        had_sidecar = sidecar.exists()
        if had_sidecar:
            os.replace(sidecar, backup)
        previous_identity = getattr(self._client, "cross_signing_identity", None)

        try:
            result = await ensure_method(password=password)
        except asyncio.CancelledError:
            self._handle_failed_reset(
                sidecar, backup, had_sidecar, previous_identity=previous_identity
            )
            raise
        except Exception as exc:
            self._handle_failed_reset(
                sidecar, backup, had_sidecar, previous_identity=previous_identity
            )
            self._fail(
                "reset_failed",
                chain_status="reset_required",
                reset_required=True,
            )
            self._logger.warning("Authenticated Matrix cross-signing reset failed: %s", exc)
            return None

        if result not in _VALID_PROVIDER_RESULTS:
            self._handle_failed_reset(
                sidecar, backup, had_sidecar, previous_identity=previous_identity
            )
            self._fail(
                "unexpected_provider_result",
                chain_status="reset_required",
                reset_required=True,
            )
            return None

        identity = getattr(self._client, "cross_signing_identity", None)
        if identity is None:
            self._handle_failed_reset(
                sidecar, backup, had_sidecar, previous_identity=previous_identity
            )
            self._fail(
                "local_identity_missing_after_reset",
                chain_status="reset_required",
                reset_required=True,
            )
            return None

        payload = await self._query_own_keys()
        check = self._check_server_chain(payload, identity)
        verified = await self._apply_chain_check(check, identity)
        if verified is None:
            # If the new provider identity was uploaded, restoring the old
            # sidecar would knowingly create a local/server mismatch.  Keep the
            # new material and remove the obsolete backup instead.
            uploaded = bool(getattr(identity, "uploaded", False))
            if uploaded:
                backup.unlink(missing_ok=True)
            else:
                self._restore_reset_backup(sidecar, backup, had_sidecar)
            return None

        backup.unlink(missing_ok=True)
        self._diagnostics = replace(
            self._diagnostics,
            local_identity_present=True,
            server_identity_present=True,
            current_device_self_signed=True,
            chain_status="valid",
            repair_required=False,
            reset_required=False,
            last_failure_category=None,
        )
        self._logger.warning(
            "Replaced Matrix cross-signing identity for %s after explicit "
            "password-authenticated reset",
            self._client_label("user_id"),
        )
        return result

    def _handle_failed_reset(
        self,
        sidecar: Path,
        backup: Path,
        had_sidecar: bool,
        *,
        previous_identity: object | None,
    ) -> None:
        identity = None
        try:
            identity = getattr(self._client, "cross_signing_identity", None)
        except Exception:
            pass
        if (
            identity is not None
            and identity is not previous_identity
            and bool(getattr(identity, "uploaded", False))
        ):
            backup.unlink(missing_ok=True)
            return
        self._restore_reset_backup(sidecar, backup, had_sidecar)

    @staticmethod
    def _restore_reset_backup(sidecar: Path, backup: Path, had_sidecar: bool) -> None:
        if sidecar.exists():
            sidecar.unlink()
        if had_sidecar and backup.exists():
            os.replace(backup, sidecar)
        else:
            backup.unlink(missing_ok=True)

    def _provider_sidecar_path(self) -> Path | None:
        store_path = getattr(self._client, "store_path", None)
        user_id = getattr(self._client, "user_id", None)
        if not isinstance(store_path, str) or not store_path:
            return None
        if not isinstance(user_id, str) or not user_id:
            return None
        try:
            from nio.crypto.cross_signing import cross_signing_sidecar_path
        except (ImportError, AttributeError):
            return None
        try:
            return Path(cross_signing_sidecar_path(store_path, user_id))
        except (OSError, TypeError, ValueError):
            return None

    def _required_client_string(self, attribute: str) -> str:
        value = getattr(self._client, attribute, None)
        if not isinstance(value, str) or not value:
            raise RuntimeError(f"Matrix client {attribute} is unavailable")
        return value

    def _client_label(self, attribute: str) -> str:
        try:
            value = getattr(self._client, attribute, None)
        except Exception:
            return "<unknown>"
        return str(value) if value else "<unknown>"

    def _fail(
        self,
        category: str,
        *,
        chain_status: str,
        repair_required: bool | None = None,
        reset_required: bool | None = None,
    ) -> None:
        updates: dict[str, object] = {
            "chain_status": chain_status,
            "last_failure_category": category,
        }
        if repair_required is not None:
            updates["repair_required"] = repair_required
        if reset_required is not None:
            updates["reset_required"] = reset_required
        self._diagnostics = replace(self._diagnostics, **updates)


def _nested_dict(container: object, *keys: str) -> dict[str, object] | None:
    current = container
    for key in keys:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return cast(dict[str, object], current) if isinstance(current, dict) else None


def _signature_value(
    payload: dict[str, object] | None,
    *,
    user_id: str,
    key_id: str,
) -> str | None:
    signatures = _nested_dict(payload, "signatures", user_id)
    if signatures is None:
        return None
    value = signatures.get(key_id)
    return value if isinstance(value, str) and value else None


def _verifiable_identity(identity: object) -> _VerifiableIdentity | None:
    try:
        master_public = getattr(identity, "master_public_key", None)
        self_signing_public = getattr(identity, "self_signing_public_key", None)
        self_signing_payload = getattr(identity, "self_signing_key_payload", None)
        signed_device_payload = getattr(identity, "signed_device_payload", None)
    except Exception:
        return None
    if not (
        isinstance(master_public, str)
        and master_public
        and isinstance(self_signing_public, str)
        and self_signing_public
        and callable(self_signing_payload)
        and callable(signed_device_payload)
    ):
        return None
    return cast(_VerifiableIdentity, identity)
