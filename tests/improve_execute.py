#!/usr/bin/env python3
"""Prepare and check one fresh-agent execution of the candidate improve skill.

The tool never invokes a model.  ``prepare`` makes a new synthetic repository
and saves its oracle and preservation facts outside that repository; a caller
then gives the workspace to one fresh agent.  ``check`` is deliberately
mechanical and retains a JSON report even when the execution is incomplete.

Examples:
  python3 tests/improve_execute.py prepare --evidence-dir /tmp/improve-evidence --workspace /tmp/improve-workspace
  python3 tests/improve_execute.py check --evidence-dir /tmp/improve-evidence --workspace /tmp/improve-workspace
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence


FORMAT = "until-loop-improve-execution-fixture/v1"
UNITTEST_RESULT_FORMAT = "until-loop-unittest-result/v1"
UNITTEST_RESULT_LAUNCHER = Path(__file__).with_name("unittest_result_launcher.py")
FIXTURE_TEST_TIMEOUT_SECONDS = 30
MAX_UNITTEST_RESULT_BYTES = 16 * 1024
SCOPED = ("formatter.py", "test_formatter.py", "README.md")
SECTIONS = ("Review:", "Plan:", "Changes:", "Validation:", "Key learnings:", "Remaining work:")
CLASSIFICATION_RE = re.compile(r"\b(?:material|trivial)\b", re.IGNORECASE)
NUMBER_WORD_RE = r"(?:zero|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|thirteen|fourteen|fifteen|sixteen|seventeen|eighteen|nineteen|twenty)"
# This is a bounded English presence heuristic, not a natural-language
# convergence parser or a scripted review counter.
STREAK_VALUE_RE = re.compile(
    rf"(?:\b(?:clean[- ]review\s+)?streak\b[\s\S]{{0,96}}?\b(?:\d+|{NUMBER_WORD_RE}|none)\b|\b(?:\d+|{NUMBER_WORD_RE})\b[\s\S]{{0,96}}?\b(?:clean[- ]review\s+)?streak\b|\bno\b[\s\S]{{0,96}}?\bstreak\b)",
    re.IGNORECASE,
)
ORACLE = """import importlib.util
import sys
from pathlib import Path

