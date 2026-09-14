#!/usr/bin/env python3
"""Recover factual evidence after an Improve observer crashes.

This helper never resumes or evaluates Improve.  It reads retained host output,
captures one or more stable final snapshots, and writes an explicitly
incomplete observation.  The original host's exit status is unknowable after a
crash and is therefore always recorded as ``null``.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from typing import Any

try:
    from . import improve_quality as quality
    from .improve_quality_fixtures import run_oracle
except ImportError:  # Direct ``python tests/improve_quality_recover.py`` use.
    import improve_quality as quality
    from improve_quality_fixtures import run_oracle


FORMAT = "improve-quality-observer-recovery/v1"
MAX_CAPTURE_ATTEMPTS = 3


class RecoveryError(RuntimeError):
    """The retained trial cannot be recovered without changing its meaning."""


def _json_object(path: Path, label: str) -> dict[str, Any]:
    if not path.is_file() or path.is_symlink():
        raise RecoveryError(label + " must be a regular file")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise RecoveryError(label + " is not valid JSON") from error
    if not isinstance(value, dict):
        raise RecoveryError(label + " must contain an object")
    return value


def _optional_regular_bytes(path: Path) -> bytes | None:
    if not path.exists():
        return None
    info = path.lstat()
    if not info or not path.is_file() or path.is_symlink():
        raise RecoveryError("candidate durable state is not a regular file: " + str(path))
    return path.read_bytes()


def active_host_processes(workspace: Path) -> list[dict[str, str]]:
    """Return live Codex hosts explicitly rooted at this candidate, if any."""
    try:
        result = subprocess.run(
            ["ps", "-axo", "pid=,command="], stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, check=False, timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise RecoveryError("cannot verify whether a Codex host is active") from error
    if result.returncode:
        raise RecoveryError("cannot verify whether a Codex host is active")
    candidate = str(workspace.resolve())
    found = []
    for line in result.stdout.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        pid, _, command = stripped.partition(" ")
        if "codex" in command and candidate in command:
            found.append({"pid": pid, "command": command})
    return found


def _next_snapshot_number(evidence: Path, snapshots: list[dict[str, Any]]) -> int:
    numbers = []
    for snapshot in snapshots:
        identifier = snapshot.get("id") if isinstance(snapshot, dict) else None
        if isinstance(identifier, str) and identifier.startswith("s") and identifier[1:].isdigit():
            numbers.append(int(identifier[1:]))
    directory = evidence / "snapshots"
    if directory.is_dir() and not directory.is_symlink():
        for path in directory.iterdir():
            if path.name.startswith("s") and path.name[1:].isdigit():
                numbers.append(int(path.name[1:]))
    return max(numbers, default=-1) + 1


def _stable_final_capture(workspace: Path, evidence: Path, snapshots: list[dict[str, Any]]) -> dict[str, Any]:
    """Add a new stable final snapshot, retaining every failed/partial attempt."""
    last_error = None
    for _ in range(MAX_CAPTURE_ATTEMPTS):
        number = _next_snapshot_number(evidence, snapshots)
        try:
            captured = quality.capture(workspace, evidence, number, "observer recovery final capture")
        except (OSError, ValueError, RuntimeError) as error:
            last_error = str(error)
            continue
        snapshots.append(captured)
        if captured.get("stable") is True:
            return captured
        last_error = "candidate changed during observer recovery capture"
    raise RecoveryError("could not create a stable final snapshot: " + str(last_error))


def recover(root: Path, reason: str) -> dict[str, Any]:
    """Write an incomplete recovered observation without changing candidate state."""
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError("recovery reason must be non-empty text")
    root = root.resolve()
    manifest = _json_object(root / "trial.json", "trial manifest")
    evidence = Path(str(manifest.get("evidence", ""))).resolve()
    workspace = Path(str(manifest.get("workspace", ""))).resolve()
    source = Path(str(manifest.get("source", {}).get("path", ""))).resolve()
    if not workspace.is_dir() or workspace.is_symlink():
        raise RecoveryError("candidate workspace is missing or unsafe")
    if not evidence.is_dir() or evidence.is_symlink():
        raise RecoveryError("evidence directory is missing or unsafe")
    if (evidence / "observed.json").exists() or (evidence / "observed.json").is_symlink():
        raise RecoveryError("refusing to overwrite an existing observation")
    if not (evidence / "invocation.json").is_file() or (evidence / "invocation.json").is_symlink():
        raise RecoveryError("recovery requires an existing invocation marker")
    active = active_host_processes(workspace)
    if active:
        raise RecoveryError("refusing recovery while a Codex host is active for candidate: " + json.dumps(active))
    return _recover_from_snapshots(root, reason)


def _recover_from_snapshots(root: Path, reason: str) -> dict[str, Any]:
    """Internal implementation kept separate so the list parser is explicit."""
    manifest = _json_object(root / "trial.json", "trial manifest")
    evidence = Path(str(manifest["evidence"])).resolve()
    workspace = Path(str(manifest["workspace"])).resolve()
    source = Path(str(manifest["source"]["path"])).resolve()
    snapshots_path = evidence / "snapshots.json"
    try:
        snapshots = json.loads(snapshots_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise RecoveryError("retained snapshots are not valid JSON") from error
    if not isinstance(snapshots, list) or not all(isinstance(item, dict) for item in snapshots):
        raise RecoveryError("retained snapshots must be a JSON list of objects")
    if not snapshots:
        raise RecoveryError("retained snapshots must include the baseline capture")

    durable_before = {
        name: _optional_regular_bytes(workspace / ".until-loop" / name)
        for name in ("state.json", "history.jsonl")
    }
    captured = _stable_final_capture(workspace, evidence, snapshots)
    quality.write_json(snapshots_path, snapshots)
    durable_after = {
        name: _optional_regular_bytes(workspace / ".until-loop" / name)
        for name in ("state.json", "history.jsonl")
    }
    if durable_before != durable_after:
        raise RecoveryError("recovery unexpectedly changed candidate durable state")
    active_after = active_host_processes(workspace)
    if active_after:
        raise RecoveryError("Codex host became active during recovery: " + json.dumps(active_after))

    events_path = evidence / "events.jsonl"
    if not events_path.is_file() or events_path.is_symlink():
        raise RecoveryError("retained event stream is missing or unsafe")
    events = quality.read_events(events_path, complete=True)
    refs = quality.retain_transcript(events, evidence)
    # Keep the same privacy-preserving normalization as the ordinary runner.
    events_path.write_text("".join(json.dumps(event) + "\n" for event in events), encoding="utf-8")
    oracle = run_oracle(str(manifest["case_id"]), workspace)
    quality.write_json(evidence / "final-oracle.json", oracle)
    snapshot_oracles = {
        snapshot["id"]: run_oracle(str(manifest["case_id"]), evidence / "snapshots" / snapshot["id"] / "candidate")
        for snapshot in snapshots if snapshot.get("stable") is True
    }
    quality.write_json(evidence / "snapshot-oracles.json", snapshot_oracles)
    reads = quality.source_read_evidence(events, source)
    source_manifest = manifest.get("source", {})
    source_integrity = (
        isinstance(source_manifest, dict)
        and quality.digest(quality.canonical(quality.tree(source))) == source_manifest.get("digest")
        and all(reads.values())
        and not quality.forbidden_accesses(events, evidence)
    )
    parse_errors = [event for event in events if event.get("type") == "evaluator.parse_error"]
    baseline = manifest.get("fixture", {}).get("protected", {})
    observed = {
        "snapshots": snapshots,
        "evidence_refs": refs + [path for snapshot in snapshots for path in snapshot.get("relative_evidence_paths", [])] +
        ["snapshot-oracles.json", "final-oracle.json", "baseline-oracle.json"],
        "runtime_phase": quality.state_value(workspace).get("phase", "absent"),
        "behavior_passed": oracle.get("passed", False),
        "protected_preserved": quality.preserved(workspace, baseline),
        "invocation_count": 1,
        "source_integrity": source_integrity,
        "source_read_evidence": reads,
        "trial_status": "incomplete",
        "elapsed_seconds": None,
        "returncode": None,
        "stop_reason": "observer_recovery",
        "capture_errors": ["observer recovery: " + reason] +
        (["structured event stream contains parse errors"] if parse_errors else []),
        "isolation": manifest.get("isolation", "unsealed same-host"),
        "contamination_status": "unsealed; recovered observer evidence; trace audit still required",
        "forbidden_access_event_ids": quality.forbidden_accesses(events, evidence),
        "baseline_oracle": json.loads((evidence / "baseline-oracle.json").read_text(encoding="utf-8")),
        "final_oracle": oracle,
        "recovery": {
            "format": FORMAT,
            "reason": reason,
            "recovered_at": time.time(),
            "original_process_result": "unknown",
            "original_returncode": None,
            "final_snapshot_id": captured["id"],
            "candidate_state_history_unchanged": True,
            "active_host_checked": True,
        },
    }
    quality.write_json(evidence / "observed.json", observed)
    return observed


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    command = commands.add_parser("recover")
    command.add_argument("--root", type=Path, required=True)
    command.add_argument("--reason", required=True)
    args = parser.parse_args()
    try:
        result = recover(args.root, args.reason)
    except (RecoveryError, ValueError) as error:
        parser.error(str(error))
    print(json.dumps({key: result[key] for key in ("trial_status", "runtime_phase", "returncode", "recovery")}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
