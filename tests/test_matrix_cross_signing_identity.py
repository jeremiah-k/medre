"""Matrix cross-signing identity policy tests.

The suite uses a provider-shaped fake so MEDRE's policy is exercised without
requiring a live homeserver or importing nio.  SDK-specific compatibility is
covered separately by the installed-provider contract tests.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from medre.adapters.matrix.identity import MatrixCrossSigningService


class FakeResponse:
    def __init__(self, payload: object, *, status: int = 200) -> None:
        self.status = status
        self._payload = payload

    async def json(self, *, content_type: object = None) -> object:
        return self._payload

    async def text(self) -> str:
        return json.dumps(self._payload)


class FakeIdentity:
    master_public_key = "MASTER"
    self_signing_public_key = "SELF"

    def __init__(self, user_id: str, *, uploaded: bool = True) -> None:
        self.user_id = user_id
        self.uploaded = uploaded
        self.signed_devices: list[str] = []

    def self_signing_key_payload(self) -> dict[str, object]:
        return {
            "user_id": self.user_id,
            "usage": ["self_signing"],
            "keys": {"ed25519:SELF": "SELF"},
            "signatures": {self.user_id: {"ed25519:MASTER": "sig-master"}},
        }

    def signed_device_payload(
        self, device_keys: dict[str, object]
    ) -> dict[str, object]:
        payload = dict(device_keys)
        payload["signatures"] = {
            self.user_id: {"ed25519:SELF": "sig-device"}
        }
        return payload


class FakeClient:
    def __init__(
        self,
        *,
        identity: FakeIdentity | None,
        server_identity: bool,
        device_signed: bool = True,
    ) -> None:
        self.user_id = "@bot:example.com"
        self.device_id = "DEVICE"
        self.access_token = "secret-token"
        self.store_path = "/tmp/fake-matrix-store"
        self.cross_signing_identity = identity
        self.ensure_calls: list[str | None] = []
        self.repair_calls = 0
        self.ensure_error: BaseException | None = None
        self.server_payload = self._payload(
            identity if server_identity else None,
            device_signed=device_signed,
        )

    def _payload(
        self,
        identity: FakeIdentity | None,
        *,
        device_signed: bool,
    ) -> dict[str, object]:
        base_device: dict[str, object] = {
            "user_id": self.user_id,
            "device_id": self.device_id,
            "algorithms": ["m.megolm.v1.aes-sha2"],
            "keys": {
                f"curve25519:{self.device_id}": "curve",
                f"ed25519:{self.device_id}": "device-ed",
            },
            "signatures": {},
            "unsigned": {"device_display_name": "MEDRE"},
        }
        if identity is None:
            return {
                "device_keys": {self.user_id: {self.device_id: base_device}},
                "failures": {},
            }
        master = {
            "user_id": self.user_id,
            "usage": ["master"],
            "keys": {"ed25519:MASTER": "MASTER"},
            "signatures": {self.user_id: {"ed25519:MASTER": "master-self"}},
        }
        self_signing = identity.self_signing_key_payload()
        if device_signed:
            base_device = identity.signed_device_payload(
                {k: v for k, v in base_device.items() if k not in ("signatures", "unsigned")}
            ) | {"unsigned": {"device_display_name": "MEDRE"}}
        return {
            "master_keys": {self.user_id: master},
            "self_signing_keys": {self.user_id: self_signing},
            "device_keys": {self.user_id: {self.device_id: base_device}},
            "failures": {},
        }

    async def send(
        self,
        method: str,
        path: str,
        data: str,
        headers: dict[str, str],
    ) -> FakeResponse:
        assert method == "POST"
        assert path == "/_matrix/client/v3/keys/query"
        assert json.loads(data) == {
            "device_keys": {self.user_id: [self.device_id]}
        }
        assert headers["Authorization"] == f"Bearer {self.access_token}"
        return FakeResponse(self.server_payload)

    async def ensure_cross_signing(self, password: str | None = None) -> str:
        self.ensure_calls.append(password)
        if self.ensure_error is not None:
            raise self.ensure_error
        freshly_created = self.cross_signing_identity is None
        if freshly_created:
            self.cross_signing_identity = FakeIdentity(self.user_id, uploaded=True)
        identity = self.cross_signing_identity
        assert identity is not None
        identity.uploaded = True
        identity.signed_devices = [self.device_id]
        self.server_payload = self._payload(identity, device_signed=True)
        return "uploaded_and_signed" if freshly_created else "device_signed"

    async def _upload_own_device_signature(self, identity: FakeIdentity) -> None:
        self.repair_calls += 1
        self.server_payload = self._payload(identity, device_signed=True)


def _identity(*, uploaded: bool = True) -> FakeIdentity:
    identity = FakeIdentity("@bot:example.com", uploaded=uploaded)
    identity.signed_devices = ["DEVICE"]
    return identity


async def test_unsupported_provider_is_reported_without_failure() -> None:
    class UnsupportedClient:
        user_id = "@bot:example.com"
        device_id = "DEVICE"
        access_token = "secret-token"

    service = MatrixCrossSigningService(UnsupportedClient())
    assert await service.reconcile() is None
    diag = service.diagnostics()
    assert diag.provider_supported is False
    assert diag.chain_status == "unsupported"
    assert diag.last_failure_category == "provider_unsupported"


async def test_runtime_preserves_server_identity_when_local_sidecar_is_missing() -> None:
    client = FakeClient(identity=None, server_identity=False)
    client.server_payload = client._payload(_identity(), device_signed=True)
    service = MatrixCrossSigningService(client)

    assert await service.reconcile() is None

    diag = service.diagnostics()
    assert diag.provider_supported is True
    assert diag.local_identity_present is False
    assert diag.server_identity_present is True
    assert diag.reset_required is True
    assert diag.last_failure_category == "local_identity_missing"
    assert client.ensure_calls == []


async def test_runtime_does_not_bootstrap_when_server_and_local_identity_are_absent() -> None:
    client = FakeClient(identity=None, server_identity=False)
    service = MatrixCrossSigningService(client)

    assert await service.reconcile() is None

    diag = service.diagnostics()
    assert diag.server_identity_present is False
    assert diag.repair_required is True
    assert diag.reset_required is False
    assert diag.last_failure_category == "bootstrap_required"
    assert client.ensure_calls == []


async def test_authenticated_bootstrap_creates_and_verifies_identity() -> None:
    client = FakeClient(identity=None, server_identity=False)
    service = MatrixCrossSigningService(client)

    result = await service.reconcile(password="pw", allow_bootstrap=True)

    assert result == "uploaded_and_signed"
    assert client.ensure_calls == ["pw"]
    diag = service.diagnostics()
    assert diag.local_identity_present is True
    assert diag.server_identity_present is True
    assert diag.current_device_self_signed is True
    assert diag.chain_status == "valid"
    assert diag.repair_required is False
    assert diag.reset_required is False
    assert diag.last_failure_category is None


async def test_existing_valid_chain_is_idempotent() -> None:
    identity = _identity()
    client = FakeClient(identity=identity, server_identity=True, device_signed=True)
    service = MatrixCrossSigningService(client)

    assert await service.reconcile() == "already_signed"
    assert client.ensure_calls == []
    assert client.repair_calls == 0
    assert service.diagnostics().chain_status == "valid"


async def test_master_key_mismatch_refuses_automatic_rotation() -> None:
    identity = _identity()
    client = FakeClient(identity=identity, server_identity=True, device_signed=True)
    master = client.server_payload["master_keys"][client.user_id]  # type: ignore[index]
    master["keys"] = {"ed25519:OTHER": "OTHER"}  # type: ignore[index]
    service = MatrixCrossSigningService(client)

    assert await service.reconcile() is None

    diag = service.diagnostics()
    assert diag.chain_status == "mismatch"
    assert diag.reset_required is True
    assert diag.last_failure_category == "identity_mismatch"
    assert client.ensure_calls == []
    assert client.repair_calls == 0


async def test_missing_current_device_signature_is_repaired_without_rotation() -> None:
    identity = _identity()
    client = FakeClient(identity=identity, server_identity=True, device_signed=False)
    service = MatrixCrossSigningService(client)

    assert await service.reconcile() == "device_signed"

    assert client.ensure_calls == []
    assert client.repair_calls == 1
    diag = service.diagnostics()
    assert diag.current_device_self_signed is True
    assert diag.chain_status == "valid"
    assert diag.repair_required is False


async def test_missing_signature_without_provider_repair_hook_stays_repairable() -> None:
    identity = _identity()
    client = FakeClient(identity=identity, server_identity=True, device_signed=False)
    client._upload_own_device_signature = None  # type: ignore[assignment]
    service = MatrixCrossSigningService(client)

    assert await service.reconcile() is None

    diag = service.diagnostics()
    assert diag.chain_status == "repairable"
    assert diag.repair_required is True
    assert diag.last_failure_category == "signature_repair_unsupported"


async def test_runtime_does_not_reupload_identity_when_server_identity_is_missing() -> None:
    """Runtime reconciliation (no password) must not publish identity material.

    Uploading master/self-signing keys is a user-interactive-authenticated
    operation; runtime has no password, so the provider call cannot succeed
    against a real homeserver. Runtime reports ``bootstrap_required`` instead.
    """
    identity = _identity(uploaded=False)
    client = FakeClient(identity=identity, server_identity=False)
    service = MatrixCrossSigningService(client)

    assert await service.reconcile() is None
    assert client.ensure_calls == []
    diag = service.diagnostics()
    assert diag.chain_status == "missing"
    assert diag.last_failure_category == "bootstrap_required"
    assert diag.repair_required is True


async def test_authenticated_reconcile_reuploads_identity_when_server_identity_is_missing() -> None:
    identity = _identity(uploaded=False)
    client = FakeClient(identity=identity, server_identity=False)
    service = MatrixCrossSigningService(client)

    result = await service.reconcile(
        password="fresh-password", allow_bootstrap=True
    )
    assert result == "device_signed"
    assert client.ensure_calls == ["fresh-password"]
    assert service.diagnostics().chain_status == "valid"


async def test_reset_requires_fresh_password() -> None:
    identity = _identity()
    client = FakeClient(identity=identity, server_identity=True)
    service = MatrixCrossSigningService(client)

    assert await service.reconcile(reset=True) is None
    diag = service.diagnostics()
    assert diag.reset_required is True
    assert diag.last_failure_category == "reset_requires_password"
    assert client.ensure_calls == []


async def test_authenticated_reset_rotates_only_on_explicit_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    identity = _identity()
    client = FakeClient(identity=identity, server_identity=True)
    service = MatrixCrossSigningService(client)
    sidecar = tmp_path / "identity.json"
    sidecar.write_text('{"old": true}', encoding="utf-8")
    monkeypatch.setattr(service, "_provider_sidecar_path", lambda: sidecar)

    original_ensure = client.ensure_cross_signing

    async def rotate(password: str | None = None) -> str:
        assert not sidecar.exists()
        client.cross_signing_identity = None
        result = await original_ensure(password=password)
        sidecar.write_text('{"new": true}', encoding="utf-8")
        return result

    client.ensure_cross_signing = rotate  # type: ignore[method-assign]

    result = await service.reconcile(password="pw", reset=True)

    assert result == "uploaded_and_signed"
    assert sidecar.read_text(encoding="utf-8") == '{"new": true}'
    assert not sidecar.with_name(f"{sidecar.name}.pre-reset").exists()
    assert service.diagnostics().chain_status == "valid"


async def test_failed_reset_before_upload_restores_previous_sidecar(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    identity = _identity()
    client = FakeClient(identity=identity, server_identity=True)
    service = MatrixCrossSigningService(client)
    sidecar = tmp_path / "identity.json"
    sidecar.write_text('{"old": true}', encoding="utf-8")
    monkeypatch.setattr(service, "_provider_sidecar_path", lambda: sidecar)

    async def fail_before_upload(password: str | None = None) -> str:
        client.cross_signing_identity = FakeIdentity(client.user_id, uploaded=False)
        sidecar.write_text('{"new": false}', encoding="utf-8")
        raise RuntimeError("rejected")

    client.ensure_cross_signing = fail_before_upload  # type: ignore[method-assign]

    assert await service.reconcile(password="pw", reset=True) is None
    assert sidecar.read_text(encoding="utf-8") == '{"old": true}'
    assert service.diagnostics().last_failure_category == "reset_failed"


async def test_cancelled_reset_restores_previous_sidecar(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    identity = _identity()
    client = FakeClient(identity=identity, server_identity=True)
    service = MatrixCrossSigningService(client)
    sidecar = tmp_path / "identity.json"
    sidecar.write_text('{"old": true}', encoding="utf-8")
    monkeypatch.setattr(service, "_provider_sidecar_path", lambda: sidecar)

    async def cancelled(password: str | None = None) -> str:
        sidecar.write_text('{"new": false}', encoding="utf-8")
        raise asyncio.CancelledError

    client.ensure_cross_signing = cancelled  # type: ignore[method-assign]

    with pytest.raises(asyncio.CancelledError):
        await service.reconcile(password="pw", reset=True)

    assert sidecar.read_text(encoding="utf-8") == '{"old": true}'
    assert not sidecar.with_name(f"{sidecar.name}.pre-reset").exists()


async def test_cancellation_from_provider_bootstrap_propagates() -> None:
    client = FakeClient(identity=None, server_identity=False)

    async def cancelled(password: str | None = None) -> str:
        raise asyncio.CancelledError

    client.ensure_cross_signing = cancelled  # type: ignore[method-assign]
    service = MatrixCrossSigningService(client)

    with pytest.raises(asyncio.CancelledError):
        await service.reconcile(password="pw", allow_bootstrap=True)


async def test_diagnostics_never_include_tokens_or_key_material() -> None:
    identity = _identity()
    client = FakeClient(identity=identity, server_identity=True)
    service = MatrixCrossSigningService(client)

    await service.reconcile()
    diagnostic_text = repr(service.diagnostics())

    assert client.access_token not in diagnostic_text
    assert identity.master_public_key not in diagnostic_text
    assert identity.self_signing_public_key not in diagnostic_text
    assert "sig-master" not in diagnostic_text
    assert "sig-device" not in diagnostic_text
