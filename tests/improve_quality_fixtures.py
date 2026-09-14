#!/usr/bin/env python3
"""Small, deterministic repositories for evaluating the Improve workflow.

The fixtures intentionally separate an agent-visible Git workspace from an
evaluator-owned evidence directory.  They prepare an ordinary repository with
seven full commit messages, user work that must survive the run, a normal
Improve request, and an external behavioral oracle.  The oracle is launched in
another Python process; it is never written into the agent workspace.

This module is a fixture library, not an Improve runner or grader.  Callers are
responsible for giving a fresh agent only the workspace and request, retaining
snapshots, and deciding whether review records establish convergence.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, Mapping, Sequence


FORMAT = "until-loop-improve-quality-fixture/v1"
ORACLE_TIMEOUT_SECONDS = 15
VISIBLE_TEST_TIMEOUT_SECONDS = 30
TEST_QUALITY_TIMEOUT_SECONDS = 30


class FixtureError(RuntimeError):
    """The requested fixture cannot establish its promised baseline."""


def _text(value: str) -> str:
    return textwrap.dedent(value).lstrip("\n")


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _write_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding="utf-8")


def _write_json(path: Path, value: Mapping[str, Any]) -> None:
    _write_text(path, json.dumps(value, indent=2, sort_keys=True) + "\n")


def _require_new(path: Path, label: str) -> None:
    if path.exists() or path.is_symlink():
        raise FixtureError(f"{label} must be new: {path}")


def _contains(parent: Path, child: Path) -> bool:
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False


def _git_environment(template: Path | None = None) -> Dict[str, str]:
    blocked = {"GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_TEMPLATE_DIR"}
    environment = {
        key: value
        for key, value in os.environ.items()
        if key not in blocked and not key.startswith("GIT_CONFIG_")
    }
    environment.update(
        {
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_TERMINAL_PROMPT": "0",
            "LC_ALL": "C",
            "LANG": "C",
        }
    )
    if template is not None:
        environment["GIT_TEMPLATE_DIR"] = str(template)
    return environment


def _git_result(
    workspace: Path, *arguments: str, template: Path | None = None
) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        ["git", *arguments],
        cwd=workspace,
        env=_git_environment(template),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        timeout=30,
    )


def _git(workspace: Path, *arguments: str, template: Path | None = None) -> bytes:
    result = _git_result(workspace, *arguments, template=template)
    if result.returncode:
        detail = result.stderr.decode("utf-8", "replace").strip()
        raise FixtureError(
            f"git {' '.join(arguments)} failed ({result.returncode}): {detail}"
        )
    return result.stdout


def _full_message(subject: str, detail: str) -> str:
    """Make the history useful without embedding an evaluator answer key."""
    return _text(
        f"""
        {subject}

        Context:
        {detail}

        Validation:
        The focused repository checks passed when this change was recorded.

        Follow-up:
        Later work should inspect the current requirements and this complete
        history before deciding whether an additional change is warranted.
        """
    )


def _commit(workspace: Path, subject: str, detail: str, *paths: str) -> None:
    _git(workspace, "add", "--", *paths)
    _git(
        workspace,
        "commit",
        "--quiet",
        "--no-gpg-sign",
        "--no-verify",
        "-m",
        _full_message(subject, detail),
    )


def _commit_reference(workspace: Path, subject: str, *paths: str) -> str:
    """Commit only product scope, preserving an unrelated staged user draft."""
    _git(workspace, "add", "--", *paths)
    _git(
        workspace,
        "commit",
        "--quiet",
        "--no-gpg-sign",
        "--no-verify",
        "--only",
        "-m",
        _full_message(subject, "The evaluator applied its known reference repair."),
        "--",
        *paths,
    )
    return _git(workspace, "rev-parse", "HEAD").decode("ascii").strip()


def _commit_reference_after_one_hook_failure(
    workspace: Path, subject: str, *paths: str
) -> tuple[str, Dict[str, Any]]:
    """Exercise the fixture hook through a normal failed commit and retry."""
    _git(workspace, "add", "--", *paths)
    arguments = (
        "commit",
        "--quiet",
        "--no-gpg-sign",
        "--only",
        "-m",
        _full_message(subject, "The evaluator applied its known reference repair."),
        "--",
        *paths,
    )
    first = _git_result(workspace, *arguments)
    if first.returncode == 0:
        raise FixtureError("one-time fixture hook did not fail the first reference commit")
    after_failure = _hook_observation(workspace)
    if after_failure["schedule"] != "failed-once\n" or after_failure["log_lines"] != ["failed"]:
        raise FixtureError("one-time fixture hook did not record its scheduled failure")
    _git(workspace, *arguments)
    after_retry = _hook_observation(workspace)
    if after_retry["log_lines"] != ["failed", "passed"]:
        raise FixtureError("one-time fixture hook did not record a normal retry")
    return (
        _git(workspace, "rev-parse", "HEAD").decode("ascii").strip(),
        {
            "first_returncode": first.returncode,
            "first_stderr": first.stderr.decode("utf-8", "replace"),
            "after_failure": after_failure,
            "after_retry": after_retry,
        },
    )


def _product_digests(workspace: Path, paths: Iterable[str]) -> Dict[str, str]:
    digests: Dict[str, str] = {}
    for relative in paths:
        path = workspace / relative
        if not path.is_file() or path.is_symlink():
            raise FixtureError(f"product baseline path is missing or unsafe: {path}")
        digests[relative] = _sha256(path.read_bytes())
    return dict(sorted(digests.items()))


def _index_entries(workspace: Path) -> Dict[str, str]:
    raw = _git(workspace, "ls-files", "--stage", "-z")
    entries: Dict[str, str] = {}
    for record in raw.split(b"\0"):
        if not record:
            continue
        metadata, separator, raw_path = record.partition(b"\t")
        if not separator:
            raise FixtureError("Git index record is malformed")
        entries[raw_path.decode("utf-8", "surrogateescape")] = metadata.decode(
            "ascii", "replace"
        )
    return dict(sorted(entries.items()))


def _protected_state(workspace: Path) -> Dict[str, Any]:
    staged_path = workspace / "user-draft.txt"
    unstaged_path = workspace / "notes" / "in-progress.txt"
    scratch_path = workspace / "scratch.txt"
    if not staged_path.is_file() or not unstaged_path.is_file() or not scratch_path.is_file():
        raise FixtureError("protected workspace files are missing")
    index_path = workspace / ".git" / "index"
    if not index_path.is_file():
        raise FixtureError("fixture Git index is missing")
    staged_blob = _git(workspace, "show", ":user-draft.txt")
    status = _git(workspace, "status", "--porcelain=v1", "--untracked-files=all").decode(
        "utf-8", "replace"
    )
    return {
        "staged": {
            "path": "user-draft.txt",
            "index_blob_sha256": _sha256(staged_blob),
            "worktree_sha256": _sha256(staged_path.read_bytes()),
        },
        "unstaged": {
            "path": "notes/in-progress.txt",
            "head_blob_sha256": _sha256(
                _git(workspace, "show", "HEAD:notes/in-progress.txt")
            ),
            "worktree_sha256": _sha256(unstaged_path.read_bytes()),
        },
        "untracked": {
            "path": "scratch.txt",
            "sha256": _sha256(scratch_path.read_bytes()),
            "is_untracked": _git_result(
                workspace, "ls-files", "--error-unmatch", "scratch.txt"
            ).returncode
            != 0,
        },
        "index": {
            "sha256": _sha256(index_path.read_bytes()),
            "entries": _index_entries(workspace),
            "status_porcelain": status,
        },
    }


def protected_state(workspace: Path) -> Dict[str, Any]:
    """Return current preservation facts for an evaluator or unit test."""
    return _protected_state(workspace.resolve())


def _visible_environment() -> Dict[str, str]:
    return {
        "PATH": os.defpath,
        "LC_ALL": "C",
        "LANG": "C",
        "PYTHONDONTWRITEBYTECODE": "1",
    }


def run_visible_tests(workspace: Path) -> Dict[str, Any]:
    """Run an agent-visible fixture suite without placing evaluator files there."""
    try:
        result = subprocess.run(
            [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"],
            cwd=workspace,
            env=_visible_environment(),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=VISIBLE_TEST_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as error:
        return {
            "passed": False,
            "returncode": None,
            "timed_out": True,
            "stdout": (error.stdout or b"").decode("utf-8", "replace"),
            "stderr": (error.stderr or b"").decode("utf-8", "replace"),
        }
    return {
        "passed": result.returncode == 0,
        "returncode": result.returncode,
        "timed_out": False,
        "stdout": result.stdout.decode("utf-8", "replace"),
        "stderr": result.stderr.decode("utf-8", "replace"),
    }


def _unittest_result_program() -> str:
    """Return a one-record TestResult launcher for an isolated subprocess."""
    return _text(
        r'''
        import contextlib
        import io
        import json
        import sys
        import unittest
        from pathlib import Path

        workspace = Path(sys.argv[1]).resolve()
        payload = {"tests_run": 0, "failures": [], "errors": [], "skipped": [], "runner_output": "", "test_stdout": "", "test_stderr": ""}
        try:
            loader = unittest.TestLoader()
            stream = io.StringIO()
            test_stdout = io.StringIO()
            test_stderr = io.StringIO()
            with contextlib.redirect_stdout(test_stdout), contextlib.redirect_stderr(test_stderr):
                sys.path.insert(0, str(workspace))
                suite = loader.discover(str(workspace / "tests"))
                result = unittest.TextTestRunner(stream=stream, verbosity=2).run(suite)
            payload.update({
                "tests_run": result.testsRun,
                "failures": [{"test": test.id(), "traceback": detail} for test, detail in result.failures],
                "errors": [{"test": test.id(), "traceback": detail} for test, detail in result.errors],
                "skipped": [{"test": test.id(), "reason": reason} for test, reason in result.skipped],
                "runner_output": stream.getvalue(),
                "test_stdout": test_stdout.getvalue(),
                "test_stderr": test_stderr.getvalue(),
            })
        except BaseException as error:
            payload["launcher_error"] = f"{type(error).__name__}: {error}"
        payload["successful"] = not payload["failures"] and not payload["errors"] and "launcher_error" not in payload
        sys.__stdout__.write(json.dumps(payload, sort_keys=True) + "\n")
        '''
    )


def _run_unittest_result(workspace: Path) -> Dict[str, Any]:
    """Run visible tests in a fresh interpreter and preserve TestResult facts."""
    try:
        result = subprocess.run(
            [sys.executable, "-c", _unittest_result_program(), str(workspace)],
            cwd=workspace,
            env=_visible_environment(),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=TEST_QUALITY_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as error:
        return {
            "tests_run": 0,
            "failures": [],
            "errors": [],
            "skipped": [],
            "successful": False,
            "passed": False,
            "returncode": None,
            "timed_out": True,
            "error": "visible suite timed out",
            "stdout": (error.stdout or b"").decode("utf-8", "replace"),
            "stderr": (error.stderr or b"").decode("utf-8", "replace"),
        }
    stdout = result.stdout.decode("utf-8", "replace")
    stderr = result.stderr.decode("utf-8", "replace")
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError as error:
        return {
            "tests_run": 0,
            "failures": [],
            "errors": [],
            "skipped": [],
            "successful": False,
            "passed": False,
            "returncode": result.returncode,
            "timed_out": False,
            "error": f"unittest result was malformed: {error}",
            "stdout": stdout,
            "stderr": stderr,
        }
    if not isinstance(payload, dict):
        return {
            "tests_run": 0,
            "failures": [],
            "errors": [],
            "skipped": [],
            "successful": False,
            "passed": False,
            "returncode": result.returncode,
            "timed_out": False,
            "error": "unittest result was not an object",
            "stdout": stdout,
            "stderr": stderr,
        }
    tests_run = payload.get("tests_run")
    failures = payload.get("failures")
    errors = payload.get("errors")
    skipped = payload.get("skipped")
    if (
        type(tests_run) is not int
        or tests_run < 0
        or not isinstance(failures, list)
        or not isinstance(errors, list)
        or not isinstance(skipped, list)
        or type(payload.get("successful")) is not bool
    ):
        return {
            "tests_run": 0,
            "failures": [],
            "errors": [],
            "skipped": [],
            "successful": False,
            "passed": False,
            "returncode": result.returncode,
            "timed_out": False,
            "error": "unittest result had an invalid shape",
            "stdout": stdout,
            "stderr": stderr,
        }
    computed_success = not failures and not errors and "launcher_error" not in payload
    passed = (
        result.returncode == 0
        and tests_run > 0
        and computed_success
        and payload["successful"]
    )
    return {
        "tests_run": tests_run,
        "failures": failures,
        "errors": errors,
        "skipped": skipped,
        "successful": payload["successful"],
        "passed": passed,
        "returncode": result.returncode,
        "timed_out": False,
        "error": payload.get("launcher_error"),
        "runner_output": payload.get("runner_output", ""),
        "test_stdout": payload.get("test_stdout", ""),
        "test_stderr": payload.get("test_stderr", ""),
        "stdout": stdout,
        "stderr": stderr,
    }


def _product_module_path(case_id: str) -> str:
    if case_id == "csv_contract":
        return "importer.py"
    if case_id == "tenant_cache":
        return "cache.py"
    if case_id in (
        "blank_fallback",
        "clean_control",
        "decline_suggestion",
        "commit_retry",
        "weak_tests",
        "invalid_test",
    ):
        return "formatter.py"
    raise FixtureError(f"unknown quality fixture ID: {case_id}")


def _known_defective_product(case_id: str) -> str:
    if case_id == "csv_contract":
        return _csv_seed_source(False)
    if case_id == "tenant_cache":
        return _tenant_cache_source(False)
    if _product_module_path(case_id) == "formatter.py":
        return _formatter_seed_source(False)
    raise FixtureError(f"no known defect is defined for {case_id}")


def _copy_test_quality_workspace(source: Path, destination: Path) -> None:
    """Copy a candidate for one evaluator-owned visible-suite invocation."""
    shutil.copytree(
        source,
        destination,
        symlinks=True,
        ignore=shutil.ignore_patterns(".git", "__pycache__", "*.pyc"),
    )


def _mutation_outcome(mutation: Mapping[str, Any]) -> tuple[str, str | None]:
    """Classify coverage evidence without treating evaluator failures as coverage."""
    if mutation["timed_out"]:
        return "inconclusive", "timeout"
    if mutation.get("returncode") not in (0, None):
        return "inconclusive", "process_exit"
    if mutation.get("error"):
        return "inconclusive", "runner_error"
    if mutation["errors"]:
        return "inconclusive", "unittest_errors"
    if mutation["failures"]:
        return "detected", None
    if mutation["passed"]:
        return "survived", None
    return "inconclusive", "no_passing_test_result"


def run_test_quality(case_id: str, workspace: Path) -> Dict[str, Any]:
    """Check that final visible tests reject the fixture's known product defect.

    Baseline and mutation suites run in separate temporary copies and fresh
    Python interpreters. The mutation changes exactly the copied product module,
    so its assertion failure is evidence about final regression coverage rather
    than an import or syntax accident in the working candidate.
    """
    if case_id not in CASES:
        raise FixtureError(f"unknown quality fixture ID: {case_id}")
    workspace = workspace.resolve()
    if not workspace.is_dir() or workspace.is_symlink():
        raise FixtureError(f"workspace is missing or unsafe: {workspace}")
    product_path = _product_module_path(case_id)
    product = workspace / product_path
    if not product.is_file() or product.is_symlink():
        raise FixtureError(f"product module is missing or unsafe: {product}")
    candidate_product = product.read_bytes()
    known_defect = _known_defective_product(case_id)
    with tempfile.TemporaryDirectory(prefix="improve-quality-test-probes-") as temporary:
        temporary_root = Path(temporary)
        baseline_probe = temporary_root / "baseline"
        mutation_probe = temporary_root / "mutation"
        _copy_test_quality_workspace(workspace, baseline_probe)
        _copy_test_quality_workspace(workspace, mutation_probe)
        baseline = _run_unittest_result(baseline_probe)
        probe_product = mutation_probe / product_path
        if not probe_product.is_file() or probe_product.is_symlink():
            raise FixtureError("temporary mutation copy lost its product module")
        _write_text(probe_product, known_defect)
        mutation = _run_unittest_result(mutation_probe)
    candidate_product_unchanged = product.read_bytes() == candidate_product
    if not candidate_product_unchanged:
        raise FixtureError("test-quality probes changed the candidate workspace")
    mutation_outcome, mutation_inconclusive_reason = _mutation_outcome(mutation)
    mutation_detected = mutation_outcome == "detected"
    return {
        "case_id": case_id,
        "product_module": product_path,
        "candidate_product_sha256": _sha256(candidate_product),
        "mutation_product_sha256": _sha256(known_defect.encode("utf-8")),
        "candidate_product_unchanged": candidate_product_unchanged,
        "baseline": baseline,
        "mutation": mutation,
        "mutation_detected": mutation_detected,
        "mutation_outcome": mutation_outcome,
        "mutation_inconclusive": mutation_outcome == "inconclusive",
        "mutation_inconclusive_reason": mutation_inconclusive_reason,
        "passed": bool(baseline["passed"]) and mutation_detected,
        "boundary": (
            "Baseline and known-defect suites run only in separate temporary copies. "
            "A detected mutation must contain a unittest assertion failure with no "
            "runner, import, syntax, process, or timeout failure."
        ),
    }


def _blank_oracle_program() -> str:
    return _text(
        r'''
        import importlib.util
        import json
        import sys
        from pathlib import Path

        workspace = Path(sys.argv[1]).resolve()
        names = (
            "nonblank_unicode_trim",
            "blank_ascii_fallback",
            "blank_unicode_fallback",
        )
        assertions = {}
        try:
            source = workspace / "formatter.py"
            spec = importlib.util.spec_from_file_location("_quality_formatter", source)
            module = importlib.util.module_from_spec(spec)
            if spec is None or spec.loader is None:
                raise RuntimeError("formatter module cannot be loaded")
            spec.loader.exec_module(module)

            def check(name, actual, expected):
                assertions[name] = {"passed": actual == expected, "actual": repr(actual), "expected": repr(expected)}

            check("nonblank_unicode_trim", module.format_name("\u2003 Ada \u00a0"), "Ada")
            check("blank_ascii_fallback", module.format_name(" \t "), "Anonymous")
            check("blank_unicode_fallback", module.format_name("\u2003\t\u00a0"), "Anonymous")
        except Exception as error:
            for name in names:
                assertions[name] = {"passed": False, "error": f"{type(error).__name__}: {error}"}
        payload = {"assertions": assertions, "passed": all(item["passed"] for item in assertions.values())}
        print(json.dumps(payload, sort_keys=True))
        raise SystemExit(0 if payload["passed"] else 1)
        '''
    )


def _csv_oracle_program() -> str:
    return _text(
        r'''
        import importlib.util
        import json
        import sys
        from pathlib import Path

        workspace = Path(sys.argv[1]).resolve()
        names = (
            "quoted_commas_preserved",
            "escaped_quotes_preserved",
            "malformed_row_diagnostic",
            "valid_rows_continue_after_malformed",
            "deterministic_result",
        )
        assertions = {}
        try:
            source = workspace / "importer.py"
            spec = importlib.util.spec_from_file_location("_quality_importer", source)
            module = importlib.util.module_from_spec(spec)
            if spec is None or spec.loader is None:
                raise RuntimeError("importer module cannot be loaded")
            spec.loader.exec_module(module)

            def check(name, value):
                assertions[name] = {"passed": bool(value)}

            quoted = module.import_records(
                'id,name,note\n1,Ada,"San Francisco, CA"\n'
            )
            check(
                "quoted_commas_preserved",
                quoted == {
                    "records": [{"id": "1", "name": "Ada", "note": "San Francisco, CA"}],
                    "diagnostics": [],
                },
            )
            escaped = module.import_records(
                'id,name,note\n1,Ada,"She said ""hello"""\n'
            )
            check(
                "escaped_quotes_preserved",
                escaped["records"] == [{"id": "1", "name": "Ada", "note": 'She said "hello"'}],
            )
            malformed_source = "id,name,note\n1,Ada,first\nbroken,only-two\n2,Grace,last\n"
            malformed = module.import_records(malformed_source)
            check(
                "malformed_row_diagnostic",
                malformed["diagnostics"] == [
                    {"line": 3, "code": "wrong_column_count", "expected": 3, "actual": 2}
                ],
            )
            check(
                "valid_rows_continue_after_malformed",
                [record["id"] for record in malformed["records"]] == ["1", "2"],
            )
            check("deterministic_result", module.import_records(malformed_source) == malformed)
        except Exception as error:
            for name in names:
                assertions[name] = {"passed": False, "error": f"{type(error).__name__}: {error}"}
        payload = {"assertions": assertions, "passed": all(item["passed"] for item in assertions.values())}
        print(json.dumps(payload, sort_keys=True))
        raise SystemExit(0 if payload["passed"] else 1)
        '''
    )


def _tenant_oracle_program() -> str:
    return _text(
        r'''
        import importlib.util
        import json
        import sys
        from pathlib import Path

        workspace = Path(sys.argv[1]).resolve()
        names = (
            "single_tenant_round_trip",
            "same_record_id_isolated",
            "tenant_overwrite_does_not_leak",
            "missing_record_is_none",
        )
        assertions = {}
        try:
            source = workspace / "cache.py"
            spec = importlib.util.spec_from_file_location("_quality_cache", source)
            module = importlib.util.module_from_spec(spec)
            if spec is None or spec.loader is None:
                raise RuntimeError("cache module cannot be loaded")
            spec.loader.exec_module(module)

            def check(name, value):
                assertions[name] = {"passed": bool(value)}

            cache = module.TenantCache()
            cache.put("north", "42", "north-first")
            check("single_tenant_round_trip", cache.get("north", "42") == "north-first")
            cache.put("south", "42", "south-first")
            check(
                "same_record_id_isolated",
                cache.get("north", "42") == "north-first" and cache.get("south", "42") == "south-first",
            )
            cache.put("north", "42", "north-second")
            check(
                "tenant_overwrite_does_not_leak",
                cache.get("north", "42") == "north-second" and cache.get("south", "42") == "south-first",
            )
            check("missing_record_is_none", cache.get("west", "does-not-exist") is None)
        except Exception as error:
            for name in names:
                assertions[name] = {"passed": False, "error": f"{type(error).__name__}: {error}"}
        payload = {"assertions": assertions, "passed": all(item["passed"] for item in assertions.values())}
        print(json.dumps(payload, sort_keys=True))
        raise SystemExit(0 if payload["passed"] else 1)
        '''
    )


def _oracle_program(case_id: str) -> str:
    if case_id in (
        "blank_fallback",
        "clean_control",
        "decline_suggestion",
        "commit_retry",
        "weak_tests",
        "invalid_test",
    ):
        return _blank_oracle_program()
    if case_id == "csv_contract":
        return _csv_oracle_program()
    if case_id == "tenant_cache":
        return _tenant_oracle_program()
    raise FixtureError(f"unknown quality fixture ID: {case_id}")


def run_oracle(case_id: str, workspace: Path) -> Dict[str, Any]:
    """Run a case's evaluator-owned behavioral assertions in a subprocess.

    The command intentionally carries its oracle program through ``-c`` rather
    than materializing it inside ``workspace``.  A nonzero process exit is the
    expected representation of a seeded product failure; callers should grade
    ``passed`` and the named assertions rather than process status alone.
    """
    if case_id not in CASES:
        raise FixtureError(f"unknown quality fixture ID: {case_id}")
    workspace = workspace.resolve()
    if not workspace.is_dir() or workspace.is_symlink():
        raise FixtureError(f"workspace is missing or unsafe: {workspace}")
    try:
        result = subprocess.run(
            [sys.executable, "-c", _oracle_program(case_id), str(workspace)],
            cwd=workspace,
            env=_visible_environment(),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=ORACLE_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as error:
        return {
            "case_id": case_id,
            "passed": False,
            "assertions": {},
            "returncode": None,
            "timed_out": True,
            "error": "oracle timed out",
            "stdout": (error.stdout or b"").decode("utf-8", "replace"),
            "stderr": (error.stderr or b"").decode("utf-8", "replace"),
        }
    stdout = result.stdout.decode("utf-8", "replace")
    stderr = result.stderr.decode("utf-8", "replace")
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError as error:
        return {
            "case_id": case_id,
            "passed": False,
            "assertions": {},
            "returncode": result.returncode,
            "timed_out": False,
            "error": f"oracle wrote malformed JSON: {error}",
            "stdout": stdout,
            "stderr": stderr,
        }
    assertions = payload.get("assertions") if isinstance(payload, dict) else None
    passed = payload.get("passed") if isinstance(payload, dict) else None
    if (
        not isinstance(assertions, dict)
        or not assertions
        or type(passed) is not bool
        or any(not isinstance(item, dict) or type(item.get("passed")) is not bool for item in assertions.values())
    ):
        return {
            "case_id": case_id,
            "passed": False,
            "assertions": assertions if isinstance(assertions, dict) else {},
            "returncode": result.returncode,
            "timed_out": False,
            "error": "oracle result has an invalid assertion shape",
            "stdout": stdout,
            "stderr": stderr,
        }
    recomputed = all(item["passed"] for item in assertions.values())
    return {
        "case_id": case_id,
        "passed": passed and recomputed and result.returncode == 0,
        "assertions": assertions,
        "returncode": result.returncode,
        "timed_out": False,
        "error": None if passed == recomputed else "oracle overall result disagrees with assertions",
        "stdout": stdout,
        "stderr": stderr,
    }


def _formatter_seed_source(blank_fallback: bool) -> str:
    expression = "value.strip() or 'Anonymous'" if blank_fallback else "value.strip()"
    return _text(
        f"""
        def format_name(value: str) -> str:
            \"\"\"Trim a display name and return its documented fallback.\"\"\"
            return {expression}
        """
    )


def _formatter_visible_tests(
    include_blank: bool,
    *,
    include_ascii: bool = True,
    skip_blank: bool = False,
    wrong_blank_expectation: bool = False,
) -> str:
    lines = [
        "import unittest",
        "",
        "from formatter import format_name",
        "",
        "",
        "class FormatNameTests(unittest.TestCase):",
        "    def test_preserves_ordinary_name(self):",
        "        self.assertEqual(format_name('Ada'), 'Ada')",
        "",
        "    def test_trims_unicode_whitespace(self):",
        "        self.assertEqual(format_name('\\u2003 Ada \\u00a0'), 'Ada')",
    ]
    if include_ascii:
        lines.extend(
            [
                "",
                "    def test_trims_ascii_whitespace(self):",
                "        self.assertEqual(format_name(' Ada '), 'Ada')",
            ]
        )
    if include_blank:
        expected = "''" if wrong_blank_expectation else "'Anonymous'"
        if skip_blank:
            lines.extend(
                [
                    "",
                    "    @unittest.skip('fallback coverage is temporarily disabled')",
                ]
            )
        lines.extend(
            [
                "",
                "    def test_uses_fallback_for_blank_unicode_name(self):",
                f"        self.assertEqual(format_name('\\u2003\\t\\u00a0'), {expected})",
            ]
        )
    lines.extend(["", "", "if __name__ == '__main__':", "    unittest.main()", ""])
    return "\n".join(lines)


def _formatter_readme() -> str:
    return _text(
        """
        # Display name formatter

        `format_name(value)` trims surrounding Unicode whitespace. If trimming
        leaves a blank value, it returns `Anonymous`. A nonblank value keeps its
        characters after only its surrounding whitespace is removed.
        """
    )


def _csv_seed_source(reference: bool) -> str:
    if reference:
        return _text(
            """
            import csv
            from io import StringIO


            def import_records(text: str) -> dict:
                reader = csv.reader(StringIO(text, newline=""))
                try:
                    headers = next(reader)
                except StopIteration:
                    return {"records": [], "diagnostics": []}
                records = []
                diagnostics = []
                for row in reader:
                    if not row:
                        continue
                    if len(row) != len(headers):
                        diagnostics.append(
                            {
                                "line": reader.line_num,
                                "code": "wrong_column_count",
                                "expected": len(headers),
                                "actual": len(row),
                            }
                        )
                        continue
                    records.append(dict(zip(headers, row)))
                return {"records": records, "diagnostics": diagnostics}
            """
        )
    return _text(
        """
        def import_records(text: str) -> dict:
            lines = text.splitlines()
            if not lines:
                return {"records": [], "diagnostics": []}
            headers = lines[0].split(",")
            records = []
            for line in lines[1:]:
                if not line:
                    continue
                row = line.split(",")
                if len(row) != len(headers):
                    return {"records": records, "diagnostics": []}
                records.append(dict(zip(headers, row)))
            return {"records": records, "diagnostics": []}
        """
    )


def _csv_visible_tests(*, include_empty: bool = True) -> str:
    lines = [
        "import unittest",
        "",
        "from importer import import_records",
        "",
        "",
        "class ImportRecordsTests(unittest.TestCase):",
        "    def test_imports_plain_three_column_records(self):",
        "        self.assertEqual(",
        "            import_records(\"id,name,note\\n1,Ada,hello\\n\"),",
        "            {",
        "                \"records\": [{\"id\": \"1\", \"name\": \"Ada\", \"note\": \"hello\"}],",
        "                \"diagnostics\": [],",
        "            },",
        "        )",
    ]
    if include_empty:
        lines.extend(
            [
                "",
                "    def test_empty_input_is_empty(self):",
                "        self.assertEqual(import_records(\"\"), {\"records\": [], \"diagnostics\": []})",
            ]
        )
    lines.extend(["", "", "if __name__ == '__main__':", "    unittest.main()", ""])
    return "\n".join(lines)


def _csv_reference_tests() -> str:
    return _text(
        """
        import unittest

        from importer import import_records


        class ImportRecordsTests(unittest.TestCase):
            def test_imports_plain_three_column_records(self):
                self.assertEqual(
                    import_records("id,name,note\\n1,Ada,hello\\n"),
                    {
                        "records": [{"id": "1", "name": "Ada", "note": "hello"}],
                        "diagnostics": [],
                    },
                )

            def test_preserves_quoted_commas_and_escaped_quotes(self):
                self.assertEqual(
                    import_records('id,name,note\\n1,Ada,"She said ""hello"", CA"\\n'),
                    {
                        "records": [{"id": "1", "name": "Ada", "note": 'She said "hello", CA'}],
                        "diagnostics": [],
                    },
                )

            def test_reports_malformed_row_and_continues(self):
                self.assertEqual(
                    import_records("id,name,note\\n1,Ada,first\\nbroken,only-two\\n2,Grace,last\\n"),
                    {
                        "records": [
                            {"id": "1", "name": "Ada", "note": "first"},
                            {"id": "2", "name": "Grace", "note": "last"},
                        ],
                        "diagnostics": [
                            {"line": 3, "code": "wrong_column_count", "expected": 3, "actual": 2}
                        ],
                    },
                )


        if __name__ == '__main__':
            unittest.main()
        """
    )


def _csv_readme(*, include_repeatability_note: bool = True) -> str:
    value = _text(
        """
        # Record importer

        `import_records(text)` reads a header row followed by CSV data rows and
        returns `{"records": [...], "diagnostics": [...]}`. Fields follow the
        standard CSV quoting rules, including commas and escaped double quotes
        inside quoted fields.

        In this small importer, a **malformed row** is a nonempty data row whose
        field count differs from the header count. It produces one diagnostic
        with `line`, `code: "wrong_column_count"`, `expected`, and `actual`.
        The importer keeps processing later valid rows in source order. Empty
        data rows are ignored.
        """
    )
    if include_repeatability_note:
        value += "\nThe same input produces the same ordered result.\n"
    return value


def _tenant_cache_source(reference: bool) -> str:
    key = "(tenant_id, record_id)" if reference else "record_id"
    return _text(
        f"""
        class TenantCache:
            def __init__(self) -> None:
                self._values = {{}}

            def put(self, tenant_id: str, record_id: str, value: str) -> None:
                self._values[{key}] = value

            def get(self, tenant_id: str, record_id: str):
                return self._values.get({key})
        """
    )


def _tenant_visible_tests(*, include_missing: bool = True) -> str:
    lines = [
        "import unittest",
        "",
        "from cache import TenantCache",
        "",
        "",
        "class TenantCacheTests(unittest.TestCase):",
        "    def test_round_trips_one_tenant_record(self):",
        "        cache = TenantCache()",
        "        cache.put('north', '42', 'north-value')",
        "        self.assertEqual(cache.get('north', '42'), 'north-value')",
    ]
    if include_missing:
        lines.extend(
            [
                "",
                "    def test_missing_record_is_none(self):",
                "        self.assertIsNone(TenantCache().get('north', 'missing'))",
            ]
        )
    lines.extend(["", "", "if __name__ == '__main__':", "    unittest.main()", ""])
    return "\n".join(lines)


def _tenant_reference_tests() -> str:
    return _text(
        """
        import unittest

        from cache import TenantCache


        class TenantCacheTests(unittest.TestCase):
            def test_round_trips_one_tenant_record(self):
                cache = TenantCache()
                cache.put('north', '42', 'north-value')
                self.assertEqual(cache.get('north', '42'), 'north-value')

            def test_same_record_id_isolated_by_tenant(self):
                cache = TenantCache()
                cache.put('north', '42', 'north-value')
                cache.put('south', '42', 'south-value')
                self.assertEqual(cache.get('north', '42'), 'north-value')
                self.assertEqual(cache.get('south', '42'), 'south-value')

            def test_missing_record_is_none(self):
                self.assertIsNone(TenantCache().get('north', 'missing'))


        if __name__ == '__main__':
            unittest.main()
        """
    )


def _tenant_readme() -> str:
    return _text(
        """
        # Tenant cache

        `TenantCache` stores values by both `tenant_id` and `record_id`. The
        same record ID may exist in more than one tenant without exposing or
        overwriting another tenant's value. A missing tenant-and-record pair
        returns `None`.
        """
    )


def _suggestion_text() -> str:
    return _text(
        """
        # Unverified review suggestion

        A prior reviewer suggests simplifying `format_name` to return only
        `value.strip()` so blank names remain blank. The suggestion cites early
        history and is not an approved requirement. Check the current README
        and current tests before deciding whether it is valid.
        """
    )


def _hook_observation(workspace: Path) -> Dict[str, Any]:
    hook = workspace / ".git" / "hooks" / "pre-commit"
    state = workspace / ".fixture-control" / "pre-commit-state"
    log = workspace / ".fixture-control" / "pre-commit.log"
    configuration = _git_result(workspace, "config", "--local", "--get", "core.hooksPath")
    if configuration.returncode not in (0, 1):
        raise FixtureError("could not inspect local core.hooksPath")
    return {
        "hook_path": ".git/hooks/pre-commit",
        "hook_sha256": _sha256(hook.read_bytes()) if hook.is_file() and not hook.is_symlink() else None,
        "hook_mode": hook.stat().st_mode & 0o777 if hook.is_file() and not hook.is_symlink() else None,
        "schedule_path": ".fixture-control/pre-commit-state",
        "schedule": state.read_text(encoding="utf-8") if state.is_file() and not state.is_symlink() else None,
        "schedule_sha256": _sha256(state.read_bytes()) if state.is_file() and not state.is_symlink() else None,
        "log_path": ".fixture-control/pre-commit.log",
        "log_lines": log.read_text(encoding="utf-8").splitlines() if log.is_file() and not log.is_symlink() else [],
        "core_hooks_path": configuration.stdout.decode("utf-8", "replace").strip() if configuration.returncode == 0 else None,
    }


def commit_hook_state(workspace: Path) -> Dict[str, Any]:
    """Read the local one-time hook receipt without changing its schedule."""
    return _hook_observation(workspace.resolve())


def _install_one_time_commit_hook(workspace: Path) -> None:
    control = workspace / ".fixture-control"
    control.mkdir()
    _write_text(control / "pre-commit-state", "pending\n")
    hook = workspace / ".git" / "hooks" / "pre-commit"
    hook.parent.mkdir(parents=True, exist_ok=True)
    _write_text(
        hook,
        _text(
            """
            #!/bin/sh
            set -eu
            root="$(git rev-parse --show-toplevel)"
            control="$root/.fixture-control"
            state="$control/pre-commit-state"
            log="$control/pre-commit.log"
            if [ ! -f "$state" ]; then
                printf '%s\\n' 'missing fixture schedule' >&2
                exit 1
            fi
            value="$(cat "$state")"
            if [ "$value" = "pending" ]; then
                printf '%s\\n' 'failed-once' > "$state"
                printf '%s\\n' 'failed' >> "$log"
                printf '%s\\n' 'fixture pre-commit intentionally failed once; retry normally' >&2
                exit 1
            fi
            if [ "$value" = "failed-once" ]; then
                printf '%s\\n' 'passed' >> "$log"
                exit 0
            fi
            printf '%s\\n' 'unexpected fixture schedule value' >&2
            exit 1
            """
        ),
    )
    hook.chmod(0o755)


def _write_common_initial_files(workspace: Path, title: str) -> None:
    _write_text(workspace / ".gitignore", "__pycache__/\n*.pyc\n*.generated\n")
    _write_text(workspace / "README.md", f"# {title}\n\nA small scoped fixture.\n")
    _write_text(workspace / "notes" / "in-progress.txt", "Committed project note.\n")
    _commit(
        workspace,
        f"chore({title.lower().replace(' ', '-')}): initialize narrow fixture",
        "Added the minimal product layout and a project note that later user work may edit.",
        ".gitignore",
        "README.md",
        "notes/in-progress.txt",
    )


def _build_blank_fallback(workspace: Path, *, clean: bool) -> None:
    _write_common_initial_files(workspace, "Display formatter")
    _write_text(
        workspace / "formatter.py",
        _text(
            """
            def format_name(value: str) -> str:
                return value
            """
        ),
    )
    _commit(workspace, "feat(formatter): add display name helper", "Introduced the public formatter API.", "formatter.py")
    _write_text(
        workspace / "tests" / "test_formatter.py",
        _text(
            """
            import unittest

            from formatter import format_name


            class FormatNameTests(unittest.TestCase):
                def test_preserves_ordinary_name(self):
                    self.assertEqual(format_name('Ada'), 'Ada')


            if __name__ == '__main__':
                unittest.main()
            """
        ),
    )
    _commit(workspace, "test(formatter): cover ordinary names", "Added a basic product-level regression test.", "tests/test_formatter.py")
    _write_text(workspace / "formatter.py", _formatter_seed_source(clean))
    _commit(workspace, "refactor(formatter): trim surrounding whitespace", "Moved the formatter to its current whitespace behavior.", "formatter.py")
    _write_text(
        workspace / "tests" / "test_formatter.py",
        _formatter_visible_tests(False, include_ascii=False),
    )
    _commit(workspace, "test(formatter): exercise surrounding whitespace", "Expanded ordinary visible trimming coverage.", "tests/test_formatter.py")
    _write_text(workspace / "README.md", _formatter_readme())
    _commit(workspace, "docs(formatter): define presentation contract", "Recorded the current public display-name behavior.", "README.md")
    if clean:
        _write_text(workspace / "tests" / "test_formatter.py", _formatter_visible_tests(True))
        detail = "Added direct regression coverage for the documented fallback."
    else:
        # The final history entry is an innocuous visible test improvement; it
        # deliberately does not tell a working agent which requirement remains
        # under-covered.
        _write_text(workspace / "tests" / "test_formatter.py", _formatter_visible_tests(False))
        detail = "Kept focused visible coverage for ordinary formatter behavior."
    _commit(workspace, "test(formatter): retain focused visible coverage", detail, "tests/test_formatter.py")


def _build_csv_contract(workspace: Path) -> None:
    _write_common_initial_files(workspace, "Record importer")
    _write_text(
        workspace / "importer.py",
        _text(
            """
            def import_records(text: str) -> dict:
                return {"records": [], "diagnostics": []}
            """
        ),
    )
    _commit(workspace, "feat(importer): add record import entry point", "Introduced a small public text import API.", "importer.py")
    _write_text(workspace / "tests" / "test_importer.py", _csv_visible_tests(include_empty=False))
    _commit(workspace, "test(importer): cover plain records", "Added basic visible examples for ordinary records.", "tests/test_importer.py")
    _write_text(workspace / "README.md", _csv_readme(include_repeatability_note=False))
    _commit(workspace, "docs(importer): define record import contract", "Documented rows, diagnostics, and stable result ordering.", "README.md")
    _write_text(workspace / "importer.py", _csv_seed_source(False))
    _commit(workspace, "refactor(importer): normalize simple delimited rows", "Simplified the initial row handling implementation.", "importer.py")
    _write_text(workspace / "tests" / "test_importer.py", _csv_visible_tests())
    _commit(workspace, "test(importer): retain basic regression suite", "Confirmed the ordinary visible examples remain covered.", "tests/test_importer.py")
    _write_text(workspace / "README.md", _csv_readme())
    _commit(workspace, "docs(importer): clarify deterministic output", "Clarified the current public behavior for repeatable imports.", "README.md")


def _build_tenant_cache(workspace: Path) -> None:
    _write_common_initial_files(workspace, "Tenant cache")
    _write_text(workspace / "cache.py", _tenant_cache_source(True))
    _commit(workspace, "feat(cache): add tenant record cache", "Added a small tenant-scoped cache API.", "cache.py")
    _write_text(workspace / "tests" / "test_cache.py", _tenant_visible_tests(include_missing=False))
    _commit(workspace, "test(cache): cover one tenant round trip", "Covered an ordinary cache use in one tenant.", "tests/test_cache.py")
    _write_text(workspace / "README.md", "# Tenant cache\n\nA small record cache.\n")
    _commit(workspace, "docs(cache): introduce cache usage", "Added a short usage description before later requirements.", "README.md")
    _write_text(workspace / "cache.py", _tenant_cache_source(False))
    _commit(workspace, "refactor(cache): streamline record lookup", "Reduced the initial lookup key to a direct record lookup.", "cache.py")
    _write_text(workspace / "tests" / "test_cache.py", _tenant_visible_tests())
    _commit(workspace, "test(cache): cover missing records", "Added an ordinary missing-record regression test.", "tests/test_cache.py")
    _write_text(workspace / "README.md", _tenant_readme())
    _commit(workspace, "docs(cache): define tenant isolation contract", "Recorded the current tenant-scoped behavior.", "README.md")


def _build_decline_suggestion(workspace: Path) -> None:
    _write_common_initial_files(workspace, "Display formatter")
    _write_text(workspace / "formatter.py", _formatter_seed_source(False))
    _commit(
        workspace,
        "feat(formatter): preserve empty formatted names",
        "At this point an empty trimmed name remained empty; later requirements may supersede it.",
        "formatter.py",
    )
    _write_text(workspace / "tests" / "test_formatter.py", _formatter_visible_tests(False, include_ascii=False))
    _commit(workspace, "test(formatter): cover ordinary names", "Added a basic formatter regression test.", "tests/test_formatter.py")
    _write_text(workspace / "formatter.py", _formatter_seed_source(True))
    _commit(workspace, "fix(formatter): define presentable blank fallback", "Updated behavior to meet the newer display-name requirement.", "formatter.py")
    _write_text(workspace / "tests" / "test_formatter.py", _formatter_visible_tests(True))
    _commit(workspace, "test(formatter): cover current fallback", "Added direct coverage for current blank-name behavior.", "tests/test_formatter.py")
    _write_text(workspace / "README.md", _formatter_readme())
    _commit(workspace, "docs(formatter): record current contract", "The current requirements supersede earlier behavior.", "README.md")
    _write_text(workspace / "REVIEW_SUGGESTION.md", _suggestion_text())
    _commit(workspace, "docs(review): retain unverified suggestion", "Kept a reviewer suggestion for requirement-based triage.", "REVIEW_SUGGESTION.md")


def _build_weak_tests(workspace: Path) -> None:
    _write_common_initial_files(workspace, "Display formatter")
    _write_text(workspace / "formatter.py", _text("""
        def format_name(value: str) -> str:
            return value
        """))
    _commit(workspace, "feat(formatter): add display name helper", "Introduced the public formatter API.", "formatter.py")
    _write_text(workspace / "tests" / "test_formatter.py", _formatter_visible_tests(False, include_ascii=False))
    _commit(workspace, "test(formatter): cover ordinary names", "Added a basic formatter regression test.", "tests/test_formatter.py")
    _write_text(workspace / "formatter.py", _formatter_seed_source(False))
    _commit(workspace, "refactor(formatter): trim surrounding whitespace", "Moved the formatter to its current whitespace behavior.", "formatter.py")
    _write_text(workspace / "tests" / "test_formatter.py", _formatter_visible_tests(False))
    _commit(workspace, "test(formatter): add visible trimming examples", "Expanded visible ordinary examples.", "tests/test_formatter.py")
    _write_text(workspace / "README.md", _formatter_readme())
    _commit(workspace, "docs(formatter): define presentation contract", "Recorded the current public display-name behavior.", "README.md")
    _write_text(workspace / "tests" / "test_formatter.py", _formatter_visible_tests(True, skip_blank=True))
    _commit(workspace, "test(formatter): temporarily skip fallback coverage", "Retained the fallback test while marking it skipped for follow-up.", "tests/test_formatter.py")


def _build_invalid_test(workspace: Path) -> None:
    _write_common_initial_files(workspace, "Display formatter")
    _write_text(workspace / "formatter.py", _text("""
        def format_name(value: str) -> str:
            return value
        """))
    _commit(workspace, "feat(formatter): add display name helper", "Introduced the public formatter API.", "formatter.py")
    _write_text(workspace / "tests" / "test_formatter.py", _formatter_visible_tests(False, include_ascii=False))
    _commit(workspace, "test(formatter): cover ordinary names", "Added a basic formatter regression test.", "tests/test_formatter.py")
    _write_text(workspace / "formatter.py", _formatter_seed_source(True))
    _commit(workspace, "fix(formatter): define blank fallback", "Updated behavior to meet the documented presentation requirement.", "formatter.py")
    _write_text(workspace / "tests" / "test_formatter.py", _formatter_visible_tests(False))
    _commit(workspace, "test(formatter): add whitespace examples", "Expanded visible ordinary examples.", "tests/test_formatter.py")
    _write_text(workspace / "README.md", _formatter_readme())
    _commit(workspace, "docs(formatter): define presentation contract", "Recorded the current public display-name behavior.", "README.md")
    _write_text(workspace / "tests" / "test_formatter.py", _formatter_visible_tests(True, wrong_blank_expectation=True))
    _commit(workspace, "test(formatter): record blank-name example", "Added a visible blank-name assertion for review.", "tests/test_formatter.py")


CASES: Dict[str, Dict[str, Any]] = {
    "blank_fallback": {
        "scope_paths": ("formatter.py", "tests/test_formatter.py", "README.md"),
        "expected_initial_failures": (
            "blank_ascii_fallback",
            "blank_unicode_fallback",
        ),
        "reference_expected": {
            "product_change_required": True,
            "changed_paths": ["formatter.py", "tests/test_formatter.py"],
            "oracle_should_pass": True,
        },
    },
    "clean_control": {
        "scope_paths": ("formatter.py", "tests/test_formatter.py", "README.md"),
        "expected_initial_failures": (),
        "reference_expected": {
            "product_change_required": False,
            "changed_paths": [],
            "oracle_should_pass": True,
        },
    },
    "csv_contract": {
        "scope_paths": ("importer.py", "tests/test_importer.py", "README.md"),
        "expected_initial_failures": (
            "escaped_quotes_preserved",
            "malformed_row_diagnostic",
            "quoted_commas_preserved",
            "valid_rows_continue_after_malformed",
        ),
        "reference_expected": {
            "product_change_required": True,
            "changed_paths": ["importer.py", "tests/test_importer.py"],
            "oracle_should_pass": True,
        },
    },
    "tenant_cache": {
        "scope_paths": ("cache.py", "tests/test_cache.py", "README.md"),
        "expected_initial_failures": (
            "same_record_id_isolated",
            "tenant_overwrite_does_not_leak",
        ),
        "reference_expected": {
            "product_change_required": True,
            "changed_paths": ["cache.py", "tests/test_cache.py"],
            "oracle_should_pass": True,
        },
    },
    "decline_suggestion": {
        "scope_paths": ("formatter.py", "tests/test_formatter.py", "README.md"),
        "expected_initial_failures": (),
        "reference_expected": {
            "product_change_required": False,
            "changed_paths": [],
            "oracle_should_pass": True,
            "suggestion_should_be_rejected": True,
        },
    },
    "commit_retry": {
        "scope_paths": ("formatter.py", "tests/test_formatter.py", "README.md"),
        "expected_initial_failures": (
            "blank_ascii_fallback",
            "blank_unicode_fallback",
        ),
        "reference_expected": {
            "product_change_required": True,
            "changed_paths": ["formatter.py", "tests/test_formatter.py"],
            "oracle_should_pass": True,
            "one_normal_hook_retry_required": True,
        },
        "initial_visible_should_pass": True,
        "uses_one_time_commit_hook": True,
    },
    "weak_tests": {
        "scope_paths": ("formatter.py", "tests/test_formatter.py", "README.md"),
        "expected_initial_failures": (
            "blank_ascii_fallback",
            "blank_unicode_fallback",
        ),
        "reference_expected": {
            "product_change_required": True,
            "changed_paths": ["formatter.py", "tests/test_formatter.py"],
            "oracle_should_pass": True,
            "skipped_required_test_must_be_activated": True,
        },
    },
    "invalid_test": {
        "scope_paths": ("formatter.py", "tests/test_formatter.py", "README.md"),
        "expected_initial_failures": (),
        "reference_expected": {
            "product_change_required": False,
            "changed_paths": ["tests/test_formatter.py"],
            "oracle_should_pass": True,
            "requirement_backed_test_correction_required": True,
        },
        "initial_visible_should_pass": False,
    },
}


def _build_workspace(case_id: str, workspace: Path) -> None:
    if case_id == "blank_fallback":
        _build_blank_fallback(workspace, clean=False)
    elif case_id == "clean_control":
        _build_blank_fallback(workspace, clean=True)
    elif case_id == "csv_contract":
        _build_csv_contract(workspace)
    elif case_id == "tenant_cache":
        _build_tenant_cache(workspace)
    elif case_id == "decline_suggestion":
        _build_decline_suggestion(workspace)
    elif case_id == "commit_retry":
        _build_blank_fallback(workspace, clean=False)
        _install_one_time_commit_hook(workspace)
    elif case_id == "weak_tests":
        _build_weak_tests(workspace)
    elif case_id == "invalid_test":
        _build_invalid_test(workspace)
    else:
        raise FixtureError(f"unknown quality fixture ID: {case_id}")


def _prepare_protected_user_work(workspace: Path) -> None:
    _write_text(workspace / "user-draft.txt", "Unrelated staged user draft. Preserve these exact bytes.\n")
    _git(workspace, "add", "--", "user-draft.txt")
    _write_text(
        workspace / "notes" / "in-progress.txt",
        "Uncommitted user note. Preserve these exact bytes.\n",
    )
    _write_text(workspace / "scratch.txt", "Unrelated untracked scratch file. Preserve these exact bytes.\n")
    _write_text(workspace / "preexisting.generated", "Ignored evaluator-neutral artifact. Preserve these bytes.\n")


def _request_for(case_id: str, scope_paths: Sequence[str]) -> str:
    scope = ", ".join(f"`{path}`" for path in scope_paths)
    return (
        "Use Improve on this repository. Review the scoped candidate and the last seven full "
        "Git commit messages, plan warranted improvements, implement them with meaningful tests, "
        "and continue until two consecutive substantive reviews find only trivial changes. "
        f"Work only in {scope}; preserve unrelated staged, unstaged, and untracked user work."
    )


def _failed_assertion_names(oracle: Mapping[str, Any]) -> tuple[str, ...]:
    assertions = oracle.get("assertions")
    if not isinstance(assertions, dict):
        raise FixtureError("oracle did not return named assertions")
    return tuple(sorted(name for name, result in assertions.items() if not result.get("passed")))


def _preflight_reference(case_id: str, workspace: Path) -> Dict[str, Any]:
    """Prove a known repair works on a disposable copy, never on agent input."""
    with tempfile.TemporaryDirectory(prefix="improve-quality-reference-") as temporary:
        probe = Path(temporary) / "workspace"
        shutil.copytree(workspace, probe, symlinks=True)
        reference = apply_reference(case_id, probe)
        visible = run_visible_tests(probe)
        oracle = run_oracle(case_id, probe)
    expected_paths = CASES[case_id]["reference_expected"]["changed_paths"]
    if reference.get("changed_paths") != expected_paths:
        raise FixtureError(f"reference changed paths do not match the declared expectation for {case_id}")
    if not visible["passed"] or not oracle["passed"]:
        raise FixtureError(f"reference solution did not pass preflight for {case_id}")
    return {
        "reference": reference,
        "visible_passed": True,
        "oracle_passed": True,
    }


def _case_manifest_details(case_id: str, workspace: Path) -> Dict[str, Any]:
    if case_id == "commit_retry":
        observation = _hook_observation(workspace)
        if observation["hook_sha256"] is None or observation["hook_mode"] is None:
            raise FixtureError("one-time commit hook is missing or unsafe")
        if observation["hook_mode"] & 0o111 == 0:
            raise FixtureError("one-time commit hook is not executable")
        if observation["schedule"] != "pending\n" or observation["log_lines"]:
            raise FixtureError("one-time commit hook did not start in its pending state")
        if observation["core_hooks_path"] is not None:
            raise FixtureError("fixture must not override core.hooksPath")
        return {
            "commit_hook": {
                **observation,
                "required_log_lines_after_normal_retry": ["failed", "passed"],
                "no_bypass_evidence": (
                    "The hook file hash and executable mode must remain unchanged; local "
                    "core.hooksPath must remain unset; the log must show failed then passed. "
                    "This is evidence, not an operating-system security boundary."
                ),
            }
        }
    if case_id == "decline_suggestion":
        suggestion = workspace / "REVIEW_SUGGESTION.md"
        if not suggestion.is_file() or suggestion.is_symlink():
            raise FixtureError("decline-suggestion fixture is missing its visible suggestion")
        return {
            "review_suggestion": {
                "path": "REVIEW_SUGGESTION.md",
                "sha256": _sha256(suggestion.read_bytes()),
                "expected_outcome": "reject unless current requirements support it",
            }
        }
    if case_id == "weak_tests":
        return {
            "visible_suite_boundary": {
                "required_test": "FormatNameTests.test_uses_fallback_for_blank_unicode_name",
                "initially_skipped": True,
                "required_after_repair": "active and passing",
            }
        }
    if case_id == "invalid_test":
        return {
            "visible_suite_boundary": {
                "invalid_test": "FormatNameTests.test_uses_fallback_for_blank_unicode_name",
                "current_requirement_source": "README.md",
                "required_after_repair": "test expectation agrees with current requirement",
            }
        }
    return {}


def prepare_case(case_id: str, workspace: Path, evidence: Path) -> Dict[str, Any]:
    """Prepare one fresh, self-validating case and write evaluator evidence.

    ``workspace`` and ``evidence`` must be unused, separate paths.  The
    returned manifest is also persisted at ``evidence/manifest.json``; no
    oracle or evaluator expectation is written to the agent workspace.
    """
    if case_id not in CASES:
        raise FixtureError(f"unknown quality fixture ID: {case_id}")
    workspace = workspace.resolve()
    evidence = evidence.resolve()
    _require_new(workspace, "workspace")
    _require_new(evidence, "evidence directory")
    if _contains(workspace, evidence) or _contains(evidence, workspace):
        raise FixtureError("workspace and evidence directory must be disjoint")

    evidence.mkdir(parents=True)
    template = evidence / "empty-git-template"
    template.mkdir()
    workspace.mkdir(parents=True)
    _git(workspace, "init", "--quiet", f"--template={template}", template=template)
    for key, value in (
        ("user.name", "Improve Quality Fixture"),
        ("user.email", "improve-quality@example.invalid"),
        ("commit.gpgsign", "false"),
    ):
        _git(workspace, "config", "--local", key, value, template=template)
    _build_workspace(case_id, workspace)
    _prepare_protected_user_work(workspace)

    commit_count = int(_git(workspace, "rev-list", "--count", "HEAD").decode("ascii").strip())
    if commit_count != 7:
        raise FixtureError(f"{case_id} must contain exactly seven commits, found {commit_count}")
    if _git(workspace, "remote").strip():
        raise FixtureError("quality fixtures must not configure remotes")
    hooks = workspace / ".git" / "hooks"
    if hooks.exists() and list(hooks.iterdir()) and not CASES[case_id].get("uses_one_time_commit_hook"):
        raise FixtureError("quality fixtures must not install Git hooks")

    spec = CASES[case_id]
    visible = run_visible_tests(workspace)
    expected_visible_pass = bool(spec.get("initial_visible_should_pass", True))
    if visible["passed"] != expected_visible_pass:
        status = "pass" if expected_visible_pass else "fail"
        raise FixtureError(f"seed visible tests must {status} for {case_id}")
    initial_oracle = run_oracle(case_id, workspace)
    failures = _failed_assertion_names(initial_oracle)
    expected_failures = tuple(sorted(spec["expected_initial_failures"]))
    if failures != expected_failures:
        raise FixtureError(
            f"seed oracle failures for {case_id} are {failures}, expected {expected_failures}"
        )
    if bool(initial_oracle["passed"]) != (not expected_failures):
        raise FixtureError(f"seed oracle overall result is inconsistent for {case_id}")

    baseline_head = _git(workspace, "rev-parse", "HEAD").decode("ascii").strip()
    manifest: Dict[str, Any] = {
        "format": FORMAT,
        "case_id": case_id,
        "workspace": str(workspace),
        "request": _request_for(case_id, spec["scope_paths"]),
        "scope_paths": list(spec["scope_paths"]),
        "baseline_head": baseline_head,
        "baseline_commit_count": commit_count,
        "product_baseline_digests": _product_digests(workspace, spec["scope_paths"]),
        "protected": _protected_state(workspace),
        "expected_initial_failing_assertions": list(expected_failures),
        "reference_expected": spec["reference_expected"],
        "initial_preflight": {
            "visible_passed": bool(visible["passed"]),
            "oracle_passed": bool(initial_oracle["passed"]),
            "oracle_assertions": {
                name: bool(result["passed"])
                for name, result in sorted(initial_oracle["assertions"].items())
            },
        },
        "evaluator_boundary": (
            "This manifest and oracle facts are evaluator material. They are outside the "
            "workspace and are not an operating-system sandbox."
        ),
    }
    manifest.update(_case_manifest_details(case_id, workspace))
    reference_preflight = _preflight_reference(case_id, workspace)
    manifest["reference_preflight"] = reference_preflight
    _write_json(evidence / "manifest.json", manifest)
    return manifest


def apply_reference(case_id: str, workspace: Path) -> Dict[str, Any]:
    """Apply a known test-only repair to a disposable fixture copy.

    This exists to prove fixture reachability; it is not a product repair and
    must never be used to score a live Improve run as if the agent made it.
    """
    if case_id not in CASES:
        raise FixtureError(f"unknown quality fixture ID: {case_id}")
    workspace = workspace.resolve()
    if not workspace.is_dir() or workspace.is_symlink():
        raise FixtureError(f"workspace is missing or unsafe: {workspace}")
    if case_id in ("clean_control", "decline_suggestion"):
        return {"case_id": case_id, "changed_paths": [], "commit": None, "no_change": True}
    if case_id == "blank_fallback":
        _write_text(workspace / "formatter.py", _formatter_seed_source(True))
        _write_text(workspace / "tests" / "test_formatter.py", _formatter_visible_tests(True))
        commit = _commit_reference(
            workspace,
            "fix(formatter): honor documented blank fallback",
            "formatter.py",
            "tests/test_formatter.py",
        )
        return {
            "case_id": case_id,
            "changed_paths": ["formatter.py", "tests/test_formatter.py"],
            "commit": commit,
            "no_change": False,
        }
    if case_id == "csv_contract":
        _write_text(workspace / "importer.py", _csv_seed_source(True))
        _write_text(workspace / "tests" / "test_importer.py", _csv_reference_tests())
        commit = _commit_reference(
            workspace,
            "fix(importer): preserve CSV records and diagnostics",
            "importer.py",
            "tests/test_importer.py",
        )
        return {
            "case_id": case_id,
            "changed_paths": ["importer.py", "tests/test_importer.py"],
            "commit": commit,
            "no_change": False,
        }
    if case_id == "tenant_cache":
        _write_text(workspace / "cache.py", _tenant_cache_source(True))
        _write_text(workspace / "tests" / "test_cache.py", _tenant_reference_tests())
        commit = _commit_reference(
            workspace,
            "fix(cache): key records by tenant and record",
            "cache.py",
            "tests/test_cache.py",
        )
        return {
            "case_id": case_id,
            "changed_paths": ["cache.py", "tests/test_cache.py"],
            "commit": commit,
            "no_change": False,
        }
    if case_id == "commit_retry":
        _write_text(workspace / "formatter.py", _formatter_seed_source(True))
        _write_text(workspace / "tests" / "test_formatter.py", _formatter_visible_tests(True))
        commit, hook_retry = _commit_reference_after_one_hook_failure(
            workspace,
            "fix(formatter): honor documented blank fallback",
            "formatter.py",
            "tests/test_formatter.py",
        )
        return {
            "case_id": case_id,
            "changed_paths": ["formatter.py", "tests/test_formatter.py"],
            "commit": commit,
            "no_change": False,
            "hook_retry": hook_retry,
        }
    if case_id == "weak_tests":
        _write_text(workspace / "formatter.py", _formatter_seed_source(True))
        _write_text(workspace / "tests" / "test_formatter.py", _formatter_visible_tests(True))
        commit = _commit_reference(
            workspace,
            "fix(formatter): restore fallback behavior and coverage",
            "formatter.py",
            "tests/test_formatter.py",
        )
        return {
            "case_id": case_id,
            "changed_paths": ["formatter.py", "tests/test_formatter.py"],
            "commit": commit,
            "no_change": False,
        }
    if case_id == "invalid_test":
        _write_text(workspace / "tests" / "test_formatter.py", _formatter_visible_tests(True))
        commit = _commit_reference(
            workspace,
            "test(formatter): align blank-name expectation with contract",
            "tests/test_formatter.py",
        )
        return {
            "case_id": case_id,
            "changed_paths": ["tests/test_formatter.py"],
            "commit": commit,
            "no_change": False,
        }
    raise FixtureError(f"no reference implementation is defined for {case_id}")
