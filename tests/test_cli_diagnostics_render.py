# medre-test-tier: compatibility
"""Human rendering of 'medre diagnostics' (readiness-style default output).

The default output is the short, path-free operator summary in the
readiness-report style; the machine snapshot is explicit via
``--format json`` (and ``--format prometheus``). These tests pin the
summary sections, the typed-status fallbacks for unshaped sections, the
ordered next steps, and the format-flag behavior.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from medre.cli.diagnostics_commands import (
    _human_adapter_line,
    _human_capacity_line,
    _human_lifecycle_line,
    _human_outbox_line,
    _human_replay_line,
    _human_retry_line,
    _human_routes_line,
    _render_human,
)
from tests.helpers.cli import _run_cli, _run_cli_both, _run_cli_raw

pytestmark = pytest.mark.usefixtures("isolated_config_env")

_CLOSING_STEP = (
    "Snapshots are point-in-time observations; they do not establish "
    "delivery success or native acceptance."
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def config_with_routes(tmp_path: Path) -> Path:
    p = tmp_path / "config.yaml"
    p.write_text("""\
runtime:
  name: test-render
logging:
  level: INFO
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
  meshtastic:
    radio:
      enabled: true
      adapter_kind: fake
      connection_type: serial
      serial_port: /dev/ttyACM0
      origin_label: TestMesh
routes:
  matrix_to_radio:
    source_adapters: [main]
    dest_adapters: [radio]
    directionality: source_to_dest
    enabled: true
""")
    return p


def test_default_output_is_human_summary(config_with_routes: Path) -> None:
    output = _run_cli("diagnostics", "--config", str(config_with_routes))
    assert output.startswith("Runtime diagnostics snapshot (build)\n")
    assert "Runtime state: initialized" in output
    assert "Adapters: 2 built" in output
    assert "Build failures: none" in output
    assert "Routes (build eligibility): 1 configured" in output
    assert "Capacity: inbound 0/100, delivery 0/100" in output
    assert "Outbox: no counts reported" in output
    assert "Replay: available" in output
    assert "Retry: disabled" in output
    assert "Next steps:" in output
    assert _CLOSING_STEP in output


def test_default_output_is_not_json(config_with_routes: Path) -> None:
    output = _run_cli("diagnostics", "--config", str(config_with_routes))
    with pytest.raises(json.JSONDecodeError):
        json.loads(output)


def test_explicit_json_format_is_machine_snapshot(config_with_routes: Path) -> None:
    output = _run_cli(
        "diagnostics",
        "--format",
        "json",
        "--config",
        str(config_with_routes),
    )
    parsed = json.loads(output)
    assert parsed["snapshot_scope"] == "build"
    assert isinstance(parsed["adapters"], dict)


def test_explicit_human_format_matches_default(config_with_routes: Path) -> None:
    default_output = _run_cli("diagnostics", "--config", str(config_with_routes))
    explicit = _run_cli(
        "diagnostics",
        "--format",
        "human",
        "--config",
        str(config_with_routes),
    )
    assert explicit == default_output


def test_unknown_format_is_rejected(config_with_routes: Path) -> None:
    _, stderr, code = _run_cli_raw(
        "diagnostics",
        "--format",
        "yaml",
        "--config",
        str(config_with_routes),
    )
    assert code != 0
    assert "invalid choice" in stderr


def test_prometheus_format_still_available(config_with_routes: Path) -> None:
    output = _run_cli(
        "diagnostics",
        "--format",
        "prometheus",
        "--config",
        str(config_with_routes),
    )
    assert "# TYPE medre_" in output


def test_human_output_carries_no_json_structure(config_with_routes: Path) -> None:
    """The summary is a rendering, not a schema: no JSON quotes or keys."""
    output = _run_cli("diagnostics", "--config", str(config_with_routes))
    assert '"' not in output
    assert "schema_version" not in output


def test_help_states_human_default_and_json_flag() -> None:
    stdout, _stderr = _run_cli_both("--help")
    help_text = " ".join(stdout.lower().split())
    assert "read-only" in help_text
    assert "config" in help_text
    assert "--format json" in help_text


CONFIG_FAKE_SINGLE = """\
runtime:
  name: diag-refresh-render
storage:
  backend: memory
