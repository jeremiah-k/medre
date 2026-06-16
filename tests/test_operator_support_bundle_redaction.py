"""Redaction tests for the operator support bundle.

Verifies that no raw secret values appear in ANY member of the produced
ZIP archive. Each test creates a bundle from a config containing a known
fake secret, extracts every member, and asserts the secret string is
absent from the concatenated content.
"""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

from medre.runtime.support_bundle import create_support_bundle

# ---------------------------------------------------------------------------
# Config templates with FAKE secret values
# ---------------------------------------------------------------------------

# Matrix access_token. Fake value: s3cret-token-test
CONFIG_MATRIX_TOKEN = """\
runtime:
  name: redact-matrix
storage:
  backend: memory
adapters:
  matrix:
    main:
      enabled: true
      adapter_kind: fake
      homeserver: https://matrix.test
      user_id: '@bot:test'
      access_token: s3cret-token-test
      room_allowlist: ['!room:test']
      encryption_mode: plaintext
"""

# Password-like field. Fake value: secretpass-test
CONFIG_PASSWORD = """\
runtime:
  name: redact-password
storage:
  backend: memory
adapters:
  matrix:
    main:
      enabled: true
      adapter_kind: fake
      homeserver: https://matrix.test
      user_id: '@bot:test'
      access_token: tok
      password: secretpass-test
      room_allowlist: ['!room:test']
      encryption_mode: plaintext
"""

# Private key field. Fake value: fake-private-key-data
CONFIG_PRIVATE_KEY = """\
runtime:
  name: redact-private-key
storage:
  backend: memory
adapters:
  matrix:
    main:
      enabled: true
      adapter_kind: fake
      homeserver: https://matrix.test
      user_id: '@bot:test'
      access_token: tok
      private_key: fake-private-key-data
      room_allowlist: ['!room:test']
      encryption_mode: plaintext
"""

# MeshCore BLE PIN. Fake value: 123456
CONFIG_BLE_PIN = """\
runtime:
  name: redact-ble-pin
storage:
  backend: memory
adapters:
  meshcore:
    core_node:
      enabled: true
      adapter_kind: fake
      connection_type: fake
      ble_pin: "123456"
"""

# LXMF identity path. Fake value: /path/to/fake/identity.key
CONFIG_IDENTITY_PATH = """\
runtime:
  name: redact-identity
storage:
  backend: memory
adapters:
  lxmf:
    lxmf_node:
      enabled: true
      adapter_kind: fake
      connection_type: fake
      identity_path: /path/to/fake/identity.key
"""

# Deeply nested secret fields to verify recursion.
CONFIG_NESTED_SECRETS = """\
runtime:
  name: redact-nested
storage:
  backend: memory
adapters:
  matrix:
    main:
      enabled: true
      adapter_kind: fake
      homeserver: https://matrix.test
      user_id: '@bot:test'
      access_token: nested-tok-test
      room_allowlist: ['!room:test']
      encryption_mode: plaintext
      nested_creds:
        api_key: nested-api-key-test
        client_secret: nested-client-secret-test
"""

# Config with a secret value but invalid for another reason (bad encryption_mode).
# Used to verify error messages do not echo the secret.
CONFIG_SECRET_WITH_VALIDATION_ERROR = """\
runtime:
  name: redact-error
storage:
  backend: memory
adapters:
  matrix:
    main:
      enabled: true
      adapter_kind: fake
      homeserver: https://matrix.test
      user_id: '@bot:test'
      access_token: s3cret-token-test
      room_allowlist: ['!room:test']
      encryption_mode: invalid_cipher_mode
"""


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_config_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in ("MEDRE_HOME", "MEDRE_CONFIG"):
        monkeypatch.delenv(var, raising=False)


def _write_config(tmp_path: Path, text: str, name: str = "config.yaml") -> Path:
    p = tmp_path / name
    p.write_text(text)
    return p


def _all_bundle_text(zip_path: Path) -> str:
    """Concatenate every member of the bundle ZIP as decoded text."""
    chunks: list[str] = []
    with zipfile.ZipFile(zip_path, "r") as zf:
        for name in zf.namelist():
            chunks.append(zf.read(name).decode("utf-8", errors="replace"))
    return "\n".join(chunks)


def _bundle_members(zip_path: Path) -> dict[str, bytes]:
    with zipfile.ZipFile(zip_path, "r") as zf:
        return {name: zf.read(name) for name in zf.namelist()}


def _build_bundle(tmp_path: Path, config_text: str) -> Path:
    """Write config, build bundle, return ZIP path."""
    cfg = _write_config(tmp_path, config_text)
    out = tmp_path / "bundle.zip"
    create_support_bundle(config_path=cfg, output_path=out)
    assert out.is_file(), "bundle ZIP was not written"
    return out


# ---------------------------------------------------------------------------
# Per-secret-type redaction tests
# ---------------------------------------------------------------------------


def test_matrix_access_token_redacted(tmp_path: Path) -> None:
    """Matrix access_token value must not appear in any bundle member."""
    zip_path = _build_bundle(tmp_path, CONFIG_MATRIX_TOKEN)
    blob = _all_bundle_text(zip_path)
    assert (
        "s3cret-token-test" not in blob
    ), "access_token value leaked into support bundle"
    # Confirm redaction marker appears in the redacted config member.
    redacted = (
        _bundle_members(zip_path)
        .get("redacted_config.yaml", b"")
        .decode("utf-8", errors="replace")
    )
    assert "***REDACTED***" in redacted


def test_password_field_redacted(tmp_path: Path) -> None:
    """Password-like field value must not appear in any bundle member."""
    zip_path = _build_bundle(tmp_path, CONFIG_PASSWORD)
    blob = _all_bundle_text(zip_path)
    assert "secretpass-test" not in blob, "password value leaked into bundle"


