"""Checkout development selection, environment ownership, and subprocess boundaries."""

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from medre.cli.main import main
from medre.dev_environment import (
    DevEnvironment,
    checkout_root,
    development_process_environment,
)
from medre.tool_management import ToolManagementError


@pytest.fixture(autouse=True)
def available_uv(monkeypatch):
    # These tests inspect commands or mock execution. uv is an explicit
    # runtime prerequisite, not a prerequisite for a pip-installed test suite.
    monkeypatch.setattr(
        "medre.dev_environment.shutil.which",
        lambda name: "/test-tools/uv" if name == "uv" else None,
    )


@pytest.fixture
def checkout(tmp_path):
    root = tmp_path / "core"
    root.mkdir()
    (root / "pyproject.toml").write_text('[project]\nname = "medre"\n')
    (root / "uv.lock").touch()
    (root / ".github").mkdir()
    (root / ".github/adapter-compatibility.json").write_text(
        json.dumps(
            [
                {
                    "transport": "matrix",
                    "distribution": "medre-adapter-matrix",
                    "repository": "owner/matrix",
                    "ref": "a" * 40,
                },
                {
                    "transport": "meshcore",
                    "distribution": "medre-adapter-meshcore",
                    "repository": "owner/meshcore",
                    "ref": "b" * 40,
                },
            ]
        )
    )
    return root


def arguments(root, **overrides):
    values = dict(
        project=str(root),
        profile=None,
        sources=None,
        sdk=None,
        editable=[],
        adapters=[],
    )
    values.update(overrides)
    return SimpleNamespace(**values)


def adapter_checkout(tmp_path, name="matrix"):
    path = tmp_path / "a different layout" / name
    path.mkdir(parents=True)
    (path / "pyproject.toml").write_text(f"""[project]
name = "medre-adapter-{name}"
[project.entry-points."medre.adapters"]
{name} = "provider:spec"
""")
    return path


@pytest.fixture
def posix_launcher(monkeypatch):
    # The launcher's win32 branch spawns a real pytest subprocess; these
    # tests exercise the POSIX execv handoff seam deterministically.
    import sys

    monkeypatch.setattr(sys, "platform", "linux")


def test_default_selection_uses_exact_snapshot_without_sdk_deps(checkout):
    env = DevEnvironment.inspect(arguments(checkout))
    commands = env.sync_commands()
    assert env.adapters == ["matrix", "meshcore"]
    assert "--locked" in commands[0] and "--extra" in commands[0]
    assert "--no-deps" in commands[1]
    assert (
        f"medre-adapter-matrix @ git+https://github.com/owner/matrix.git@{'a' * 40}"
        in commands[1]
    )
    assert (
        checkout.as_uri() in commands[1]
    )  # editable core cannot be replaced by index MEDRE
    assert str(env.python) == commands[1][commands[1].index("--python") + 1]


def test_sdk_mode_constrains_resolution_to_exported_core_lock(checkout):
    env = DevEnvironment.inspect(arguments(checkout, sdk=True))
    sync, export, install = env.sync_commands(python="3.13")
    assert sync[-2:] == ["--python", "3.13"]
    assert export[1] == "export" and "--locked" in export
    assert "--no-emit-project" in export
    assert (
        install[install.index("--constraint") + 1]
        == (checkout / ".venv/medre-core-constraints.txt").as_uri()
    )
    assert "--no-deps" not in install


def test_local_profile_and_relative_paths_are_independent_of_cwd(
    checkout, tmp_path, monkeypatch
):
    adapter = adapter_checkout(tmp_path)
    (checkout / ".medre-dev.yaml").write_text(
        json.dumps(
            {
                "adapters": ["matrix"],
                "sdk": True,
                "editable": {
                    "matrix": "../" + adapter.relative_to(tmp_path).as_posix()
                },
            }
        )
    )
    monkeypatch.chdir(tmp_path)
    env = DevEnvironment.inspect(arguments(checkout))
    assert env.editable == {"matrix": adapter}
    assert env.sdk is True
    install = env.sync_commands()[-1]
    assert adapter.as_uri() in install
    assert all("git+" not in item for item in install)


