#!/usr/bin/env python3
"""Bounded, append-only orchestration for repeated Improve quality trials.

This is intentionally an opt-in launcher.  It freezes the harness identity in
its manifest, prepares every autonomous fixture before any host starts, and
never retries a trial root that has been launched once.
"""
from __future__ import annotations

import argparse
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
from typing import Any


FORMAT = "improve-quality-batch/v1"
AUTONOMOUS_CASES = (
    "blank_fallback", "clean_control", "csv_contract", "tenant_cache",
    "decline_suggestion", "commit_retry", "weak_tests", "invalid_test",
)
ATTEMPTS = "attempts.jsonl"
MANIFEST = "batch.json"
MANIFEST_FREEZE = "batch-freeze.json"
STOP_DISPATCH = "STOP_DISPATCH"
_append_lock = threading.Lock()


class BatchError(RuntimeError):
    pass


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


def tree_digest(root: Path) -> str:
    entries: list[tuple[str, str]] = []
    for path in sorted(root.rglob("*")):
        if ".git" in path.parts or "__pycache__" in path.parts:
            continue
        if path.is_symlink() or (not path.is_file() and not path.is_dir()):
            raise BatchError("frozen harness contains unsupported path: " + str(path))
        if path.is_dir():
            continue
        entries.append((str(path.relative_to(root)), hashlib.sha256(path.read_bytes()).hexdigest()))
    return hashlib.sha256(_canonical(entries)).hexdigest()


def _git_revision(repository: Path, revision: str) -> str:
    result = subprocess.run(["git", "-C", str(repository), "rev-parse", "--verify", revision + "^{commit}"],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False, timeout=30)
    if result.returncode:
        raise BatchError("cannot resolve pinned source revision: " + result.stderr.strip())
    return result.stdout.strip()


def _write_new_json(path: Path, value: Any) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump(value, handle, indent=2, sort_keys=True)
        handle.write("\n")


def _read_manifest(root: Path, expected_sha256: str | None = None) -> dict[str, Any]:
    path = root / MANIFEST
    try:
        if path.is_symlink() or (root / MANIFEST_FREEZE).is_symlink():
            raise BatchError("batch manifest or freeze record is unsafe")
        raw = path.read_bytes()
        if expected_sha256 is None:
            freeze = json.loads((root / MANIFEST_FREEZE).read_text(encoding="utf-8"))
            expected_sha256 = freeze.get("manifest_sha256") if isinstance(freeze, dict) else None
        if expected_sha256 != hashlib.sha256(raw).hexdigest():
            raise BatchError("batch manifest changed after preparation")
        value = json.loads(raw)
    except (OSError, ValueError) as error:
        raise BatchError("batch manifest is unreadable") from error
    if not isinstance(value, dict) or value.get("format") != FORMAT:
        raise BatchError("batch manifest has an unsupported format")
    if value.get("root") != str(root.resolve()):
        raise BatchError("batch manifest belongs to another root")
    entries = value.get("schedule")
    if not isinstance(entries, list) or not entries:
        raise BatchError("batch schedule is empty or invalid")
    seen = set()
    for ordinal, entry in enumerate(entries, 1):
        if not isinstance(entry, dict) or not isinstance(entry.get("id"), str) or entry["id"] in seen:
            raise BatchError("batch schedule contains an invalid or duplicate entry")
        seen.add(entry["id"])
        mode = entry.get("cohort")
        if mode not in ("autonomous", "controlled_resume") or entry.get("execution_mode") != mode:
            raise BatchError("batch schedule cohort is invalid")
        directory = "autonomous" if mode == "autonomous" else "controlled-resume"
        expected_root = directory + "/trial-%03d" % ordinal
        if entry.get("root") != expected_root or entry.get("relative_root") != expected_root:
            raise BatchError("batch schedule path is not its opaque trial root")
        if entry.get("case_id") not in AUTONOMOUS_CASES or (mode == "controlled_resume" and entry["case_id"] != "clean_control"):
            raise BatchError("batch schedule case is invalid")
        if type(entry.get("repeat")) is not int or not 1 <= entry["repeat"] <= 20:
            raise BatchError("batch schedule repetition is invalid")
    return value


