# medre-test-tier: compatibility
"""Guided bridge authoring against the shipped sample providers.

These tests require the installed adapter packages whose sample
configurations declare native restrictions (for example the Matrix room
allowlist), so they stay out of the adapter-free suite.
"""

import io

from medre import adapter_registry
from medre.adapter_registry import AdapterTypeRegistry, iter_adapter_specs
from medre.config import loader
from medre.config._yaml import parse_yaml_config
from tests.helpers.cli import _run_cli_raw

# Captured before any test swaps the registry, so the shipped providers stay
# available regardless of collection order within the session.
_REAL_SPECS = tuple(iter_adapter_specs())
_REAL_TRANSPORTS = frozenset(loader._KNOWN_TRANSPORTS)


class Terminal(io.StringIO):
    def isatty(self):
        return True


def test_matrix_context_outside_sample_allowlist_is_rejected_at_entry(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("MEDRE_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(
        adapter_registry, "ADAPTER_REGISTRY", AdapterTypeRegistry(_REAL_SPECS)
    )
    monkeypatch.setattr(loader, "_KNOWN_TRANSPORTS", _REAL_TRANSPORTS)
    monkeypatch.setattr(
        "sys.stdin",
        Terminal(
            "matrix_1\nmeshtastic_1\nbidirectional\n"
            "!wrongroom:example.com\n1\n"
            "!room:example.com\n1\n"
            "\n\n\nyes\n"
        ),
    )
    _, stderr, code = _run_cli_raw(
        "config", "bridge", "--output", str(tmp_path / "bridge.yaml")
    )
    assert code == 0, stderr
    assert "Context rejected" in stderr and "room_allowlist" in stderr
    document = parse_yaml_config((tmp_path / "bridge.yaml").read_text())
    assert document["routes"]["bridge"]["context_map"] == {
        "!room:example.com": {"dest_context": "1"}
    }


def test_guided_bridge_rejects_matrix_alias_not_authorized_by_room_allowlist(
    monkeypatch, tmp_path
):
    """Provider preflight checks aliases too, not only !room ID prefixes."""
    monkeypatch.setenv("MEDRE_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(
        adapter_registry, "ADAPTER_REGISTRY", AdapterTypeRegistry(_REAL_SPECS)
    )
    monkeypatch.setattr(loader, "_KNOWN_TRANSPORTS", _REAL_TRANSPORTS)
    output = tmp_path / "bridge.yaml"
    _, stderr, code = _run_cli_raw(
        "config",
        "bridge",
        "--output",
        str(output),
        "--source",
        "matrix_1",
        "--dest",
        "meshtastic_1",
        "--direction",
        "bidirectional",
        "--map",
        "#alias:example.com",
        "1",
    )
    assert code == 2, stderr
    assert "must be a room ID" in stderr
    assert not output.exists()
