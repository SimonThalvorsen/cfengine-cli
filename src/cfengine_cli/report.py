import re
import subprocess
import sys
from collections import Counter
from typing import NamedTuple

_ASSERT_RE = re.compile(r"\[ASSERT\] (PASS|FAIL) (.+?)(?: # (.*))?$")
_START_RE = re.compile(r"\[CFTEST-START\] (\S+)")
_DONE_RE = re.compile(r"\[CFTEST-DONE\] (\S+)")
# An assert: promise that fails validation is skipped by cf-agent and never
# prints a verdict, so it has to be caught from the error itself
_INVALID_ASSERT_RE = re.compile(
    r"error: (.*) for assert promise with promiser '([^']*)'"
)
# cf-agent prints this when cf-promises rejected the policy, and then runs
# its built-in failsafe policy instead -- still exiting 0
_FAILSAFE_RE = re.compile(r"going to failsafe")

_GREEN = "\033[32m"
_RED = "\033[31m"
_RESET = "\033[0m"


def _color(text: str, code: str) -> str:
    return f"{code}{text}{_RESET}" if sys.stdout.isatty() else text


class RunResults(NamedTuple):
    completed: set  # test (bundle) names that ran to completion
    marks_by_test: dict  # test name -> list of "." / "F", one per assert, in order
    failures: list  # [(test, assertion, reason), ...], in the order they failed


def _failure_line(test: str, assertion: str, reason: str) -> str:
    suffix = f"  ->  {reason}" if reason else ""
    return f"  {_color('FAIL', _RED)}   {test}::{assertion}{suffix}"


def _record(results: RunResults, test: str, outcome: str, assertion: str, reason: str):
    """Tallies one assert result as it streams in; a FAIL is printed right
    away (and kept for the end-of-run recap), so it's visible next to the
    agent output that caused it."""
    if outcome == "PASS":
        results.marks_by_test[test].append(".")
        return
    results.marks_by_test[test].append("F")
    results.failures.append((test, assertion, reason))
    print(_failure_line(test, assertion, reason), flush=True)


def run_and_parse(
    mount_args: list, script: str, test_names: list, image: str
) -> RunResults:
    proc = subprocess.Popen(
        ["docker", "run", "--rm", *mount_args, image, "sh", "-c", script],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    assert proc.stdout is not None

    results = RunResults(set(), {name: [] for name in test_names}, [])
    current = None  # test whose cftest_start marker fired most recently

    for line in proc.stdout:
        print(line, end="", flush=True)

        start_match = _START_RE.search(line)
        if start_match:
            current = start_match.group(1)

        assert_match = _ASSERT_RE.search(line)
        if assert_match and current:
            outcome, name, reason = assert_match.groups()
            _record(results, current, outcome, name, reason or "")

        invalid_match = _INVALID_ASSERT_RE.search(line)
        if invalid_match and current:
            reason, name = invalid_match.groups()
            _record(results, current, "FAIL", name, f"invalid assert: {reason}")

        done_match = _DONE_RE.search(line)
        if done_match:
            results.completed.add(done_match.group(1))
            current = None

    proc.wait()
    return results


class DeployResults(NamedTuple):
    returncode: (
        int  # the deploy run's own exit code, used when it has no asserts at all
    )
    passed: int
    failed: int
    fell_back_to_failsafe: bool = False


def run_and_scan_asserts(cmd: list) -> DeployResults:
    """Like run_and_parse, but for a single plain policy run with no
    CFTEST-START/DONE wrapper -- there's only one implicit "test" (the whole
    run), so asserts are just counted flat, not attributed to a test name."""
    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True
    )
    assert proc.stdout is not None

    passed = failed = 0
    fell_back_to_failsafe = False
    for line in proc.stdout:
        print(line, end="", flush=True)

        if _FAILSAFE_RE.search(line):
            fell_back_to_failsafe = True

        assert_match = _ASSERT_RE.search(line)
        if assert_match:
            if assert_match.group(1) == "PASS":
                passed += 1
            else:
                failed += 1
        elif _INVALID_ASSERT_RE.search(line):
            failed += 1

    proc.wait()
    return DeployResults(proc.returncode, passed, failed, fell_back_to_failsafe)


def print_assert_totals(passed: int, failed: int, errors: int = 0) -> int:
    print()
    print("=" * 40)
    print(
        f"  cfengine test: {_color(f'{passed} passed', _GREEN)}, "
        f"{_color(f'{failed} failed', _RED)}, {errors} errors"
    )
    print("=" * 40)
    return 0 if failed == 0 and errors == 0 else 1


def _assert_dots(name: str, results: RunResults) -> str:
    """One character per assert, in order; an incomplete test gets a
    trailing "E", like pytest's "...E...." distinguishes error from fail."""
    marks = [
        _color(mark, _GREEN if mark == "." else _RED)
        for mark in results.marks_by_test[name]
    ]
    if name not in results.completed:
        marks.append(_color("E", _RED))
    return "".join(marks)


def _outcome_of(name: str, results: RunResults) -> str:
    """cftest_marker only fires if the test bundle finished evaluating, so a
    fatal abort (syntax error, missing bundle, ...) shows as ERROR rather
    than FAIL."""
    if name not in results.completed:
        return "ERROR"
    if "F" in results.marks_by_test[name]:
        return "FAIL"
    return "PASS"


def print_report(test_names: list, results: RunResults) -> int:
    outcomes = [(name, _outcome_of(name, results)) for name in test_names]

    print()
    for name in test_names:
        print(f"{name}  {_assert_dots(name, results)}")
    print("=" * 40)
    # Passed/failed count individual assert: checks, like pytest counts test
    # functions; errors count per file since an aborted run never reports
    # the rest of its checks.
    marks = Counter(mark for name in test_names for mark in results.marks_by_test[name])
    errored_files = sum(1 for _, outcome in outcomes if outcome == "ERROR")
    passed = marks["."]
    failed = marks["F"]
    print(
        f"  cfengine test: {_color(f'{passed} passed', _GREEN)}, "
        f"{_color(f'{failed} failed', _RED)}, "
        f"{errored_files} errors"
    )
    print("=" * 40)

    if all(outcome == "PASS" for _, outcome in outcomes):
        return 0

    for test, assertion, reason in results.failures:
        print(_failure_line(test, assertion, reason))
    for name, outcome in outcomes:
        if outcome == "ERROR":
            print(
                f"  {_color('ERROR', _RED)}  {name}  ->  test did not run to completion -- check "
                "output above for a cf-agent error (syntax error, missing bundle, ...)"
            )
    print()
    return 1
