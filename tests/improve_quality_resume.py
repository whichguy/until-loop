#!/usr/bin/env python3
"""Legacy-v2 controlled-resume support for the opt-in Improve quality experiment.

This historical protocol harness is deliberately an evaluator controller, not a second Improve state
machine.  It never creates, rewrites, or submits until-loop state/history.
It can act only after a live agent has recorded the one-time dependency pause
described by :func:`request_augmentation`.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import argparse
from typing import Any, Mapping

try:
    from . import improve_quality_fixtures as fixtures
except ImportError:  # Direct ``python tests/improve_quality_resume.py`` use.
    import improve_quality_fixtures as fixtures


FORMAT = "until-loop-improve-quality-controlled-resume/v1"
FIRST_REVIEW_HEADING = "## Controlled-resume first review"
CONTEXT_FILENAME = "review-context.txt"
CONTROLLER_DIRNAME = "controlled-resume"
CONTINUATION_DIRNAME = "controlled-resume-continuation"
COMBINED_DIRNAME = "controlled-resume-combined"


class ResumeControllerError(RuntimeError):
    """The evaluator cannot safely perform the requested intervention."""


def request_augmentation() -> str:
    """Return the Q4-only public dependency boundary for a clean control.

    This deliberately asks for a pause rather than fabricating a review in the
    controller.  The agent still decides whether the first review is complete
    and must leave durable, candidate-bound evidence before it can pause.
    """
    return "\n\n".join((
        "Controlled-resume evaluation boundary (this clean-control trial only):",
        "Complete one substantive Improve review under the selected card before "
        "starting another review. Record that completed review in "
        "`.until-loop/working.md` under the exact heading "
        f"`{FIRST_REVIEW_HEADING}`, including Scope:, Evidence:, and "
        "Classification:. This is evidence to be audited, not a claim that a "
        "callback is a review.",
        f"Before the next review, inspect the public workspace dependency "
        f"`{CONTEXT_FILENAME}`. It is initially absent. If it remains absent, "
        "pause the existing until-loop task with `resume_on: condition_observed` "
        "until that file is restored. The existing task already authorizes "
        "resumption when that observed dependency is restored; do not treat this "
        "as a user stop and do not invent a host-specific prerequisite.",
        "After a valid resume, read the current packet and the restored public "
        "context, then continue the same task through the package adapter. Do "
        "not count pausing, resuming, a callback, or a verifier invocation as "
        "another review.",
    ))


def augmented_request(request: str) -> str:
    """Append the Q4 boundary without changing the fixture's ordinary request."""
    if not isinstance(request, str) or not request.strip():
        raise ValueError("request must be non-empty text")
    return request.rstrip() + "\n\n" + request_augmentation() + "\n"


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _quality() -> Any:
    """Load the opt-in runner only when a wrapper command needs it."""
    try:
        from . import improve_quality
    except ImportError:
        import improve_quality
    return improve_quality


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ResumeControllerError(f"expected object in {path}")
    return value


def _regular_bytes(path: Path, label: str) -> bytes:
    try:
        info = path.lstat()
    except FileNotFoundError as error:
        raise ResumeControllerError(f"{label} is absent") from error
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise ResumeControllerError(f"{label} must be a regular single-link file")
    return path.read_bytes()