workspace = Path(sys.argv[1]).resolve()
source = workspace / 'formatter.py'
spec = importlib.util.spec_from_file_location('_fixture_formatter_oracle', source)
module = importlib.util.module_from_spec(spec)
assert spec and spec.loader
spec.loader.exec_module(module)
assert module.format_name('\\u2003 Ada \\u2003') == 'Ada'
assert module.format_name('\\u2003\\t\\u00a0') == 'Anonymous'
assert module.format_name('Ada') == 'Ada'
"""


class FixtureError(RuntimeError):
    pass


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def write_bytes(path: Path, value: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(value)


def write_json(path: Path, value: Any) -> None:
    write_bytes(path, (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8"))


def digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def require_new(path: Path, label: str) -> None:
    if path.exists() or path.is_symlink():
        raise FixtureError(f"{label} must be new: {path}")


def contains(parent: Path, child: Path) -> bool:
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False


def git_environment(template: Path | None = None) -> dict[str, str]:
    blocked = {"GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_TEMPLATE_DIR"}
    environment = {
        key: value for key, value in os.environ.items()
        if key not in blocked and not key.startswith("GIT_CONFIG_")
    }
    environment.update({
        "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_TERMINAL_PROMPT": "0", "LC_ALL": "C", "LANG": "C",
    })
    if template is not None:
        environment["GIT_TEMPLATE_DIR"] = str(template)
    return environment


def git_result(workspace: Path, *arguments: str, template: Path | None = None) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        ["git", *arguments], cwd=workspace, env=git_environment(template),
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False, timeout=30,
    )


def git(workspace: Path, *arguments: str, template: Path | None = None) -> bytes:
    result = git_result(workspace, *arguments, template=template)
    if result.returncode:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise FixtureError(f"git {' '.join(arguments)} failed ({result.returncode}): {detail}")
    return result.stdout


def snapshot_record(path: Path) -> dict[str, Any]:
    """Describe one inventory entry without following a symlink."""
    item = path.lstat()
    if stat.S_ISLNK(item.st_mode):
        return {"kind": "symlink", "target": os.readlink(path)}
    if stat.S_ISREG(item.st_mode):
        raw = path.read_bytes()
        return {"kind": "file", "bytes": len(raw), "sha256": digest(raw)}
    if stat.S_ISDIR(item.st_mode):
        return {"kind": "directory"}
    return {"kind": "special", "mode": stat.S_IFMT(item.st_mode)}


def snapshot(workspace: Path) -> dict[str, dict[str, Any]]:
    """Inventory every workspace entry except Git's private directory.

    This deliberately includes ignored files and directory symlinks.  A Git
    status view cannot see those artifacts, yet they can be left behind by a
    run and affect a later agent's environment.
    """
    records: dict[str, dict[str, Any]] = {}
    for current, directories, files in os.walk(workspace, topdown=True, followlinks=False):
        current_path = Path(current)
        directories.sort()
        files.sort()
        if current_path == workspace:
            directories[:] = [name for name in directories if name != ".git"]
        for name in list(directories):
            path = current_path / name
            relative = str(path.relative_to(workspace))
            record = snapshot_record(path)
            records[relative] = record
            if record["kind"] == "symlink":
                # os.walk normally does not follow this with followlinks=False,
                # but removing it makes that safety property explicit.
                directories.remove(name)
        for name in files:
            path = current_path / name
            records[str(path.relative_to(workspace))] = snapshot_record(path)
    return records


def commit(workspace: Path, subject: str, *paths: str) -> None:
    git(workspace, "add", "--", *paths)
    git(workspace, "commit", "--quiet", "--no-gpg-sign", "--no-verify", "-m", subject)


def run_oracle(evidence: Path, workspace: Path) -> dict[str, Any]:
    result = subprocess.run(
        [sys.executable, str(evidence / "oracle.py"), str(workspace)], cwd=evidence,
        env={"PATH": os.defpath, "LC_ALL": "C", "LANG": "C", "PYTHONDONTWRITEBYTECODE": "1"},
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False, timeout=30,
    )
    return {"returncode": result.returncode, "stdout": result.stdout.decode("utf-8", "replace"),
            "stderr": result.stderr.decode("utf-8", "replace")}


def decode_process_output(value: bytes | str | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return value


def parse_unittest_result(raw: bytes) -> tuple[dict[str, Any] | None, str, str | None]:
    """Validate the launcher's one-record TestResult channel."""
    if not raw:
        return None, "missing", "structured unittest result was not written"
    if len(raw) > MAX_UNITTEST_RESULT_BYTES:
        return None, "malformed", "structured unittest result exceeded the size limit"
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        return None, "malformed", f"structured unittest result was not valid JSON: {error}"
    if not isinstance(payload, dict) or payload.get("format") != UNITTEST_RESULT_FORMAT:
        return None, "malformed", "structured unittest result had an unexpected format"
    if payload.get("complete") is not True:
        return None, "incomplete", "structured unittest result was incomplete"
    integer_fields = (
        "tests_ran", "failures", "errors", "skipped_tests",
        "expected_failures", "unexpected_successes",
    )
    for field in integer_fields:
        value = payload.get(field)
        if type(value) is not int or value < 0:
            return None, "incomplete", f"structured unittest result had an invalid {field} value"
    if payload["skipped_tests"] > payload["tests_ran"]:
        return None, "incomplete", "structured unittest result skipped more tests than it ran"
    if type(payload.get("successful")) is not bool:
        return None, "incomplete", "structured unittest result had an invalid successful value"
    computed_success = not (
        payload["failures"] or payload["errors"] or payload["unexpected_successes"]
    )
    if payload["successful"] != computed_success:
        return None, "incomplete", "structured unittest result had inconsistent success data"
    return payload, "complete", None


