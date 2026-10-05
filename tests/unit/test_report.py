import cfengine_cli.report as report
from cfengine_cli.report import RunResults

# ---------------------------------------------------------------------------
# run_and_parse
# ---------------------------------------------------------------------------


class _FakeProc:
    def __init__(self, lines, returncode=0):
        self.stdout = iter(lines)
        self.returncode = returncode

    def wait(self):
        return self.returncode


def test_run_and_parse_tracks_pass_fail_and_completion(monkeypatch):
    lines = [
        "R: [CFTEST-START] test_a\n",
        "    info: [ASSERT] PASS one\n",
        "   error: [ASSERT] FAIL two # expected 1, got 2\n",
        "R: [CFTEST-DONE] test_a\n",
    ]
    monkeypatch.setattr(report.subprocess, "Popen", lambda *a, **kw: _FakeProc(lines))

    results = report.run_and_parse(
        [], "irrelevant script", ["test_a"], image="irrelevant-image"
    )

    assert results.verdicts[("test_a", "one")] == ("PASS", "")
    assert results.verdicts[("test_a", "two")] == ("FAIL", "expected 1, got 2")
    assert results.completed == {"test_a"}
    assert results.marks_by_test == {"test_a": [".", "F"]}


def test_run_and_parse_marks_invalid_assert_as_fail(monkeypatch):
    lines = [
        "R: [CFTEST-START] test_a\n",
        "error: 'foo' doesn't take ['bar'] for assert promise with promiser 'broken'\n",
        "R: [CFTEST-DONE] test_a\n",
    ]
    monkeypatch.setattr(report.subprocess, "Popen", lambda *a, **kw: _FakeProc(lines))

    results = report.run_and_parse(
        [], "irrelevant script", ["test_a"], image="irrelevant-image"
    )

    outcome, reason = results.verdicts[("test_a", "broken")]
    assert outcome == "FAIL"
    assert "invalid assert" in reason


def test_run_and_parse_test_that_never_completes(monkeypatch):
    lines = [
        "R: [CFTEST-START] test_a\n",
        "    info: [ASSERT] PASS one\n",
        "error: some fatal cf-agent error, run aborted\n",
    ]
    monkeypatch.setattr(report.subprocess, "Popen", lambda *a, **kw: _FakeProc(lines))

    results = report.run_and_parse(
        [], "irrelevant script", ["test_a"], image="irrelevant-image"
    )

    assert results.completed == set()


# ---------------------------------------------------------------------------
# run_and_scan_asserts / print_assert_totals
# ---------------------------------------------------------------------------


def test_run_and_scan_asserts_counts_pass_and_fail(monkeypatch):
    lines = [
        "    info: [ASSERT] PASS one\n",
        "   error: [ASSERT] FAIL two # expected 1, got 2\n",
    ]
    monkeypatch.setattr(
        report.subprocess, "Popen", lambda *a, **kw: _FakeProc(lines, returncode=1)
    )

    results = report.run_and_scan_asserts(["irrelevant", "cmd"])

    assert results == report.DeployResults(returncode=1, passed=1, failed=1)


def test_run_and_scan_asserts_counts_invalid_assert_as_fail(monkeypatch):
    lines = [
        "error: 'foo' doesn't take ['bar'] for assert promise with promiser 'broken'\n"
    ]
    monkeypatch.setattr(report.subprocess, "Popen", lambda *a, **kw: _FakeProc(lines))

    results = report.run_and_scan_asserts(["irrelevant", "cmd"])

    assert results.passed == 0
    assert results.failed == 1


def test_run_and_scan_asserts_no_asserts_keeps_agent_returncode(monkeypatch):
    lines = ["R: ordinary policy output, no asserts here\n"]
    monkeypatch.setattr(
        report.subprocess, "Popen", lambda *a, **kw: _FakeProc(lines, returncode=0)
    )

    results = report.run_and_scan_asserts(["irrelevant", "cmd"])

    assert results == report.DeployResults(returncode=0, passed=0, failed=0)


def test_print_assert_totals_all_passed_returns_zero(capsys):
    rc = report.print_assert_totals(passed=3, failed=0)
    out = capsys.readouterr().out
    assert rc == 0
    assert "3 passed, 0 failed, 0 errors" in out


def test_print_assert_totals_any_failed_returns_one(capsys):
    rc = report.print_assert_totals(passed=2, failed=1)
    out = capsys.readouterr().out
    assert rc == 1
    assert "2 passed, 1 failed, 0 errors" in out


# ---------------------------------------------------------------------------
# _assert_dots / _outcome_of / print_report
# ---------------------------------------------------------------------------


def test_assert_dots_completed_test():
    results = RunResults(
        verdicts={}, completed={"test_a"}, marks_by_test={"test_a": [".", "F"]}
    )
    assert report._assert_dots("test_a", results) == ".F"


def test_assert_dots_incomplete_test_gets_trailing_e():
    results = RunResults(verdicts={}, completed=set(), marks_by_test={"test_a": ["."]})
    assert report._assert_dots("test_a", results) == ".E"


def test_outcome_of_error_when_not_completed():
    results = RunResults(verdicts={}, completed=set(), marks_by_test={"test_a": []})
    assert report._outcome_of("test_a", results) == "ERROR"


def test_outcome_of_fail_when_any_assertion_failed():
    results = RunResults(
        verdicts={("test_a", "x"): ("PASS", ""), ("test_a", "y"): ("FAIL", "boom")},
        completed={"test_a"},
        marks_by_test={"test_a": [".", "F"]},
    )
    assert report._outcome_of("test_a", results) == "FAIL"


def test_outcome_of_pass_when_all_assertions_passed():
    results = RunResults(
        verdicts={("test_a", "x"): ("PASS", "")},
        completed={"test_a"},
        marks_by_test={"test_a": ["."]},
    )
    assert report._outcome_of("test_a", results) == "PASS"


def test_print_report_all_passed_returns_zero(capsys):
    results = RunResults(
        verdicts={("test_a", "x"): ("PASS", "")},
        completed={"test_a"},
        marks_by_test={"test_a": ["."]},
    )
    rc = report.print_report(["test_a"], results)
    out = capsys.readouterr().out
    assert rc == 0
    assert "test_a  ." in out
    assert "1 passed, 0 failed, 0 errors" in out


def test_print_report_counts_checks_not_files(capsys):
    """The summary counts individual assert: checks (pytest counts test
    functions, not files) -- a single file with 2 passing and 1 failing
    check must show "2 passed, 1 failed", not "0 passed, 1 failed" (which is
    what you'd get by counting the file's own overall PASS/FAIL outcome)."""
    results = RunResults(
        verdicts={
            ("test_a", "one"): ("PASS", ""),
            ("test_a", "two"): ("PASS", ""),
            ("test_a", "three"): ("FAIL", "boom"),
        },
        completed={"test_a"},
        marks_by_test={"test_a": [".", ".", "F"]},
    )
    rc = report.print_report(["test_a"], results)
    out = capsys.readouterr().out

    assert rc == 1
    assert "2 passed, 1 failed, 0 errors" in out


def test_print_report_prints_fail_and_error_details(capsys):
    results = RunResults(
        verdicts={("test_a", "x"): ("FAIL", "boom")},
        completed={"test_a"},
        marks_by_test={"test_a": ["F"], "test_b": []},
    )
    rc = report.print_report(["test_a", "test_b"], results)
    out = capsys.readouterr().out

    assert rc == 1
    assert "FAIL   test_a::x  ->  boom" in out
    assert "ERROR  test_b  ->" in out
