"""Startup header lines report installed core and provider provenance."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest

from medre.runtime import installation_summary


class _FakeDist:
    def __init__(self, name: str, version: str, direct: dict[str, Any] | None = None):
        self._name = name
        self._version = version
        self._direct = direct
        self.metadata = {"Name": name}

    @property
    def version(self) -> str:
        return self._version

    def read_text(self, filename: str) -> str | None:
        if filename == "direct_url.json" and self._direct is not None:
            return json.dumps(self._direct)
        return None


def _entry(dist: _FakeDist, name: str) -> SimpleNamespace:
    return SimpleNamespace(name=name, dist=dist)


GIT_MATRIX = _FakeDist(
    "medre-adapter-matrix",
    "0.1.0",
    {
        "url": "https://github.com/jeremiah-k/medre-adapter-matrix",
        "vcs_info": {
            "vcs": "git",
            "commit_id": "c987da62baea6b31627d4f8fc5f5b6c69d07f844",
        },
    },
)
INDEX_MESHTASTIC = _FakeDist("medre-adapter-meshtastic", "0.1.1")


def _install_environment(
    monkeypatch: pytest.MonkeyPatch, providers: list[SimpleNamespace]
) -> None:
    def version(name: str) -> str:
        assert name == "medre"
        return "0.1.1"

    monkeypatch.setattr(installation_summary.importlib.metadata, "version", version)
    monkeypatch.setattr(
        installation_summary.importlib.metadata,
        "distribution",
        lambda name: _FakeDist(name, "0.1.1"),
    )

    def entry_points(*, group: str):
        assert group == "medre.adapters"
        return providers

    monkeypatch.setattr(
        installation_summary.importlib.metadata, "entry_points", entry_points
    )


def test_banner_reports_core_python_and_provider_provenance(
    monkeypatch: pytest.MonkeyPatch,
):
    _install_environment(
        monkeypatch,
        [
            _entry(GIT_MATRIX, "matrix"),
            _entry(INDEX_MESHTASTIC, "meshtastic"),
            _entry(_FakeDist("medre", "0.1.1"), "builtin"),
        ],
    )
    monkeypatch.setattr(
        installation_summary.platform, "python_version", lambda: "3.14.8"
    )
    monkeypatch.setattr(installation_summary.platform, "system", lambda: "Linux")
    monkeypatch.setattr(installation_summary.platform, "machine", lambda: "x86_64")
    lines = installation_summary.installation_lines()
    assert "  Core:    medre 0.1.1 (index)" in lines
    assert "  Python:  3.14.8 (Linux x86_64)" in lines
    assert "  Adapter: matrix 0.1.0 (git c987da62)" in lines
    assert "  Adapter: meshtastic 0.1.1 (index)" in lines


def test_community_distribution_names_are_surfaced(monkeypatch: pytest.MonkeyPatch):
    _install_environment(
        monkeypatch, [_entry(_FakeDist("community-radio", "1.2.3"), "radio")]
    )
    lines = installation_summary.installation_lines()
    assert "  Adapter: radio 1.2.3 (index) [community-radio]" in lines


def test_editable_and_url_labels_redact_credentials(monkeypatch: pytest.MonkeyPatch):
    editable = _FakeDist(
        "medre-adapter-lxmf",
        "0.1.0",
        {"url": "file:///home/operator/dev/lxmf", "dir_info": {"editable": True}},
    )
    wheel = _FakeDist(
        "community-wheel",
        "2.0.0",
        {"url": "https://example.invalid/pkg.whl?token=fixture-secret-sentinel"},
    )
    _install_environment(
        monkeypatch, [_entry(editable, "lxmf"), _entry(wheel, "extra")]
    )
    text = "\n".join(installation_summary.installation_lines())
    assert "editable file:///home/operator/dev/lxmf" in text
    assert "fixture-secret-sentinel" not in text
    assert "?redacted" in text


def test_corrupt_direct_metadata_degrades_per_distribution(
    monkeypatch: pytest.MonkeyPatch,
):
    broken = _FakeDist("medre-adapter-xmpp", "0.1.0", {"unexpected": "shape"})
    broken.read_text = lambda filename: "{not json"  # type: ignore[method-assign]
    _install_environment(
        monkeypatch, [_entry(broken, "xmpp"), _entry(INDEX_MESHTASTIC, "meshtastic")]
    )
    lines = installation_summary.installation_lines()
    assert "  Adapter: xmpp 0.1.0 (unknown source)" in lines
    assert "  Adapter: meshtastic 0.1.1 (index)" in lines


def test_provider_failures_never_block_the_banner(monkeypatch: pytest.MonkeyPatch):
    def entry_points(*, group: str):
        raise RuntimeError("metadata exploded")

    monkeypatch.setattr(
        installation_summary.importlib.metadata, "entry_points", entry_points
    )
    lines = installation_summary.installation_lines()
    assert lines and lines[0].startswith("  Core:")


def test_core_distribution_missing_reports_source_tree(
    monkeypatch: pytest.MonkeyPatch,
):
    missing = installation_summary.importlib.metadata.PackageNotFoundError

    def version(name: str):
        raise missing(name)

    monkeypatch.setattr(installation_summary.importlib.metadata, "version", version)
    monkeypatch.setattr(
        installation_summary.importlib.metadata,
        "distribution",
        lambda name: (_ for _ in ()).throw(missing(name)),
    )
    monkeypatch.setattr(
        installation_summary.importlib.metadata, "entry_points", lambda **kw: []
    )
    lines = installation_summary.installation_lines()
    expected = (
        f"  Core:    medre {installation_summary._CORE_FALLBACK_VERSION} (source)"
    )
    assert expected in lines
