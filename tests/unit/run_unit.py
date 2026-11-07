"""Runs the unit tests (tests/unit/test_*.py) in the built image and prints one line per test,
in the format of tests/run.sh:

    PASS: unit: <module>: <description>
    FAIL: unit: <module>: <description>: <reason>       (then up to 11 indented lines)

<module> is the file name without "test_" and ".py", <description> the test method's
one-line docstring (required: a test without one fails). tests/run.sh reads the same names
from the files, applies TEST_FILTER and passes the selected names:

    python /tests/unit/run_unit.py [<name>...]      no names: all tests

A name that matches no test prints a FAIL line. Exit code 0 when everything passed, else 1.
Each test's own output (log lines, prints) is captured and shown only when it fails.
"""
import contextlib
import io
import logging
import os
import sys
import unittest

UNIT_DIR = os.path.dirname(os.path.abspath(__file__))
MAX_LINES = 12


def tests_of(suite):
    for t in suite:
        if isinstance(t, unittest.TestSuite):
            yield from tests_of(t)
        else:
            yield t


def name_of(test) -> str:
    module = type(test).__module__.rsplit(".", 1)[-1]
    module = module[len("test_"):] if module.startswith("test_") else module
    doc = (getattr(test, test._testMethodName).__doc__ or "").strip()
    if not doc or "\n" in doc:
        return f"unit: {module}: {test._testMethodName} (no one-line docstring)"
    return f"unit: {module}: {doc}"


class _CurrentStderr:
    """Stream that writes to whatever sys.stderr is now (log handlers keep their stream)."""
    def write(self, s):
        return sys.stderr.write(s)

    def flush(self):
        sys.stderr.flush()


def run_one(test):
    """Returns None (passed) or the failure text."""
    result = unittest.TestResult()
    out = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
        test(result)
    problems = result.errors + result.failures + [(t, "unexpected success") for t in result.unexpectedSuccesses]
    if not problems:
        return None
    text = problems[0][1]
    lines = text.rstrip().splitlines()
    # reason first (the exception line), then the end of the traceback, then the output
    reason = lines[-1] if lines else "failed"
    detail = lines[-8:-1] + [ln for ln in out.getvalue().rstrip().splitlines()[-4:]]
    return "\n".join([reason] + detail)


def main(argv):
    if os.getuid() == 0:
        print("FAIL: unit: runner: run the unit tests as the scraper user, not root")
        return 1
    suite = unittest.defaultTestLoader.discover(UNIT_DIR, pattern="test_*.py", top_level_dir=UNIT_DIR)
    for h in logging.getLogger().handlers:
        if isinstance(h, logging.StreamHandler) and h.stream in (sys.stderr, sys.__stderr__):
            h.setStream(_CurrentStderr())
    tests = list(tests_of(suite))
    failed = 0
    # Test modules that do not import, and tests without a usable name: always reported
    for t in tests:
        if isinstance(t, unittest.loader._FailedTest):
            failed += 1
            err = str(t._exception).strip().splitlines()
            print(f"FAIL: unit: {t._testMethodName}: cannot load the test module: " + (err[-1] if err else "?"), flush=True)
    tests = [t for t in tests if not isinstance(t, unittest.loader._FailedTest)]
    by_name = {}
    for t in tests:
        by_name.setdefault(name_of(t), []).append(t)
    for name, group in by_name.items():
        if len(group) > 1 or name.endswith("(no one-line docstring)"):
            failed += 1
            print(f"FAIL: {name}: test names must be unique one-line docstrings", flush=True)
    wanted = argv or list(by_name)
    for name in wanted:
        group = by_name.get(name)
        if group is None:
            failed += 1
            print(f"FAIL: {name}: no such unit test", flush=True)
            continue
        if len(group) > 1 or name.endswith("(no one-line docstring)"):
            continue
        problem = run_one(group[0])
        if problem is None:
            print(f"PASS: {name}", flush=True)
        else:
            failed += 1
            lines = problem.splitlines()[:MAX_LINES]
            print(f"FAIL: {name}: " + "\n".join([lines[0]] + ["    " + ln for ln in lines[1:]]), flush=True)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