def test_explicit_path_override_validates_distribution_before_sync(checkout, tmp_path):
    wrong = adapter_checkout(tmp_path, "meshcore")
    with pytest.raises(ToolManagementError, match="wrong distribution"):
        DevEnvironment.inspect(arguments(checkout, editable=[f"matrix={wrong}"]))


@pytest.mark.parametrize(
    "content",
    [
        "sdk: yesplease",
        "adapters: matrix",
        "editable: []",
        "surprise: true",
        "adapters: [matrix, 'matrix[e2e]']",
        "sdk: false\nsdk: true",
    ],
)
def test_profile_rejects_ambiguous_settings(checkout, content):
    (checkout / ".medre-dev.yaml").write_text(content)
    with pytest.raises(ValueError):
        DevEnvironment.inspect(arguments(checkout))


def test_catalog_defaults_remain_the_snapshot_for_unmodified_aliases(
    checkout, tmp_path
):
    catalog = tmp_path / "sources.yaml"
    catalog.write_text(
        "adapters:\n  matrix: vendor-matrix @ git+https://example.org/vendor.git@candidate\n"
    )
    env = DevEnvironment.inspect(arguments(checkout, sources=str(catalog)))
    assert env.sources["matrix"].startswith("vendor-matrix @")
    assert env.sources["meshcore"].endswith("b" * 40)


def test_core_only_profile_syncs_without_installing_adapters(checkout):
    (checkout / ".medre-dev.yaml").write_text("adapters: []\n")
    env = DevEnvironment.inspect(arguments(checkout))
    assert len(env.sync_commands()) == 1


def test_checkout_discovery_stops_at_other_projects(checkout, monkeypatch):
    nested = checkout / "other"
    nested.mkdir()
    (nested / "pyproject.toml").write_text('[project]\nname = "other"\n')
    monkeypatch.chdir(nested)
    with pytest.raises(ToolManagementError, match="MEDRE checkout"):
        checkout_root()


def test_checkout_refuses_symlinked_environment(checkout, tmp_path):
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    try:
        (checkout / ".venv").symlink_to(foreign, target_is_directory=True)
    except OSError:
        pytest.skip("symlink creation unavailable")
    with pytest.raises(ToolManagementError, match="symlink"):
        checkout_root(str(checkout))


def test_process_environment_keeps_package_config_but_clears_target_overrides(
    monkeypatch,
):
    monkeypatch.setenv("UV_PROJECT_ENVIRONMENT", "/foreign")
    monkeypatch.setenv("VIRTUAL_ENV", "/foreign")
    monkeypatch.setenv("PYTHONPATH", "/foreign/source")
    monkeypatch.setenv("UV_INDEX_URL", "https://example.org/simple")
    result = development_process_environment()
    assert not {"UV_PROJECT_ENVIRONMENT", "VIRTUAL_ENV", "PYTHONPATH"} & result.keys()
    assert result["UV_INDEX_URL"] == "https://example.org/simple"


def test_dry_run_has_no_subprocess_or_profile_writes(checkout, monkeypatch, capsys):
    def forbidden(*args, **kwargs):
        pytest.fail("dry-run must not run uv")

    monkeypatch.setattr(subprocess, "run", forbidden)
    main(["dev", "sync", "--project", str(checkout), "--dry-run", "matrix"])
    assert "--no-deps" in capsys.readouterr().out
    assert not (checkout / ".venv").exists()
    assert not (checkout / ".medre-dev.yaml").exists()


def test_sync_stops_after_failure_without_running_later_commands(checkout, monkeypatch):
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(returncode=7)

    monkeypatch.setattr(subprocess, "run", run)
    with pytest.raises(SystemExit) as exc:
        main(["dev", "sync", "--project", str(checkout), "--sdk"])
    assert exc.value.code == 7
    assert len(calls) == 1
    assert calls[0][1]["cwd"] == checkout


