#!/usr/bin/env python3
"""Fresh-context behavioral harness for the isolated until-loop v2 candidate.

This harness deliberately separates fixture construction from host runs.  It
uses the public v2 CLI to build every durable state and saves the packet the
CLI printed.  Model workers receive either that packet alone or a raw user
request plus a frozen skill snapshot.  It records evidence; it does not judge
whether a worker's reasoning is semantically correct.

Examples:
  python3 tests/fresh_context.py prepare --label before
  python3 tests/fresh_context.py run --label before --run-id priority \
    --case packet-paused-condition-observed --case packet-uncertain-verifier
  python3 tests/fresh_context.py summarize --label before --run-id priority
"""
from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


HERE = Path(__file__).resolve()
DEFAULT_SKILL_ROOT = HERE.parents[1]
DEFAULT_REVIEW_ROOT = DEFAULT_SKILL_ROOT.parent / "until-loop-v2-validation" / "fresh-context-review"
CASE_PATH = HERE.with_name("fresh-context-cases.json")
MAX_WORKERS = 2
TIMEOUT_SECONDS = 180
SNAPSHOT_FILES = (
    Path("SKILL.md"),
    Path("scripts/until-loop"),
    Path("scripts/until_loop_v2.py"),
    Path("scripts/until_loop_packet.py"),
)
POLICY_PATTERN = re.compile(r"^```json\s*$\n(.*?)^```\s*$", re.MULTILINE | re.DOTALL)


# These setup facts stay out of fresh-context-cases.json, which is never made
# available to a worker.  They describe only protocol construction and exact
# runtime outcomes, not an expected worker answer.
PACKET_SETUP: dict[str, dict[str, Any]] = {
    "packet-independent-work-missing-fact": {
        "request": "Prepare report.md with the complete source-supported mailing address and correct the spelling of receive in README.md. Keep working until both are done. If the postal code is absent, complete the independent README correction and report the missing postal code without inventing one.",
        "interpretation": "Inspect the source and correct any independent authorized work. Continue while useful work remains; a missing postal code prevents a complete address but does not erase independent work.",
        "criteria": (
            "report.md has the complete source-supported mailing address",
            "README.md spells receive correctly",
        ),
        "phase": "active",
    },
    "packet-explicit-stop-missing-source": {
        "request": "If the signed source is absent, stop this entire request immediately and report that blocker. Do not make unrelated corrections after that stop condition, even though README.md contains a spelling error. Otherwise prepare the signed summary and correct README.md.",
        "interpretation": "Check whether the signed source exists before any work. Its absence is an explicit global early stop for the entire request.",
        "criteria": (
            "signed-summary.md uses the signed source",
            "README.md spelling is corrected only if the signed-source stop condition does not apply",
        ),
        "phase": "active",
    },
    "packet-weak-verifier-unmet-clause": {
        "request": "Keep working until display_name trims whitespace and README.md documents that behavior. The configured test is useful evidence but does not replace the documentation requirement.",
        "interpretation": "Inspect both the helper and README. Preserve the separate documentation clause even if a narrow verifier passes.",
        "criteria": (
            "display_name handles the exercised nonblank name",
            "README.md documents whitespace trimming",
        ),
        "verify": "python3 -m unittest -q test_display_name.py",
        "phase": "active",
    },
    "packet-failed-verifier": {
        "request": "Repair the record formatter and keep working until its configured verification succeeds.",
        "interpretation": "Diagnose a failed configured check and reassess the formatter against current evidence.",
        "criteria": (
            "record formatter meets the requested output",
            "configured verification succeeds",
        ),
        "verify": "python3 verifier.py",
        "phase": "active",
    },
    "packet-paused-user-instruction": {
        "request": "Do not contact the account until I explicitly tell you to resume. Prepare the account summary once that instruction is available.",
        "interpretation": "The work is paused pending an explicit user instruction.",
        "criteria": ("account summary is prepared from authorized account access",),
        "phase": "paused",
        "resume_on": "user_instruction",
    },
    "packet-paused-condition-observed": {
        "request": "Prepare source-summary.md from authoritative-source.txt. If the source is missing, pause until it is observed, then resume through the adapter.",
        "interpretation": "The source was absent when the pause was recorded. A later observed source is a resumption condition, not permission to bypass the adapter.",
        "criteria": ("source-summary.md uses authoritative-source.txt",),
        "phase": "paused",
        "resume_on": "condition_observed",
    },
    "packet-terminal-done": {
        "request": "Confirm finished.txt contains the approved release marker and stop when it does.",
        "interpretation": "Completion requires current evidence for the release marker.",
        "criteria": ("finished.txt contains the approved release marker",),
        "phase": "done",
    },
    "packet-terminal-halted": {
        "request": "Improve pending.txt until its open issue is resolved.",
        "interpretation": "The issue remains unresolved and the configured cycle boundary can halt the run without claiming success.",
        "criteria": ("pending.txt has no open issue",),
        "phase": "halted",
    },
    "packet-rejected-current-assessment": {
        "request": "Update the two required release notes and keep working until both are present.",
        "interpretation": "Both release-note clauses require current evidence before completion.",
        "criteria": ("release note A is present", "release note B is present"),
        "phase": "active",
    },
    "packet-uncertain-verifier": {
        "request": "Validate the import and continue only after its verifier outcome is known.",
        "interpretation": "A verifier interruption requires recovery before another assessment or replay.",
        "criteria": ("import behavior is assessed against current evidence",),
        "verify": "python3 verifier.py",
        "phase": "active",
        "recovery": "verifier_uncertain",
    },
    "packet-truncated-contract": {
        "request": "Satisfy all criteria in release-criteria.md before completion.",
        "interpretation": "Read release-criteria.md and the full recorded contract before deciding whether all criteria have current evidence.",
        "criteria": tuple(
            [f"release criterion {index} is satisfied" for index in range(1, 13)]
            + ["publication remains on hold until legal review is recorded"]
        ),
        "phase": "active",
    },
}


