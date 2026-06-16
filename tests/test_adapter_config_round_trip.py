"""Adapter config round-trip tests.

Verifies each transport's adapter config survives the full load path:

    Python dict  →  YAML text (yaml.safe_dump)
                  →  strict YAML parse (parse_yaml_config)
                  →  load_config
                  →  typed adapter dataclass (.config)

All configs use ``adapter_kind: fake`` so no hardware, network, or
credentials are required. Matrix additionally uses
``encryption_mode: plaintext`` to avoid crypto-store derivation.

Covers three scenarios per transport:

  * **minimal valid** — the narrowest table that loads successfully,
    with explicitly set values preserved through the round-trip.
  * **unknown key rejection** — a bogus key raises
    :class:`ConfigValidationError` matching the loader's message,
    carrying ``transport`` and ``section_path`` context.
  * **removed legacy key** — ``meshnet_name`` (a removed template
    placeholder) is rejected as an unknown adapter key. There is no
    "did you mean" suggestion mechanism yet, so only the rejection is
    asserted; when one lands it can be strengthened to check the hint.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from medre.config.adapters.lxmf import LxmfConfig
from medre.config.adapters.matrix import MatrixConfig
from medre.config.adapters.meshcore import MeshCoreConfig
from medre.config.adapters.meshtastic import MeshtasticConfig
from medre.config.errors import ConfigValidationError
from medre.config.loader import load_config

# ---------------------------------------------------------------------------
# Round-trip helper
# ---------------------------------------------------------------------------


def _load_single_adapter(
    transport: str,
    instance: str,
    adapter_table: dict[str, Any],
    tmp_path: Path,
) -> Any:
    """Serialize a single-adapter config to YAML, load it via
    :func:`load_config`, and return the typed adapter config stored under
    the runtime wrapper's ``.config`` attribute."""
    doc = {"adapters": {transport: {instance: adapter_table}}}
    text = yaml.safe_dump(doc, sort_keys=True)
    path = tmp_path / "config.yaml"
    path.write_text(text)
    runtime_config, _source, _paths = load_config(str(path))
    group = getattr(runtime_config.adapters, transport)
    return group[instance].config


# ---------------------------------------------------------------------------
# Minimal valid configs (the narrowest table that loads for each transport)
# ---------------------------------------------------------------------------

_MINIMAL_MATRIX = {
    "adapter_id": "matrix_main",
    "adapter_kind": "fake",
    "homeserver": "https://matrix.example.org",
    "user_id": "@bot:example.org",
    "access_token": "syntoken-abc",
    "encryption_mode": "plaintext",
}

_MINIMAL_MESHTASTIC = {
    "adapter_id": "radio_a",
    "adapter_kind": "fake",
    "connection_type": "fake",
}

_MINIMAL_MESHCORE = {
    "adapter_id": "meshcore_a",
    "adapter_kind": "fake",
    "connection_type": "fake",
}

_MINIMAL_LXMF = {
    "adapter_id": "lxmf_a",
    "adapter_kind": "fake",
    "connection_type": "fake",
}


# ---------------------------------------------------------------------------
# Minimal valid round-trip per transport
# ---------------------------------------------------------------------------


def test_matrix_minimal_round_trip(tmp_path: Path) -> None:
    """Matrix minimal config round-trips with values preserved."""
    cfg = _load_single_adapter("matrix", "main", _MINIMAL_MATRIX, tmp_path)
    assert isinstance(cfg, MatrixConfig)
    assert cfg.adapter_id == "matrix_main"
    assert cfg.homeserver == "https://matrix.example.org"
    assert cfg.user_id == "@bot:example.org"
    assert cfg.access_token == "syntoken-abc"
    assert cfg.encryption_mode == "plaintext"
    # Defaults flow through untouched.
    assert cfg.auto_join_rooms == ()
    assert cfg.require_encrypted_rooms is False


