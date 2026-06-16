"""CLI tests for ``medre support bundle``.

Exercises the command through the real CLI entry point using
``_run_cli_raw``. Verifies exit codes, output paths, flag handling,
redaction in stdout/stderr, and graceful handling of unsupported
config formats (``.toml``).
"""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

from tests.helpers.cli import _run_cli_raw

# ---------------------------------------------------------------------------
# Config constants
# ---------------------------------------------------------------------------

CONFIG_VALID = """\
runtime:
  name: cli-support-test
storage:
  backend: memory
adapters:
  matrix:
    main:
      enabled: true
      adapter_kind: fake
      homeserver: https://matrix.test
      user_id: '@bot:test'
      access_token: s3cret-token-cli-test
      room_allowlist: ['!room:test']
      encryption_mode: plaintext
  meshtastic:
    radio:
      enabled: true
      adapter_kind: fake
      connection_type: fake
      origin_label: TestMesh
routes:
  matrix_to_radio:
    source_adapters: [main]
    dest_adapters: [radio]
    directionality: source_to_dest
    enabled: true
"""

# Valid YAML but fails runtime validation (negative limit).
CONFIG_INVALID = """\
runtime:
  name: cli-support-invalid
  limits:
    max_inflight_deliveries: -1
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
      room_allowlist: ['!room:test']
      encryption_mode: plaintext
"""

# A TOML-style file content — the suffix is what triggers rejection.
TOML_CONTENT = """\
[runtime]
name = "toml-reject"
"""


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_config_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in ("MEDRE_HOME", "MEDRE_CONFIG"):
        monkeypatch.delenv(var, raising=False)


def _write(tmp_path: Path, text: str, name: str = "config.yaml") -> Path:
    p = tmp_path / name
    p.write_text(text)
    return p


def _bundle_members(zip_path: Path) -> dict[str, bytes]:
    with zipfile.ZipFile(zip_path, "r") as zf:
        return {name: zf.read(name) for name in zf.namelist()}


# ---------------------------------------------------------------------------
# Happy path: valid config
# ---------------------------------------------------------------------------


def test_valid_config_writes_zip_exit_zero(tmp_path: Path) -> None:
    """Valid config writes a ZIP and exits 0; stdout names the bundle path."""
    cfg = _write(tmp_path, CONFIG_VALID)
    out = tmp_path / "bundle.zip"
    stdout, stderr, code = _run_cli_raw(
        "support", "bundle", "--config", str(cfg), "--output", str(out)
    )
    assert code == 0
    assert out.is_file()
    combined = stdout + stderr
    assert str(out) in combined or "bundle" in combined.lower()


def test_valid_config_bundle_has_members(tmp_path: Path) -> None:
    """The CLI-written bundle contains the expected JSON members."""
    cfg = _write(tmp_path, CONFIG_VALID)
    out = tmp_path / "bundle.zip"
    _run_cli_raw("support", "bundle", "--config", str(cfg), "--output", str(out))
    members = _bundle_members(out)
    assert "manifest.json" in members
    assert "config_check.json" in members
    check = json.loads(members["config_check.json"].decode("utf-8"))
    assert check["success"] is True


# ---------------------------------------------------------------------------
# Invalid config: partial output, still exit 0
# ---------------------------------------------------------------------------


def test_invalid_config_writes_zip_exit_zero(tmp_path: Path) -> None:
    """Invalid config still writes a ZIP with the error recorded; exit 0."""
    cfg = _write(tmp_path, CONFIG_INVALID)
    out = tmp_path / "bundle.zip"
    _stdout, _stderr, code = _run_cli_raw(
        "support", "bundle", "--config", str(cfg), "--output", str(out)
    )
    assert code == 0, f"expected exit 0 for invalid config, got {code}"
    assert out.is_file()
    members = _bundle_members(out)
    check = json.loads(members["config_check.json"].decode("utf-8"))
    assert check["success"] is False
    assert isinstance(check["error"], str)


# ---------------------------------------------------------------------------
# Flag handling
# ---------------------------------------------------------------------------


