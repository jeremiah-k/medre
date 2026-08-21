"""Mechanical checks for current-state developer documentation authority."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]


def test_current_state_inventory_is_generated_and_current() -> None:
    result = subprocess.run(
        [sys.executable, "scripts/generate-current-state-inventory.py", "--check"],
        cwd=_ROOT,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_historical_audits_declare_non_authoritative_status() -> None:
    audits = sorted((_ROOT / "docs" / "dev").glob("*audit*.md"))
    assert audits
    for path in audits:
        text = path.read_text(encoding="utf-8")
        assert "**Historical snapshot — not contract authority.**" in text, path.name


def test_removed_development_shape_entry_points_do_not_return() -> None:
    """Current production surfaces do not reintroduce abandoned MEDRE shapes."""
    replay_rendering = (_ROOT / "src/medre/core/engine/replay/rendering.py").read_text(
        encoding="utf-8"
    )
    runner = (_ROOT / "src/medre/core/engine/pipeline/runner.py").read_text(
        encoding="utf-8"
    )
    smoke = (_ROOT / "src/medre/cli/smoke_commands.py").read_text(encoding="utf-8")
    lxmf_renderer = (_ROOT / "src/medre/adapters/lxmf/renderer.py").read_text(
        encoding="utf-8"
    )

    assert '_explicit_pipeline_method("render_event")' not in replay_rendering
    assert "def ingress_handler" not in runner
    assert 'commands.get("commands_text", commands)' not in smoke
    assert 'relay_prefix: str = ""' not in lxmf_renderer


def test_transport_attribution_reads_versioned_namespaces_only() -> None:
    """Built-in attribution readers use strict current-version accessors."""
    expected = {
        "meshtastic": "meshtastic_namespace",
        "meshcore": "meshcore_namespace",
        "lxmf": "lxmf_namespace",
    }
    for transport, accessor in expected.items():
        text = (_ROOT / f"src/medre/adapters/{transport}/attribution.py").read_text(
            encoding="utf-8"
        )
        assert accessor in text
        assert "native_data.get(" not in text
