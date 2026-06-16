"""No-I/O verification for the operator support bundle.

The support bundle is contractually offline: it must not import any
adapter SDK, start any adapter, or perform network/hardware I/O. These
tests verify that guarantee by inspecting the source and by building a
bundle with fake adapters without any SDK installed.
"""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from medre.runtime import support_bundle
from medre.runtime.support_bundle import create_support_bundle

# ---------------------------------------------------------------------------
# Config: fake adapters only (no SDK required)
# ---------------------------------------------------------------------------

CONFIG_FAKE_ONLY = """\
runtime:
  name: no-io-test
storage:
  backend: memory
adapters:
  matrix:
    fake_matrix:
      enabled: true
      adapter_kind: fake
      homeserver: https://fake.test
      user_id: '@bot:fake.test'
      access_token: fake-tok-no-io
      room_allowlist: ['!room:fake.test']
      encryption_mode: plaintext
  meshtastic:
    fake_mesh:
      enabled: true
      adapter_kind: fake
      connection_type: fake
      origin_label: FakeMesh
routes:
  fake_bridge:
    source_adapters: [fake_matrix]
    dest_adapters: [fake_mesh]
    directionality: source_to_dest
    enabled: true
"""


@pytest.fixture(autouse=True)
def _clean_config_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in ("MEDRE_HOME", "MEDRE_CONFIG"):
        monkeypatch.delenv(var, raising=False)


# ---------------------------------------------------------------------------
# Source-level static checks
# ---------------------------------------------------------------------------

# ponytail: substring scan of the module source is the shortest reliable
# gate that the bundle stays SDK-free and side-effect-free. A unit test
# exercising the offline path is paired below.
_SDK_IMPORT_TOKENS = (
    "import nio",
    "import meshtastic",
    "import meshcore",
    "import lxmf",
)
_STARTUP_TOKENS = (".start()", ".connect()", ".stop()", ".close()")


def _module_source() -> str:
    """Return the full source text of support_bundle.py."""
    return Path(support_bundle.__file__).read_text(encoding="utf-8")


def test_bundle_builds_with_fake_adapters(tmp_path: Path) -> None:
    """create_support_bundle succeeds with fake adapters and no SDK installed."""
    cfg = tmp_path / "config.yaml"
    cfg.write_text(CONFIG_FAKE_ONLY)
    out = tmp_path / "bundle.zip"
    result = create_support_bundle(config_path=cfg, output_path=out)
    assert out.is_file()
    assert result == out.resolve()
    # The bundle should report a successful config load.
    with zipfile.ZipFile(out, "r") as zf:
        import json

        check = json.loads(zf.read("config_check.json").decode("utf-8"))
    assert check["success"] is True


def test_no_sdk_imports_in_module() -> None:
    """support_bundle.py does not import any adapter SDK."""
    source = _module_source()
    for token in _SDK_IMPORT_TOKENS:
        assert (
            token not in source
        ), f"forbidden SDK import {token!r} found in support_bundle.py"


def test_no_adapter_startup_calls_in_module() -> None:
    """support_bundle.py does not call adapter .start()/.connect()/.stop()/.close()."""
    source = _module_source()
    for token in _STARTUP_TOKENS:
        assert (
            token not in source
        ), f"forbidden adapter I/O call {token!r} found in support_bundle.py"