def schedule(repetitions: int, controlled_repetitions: int) -> list[dict[str, Any]]:
    if type(repetitions) is not int or not 1 <= repetitions <= 20:
        raise ValueError("repetitions must be an integer from 1 through 20")
    if type(controlled_repetitions) is not int or not 0 <= controlled_repetitions <= 20:
        raise ValueError("controlled repetitions must be an integer from 0 through 20")
    result = []
    # Interleave case types in each repetition so an early defect does not
    # systematically suppress every later case in the same cohort.
    ordinal = 0
    for repeat in range(1, repetitions + 1):
        for case_id in AUTONOMOUS_CASES:
            ordinal += 1
            # Never put fixture semantics in the candidate's physical path:
            # both the package cwd and git status are visible to the working host.
            relative_root = "autonomous/trial-%03d" % ordinal
            result.append({"id": "autonomous-%s-%02d" % (case_id, repeat), "cohort": "autonomous",
                           "execution_mode": "autonomous", "case_id": case_id, "repeat": repeat,
                           # ``root`` is intentionally relative to the batch root,
                           # so the manifest remains portable while its root is fixed.
                           "root": relative_root, "relative_root": relative_root})
    for repeat in range(1, controlled_repetitions + 1):
        ordinal += 1
        relative_root = "controlled-resume/trial-%03d" % ordinal
        result.append({"id": "controlled-resume-%02d" % repeat, "cohort": "controlled_resume",
                       "execution_mode": "controlled_resume", "case_id": "clean_control", "repeat": repeat,
                       "root": relative_root, "relative_root": relative_root})
    return result


def command(argv: list[str], cwd: Path | None = None, *, timeout: int = 1800) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          text=True, check=False, timeout=timeout)


def prepare_trial(entry: dict[str, Any], manifest: dict[str, Any], python: str) -> None:
    root = Path(manifest["root"]) / entry["relative_root"]
    if root.exists():
        raise BatchError("prepared trial root already exists: " + str(root))
    harness = Path(manifest["harness"]["path"])
    source = manifest["source"]
    script = harness / "tests" / ("improve_quality_resume.py" if entry["cohort"] == "controlled_resume" else "improve_quality.py")
    argv = [python, str(script), "prepare", "--source-repo", source["repository"], "--revision", source["revision"], "--root", str(root)]
    if entry["cohort"] == "autonomous":
        argv.extend(["--case", entry["case_id"]])
    result = command(argv)
    if result.returncode:
        raise BatchError("fixture preparation failed for %s: %s" % (entry["id"], result.stderr.strip()))
    if not (root / "trial.json").is_file():
        raise BatchError("fixture preparation did not create trial manifest for " + entry["id"])


def prepare_batch(root: Path, harness: Path, source: Path, revision: str, *, repetitions: int = 3,
                  controlled_repetitions: int = 0, python: str = sys.executable) -> dict[str, Any]:
    root, harness, source = root.resolve(), harness.resolve(), source.resolve()
    if root.exists():
        raise BatchError("batch root must be new")
    if not (harness / "tests" / "improve_quality.py").is_file() or not (harness / "tests" / "improve_quality_resume.py").is_file():
        raise BatchError("harness does not contain the frozen quality launchers")
    resolved = _git_revision(source, revision)
    entries = schedule(repetitions, controlled_repetitions)
    root.mkdir(parents=True)
    manifest = {
        "format": FORMAT, "root": str(root), "created_at": time.time(),
        "harness": {"path": str(harness), "tree_sha256": tree_digest(harness)},
        "source": {"repository": str(source), "requested_revision": revision, "revision": resolved},
        "schedule": entries, "launch_policy": {
            "autonomous_invocations_per_trial": 1, "controlled_mode": "separate optional cohort",
            "stop_after": "first fail or incomplete verdict", "resume": "never relaunch an attempted root",
        },
    }
    _write_new_json(root / MANIFEST, manifest)
    _write_new_json(root / MANIFEST_FREEZE, {
        "format": "improve-quality-batch-freeze/v1",
        "manifest_sha256": hashlib.sha256((root / MANIFEST).read_bytes()).hexdigest(),
    })
    (root / ATTEMPTS).touch(exist_ok=False)
    try:
        for entry in entries:
            # The controlled wrapper's public ``run-all`` owns its own prepare
            # step; pre-creating its root would make that command unsafe.
            if entry["cohort"] == "autonomous":
                prepare_trial(entry, manifest, python)
    except Exception:
        # The append-only manifest records that preparation was attempted; keep
        # all created fixtures for inspection rather than deleting them.
        raise
    return manifest


def _attempts(root: Path) -> list[dict[str, Any]]:
    path = root / ATTEMPTS
    if not path.is_file() or path.is_symlink():
        raise BatchError("append-only attempt log is missing or unsafe")
    result = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        try:
            value = json.loads(line)
        except ValueError as error:
            raise BatchError("attempt log contains invalid JSON at line %d" % number) from error
        if not isinstance(value, dict) or not isinstance(value.get("id"), str):
            raise BatchError("attempt log contains invalid record at line %d" % number)
        result.append(value)
    return result


def _append(root: Path, value: dict[str, Any]) -> None:
    payload = json.dumps(value, sort_keys=True) + "\n"
    with _append_lock:
        descriptor = os.open(root / ATTEMPTS, os.O_WRONLY | os.O_APPEND)
        with os.fdopen(descriptor, "a", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())


