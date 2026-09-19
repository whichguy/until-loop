#!/usr/bin/env python3
"""Opt-in, callback-native release probes for the installed Improve package.

The controller freezes a package and builds a disposable candidate without
starting a model.  A host deliberately runs each fresh worker, saves the
runtime's *raw* ``done`` stdout plus its transcript, then calls ``checkpoint``.
Checkpoints retain protocol and independent outcome facts but intentionally do
not turn callback text into a semantic review verdict.

Examples:
  python3 tests/release_validation.py prepare --source-repo /repo --revision SHA \\
    --root /tmp/release-repair --case repair
  python3 tests/release_validation.py packet --root /tmp/release-repair
  python3 tests/release_validation.py checkpoint --root /tmp/release-repair \\
    --receipt /tmp/raw-done.json --transcript /tmp/worker-transcript.txt
  python3 tests/release_validation.py assess --root /tmp/release-repair \\
    --assessment /tmp/independent-assessment.json
  python3 tests/release_validation.py inject-regression --root /tmp/release-regression
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence

if __package__:
    from . import fresh_context
    from . import improve_quality as quality
    from . import improve_quality_fixtures as fixtures
else:
    import fresh_context
    import improve_quality as quality
    import improve_quality_fixtures as fixtures


FORMAT = "until-loop-release-validation/v1"
ASSESSMENT_FORMAT = "until-loop-release-validation-assessment/v1"
CASE_FIXTURES = {"repair": "blank_fallback", "clean": "clean_control", "regression": "clean_control"}
NL_CASES = (
    "packet-explicit-stop-missing-source",
    "packet-independent-work-missing-fact",
    "packet-weak-verifier-unmet-clause",
)
RUNTIME_RELATIVE = Path("runtime/until-loop/scripts/until_loop_ephemeral.py")


def _new(path: Path, label: str) -> None:
    if path.exists() or path.is_symlink():
        raise ValueError(f"{label} must be new: {path}")


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _digest_tree(path: Path) -> str:
    return quality.digest(quality.canonical(quality.tree(path)))


def _write_raw(path: Path, raw: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)


def _regular_copy(source: Path, destination: Path) -> tuple[bool, str | None]:
    if not source.is_file() or source.is_symlink():
        return False, "source is missing, non-regular, or a symlink"
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, destination)
    return True, None


def _package_intact(manifest: Mapping[str, Any]) -> dict[str, Any]:
    package = Path(str(manifest["package"]["path"]))
    try:
        actual = quality.tree(package)
        actual_digest = quality.digest(quality.canonical(actual))
        return {"passed": actual_digest == manifest["package"]["digest"], "actual_digest": actual_digest}
    except (OSError, ValueError) as error:
        return {"passed": False, "error": str(error)}


def _protected_preserved(before: Mapping[str, Any], after: Mapping[str, Any]) -> bool:
    try:
        for kind in ("staged", "unstaged", "untracked"):
            if before[kind] != after[kind]:
                return False
            name = before[kind]["path"]
            if before["index"]["entries"].get(name) != after["index"]["entries"].get(name):
                return False
            left = [line for line in before["index"]["status_porcelain"].splitlines() if line[3:] == name]
            right = [line for line in after["index"]["status_porcelain"].splitlines() if line[3:] == name]
            if left != right:
                return False
        return True
    except (KeyError, TypeError):
        return False


def _packet_error(packet: Any, workspace: Path) -> str | None:
    if not isinstance(packet, dict):
        return "callback JSON must be an object"
    status = packet.get("status")
    if status not in {"active", "complete", "stopped"}:
        return "callback status must be active, complete, or stopped"
    if packet.get("workspace") != str(workspace):
        return "callback workspace does not match the bound candidate"
    progress = packet.get("progress")
    conditions = packet.get("conditions")
    context = packet.get("context")
    if not isinstance(progress, dict) or not isinstance(conditions, dict) or not isinstance(context, dict):
        return "callback lacks progress, conditions, or context"
    if not isinstance(progress.get("action_number"), int) or progress["action_number"] < 1:
        return "callback action number is invalid"
    if (not isinstance(progress.get("trivial_streak"), int) or progress["trivial_streak"] < 0 or
            not isinstance(progress.get("required_trivial_reviews"), int) or progress["required_trivial_reviews"] < 0):
        return "callback review progress is invalid"
    if not isinstance(packet.get("state_file"), str) or not packet["state_file"]:
        return "callback state file is invalid"
    if status == "active":
        if not isinstance(packet.get("next_argv"), list) or not isinstance(packet.get("done_argv"), list):
            return "active callback lacks transport argv"
        if not isinstance(packet.get("report_schema"), dict):
            return "active callback lacks report schema"
    elif packet.get("next_argv") is not None or packet.get("done_argv") is not None:
        return "terminal callback must not issue a successor callback"
    report = packet.get("last_report")
    if status != "active" and not isinstance(report, dict):
        return "terminal callback lacks its final report"
    if report is not None:
        required = {"classification", "exit_assessment", "continuation_assessment", "evidence", "handoff"}
        if not isinstance(report, dict) or set(report) != required:
            return "callback report has an invalid schema"
        if (report["classification"] not in {"trivial", "non-trivial", "unresolved"} or
                report["exit_assessment"] not in {"satisfied", "unsatisfied", "unknown"} or
                report["continuation_assessment"] not in {"allowed", "blocked", "cancelled"} or
                not isinstance(report["evidence"], str) or not report["evidence"].strip() or
                not isinstance(report["handoff"], str) or not report["handoff"].strip()):
            return "callback report has invalid values"
        if report["classification"] == "unresolved" and report["exit_assessment"] == "satisfied":
            return "unresolved callback cannot assert a satisfied exit"
    return None


def _parse_packet(raw: bytes, workspace: Path) -> tuple[dict[str, Any] | None, str | None]:
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as error:
        return None, f"callback is not valid JSON: {error}"
    error = _packet_error(parsed, workspace)
    return (parsed, error) if error else (parsed, None)


def _chain_error(previous: Mapping[str, Any], current: Mapping[str, Any]) -> str | None:
    if previous.get("status") != "active":
        return "a terminal packet cannot accept another callback"
    if current.get("state_file") != previous.get("state_file"):
        return "callback state file does not continue the issued packet"
    for field in ("workspace", "work", "conditions", "context"):
        if current.get(field) != previous.get(field):
            return f"callback changed frozen {field}"
    old = previous["progress"]["action_number"]
    new = current["progress"]["action_number"]
    if current["progress"].get("required_trivial_reviews") != previous["progress"].get("required_trivial_reviews"):
        return "callback changed the required review gate"
    report = current.get("last_report")
    if not isinstance(report, dict) or report.get("classification") not in {"trivial", "non-trivial", "unresolved"}:
        return "callback lacks a valid completed-cycle report"
    expected_streak = previous["progress"].get("trivial_streak", 0) + 1 if report["classification"] == "trivial" else 0
    if current["progress"].get("trivial_streak") != expected_streak:
        return "callback streak does not match its reported classification"
    if current.get("status") == "active" and new != old + 1:
        return "active callback did not advance exactly one action"
    if current.get("status") != "active" and new != old:
        return "terminal callback has an unexpected action number"
    if current.get("status") != "active" and Path(str(current["state_file"])).exists():
        return "terminal callback retained its runtime state file"
    return None


def _runtime(package: Path, arguments: Sequence[str], payload: Mapping[str, Any]) -> bytes:
    script = package / RUNTIME_RELATIVE
    result = subprocess.run(
        [sys.executable, str(script), *arguments],
        input=json.dumps(payload).encode("utf-8"),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        timeout=30,
    )
    if result.returncode:
        raise RuntimeError(result.stderr.decode("utf-8", "replace") or result.stdout.decode("utf-8", "replace"))
    return result.stdout


def _contract(manifest: Mapping[str, Any]) -> dict[str, Any]:
    candidate = str(manifest["candidate"])
    package = Path(str(manifest["package"]["path"]))
    fixture = manifest["fixture"]
    scope = ", ".join(fixture["scope_paths"])
    return {
        "workspace": candidate,
        "work": (
            "Complete exactly one Improve review cycle: inspect the scoped candidate and the last seven full "
            "Git commit messages, plan warranted work, implement authorized fixes, run applicable checks, and "
            "retain actual review, check, and history records before the issued callback."
        ),
        "exit_condition": "Two consecutive substantive reviews find only trivial or no changes, current checks pass, and no material issue remains.",
        "repeat_condition": "Repeat while useful authorized work or a required distinct review remains; stop incomplete on an actual blocker or requested stop.",
        "required_trivial_reviews": 2,
        "context": {
            "request": fixture["request"],
            "scope": f"Initial candidate baseline is {manifest['baseline']['head']}; only {scope}. Preserve unrelated staged, unstaged, and untracked user work.",
            "authority": "Local scoped commits after checks are authorized when warranted. Do not push, publish, install, alter hooks, or absorb unrelated user work.",
            "environment": "Use the bound candidate and frozen package. Recheck local commands and current artifacts before relying on them.",
            "resources": [
                {"purpose": "selected Improve card", "locator": str(package / "SKILL.md")},
                {"purpose": "review policy", "locator": str(package / "references/review-policy.md")},
                {"purpose": "callback runtime", "locator": str(package / RUNTIME_RELATIVE)},
            ],
        },
    }


def _current_raw(root: Path, manifest: Mapping[str, Any]) -> bytes:
    relative = Path(str(manifest["current_packet"]["path"]))
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError("current packet path escapes the trial root")
    path = (root / relative).resolve()
    try:
        path.relative_to(root.resolve())
    except ValueError as error:
        raise ValueError("current packet path escapes the trial root") from error
    raw = path.read_bytes()
    if quality.digest(raw) != manifest["current_packet"]["sha256"]:
        raise ValueError("retained current packet changed")
    return raw


def _transport(root: Path, manifest: Mapping[str, Any]) -> dict[str, str]:
    cycle = int(manifest["next_cycle"])
    attempts = Path(str(manifest["evidence"])) / "cycles" / f"c{cycle:03d}"
    number = 1
    while (attempts / f"a{number:03d}").exists():
        number += 1
    directory = root / "transport" / f"cycle-{cycle:03d}" / f"attempt-{number:03d}"
    directory.mkdir(parents=True, exist_ok=True)
    return {
        "directory": str(directory),
        "receipt": str(directory / "done-stdout.raw.json"),
        "transcript": str(directory / "host-transcript.txt"),
        "prompt": str(directory / "worker-prompt.txt"),
    }


def _worker_prompt(root: Path, manifest: Mapping[str, Any], packet: Mapping[str, Any], raw: bytes) -> str:
    transport = _transport(root, manifest)
    prior = raw.decode("utf-8", "replace")
    if packet["progress"]["action_number"] == 1 and packet.get("last_report") is None:
        preface = (
            f"Use only the frozen Improve card at `{manifest['package']['path']}/SKILL.md` and the bound candidate "
            f"`{manifest['candidate']}`. The raw user request is carried in the packet context. Complete one full "
            "callback work cycle; retain actual scope, review, check, and seven-message history observations."
        )
    else:
        preface = (
            "You are a fresh executor receiving only the prior real callback packet below. First use its exact "
            "read-only next_argv once, inspect the live current candidate, then complete exactly one returned work "
            "cycle. Do not replay the prior done call or rely on its evidence as proof."
        )
    return (
        preface
        + "\n\nDo not inspect evaluator artifacts, answer keys, sibling repositories, or external result directories. "
        "Do not push or alter protected user work. After the cycle, invoke the issued exact done_argv once with an "
        "actual report. Save that command's unmodified stdout to this receipt path: "
        + transport["receipt"]
        + ". Save a nonempty actual host transcript (for example raw `codex exec --json` events JSONL) or record of the review, history, commands, and checks to: "
        + transport["transcript"]
        + ". Do not manually reconstruct either artifact. The controller separately assesses retained evidence; do "
        "not target a desired classification or oracle result. After saving the returned packet, stop this executor "
        "even if that packet is active: the controller dispatches its successor in a fresh context.\n\nPrior raw callback packet:\n"
        + prior
    )


def _write_worker_material(root: Path, manifest: dict[str, Any]) -> dict[str, str]:
    if not _package_intact(manifest)["passed"]:
        raise ValueError("frozen package changed; do not dispatch a worker")
    raw = _current_raw(root, manifest)
    packet, error = _parse_packet(raw, Path(str(manifest["candidate"])))
    if error or packet is None:
        raise ValueError(f"stored packet is invalid: {error}")
    if packet["status"] != "active":
        raise ValueError("terminal packet cannot dispatch a worker")
    material = _transport(root, manifest)
    prompt = _worker_prompt(root, manifest, packet, raw)
    Path(material["prompt"]).write_text(prompt, encoding="utf-8")
    quality.write_json(Path(material["directory"]) / "packet.json", packet)
    return material


def prepare(source_repo: Path, revision: str, root: Path, case: str) -> dict[str, Any]:
    if case not in CASE_FIXTURES:
        raise ValueError(f"unknown case: {case}")
    root = root.resolve()
    _new(root, "trial root")
    root.mkdir(parents=True)
    package = quality.freeze(source_repo.resolve(), revision, root / "package")
    # Persist this before fixture setup. A later setup step has no authority to
    # replace the selected source or its measured package hash.
    quality.write_json(root / "freeze.json", {"format": FORMAT, "package": package})
    if not _package_intact({"package": package})["passed"]:
        raise RuntimeError("frozen package drifted immediately after extraction")

    candidate = root / "candidate"
    evidence = root / "evaluator"
    fixture = fixtures.prepare_case(CASE_FIXTURES[case], candidate, evidence)
    baseline = {
        "head": quality.git(candidate, "rev-parse", "HEAD").strip(),
        "candidate_digest": _digest_tree(candidate),
        "protected": fixtures.protected_state(candidate),
    }
    manifest: dict[str, Any] = {
        "format": FORMAT,
        "case": case,
        "fixture_case": CASE_FIXTURES[case],
        "candidate": str(candidate),
        "evidence": str(evidence),
        "package": package,
        "fixture": fixture,
        "baseline": baseline,
        "next_cycle": 1,
        "checkpoints": [],
        "assessment": None,
        "injection": None,
        "note": "Evaluator facts are procedural isolation only, not an operating-system read boundary.",
    }
    runtime_dir = root / "runtime"; runtime_dir.mkdir()
    raw = _runtime(Path(package["path"]), ["start", "--directory", str(runtime_dir)], _contract(manifest))
    packet, error = _parse_packet(raw, candidate)
    if error or packet is None or packet["status"] != "active":
        raise RuntimeError(f"runtime did not issue an active packet: {error}")
    start_path = Path("evaluator/packets/c000.raw.json")
    _write_raw(root / start_path, raw)
    manifest["current_packet"] = {"path": str(start_path), "sha256": quality.digest(raw), "status": "active"}
    quality.write_json(root / "trial.json", manifest)
    quality.write_json(evidence / "baseline-oracle.json", fixtures.run_oracle(CASE_FIXTURES[case], candidate))
    quality.write_json(evidence / "baseline-test-quality.json", fixtures.run_test_quality(CASE_FIXTURES[case], candidate))
    snapshot = quality.capture(candidate, evidence, 0, "prepared callback baseline")
    quality.write_json(evidence / "snapshots.json", [snapshot])
    material = _write_worker_material(root, manifest)
    return {"root": str(root), "packet": str(root / start_path), "prompt": material["prompt"], **material}


def packet(root: Path) -> dict[str, Any]:
    root = root.resolve()
    manifest = _read_json(root / "trial.json")
    if not _package_intact(manifest)["passed"]:
        raise ValueError("frozen package changed; do not dispatch a worker")
    raw = _current_raw(root, manifest)
    current, error = _parse_packet(raw, Path(str(manifest["candidate"])))
    if error or current is None:
        raise ValueError(f"stored packet is invalid: {error}")
    if current["status"] != "active":
        return {
            "packet": str(root / manifest["current_packet"]["path"]),
            "status": current["status"],
            "directory": None,
            "prompt": None,
            "receipt": None,
            "transcript": None,
        }
    material = _write_worker_material(root, manifest)
    return {"packet": str(root / manifest["current_packet"]["path"]), "status": current["status"], **material}


def _outcomes(manifest: Mapping[str, Any], candidate: Path, evidence: Path, cycle: int) -> dict[str, Any]:
    package = _package_intact(manifest)
    try:
        protected_now = fixtures.protected_state(candidate)
        protected = {"passed": _protected_preserved(manifest["baseline"]["protected"], protected_now), "observed": protected_now}
    except (OSError, RuntimeError, KeyError) as error:
        protected = {"passed": False, "error": str(error)}
    result: dict[str, Any] = {
        "package": package,
        "protected_work": protected,
        "oracle": fixtures.run_oracle(manifest["fixture_case"], candidate),
        "test_quality": fixtures.run_test_quality(manifest["fixture_case"], candidate),
    }
    try:
        snapshots = _read_json(evidence / "snapshots.json")
        snapshot = quality.capture(candidate, evidence, len(snapshots), "controller checkpoint")
        snapshots.append(snapshot)
        quality.write_json(evidence / "snapshots.json", snapshots)
        result["snapshot"] = snapshot
    except (OSError, RuntimeError, ValueError) as error:
        result["snapshot"] = {"stable": False, "error": str(error)}
    return result


def checkpoint(root: Path, receipt: Path, transcript: Path | None) -> dict[str, Any]:
    root = root.resolve()
    manifest = _read_json(root / "trial.json")
    candidate = Path(str(manifest["candidate"]))
    evidence = Path(str(manifest["evidence"]))
    cycle = int(manifest["next_cycle"])
    cycle_parent = evidence / "cycles" / f"c{cycle:03d}"
    cycle_parent.mkdir(parents=True, exist_ok=True)
    attempt = 1
    while (cycle_parent / f"a{attempt:03d}").exists():
        attempt += 1
    cycle_dir = cycle_parent / f"a{attempt:03d}"
    _new(cycle_dir, "checkpoint evidence")
    cycle_dir.mkdir(parents=True)
    raw_ok, raw_copy_error = _regular_copy(receipt, cycle_dir / "callback.raw.json")
    raw_digest = quality.digest((cycle_dir / "callback.raw.json").read_bytes()) if raw_ok else None
    transcript_ok = False
    transcript_error = "transcript was not supplied"
    if transcript is not None:
        transcript_ok, transcript_error = _regular_copy(transcript, cycle_dir / "transcript.txt")
        if transcript_ok and not (cycle_dir / "transcript.txt").read_bytes().strip():
            transcript_ok, transcript_error = False, "transcript is empty"

    callback: dict[str, Any] | None = None
    errors: list[str] = []
    previous: dict[str, Any] | None = None
    chain_error: str | None = None
    try:
        previous_raw = _current_raw(root, manifest)
        previous, previous_error = _parse_packet(previous_raw, candidate)
        if previous_error or previous is None:
            chain_error = f"stored packet is invalid: {previous_error}"
            errors.append(chain_error)
    except (OSError, ValueError) as error:
        chain_error = f"stored packet is invalid: {error}"
        errors.append(chain_error)
    if not raw_ok:
        errors.append(f"raw callback was not retained: {raw_copy_error}")
    else:
        callback, parse_error = _parse_packet((cycle_dir / "callback.raw.json").read_bytes(), candidate)
        if parse_error or callback is None:
            chain_error = parse_error or "callback parse failed"
            errors.append(chain_error)
        elif previous is not None:
            chain = _chain_error(previous, callback)
            if chain:
                chain_error = chain
                errors.append(chain)
    if not transcript_ok:
        errors.append(f"transcript is incomplete: {transcript_error}")
    legacy_absent = not (candidate / ".until-loop").exists()
    if not legacy_absent:
        errors.append("candidate contains legacy .until-loop state")
    outcomes = _outcomes(manifest, candidate, evidence, cycle)
    if not outcomes["package"]["passed"]:
        errors.append("frozen package changed")
    protocol = {
        "raw_callback_captured": raw_ok,
        "transcript_captured": transcript_ok,
        "legacy_state_absent": legacy_absent,
        "chain_valid": previous is not None and callback is not None and chain_error is None,
        "errors": errors,
    }
    valid = not errors
    record = {
        "format": FORMAT,
        "cycle": cycle,
        "raw_callback": {"path": "callback.raw.json", "sha256": raw_digest},
        "protocol": protocol,
        "outcomes": outcomes,
        "semantic_review": {"status": "unassessed", "reason": "Callbacks and transcripts require an independent structured assessment."},
        "status": "recorded" if valid else "incomplete",
    }
    quality.write_json(cycle_dir / "checkpoint.json", record)
    callback_summary = None if callback is None else {
        "status": callback["status"],
        "classification": (callback.get("last_report") or {}).get("classification"),
        "trivial_streak": callback["progress"].get("trivial_streak"),
        "required_trivial_reviews": callback["progress"].get("required_trivial_reviews"),
    }
    manifest["checkpoints"].append({
        "cycle": cycle, "attempt": attempt, "path": str((cycle_dir / "checkpoint.json").relative_to(root)),
        "status": record["status"], "callback_sha256": raw_digest, "callback": callback_summary,
    })
    if valid and callback is not None:
        callback_path = Path(str((cycle_dir / "callback.raw.json").relative_to(root)))
        manifest["current_packet"] = {"path": str(callback_path), "sha256": quality.digest((cycle_dir / "callback.raw.json").read_bytes()), "status": callback["status"]}
        manifest["next_cycle"] = cycle + 1
    quality.write_json(root / "trial.json", manifest)
    return record


def _recorded_callbacks(root: Path, manifest: Mapping[str, Any]) -> tuple[dict[int, dict[str, Any]], str | None]:
    candidate = Path(str(manifest["candidate"]))
    result: dict[int, dict[str, Any]] = {}
    for item in manifest["checkpoints"]:
        if item.get("status") != "recorded":
            continue
        cycle = item.get("cycle")
        if not isinstance(cycle, int) or cycle in result:
            return {}, "recorded checkpoint identity is invalid"
        expected = item.get("callback_sha256")
        if not isinstance(expected, str) or len(expected) != 64:
            return {}, "recorded callback lacks an expected sha256"
        path = root / str(item.get("path", ""))
        try:
            raw = (path.parent / "callback.raw.json").read_bytes()
            record = _read_json(path)
        except (OSError, ValueError) as error:
            return {}, f"recorded callback is unavailable: {error}"
        raw_record = record.get("raw_callback") if isinstance(record, dict) else None
        if not isinstance(raw_record, dict) or raw_record.get("path") != "callback.raw.json" or raw_record.get("sha256") != expected:
            return {}, "recorded callback digest metadata is inconsistent"
        if quality.digest(raw) != expected:
            return {}, "recorded callback digest does not match retained bytes"
        packet, error = _parse_packet(raw, candidate)
        if error or packet is None:
            return {}, f"recorded callback is invalid: {error}"
        result[cycle] = packet
    return result, None


def _assessment_error(root: Path, value: Any, manifest: Mapping[str, Any]) -> str | None:
    if not isinstance(value, dict) or set(value) != {"format", "reviews", "unresolved_material_findings"}:
        return "assessment has an invalid schema"
    if value["format"] != ASSESSMENT_FORMAT or not isinstance(value["unresolved_material_findings"], bool):
        return "assessment has invalid top-level values"
    if not isinstance(value["reviews"], list) or not value["reviews"]:
        return "assessment needs at least one review"
    callbacks, callback_error = _recorded_callbacks(root, manifest)
    if callback_error:
        return callback_error
    valid = {item["cycle"]: item for item in manifest["checkpoints"] if item["status"] == "recorded"}
    seen: set[int] = set()
    for row in value["reviews"]:
        if not isinstance(row, dict) or set(row) != {"cycle", "judgment", "basis"}:
            return "assessment review has an invalid schema"
        if not isinstance(row["cycle"], int) or row["cycle"] not in valid or row["cycle"] in seen:
            return "assessment review refers to an unavailable cycle"
        if row["judgment"] not in {"qualifying", "material", "incomplete"} or not isinstance(row["basis"], str) or not row["basis"].strip():
            return "assessment review has invalid values"
        callback = callbacks[row["cycle"]]
        classification = (callback.get("last_report") or {}).get("classification")
        streak = callback.get("progress", {}).get("trivial_streak")
        if row["judgment"] == "qualifying" and (classification != "trivial" or not isinstance(streak, int) or streak < 1):
            return "qualifying assessment contradicts callback classification or streak"
        if row["judgment"] == "material" and (classification != "non-trivial" or streak != 0):
            return "material assessment contradicts callback classification or reset"
        seen.add(row["cycle"])
    if seen != set(valid):
        return "assessment must cover every recorded cycle"
    prior = (manifest.get("assessment") or {}).get("assessment")
    if isinstance(prior, dict):
        old = {row["cycle"]: row["judgment"] for row in prior.get("reviews", []) if isinstance(row, dict) and "cycle" in row}
        new = {row["cycle"]: row["judgment"] for row in value["reviews"]}
        if any(new.get(cycle) != judgment for cycle, judgment in old.items()):
            return "assessment cannot rewrite a prior judgment"
    return None


def assess(root: Path, assessment: Path) -> dict[str, Any]:
    root = root.resolve()
    manifest = _read_json(root / "trial.json")
    evidence = Path(str(manifest["evidence"]))
    revision = 1
    while (evidence / "assessments" / f"a{revision:03d}.raw.json").exists() or (evidence / "assessments" / f"a{revision:03d}.json").exists():
        revision += 1
    raw_path = evidence / "assessments" / f"a{revision:03d}.raw.json"
    raw_ok, copy_error = _regular_copy(assessment, raw_path)
    if not raw_ok:
        raise ValueError(f"assessment was not retained: {copy_error}")
    try:
        value = _read_json(raw_path)
    except (OSError, ValueError) as error:
        raise ValueError(f"assessment is not valid JSON: {error}") from error
    error = _assessment_error(root, value, manifest)
    if error:
        raise ValueError(error)
    qualifying = [row["cycle"] for row in value["reviews"] if row["judgment"] == "qualifying"]
    record = {"assessment": value, "first_qualifying_cycle": min(qualifying) if qualifying else None, "revision": revision}
    record_path = evidence / "assessments" / f"a{revision:03d}.json"
    quality.write_json(record_path, record)
    manifest.setdefault("assessment_revisions", []).append({"path": str(record_path.relative_to(root)), "revision": revision})
    manifest["assessment"] = {"path": str(record_path.relative_to(root)), **record}
    quality.write_json(root / "trial.json", manifest)
    return record


def inject_regression(root: Path) -> dict[str, Any]:
    root = root.resolve()
    manifest = _read_json(root / "trial.json")
    if manifest["case"] != "regression":
        raise ValueError("regression injection is available only for --case regression")
    if manifest.get("injection") is not None:
        raise ValueError("regression was already injected")
    assessment = manifest.get("assessment") or {}
    if not isinstance(assessment.get("first_qualifying_cycle"), int):
        raise ValueError("regression injection requires an independently assessed qualifying review")
    if assessment.get("assessment", {}).get("unresolved_material_findings") is not False:
        raise ValueError("regression injection requires no unresolved material findings")
    rows = assessment.get("assessment", {}).get("reviews", [])
    if not isinstance(rows, list) or not rows:
        raise ValueError("regression injection requires a complete current assessment")
    assessment_error = _assessment_error(root, assessment["assessment"], manifest)
    if assessment_error:
        raise ValueError(f"regression injection requires a complete current assessment: {assessment_error}")
    latest = max(rows, key=lambda row: row["cycle"])
    if latest.get("judgment") != "qualifying" or latest.get("cycle") != assessment["first_qualifying_cycle"]:
        raise ValueError("regression injection requires the latest independently assessed first qualifying review")
    callbacks, callback_error = _recorded_callbacks(root, manifest)
    if callback_error:
        raise ValueError(callback_error)
    callback = callbacks.get(latest["cycle"])
    if (not isinstance(callback, dict) or (callback.get("last_report") or {}).get("classification") != "trivial" or
            callback.get("progress", {}).get("trivial_streak") != 1):
        raise ValueError("regression injection requires a first qualifying callback with actual streak one")
    if manifest["current_packet"]["status"] != "active":
        raise ValueError("regression injection requires a live active packet for re-evaluation")
    package = _package_intact(manifest)
    if not package["passed"]:
        raise ValueError("frozen package changed before regression injection")
    candidate = Path(str(manifest["candidate"]))
    source = candidate / "formatter.py"
    before = source.read_bytes()
    if b"value.strip() or 'Anonymous'" not in before:
        raise ValueError("clean formatter does not contain the expected blank fallback")
    protected_before = fixtures.protected_state(candidate)
    current_raw = _current_raw(root, manifest)
    current, error = _parse_packet(current_raw, candidate)
    if error or current is None:
        raise ValueError(f"current packet is invalid: {error}")
    state_file = Path(current["state_file"])
    state_before = state_file.read_bytes() if state_file.is_file() and not state_file.is_symlink() else None
    head_before = quality.git(candidate, "rev-parse", "HEAD").strip()
    candidate_before = _digest_tree(candidate)
    source.write_text(
        "def format_name(value: str) -> str:\n    \"\"\"Trim a display name and return its documented fallback.\"\"\"\n    return value.strip()\n",
        encoding="utf-8",
    )
    protected_after = fixtures.protected_state(candidate)
    oracle = fixtures.run_oracle("clean_control", candidate)
    state_after = state_file.read_bytes() if state_file.is_file() and not state_file.is_symlink() else None
    result = {
        "format": FORMAT,
        "after_qualifying_cycle": latest["cycle"],
        "head": {"before": head_before, "after": quality.git(candidate, "rev-parse", "HEAD").strip()},
        "candidate_digest": {"before": candidate_before, "after": _digest_tree(candidate)},
        "formatter_sha256": {"before": quality.digest(before), "after": quality.digest(source.read_bytes())},
        "package_unchanged": _package_intact(manifest),
        "protected_work_unchanged": _protected_preserved(protected_before, protected_after),
        "runtime_state_unchanged": state_before is not None and state_before == state_after,
        "prior_packet_unchanged": quality.digest(current_raw) == manifest["current_packet"]["sha256"],
        "oracle": oracle,
        "note": "Controller-only uncommitted intervention; a fresh executor must inspect the live candidate through the retained real packet.",
    }
    result["passed"] = (
        result["candidate_digest"]["before"] != result["candidate_digest"]["after"]
        and result["head"]["before"] == result["head"]["after"]
        and not oracle["passed"]
        and result["package_unchanged"]["passed"]
        and result["protected_work_unchanged"]
        and result["runtime_state_unchanged"]
        and result["prior_packet_unchanged"]
    )
    evidence = Path(str(manifest["evidence"]))
    quality.write_json(evidence / "intervention.json", result)
    try:
        snapshots = _read_json(evidence / "snapshots.json")
        snapshot = quality.capture(candidate, evidence, len(snapshots), "controller regression intervention")
        snapshots.append(snapshot)
        quality.write_json(evidence / "snapshots.json", snapshots)
        result["snapshot"] = snapshot
    except (OSError, RuntimeError, ValueError) as error:
        result["snapshot"] = {"stable": False, "error": str(error)}
        result["passed"] = False
    manifest["injection"] = result
    quality.write_json(root / "trial.json", manifest)
    if not result["passed"]:
        raise RuntimeError("regression intervention did not preserve its required controls")
    return result


NL_SCHEMA = fresh_context.NL_OUTPUT_SCHEMA


def _nl_prompt(case_id: str, snapshot: Path, workspace: Path) -> str:
    request = fresh_context.PACKET_SETUP[case_id]["request"]
    return (
        f"You are a fresh interpreter in a read-only experiment. Read the selected Improve card `{snapshot / 'SKILL.md'}`, "
        f"its review policy `{snapshot / 'references/review-policy.md'}`, and bound Until Loop adapter "
        f"`{snapshot / 'runtime/until-loop/ADAPTER.md'}` in full. Read only those package resources and fixture files in "
        f"`{workspace}` as needed. Interpret the raw request into the CURRENT ephemeral Until Loop contract: workspace, "
        "work, exit_condition, repeat_condition, required_trivial_reviews, and context. The context must preserve request, "
        "scope, authority, environment, and actual resource locators. Return that object serialized in contract_json, then "
        "complete execute, continue, success, early_stop, initial_decision, immediate_next_action, and evidence. Preserve the "
        "actual no-commit, no-push, no-publication, and no-contact authority. Do not use a historical v2 contract. This is "
        "interpretation only: do not execute product work, tests, start, done, commits, or network operations. Do not inspect "
        "evaluator sources or other trials, invent test results/history/source facts, or self-grade. These probe restrictions "
        "do not add exit requirements to the user's request.\n\n"
        f"Exact user request:\n{request}\n"
    )


def prepare_nl(root: Path, repetitions: int = 2) -> dict[str, Any]:
    root = root.resolve()
    manifest = _read_json(root / "trial.json")
    if type(repetitions) is not int or repetitions < 1:
        raise ValueError("repetitions must be a positive integer")
    destination = root / "interpretation"
    _new(destination, "NL preparation root")
    destination.mkdir()
    cases = fresh_context.load_cases()
    entries = []
    for repeat in range(1, repetitions + 1):
        for number, case_id in enumerate(NL_CASES, start=1):
            item = cases[case_id]
            workspace = destination / f"r{repeat}-case-{number:02d}" / "workspace"
            workspace.mkdir(parents=True)
            fresh_context.materialize_files(workspace, item["files"])
            entries.append({"id": f"r{repeat}-{number:02d}", "case_id": case_id, "mode": "raw_nl", "workspace": str(workspace)})
    prepared = {"format": FORMAT, "snapshot": manifest["package"]["path"], "schema": NL_SCHEMA, "cases": entries}
    quality.write_json(destination / "prepared.json", prepared)
    return {"prepared": str(destination / "prepared.json"), "case_count": len(entries)}


def run_nl(root: Path, run_id: str, timeout_seconds: int = 600) -> list[dict[str, Any]]:
    root = root.resolve()
    manifest = _read_json(root / "trial.json")
    if not _package_intact(manifest)["passed"]:
        raise ValueError("frozen package changed; do not dispatch NL probes")
    prepared = _read_json(root / "interpretation" / "prepared.json")
    if not isinstance(run_id, str) or not run_id or "/" in run_id:
        raise ValueError("run id is invalid")
    run_root = root / "interpretation" / "runs" / run_id
    _new(run_root, "NL run root")
    if type(timeout_seconds) is not int or not 1 <= timeout_seconds <= 600:
        raise ValueError("NL timeout must be an integer from 1 through 600 seconds")
    run_root.mkdir(parents=True)
    snapshot = Path(str(prepared["snapshot"]))
    results = []
    for entry in prepared["cases"]:
        case = dict(entry)
        prompt = _nl_prompt(case["case_id"], snapshot, Path(case["workspace"]))
        results.append(fresh_context.run_host(case, snapshot, run_root, prompt_spec=(prompt, NL_SCHEMA), timeout_seconds=timeout_seconds))
    quality.write_json(run_root / "run.json", {"format": FORMAT, "records": results})
    return results


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("prepare", help="freeze a package and create a no-model callback probe")
    p.add_argument("--source-repo", type=Path, required=True); p.add_argument("--revision", required=True)
    p.add_argument("--root", type=Path, required=True); p.add_argument("--case", choices=sorted(CASE_FIXTURES), required=True)
    p = sub.add_parser("packet", help="write the fresh-worker transport prompt for the retained packet")
    p.add_argument("--root", type=Path, required=True)
    p = sub.add_parser("checkpoint", help="retain one actual done stdout and transcript")
    p.add_argument("--root", type=Path, required=True); p.add_argument("--receipt", type=Path, required=True)
    p.add_argument("--transcript", type=Path)
    p = sub.add_parser("assess", help="record a separate independent structured assessment")
    p.add_argument("--root", type=Path, required=True); p.add_argument("--assessment", type=Path, required=True)
    p = sub.add_parser("inject-regression", help="inject the known clean-control blank fallback defect")
    p.add_argument("--root", type=Path, required=True)
    p = sub.add_parser("prepare-nl", help="prepare two read-only interpretation repetitions without models")
    p.add_argument("--root", type=Path, required=True); p.add_argument("--repetitions", type=int, default=2)
    p = sub.add_parser("run-nl", help="explicitly run prepared read-only NL probes")
    p.add_argument("--root", type=Path, required=True); p.add_argument("--run-id", required=True)
    p.add_argument("--timeout", type=int, default=600)
    args = parser.parse_args(argv)
    try:
        if args.command == "prepare": result = prepare(args.source_repo, args.revision, args.root, args.case)
        elif args.command == "packet": result = packet(args.root)
        elif args.command == "checkpoint": result = checkpoint(args.root, args.receipt, args.transcript)
        elif args.command == "assess": result = assess(args.root, args.assessment)
        elif args.command == "inject-regression": result = inject_regression(args.root)
        elif args.command == "prepare-nl": result = prepare_nl(args.root, args.repetitions)
        else: result = run_nl(args.root, args.run_id, args.timeout)
    except (OSError, RuntimeError, ValueError, subprocess.TimeoutExpired) as error:
        parser.error(str(error))
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
