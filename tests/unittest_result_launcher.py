#!/usr/bin/env python3
"""Run discovered tests and send their actual TestResult through a file descriptor.

The descriptor keeps result facts separate from test stdout and stderr. It is
not an operating-system isolation boundary for test code with the same
privileges as this launcher.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import unittest
from typing import Any, Sequence


RESULT_FORMAT = "until-loop-unittest-result/v1"


def result_payload(result: unittest.TestResult) -> dict[str, Any]:
    """Serialize the fields used by the parent checker from TestResult itself."""
    return {
        "format": RESULT_FORMAT,
        "complete": True,
        "tests_ran": result.testsRun,
        "failures": len(result.failures),
        "errors": len(result.errors),
        "skipped_tests": len(result.skipped),
        "expected_failures": len(result.expectedFailures),
        "unexpected_successes": len(result.unexpectedSuccesses),
        "successful": result.wasSuccessful(),
    }


def write_payload(result_fd: int, payload: dict[str, Any]) -> None:
    data = (json.dumps(payload, sort_keys=True) + "\n").encode("utf-8")
    offset = 0
    while offset < len(data):
        written = os.write(result_fd, data[offset:])
        if written <= 0:
            raise OSError("could not write structured unittest result")
        offset += written


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--result-fd", required=True, type=int)
    parser.add_argument("--pattern", default="test*.py")
    parser.add_argument("--verbosity", default=2, type=int)
    args = parser.parse_args(argv)
    try:
        suite = unittest.defaultTestLoader.discover(".", pattern=args.pattern)
        result = unittest.TextTestRunner(verbosity=args.verbosity).run(suite)
    except BaseException as error:
        try:
            write_payload(args.result_fd, {
                "format": RESULT_FORMAT,
                "complete": False,
                "launcher_error": f"{type(error).__name__}: {error}",
            })
        except OSError:
            pass
        print(f"unittest launcher failed: {error}", file=sys.stderr)
        return 2
    try:
        write_payload(args.result_fd, result_payload(result))
    except OSError as error:
        print(f"unittest launcher could not write its result: {error}", file=sys.stderr)
        return 2
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