adapters:
  matrix:
    fake_main:
      enabled: true
      adapter_kind: fake
      homeserver: https://fake.test
      user_id: '@fake:fake.test'
      access_token: fake_token_for_test
      room_allowlist: ['!fake:fake.test']
      encryption_mode: plaintext
"""


@pytest.fixture()
def fake_single_config(tmp_path: Path) -> Path:
    p = tmp_path / "config.yaml"
    p.write_text(CONFIG_FAKE_SINGLE)
    return p


def test_refresh_health_human_shows_live_health(
    fake_single_config: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for var in ("MEDRE_HOME", "XDG_CONFIG_HOME", "XDG_STATE_HOME"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("MEDRE_HOME", str(tmp_path))

    output = _run_cli(
        "diagnostics",
        "--refresh-health",
        "--format",
        "human",
        "--config",
        str(fake_single_config),
    )
    assert output.startswith("Runtime diagnostics snapshot (live)\n")
    assert "Startup outcome: success" in output
    assert "state ready" in output
    assert "Live health: healthy (" in output
    assert "  fake_main: health healthy" in output
    assert _CLOSING_STEP in output


def test_refresh_health_json_remains_explicit(
    fake_single_config: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for var in ("MEDRE_HOME", "XDG_CONFIG_HOME", "XDG_STATE_HOME"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("MEDRE_HOME", str(tmp_path))

    output = _run_cli(
        "diagnostics",
        "--refresh-health",
        "--format",
        "json",
        "--config",
        str(fake_single_config),
    )
    parsed = json.loads(output)
    assert parsed["snapshot_scope"] == "live"
    assert parsed["health"]["live_refresh"] is True


# ---------------------------------------------------------------------------
# Section line helpers (pure formatting)
# ---------------------------------------------------------------------------


def test_lifecycle_line_shows_uptime_when_started() -> None:
    assert (
        _human_lifecycle_line({"runtime_state": "running", "uptime_seconds": 12.5})
        == "Runtime state: running (uptime 12.5s)"
    )


def test_lifecycle_line_marks_unstarted_runtime() -> None:
    assert (
        _human_lifecycle_line({"runtime_state": "initialized", "uptime_seconds": None})
        == "Runtime state: initialized (not started)"
    )


def test_lifecycle_line_degrades_without_section() -> None:
    assert _human_lifecycle_line(None) == "Runtime state: unknown (not started)"


def test_adapter_line_shows_platform_role_health() -> None:
    row = {"adapter_id": "bot", "platform": "matrix", "role": "presentation"}
    assert _human_adapter_line(row) == "  bot (matrix, presentation): health unknown"


@pytest.mark.parametrize(
    "routes, expected",
    [
        (None, "Routes (build eligibility): not reported"),
        ("not-a-mapping", "Routes (build eligibility): not reported"),
        (
            {"eligibility": {"configured": ["a", "b"]}},
            "Routes (build eligibility): 2 configured (all eligible)",
        ),
        (
            {"eligibility": {"configured": ["a"], "degraded": ["a"]}},
            "Routes (build eligibility): 1 configured (1 degraded)",
        ),
        (
            {
                "eligibility": {
                    "configured": ["a", "b"],
                    "disabled": ["b"],
                    "unavailable": ["b"],
                    "skipped": ["b"],
                }
            },
            "Routes (build eligibility): 2 configured (1 disabled, 1 unavailable, 1 skipped)",
        ),
    ],
    ids=["none", "unshaped", "all-eligible", "degraded", "mixed-facts"],
)
def test_routes_line_variants(routes: object, expected: str) -> None:
    assert _human_routes_line(routes) == expected


@pytest.mark.parametrize(
    "state, expected",
    [
        (None, "Capacity: state unavailable"),
        ("not-a-mapping", "Capacity: state unavailable"),
        (
            {
                "inbound_current": 0,
                "inbound_limit": 100,
                "delivery_current": 1,
                "delivery_limit": 50,
            },
            "Capacity: inbound 0/100, delivery 1/50",
        ),
        (
            {
                "inbound_current": 0,
                "inbound_limit": 100,
                "delivery_current": 0,
                "delivery_limit": 100,
                "accepting_work": False,
            },
            "Capacity: inbound 0/100, delivery 0/100, not accepting work",
        ),
    ],
    ids=["none", "unshaped", "counters", "not-accepting"],
)
def test_capacity_line_variants(state: object, expected: str) -> None:
    assert _human_capacity_line(state) == expected


@pytest.mark.parametrize(
    "outbox, expected",
    [
        (None, "Outbox: counts unavailable"),
        ({"counts": None}, "Outbox: counts unavailable"),
        ({"counts": {}}, "Outbox: no counts reported"),
        (
            {"counts": {"pending": 2, "failed": 1, "sent": 5}},
            "Outbox: failed=1, pending=2, sent=5",
        ),
    ],
    ids=["none", "null-counts", "empty-counts", "sorted-counts"],
)
def test_outbox_line_variants(outbox: object, expected: str) -> None:
    assert _human_outbox_line(outbox) == expected


@pytest.mark.parametrize(
    "replay, expected",
    [
        (None, "Replay: state unavailable"),
        ({"available": True}, "Replay: available"),
        ({"available": False}, "Replay: unavailable"),
    ],
    ids=["none", "available", "unavailable"],
)
def test_replay_line_variants(replay: object, expected: str) -> None:
    assert _human_replay_line(replay) == expected


@pytest.mark.parametrize(
    "retry, expected",
    [
        (None, "Retry: state unavailable"),
        ({"enabled": False}, "Retry: disabled"),
        ({"enabled": True}, "Retry: enabled"),
        (
            {"enabled": True, "failed": 2, "dead_lettered": 1},
            "Retry: enabled (2 failed, 1 dead_lettered)",
        ),
    ],
    ids=["none", "disabled", "enabled", "enabled-with-facts"],
)
def test_retry_line_variants(retry: object, expected: str) -> None:
    assert _human_retry_line(retry) == expected


# ---------------------------------------------------------------------------
# Full-summary rendering branches
# ---------------------------------------------------------------------------


def _snapshot(**overrides: object) -> dict[str, object]:
    snapshot: dict[str, object] = {
        "snapshot_scope": "build",
        "lifecycle": {"runtime_state": "initialized", "uptime_seconds": None},
        "adapters": {
            "bot": {
                "adapter_id": "bot",
                "platform": "matrix",
                "role": "presentation",
                "health": "unknown",
            }
        },
        "startup": {"build_failures": []},
        "routes": {"eligibility": {"configured": ["r"]}},
        "capacity": {
            "state": {
                "inbound_current": 0,
                "inbound_limit": 100,
                "delivery_current": 0,
                "delivery_limit": 100,
            }
        },
        "outbox": {"counts": {}},
        "replay": {"available": True},
        "retry": {"enabled": False},
        "health": {"live_health": None},
    }
    snapshot.update(overrides)
    return snapshot


def test_render_lists_build_failures_with_next_step(
    capsys: pytest.CaptureFixture[str],
) -> None:
    _render_human(
        _snapshot(
            startup={
                "build_failures": [
                    {"adapter_id": "broken", "error": "sanitized detail"}
                ]
            }
        )
    )
    out = capsys.readouterr().out
    assert "Build failures: 1 adapter(s) failed to construct" in out
    assert "  broken: failed to construct" in out
    # Sanitized error text never enters the human summary.
    assert "sanitized detail" not in out
    assert "startup.build_failures" in out


def test_render_unhealthy_adapters_add_refresh_next_step(
    capsys: pytest.CaptureFixture[str],
) -> None:
    _render_human(
        _snapshot(
            adapters={
                "bot": {
                    "adapter_id": "bot",
                    "platform": "matrix",
                    "role": "presentation",
                    "health": "degraded",
                }
            }
        )
    )
    out = capsys.readouterr().out
    assert "--refresh-health" in out


def test_render_unhealthy_live_health_adds_refresh_next_step(
    capsys: pytest.CaptureFixture[str],
) -> None:
    _render_human(
        _snapshot(
            snapshot_scope="live",
            health={
                "live_health": {
                    "runtime_health": "degraded",
                    "adapter_summary": {"healthy": 0, "degraded": 1, "total": 1},
                    "adapters": {"bot": {"adapter_id": "bot", "health": "degraded"}},
                }
            },
        )
    )
    out = capsys.readouterr().out
    assert "Live health: degraded (degraded=1, healthy=0, total=1)" in out
    assert "  bot: health degraded" in out
    assert "--refresh-health" in out


def test_render_degraded_routes_add_plan_next_step(
    capsys: pytest.CaptureFixture[str],
) -> None:
    _render_human(
        _snapshot(routes={"eligibility": {"configured": ["r"], "degraded": ["r"]}})
    )
    out = capsys.readouterr().out
    assert "'medre routes plan'" in out


def test_render_capacity_backpressure_adds_next_step(
    capsys: pytest.CaptureFixture[str],
) -> None:
    _render_human(
        _snapshot(
            capacity={
                "state": {
                    "inbound_current": 0,
                    "inbound_limit": 100,
                    "delivery_current": 0,
                    "delivery_limit": 100,
                    "accepting_work": False,
                }
            }
        )
    )
    out = capsys.readouterr().out
    assert "not accepting work" in out
    assert "capacity.state" in out


def test_render_skips_unshaped_sections(
    capsys: pytest.CaptureFixture[str],
) -> None:
    _render_human(
        _snapshot(
            adapters=None,
            startup=None,
            routes=None,
            capacity=None,
            outbox=None,
            replay=None,
            retry=None,
        )
    )
    out = capsys.readouterr().out
    assert "Adapters: 0 built" in out
    assert "Build failures: none" in out
    assert "Routes (build eligibility): not reported" in out
    assert "Capacity: state unavailable" in out
    assert "Outbox: counts unavailable" in out
    assert "Replay: state unavailable" in out
    assert "Retry: state unavailable" in out


@pytest.mark.parametrize(
    "live_health, expected_warning",
    [("healthy", False), ("failed", True), (None, True)],
)
def test_live_health_supersedes_startup_for_each_adapter(
    capsys: pytest.CaptureFixture[str], live_health: str | None, expected_warning: bool
) -> None:
    live_rows = {"bot": {"health": live_health}} if live_health is not None else {}
    _render_human(
        _snapshot(
            adapters={"bot": {"adapter_id": "bot", "health": "degraded"}},
            health={"live_health": {"adapters": live_rows}},
        )
    )
    out = capsys.readouterr().out
    assert ("Degraded or failed adapter health reported" in out) is expected_warning


def test_live_health_does_not_hide_unobserved_adapter(
    capsys: pytest.CaptureFixture[str],
) -> None:
    _render_human(
        _snapshot(
            adapters={
                "bot": {"adapter_id": "bot", "health": "degraded"},
                "other": {"adapter_id": "other", "health": "failed"},
            },
            health={"live_health": {"adapters": {"bot": {"health": "healthy"}}}},
        )
    )
    assert "Degraded or failed adapter health reported" in capsys.readouterr().out


def test_render_keeps_build_and_startup_authorities_separate(
    capsys: pytest.CaptureFixture[str],
) -> None:
    _render_human(
        _snapshot(
            snapshot_scope="live",
            lifecycle={
                "runtime_state": "running",
                "uptime_seconds": 2,
                "adapters": {"bot": "failed"},
            },
            startup={
                "build_failures": [],
                "boot_summary": {
                    "startup_outcome": "partial",
                    "failed_adapter_ids": ["bot"],
                    "storage_path": "/private/location",
                },
            },
            routes={
                "eligibility": {"configured": ["r"], "registered": ["r"]},
                "startup_readiness": {
                    "readiness": {"r": "skipped"},
                    "skipped": [
                        {"route_id": "r", "reason": "no_surviving_targets_start_failed"}
                    ],
                },
            },
            outbox={"counts": {}},
        )
    )
    out = capsys.readouterr().out
    assert "state failed, health unknown" in out
    assert "Startup outcome: partial" in out
    assert "bot: failed to start" in out
    assert "Routes (build eligibility): 1 configured (all eligible)" in out
    assert "Routes (startup readiness): 1 skipped" in out
    assert "routes.startup_readiness" in out
    assert "Reuse the original configuration selection" in out
    assert "--config if supplied" in out
    assert "Outbox: no counts reported" in out
    assert "/private/location" not in out


def test_follow_up_config_note_does_not_echo_path(
    capsys: pytest.CaptureFixture[str],
) -> None:
    _render_human(
        _snapshot(routes={"eligibility": {"configured": ["r"], "skipped": ["r"]}})
    )
    out = capsys.readouterr().out
    assert "medre routes plan" in out
    assert "Reuse the original configuration selection" in out
    assert out.index("Reuse the original configuration selection") < out.index(
        _CLOSING_STEP
    )