def test_adapter_tests_use_adapter_cwd_and_checkout_interpreter(
    checkout, tmp_path, monkeypatch
):
    adapter = adapter_checkout(tmp_path)
    env = DevEnvironment.inspect(arguments(checkout, editable=[f"matrix={adapter}"]))
    env.python.parent.mkdir(parents=True)
    env.python.touch()
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(subprocess, "run", run)
    main(
        [
            "dev",
            "test",
            "--project",
            str(checkout),
            "--editable",
            f"matrix={adapter}",
            "matrix",
            "--",
            "-m",
            "matrix_sdk",
            "-q",
        ]
    )
    command = calls[0][0]
    assert command[:3] == [str(env.python), "-m", "medre.dev_environment"]
    assert command[3:] == [
        "medre-adapter-matrix",
        adapter.as_uri(),
        checkout.as_uri(),
        "-m",
        "matrix_sdk",
        "-q",
    ]
    assert calls[0][1]["cwd"] == adapter


def test_adapter_tests_require_source_checkout(checkout):
    env = DevEnvironment.inspect(arguments(checkout))
    with pytest.raises(ToolManagementError, match="editable checkout"):
        env.test_command("matrix", [])


def test_editable_install_retains_catalog_and_selection_extras(checkout, tmp_path):
    adapter = adapter_checkout(tmp_path)
    catalog = tmp_path / "sources.yaml"
    catalog.write_text(
        "adapters:\n  matrix: medre-adapter-matrix[e2e] @ git+https://example.org/matrix.git@main\n"
    )
    env = DevEnvironment.inspect(
        arguments(
            checkout,
            adapters=["matrix[test]"],
            sdk=True,
            sources=str(catalog),
            editable=[f"matrix={adapter}"],
        )
    )
    assert adapter.as_uri() + "[e2e,test]" in env.sync_commands()[-1]


def test_test_bootstrap_refuses_mismatched_installed_provider(tmp_path):
    import sys

    # A real child interpreter proves pytest cannot run for a missing provider.
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "medre.dev_environment",
            "medre-provider-that-is-not-installed",
            tmp_path.as_uri(),
            Path(__file__).resolve().parents[1].as_uri(),
            "--version",
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 2
    assert "does not match" in result.stderr
    assert "pytest" not in result.stdout


def test_checkout_refuses_parent_uv_workspace(checkout):
    (checkout.parent / "pyproject.toml").write_text(
        '[tool.uv.workspace]\nmembers = ["core"]\n'
    )
    with pytest.raises(ToolManagementError, match="parent uv workspace"):
        checkout_root(str(checkout))


def test_profile_schema_matches_documented_preferences(checkout):
    from pathlib import Path

    import jsonschema

    schema_path = (
        Path(__file__).resolve().parents[1] / "docs/schemas/dev-environment.schema.json"
    )
    schema = json.loads(schema_path.read_text())
    jsonschema.Draft202012Validator.check_schema(schema)
    jsonschema.validate(
        {
            "adapters": ["matrix[e2e]"],
            "sdk": True,
            "editable": {"matrix": "../providers/matrix"},
            "sources": "./sources.yaml",
        },
        schema,
    )
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate({"sdk": "true"}, schema)


def test_editable_profile_keys_do_not_silently_discard_extras(checkout, tmp_path):
    adapter = adapter_checkout(tmp_path)
    (checkout / ".medre-dev.yaml").write_text(
        json.dumps({"editable": {"matrix[e2e]": str(adapter)}})
    )
    with pytest.raises(ToolManagementError, match="without extras"):
        DevEnvironment.inspect(arguments(checkout))


