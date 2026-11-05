"""Offline readiness report composition tests (SDK-free, hermetic).

Every scenario stubs the compatibility snapshot, the adapter registry, and
distribution metadata, so outcomes never depend on which adapter
distributions happen to be installed in the running environment.
"""

from __future__ import annotations

import importlib.metadata
import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from medre import adapter_registry
from medre.adapter_registry import (
    AdapterDiscoveryFailure,
    AdapterTypeRegistry,
)
from medre.adapter_spec import NativeSetupField, SymbolRef
from medre.cli.json import to_json
from medre.cli.main import main
from medre.config import loader as config_loader
from medre.runtime import readiness_report
from medre.runtime.readiness_report import build_readiness_report
from tests.helpers.adapter_spec import make_adapter_spec
from tests.helpers.bridge_setup import CONFIG

_REVISION = "a" * 40


# ---------------------------------------------------------------------------
# Stub helpers
# ---------------------------------------------------------------------------


def _entry(transport: str = "matrix", **overrides: Any) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "transport": transport,
        "repository": f"example/medre-adapter-{transport}",
        "ref": _REVISION,
        "extras": "",
        "local_integration": False,
        "distribution": f"medre-adapter-{transport}",
    }
    entry.update(overrides)
    return entry


def _stub_manifest(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, entries: Any
) -> None:
    """Stub snapshot discovery so tests never read the real checkout."""
    text = entries if isinstance(entries, str) else json.dumps(entries)
    path = tmp_path / "adapter-compatibility.json"
    path.write_text(text, encoding="utf-8")
    monkeypatch.setattr(readiness_report, "_locate_manifest", lambda: path)