def test_output_flag_respected(tmp_path: Path) -> None:
    """--output writes the ZIP to the specified custom path."""
    cfg = _write(tmp_path, CONFIG_VALID)
    custom = tmp_path / "custom-dir" / "my-bundle.zip"
    custom.parent.mkdir()
    _run_cli_raw("support", "bundle", "--config", str(cfg), "--output", str(custom))
    assert custom.is_file()
    # The default name is NOT used when --output is given.
    assert not (tmp_path / "medre-support-bundle.zip").exists()


def test_config_flag_respected(tmp_path: Path) -> None:
    """--config loads from the specified path (not auto-discovery)."""
    cfg = _write(tmp_path, CONFIG_VALID, name="my-special-config.yaml")
    out = tmp_path / "bundle.zip"
    _run_cli_raw("support", "bundle", "--config", str(cfg), "--output", str(out))
    assert out.is_file()
    members = _bundle_members(out)
    # config_source.json should record the explicit path.
    src = json.loads(members["config_source.json"].decode("utf-8"))
    assert src["source"] == "explicit"
    assert "my-special-config.yaml" in src["path"]


# ---------------------------------------------------------------------------
# Secret safety in CLI output
# ---------------------------------------------------------------------------


def test_no_secrets_in_stdout_stderr(tmp_path: Path) -> None:
    """CLI stdout/stderr must not contain the fake access_token value."""
    cfg = _write(tmp_path, CONFIG_VALID)
    out = tmp_path / "bundle.zip"
    stdout, stderr, code = _run_cli_raw(
        "support", "bundle", "--config", str(cfg), "--output", str(out)
    )
    assert code == 0
    assert "s3cret-token-cli-test" not in stdout
    assert "s3cret-token-cli-test" not in stderr


def test_no_secrets_in_written_bundle(tmp_path: Path) -> None:
    """The CLI-written bundle ZIP must not contain the raw token."""
    cfg = _write(tmp_path, CONFIG_VALID)
    out = tmp_path / "bundle.zip"
    _run_cli_raw("support", "bundle", "--config", str(cfg), "--output", str(out))
    members = _bundle_members(out)
    blob = "\n".join(
        data.decode("utf-8", errors="replace") for data in members.values()
    )
    assert "s3cret-token-cli-test" not in blob


# ---------------------------------------------------------------------------
# TOML rejection
# ---------------------------------------------------------------------------


def test_toml_config_rejected_safely(tmp_path: Path) -> None:
    """A .toml config is rejected gracefully — error in bundle, not a crash."""
    cfg = _write(tmp_path, TOML_CONTENT, name="config.toml")
    out = tmp_path / "bundle.zip"
    _stdout, _stderr, code = _run_cli_raw(
        "support", "bundle", "--config", str(cfg), "--output", str(out)
    )
    # Exit 0: the ZIP is still written with the error recorded.
    assert code == 0, f"expected exit 0 for .toml rejection, got {code}"
    assert out.is_file()
    members = _bundle_members(out)
    check = json.loads(members["config_check.json"].decode("utf-8"))
    assert check["success"] is False
    assert isinstance(check["error"], str)
    # The error message should reference TOML being unsupported.
    assert "toml" in check["error"].lower() or "extension" in check["error"].lower()


# ---------------------------------------------------------------------------
# Human output mentions redaction
# ---------------------------------------------------------------------------


def test_human_output_mentions_redaction(tmp_path: Path) -> None:
    """The human-readable stdout mentions that secrets were redacted."""
    cfg = _write(tmp_path, CONFIG_VALID)
    out = tmp_path / "bundle.zip"
    stdout, _stderr, code = _run_cli_raw(
        "support", "bundle", "--config", str(cfg), "--output", str(out)
    )
    assert code == 0
    assert (
        "redact" in stdout.lower()
    ), f"expected stdout to mention redaction, got: {stdout!r}"
    # Also mentions the bundle was written.
    assert "bundle" in stdout.lower()
