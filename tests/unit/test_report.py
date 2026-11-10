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


def test_run_and_parse_attributes_pass_and_fail_to_current_test(monkeypatch, capsys):
    lines = [
        "[CFTEST-START] file.cf::bundle_a\n",
        "    info: [ASSERT] PASS a check\n",
        "   error: [ASSERT] FAIL another check # it broke\n",
        "[CFTEST-DONE] file.cf::bundle_a\n",
    ]
    monkeypatch.setattr(report.subprocess, "Popen", lambda *a, **k: _FakeProc(lines))

    results = report.run_and_parse([], "script", ["file.cf::bundle_a"], image="img")

    assert results.failures == [("file.cf::bundle_a", "another check", "it broke")]
    assert results.completed == {"file.cf::bundle_a"}
    assert results.marks_by_test["file.cf::bundle_a"] == [".", "F"]
    # The FAIL is printed as soon as it's parsed, not only in the recap
    out = capsys.readouterr().out
    assert "FAIL   file.cf::bundle_a::another check  ->  it broke" in out


def test_run_and_parse_records_invalid_assert_as_fail(monkeypatch):
    lines = [
        "[CFTEST-START] file.cf::bundle_a\n",
        "error: wrong type for 'int' for assert promise with promiser 'oops'\n",
        "[CFTEST-DONE] file.cf::bundle_a\n",
    ]
    monkeypatch.setattr(report.subprocess, "Popen", lambda *a, **k: _FakeProc(lines))

    results = report.run_and_parse([], "script", ["file.cf::bundle_a"], image="img")

    [(_test, name, reason)] = results.failures
    assert name == "oops"
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


def test_run_and_scan_asserts_notices_failsafe_fallback(monkeypatch):
    lines = [
        "   error: CFEngine was not able to get confirmation of promises "
        "from cf-promises, so going to failsafe\n",
    ]
    monkeypatch.setattr(report.subprocess, "Popen", lambda *a, **k: _FakeProc(lines))

    results = report.run_and_scan_asserts(["docker", "run"])

    assert results.fell_back_to_failsafe
    assert results.returncode == 0


def test_print_assert_totals_reports_zero_failures_as_success(capsys):
    rc = report.print_assert_totals(passed=3, failed=0)
    out = capsys.readouterr().out
    assert rc == 0
    assert "3 passed" in out
    assert "0 failed" in out


def test_print_assert_totals_reports_failures_as_failure(capsys):
    rc = report.print_assert_totals(passed=1, failed=2)
    assert rc == 1


def test_print_assert_totals_reports_errors_as_failure(capsys):
    rc = report.print_assert_totals(passed=1, failed=0, errors=1)
    out = capsys.readouterr().out
    assert rc == 1
    assert "1 errors" in out


def test_run_and_parse_keeps_every_result_for_a_repeated_promiser(monkeypatch):
    lines = [
        "[CFTEST-START] file.cf::bundle_a\n",
        "   error: [ASSERT] FAIL check # first one broke\n",
        "    info: [ASSERT] PASS check\n",
        "[CFTEST-DONE] file.cf::bundle_a\n",
    ]
    monkeypatch.setattr(report.subprocess, "Popen", lambda *a, **k: _FakeProc(lines))

    results = report.run_and_parse([], "script", ["file.cf::bundle_a"], image="img")

    assert results.marks_by_test["file.cf::bundle_a"] == ["F", "."]
    assert len(results.failures) == 1
    assert report._outcome_of("file.cf::bundle_a", results) == "FAIL"


def test_print_report_counts_every_result_for_a_repeated_promiser(capsys):
    results = RunResults(
        completed={"t"},
        marks_by_test={"t": ["F", "."]},
        failures=[("t", "check", "broke")],
    )
    rc = report.print_report(["t"], results)
    out = capsys.readouterr().out
    assert rc == 1
    assert "1 passed, 1 failed, 0 errors" in out
    assert "FAIL   t::check  ->  broke" in out


def test_assert_dots_marks_incomplete_test_with_trailing_e():
    results = RunResults(completed=set(), marks_by_test={"t": [".", "."]}, failures=[])
    assert report._assert_dots("t", results) == "..E"


def test_outcome_of_is_error_when_test_never_completed():
    results = RunResults(completed=set(), marks_by_test={"t": []}, failures=[])
    assert report._outcome_of("t", results) == "ERROR"


def test_print_report_returns_zero_when_everything_passes(capsys):
    results = RunResults(
        completed={"t"},
        marks_by_test={"t": ["."]},
        failures=[],
    )
    rc = report.print_report(["t"], results)
    assert rc == 0


def test_print_report_prints_error_line_for_incomplete_test(capsys):
    results = RunResults(completed=set(), marks_by_test={"t": []}, failures=[])
    rc = report.print_report(["t"], results)
    out = capsys.readouterr().out
    assert rc == 1
    assert "ERROR" in out
    assert "test did not run to completion" in out


def test_run_and_parse_ignores_asserts_after_done_marker(monkeypatch):
    lines = [
        "[CFTEST-START] file.cf::bundle_a\n",
        "    info: [ASSERT] PASS a check\n",
        "[CFTEST-DONE] file.cf::bundle_a\n",
        "   error: [ASSERT] FAIL not part of any test # printed after DONE\n",
    ]
    monkeypatch.setattr(report.subprocess, "Popen", lambda *a, **k: _FakeProc(lines))

    results = report.run_and_parse([], "script", ["file.cf::bundle_a"], image="img")

    assert results.marks_by_test["file.cf::bundle_a"] == ["."]
    assert results.failures == []
    assert report._outcome_of("file.cf::bundle_a", results) == "PASS"