def run_fixture_tests(workspace: Path) -> dict[str, Any]:
    """Run the fixture suite and obtain TestResult facts outside its output streams."""
    command = [sys.executable, str(UNITTEST_RESULT_LAUNCHER), "--result-fd"]
    environment = {
        "PATH": os.defpath,
        "LC_ALL": "C",
        "LANG": "C",
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    record: dict[str, Any] = {"command": command, "cwd": str(workspace)}
    if not UNITTEST_RESULT_LAUNCHER.is_file():
        record.update({
            "returncode": None,
            "launch_error": f"trusted unittest launcher is missing: {UNITTEST_RESULT_LAUNCHER}",
            "stdout": "",
            "stderr": "",
            "result_complete": False,
            "result_status": "missing",
            "result_error": "trusted unittest launcher is missing",
            "tests_ran": None,
            "failures": None,
            "errors": None,
            "skipped_tests": None,
            "expected_failures": None,
            "unexpected_successes": None,
            "successful": None,
            "executed_tests": None,
        })
        return record
    try:
        with tempfile.TemporaryFile() as result_channel:
            result_fd = result_channel.fileno()
            result = subprocess.run(
                [*command, str(result_fd)], cwd=workspace, env=environment,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
                timeout=FIXTURE_TEST_TIMEOUT_SECONDS, pass_fds=(result_fd,),
            )
            result_channel.seek(0)
            raw_result = result_channel.read(MAX_UNITTEST_RESULT_BYTES + 1)
    except subprocess.TimeoutExpired as error:
        record.update({
            "returncode": None,
            "timed_out": True,
            "stdout": decode_process_output(error.stdout),
            "stderr": decode_process_output(error.stderr),
            "result_complete": False,
            "result_status": "missing",
            "result_error": "structured unittest result was unavailable after timeout",
            "tests_ran": None,
            "failures": None,
            "errors": None,
            "skipped_tests": None,
            "expected_failures": None,
            "unexpected_successes": None,
            "successful": None,
            "executed_tests": None,
        })
        return record
    except (OSError, ValueError) as error:
        record.update({
            "returncode": None,
            "launch_error": str(error),
            "stdout": "",
            "stderr": "",
            "result_complete": False,
            "result_status": "missing",
            "result_error": "structured unittest result was unavailable after launch failure",
            "tests_ran": None,
            "failures": None,
            "errors": None,
            "skipped_tests": None,
            "expected_failures": None,
            "unexpected_successes": None,
            "successful": None,
            "executed_tests": None,
        })
        return record
    stdout = decode_process_output(result.stdout)
    stderr = decode_process_output(result.stderr)
    structured, result_status, result_error = parse_unittest_result(raw_result)
    tests_ran = structured["tests_ran"] if structured is not None else None
    skipped_tests = structured["skipped_tests"] if structured is not None else None
    executed_tests = tests_ran - skipped_tests if tests_ran is not None else None
    record.update({
        "returncode": result.returncode,
        "stdout": stdout,
        "stderr": stderr,
        "result_complete": structured is not None,
        "result_status": result_status,
        "result_error": result_error,
        "tests_ran": tests_ran,
        "failures": structured["failures"] if structured is not None else None,
        "errors": structured["errors"] if structured is not None else None,
        "skipped_tests": skipped_tests,
        "expected_failures": structured["expected_failures"] if structured is not None else None,
        "unexpected_successes": structured["unexpected_successes"] if structured is not None else None,
        "successful": structured["successful"] if structured is not None else None,
        "executed_tests": executed_tests,
    })
    return record


def permitted_runtime_path(relative: str) -> bool:
    return relative == ".until-loop" or relative.startswith(".until-loop/")


def inventory_differences(
    baseline: dict[str, dict[str, Any]], current: dict[str, dict[str, Any]], *,
    legacy_without_directories: bool,
) -> dict[str, Any]:
    """Compare final entries to the prepared baseline without deleting anything."""
    allowed_scoped = set(SCOPED)
    unexpected = []
    modified = []
    missing = []
    for relative, record in current.items():
        if permitted_runtime_path(relative) or relative in allowed_scoped:
            continue
        if legacy_without_directories and record.get("kind") == "directory":
            continue
        before = baseline.get(relative)
        if before is None:
            unexpected.append(relative)
        elif before != record:
            modified.append(relative)
    for relative in baseline:
        if permitted_runtime_path(relative) or relative in allowed_scoped:
            continue
        if relative not in current:
            missing.append(relative)
    return {
        "baseline_records": len(baseline),
        "current_records": len(current),
        "legacy_baseline_without_directories": legacy_without_directories,
        "allowed_runtime_records": sorted(path for path in current if permitted_runtime_path(path)),
        "unexpected_records": sorted(unexpected),
        "modified_baseline_records": sorted(modified),
        "missing_baseline_records": sorted(missing),
    }


def commit_body(message: str) -> str:
    """Return the body portion of Git's full commit-message rendering."""
    _, separator, body = message.partition("\n")
    return body if separator else ""


def section_content(message: str, section: str) -> str | None:
    """Read flexible section content without imposing a new heading grammar."""
    start = message.find(section)
    if start < 0:
        return None
    content_start = start + len(section)
    ends = [
        message.find(other, content_start)
        for other in SECTIONS
        if other != section and message.find(other, content_start) >= 0
    ]
    end = min(ends) if ends else len(message)
    return message[content_start:end].strip()


def commit_message_checks(message: str) -> dict[str, Any]:
    """Check only the presence of durable handoff details in a commit body."""
    body = commit_body(message)
    missing_sections = [section for section in SECTIONS if section_content(body, section) is None]
    empty_sections = [section for section in SECTIONS if section_content(body, section) == ""]
    return {
        "missing_sections": missing_sections,
        "empty_sections": empty_sections,
        "classification_mentioned": bool(CLASSIFICATION_RE.search(body)),
        "streak_value_mentioned": bool(STREAK_VALUE_RE.search(body)),
    }


def prepare(evidence: Path, workspace: Path) -> Path:
    evidence, workspace = evidence.resolve(), workspace.resolve()
    require_new(evidence, "evidence directory")
    require_new(workspace, "workspace")
    if contains(evidence, workspace) or contains(workspace, evidence):
        raise FixtureError("evidence directory and workspace must be disjoint")
    evidence.mkdir(parents=True)
    template = evidence / "empty-git-template"
    template.mkdir()
    workspace.mkdir(parents=True)
    write_bytes(evidence / "oracle.py", ORACLE.encode("utf-8"))
    git(workspace, "init", "--quiet", f"--template={template}", template=template)
    for key, value in (("user.name", "Improve Execution Fixture"),
                       ("user.email", "improve-execution@example.invalid"),
                       ("commit.gpgsign", "false")):
        git(workspace, "config", "--local", key, value, template=template)

    write_bytes(workspace / ".gitignore", b"*.generated\n__pycache__/\n*.pyc\n")
    write_bytes(workspace / "formatter.py", b"def format_name(value: str) -> str:\n    return value\n")
    commit(workspace, "feat(formatter): add format_name helper", ".gitignore", "formatter.py")
    write_bytes(workspace / "test_formatter.py", b"import unittest\n\nfrom formatter import format_name\n\n\nclass FormatNameTests(unittest.TestCase):\n    def test_preserves_ordinary_name(self):\n        self.assertEqual(format_name('Ada'), 'Ada')\n\n\nif __name__ == '__main__':\n    unittest.main()\n")
    commit(workspace, "test(formatter): cover ordinary names", "test_formatter.py")
    write_bytes(workspace / "README.md", b"# format_name\n\nFormat a display name for presentation.\n")
    commit(workspace, "docs(formatter): introduce the helper", "README.md")
    write_bytes(workspace / "formatter.py", b"def format_name(value: str) -> str:\n    return value.strip()\n")
    commit(workspace, "refactor(formatter): trim surrounding whitespace", "formatter.py")
    write_bytes(workspace / "test_formatter.py", (workspace / "test_formatter.py").read_bytes().replace(
        b"    def test_preserves_ordinary_name", b"    def test_trims_unicode_whitespace(self):\n        self.assertEqual(format_name('\\u2003 Ada \\u2003'), 'Ada')\n\n    def test_preserves_ordinary_name"))
    commit(workspace, "test(formatter): exercise Unicode whitespace trimming", "test_formatter.py")
    write_bytes(workspace / "README.md", b"# format_name\n\n`format_name(value)` trims surrounding Unicode whitespace. If trimming leaves a blank value, it returns `Anonymous`. For example, `\\u2003 Ada \\u2003` becomes `Ada`.\n")
    commit(workspace, "docs(formatter): define blank fallback behavior", "README.md")
    # This final test-only evolution intentionally still has no blank fallback case.
    write_bytes(workspace / "test_formatter.py", (workspace / "test_formatter.py").read_bytes().replace(
        b"        self.assertEqual(format_name('Ada'), 'Ada')", b"        self.assertEqual(format_name('Ada'), 'Ada')\n\n    def test_trims_ascii_whitespace(self):\n        self.assertEqual(format_name(' Ada '), 'Ada')"))
    commit(workspace, "test(formatter): cover ASCII whitespace trimming", "test_formatter.py")

    tests = (workspace / "test_formatter.py").read_bytes()
    if b"Anonymous" in tests or b"blank" in tests.lower():
        raise FixtureError("fixture accidentally includes blank fallback coverage")
    readme = (workspace / "README.md").read_text(encoding="utf-8")
    if "Unicode whitespace" not in readme or "Anonymous" not in readme:
        raise FixtureError("fixture README is not the normative behavior contract")
    write_bytes(workspace / "user-draft.txt", b"Unrelated staged user draft. Preserve these exact bytes.\n")
    git(workspace, "add", "--", "user-draft.txt", template=template)
    write_bytes(workspace / "scratch.txt", b"Unrelated untracked scratch file. Preserve these exact bytes.\n")
    write_bytes(workspace / "preexisting.generated", b"Pre-existing ignored artifact. Preserve these exact bytes.\n")
    baseline = git(workspace, "rev-parse", "HEAD", template=template).decode().strip()
    count = int(git(workspace, "rev-list", "--count", "HEAD", template=template).decode().strip())
    if count != 7 or git(workspace, "remote", template=template) or list((workspace / ".git" / "hooks").glob("*")):
        raise FixtureError("fixture must have seven commits and no remotes or hooks")
    staged = git(workspace, "show", ":user-draft.txt", template=template)
    worktree = (workspace / "user-draft.txt").read_bytes()
    scratch = (workspace / "scratch.txt").read_bytes()
    write_bytes(evidence / "original-staged-user-draft.bin", staged)
    write_bytes(evidence / "original-worktree-user-draft.bin", worktree)
    write_bytes(evidence / "original-untracked-scratch.bin", scratch)
    write_bytes(evidence / "baseline-head.txt", (baseline + "\n").encode("ascii"))
    initial_oracle = run_oracle(evidence, workspace)
    if initial_oracle["returncode"] == 0:
        raise FixtureError("fixture no longer contains the deliberate blank fallback regression")
    write_json(evidence / "before.json", {"created_utc": utc_now(), "baseline_head": baseline,
        "git_status": git(workspace, "status", "--porcelain=v1", "--untracked-files=all", template=template).decode(),
        "worktree_inventory_version": 2, "worktree": snapshot(workspace), "initial_oracle": initial_oracle})
    facts = {"format": FORMAT, "workspace": str(workspace), "baseline_head": baseline,
        "baseline_commit_count": count, "scoped_files": list(SCOPED),
        "unrelated": {"staged": "user-draft.txt", "untracked": "scratch.txt",
            "staged_blob_sha256": digest(staged), "worktree_sha256": digest(worktree), "scratch_sha256": digest(scratch)},
        "preexisting_ignored": {"path": "preexisting.generated",
            "sha256": digest((workspace / "preexisting.generated").read_bytes())},
        "initial_fixture": {"blank_fallback_regression": True, "blank_regression_coverage": "missing"},
        "final_oracle": "Unicode whitespace trims; blank Unicode whitespace returns Anonymous.",
        "note": "Expected facts, oracle, baseline, snapshots, and original bytes are outside the agent workspace."}
    write_json(evidence / "expected.json", facts)
    return evidence / "expected.json"


def read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise FixtureError(f"cannot read {path}: {error}") from error


def changed_paths(workspace: Path, commit_id: str) -> list[str]:
    raw = git(workspace, "diff-tree", "--no-commit-id", "--name-only", "-r", "-m", "-z", commit_id)
    return [value.decode("utf-8", "surrogateescape") for value in raw.split(b"\0") if value]


def check(evidence: Path, workspace: Path) -> Path:
    evidence, workspace = evidence.resolve(), workspace.resolve()
    report: dict[str, Any] = {"format": FORMAT, "checked_utc": utc_now(), "workspace": str(workspace),
                               "no_model_calls": True, "errors": []}
    errors: list[str] = report["errors"]
    try:
        facts = read_json(evidence / "expected.json")
        if facts.get("format") != FORMAT or facts.get("workspace") != str(workspace):
            raise FixtureError("expected fixture facts do not bind this workspace")
        baseline = facts["baseline_head"]
        report["oracle"] = run_oracle(evidence, workspace)
        if report["oracle"]["returncode"] != 0:
            errors.append("independent oracle did not confirm final behavior")
        report["fixture_tests"] = run_fixture_tests(workspace)
        fixture_tests = report["fixture_tests"]
        if fixture_tests.get("timed_out"):
            errors.append("fixture project unittest suite timed out")
        elif not fixture_tests.get("result_complete"):
            errors.append("fixture project unittest suite did not provide a complete structured result")
        elif fixture_tests.get("tests_ran") == 0:
            errors.append("fixture project unittest suite did not run a meaningful test count")
        elif fixture_tests.get("executed_tests") == 0:
            errors.append("fixture project unittest suite did not execute a non-skipped test")
        elif fixture_tests.get("returncode") != 0 or fixture_tests.get("successful") is not True:
            errors.append("fixture project unittest suite did not pass")
        elif (not isinstance(fixture_tests.get("tests_ran"), int)
              or not isinstance(fixture_tests.get("executed_tests"), int)
              or fixture_tests["executed_tests"] < 1):
            errors.append("fixture project unittest suite did not run a meaningful test count")
        state_path = workspace / ".until-loop" / "state.json"
        state = read_json(state_path) if state_path.is_file() else None
        report["runtime_state"] = {"present": state is not None,
            "version": state.get("version") if isinstance(state, dict) else None,
            "phase": state.get("phase") if isinstance(state, dict) else None}
        if not isinstance(state, dict) or state.get("version") != 2 or state.get("phase") != "done":
            errors.append("runtime v2 state is absent or not done")
        history_path = workspace / ".until-loop" / "history.jsonl"
        history = [json.loads(line) for line in history_path.read_text(encoding="utf-8").splitlines() if line] if history_path.is_file() else []
        accepted = [entry for entry in history if isinstance(entry, dict) and entry.get("type") == "assessment"
                    and entry.get("decision") in ("continue", "complete") and isinstance(entry.get("assessment"), dict)]
        report["work_assessment_proxy"] = {"minimum_accepted_work_assessments": 3,
            "observed_accepted_work_assessments": len(accepted), "mechanical_proxy_only": True,
            "semantic_notebook_review_required": True,
            "semantic_boundary": "The count cannot establish that the work was a material fix followed by two full reviews; inspect retained notebook/transcript evidence."}
        if len(accepted) < 3:
            errors.append("fewer than three accepted work assessments")
        if git_result(workspace, "merge-base", "--is-ancestor", baseline, "HEAD").returncode:
            errors.append("baseline HEAD is not an ancestor of final HEAD")
        commits = git(workspace, "rev-list", "--reverse", f"{baseline}..HEAD").decode().splitlines()
        records = []
        report["commit_message_boundary"] = {
            "mechanical_presence_only": True,
            "semantic_notebook_review_required": True,
            "semantic_boundary": "Required sections, material-or-trivial language, and a streak value establish only message presence. They do not establish materiality, truth, or the correct clean-review streak; inspect retained notebook/transcript evidence.",
        }
        changed_scoped = False
        for commit_id in commits:
            paths = changed_paths(workspace, commit_id)
            message = git(workspace, "show", "-s", "--format=%B", commit_id).decode("utf-8", "replace")
            outside = [path for path in paths if path not in SCOPED]
            forbidden = [path for path in paths if path in ("user-draft.txt", "scratch.txt") or path == ".until-loop" or path.startswith(".until-loop/")]
            message_checks = commit_message_checks(message)
            sections = not message_checks["missing_sections"] and not message_checks["empty_sections"]
            scoped = bool(set(paths) & set(SCOPED))
            records.append({"id": commit_id, "paths": paths, "verbose_sections": sections,
                            "outside_scope_paths": outside, "forbidden_paths": forbidden,
                            **message_checks})
            changed_scoped = changed_scoped or (scoped and bool(paths))
            if outside:
                errors.append(f"commit {commit_id} includes paths outside the fixture scope: {outside}")
            if paths and not sections:
                errors.append(f"commit {commit_id} is missing required learning sections")
            if paths and not message_checks["classification_mentioned"]:
                errors.append(f"commit {commit_id} does not state a material or trivial classification")
            if paths and not message_checks["streak_value_mentioned"]:
                errors.append(f"commit {commit_id} does not state a clean-review streak value")
        report["new_commits"] = records
        if not changed_scoped:
            errors.append("no changed scoped commit was recorded after the fixture baseline")
        staged = git(workspace, "show", ":user-draft.txt")
        worktree = (workspace / "user-draft.txt").read_bytes() if (workspace / "user-draft.txt").is_file() else b""
        scratch = (workspace / "scratch.txt").read_bytes() if (workspace / "scratch.txt").is_file() else b""
        preservation = {"staged_blob_matches": staged == (evidence / "original-staged-user-draft.bin").read_bytes(),
            "worktree_bytes_match": worktree == (evidence / "original-worktree-user-draft.bin").read_bytes(),
            "scratch_bytes_match": scratch == (evidence / "original-untracked-scratch.bin").read_bytes(),
            "scratch_is_untracked": git_result(workspace, "ls-files", "--error-unmatch", "scratch.txt").returncode != 0}
        report["unrelated_preservation"] = preservation
        if not all(preservation.values()):
            errors.append("unrelated staged or untracked bytes were not preserved")
        dirty = git(workspace, "--no-optional-locks", "-c", "diff.autoRefreshIndex=false", "diff", "--name-only", "HEAD", "--", *SCOPED).decode().splitlines()
        report["unresolved_scoped_edits"] = dirty
        if dirty:
            errors.append(f"unresolved tracked scoped edits remain: {dirty}")
        before = read_json(evidence / "before.json")
        baseline_inventory = before.get("worktree")
        if not isinstance(baseline_inventory, dict):
            raise FixtureError("prepared workspace inventory is missing or invalid")
        # v1 evidence captured only files and symlinks.  It remains valid for
        # the pre-upgrade live scenarios; fresh fixtures declare inventory v2
        # and additionally audit directory entries.
        report["workspace_inventory"] = inventory_differences(
            baseline_inventory,
            snapshot(workspace),
            legacy_without_directories=before.get("worktree_inventory_version") != 2,
        )
        inventory = report["workspace_inventory"]
        if inventory["unexpected_records"]:
            errors.append(f"unexpected workspace artifacts outside scoped outputs and .until-loop: {inventory['unexpected_records']}")
        if inventory["modified_baseline_records"]:
            errors.append(f"pre-existing workspace artifacts were modified: {inventory['modified_baseline_records']}")
        if inventory["missing_baseline_records"]:
            errors.append(f"pre-existing workspace artifacts were removed: {inventory['missing_baseline_records']}")
    except (FixtureError, KeyError, OSError, ValueError, json.JSONDecodeError, subprocess.TimeoutExpired) as error:
        errors.append(str(error))
    report["passed"] = not errors
    reports = evidence / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    report_path = reports / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ-") + str(os.getpid()) + ".json")
    write_json(report_path, report)
    if errors:
        raise FixtureError(f"check failed; retained report: {report_path}: {'; '.join(errors)}")
    return report_path


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("prepare", "check"):
        command = commands.add_parser(name)
        command.add_argument("--evidence-dir", "--evidence", dest="evidence", required=True, type=Path)
        command.add_argument("--workspace", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        path = prepare(args.evidence, args.workspace) if args.command == "prepare" else check(args.evidence, args.workspace)
    except FixtureError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