def test_adapter_suite_refuses_a_different_installed_core(
    checkout, monkeypatch, capsys
):
    import importlib.metadata

    from medre.dev_environment import _test_installed_adapter

    distribution = SimpleNamespace(
        metadata={"Name": "medre"},
        read_text=lambda _: json.dumps(
            {"url": "file:///another/core", "dir_info": {"editable": True}}
        ),
    )
    monkeypatch.setattr(
        importlib.metadata, "distributions", lambda **kwargs: [distribution]
    )
    with pytest.raises(SystemExit) as exc:
        _test_installed_adapter(
            "medre-adapter-matrix", "file:///provider", checkout.as_uri(), ["-q"]
        )
    assert exc.value.code == 2
    assert "Core checkout does not match" in capsys.readouterr().err


def test_explicit_adapter_test_target_overrides_profile_sync_selection(
    checkout, tmp_path
):
    adapter = adapter_checkout(tmp_path, "meshcore")
    (checkout / ".medre-dev.yaml").write_text("adapters: [matrix]\n")
    env = DevEnvironment.inspect(
        arguments(checkout, adapter="meshcore", editable=[f"meshcore={adapter}"])
    )
    assert env.adapters == ["meshcore"]
    assert env.editable["meshcore"] == adapter


@pytest.mark.parametrize("document", ["", "[]", "plain", "null", "42"])
def test_nonmapping_profiles_report_clean_cli_errors(checkout, capsys, document):
    (checkout / ".medre-dev.yaml").write_text(document)
    with pytest.raises(SystemExit) as exc:
        main(["dev", "sync", "--project", str(checkout), "--dry-run"])
    assert exc.value.code == 2
    error = capsys.readouterr().err
    assert "Development error:" in error
    assert "Traceback" not in error


def test_sync_reports_missing_uv_without_needing_it_installed(checkout, monkeypatch):
    monkeypatch.setattr("medre.dev_environment.shutil.which", lambda _: None)
    with pytest.raises(ToolManagementError, match="Install uv"):
        DevEnvironment.inspect(arguments(checkout)).sync_commands()


def test_windows_pytest_runner_waits_and_propagates_failure(checkout, monkeypatch):
    import importlib.metadata

    import medre.dev_environment as dev

    distributions = [
        SimpleNamespace(
            metadata={"Name": name},
            read_text=lambda _, path=path: json.dumps(
                {"url": path, "dir_info": {"editable": True}}
            ),
        )
        for name, path in (
            ("medre", checkout.as_uri()),
            ("medre-adapter-matrix", "file:///provider"),
        )
    ]
    monkeypatch.setattr(
        importlib.metadata, "distributions", lambda **kwargs: distributions
    )
    monkeypatch.setattr(dev.sys, "platform", "win32")
    calls = []

    def waited(command, **kwargs):
        calls.append(command)
        return SimpleNamespace(returncode=7)

    monkeypatch.setattr(subprocess, "run", waited)
    with pytest.raises(SystemExit) as exc:
        dev._test_installed_adapter(
            "medre-adapter-matrix", "file:///provider", checkout.as_uri(), ["-q"]
        )
    assert exc.value.code == 7
    assert calls == [[dev.sys.executable, "-m", "pytest", "-q"]]