def _attempt_state(entries: list[dict[str, Any]], records: list[dict[str, Any]]) -> tuple[set[str], set[str]]:
    """Validate append-only history and return (launched, unfinished) IDs."""
    expected = {entry["id"] for entry in entries}
    state: dict[str, str] = {}
    for record in records:
        trial_id, kind = record["id"], record.get("kind")
        if trial_id not in expected:
            raise BatchError("attempt log refers to trial outside this manifest: " + trial_id)
        if kind == "launched":
            if trial_id in state:
                raise BatchError("attempt log relaunches a trial: " + trial_id)
            state[trial_id] = "launched"
        elif kind == "finished":
            if state.get(trial_id) != "launched":
                raise BatchError("attempt log finishes a trial that was not launched: " + trial_id)
            if record.get("status") not in ("pass", "fail", "incomplete"):
                raise BatchError("attempt log has invalid terminal status for " + trial_id)
            state[trial_id] = "finished"
        else:
            raise BatchError("attempt log has unsupported record kind for " + trial_id)
    launched = set(state)
    return launched, {trial_id for trial_id, value in state.items() if value == "launched"}


def _assert_prepared(root: Path, entries: list[dict[str, Any]]) -> None:
    """Refuse a partially prepared autonomous cohort before starting a host."""
    missing = [entry["id"] for entry in entries if entry["cohort"] == "autonomous"
               and not (root / entry["relative_root"] / "trial.json").is_file()]
    if missing:
        raise BatchError("autonomous fixtures are not completely prepared: " + ", ".join(missing))


def _dispatch_stop_requested(root: Path) -> bool:
    """An operator-owned durable marker that prevents only new submissions."""
    return (root / STOP_DISPATCH).exists()


def _grade_status(root: Path, entry: dict[str, Any]) -> str:
    trial = root / entry["relative_root"]
    evidence = (trial / "controlled-resume-combined" / "evidence") if entry["cohort"] == "controlled_resume" else (trial / "evidence")
    selected = evidence / "audit-selection.json"
    if evidence.is_symlink() or selected.is_symlink() or not selected.is_file():
        return "incomplete"
    try:
        selection = json.loads(selected.read_text(encoding="utf-8"))
        if not isinstance(selection, dict):
            return "incomplete"
        directory = selection["directory"]
        grade_file = selection.get("grade_file", "grade.json")
        if not isinstance(directory, str) or not isinstance(grade_file, str):
            return "incomplete"
        for relative in (directory, grade_file):
            if not relative or Path(relative).is_absolute() or ".." in Path(relative).parts:
                return "incomplete"
        grade_path = evidence / directory / grade_file
        try:
            grade_path.resolve().relative_to(evidence.resolve())
        except ValueError:
            return "incomplete"
        grade = json.loads(grade_path.read_text(encoding="utf-8"))
        if not isinstance(grade, dict):
            return "incomplete"
        status = grade.get("status")
        if status in ("pass", "fail") and (
            grade.get("schema_valid") is not True
            or not isinstance(grade.get("sequence"), dict)
            or not isinstance(grade.get("outcome"), dict)
        ):
            return "incomplete"
    except (OSError, ValueError, KeyError, TypeError):
        return "incomplete"
    return status if status in ("pass", "fail", "incomplete") else "incomplete"


def execute_trial(root: Path, entry: dict[str, Any], manifest: dict[str, Any], python: str) -> dict[str, Any]:
    harness = Path(manifest["harness"]["path"])
    trial = root / entry["relative_root"]
    script = harness / "tests" / ("improve_quality_resume.py" if entry["cohort"] == "controlled_resume" else "improve_quality.py")
    if entry["cohort"] == "controlled_resume":
        argv = [python, str(script), "run-all", "--source-repo", manifest["source"]["repository"],
                "--revision", manifest["source"]["revision"], "--root", str(trial)]
    else:
        run_argv = [python, str(script), "run", "--root", str(trial), "--timeout", "1200"]
        audit_argv = [python, str(script), "audit", "--root", str(trial), "--timeout", "900"]
        first = command(run_argv, timeout=1300)
        if first.returncode:
            return {"status": "incomplete", "process_failure": "run", "returncode": first.returncode,
                    "stderr": first.stderr[-2000:]}
        second = command(audit_argv, timeout=1000)
        if second.returncode:
            return {"status": "incomplete", "process_failure": "audit", "returncode": second.returncode,
                    "stderr": second.stderr[-2000:]}
        return {"status": _grade_status(root, entry)}
    # run-all performs its two 1200-second host phases and a bounded 600-second
    # audit itself.  The outer deadline must encompass all three phases.
    result = command(argv, timeout=3300)
    if result.returncode:
        return {"status": "incomplete", "process_failure": "controlled_run_all", "returncode": result.returncode,
                "stderr": result.stderr[-2000:]}
    return {"status": _grade_status(root, entry)}


