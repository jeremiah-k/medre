"""Human rendering of the offline readiness report (pure formatting).

The renderer prints a path-free operator view. These tests pin every
branch of that view: provider registration suffixes, configuration
validity, adapter fact lines, route counts, and the degraded outcomes
(unavailable snapshot, unshaped sections, empty guidance).
"""

from __future__ import annotations

from typing import Any

import pytest

from medre.cli.readiness_commands import (
    _adapter_line,
    _config_line,
    _provider_line,
    _render_human,
    _routes_line,
)


def _provider(**overrides: Any) -> dict[str, Any]:
    provider: dict[str, Any] = {
        "transport": "matrix",
        "distribution": "medre-adapter-matrix",
        "ref": "a" * 40,
        "distribution_status": "missing",
        "registration": "registered",
    }
    provider.update(overrides)
    return provider


def _report(**overrides: Any) -> dict[str, Any]:
    report: dict[str, Any] = {
        "manifest": {
            "status": "available",
            "providers": [],
            "provider_count": 0,
            "installed_count": 0,
        },
        "config": {},
        "adapters": [],
        "boundary": {},
        "guidance": [],
    }
    report.update(overrides)
    return report


def test_provider_line_marks_unregistered_registration() -> None:
    line = _provider_line(_provider(registration="unregistered"))
    assert line.endswith("(unregistered)")
    assert "medre-adapter-matrix @ aaaaaaaa" in line


def test_provider_line_omits_registered_suffix() -> None:
    assert "(registered)" not in _provider_line(_provider())


def test_valid_configuration_line_carries_source_and_counts() -> None:
    line = _config_line(
        {
            "status": "valid",
            "source": "explicit",
            "adapter_count": 2,
            "enabled_adapter_count": 1,
        }
    )
    assert line == "Configuration: valid (source explicit; 2 adapter(s), 1 enabled)"


def test_routes_line_reports_plan_counts_when_passed() -> None:
    assert (
        _routes_line({"status": "passed", "total": 3, "enabled": 2})
        == "Routes: plan passed (3 configured (2 enabled))"
    )


@pytest.mark.parametrize(
    "row, fragment",
    [
        (
            {"sdk_distribution_status": "missing", "sdk_distribution": "sdk-dist"},
            "sdk distribution sdk-dist missing",
        ),
        ({"sdk_distribution_status": "unknown"}, "sdk requirement unknown"),
        (
            {
                "provider_distribution_status": "missing",
                "provider_distribution": "medre-adapter-matrix",
            },
            "provider distribution medre-adapter-matrix missing",
        ),
        (
            {"config": "actionable_error", "blocked_category": "config_invalid"},
            "config error [config_invalid]",
        ),
        (
            {"preflight": "blocked", "blocked_category": "sdk_distribution_missing"},
            "preflight blocked [sdk_distribution_missing]",
        ),
        ({}, "preflight unknown"),
    ],
    ids=[
        "sdk-missing",
        "sdk-unknown",
        "provider-missing",
        "config-error",
        "preflight-blocked",
        "no-facts",
    ],
)
def test_adapter_line_reports_each_offline_fact(
    row: dict[str, Any], fragment: str
) -> None:
    line = _adapter_line(
        {"adapter_id": "bot", "adapter_kind": "real", "enabled": True, **row}
    )
    assert line.startswith("  bot (real, enabled): ")
    assert fragment in line


def test_render_unavailable_manifest_reports_typed_status(
    capsys: pytest.CaptureFixture[str],
) -> None:
    _render_human(_report(manifest={"status": "malformed"}))
    out = capsys.readouterr().out
    assert "Compatibility snapshot: unavailable [malformed]" in out


def test_render_non_mapping_manifest_falls_back_to_unknown(
    capsys: pytest.CaptureFixture[str],
) -> None:
    _render_human(_report(manifest=None))
    out = capsys.readouterr().out
    assert "Compatibility snapshot: unavailable [unknown]" in out


def test_render_non_list_providers_print_only_snapshot_line(
    capsys: pytest.CaptureFixture[str],
) -> None:
    manifest = {
        "status": "available",
        "providers": None,
        "provider_count": 0,
        "installed_count": 0,
    }
    _render_human(_report(manifest=manifest))
    out = capsys.readouterr().out
    assert "Compatibility snapshot: 0 provider(s), 0 installed" in out
    assert "Routes: not evaluated (configuration not loaded)" in out


def test_render_skips_unshaped_provider_entries(
    capsys: pytest.CaptureFixture[str],
) -> None:
    manifest = {
        "status": "available",
        "providers": [_provider(), "not-a-mapping", None],
        "provider_count": 3,
        "installed_count": 0,
    }
    _render_human(_report(manifest=manifest))
    out = capsys.readouterr().out
    assert out.count("medre-adapter-matrix @ aaaaaaaa") == 1


def test_render_non_mapping_config_falls_back_to_unknown(
    capsys: pytest.CaptureFixture[str],
) -> None:
    _render_human(_report(config=None))
    out = capsys.readouterr().out
    assert "Configuration: unknown (no source) [unknown]" in out
    assert "Routes: not evaluated (configuration not loaded)" in out


def test_render_lists_adapter_rows_and_skips_unshaped_ones(
    capsys: pytest.CaptureFixture[str],
) -> None:
    row = {
        "adapter_id": "bot",
        "adapter_kind": "fake",
        "enabled": False,
        "preflight": "passed",
    }
    _render_human(_report(adapters=[row, "not-a-mapping"]))
    out = capsys.readouterr().out
    assert "Adapters:" in out
    assert "bot (fake, disabled): preflight passed" in out


def test_render_omits_empty_boundary_and_guidance_sections(
    capsys: pytest.CaptureFixture[str],
) -> None:
    _render_human(_report())
    out = capsys.readouterr().out
    assert "Boundary:" not in out
    assert "Next steps:" not in out


def test_render_prints_boundary_statement_and_guidance_steps(
    capsys: pytest.CaptureFixture[str],
) -> None:
    _render_human(
        _report(
            boundary={"statement": "Offline checks run without SDK sessions."},
            guidance=["Install missing provider distributions"],
        )
    )
    out = capsys.readouterr().out
    assert "Boundary: Offline checks run without SDK sessions." in out
    assert "  - Install missing provider distributions" in out
