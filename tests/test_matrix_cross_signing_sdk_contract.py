"""Installed mindroom-nio cross-signing contract guard.

This test is intentionally skipped in the base development environment and
becomes active when the Matrix E2EE extra is installed.  It protects the
provider surface MEDRE's identity policy depends on from silent SDK drift.
"""

from __future__ import annotations

import inspect

import pytest


def test_mindroom_nio_exposes_cross_signing_contract() -> None:
    nio = pytest.importorskip("nio")
    crypto = pytest.importorskip("nio.crypto")
    if not bool(getattr(crypto, "ENCRYPTION_ENABLED", False)):
        pytest.skip("nio E2EE support is not enabled")

    ensure = getattr(nio.AsyncClient, "ensure_cross_signing", None)
    identity = getattr(nio.AsyncClient, "cross_signing_identity", None)
    assert callable(ensure)
    assert isinstance(identity, property)
    assert "password" in inspect.signature(ensure).parameters

    cross_signing = pytest.importorskip("nio.crypto.cross_signing")
    identity_cls = cross_signing.CrossSigningIdentity
    assert callable(getattr(identity_cls, "load", None))
    assert callable(getattr(identity_cls, "generate", None))
    assert callable(getattr(identity_cls, "self_signing_key_payload", None))
    assert callable(getattr(identity_cls, "signed_device_payload", None))
    assert callable(getattr(cross_signing, "cross_signing_sidecar_path", None))

    repair = getattr(nio.AsyncClient, "_upload_own_device_signature", None)
    assert callable(repair)