def test_pytest_launcher_returns_the_test_process_failure(tmp_path):
    import sys

    # Exercise the real child process on every CI platform. Matching metadata
    # is isolated from this regression so only process/exit semantics decide it.
    suite = tmp_path / "test suite"
    suite.mkdir()
    (suite / "test_failed.py").write_text(
        "def test_failure():\n    assert False, 'launcher status proof'\n"
    )
    bootstrap = """import importlib.metadata
import json
from types import SimpleNamespace
from medre.dev_environment import _test_installed_adapter
records = [SimpleNamespace(metadata={"Name": name}, read_text=lambda _, url=url: json.dumps({"url": url, "dir_info": {"editable": True}})) for name, url in (("medre", "file:///core"), ("medre-adapter-matrix", "file:///provider"))]
importlib.metadata.distributions = lambda **kwargs: records
_test_installed_adapter("medre-adapter-matrix", "file:///provider", "file:///core", ["-q", "test suite/test_failed.py"])
"""
    result = subprocess.run(
        [sys.executable, "-c", bootstrap],
        cwd=tmp_path,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 1
    assert "launcher status proof" in result.stdout
    assert "1 failed" in result.stdout


def test_adapter_evidence_records_the_profile_checkout_not_the_core(
    checkout, tmp_path, monkeypatch
):
    adapter = adapter_checkout(tmp_path)
    env = DevEnvironment.inspect(arguments(checkout, editable=[f"matrix={adapter}"]))
    calls = []

    def git(command, **kwargs):
        calls.append(command)
        return SimpleNamespace(
            returncode=0,
            stdout=("recorded " + " ".join(command[3:]) + "\n").encode("utf-8")
            + (b"\xff\n" if command[3] == "diff" else b""),
        )

    monkeypatch.setattr(subprocess, "run", git)
    evidence = tmp_path / "evidence"
    env.record_adapter_evidence("matrix", evidence)
    assert all(command[:3] == ["git", "-C", str(adapter)] for command in calls)
    assert (evidence / "adapter-checkout.txt").read_text() == str(adapter) + "\n"
    assert (evidence / "adapter-commit.txt").read_text() == "recorded rev-parse HEAD\n"
    assert (
        evidence / "adapter-working-tree.patch"
    ).read_bytes() == b"recorded diff --binary HEAD\n\xff\n"
    assert (
        evidence / "adapter-working-tree-status.txt"
    ).read_text() == "recorded status --short\n"
    with pytest.raises(FileExistsError):
        env.record_adapter_evidence("matrix", evidence)


def test_failed_git_evidence_does_not_start_adapter_tests(
    checkout, tmp_path, monkeypatch, capsys
):
    adapter = adapter_checkout(tmp_path)
    env = DevEnvironment.inspect(arguments(checkout, editable=[f"matrix={adapter}"]))
    env.python.parent.mkdir(parents=True)
    env.python.touch()
    calls = []

    def failed_git(command, **kwargs):
        calls.append(command)
        return SimpleNamespace(returncode=1, stdout="", stderr="not a git repository")

    monkeypatch.setattr(subprocess, "run", failed_git)
    evidence = tmp_path / "evidence"
    with pytest.raises(SystemExit) as exc:
        main(
            [
                "dev",
                "test",
                "--project",
                str(checkout),
                "--editable",
                f"matrix={adapter}",
                "--evidence-dir",
                str(evidence),
                "matrix",
                "--",
                "-q",
            ]
        )
    assert exc.value.code == 2
    assert len(calls) == 1 and calls[0][0] == "git"
    assert not evidence.exists()
    assert "Cannot record adapter Git state" in capsys.readouterr().err


def test_checkout_discovery_walks_up_from_a_nested_cwd(checkout, tmp_path, monkeypatch):
    """Discovery without --project climbs to the checkout past non-project folders."""
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "wrapper"\n')
    nested = checkout / "src" / "medre"
    nested.mkdir(parents=True)
    monkeypatch.chdir(nested)
    assert DevEnvironment.inspect(arguments(checkout, project=None)).root == (
        checkout.resolve()
    )


def test_checkout_discovery_reports_a_missing_checkout(checkout, tmp_path, monkeypatch):
    """A directory chain with no MEDRE metadata exhausts discovery."""
    bare = tmp_path / "plain" / "nested"
    bare.mkdir(parents=True)
    monkeypatch.chdir(bare)
    with pytest.raises(ToolManagementError, match="Run from a MEDRE checkout"):
        checkout_root()


@pytest.mark.parametrize(
    "entry",
    [
        {
            "transport": "1bad",
            "distribution": "medre-adapter-x",
            "repository": "o/x",
            "ref": "a" * 40,
        },
        {
            "transport": "matrix",
            "distribution": "medre-adapter-x",
            "repository": "o/x",
            "ref": "short",
        },
    ],
)
def test_snapshot_rejects_malformed_entries(checkout, entry):
    """Snapshot entries need package-style aliases and full commit SHAs."""
    (checkout / ".github/adapter-compatibility.json").write_text(
        json.dumps([entry, entry])
    )
    with pytest.raises(
        ToolManagementError, match="unique aliases and full commit SHAs"
    ):
        DevEnvironment.inspect(arguments(checkout))


def test_snapshot_rejects_duplicate_aliases(checkout):
    """The same alias twice in a snapshot is a corruption, not a choice."""
    entry = {
        "transport": "matrix",
        "distribution": "medre-adapter-matrix",
        "repository": "owner/matrix",
        "ref": "a" * 40,
    }
    (checkout / ".github/adapter-compatibility.json").write_text(
        json.dumps([entry, entry])
    )
    with pytest.raises(
        ToolManagementError, match="unique aliases and full commit SHAs"
    ):
        DevEnvironment.inspect(arguments(checkout))


def test_snapshot_rejects_an_empty_compatibility_snapshot(checkout):
    (checkout / ".github/adapter-compatibility.json").write_text("[]")
    with pytest.raises(ToolManagementError, match="must contain adapters"):
        DevEnvironment.inspect(arguments(checkout))


def test_profile_sources_must_name_a_catalog_file(checkout):
    (checkout / ".medre-dev.yaml").write_text('sources: ""\n')
    with pytest.raises(ToolManagementError, match="must name a source catalog file"):
        DevEnvironment.inspect(arguments(checkout))


def test_editable_overrides_must_use_alias_equals_path(checkout):
    with pytest.raises(ToolManagementError, match="once per alias"):
        DevEnvironment.inspect(arguments(checkout, editable=["matrix"]))


def test_editable_overrides_must_be_selected(checkout, tmp_path):
    adapter = adapter_checkout(tmp_path, "meshcore")
    with pytest.raises(ToolManagementError, match="must be selected: meshcore"):
        DevEnvironment.inspect(
            arguments(checkout, editable=[f"meshcore={adapter}"], adapters=["matrix"])
        )


def test_adapter_test_command_requires_a_synced_environment(checkout, tmp_path):
    adapter = adapter_checkout(tmp_path)
    env = DevEnvironment.inspect(arguments(checkout, editable=[f"matrix={adapter}"]))
    with pytest.raises(ToolManagementError, match="run medre dev sync first"):
        env.test_command("matrix", ["-q"])


def test_installed_metadata_scan_skips_foreign_and_malformed_distributions(
    checkout, monkeypatch, posix_launcher
):
    """The launcher bootstrap ignores distributions it cannot identify."""
    import importlib.metadata

    import medre.dev_environment as dev

    def record(name, url):
        return SimpleNamespace(
            metadata={"Name": name},
            read_text=lambda _, url=url: json.dumps(
                {"url": url, "dir_info": {"editable": True}}
            ),
        )

    distributions = [
        SimpleNamespace(metadata={}),
        SimpleNamespace(metadata={"Name": "!unparseable"}),
        record("other-adapter", "file:///elsewhere"),
        record("medre", checkout.as_uri()),
        record("medre-adapter-matrix", "file:///provider"),
    ]
    monkeypatch.setattr(
        importlib.metadata, "distributions", lambda **kwargs: distributions
    )
    calls = []

    def replaced(*command):
        calls.append(command)
        raise RuntimeError("replaced execv")

    monkeypatch.setattr("medre.dev_environment.os.execv", replaced)
    with pytest.raises(RuntimeError, match="replaced execv"):
        dev._test_installed_adapter(
            "medre-adapter-matrix", "file:///provider", checkout.as_uri(), ["-q"]
        )
    assert calls == [(dev.sys.executable, [dev.sys.executable, "-m", "pytest", "-q"])]


def test_unreadable_direct_url_fails_closed(checkout, monkeypatch, capsys):
    import importlib.metadata

    import medre.dev_environment as dev

    def record(name, direct_url_body):
        return SimpleNamespace(
            metadata={"Name": name},
            read_text=lambda _, body=direct_url_body: body,
        )

    distributions = [
        record(
            "medre",
            json.dumps({"url": checkout.as_uri(), "dir_info": {"editable": True}}),
        ),
        record("medre-adapter-matrix", "not json"),
    ]
    monkeypatch.setattr(
        importlib.metadata, "distributions", lambda **kwargs: distributions
    )
    with pytest.raises(SystemExit) as exc:
        dev._test_installed_adapter(
            "medre-adapter-matrix", "file:///provider", checkout.as_uri(), ["-q"]
        )
    assert exc.value.code == 2
    assert "does not match" in capsys.readouterr().err


def test_dev_test_strips_a_leading_separator_argument(
    checkout, tmp_path, capsys, posix_launcher
):
    adapter = adapter_checkout(tmp_path)
    python = checkout / ".venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.touch()
    main(
        [
            "dev",
            "test",
            "--project",
            str(checkout),
            "--editable",
            f"matrix={adapter}",
            "--dry-run",
            "matrix",
            "--",
            "--",
            "-q",
        ]
    )
    out = capsys.readouterr().out
    assert out.rstrip().endswith("-q")
    assert "-- -q" not in out


def test_windows_sync_refuses_to_replace_the_running_tool(
    checkout, monkeypatch, capsys
):
    import medre.cli.dev_commands as dev_cli

    monkeypatch.setattr(dev_cli.sys, "platform", "win32")
    monkeypatch.setattr(dev_cli.sys, "prefix", str(checkout / ".venv"))
    with pytest.raises(SystemExit) as exc:
        main(["dev", "sync", "--project", str(checkout)])
    assert exc.value.code == 2
    assert "after medre.exe exits" in capsys.readouterr().err


def test_failed_adapter_tests_exit_with_the_test_status(
    checkout, tmp_path, monkeypatch, capsys, posix_launcher
):
    import medre.cli.dev_commands as dev_cli

    adapter = adapter_checkout(tmp_path)
    python = checkout / ".venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.touch()
    monkeypatch.setattr(
        dev_cli.subprocess,
        "run",
        lambda command, **kwargs: SimpleNamespace(returncode=3),
    )
    with pytest.raises(SystemExit) as exc:
        main(
            [
                "dev",
                "test",
                "--project",
                str(checkout),
                "--editable",
                f"matrix={adapter}",
                "matrix",
                "--",
                "-q",
            ]
        )
    assert exc.value.code == 3
    assert "Synchronization may be incomplete" not in capsys.readouterr().err


def test_successful_sync_reports_completion(checkout, monkeypatch, capsys):
    import medre.cli.dev_commands as dev_cli

    runs = []

    def ok(command, **kwargs):
        runs.append(command)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(dev_cli.subprocess, "run", ok)
    main(["dev", "sync", "--project", str(checkout)])
    assert "Checkout ready" in capsys.readouterr().out
    assert len(runs) == 2
    assert runs[0][1] == "sync"
    assert "--no-deps" in runs[1]


def test_development_errors_report_a_clean_cli_failure(checkout, capsys):
    (checkout / ".medre-dev.yaml").mkdir()
    with pytest.raises(SystemExit) as exc:
        main(["dev", "sync", "--project", str(checkout)])
    assert exc.value.code == 2
    assert "Development error:" in capsys.readouterr().err


def test_interrupted_development_command_reports_rerun(checkout, monkeypatch, capsys):
    import medre.cli.dev_commands as dev_cli

    def interrupted(args):
        raise KeyboardInterrupt

    monkeypatch.setattr(dev_cli, "DevEnvironment", SimpleNamespace(inspect=interrupted))
    with pytest.raises(SystemExit) as exc:
        main(["dev", "sync", "--project", str(checkout)])
    assert exc.value.code == 130
    assert "interrupted" in capsys.readouterr().err