def _load_json(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(_regular_bytes(path, label).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ResumeControllerError(f"{label} is not valid JSON") from error
    if not isinstance(value, dict):
        raise ResumeControllerError(f"{label} must contain a JSON object")
    return value


def _history(path: Path) -> list[dict[str, Any]]:
    raw = _regular_bytes(path, "until-loop history")
    result: list[dict[str, Any]] = []
    try:
        for line in raw.decode("utf-8").splitlines():
            if not line:
                continue
            value = json.loads(line)
            if not isinstance(value, dict):
                raise ValueError("history event is not an object")
            result.append(value)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise ResumeControllerError("until-loop history is malformed") from error
    return result


def _durable_first_review(workspace: Path) -> tuple[bool, str]:
    path = workspace / ".until-loop" / "working.md"
    try:
        text = _regular_bytes(path, "first-review notebook").decode("utf-8")
    except (ResumeControllerError, UnicodeDecodeError) as error:
        return False, str(error)
    if FIRST_REVIEW_HEADING not in text:
        return False, "first-review notebook lacks the controlled-resume heading"
    heading = text.split(FIRST_REVIEW_HEADING, 1)[1]
    missing = [label for label in ("Scope:", "Evidence:", "Classification:") if label not in heading]
    if missing:
        return False, "first-review notebook lacks " + ", ".join(missing)
    return True, "durable first-review record present"


def _state_and_pause(workspace: Path) -> tuple[dict[str, Any] | None, str | None]:
    state_path = workspace / ".until-loop" / "state.json"
    history_path = workspace / ".until-loop" / "history.jsonl"
    try:
        state = _load_json(state_path, "until-loop state")
        history = _history(history_path)
    except ResumeControllerError as error:
        return None, str(error)
    pause = state.get("pause")
    if state.get("version") != 2 or state.get("phase") != "paused":
        return None, "runtime is not in the paused v2 phase"
    if not isinstance(pause, dict) or pause.get("resume_on") != "condition_observed":
        return None, "pause does not authorize condition_observed resumption"
    if not any(event.get("type") == "assessment" and event.get("decision") == "blocked" for event in history):
        return None, "paused runtime lacks a blocked assessment receipt"
    return state, None


def _git(workspace: Path, *arguments: str) -> str:
    environment = dict(os.environ)
    for key in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_TEMPLATE_DIR"):
        environment.pop(key, None)
    environment.update({"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
                        "GIT_TERMINAL_PROMPT": "0", "LC_ALL": "C", "LANG": "C"})
    result = subprocess.run(["git", *arguments], cwd=workspace, env=environment,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            check=False, timeout=30)
    if result.returncode:
        detail = result.stderr.decode("utf-8", "replace").strip()
        raise ResumeControllerError("git " + " ".join(arguments) + " failed: " + detail)
    return result.stdout.decode("utf-8", "replace")


def _atomic_new_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump(value, handle, indent=2, sort_keys=True)
        handle.write("\n")


def _write_new_context(workspace: Path) -> Path:
    path = workspace / CONTEXT_FILENAME
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(
            "# Restored review context\n\n"
            "The current public README contract is authoritative. Review the "
            "current candidate and its visible requirements before deciding "
            "whether further work is warranted.\n"
        )
    return path


def _protected_work_preserved(before: Mapping[str, Any], after: Mapping[str, Any]) -> bool:
    """Compare fixture-owned user work while allowing the public context file."""
    try:
        for kind in ("staged", "unstaged", "untracked"):
            if after[kind] != before[kind]:
                return False
            path = before[kind]["path"]
            if after["index"]["entries"].get(path) != before["index"]["entries"].get(path):
                return False
            before_status = [line for line in before["index"]["status_porcelain"].splitlines() if line[3:] == path]
            after_status = [line for line in after["index"]["status_porcelain"].splitlines() if line[3:] == path]
            if before_status != after_status:
                return False
    except (KeyError, TypeError):
        return False
    return True


def _neutral_resume_prompt(card_path: Path, workspace: Path, provenance_path: Path) -> str:
    return (
        "Resume the existing Improve task in this workspace using the exact frozen "
        f"package at {card_path}. Read its card, policy, current until-loop packet, "
        f"and the restored public {CONTEXT_FILENAME}. The recorded dependency is now "
        "observed. Use the evaluator-provided condition-observed provenance at "
        f"{provenance_path} only through the package's normal v2 resume protocol, "
        "then reassess the same task. Do not treat the resume action, callback, or "
        "verification as a completed review."
    )


def intervene_if_ready(
    workspace: Path,
    evidence: Path,
    fixture: Mapping[str, Any],
    card_path: Path,
    *,
    protocol_only: bool = False,
) -> dict[str, Any]:
    """Restore Q4's public dependency only after an actual authorized pause.

    Returns ``not_exercised`` without changing the candidate whenever the live
    run did not produce the required pause and durable first-review record.
    ``protocol_only`` exists solely for deterministic runtime tests; it labels
    their report and never upgrades synthetic setup into agent evidence.
    """
    workspace, evidence, card_path = workspace.resolve(), evidence.resolve(), card_path.resolve()
    result: dict[str, Any] = {
        "format": FORMAT,
        "status": "not_exercised",
        "mode": "protocol-only" if protocol_only else "live-agent-controlled",
        "workspace": str(workspace),
    }
    if fixture.get("case_id") != "clean_control":
        result["reason"] = "controlled resume is defined only for the clean_control fixture"
        return result
    marker = evidence / CONTROLLER_DIRNAME / "intervention.json"
    if marker.exists() or marker.is_symlink():
        result["status"] = "already_intervened"
        result["reason"] = "one-time controlled-resume boundary was already recorded"
        return result
    if (workspace / CONTEXT_FILENAME).exists() or (workspace / CONTEXT_FILENAME).is_symlink():
        result["reason"] = "public review context is already present before intervention"
        return result
    state, reason = _state_and_pause(workspace)
    if state is None:
        result["reason"] = reason
        return result
    has_review, review_reason = _durable_first_review(workspace)
    if not has_review:
        result["reason"] = review_reason
        return result
    formatter = workspace / "formatter.py"
    try:
        before_formatter = _regular_bytes(formatter, "formatter source")
    except ResumeControllerError as error:
        result["reason"] = str(error)
        return result
    if b"Anonymous" not in before_formatter:
        result["reason"] = "clean-control formatter is no longer at the documented fallback behavior"
        return result
    state_path = workspace / ".until-loop" / "state.json"
    history_path = workspace / ".until-loop" / "history.jsonl"
    state_before, history_before = _regular_bytes(state_path, "until-loop state"), _regular_bytes(history_path, "until-loop history")
    protected_before = fixtures.protected_state(workspace)

    # The controller's only product intervention is a visible regression in an
    # evaluator-scoped commit. ``--only`` avoids absorbing the fixture's staged
    # user draft; the context file remains a public untracked dependency.
    _write_new_context(workspace)
    formatter.write_text(
        "def format_name(value: str) -> str:\n"
        "    \"\"\"Trim a display name and return its documented fallback.\"\"\"\n"
        "    return value.strip()\n",
        encoding="utf-8",
    )
    _git(workspace, "add", "--", "formatter.py")
    _git(workspace, "commit", "--quiet", "--no-gpg-sign", "--no-verify", "--only",
         "-m", "test(controlled-resume): introduce visible formatter regression",
         "--", "formatter.py")
    if not _protected_work_preserved(protected_before, fixtures.protected_state(workspace)):
        raise ResumeControllerError("controller failed to preserve unrelated fixture work")
    if _regular_bytes(state_path, "until-loop state") != state_before or _regular_bytes(history_path, "until-loop history") != history_before:
        raise ResumeControllerError("controller must not modify until-loop state or history")

    provenance_path = evidence / CONTROLLER_DIRNAME / "condition-observed.json"
    provenance = {
        "provenance": {
            "kind": "condition_observed",
            "reference": (
                f"Evaluator observed restored public {CONTEXT_FILENAME} after the "
                "recorded condition-observed pause."
            ),
        }
    }
    _atomic_new_json(provenance_path, provenance)
    state_after = _regular_bytes(state_path, "until-loop state")
    history_after = _regular_bytes(history_path, "until-loop history")
    record = {
        "format": FORMAT,
        "status": "ready_for_resume",
        "mode": result["mode"],
        "first_review_evidence": review_reason,
        "paused_state_sha256": _sha256(state_before),
        "paused_history_sha256": _sha256(history_before),
        "state_history_unchanged": state_before == state_after and history_before == history_after,
        "context": {"path": CONTEXT_FILENAME, "sha256": _sha256((workspace / CONTEXT_FILENAME).read_bytes())},
        "evaluator_commit": _git(workspace, "rev-parse", "HEAD").strip(),
        "changed_path": "formatter.py",
        "provenance_path": str(provenance_path),
        "resume_prompt": _neutral_resume_prompt(card_path, workspace, provenance_path),
    }
    _atomic_new_json(marker, record)
    return record


def prepare(source_repo: Path, revision: str, root: Path) -> dict[str, Any]:
    """Prepare Q4 using the ordinary runner's clean-control fixture.

    The ordinary runner prepares and freezes all source/fixture evidence first.
    This wrapper changes only the user-visible request in the new trial's own
    manifest, before either host invocation has occurred.
    """
    quality = _quality()
    manifest = quality.prepare(source_repo, revision, root, "clean_control")
    trial_path = root / "trial.json"
    manifest = _read_json(trial_path)
    fixture = dict(manifest["fixture"])
    fixture["request"] = augmented_request(str(fixture["request"]))
    manifest["fixture"] = fixture
    manifest["execution_mode"] = "controlled_resume"
    manifest["controlled_resume"] = {
        "format": FORMAT,
        "initial_invocation": "one fresh agent context; controller may not intervene before a real pause",
        "continuation": "one fresh agent context only after the recorded condition-observed pause",
        "synthetic_setup_is_not_a_review": True,
    }
    _write_json(trial_path, manifest)
    evidence = Path(manifest["evidence"])
    prompt = quality.execution_prompt(manifest)
    ordinary_boundary = "Complete the work in this invocation; there will be no evaluator coaching or follow-up continuation prompt."
    controlled_boundary = (
        "Complete the work in this invocation unless the request's explicit public "
        "dependency boundary requires an authorized condition-observed pause. There "
        "will be no evaluator coaching. Only that recorded pause may receive one "
        "fresh continuation context through the normal resume protocol."
    )
    if ordinary_boundary not in prompt:
        raise ResumeControllerError("ordinary runner prompt changed; cannot safely apply controlled-resume boundary")
    (evidence / "prompt.txt").write_text(prompt.replace(ordinary_boundary, controlled_boundary), encoding="utf-8")
    return manifest


def start(root: Path, timeout: int = 1200, max_commands: int = 100) -> dict[str, Any]:
    """Execute exactly the first Q4 context through the normal runner."""
    return _quality().run(root, timeout=timeout, max_commands=max_commands)


def _allowed_provenance_event_ids(events: list[dict[str, Any]], provenance_path: Path) -> list[int]:
    """Allow the one exact evaluator file needed by normal v2 resume.

    This is a trace-audit exception, not a sandbox exception: a command that
    names the provenance path and any other evidence path stays forbidden.
    """
    allowed: list[int] = []
    exact = str(provenance_path)
    for number, event in enumerate(events):
        item = event.get("item", {}) if isinstance(event, dict) else {}
        command = str(item.get("command", "")) + " " + str(item.get("arguments", ""))
        if exact in command:
            allowed.append(number)
    return allowed


def _combined_event_accesses(events: list[dict[str, Any]], evidence: Path, provenance_path: Path) -> tuple[list[int], list[int]]:
    quality = _quality()
    ordinary = set(quality.forbidden_accesses(events, evidence))
    allowed = set(_allowed_provenance_event_ids(events, provenance_path))
    # Only the exact provenance file may be read.  A provenance-bearing command
    # that also names a broader evaluator location is not excused.
    permitted: list[int] = []
    forbidden: list[int] = []
    controller_evidence = provenance_path.parent.parent
    candidates = set(ordinary)
    for number, event in enumerate(events):
        item = event.get("item", {}) if isinstance(event, dict) else {}
        command = str(item.get("command", "")) + " " + str(item.get("arguments", ""))
        if str(provenance_path) in command or str(controller_evidence) in command:
            candidates.add(number)
    for number in sorted(candidates):
        item = events[number].get("item", {})
        command = str(item.get("command", "")) + " " + str(item.get("arguments", ""))
        without_provenance = command.replace(str(provenance_path), "")
        broad = "../" in command or str(evidence) in command or str(controller_evidence) in without_provenance
        if number in allowed and not broad:
            permitted.append(number)
        else:
            forbidden.append(number)
    return permitted, forbidden


def _continuation_manifest(root: Path, intervention: Mapping[str, Any]) -> tuple[Path, dict[str, Any]]:
    """Create a second, append-only runner root for the fresh continuation."""
    original = _read_json(root / "trial.json")
    continuation_root = root / CONTINUATION_DIRNAME
    if continuation_root.exists() or continuation_root.is_symlink():
        raise ResumeControllerError("controlled-resume continuation root already exists")
    evidence = continuation_root / "evidence"
    evidence.mkdir(parents=True)
    source = dict(original["source"])
    fixture = dict(original["fixture"])
    manifest = {
        "format": "improve-quality-controlled-resume-continuation/v1",
        "case_id": original["case_id"], "workspace": original["workspace"],
        "evidence": str(evidence), "source": source, "fixture": fixture,
        "isolation": original["isolation"], "execution_mode": "controlled_resume_continuation",
        "controlled_resume": dict(intervention),
    }
    _write_json(continuation_root / "trial.json", manifest)
    (evidence / "prompt.txt").write_text(str(intervention["resume_prompt"]), encoding="utf-8")
    quality = _quality()
    initial = quality.capture(Path(manifest["workspace"]), evidence, 0,
                              "after controlled-resume intervention before fresh continuation")
    _write_json(evidence / "snapshots.json", [initial])
    _write_json(evidence / "baseline-oracle.json", fixtures.run_oracle("clean_control", Path(manifest["workspace"])))
    return continuation_root, manifest


def resume(root: Path, timeout: int = 1200, max_commands: int = 100) -> dict[str, Any]:
    """Intervene once if Q4 was actually paused, then run one fresh context."""
    original = _read_json(root / "trial.json")
    evidence = Path(original["evidence"])
    intervention = intervene_if_ready(
        Path(original["workspace"]), evidence, original["fixture"],
        Path(original["source"]["path"]) / "SKILL.md",
    )
    if intervention["status"] != "ready_for_resume":
        return intervention
    continuation_root, _manifest = _continuation_manifest(root, intervention)
    observed = _quality().run(continuation_root, timeout=timeout, max_commands=max_commands)
    return {"status": "continuation_finished", "intervention": intervention,
            "continuation_root": str(continuation_root), "observed": observed}


def _copy_stream_snapshots(source_evidence: Path, destination: Path, prefix: str) -> list[dict[str, Any]]:
    snapshots_path = source_evidence / "snapshots.json"
    if not snapshots_path.is_file():
        return []
    snapshots = json.loads(snapshots_path.read_text(encoding="utf-8"))
    if not isinstance(snapshots, list):
        raise ResumeControllerError("snapshot list is malformed")
    output = []
    for position, snapshot in enumerate(snapshots):
        if not isinstance(snapshot, dict) or not isinstance(snapshot.get("id"), str):
            raise ResumeControllerError("snapshot entry is malformed")
        old_id, new_id = snapshot["id"], f"{prefix}-{position:04d}"
        old = source_evidence / "snapshots" / old_id
        new = destination / "snapshots" / new_id
        if not old.is_dir() or old.is_symlink():
            raise ResumeControllerError("snapshot directory is missing or unsafe")
        shutil.copytree(old, new, symlinks=True)
        copied = dict(snapshot)
        copied["id"] = new_id
        copied["stream"] = prefix
        copied["source_snapshot_id"] = old_id
        references = snapshot.get("relative_evidence_paths", [])
        if not isinstance(references, list) or not all(isinstance(item, str) for item in references):
            raise ResumeControllerError("snapshot references are malformed")
        remapped = []
        old_prefix = Path("snapshots") / old_id
        for reference in references:
            relative = Path(reference)
            if relative.is_absolute() or ".." in relative.parts:
                raise ResumeControllerError("snapshot reference escapes its source snapshot")
            try:
                suffix = relative.relative_to(old_prefix)
            except ValueError as error:
                raise ResumeControllerError("snapshot reference is outside its source snapshot") from error
            if not suffix.parts:
                raise ResumeControllerError("snapshot reference names its source directory")
            target = new / suffix
            if not target.is_file() or target.is_symlink():
                raise ResumeControllerError("snapshot reference is missing from copied evidence")
            remapped.append(str(Path("snapshots") / new_id / suffix))
        copied["relative_evidence_paths"] = remapped
        _write_json(new / "snapshot.json", copied)
        output.append(copied)
    return output


def combine(root: Path) -> dict[str, Any]:
    """Build independent combined audit evidence after both real invocations."""
    quality = _quality()
    original = _read_json(root / "trial.json")
    continuation_root = root / CONTINUATION_DIRNAME
    continuation = _read_json(continuation_root / "trial.json")
    first_evidence, second_evidence = Path(original["evidence"]), Path(continuation["evidence"])
    first_observed = _read_json(first_evidence / "observed.json")
    second_observed = _read_json(second_evidence / "observed.json")
    intervention = _read_json(first_evidence / CONTROLLER_DIRNAME / "intervention.json")
    if intervention.get("status") != "ready_for_resume":
        raise ResumeControllerError("cannot combine without a ready controlled-resume intervention")
    combined_root = root / COMBINED_DIRNAME
    if combined_root.exists() or combined_root.is_symlink():
        raise ResumeControllerError("combined audit root already exists")
    combined_evidence = combined_root / "evidence"
    combined_evidence.mkdir(parents=True)
    snapshots = _copy_stream_snapshots(first_evidence, combined_evidence, "initial")
    snapshots.extend(_copy_stream_snapshots(second_evidence, combined_evidence, "continuation"))
    initial_events = quality.read_events(first_evidence / "events.jsonl", complete=True)
    continuation_events = quality.read_events(second_evidence / "events.jsonl", complete=True)
    combined_events = []
    for stream, events in (("initial", initial_events), ("continuation", continuation_events)):
        for event in events:
            copied = dict(event)
            copied["evaluator_stream"] = stream
            combined_events.append(copied)
    (combined_evidence / "events.jsonl").write_text(
        "".join(json.dumps(event, sort_keys=True) + "\n" for event in combined_events), encoding="utf-8")
    event_refs = quality.retain_transcript(combined_events, combined_evidence)
    provenance = Path(intervention["provenance_path"])
    initial_forbidden = quality.forbidden_accesses(initial_events, first_evidence)
    permitted, forbidden = _combined_event_accesses(continuation_events, second_evidence, provenance)
    source_reads = [first_observed.get("source_read_evidence", {}), second_observed.get("source_read_evidence", {})]
    expected_source_markers = {"card", "policy", "adapter", "selected_path_observed"}
    source_verified = all(
        isinstance(read, dict) and set(read) == expected_source_markers and all(value is True for value in read.values())
        for read in source_reads
    )
    policy_path = Path(original["source"]["path"]) / "references" / "review-policy.md"
    policy_bytes = _regular_bytes(policy_path, "frozen Improve review policy")
    source_policy = {"path": str(policy_path), "sha256": _sha256(policy_bytes)}
    frozen_source_verified = quality.digest(quality.canonical(quality.tree(Path(original["source"]["path"])))) == original["source"].get("digest")
    pause_verified = first_observed.get("runtime_phase") == "paused"
    initial_verified = first_observed.get("invocation_count") == 1
    continuation_verified = second_observed.get("invocation_count") == 1 and second_observed.get("runtime_phase") != "paused"
    try:
        provenance_verified = _load_json(provenance, "controlled-resume provenance").get("provenance", {}).get("kind") == "condition_observed"
    except ResumeControllerError:
        provenance_verified = False
    initial_host_integrity = first_observed.get("source_integrity") is True and not initial_forbidden
    continuation_host_integrity = second_observed.get("source_integrity") is True
    reported_continuation_tripwires = second_observed.get("forbidden_access_event_ids", [])
    reported_exact_provenance_only = (
        isinstance(reported_continuation_tripwires, list)
        and bool(reported_continuation_tripwires)
        and set(reported_continuation_tripwires) == set(permitted)
    )
    continuation_exact_provenance_excused = bool(
        not continuation_host_integrity and reported_exact_provenance_only and not forbidden
        and source_verified and frozen_source_verified
    )
    combined_source_integrity = bool(
        frozen_source_verified and source_verified and initial_host_integrity and
        (continuation_host_integrity or continuation_exact_provenance_excused) and not forbidden
    )
    controlled_verified = bool(
        initial_verified and pause_verified and intervention.get("state_history_unchanged") is True and
        provenance_verified and combined_source_integrity and continuation_verified
    )
    workspace = Path(original["workspace"])
    final_oracle = fixtures.run_oracle("clean_control", workspace)
    _write_json(combined_evidence / "final-oracle.json", final_oracle)
    _write_json(combined_evidence / "baseline-oracle.json", _read_json(first_evidence / "baseline-oracle.json"))
    snapshot_oracles = {
        snapshot["id"]: fixtures.run_oracle("clean_control", combined_evidence / "snapshots" / snapshot["id"] / "candidate")
        for snapshot in snapshots if snapshot.get("stable")
    }
    _write_json(combined_evidence / "snapshot-oracles.json", snapshot_oracles)
    observed = {
        "snapshots": snapshots,
        "runtime_phase": second_observed.get("runtime_phase", "absent"),
        "behavior_passed": final_oracle.get("passed", False),
        "protected_preserved": quality.preserved(workspace, original["fixture"]["protected"]),
        "invocation_count": 2,
        "source_integrity": combined_source_integrity,
        "execution_mode": "controlled_resume",
        "controlled_resume_verified": controlled_verified,
        "source_policy": source_policy,
        "controlled_resume": {
            "not_autonomous_discovery": True, "initial_pause_observed": pause_verified,
            "initial_invocation_verified": initial_verified,
            "initial_forbidden_access_event_ids": initial_forbidden,
            "intervention_record": str(first_evidence / CONTROLLER_DIRNAME / "intervention.json"),
            "provenance_path": str(provenance), "permitted_exact_provenance_access_event_ids": permitted,
            "forbidden_continuation_access_event_ids": forbidden,
            "source_read_evidence": source_reads,
            "frozen_source_verified": frozen_source_verified,
            "initial_host_integrity": initial_host_integrity,
            "continuation_host_integrity": continuation_host_integrity,
            "continuation_exact_provenance_excused": continuation_exact_provenance_excused,
            "continuation_record": str(second_evidence / "observed.json"),
        },
        "trial_status": "completed" if first_observed.get("trial_status") == "completed" and second_observed.get("trial_status") == "completed" else "incomplete",
        "isolation": original["isolation"],
        "contamination_status": "unsealed; exact resume provenance is permitted; other evaluator access is forbidden",
        "forbidden_access_event_ids": forbidden,
        "baseline_oracle": _read_json(first_evidence / "baseline-oracle.json"), "final_oracle": final_oracle,
        "evidence_refs": (
            event_refs
            + [reference for snapshot in snapshots for reference in snapshot["relative_evidence_paths"]]
            + ["transcript.md", "snapshot-oracles.json", "baseline-oracle.json", "final-oracle.json"]
        ),
    }
    _write_json(combined_evidence / "observed.json", observed)
    combined_manifest = {
        "format": "improve-quality-controlled-resume-combined/v1", "case_id": "clean_control",
        "workspace": original["workspace"], "evidence": str(combined_evidence), "source": original["source"],
        "fixture": original["fixture"], "isolation": original["isolation"], "execution_mode": "controlled_resume",
    }
    _write_json(combined_root / "trial.json", combined_manifest)
    return {"combined_root": str(combined_root), "observed": observed}


def audit(root: Path, timeout: int = 600) -> dict[str, Any]:
    """Audit a combined Q4 record; no agent is asked to continue the task."""
    combined = combine(root)
    result = _quality().audit(Path(combined["combined_root"]), timeout=timeout)
    return {"combined": combined, "audit": result}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    prep = commands.add_parser("prepare")
    prep.add_argument("--source-repo", type=Path, required=True)
    prep.add_argument("--revision", default="d8b8432beb6d3cef26e4402a80f8e778f64f129d")
    prep.add_argument("--root", type=Path, required=True)
    for name in ("start", "resume"):
        sub = commands.add_parser(name)
        sub.add_argument("--root", type=Path, required=True)
        sub.add_argument("--timeout", type=int, default=1200)
        sub.add_argument("--max-commands", type=int, default=100)
    checker = commands.add_parser("audit")
    checker.add_argument("--root", type=Path, required=True)
    checker.add_argument("--timeout", type=int, default=600)
    all_run = commands.add_parser("run-all")
    all_run.add_argument("--source-repo", type=Path, required=True)
    all_run.add_argument("--revision", default="d8b8432beb6d3cef26e4402a80f8e778f64f129d")
    all_run.add_argument("--root", type=Path, required=True)
    all_run.add_argument("--timeout", type=int, default=1200)
    args = parser.parse_args()
    if args.command == "prepare":
        result = prepare(args.source_repo.resolve(), args.revision, args.root.resolve())
    elif args.command == "start":
        result = start(args.root.resolve(), args.timeout, args.max_commands)
    elif args.command == "resume":
        result = resume(args.root.resolve(), args.timeout, args.max_commands)
    elif args.command == "audit":
        result = audit(args.root.resolve(), args.timeout)
    else:
        prepare(args.source_repo.resolve(), args.revision, args.root.resolve())
        first = start(args.root.resolve(), args.timeout)
        second = resume(args.root.resolve(), args.timeout)
        result = {"start": first, "resume": second}
        if second.get("status") == "continuation_finished":
            result["audit"] = audit(args.root.resolve(), min(args.timeout, 600))
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
