from cfengine_cli import report
from cfengine_cli.report import RunResults


class _FakeProc:
    """Stands in for subprocess.Popen: stdout is pre-canned lines, no real
    process behind it."""

    def __init__(self, lines, returncode=0):
        self.stdout = iter(lines)
        self.returncode = returncode

    def wait(self):
        return self.returncode


def test_run_and_parse_attributes_pass_and_fail_to_current_test(monkeypatch):
    lines = [
        "[CFTEST-START] file.cf::bundle_a\n",
        "    info: [ASSERT] PASS a check\n",
        "   error: [ASSERT] FAIL another check # it broke\n",
        "[CFTEST-DONE] file.cf::bundle_a\n",
    ]
    monkeypatch.setattr(report.subprocess, "Popen", lambda *a, **k: _FakeProc(lines))

    results = report.run_and_parse([], "script", ["file.cf::bundle_a"], image="img")

    assert results.verdicts[("file.cf::bundle_a", "a check")] == ("PASS", "")
    assert results.verdicts[("file.cf::bundle_a", "another check")] == (
        "FAIL",
        "it broke",
    )
    assert results.completed == {"file.cf::bundle_a"}
    assert results.marks_by_test["file.cf::bundle_a"] == [".", "F"]


def test_run_and_parse_records_invalid_assert_as_fail(monkeypatch):
    lines = [
        "[CFTEST-START] file.cf::bundle_a\n",
        "error: wrong type for 'int' for assert promise with promiser 'oops'\n",
        "[CFTEST-DONE] file.cf::bundle_a\n",
    ]
    monkeypatch.setattr(report.subprocess, "Popen", lambda *a, **k: _FakeProc(lines))

    results = report.run_and_parse([], "script", ["file.cf::bundle_a"], image="img")

    outcome, reason = results.verdicts[("file.cf::bundle_a", "oops")]
    assert outcome == "FAIL"
    assert "invalid assert: wrong type for 'int'" == reason
    assert results.marks_by_test["file.cf::bundle_a"] == ["F"]


def test_run_and_scan_asserts_counts_pass_fail_and_invalid(monkeypatch):
    lines = [
        "    info: [ASSERT] PASS one\n",
        "   error: [ASSERT] FAIL two # bad\n",
        "error: wrong type for 'int' for assert promise with promiser 'three'\n",
    ]
    monkeypatch.setattr(
        report.subprocess, "Popen", lambda *a, **k: _FakeProc(lines, returncode=1)
    )

    results = report.run_and_scan_asserts(["docker", "run"])

    assert results.passed == 1
    assert results.failed == 2
    assert results.returncode == 1


def test_print_assert_totals_reports_zero_failures_as_success(capsys):
    rc = report.print_assert_totals(passed=3, failed=0)
    out = capsys.readouterr().out
    assert rc == 0
    assert "3 passed" in out
    assert "0 failed" in out


def test_print_assert_totals_reports_failures_as_failure(capsys):
    rc = report.print_assert_totals(passed=1, failed=2)
    assert rc == 1


def test_assert_dots_marks_incomplete_test_with_trailing_e():
    results = RunResults(verdicts={}, completed=set(), marks_by_test={"t": [".", "."]})
    assert report._assert_dots("t", results) == "..E"


def test_outcome_of_is_error_when_test_never_completed():
    results = RunResults(verdicts={}, completed=set(), marks_by_test={"t": []})
    assert report._outcome_of("t", results) == "ERROR"


def test_print_report_returns_zero_when_everything_passes(capsys):
    results = RunResults(
        verdicts={("t", "a check"): ("PASS", "")},
        completed={"t"},
        marks_by_test={"t": ["."]},
    )
    rc = report.print_report(["t"], results)
    assert rc == 0


def test_print_report_prints_error_line_for_incomplete_test(capsys):
    results = RunResults(verdicts={}, completed=set(), marks_by_test={"t": []})
    rc = report.print_report(["t"], results)
    out = capsys.readouterr().out
    assert rc == 1
    assert "ERROR" in out
    assert "test did not run to completion" in out