@contextmanager
def _exclusive_run_lock(root: Path):
    """Prevent two launcher processes from selecting the same queued entries."""
    path = root / ".batch-run.lock"
    if path.is_symlink():
        raise BatchError("batch run lock is unsafe")
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise BatchError("another batch launcher is already running") from error
        yield
    finally:
        os.close(descriptor)


def run_batch(root: Path, *, concurrency: int = 4, continue_after_inspection: bool = False,
              python: str = sys.executable) -> dict[str, Any]:
    root = root.resolve()
    with _exclusive_run_lock(root):
        return _run_batch_locked(root, concurrency=concurrency,
                                 continue_after_inspection=continue_after_inspection, python=python)


def _run_batch_locked(root: Path, *, concurrency: int, continue_after_inspection: bool,
                      python: str) -> dict[str, Any]:
    if type(concurrency) is not int or not 1 <= concurrency <= 16:
        raise ValueError("concurrency must be an integer from 1 through 16")
    manifest = _read_manifest(root)
    if tree_digest(Path(manifest["harness"]["path"])) != manifest["harness"]["tree_sha256"]:
        raise BatchError("frozen harness drifted; prepare a new batch")
    attempts = _attempts(root)
    entries = manifest["schedule"]
    _assert_prepared(root, entries)
    launched, unfinished = _attempt_state(entries, attempts)
    prior_bad = bool(unfinished) or any(record.get("kind") == "finished" and record.get("status") in ("fail", "incomplete") for record in attempts)
    if prior_bad and not continue_after_inspection:
        return {"status": "stopped", "reason": "prior non-pass requires explicit continue-after-inspection", "launched": sorted(launched)}
    queued = [entry for entry in entries if entry["id"] not in launched]
    stop = False
    stop_reason: str | None = None
    launched_now: list[str] = []

    def launch(entry: dict[str, Any]) -> dict[str, Any]:
        _append(root, {"kind": "launched", "id": entry["id"], "at": time.time(), "root": entry["relative_root"]})
        outcome = execute_trial(root, entry, manifest, python)
        outcome.update({"kind": "finished", "id": entry["id"], "at": time.time(), "root": entry["relative_root"]})
        _append(root, outcome)
        return outcome

    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        futures = {}
        iterator = iter(queued)
        while len(futures) < concurrency and not stop:
            if _dispatch_stop_requested(root):
                stop, stop_reason = True, "external stop-dispatch marker"
                break
            try:
                entry = next(iterator)
            except StopIteration:
                break
            launched_now.append(entry["id"])
            futures[pool.submit(launch, entry)] = entry
        while futures:
            done, _ = wait(futures, return_when=FIRST_COMPLETED)
            for future in done:
                entry = futures.pop(future)
                try:
                    outcome = future.result()
                except Exception as error:
                    outcome = {"status": "incomplete", "process_failure": "orchestrator", "error": repr(error),
                               "kind": "finished", "id": entry["id"], "at": time.time(), "root": entry["relative_root"]}
                    _append(root, outcome)
                if outcome.get("status") in ("fail", "incomplete"):
                    stop, stop_reason = True, "non-pass verdict"
            while not stop and len(futures) < concurrency:
                if _dispatch_stop_requested(root):
                    stop, stop_reason = True, "external stop-dispatch marker"
                    break
                try:
                    entry = next(iterator)
                except StopIteration:
                    break
                launched_now.append(entry["id"])
                futures[pool.submit(launch, entry)] = entry
    _, unfinished = _attempt_state(entries, _attempts(root))
    return {"status": "stopped" if stop or unfinished else "complete", "reason": stop_reason,
            "launched_now": launched_now,
            "remaining": [entry["id"] for entry in entries if entry["id"] not in {r["id"] for r in _attempts(root)}],
            "unfinished": sorted(unfinished)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    prepare = commands.add_parser("prepare")
    prepare.add_argument("--root", type=Path, required=True)
    prepare.add_argument("--harness-root", type=Path, required=True)
    prepare.add_argument("--source-repo", type=Path, required=True)
    prepare.add_argument("--revision", required=True)
    prepare.add_argument("--repetitions", type=int, default=3)
    prepare.add_argument("--controlled-repetitions", type=int, default=0)
    run = commands.add_parser("run")
    run.add_argument("--root", type=Path, required=True)
    run.add_argument("--concurrency", type=int, default=4)
    run.add_argument("--continue-after-inspection", action="store_true")
    args = parser.parse_args()
    if args.command == "prepare":
        result = prepare_batch(args.root, args.harness_root, args.source_repo, args.revision,
                               repetitions=args.repetitions, controlled_repetitions=args.controlled_repetitions)
    else:
        result = run_batch(args.root, concurrency=args.concurrency,
                           continue_after_inspection=args.continue_after_inspection)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