PACKET_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "decision",
        "immediate_next_action",
        "runtime_command_argv",
        "runtime_input_json",
        "command_precondition",
        "evidence",
    ],
    "properties": {
        "decision": {"type": "string"},
        "immediate_next_action": {"type": "string"},
        "runtime_command_argv": {
            "anyOf": [
                {"type": "null"},
                {"type": "array", "items": {"type": "string"}},
            ]
        },
        "runtime_input_json": {"type": ["string", "null"]},
        "command_precondition": {"type": "string"},
        "evidence": {"type": "string"},
    },
}

NL_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "contract_json",
        "execute",
        "continue",
        "success",
        "early_stop",
        "initial_decision",
        "immediate_next_action",
        "evidence",
    ],
    "properties": {
        "contract_json": {"type": "string"},
        "execute": {"type": "string"},
        "continue": {"type": "string"},
        "success": {"type": "string"},
        "early_stop": {"type": "string"},
        "initial_decision": {"type": "string"},
        "immediate_next_action": {"type": "string"},
        "evidence": {"type": "string"},
    },
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def canonical_json(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def write_json(path: Path, value: Any) -> None:
    write_text(path, json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n")


def require_new(path: Path, label: str) -> None:
    if path.exists() or path.is_symlink():
        raise RuntimeError(f"refusing to overwrite existing {label}: {path}")


def safe_component(value: str, label: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", value) or value in {".", ".."}:
        raise ValueError(f"{label} must be one safe path component")
    return value


def safe_relative(value: str) -> Path:
    candidate = Path(value)
    if candidate.is_absolute() or not candidate.parts or ".." in candidate.parts:
        raise ValueError(f"unsafe fixture path: {value!r}")
    return candidate


def snapshot_tree(root: Path) -> dict[str, Any]:
    """Hash a tree without following symlinks; retained JSON is the evidence."""
    entries: dict[str, Any] = {}
    if not root.is_dir():
        return {"_root_error": f"not a directory: {root}"}
    for path in sorted(root.rglob("*")):
        relative = str(path.relative_to(root))
        metadata = path.lstat()
        if path.is_symlink():
            entries[relative] = {"kind": "symlink", "target": os.readlink(path)}
        elif path.is_file():
            entries[relative] = {
                "kind": "file",
                "bytes": metadata.st_size,
                "mode": oct(metadata.st_mode & 0o777),
                "sha256": sha256_bytes(path.read_bytes()),
            }
        elif path.is_dir():
            entries[relative] = {"kind": "directory", "mode": oct(metadata.st_mode & 0o777)}
        else:
            entries[relative] = {"kind": "other", "mode": oct(metadata.st_mode & 0o777)}
    return entries


def tree_hash(root: Path) -> str:
    return sha256_bytes(canonical_json(snapshot_tree(root)))


def required_snapshot_paths(skill_root: Path) -> list[Path]:
    references = skill_root / "references"
    if not references.is_dir():
        raise RuntimeError(f"candidate references are missing: {references}")
    paths = list(SNAPSHOT_FILES)
    paths.extend(sorted(path.relative_to(skill_root) for path in references.glob("*.md") if path.is_file()))
    for relative in paths:
        source = skill_root / relative
        if source.is_symlink() or not source.is_file():
            raise RuntimeError(f"candidate snapshot input is not a regular file: {source}")
    return paths


def copy_snapshot(skill_root: Path, destination: Path) -> dict[str, Any]:
    require_new(destination, "candidate source snapshot")
    paths = required_snapshot_paths(skill_root)
    destination.mkdir(parents=True)
    for relative in paths:
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(skill_root / relative, target, follow_symlinks=False)
    return {
        "created_utc": utc_now(),
        "source_root": str(skill_root.resolve()),
        "selected_files": [str(path) for path in paths],
        "source_hash": tree_hash(destination),
    }


def source_record_path(review_root: Path, label: str) -> Path:
    return review_root / "source-records" / f"{label}.json"


def ensure_source_snapshot(review_root: Path, label: str, skill_root: Path | None) -> tuple[Path, dict[str, Any]]:
    snapshot = review_root / label
    record_path = source_record_path(review_root, label)
    if snapshot.exists() or snapshot.is_symlink():
        if not snapshot.is_dir():
            raise RuntimeError(f"candidate snapshot is not a directory: {snapshot}")
        required_snapshot_paths(snapshot)
        record = {
            "created_utc": utc_now(),
            "source_root": "pre-frozen snapshot",
            "selected_files": [str(path) for path in required_snapshot_paths(snapshot)],
            "source_hash": tree_hash(snapshot),
            "snapshot": str(snapshot),
        }
        if not record_path.exists():
            write_json(record_path, record)
        return snapshot, record
    if skill_root is None:
        raise RuntimeError(f"snapshot {snapshot} is absent; pass --skill-root to freeze it")
    record = copy_snapshot(skill_root.resolve(), snapshot)
    record["snapshot"] = str(snapshot)
    require_new(record_path, "source record")
    write_json(record_path, record)
    return snapshot, record


def policy_version(skill_root: Path) -> str:
    text = (skill_root / "references" / "decision-rubric.md").read_text(encoding="utf-8")
    match = POLICY_PATTERN.search(text)
    if match is None:
        raise RuntimeError("candidate decision-rubric.md has no JSON policy object")
    raw = json.loads(match.group(1))
    version = raw.get("version") if isinstance(raw, dict) else None
    if not isinstance(version, str) or not version:
        raise RuntimeError("candidate decision-rubric.md has no policy version")
    return version


def load_cases() -> dict[str, dict[str, Any]]:
    raw = json.loads(CASE_PATH.read_text(encoding="utf-8"))
    if raw.get("format") != "until-loop-v2-fresh-context-cases/v1":
        raise ValueError("unsupported fresh-context case format")
    cases: dict[str, dict[str, Any]] = {}
    for mode in ("packet_cases", "nl_cases"):
        values = raw.get(mode)
        if not isinstance(values, list):
            raise ValueError(f"{mode} must be an array")
        for item in values:
            if not isinstance(item, dict) or item.get("mode") not in ("packet", "raw_nl"):
                raise ValueError(f"invalid case in {mode}")
            case_id = item.get("id")
            files = item.get("files")
            if not isinstance(case_id, str) or not case_id or case_id in cases or not isinstance(files, dict):
                raise ValueError(f"invalid or duplicate case id: {case_id!r}")
            if item["mode"] == "raw_nl":
                for key in ("request", "raw_facts"):
                    if not isinstance(item.get(key), str) or not item[key]:
                        raise ValueError(f"raw NL case {case_id} lacks {key}")
            for name, content in files.items():
                if not isinstance(name, str) or not isinstance(content, str):
                    raise ValueError(f"invalid fixture file in {case_id}")
                safe_relative(name)
            cases[case_id] = item
    if set(case for case, value in cases.items() if value["mode"] == "packet") != set(PACKET_SETUP):
        raise ValueError("packet cases do not match the fixed protocol setup map")
    return cases


def select_cases(cases: Mapping[str, dict[str, Any]], requested: Sequence[str]) -> list[dict[str, Any]]:
    if not requested:
        return [cases[key] for key in sorted(cases)]
    seen: set[str] = set()
    selected: list[dict[str, Any]] = []
    for case_id in requested:
        if case_id in seen:
            raise ValueError(f"duplicate --case: {case_id}")
        if case_id not in cases:
            raise ValueError(f"unknown --case: {case_id}")
        seen.add(case_id)
        selected.append(cases[case_id])
    return selected


def opaque_workspace_name(cases: Mapping[str, dict[str, Any]], case_id: str) -> str:
    """Keep evaluator-facing IDs out of the model-visible bound path."""
    ordered = sorted(cases)
    try:
        return f"case-{ordered.index(case_id) + 1:03d}"
    except ValueError as error:
        raise ValueError(f"unknown case for opaque workspace: {case_id}") from error


def materialize_files(workspace: Path, files: Mapping[str, Any]) -> None:
    for name, content in files.items():
        if not isinstance(name, str) or not isinstance(content, str):
            raise ValueError(f"invalid fixture record: {name!r}")
        target = workspace / safe_relative(name)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")


def cli_environment(extra: Mapping[str, str] | None = None) -> dict[str, str]:
    environment = os.environ.copy()
    for key in list(environment):
        if key.startswith("UNTIL_LOOP_"):
            environment.pop(key)
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    environment["LC_ALL"] = "C"
    environment["LANG"] = "C"
    if extra:
        environment.update(extra)
    return environment


def run_adapter(
    *, skill_root: Path, workspace: Path, arguments: Sequence[str], log_dir: Path,
    name: str, extra_env: Mapping[str, str] | None = None,
) -> subprocess.CompletedProcess[bytes]:
    """Run a public CLI mutation and retain command, stdout, stderr, and exit."""
    argv = [sys.executable, str(skill_root / "scripts" / "until-loop"), "v2", *arguments]
    result = subprocess.run(
        argv,
        cwd=workspace,
        env=cli_environment(extra_env),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        timeout=30,
    )
    write_json(
        log_dir / f"{name}.json",
        {
            "argv": argv,
            "cwd": str(workspace),
            "returncode": result.returncode,
            "stdout": result.stdout.decode("utf-8", errors="replace"),
            "stderr": result.stderr.decode("utf-8", errors="replace"),
            "environment_overrides": dict(extra_env or {}),
        },
    )
    return result


def require_exit(result: subprocess.CompletedProcess[bytes], expected: int, label: str) -> None:
    if result.returncode != expected:
        raise RuntimeError(
            f"{label} returned {result.returncode}, expected {expected}: "
            f"{result.stderr.decode('utf-8', errors='replace')[-1000:]}"
        )


def read_state(workspace: Path) -> dict[str, Any]:
    value = json.loads((workspace / ".until-loop" / "state.json").read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise RuntimeError("runtime state was not an object")
    return value


def criterion(identifier: str, text: str) -> dict[str, Any]:
    return {"id": identifier, "text": text, "basis": {"kind": "request", "reference": text}}


def contract_for(case_id: str, policy: str) -> dict[str, Any]:
    setup = PACKET_SETUP[case_id]
    return {
        "version": 1,
        "policy": policy,
        "original_request": setup["request"],
        "interpretation": setup["interpretation"],
        "criteria": [criterion(f"C{index}", text) for index, text in enumerate(setup["criteria"], start=1)],
    }


def assessment_for(
    state: Mapping[str, Any], decision: str, statuses: Mapping[str, str], *,
    next_action: str | None = None, blocker: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    action = state.get("action")
    contract = state.get("contract")
    if not isinstance(action, dict) or not isinstance(contract, dict):
        raise RuntimeError("active state is missing action or contract")
    rows: list[dict[str, str]] = []
    for item in contract.get("criteria", []):
        identifier = item.get("id") if isinstance(item, dict) else None
        if not isinstance(identifier, str) or identifier not in statuses:
            raise RuntimeError("assessment status did not cover each criterion")
        rows.append({
            "id": identifier,
            "status": statuses[identifier],
            "evidence": f"Fixture observation for {identifier}: {statuses[identifier]}.",
        })
    return {
        "action_id": action["id"],
        "contract_revision": contract["revision"],
        "decision": decision,
        "criteria": rows,
        "next_action": next_action,
        "blocker": dict(blocker) if blocker is not None else None,
    }


def submit_assessment(
    *, skill_root: Path, workspace: Path, log_dir: Path, name: str,
    assessment: Mapping[str, Any], expected: int = 0, extra_env: Mapping[str, str] | None = None,
) -> subprocess.CompletedProcess[bytes]:
    action_id = assessment.get("action_id")
    if not isinstance(action_id, str):
        raise RuntimeError("assessment has no action id")
    result_path = workspace / ".until-loop" / "results" / f"{action_id}.json"
    require_new(result_path, "assessment inbox")
    write_json(result_path, assessment)
    result = run_adapter(
        skill_root=skill_root,
        workspace=workspace,
        arguments=("submit", "--repo", str(workspace), "--action-id", action_id),
        log_dir=log_dir,
        name=name,
        extra_env=extra_env,
    )
    require_exit(result, expected, name)
    return result


def expect_state(workspace: Path, *, phase: str, recovery: str | None = None) -> dict[str, Any]:
    state = read_state(workspace)
    if state.get("phase") != phase:
        raise RuntimeError(f"fixture state phase is {state.get('phase')!r}, expected {phase!r}")
    actual_recovery = state.get("recovery")
    if recovery is None and actual_recovery is not None:
        raise RuntimeError("fixture unexpectedly has recovery state")
    if recovery is not None:
        if not isinstance(actual_recovery, dict) or actual_recovery.get("kind") != recovery:
            raise RuntimeError(f"fixture recovery is not {recovery!r}")
    return state


def packet_from_next(skill_root: Path, workspace: Path, log_dir: Path, name: str = "next") -> subprocess.CompletedProcess[bytes]:
    result = run_adapter(
        skill_root=skill_root,
        workspace=workspace,
        arguments=("next", "--repo", str(workspace)),
        log_dir=log_dir,
        name=name,
    )
    require_exit(result, 0, name)
    return result


def build_packet_fixture(case_id: str, workspace: Path, skill_root: Path, prepared: Path, policy: str) -> dict[str, Any]:
    """Build only by CLI transitions; input/result JSON is adapter input, never state."""
    log_dir = prepared / "cli-logs" / case_id
    contract_path = prepared / "adapter-inputs" / case_id / "contract.json"
    write_json(contract_path, contract_for(case_id, policy))
    setup = PACKET_SETUP[case_id]
    init_args: list[str] = ["init", "--repo", str(workspace), "--contract-file", str(contract_path)]
    if isinstance(setup.get("verify"), str):
        init_args.extend(["--verify", setup["verify"]])
    if case_id == "packet-terminal-halted":
        init_args.extend(["--max-cycles", "1"])
    initialized = run_adapter(skill_root=skill_root, workspace=workspace, arguments=init_args, log_dir=log_dir, name="init")
    require_exit(initialized, 0, "init")
    packet_result: subprocess.CompletedProcess[bytes]

    if case_id in {"packet-independent-work-missing-fact", "packet-explicit-stop-missing-source"}:
        expect_state(workspace, phase="active")
        packet_result = packet_from_next(skill_root, workspace, log_dir)
    elif case_id == "packet-weak-verifier-unmet-clause":
        state = read_state(workspace)
        submitted = submit_assessment(
            skill_root=skill_root, workspace=workspace, log_dir=log_dir, name="submit-continue",
            assessment=assessment_for(state, "continue", {"C1": "satisfied", "C2": "unsatisfied"}, next_action="Document the trimming behavior in README.md."),
        )
        del submitted
        expect_state(workspace, phase="active")
        packet_result = packet_from_next(skill_root, workspace, log_dir)
    elif case_id == "packet-failed-verifier":
        state = read_state(workspace)
        submit_assessment(
            skill_root=skill_root, workspace=workspace, log_dir=log_dir, name="submit-continue",
            assessment=assessment_for(state, "continue", {"C1": "unknown", "C2": "unsatisfied"}, next_action="Diagnose the configured verifier failure."),
        )
        state = expect_state(workspace, phase="active")
        if not isinstance(state.get("last_verify"), dict) or state["last_verify"].get("ok") is not False:
            raise RuntimeError("failed-verifier fixture did not retain a failed verifier result")
        packet_result = packet_from_next(skill_root, workspace, log_dir)
    elif case_id in {"packet-paused-user-instruction", "packet-paused-condition-observed"}:
        state = read_state(workspace)
        resume_on = str(setup["resume_on"])
        assessment = assessment_for(
            state,
            "blocked",
            {"C1": "unknown"},
            blocker={
                "reason": "The required source or authorization was not available when assessed.",
                "resumption_condition": "The recorded condition is actually observed through the adapter.",
                "resume_on": resume_on,
            },
        )
        submit_assessment(skill_root=skill_root, workspace=workspace, log_dir=log_dir, name="submit-blocked", assessment=assessment)
        expect_state(workspace, phase="paused")
        if case_id == "packet-paused-condition-observed":
            write_text(workspace / "authoritative-source.txt", "Observed authoritative source: ready for adapter-authorized resume.\n")
        packet_result = packet_from_next(skill_root, workspace, log_dir)
    elif case_id == "packet-terminal-done":
        state = read_state(workspace)
        submit_assessment(
            skill_root=skill_root, workspace=workspace, log_dir=log_dir, name="submit-complete",
            assessment=assessment_for(state, "complete", {"C1": "satisfied"}),
        )
        expect_state(workspace, phase="done")
        packet_result = packet_from_next(skill_root, workspace, log_dir)
    elif case_id == "packet-terminal-halted":
        state = read_state(workspace)
        submit_assessment(
            skill_root=skill_root, workspace=workspace, log_dir=log_dir, name="submit-continue",
            assessment=assessment_for(state, "continue", {"C1": "unsatisfied"}, next_action="Resolve the open issue."),
        )
        expect_state(workspace, phase="halted")
        packet_result = packet_from_next(skill_root, workspace, log_dir)
    elif case_id == "packet-rejected-current-assessment":
        state = read_state(workspace)
        packet_result = submit_assessment(
            skill_root=skill_root, workspace=workspace, log_dir=log_dir, name="submit-rejected",
            assessment=assessment_for(state, "complete", {"C1": "satisfied", "C2": "unsatisfied"}),
            expected=2,
        )
        expect_state(workspace, phase="active")
    elif case_id == "packet-uncertain-verifier":
        state = read_state(workspace)
        submit_assessment(
            skill_root=skill_root,
            workspace=workspace,
            log_dir=log_dir,
            name="submit-deliberate-verifier-interruption",
            assessment=assessment_for(state, "continue", {"C1": "unknown"}, next_action="Inspect the verifier effect before any replay."),
            expected=70,
            extra_env={"UNTIL_LOOP_V2_CRASH_AFTER_VERIFIER": "1"},
        )
        expect_state(workspace, phase="active", recovery="verifier_uncertain")
        packet_result = packet_from_next(skill_root, workspace, log_dir, "next-recovery")
    elif case_id == "packet-truncated-contract":
        state = read_state(workspace)
        statuses = {f"C{index}": ("unsatisfied" if index == 13 else "satisfied") for index in range(1, 14)}
        submit_assessment(
            skill_root=skill_root,
            workspace=workspace,
            log_dir=log_dir,
            name="submit-continue",
            assessment=assessment_for(state, "continue", statuses, next_action="Reassess the full recorded criteria using current evidence."),
        )
        expect_state(workspace, phase="active")
        packet_result = packet_from_next(skill_root, workspace, log_dir)
    else:
        raise AssertionError(f"unhandled packet fixture: {case_id}")

    packet_path = prepared / "packets" / f"{case_id}.txt"
    require_new(packet_path, "prepared packet")
    write_text(packet_path, packet_result.stdout.decode("utf-8", errors="replace"))
    if not packet_path.read_text(encoding="utf-8").strip():
        raise RuntimeError(f"CLI produced no packet for {case_id}")
    return {
        "case_id": case_id,
        "packet_path": str(packet_path),
        "packet_command": str((log_dir / ("submit-rejected.json" if case_id == "packet-rejected-current-assessment" else "next.json" if case_id != "packet-uncertain-verifier" else "next-recovery.json")).resolve()),
        "state_hash": sha256_bytes((workspace / ".until-loop" / "state.json").read_bytes()),
        "workspace_hash": tree_hash(workspace),
    }


def prepare(review_root: Path, label: str, skill_root: Path | None, case_ids: Sequence[str]) -> Path:
    """Freeze/select a candidate and build durable packet/NL fixtures without hosts."""
    label = safe_component(label, "label")
    cases = load_cases()
    selected = select_cases(cases, case_ids)
    snapshot, source_record = ensure_source_snapshot(review_root, label, skill_root)
    prepared = review_root / "prepared" / label
    require_new(prepared, "prepared fixture root")
    prepared.mkdir(parents=True)
    policy = policy_version(snapshot)
    manifest: list[dict[str, Any]] = []
    for case in selected:
        case_id = str(case["id"])
        workspace = prepared / "workspaces" / opaque_workspace_name(cases, case_id)
        workspace.mkdir(parents=True)
        materialize_files(workspace, case["files"])
        entry: dict[str, Any] = {
            "id": case_id,
            "mode": case["mode"],
            "workspace": str(workspace.resolve()),
        }
        if case["mode"] == "packet":
            entry.update(build_packet_fixture(case_id, workspace, snapshot, prepared, policy))
        else:
            entry["request"] = case["request"]
            entry["raw_facts"] = case["raw_facts"]
            entry["workspace_hash"] = tree_hash(workspace)
        manifest.append(entry)
    write_json(
        prepared / "manifest.json",
        {
            "format": "until-loop-v2-fresh-context-prepared/v1",
            "created_utc": utc_now(),
            "source_label": label,
            "source_snapshot": str(snapshot.resolve()),
            "source_snapshot_hash": tree_hash(snapshot),
            "source_record": source_record,
            "policy_version": policy,
            "max_workers": MAX_WORKERS,
            "timeout_seconds": TIMEOUT_SECONDS,
            "cases": manifest,
            "note": "Packets and durable state were generated through the selected public v2 CLI. No model workers ran during preparation.",
        },
    )
    return prepared / "manifest.json"


def worker_prompt(case: Mapping[str, Any], snapshot: Path, packet: str | None) -> tuple[str, dict[str, Any]]:
    workspace = str(case["workspace"])
    if case["mode"] == "packet":
        assert packet is not None
        return (
            "You are entering a saved until-loop action from a new context. Work only in the bound "
            f"workspace `{workspace}`. Do not read a skill card or search for an adapter outside paths "
            "explicitly linked by the packet. You may use local read-only shell or file inspection in the bound "
            "workspace; do not edit files, use network or external services, or send messages. Based only on the "
            "packet and current workspace, state what you would do next. "
            "Predict the executing agent's next action under the original task authorization; this probe's "
            "read-only restriction means describe that action, never execute it. "
            "Return the required JSON with your immediate next action, decision, evidence, and the next applicable "
            "runtime call. Use `runtime_command_argv` for an argv array or null when no call is permitted; use "
            "`runtime_input_json` for a serialized input record or null when unknown or unavailable; state the "
            "precondition for that call. Do not self-grade.\n\n"
            "Actual CLI-generated packet:\n"
            f"{packet}",
            PACKET_OUTPUT_SCHEMA,
        )
    return (
        "Use the candidate until-loop skill at "
        f"`{snapshot / 'SKILL.md'}`. Its only bound workspace is `{workspace}`. You may read that skill and "
        "adapter/reference files under the same frozen skill root, plus files in the bound workspace. Do not "
        "access another project, tests, expectations, network, external services, or messages; do not edit files. "
        "Local read-only shell or file inspection is allowed only in the bound workspace and frozen skill root. "
        "Derive the task contract and the immediate decision from the exact request and raw facts. Return the "
        "original task contract without adding this probe's read-only restriction as a new task condition. Return the "
        "required JSON. Its `contract_json` field must be a serialized complete runtime contract JSON object with "
        "version, policy, original_request, interpretation, and criteria; every criterion must include id, text, "
        "and basis. Also cover execute, continue, success, early stop, initial decision, immediate next action, "
        "and evidence. Do not self-grade.\n\n"
        "Exact user request:\n"
        f"{case['request']}\n\n"
        "Raw facts:\n"
        f"{case['raw_facts']}",
        NL_OUTPUT_SCHEMA,
    )


def terminate_process(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    try:
        if os.name != "nt":
            os.killpg(process.pid, signal.SIGTERM)
        else:
            process.terminate()
        process.wait(timeout=10)
        return
    except (OSError, subprocess.TimeoutExpired):
        pass
    try:
        if os.name != "nt":
            os.killpg(process.pid, signal.SIGKILL)
        else:
            process.kill()
    except OSError:
        process.kill()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=10)


def event_counters(events: Path) -> dict[str, Any]:
    counts: dict[str, int] = {"lines": 0, "json_lines": 0}
    kinds: dict[str, int] = {}
    for line in events.read_text(encoding="utf-8", errors="replace").splitlines():
        counts["lines"] += 1
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        counts["json_lines"] += 1
        if isinstance(item, dict) and isinstance(item.get("type"), str):
            kinds[item["type"]] = kinds.get(item["type"], 0) + 1
    return {"counts": counts, "event_types": kinds}


def structural_final(path: Path, mode: str) -> dict[str, Any]:
    if not path.is_file():
        return {"present": False, "json_object": False, "minimal_schema_valid": False}
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        return {"present": True, "json_object": False, "minimal_schema_valid": False, "error": str(error)}
    if not isinstance(parsed, dict):
        return {"present": True, "json_object": False, "minimal_schema_valid": False}
    expected = PACKET_OUTPUT_SCHEMA if mode == "packet" else NL_OUTPUT_SCHEMA
    required = set(expected["required"])
    exact_keys = set(parsed) == required
    types_ok = all(isinstance(parsed.get(key), str) for key in required if key not in {"runtime_command_argv", "runtime_input_json"})
    if mode == "packet":
        command = parsed.get("runtime_command_argv")
        command_ok = command is None or (isinstance(command, list) and all(isinstance(item, str) for item in command))
        input_ok = parsed.get("runtime_input_json") is None or isinstance(parsed.get("runtime_input_json"), str)
        types_ok = types_ok and command_ok and input_ok
    return {
        "present": True,
        "json_object": True,
        "keys": sorted(parsed),
        "exact_required_keys": exact_keys,
        "required_field_types": types_ok,
        "minimal_schema_valid": exact_keys and types_ok,
    }


def run_host(
    case: Mapping[str, Any], snapshot: Path, run_root: Path,
    prompt_spec: tuple[str, dict[str, Any]] | None = None,
    timeout_seconds: int = TIMEOUT_SECONDS,
) -> dict[str, Any]:
    if type(timeout_seconds) is not int or not 1 <= timeout_seconds <= 600:
        raise ValueError("host timeout must be an integer from 1 through 600 seconds")
    case_id = str(case["id"])
    host_dir = run_root / case_id
    require_new(host_dir, "host result directory")
    host_dir.mkdir(parents=True)
    workspace = Path(str(case["workspace"]))
    packet = None
    if case["mode"] == "packet":
        packet = Path(str(case["packet_path"])).read_text(encoding="utf-8")
        write_text(host_dir / "packet.txt", packet)
    prompt, schema = prompt_spec if prompt_spec is not None else worker_prompt(case, snapshot, packet)
    write_text(host_dir / "prompt.txt", prompt)
    write_json(host_dir / "output-schema.json", schema)
    workspace_before = snapshot_tree(workspace)
    source_before = snapshot_tree(snapshot)
    write_json(host_dir / "workspace-before.json", workspace_before)
    write_json(host_dir / "candidate-source-before.json", source_before)
    events = host_dir / "events.jsonl"
    stderr = host_dir / "stderr.txt"
    final = host_dir / "final.json"
    argv = [
        "codex", "exec", "--ephemeral", "--json", "--color", "never", "--skip-git-repo-check",
        "--sandbox", "read-only", "--cd", str(workspace), "--output-schema", str(host_dir / "output-schema.json"),
        "--output-last-message", str(final), prompt,
    ]
    started = time.monotonic()
    started_utc = utc_now()
    timed_out = False
    with events.open("wb") as stdout_handle, stderr.open("wb") as stderr_handle:
        process = subprocess.Popen(argv, cwd=workspace, stdout=stdout_handle, stderr=stderr_handle, start_new_session=True)
        try:
            returncode = process.wait(timeout=timeout_seconds)
        except subprocess.TimeoutExpired:
            timed_out = True
            terminate_process(process)
            returncode = process.returncode
    workspace_after = snapshot_tree(workspace)
    source_after = snapshot_tree(snapshot)
    write_json(host_dir / "workspace-after.json", workspace_after)
    write_json(host_dir / "candidate-source-after.json", source_after)
    counters = event_counters(events)
    write_json(host_dir / "counters.json", counters)
    record = {
        "case_id": case_id,
        "mode": case["mode"],
        "workspace": str(workspace),
        "candidate_snapshot": str(snapshot),
        "candidate_source_hash_before": sha256_bytes(canonical_json(source_before)),
        "candidate_source_hash_after": sha256_bytes(canonical_json(source_after)),
        "candidate_source_drift": source_before != source_after,
        "workspace_hash_before": sha256_bytes(canonical_json(workspace_before)),
        "workspace_hash_after": sha256_bytes(canonical_json(workspace_after)),
        "workspace_drift": workspace_before != workspace_after,
        "argv": argv[:-1] + ["<recorded in prompt.txt>"],
        "started_utc": started_utc,
        "ended_utc": utc_now(),
        "timeout_seconds": timeout_seconds,
        "wall_time_seconds": round(time.monotonic() - started, 3),
        "returncode": returncode,
        "timed_out": timed_out,
        "structural_final": structural_final(final, str(case["mode"])),
        "counters": counters,
        "note": "No semantic grading is performed here; retained transcripts and JSON are authoritative.",
    }
    write_json(host_dir / "run.json", record)
    return record


def load_prepared(review_root: Path, label: str) -> tuple[dict[str, Any], Path]:
    prepared = review_root / "prepared" / label
    manifest_path = prepared / "manifest.json"
    if not manifest_path.is_file():
        raise RuntimeError(f"prepared manifest is missing: {manifest_path}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("format") != "until-loop-v2-fresh-context-prepared/v1":
        raise RuntimeError("unsupported prepared manifest")
    snapshot = Path(str(manifest.get("source_snapshot", "")))
    required_snapshot_paths(snapshot)
    expected_hash = manifest.get("source_snapshot_hash")
    current_hash = tree_hash(snapshot)
    if not isinstance(expected_hash, str) or current_hash != expected_hash:
        raise RuntimeError("prepared candidate snapshot drifted; prepare a new immutable label")
    return manifest, snapshot


def run(review_root: Path, label: str, run_id: str, case_ids: Sequence[str]) -> Path:
    """Run selected fresh hosts with fixed concurrency and no semantic verdict."""
    label = safe_component(label, "label")
    run_id = safe_component(run_id, "run-id")
    manifest, snapshot = load_prepared(review_root, label)
    listed = {str(case["id"]): case for case in manifest.get("cases", []) if isinstance(case, dict)}
    selected = select_cases(listed, case_ids)
    destination = review_root / "runs" / label / run_id
    require_new(destination, "run root")
    destination.mkdir(parents=True)
    write_json(
        destination / "run-manifest.json",
        {
            "created_utc": utc_now(),
            "label": label,
            "run_id": run_id,
            "source_snapshot": str(snapshot),
            "source_snapshot_hash": tree_hash(snapshot),
            "selected_cases": [case["id"] for case in selected],
            "max_workers": MAX_WORKERS,
            "timeout_seconds": TIMEOUT_SECONDS,
            "codex_argv_contract": ["codex", "exec", "--ephemeral", "--json", "--sandbox", "read-only", "--output-schema"],
            "note": "Workers have no model or configuration override. This run records evidence and structural outcomes only.",
        },
    )
    records: list[dict[str, Any]] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        future_map = {executor.submit(run_host, case, snapshot, destination): str(case["id"]) for case in selected}
        for future in concurrent.futures.as_completed(future_map):
            case_id = future_map[future]
            try:
                records.append(future.result())
            except Exception as error:
                records.append({"case_id": case_id, "harness_error": repr(error)})
    write_json(destination / "run-summary.json", {"records": sorted(records, key=lambda item: item["case_id"])})
    failed = any(item.get("harness_error") or item.get("returncode") != 0 or item.get("timed_out") for item in records)
    if failed:
        raise RuntimeError(f"one or more hosts did not complete cleanly; evidence is retained in {destination}")
    return destination


def summarize(review_root: Path, label: str, run_id: str) -> Path:
    """Produce a structural summary only; no response content is semantically scored."""
    label = safe_component(label, "label")
    run_id = safe_component(run_id, "run-id")
    run_root = review_root / "runs" / label / run_id
    if not run_root.is_dir():
        raise RuntimeError(f"run root is missing: {run_root}")
    records: list[dict[str, Any]] = []
    for path in sorted(run_root.glob("*/run.json")):
        records.append(json.loads(path.read_text(encoding="utf-8")))
    summary = {
        "created_utc": utc_now(),
        "label": label,
        "run_id": run_id,
        "hosts": len(records),
        "returncodes": {record["case_id"]: record.get("returncode") for record in records},
        "timeouts": [record["case_id"] for record in records if record.get("timed_out")],
        "candidate_source_drift": [record["case_id"] for record in records if record.get("candidate_source_drift")],
        "workspace_drift": [record["case_id"] for record in records if record.get("workspace_drift")],
        "structural_output_failures": [
            record["case_id"] for record in records if not record.get("structural_final", {}).get("minimal_schema_valid")
        ],
        "records": records,
        "note": "This is a process and evidence inventory. It intentionally does not score decisions, evidence, or semantic completion.",
    }
    path = run_root / "structural-summary.json"
    require_new(path, "structural summary")
    write_json(path, summary)
    return path


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--review-root", type=Path, default=DEFAULT_REVIEW_ROOT)
    sub = parser.add_subparsers(dest="command", required=True)
    prepare_parser = sub.add_parser("prepare", help="freeze/select source and prepare fixtures without model workers")
    prepare_parser.add_argument("--label", required=True, help="immutable source label, normally before or after")
    prepare_parser.add_argument("--skill-root", type=Path, help="candidate source to freeze when label is not already frozen")
    prepare_parser.add_argument("--case", action="append", default=[], help="prepare only a selected case (repeatable)")
    run_parser = sub.add_parser("run", help="run selected read-only fresh workers")
    run_parser.add_argument("--label", required=True)
    run_parser.add_argument("--run-id", required=True, help="new evidence run identifier")
    run_parser.add_argument("--case", action="append", default=[], help="run only a selected prepared case (repeatable)")
    summary_parser = sub.add_parser("summarize", help="write structural evidence summary")
    summary_parser.add_argument("--label", required=True)
    summary_parser.add_argument("--run-id", required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "prepare":
            prepared = prepare(args.review_root, args.label, args.skill_root, args.case)
            print(prepared)
        elif args.command == "run":
            destination = run(args.review_root, args.label, args.run_id, args.case)
            print(destination)
        else:
            summary = summarize(args.review_root, args.label, args.run_id)
            print(summary)
    except (OSError, RuntimeError, ValueError, subprocess.TimeoutExpired) as error:
        parser.error(str(error))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