@pytest.fixture(autouse=True)
def isolated_config_discovery(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Pin config discovery to a private empty MEDRE_HOME."""
    monkeypatch.setenv("MEDRE_HOME", str(tmp_path / "private-state"))
    monkeypatch.delenv("MEDRE_CONFIG", raising=False)


def _stub_distributions(
    monkeypatch: pytest.MonkeyPatch, installed: dict[str, str]
) -> None:
    """Stub distribution metadata; no module is probed or imported."""

    def fake_version(name: str) -> str:
        if name in installed:
            return installed[name]
        raise importlib.metadata.PackageNotFoundError(name)

    monkeypatch.setattr(importlib.metadata, "version", fake_version)


def _install_spec(monkeypatch: pytest.MonkeyPatch, **overrides: Any) -> Any:
    """Install one SDK-free synthetic provider spec."""
    spec = replace(
        make_adapter_spec("independent_chat"),
        config=SymbolRef("tests.helpers.bridge_setup", "ChatConfig"),
        **overrides,
    )
    monkeypatch.setattr(
        adapter_registry, "ADAPTER_REGISTRY", AdapterTypeRegistry((spec,))
    )
    monkeypatch.setattr(
        config_loader, "_KNOWN_TRANSPORTS", frozenset((spec.transport,))
    )
    return spec


def _write_config(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "bridge.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def _failing_preparer(
    config: object, *, adapter_id: str, paths: object, routes: object
) -> object:
    raise RuntimeError("provider refused this configuration")


# ---------------------------------------------------------------------------
# Compatibility snapshot section
# ---------------------------------------------------------------------------


class TestManifestSection:
    def test_all_snapshot_providers_stay_visible_when_installed(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A missing provider is a report row, never an error or an omission."""
        _stub_manifest(monkeypatch, tmp_path, [_entry("matrix"), _entry("meshtastic")])
        _stub_distributions(monkeypatch, {"medre-adapter-matrix": "9.0.0"})
        report = build_readiness_report(None)
        manifest = report["manifest"]
        assert manifest["status"] == "available"
        assert [p["transport"] for p in manifest["providers"]] == [
            "matrix",
            "meshtastic",
        ]
        states = {p["transport"]: p for p in manifest["providers"]}
        assert states["matrix"]["distribution_status"] == "installed"
        assert states["matrix"]["installed_version"] == "9.0.0"
        assert states["meshtastic"]["distribution_status"] == "missing"
        assert states["meshtastic"]["installed_version"] is None
        assert manifest["installed_count"] == 1
        assert manifest["provider_count"] == 2

    def test_registration_states_and_setup_forms(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Registry state and the provider-owned setup form are reported."""
        _stub_manifest(monkeypatch, tmp_path, [_entry("matrix"), _entry("meshtastic")])
        guided = replace(
            make_adapter_spec("matrix"),
            native_setup_fields=(
                NativeSetupField(name="credential", label="Credential", secret=True),
            ),
        )
        plain = make_adapter_spec("meshtastic")
        monkeypatch.setattr(
            adapter_registry,
            "ADAPTER_REGISTRY",
            AdapterTypeRegistry((guided, plain)),
        )
        report = build_readiness_report(None)
        states = {p["transport"]: p for p in report["manifest"]["providers"]}
        assert states["matrix"]["registration"] == "registered"
        assert states["matrix"]["native_setup"] == "provider_guided"
        assert states["meshtastic"]["registration"] == "registered"
        assert states["meshtastic"]["native_setup"] == "manual_provisioning"

    def test_broken_registration_reports_only_a_typed_category(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Quarantined providers reduce to a typed category without detail."""
        _stub_manifest(monkeypatch, tmp_path, [_entry("matrix")])
        failure = AdapterDiscoveryFailure(
            "matrix",
            "broken_plugin:spec",
            "ImportError: cannot import name 'homeserver-secret-value'",
        )
        monkeypatch.setattr(
            adapter_registry,
            "ADAPTER_REGISTRY",
            AdapterTypeRegistry((), failures=(failure,)),
        )
        report = build_readiness_report(None)
        provider = report["manifest"]["providers"][0]
        assert provider["registration"] == "broken"
        assert provider["registration_error_category"] == "ImportError"
        assert "homeserver-secret-value" not in to_json(report)

    def test_unavailable_snapshot_is_a_typed_outcome(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(readiness_report, "_locate_manifest", lambda: None)
        report = build_readiness_report(None)
        assert report["manifest"]["status"] == "not_found"
        assert report["manifest"]["providers"] == []
        assert any("checkout" in step for step in report["guidance"])

    @pytest.mark.parametrize(
        "entries",
        [
            [],
            {},
            [_entry("matrix", ref="short")],
            [_entry("matrix", repository="no-owner-here")],
            [_entry("matrix"), _entry("matrix")],
            [_entry("matrix", distribution="")],
        ],
        ids=[
            "empty",
            "non-list",
            "short-revision",
            "unshaped-repository",
            "duplicate-transport",
            "unnamed-distribution",
        ],
    )
    def test_malformed_snapshot_fails_closed(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, entries: Any
    ) -> None:
        _stub_manifest(monkeypatch, tmp_path, entries)
        report = build_readiness_report(None)
        assert report["manifest"]["status"] == "malformed"
        assert report["manifest"]["providers"] == []

    def test_unparsable_snapshot_fails_closed(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        _stub_manifest(monkeypatch, tmp_path, "{not json")
        report = build_readiness_report(None)
        assert report["manifest"]["status"] == "malformed"


# ---------------------------------------------------------------------------
# Configuration and per-adapter section
# ---------------------------------------------------------------------------


class TestConfiguredAdapters:
    def test_fake_provider_config_passes_offline_preflight(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        _install_spec(monkeypatch)
        report = build_readiness_report(_write_config(tmp_path, CONFIG))
        assert report["config"]["status"] == "valid"
        assert report["config"]["adapter_count"] == 2
        rows = {row["adapter_id"]: row for row in report["adapters"]}
        assert set(rows) == {"left", "right"}
        for row in rows.values():
            assert row["adapter_kind"] == "fake"
            assert row["sdk_distribution_status"] == "not_required"
            assert row["config"] == "valid"
            assert row["preflight"] == "passed"
            assert row["native_setup"] == "manual_provisioning"
            assert row["runtime"] == "not_observed"
            assert row["recipient"] == "not_observed"
            assert row["blocked_category"] is None
        routes = report["config"]["routes"]
        assert routes["status"] == "passed"
        assert routes["total"] == 0

    def test_config_values_and_paths_never_enter_the_report(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        _install_spec(monkeypatch)
        report = build_readiness_report(_write_config(tmp_path, CONFIG))
        text = to_json(report)
        assert "keep-this-private" not in text
        assert "provider-declared-sensitive-value" not in text
        assert str(tmp_path) not in text
        assert str(readiness_report._PACKAGE_ROOT) not in text

    def test_report_is_deterministic(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        _stub_manifest(
            monkeypatch,
            tmp_path,
            [_entry("matrix"), _entry("meshtastic"), _entry("lxmf")],
        )
        _install_spec(monkeypatch)
        path = _write_config(tmp_path, CONFIG)
        first = to_json(build_readiness_report(path))
        second = to_json(build_readiness_report(path))
        assert first == second

    def test_missing_sdk_distribution_blocks_real_adapter(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        _install_spec(monkeypatch, distribution="some-sdk-dist")
        path = _write_config(
            tmp_path,
            CONFIG.replace("adapter_kind: fake", "adapter_kind: real"),
        )
        report = build_readiness_report(path)
        assert report["adapters"]
        for row in report["adapters"]:
            assert row["sdk_distribution"] == "some-sdk-dist"
            assert row["sdk_distribution_status"] == "missing"
            assert row["preflight"] == "blocked"
            assert row["blocked_category"] == "sdk_distribution_missing"
            assert row["config"] == "valid"

    def test_preparation_failure_is_actionable(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        _install_spec(
            monkeypatch,
            runtime_config_preparer=SymbolRef(__name__, "_failing_preparer"),
        )
        report = build_readiness_report(_write_config(tmp_path, CONFIG))
        assert report["adapters"]
        for row in report["adapters"]:
            assert row["preflight"] == "blocked"
            assert row["blocked_category"] == "preparation_failed"
            assert row["config"] == "actionable_error"
        text = to_json(report)
        assert "provider refused this configuration" not in text

    def test_invalid_instance_config_reports_section_and_category(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        _install_spec(monkeypatch)
        path = _write_config(
            tmp_path,
            CONFIG.replace("adapter_kind: fake", "adapter_kind: not-a-kind", 1),
        )
        report = build_readiness_report(path)
        assert report["config"]["status"] == "validation_error"
        assert report["config"]["error_category"] == "validation_error"
        assert (report["config"]["section_path"] or "").startswith("adapters.")
        assert report["adapters"] == []
        assert "not-a-kind" not in to_json(report)

    def test_explicit_missing_config_is_typed(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        report = build_readiness_report(tmp_path / "absent.yaml")
        assert report["config"]["status"] == "config_file_error"
        assert report["config"]["routes"] is None
        assert report["adapters"] == []


class TestDefensiveAdapterRows:
    """Rows answer truthfully even when the registry diverges from config."""

    @staticmethod
    def _fake_config() -> SimpleNamespace:
        wrapper = SimpleNamespace(enabled=True, adapter_kind="real", config=None)
        return SimpleNamespace(
            adapters=SimpleNamespace(all_configs=lambda: [("matrix", "bot", wrapper)]),
            routes=SimpleNamespace(routes=[]),
        )

    def test_unregistered_transport_row(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            adapter_registry, "ADAPTER_REGISTRY", AdapterTypeRegistry(())
        )
        rows = readiness_report._adapter_rows(self._fake_config(), [])
        assert rows[0]["blocked_category"] == "transport_unregistered"
        assert rows[0]["preflight"] == "blocked"
        assert rows[0]["sdk_distribution_status"] == "unknown"

    def test_broken_transport_row(self, monkeypatch: pytest.MonkeyPatch) -> None:
        failure = AdapterDiscoveryFailure("matrix", "ep:val", "ValueError: broken")
        monkeypatch.setattr(
            adapter_registry,
            "ADAPTER_REGISTRY",
            AdapterTypeRegistry((), failures=(failure,)),
        )
        rows = readiness_report._adapter_rows(self._fake_config(), [])
        assert rows[0]["blocked_category"] == "transport_broken"

    def test_manifest_provider_distribution_is_visible(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            adapter_registry, "ADAPTER_REGISTRY", AdapterTypeRegistry(())
        )
        providers = [{"transport": "matrix", "distribution": "medre-adapter-matrix"}]
        _stub_distributions(monkeypatch, {"medre-adapter-matrix": "1.2.3"})
        rows = readiness_report._adapter_rows(self._fake_config(), providers)
        assert rows[0]["provider_distribution"] == "medre-adapter-matrix"
        assert rows[0]["provider_distribution_status"] == "installed"


# ---------------------------------------------------------------------------
# CLI surface
# ---------------------------------------------------------------------------


class TestReadinessCommand:
    def test_json_mode_is_machine_readable(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        _stub_manifest(monkeypatch, tmp_path, [_entry("matrix"), _entry("meshtastic")])
        _install_spec(monkeypatch)
        path = _write_config(tmp_path, CONFIG)
        main(["readiness", "--config", str(path), "--json"])
        report = json.loads(capsys.readouterr().out)
        assert report["report_version"] == 1
        assert report["boundary"]["runtime"] == "not_observed"
        assert report["boundary"]["recipients"] == "not_observed"
        assert len(report["manifest"]["providers"]) == 2

    def test_human_mode_names_missing_providers_and_boundary(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        _stub_manifest(
            monkeypatch,
            tmp_path,
            [_entry("matrix"), _entry("meshtastic"), _entry("meshcore")],
        )
        main(["readiness"])
        out = capsys.readouterr().out
        assert "Offline readiness report" in out
        assert "medre-adapter-matrix" in out
        assert "missing" in out
        assert "never observe the live runtime" in out
        assert "never verify recipients" in out
        assert str(tmp_path) not in out

    def test_zero_adapters_installed_lists_every_provider(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Zero installed providers is a first-class outcome, not an error."""
        _stub_manifest(
            monkeypatch,
            tmp_path,
            [
                _entry(name)
                for name in (
                    "matrix",
                    "meshtastic",
                    "meshcore",
                    "lxmf",
                    "xmpp",
                    "chatmail",
                    "simplexchat",
                )
            ],
        )
        _stub_distributions(monkeypatch, {})
        main(["readiness", "--json"])
        report = json.loads(capsys.readouterr().out)
        assert len(report["manifest"]["providers"]) == 7
        assert all(
            p["distribution_status"] == "missing"
            for p in report["manifest"]["providers"]
        )
        assert report["config"]["status"] == "config_not_found"
        assert any("adapter-installation.md" in step for step in report["guidance"])

    def test_help_is_available(self, capsys: pytest.CaptureFixture[str]) -> None:
        with pytest.raises(SystemExit) as excinfo:
            main(["readiness", "--help"])
        assert excinfo.value.code == 0
        assert "readiness" in capsys.readouterr().out
