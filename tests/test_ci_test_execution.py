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


@pytest.mark.parametrize(
    "cases",
    [
        "",
        '<testcase name="one"><skipped/></testcase>'
        '<testcase name="two"><skipped/></testcase>',
        '<testcase name="one"><failure/></testcase><testcase name="two"/>',
        '<testcase name="one"><error/></testcase><testcase name="two"/>',
        '<testcase name="same"/><testcase name="same"/>',
    ],
)
def test_summary_totals_cannot_hide_missing_or_failed_records(tmp_path, cases):
    report = tmp_path / "junit.xml"
    report.write_text(
        '<testsuites><testsuite tests="2" skipped="0" errors="0" failures="0">'
        + cases
        + "</testsuite></testsuites>"
    )
    assert _script().check_report(report, 2)


@pytest.mark.parametrize("count", ["invalid", "-2"])
def test_malformed_counters_report_a_reason_instead_of_crashing(tmp_path, count):
    report = tmp_path / "junit.xml"
    report.write_text(
        f'<testsuite tests="{count}" skipped="0" errors="0" failures="0">'
        '<testcase name="one"/><testcase name="two"/></testsuite>'
    )
    problems = _script().check_report(report, 2)
    assert any("invalid tests total" in problem for problem in problems)


def test_one_present_artifact_cannot_mask_another_missing_verdict(tmp_path):
    report = tmp_path / "junit.xml"
    report.write_text(
        '<testsuite tests="2" skipped="0" errors="0" failures="0">'
        '<testcase name="one"/><testcase name="two"/></testsuite>'
    )
    ready = tmp_path / "positive" / "b-ready.json"
    ready.parent.mkdir()
    ready.write_text("{}")
    patterns = (
        str(tmp_path / "positive*" / "b-ready.json"),
        str(tmp_path / "negative*" / "b-received.json"),
    )
    problems = _script().check_report(report, 2, required_artifacts=patterns)
    assert len(problems) == 1
    assert "required qualification artifact is missing" in problems[0]
    received = tmp_path / "negative" / "b-received.json"
    received.parent.mkdir()
    received.write_text("{}")
    assert _script().check_report(report, 2, required_artifacts=patterns) == []


def test_required_lxmf_job_checks_each_scenario_verdict():
    import shlex
    import yaml

    workflow = (
        Path(__file__).resolve().parents[1] / ".github/workflows/test-and-coverage.yml"
    )
    job = yaml.safe_load(workflow.read_text())["jobs"]["core-owned-lxmf-integration"]
    enforcement = next(
        step["run"]
        for step in job["steps"]
        if step.get("name") == "Enforce executed-at-pin qualification"
    )
    arguments = shlex.split(enforcement.replace(chr(92) + chr(10), " "))
    patterns = [
        arguments[index + 1]
        for index, argument in enumerate(arguments)
        if argument == "--require-artifact"
    ]
    assert len(patterns) == 5
    for scenario in ("test_propagated_delivery*", "test_unobserved_exact_dest*"):
        for verdict in ("b-ready.json", "b-received.json"):
            assert any(f"/{scenario}/{verdict}" in pattern for pattern in patterns)