def test_meshtastic_minimal_round_trip(tmp_path: Path) -> None:
    """Meshtastic minimal config round-trips with values preserved."""
    cfg = _load_single_adapter("meshtastic", "radio_a", _MINIMAL_MESHTASTIC, tmp_path)
    assert isinstance(cfg, MeshtasticConfig)
    assert cfg.adapter_id == "radio_a"
    assert cfg.connection_type == "fake"
    # Defaults survive the YAML round-trip.
    assert cfg.max_text_bytes == 227
    assert cfg.outbound_mode == "enabled"
    assert cfg.encrypted_action == "drop"


def test_meshcore_minimal_round_trip(tmp_path: Path) -> None:
    """MeshCore minimal config round-trips with values preserved."""
    cfg = _load_single_adapter("meshcore", "meshcore_a", _MINIMAL_MESHCORE, tmp_path)
    assert isinstance(cfg, MeshCoreConfig)
    assert cfg.adapter_id == "meshcore_a"
    assert cfg.connection_type == "fake"
    assert cfg.max_text_bytes == 512
    assert cfg.node_config == {}


def test_lxmf_minimal_round_trip(tmp_path: Path) -> None:
    """LXMF minimal config round-trips with values preserved."""
    cfg = _load_single_adapter("lxmf", "lxmf_a", _MINIMAL_LXMF, tmp_path)
    assert isinstance(cfg, LxmfConfig)
    assert cfg.adapter_id == "lxmf_a"
    assert cfg.connection_type == "fake"
    assert cfg.default_delivery_method == "direct"
    assert cfg.metadata_embedding is True


# ---------------------------------------------------------------------------
# Unknown-key rejection
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("transport", "instance", "minimal"),
    [
        ("matrix", "main", _MINIMAL_MATRIX),
        ("meshtastic", "radio_a", _MINIMAL_MESHTASTIC),
        ("meshcore", "meshcore_a", _MINIMAL_MESHCORE),
        ("lxmf", "lxmf_a", _MINIMAL_LXMF),
    ],
    ids=["matrix", "meshtastic", "meshcore", "lxmf"],
)
def test_unknown_adapter_key_rejected(
    transport: str,
    instance: str,
    minimal: dict[str, Any],
    tmp_path: Path,
) -> None:
    """A bogus key in an adapter table is rejected end-to-end with
    ``ConfigValidationError`` carrying transport/section_path context.

    Mirrors ``test_unknown_adapter_key_rejected_via_load`` in
    ``test_config_model.py`` but exercises all four transports through
    the YAML round-trip path (not just Matrix via hand-written YAML).
    """
    bad = dict(minimal)
    bad["totally_bogus_key"] = 42
    with pytest.raises(
        ConfigValidationError, match="unknown adapter config key"
    ) as exc_info:
        _load_single_adapter(transport, instance, bad, tmp_path)
    assert exc_info.value.transport == transport
    assert exc_info.value.section_path == f"adapters.{transport}.{instance}"
    assert "totally_bogus_key" in str(exc_info.value)


# ---------------------------------------------------------------------------
# Removed legacy key (meshnet_name) rejection
# ---------------------------------------------------------------------------


def test_removed_meshnet_name_rejected(tmp_path: Path) -> None:
    """``meshnet_name`` was a removed template placeholder. As an adapter
    config key it is rejected as unknown.

    There is no "did you mean" suggestion mechanism in place yet, so only
    the rejection is asserted. When one is added this test can be
    strengthened to check the suggestion content.
    """
    bad = dict(_MINIMAL_MESHTASTIC)
    bad["meshnet_name"] = "old-net"
    with pytest.raises(
        ConfigValidationError, match="unknown adapter config key"
    ) as exc_info:
        _load_single_adapter("meshtastic", "radio_a", bad, tmp_path)
    assert exc_info.value.transport == "meshtastic"
    assert "meshnet_name" in str(exc_info.value)