def test_private_key_field_redacted(tmp_path: Path) -> None:
    """Private key field value must not appear in any bundle member."""
    zip_path = _build_bundle(tmp_path, CONFIG_PRIVATE_KEY)
    blob = _all_bundle_text(zip_path)
    assert "fake-private-key-data" not in blob, "private_key value leaked"


def test_ble_pin_redacted(tmp_path: Path) -> None:
    """MeshCore BLE PIN value must not appear in any bundle member."""
    zip_path = _build_bundle(tmp_path, CONFIG_BLE_PIN)
    _all_bundle_text(zip_path)
    # The PIN "123456" is short and could appear coincidentally in
    # structural data; assert against the full key=value form and the
    # raw value within the redacted_config.yaml member specifically.
    members = _bundle_members(zip_path)
    redacted_yaml = members.get("redacted_config.yaml", b"").decode(
        "utf-8", errors="replace"
    )
    assert "123456" not in redacted_yaml, "ble_pin value leaked into redacted YAML"
    assert "ble_pin" in redacted_yaml  # key name preserved
    assert "***REDACTED***" in redacted_yaml


def test_identity_path_redacted(tmp_path: Path) -> None:
    """LXMF identity_path value must not appear in any bundle member."""
    zip_path = _build_bundle(tmp_path, CONFIG_IDENTITY_PATH)
    blob = _all_bundle_text(zip_path)
    assert (
        "/path/to/fake/identity.key" not in blob
    ), "identity_path value leaked into bundle"


def test_redaction_recursesthrough_nested_dicts(tmp_path: Path) -> None:
    """Secrets nested inside sub-dicts are redacted at every level."""
    zip_path = _build_bundle(tmp_path, CONFIG_NESTED_SECRETS)
    members = _bundle_members(zip_path)
    redacted_yaml = members.get("redacted_config.yaml", b"").decode(
        "utf-8", errors="replace"
    )
    # The top-level access_token is redacted.
    assert "nested-tok-test" not in redacted_yaml
    # Nested api_key and client_secret values are also redacted.
    assert "nested-api-key-test" not in redacted_yaml
    assert "nested-client-secret-test" not in redacted_yaml
    # All three should show the redaction marker.
    assert redacted_yaml.count("***REDACTED***") >= 3
    # Full blob sweep for completeness.
    blob = _all_bundle_text(zip_path)
    for secret in (
        "nested-tok-test",
        "nested-api-key-test",
        "nested-client-secret-test",
    ):
        assert secret not in blob, f"{secret!r} leaked into bundle"


# ---------------------------------------------------------------------------
# Non-secret fields preserved
# ---------------------------------------------------------------------------


def test_non_secret_fields_preserved(tmp_path: Path) -> None:
    """Non-secret fields like adapter_id, transport, enabled are not redacted."""
    zip_path = _build_bundle(tmp_path, CONFIG_MATRIX_TOKEN)
    members = _bundle_members(zip_path)

    # adapters.json should contain safe wrapper fields, unredacted.
    adapters_doc = json.loads(members["adapters.json"].decode("utf-8"))
    assert len(adapters_doc["adapters"]) == 1
    adapter = adapters_doc["adapters"][0]
    assert adapter["adapter_id"] == "main"
    assert adapter["transport"] == "matrix"
    assert adapter["enabled"] is True

    # redacted_config.yaml preserves non-secret keys and values.
    redacted_yaml = members["redacted_config.yaml"].decode("utf-8")
    assert "homeserver" in redacted_yaml
    assert "matrix.test" in redacted_yaml
    assert "user_id" in redacted_yaml
    assert "encryption_mode" in redacted_yaml
    assert "room_allowlist" in redacted_yaml


def test_route_plan_origin_label_preserved(tmp_path: Path) -> None:
    """origin_label (a non-secret field) survives in route_plan and adapters."""
    cfg_text = """\
runtime:
  name: redact-label
storage:
  backend: memory
adapters:
  meshtastic:
    radio:
      enabled: true
      adapter_kind: fake
      connection_type: fake
      origin_label: SafeOriginLabel
"""
    zip_path = _build_bundle(tmp_path, cfg_text)
    members = _bundle_members(zip_path)
    adapters_doc = json.loads(members["adapters.json"].decode("utf-8"))
    assert adapters_doc["adapters"][0]["origin_label"] == "SafeOriginLabel"
    # And it appears in the redacted YAML (not a secret).
    redacted_yaml = members["redacted_config.yaml"].decode("utf-8")
    assert "SafeOriginLabel" in redacted_yaml


# ---------------------------------------------------------------------------
# Error-message safety
# ---------------------------------------------------------------------------


def test_error_messages_are_value_free(tmp_path: Path) -> None:
    """Config error messages do not echo the secret token value.

    The config below has a valid YAML structure but an invalid
    encryption_mode, so load_config raises ConfigValidationError. The
    access_token value 's3cret-token-test' must not appear in the error
    field of config_check.json nor anywhere else in the bundle.
    """
    zip_path = _build_bundle(tmp_path, CONFIG_SECRET_WITH_VALIDATION_ERROR)
    members = _bundle_members(zip_path)

    check = json.loads(members["config_check.json"].decode("utf-8"))
    assert check["success"] is False
    assert isinstance(check["error"], str)
    # The error message must not contain the token value.
    assert "s3cret-token-test" not in check["error"]

    # Full-bundle sweep: no member contains the raw token.
    blob = _all_bundle_text(zip_path)
    assert (
        "s3cret-token-test" not in blob
    ), "secret token value leaked into bundle despite validation error"
