"""Required-check execution enforcement is proven by junit, never console text."""

import importlib.util
from pathlib import Path

import pytest


def _script():
    scripts = Path(__file__).resolve().parents[1] / "scripts/ci"
    spec = importlib.util.spec_from_file_location(
        "enforce_test_execution", scripts / "enforce_test_execution.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_passing_report_with_expected_count_enforces_nothing(tmp_path):
    module = _script()
    report = tmp_path / "junit.xml"
    report.write_text(
        '<testsuites><testsuite name="pytest" tests="2" errors="0" '
        'failures="0" skipped="0" time="1.0">'
        '<testcase classname="tests.integration" name="one"/>'
        '<testcase classname="tests.integration" name="two"/>'
        "</testsuite></testsuites>",
        encoding="utf-8",
    )
    assert module.check_report(report, 2) == []


def test_missing_report_is_a_collection_failure(tmp_path):
    module = _script()
    problems = module.check_report(tmp_path / "absent.xml", 2)
    assert len(problems) == 1
    assert "missing" in problems[0]


def test_skipped_tests_never_count_as_a_pass(tmp_path):
    module = _script()
    report = tmp_path / "junit.xml"
    report.write_text(
        '<testsuites><testsuite name="pytest" tests="2" errors="0" '
        'failures="0" skipped="2" time="1.0">'
        '<testcase classname="tests.integration" name="one">'
        '<skipped type="pytest.skip" message="requires the pinned SDKs">'
        "</skipped></testcase>"
        '<testcase classname="tests.integration" name="two">'
        '<skipped type="pytest.skip" message="requires the pinned SDKs">'
        "</skipped></testcase>"
        "</testsuite></testsuites>",
        encoding="utf-8",
    )
    problems = module.check_report(report, 2)
    assert any("skip" in problem for problem in problems)


def test_zero_recorded_tests_is_a_failure(tmp_path):
    module = _script()
    report = tmp_path / "junit.xml"
    report.write_text(
        '<testsuites><testsuite name="pytest" tests="0" errors="0" '
        'failures="0" skipped="0" time="0.0"></testsuite></testsuites>',
        encoding="utf-8",
    )
    problems = module.check_report(report, 2)
    assert any("did not execute" in problem for problem in problems)


def test_count_mismatch_is_a_failure(tmp_path):
    module = _script()
    report = tmp_path / "junit.xml"
    report.write_text(
        '<testsuites><testsuite name="pytest" tests="1" errors="0" '
        'failures="0" skipped="0" time="1.0">'
        '<testcase classname="tests.integration" name="one"/>'
        "</testsuite></testsuites>",
        encoding="utf-8",
    )
    problems = module.check_report(report, 2)
    assert any("expected 2" in problem for problem in problems)


def test_errors_and_failures_are_failures(tmp_path):
    module = _script()
    report = tmp_path / "junit.xml"
    report.write_text(
        '<testsuites><testsuite name="pytest" tests="2" errors="1" '
        'failures="1" skipped="0" time="1.0">'
        '<testcase classname="tests.integration" name="one">'
        "<error>collection error</error></testcase>"
        '<testcase classname="tests.integration" name="two">'
        "<failure>assertion</failure></testcase>"
        "</testsuite></testsuites>",
        encoding="utf-8",
    )
    problems = module.check_report(report, 2)
    assert any("error" in problem for problem in problems)
    assert any("failure" in problem for problem in problems)


def test_unparseable_report_is_a_failure(tmp_path):
    module = _script()
    report = tmp_path / "junit.xml"
    report.write_text("<testsuites><testsuite>", encoding="utf-8")
    problems = module.check_report(report, 2)
    assert len(problems) == 1
    assert "parseable" in problems[0]


def test_main_exits_nonzero_on_a_skip(tmp_path, monkeypatch, capsys):
    module = _script()
    report = tmp_path / "junit.xml"
    report.write_text(
        '<testsuites><testsuite name="pytest" tests="2" errors="0" '
        'failures="0" skipped="2" time="1.0"></testsuite></testsuites>',
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "sys.argv",
        ["enforce_test_execution", "--junit", str(report), "--expect-tests", "2"],
    )
    with pytest.raises(SystemExit) as excinfo:
        module.main()
    assert excinfo.value.code == 1
    assert "::error::" in capsys.readouterr().out


def test_main_exits_zero_when_execution_is_proven(tmp_path, monkeypatch, capsys):
    module = _script()
    report = tmp_path / "junit.xml"
    report.write_text(
        '<testsuites><testsuite name="pytest" tests="2" errors="0" '
        'failures="0" skipped="0" time="1.0">'
        '<testcase classname="tests.integration" name="one"/>'
        '<testcase classname="tests.integration" name="two"/>'
        "</testsuite></testsuites>",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "sys.argv",
        ["enforce_test_execution", "--junit", str(report), "--expect-tests", "2"],
    )
    module.main()
    assert capsys.readouterr().out.startswith("Execution enforced")


def test_lxmf_required_job_preserves_visible_qualification_artifacts():
    """A passing qualification must leave retrievable CI evidence."""
    import yaml

    workflow = (
        Path(__file__).resolve().parents[1] / ".github/workflows/test-and-coverage.yml"
    )
    definition = yaml.safe_load(workflow.read_text(encoding="utf-8"))
    job = definition["jobs"]["core-owned-lxmf-integration"]
    upload = next(
        step
        for step in job["steps"]
        if "actions/upload-artifact@" in step.get("uses", "")
    )
    paths = upload["with"]["path"].splitlines()
    assert paths
    assert all(path.lstrip().startswith("ci-artifacts/") for path in paths)
    assert all(
        not segment.startswith(".") for path in paths for segment in path.split("/")
    )
    assert upload["with"]["if-no-files-found"] == "error"
    assert any("junit.xml" in path for path in paths)
    assert any("b-received.json" in path for path in paths)
