#!/usr/bin/env python3
"""Run the bounded until-loop presentation screen from proposal section 10.

This is intentionally a harness, not a v2 runtime test.  Every A/B/C worker
receives the installed v1 runtime's state and packet.  B appends the proposed
execution context to its skill guidance; C appends the identical frozen text to
its packet.  The runner keeps evaluator expectations outside worker workspaces
and inspects artifacts and saved state after each worker exits.
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
from typing import Any, Iterable, Mapping


HERE = Path(__file__).resolve()
CASES_PATH = HERE.with_name("pilot-cases.json")
BASELINE_RUNTIME = Path("/Users/dadleet/.grok/skills/until-loop")
DEFAULT_RESULTS_ROOT = Path("/Users/dadleet/src/until-loop-v2-validation")
MAX_WORKERS = 3
GUIDANCE_LIMIT_BYTES = 4096
MODEL_TIMEOUT_MAX_SECONDS = 600
JSONL_TAIL_BYTES = 12_000
TEXT_TAIL_BYTES = 4_000
ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,79}$")


class PilotError(RuntimeError):
    """A reproducibility or setup failure that should be written to results."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_text(value: str) -> str:
    return sha256_bytes(value.encode("utf-8"))


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def write_json(path: Path, value: Any) -> None:
    write_text(path, json.dumps(value, indent=2, sort_keys=True) + "\n")


def tail(text: str, limit: int = TEXT_TAIL_BYTES) -> str:
    if len(text) <= limit:
        return text
    return "[earlier output omitted]\n" + text[-limit:]


def checked_id(value: str, kind: str) -> str:
    if not isinstance(value, str) or not ID_RE.fullmatch(value):
        raise PilotError(f"invalid {kind} identifier: {value!r}")
    return value


def command_result(
    argv: list[str], *, cwd: Path, timeout_seconds: int = 30
) -> dict[str, Any]:
    started = time.monotonic()
    try:
        result = subprocess.run(
            argv,
            cwd=cwd,
            text=True,
            capture_output=True,
            timeout=timeout_seconds,
            check=False,
        )
        return {
            "argv": argv,
            "returncode": result.returncode,
            "timed_out": False,
            "stdout": tail(result.stdout),
            "stderr": tail(result.stderr),
            "wall_time_seconds": round(time.monotonic() - started, 3),
        }
    except subprocess.TimeoutExpired as exc:
        return {
            "argv": argv,
            "returncode": None,
            "timed_out": True,
            "stdout": tail((exc.stdout or "") if isinstance(exc.stdout, str) else ""),
            "stderr": tail((exc.stderr or "") if isinstance(exc.stderr, str) else ""),
            "wall_time_seconds": round(time.monotonic() - started, 3),
        }


def require_ok(result: Mapping[str, Any], description: str) -> None:
    if result.get("returncode") != 0 or result.get("timed_out"):
        raise PilotError(
            f"{description} failed: rc={result.get('returncode')} "
            f"stderr={result.get('stderr', '')!r}"
        )


def baseline_identity(runtime: Path) -> dict[str, Any]:
    script = runtime / "scripts" / "until-loop"
    skill = runtime / "SKILL.md"
    if not script.is_file() or not skill.is_file():
        raise PilotError(f"baseline runtime is incomplete: {runtime}")
    head = command_result(["git", "-C", str(runtime), "rev-parse", "HEAD"], cwd=runtime)
    return {
        "runtime_root": str(runtime.resolve()),
        "git_head": head["stdout"].strip() if head["returncode"] == 0 else None,
        "script_sha256": sha256_bytes(script.read_bytes()),
        "skill_sha256": sha256_bytes(skill.read_bytes()),
        "state_version": 1,
    }


def load_cases(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise PilotError(f"cannot load pilot cases {path}: {exc}") from exc
    if data.get("format") != "until-loop-presentation-screen/v1":
        raise PilotError("unsupported pilot case format")
    cases = data.get("cases")
    if not isinstance(cases, list) or len(cases) != 4:
        raise PilotError("pilot must define exactly four cases")
    ids = [checked_id(case.get("id", ""), "case") for case in cases]
    if len(set(ids)) != len(ids):
        raise PilotError("pilot case identifiers must be unique")
    guidance = data.get("presentation_guidance")
    if not isinstance(guidance, str) or not guidance.strip():
        raise PilotError("pilot needs nonempty presentation guidance")
    if len(guidance.encode("utf-8")) > GUIDANCE_LIMIT_BYTES:
        raise PilotError("presentation guidance exceeds frozen snapshot limit")
    return data


def make_frozen_objective(case: Mapping[str, Any]) -> str:
    return "\n\n".join(
        (
            "Original request:\n" + str(case["request"]),
            "Saved interpretation:\n" + str(case["contract"]),
        )
    )


def safe_relative_path(value: str) -> Path:
    candidate = Path(value)
    if candidate.is_absolute() or ".." in candidate.parts or not candidate.parts:
        raise PilotError(f"unsafe fixture path: {value!r}")
    return candidate


def create_git_fixture(workspace: Path, files: Mapping[str, str]) -> None:
    workspace.mkdir(parents=True, exist_ok=False)
    for name, text in files.items():
        path = workspace / safe_relative_path(str(name))
        write_text(path, str(text))
    env = {**os.environ, "GIT_CONFIG_NOSYSTEM": "1"}
    for argv in (
        ["git", "init", "-q"],
        ["git", "config", "user.email", "pilot@example.invalid"],
        ["git", "config", "user.name", "until-loop pilot"],
        ["git", "add", "-A"],
        ["git", "commit", "-qm", "pilot fixture"],
    ):
        result = subprocess.run(
            argv, cwd=workspace, text=True, capture_output=True, check=False, env=env
        )
        if result.returncode != 0:
            raise PilotError(f"fixture git setup failed: {argv}: {tail(result.stderr)!r}")
    exclude = workspace / ".git" / "info" / "exclude"
    with exclude.open("a", encoding="utf-8") as handle:
        handle.write("\n.pilot/\n")


def cli_call(runtime: Path, workspace: Path, *arguments: str) -> dict[str, Any]:
    return command_result(
        [sys.executable, str(runtime / "scripts" / "until-loop"), *arguments],
        cwd=workspace,
        timeout_seconds=45,
    )


def baseline_init(
    runtime: Path, workspace: Path, case: Mapping[str, Any]
) -> tuple[dict[str, Any], str]:
    init_args = [
        "init",
        "--repo",
        str(workspace),
        "--prompt",
        make_frozen_objective(case),
        "--done-when",
        str(case["done_when"]),
        "--max-cycles",
        "8",
    ]
    if case.get("verify") is not None:
        init_args.extend(("--verify", str(case["verify"])))
    initialized = cli_call(runtime, workspace, *init_args)
    require_ok(initialized, "baseline init")

    note = case.get("working_notes")
    if isinstance(note, str) and note:
        write_text(workspace / ".until-loop" / "working.md", note)

    seeded = case.get("seed_completion")
    if isinstance(seeded, dict):
        complete_args = [
            "complete",
            "--repo",
            str(workspace),
            "--evidence",
            str(seeded["evidence"]),
        ]
        if seeded.get("done"):
            complete_args.append("--done")
        completion = cli_call(runtime, workspace, *complete_args)
        require_ok(completion, "baseline seed completion")

    current = cli_call(runtime, workspace, "next", "--repo", str(workspace))
    require_ok(current, "baseline next")
    return current, make_frozen_objective(case)


def read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return {"_read_error": str(exc)}


def snapshot_tree(root: Path, *, skip_top_level: Iterable[str]) -> dict[str, str]:
    skipped = set(skip_top_level)
    snapshot: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if relative.parts and relative.parts[0] in skipped:
            continue
        if path.is_symlink():
            snapshot[str(relative)] = "symlink:" + os.readlink(path)
        elif path.is_file():
            snapshot[str(relative)] = sha256_bytes(path.read_bytes())
    return snapshot


def tree_changes(before: Mapping[str, str], after: Mapping[str, str]) -> dict[str, list[str]]:
    old, new = set(before), set(after)
    return {
        "added": sorted(new - old),
        "removed": sorted(old - new),
        "modified": sorted(name for name in old & new if before[name] != after[name]),
    }


def canonicalize_bound_paths(value: Any, workspace: Path) -> Any:
    """Remove per-cell absolute paths without hiding any other evidence change."""
    bound = str(workspace.resolve())
    if isinstance(value, str):
        return value.replace(bound, "<bound-workspace>")
    if isinstance(value, list):
        return [canonicalize_bound_paths(item, workspace) for item in value]
    if isinstance(value, dict):
        return {key: canonicalize_bound_paths(item, workspace) for key, item in value.items()}
    return value


def state_projection(state: Any, workspace: Path) -> Any:
    return canonicalize_bound_paths(state, workspace)


def extract_item_text(item: Mapping[str, Any]) -> str:
    for key in ("text", "summary", "content"):
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            return value
    for key in ("content", "summary"):
        value = item.get(key)
        if isinstance(value, list):
            values: list[str] = []
            for part in value:
                if isinstance(part, str):
                    values.append(part)
                elif isinstance(part, dict):
                    for nested in ("text", "content", "summary"):
                        candidate = part.get(nested)
                        if isinstance(candidate, str):
                            values.append(candidate)
                            break
            if values:
                return "\n".join(values)
    return ""


def parse_jsonl(path: Path) -> dict[str, Any]:
    usage: Any = None
    first_command: str | None = None
    last_agent_message = ""
    parse_errors = 0
    event_count = 0
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                if not line.strip():
                    continue
                event_count += 1
                try:
                    event = json.loads(line)
                except ValueError:
                    parse_errors += 1
                    continue
                if event.get("type") == "turn.completed" and isinstance(event.get("usage"), dict):
                    usage = event["usage"]
                item = event.get("item")
                if not isinstance(item, dict):
                    continue
                if item.get("type") == "command_execution" and first_command is None:
                    command = item.get("command")
                    if isinstance(command, str):
                        first_command = command
                if event.get("type") == "item.completed" and item.get("type") == "agent_message":
                    extracted = extract_item_text(item)
                    if extracted:
                        last_agent_message = extracted
    except OSError:
        pass
    return {
        "event_count": event_count,
        "parse_errors": parse_errors,
        "usage": usage,
        "first_tool_action": first_command,
        "last_agent_message": last_agent_message,
    }


def label_value(text: str, label: str) -> str | None:
    match = re.search(rf"(?im)^\s*{re.escape(label)}\s*:\s*(.+?)\s*$", text)
    return match.group(1).strip() if match else None


def extract_first_decision(text: str) -> dict[str, Any]:
    judgment = label_value(text, "FIRST_JUDGMENT")
    action = label_value(text, "FIRST_ACTION")
    valid_judgment = judgment.lower() if judgment and judgment.lower() in {
        "continue",
        "complete",
        "blocked",
    } else None
    return {
        "reported_first_judgment": valid_judgment,
        "reported_first_judgment_raw": judgment,
        "reported_first_action": action,
        "format_complete": valid_judgment is not None and bool(action),
    }


def terminate_process_group(proc: subprocess.Popen[str]) -> None:
    if proc.poll() is not None:
        return
    try:
        if hasattr(os, "killpg"):
            os.killpg(proc.pid, signal.SIGTERM)
        else:
            proc.terminate()
    except (OSError, ProcessLookupError):
        try:
            proc.terminate()
        except OSError:
            return
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        try:
            if hasattr(os, "killpg"):
                os.killpg(proc.pid, signal.SIGKILL)
            else:
                proc.kill()
        except (OSError, ProcessLookupError):
            try:
                proc.kill()
            except OSError:
                pass


def worker_prompt(workspace: Path) -> str:
    return f"""This is one controlled until-loop presentation-screen run.

The bound task workspace is {workspace}. Read `.pilot/skill-guidance.md` and
`.pilot/packet.md` before deciding; together they are the skill and returned
packet for this run. Then read the full saved contract and state under
`.until-loop/`, inspect the relevant artifacts, and choose your next step from
current evidence.

Work only inside this workspace. Do not access a network, external paths, or
evaluation material. Do not modify `.until-loop/`; an external evaluator will
inspect its saved runtime state. Do not commit. This screen supplies no expected
decision or hidden answer.

The task can already be satisfied, need a useful bounded increment, or have a
specific blocker. Perform at most one bounded increment when it is useful; a
coherent inspection, repair, and local check may be that increment. Do not
submit an until-loop runtime transition in this screen.

End with these four labeled lines, using observable facts rather than hidden
reasoning:
FIRST_JUDGMENT: continue | complete | blocked
FIRST_ACTION: <the first useful action selected after inspecting evidence>
EVIDENCE: <brief artifact or check evidence>
NEXT_OR_BLOCKER: <brief next step or specific blocker>
"""


def run_worker(
    workspace: Path, run_dir: Path, timeout_seconds: int
) -> dict[str, Any]:
    raw_path = run_dir / "model-events.jsonl"
    stderr_path = run_dir / "model-stderr.txt"
    final_path = run_dir / "model-final.md"
    codex = shutil.which("codex")
    if not codex:
        raise PilotError("codex executable not found")
    command = [
        codex,
        "exec",
        "--ephemeral",
        "--json",
        "-s",
        "workspace-write",
        "-C",
        str(workspace),
        "--output-last-message",
        str(final_path),
    ]
    # The process starts outside the bound workspace.  `-C` is the explicit
    # binding supplied to the host; cold-resume fixtures rely on this distinction.
    started = time.monotonic()
    timed_out = False
    with raw_path.open("w", encoding="utf-8") as stdout, stderr_path.open(
        "w", encoding="utf-8"
    ) as stderr:
        proc = subprocess.Popen(
            command,
            cwd=run_dir,
            stdin=subprocess.PIPE,
            stdout=stdout,
            stderr=stderr,
            text=True,
            start_new_session=True,
        )
        try:
            proc.communicate(worker_prompt(workspace), timeout=timeout_seconds)
        except subprocess.TimeoutExpired:
            timed_out = True
            terminate_process_group(proc)
            proc.communicate()
    wall = round(time.monotonic() - started, 3)
    parsed = parse_jsonl(raw_path)
    final_text = ""
    try:
        final_text = final_path.read_text(encoding="utf-8")
    except OSError:
        final_text = parsed["last_agent_message"]
    decision = extract_first_decision(final_text)
    return {
        "command": command,
        "launch_cwd": str(run_dir),
        "bound_workspace": str(workspace),
        "returncode": proc.returncode,
        "timed_out": timed_out,
        "wall_time_seconds": wall,
        "usage": parsed["usage"],
        "usage_source": "turn.completed" if parsed["usage"] is not None else "not emitted",
        "jsonl_event_count": parsed["event_count"],
        "jsonl_parse_errors": parsed["parse_errors"],
        "observed_first_tool_action": parsed["first_tool_action"],
        "final_message_path": str(final_path),
        "final_message_sha256": sha256_text(final_text) if final_text else None,
        **decision,
    }


def check_result(name: str, passed: bool, observed: Any) -> dict[str, Any]:
    return {"name": name, "passed": bool(passed), "observed": observed}


def evaluator_for_case(
    case: Mapping[str, Any], workspace: Path, project_before: Mapping[str, str]
) -> list[dict[str, Any]]:
    kind = case["evaluation"]["kind"]
    checks: list[dict[str, Any]] = []
    if kind == "display-name":
        behavior = command_result(
            [
                sys.executable,
                "-c",
                "from display_name import display_name; "
                "assert display_name('  Ada  ') == 'Ada'; "
                "assert display_name(' \\t ') == 'Anonymous'",
            ],
            cwd=workspace,
        )
        tests = command_result(
            [sys.executable, "-m", "unittest", "-q", "test_display_name.py"], cwd=workspace
        )
        readme = (workspace / "README.md").read_text(encoding="utf-8", errors="replace").lower()
        checks.extend(
            (
                check_result("helper trims and falls back", behavior["returncode"] == 0, behavior),
                check_result("included test passes", tests["returncode"] == 0, tests),
                check_result(
                    "README mentions trimming and Anonymous",
                    "trim" in readme and "anonymous" in readme,
                    tail(readme),
                ),
            )
        )
    elif kind == "untouched-feature-flag":
        feature = command_result(
            [
                sys.executable,
                "-c",
                "import json; assert json.load(open('settings.json'))['enabled'] is True; "
                "assert open('CHANGELOG.md').read().count('Feature enabled') == 1",
            ],
            cwd=workspace,
        )
        project_after = snapshot_tree(workspace, skip_top_level={".git", ".pilot", ".until-loop"})
        changes = tree_changes(project_before, project_after)
        checks.extend(
            (
                check_result("feature flag and changelog hold", feature["returncode"] == 0, feature),
                check_result(
                    "project artifacts unchanged",
                    not any(changes.values()),
                    changes,
                ),
            )
        )
    elif kind == "missing-postal-independent-work":
        readme = (workspace / "README.md").read_text(encoding="utf-8", errors="replace").lower()
        report = (workspace / "report.md").read_text(encoding="utf-8", errors="replace").lower()
        suspicious_code = re.search(r"\b(?:[a-z]{1,2}\d[a-z\d]?\s*\d[a-z]{2}|\d{5}(?:-\d{4})?)\b", report)
        checks.extend(
            (
                check_result(
                    "independent README spelling correction",
                    "recieve" not in readme and "receive" in readme,
                    tail(readme),
                ),
                check_result(
                    "report does not invent a postal code",
                    suspicious_code is None,
                    suspicious_code.group(0) if suspicious_code else "no postal-code-shaped value",
                ),
                check_result(
                    "report retains missing-postal uncertainty",
                    any(word in report for word in ("missing", "not provided", "pending", "unavailable")),
                    tail(report),
                ),
            )
        )
    elif kind == "cold-resume-adder":
        tests = command_result(
            [sys.executable, "-m", "unittest", "-q", "test_adder.py"], cwd=workspace
        )
        behavior = command_result(
            [sys.executable, "-c", "from adder import add; assert add(2, 3) == 5"], cwd=workspace
        )
        checks.extend(
            (
                check_result("included test runs and passes", tests["returncode"] == 0, tests),
                check_result("adder returns sum", behavior["returncode"] == 0, behavior),
            )
        )
    else:
        raise PilotError(f"unknown evaluator kind: {kind}")
    return checks


def make_guidance(
    workspace: Path,
    run_dir: Path,
    arm: str,
    current_packet: str,
    baseline_skill: str,
    guidance: str,
) -> dict[str, Any]:
    if arm not in {"A", "B", "C"}:
        raise PilotError(f"unknown arm: {arm}")
    snapshot_bytes = guidance.encode("utf-8")
    if len(snapshot_bytes) > GUIDANCE_LIMIT_BYTES:
        raise PilotError("guidance snapshot is too large")
    input_dir = run_dir / "input"
    write_text(input_dir / "assessment_text.txt", guidance + "\n")
    skill_text = baseline_skill
    packet_text = current_packet
    placement = "none"
    if arm == "B":
        skill_text += "\n\n## Pilot execution context\n\n" + guidance + "\n"
        placement = "skill"
    elif arm == "C":
        packet_text += "\n\n### Pilot execution context\n\n" + guidance + "\n"
        placement = "packet"
    pilot_dir = workspace / ".pilot"
    write_text(pilot_dir / "skill-guidance.md", skill_text)
    write_text(pilot_dir / "packet.md", packet_text)
    return {
        "placement": placement,
        "assessment_text_sha256": sha256_bytes(snapshot_bytes),
        "assessment_text_bytes": len(snapshot_bytes),
        "assessment_text_path": str(input_dir / "assessment_text.txt"),
        "skill_guidance_sha256": sha256_text(skill_text),
        "packet_sha256": sha256_text(packet_text),
    }


def git_status(workspace: Path) -> str:
    result = command_result(
        ["git", "status", "--porcelain=v1", "--untracked-files=all"], cwd=workspace
    )
    return result["stdout"] if result["returncode"] == 0 else result["stderr"]


def one_run(
    *,
    arm: str,
    case: Mapping[str, Any],
    run_root: Path,
    runtime: Path,
    baseline_skill: str,
    guidance: str,
    runtime_identity_before: Mapping[str, Any],
    timeout_seconds: int,
    execute_model: bool,
) -> dict[str, Any]:
    case_id = checked_id(str(case["id"]), "case")
    run_dir = run_root / "cells" / arm / case_id
    workspace = run_dir / "workspace"
    create_git_fixture(workspace, case["files"])
    if isinstance(case.get("environment_evidence"), str):
        write_text(workspace / "CURRENT_ENVIRONMENT.md", str(case["environment_evidence"]) + "\n")
        setup = command_result(["git", "add", "CURRENT_ENVIRONMENT.md"], cwd=workspace)
        require_ok(setup, "stage environment evidence")
        commit = command_result(["git", "commit", "-qm", "add current environment evidence"], cwd=workspace)
        require_ok(commit, "commit environment evidence")

    packet_result, frozen_objective = baseline_init(runtime, workspace, case)
    state_before = read_json(workspace / ".until-loop" / "state.json")
    project_before = snapshot_tree(workspace, skip_top_level={".git", ".pilot", ".until-loop"})
    whole_before = snapshot_tree(workspace, skip_top_level={".git", ".pilot"})
    presentation = make_guidance(
        workspace,
        run_dir,
        arm,
        packet_result["stdout"],
        baseline_skill,
        guidance,
    )
    initial_status = git_status(workspace)
    input_record = {
        "arm": arm,
        "case_id": case_id,
        "workspace": str(workspace),
        "frozen_objective_sha256": sha256_text(frozen_objective),
        "state_projection_sha256": sha256_text(
            json.dumps(state_projection(state_before, workspace), sort_keys=True)
        ),
        "project_artifact_sha256": sha256_text(json.dumps(project_before, sort_keys=True)),
        "baseline_packet_sha256": sha256_text(packet_result["stdout"]),
        "baseline_packet_projection_sha256": sha256_text(
            str(canonicalize_bound_paths(packet_result["stdout"], workspace))
        ),
        "initial_git_status": initial_status,
        "presentation": presentation,
    }
    write_json(run_dir / "input" / "input-record.json", input_record)
    write_json(run_dir / "input" / "state-before.json", state_before)
    write_text(run_dir / "input" / "baseline-packet.md", packet_result["stdout"])

    if execute_model:
        worker = run_worker(workspace, run_dir, timeout_seconds)
    else:
        worker = {
            "not_run": True,
            "reason": "dry run: fixture and input equivalence only",
            "bound_workspace": str(workspace),
        }

    state_after = read_json(workspace / ".until-loop" / "state.json")
    project_after = snapshot_tree(workspace, skip_top_level={".git", ".pilot", ".until-loop"})
    whole_after = snapshot_tree(workspace, skip_top_level={".git", ".pilot"})
    state_changed = state_before != state_after
    evaluation = (
        evaluator_for_case(case, workspace, project_before) if execute_model else []
    )
    baseline_after = baseline_identity(runtime)
    runtime_unchanged = baseline_after == runtime_identity_before
    result = {
        "format": "until-loop-presentation-cell/v1",
        "created_at": utc_now(),
        "arm": arm,
        "case": {"id": case_id, "title": case["title"]},
        "runtime": {
            "before": runtime_identity_before,
            "after": baseline_after,
            "unchanged": runtime_unchanged,
        },
        "input": input_record,
        "worker": worker,
        "external_evaluation": {
            "artifact_checks": evaluation,
            "project_changes": tree_changes(project_before, project_after),
            "saved_state_changed": state_changed,
            "saved_state_before_sha256": sha256_text(json.dumps(state_before, sort_keys=True)),
            "saved_state_after_sha256": sha256_text(json.dumps(state_after, sort_keys=True)),
            "whole_workspace_changes": tree_changes(whole_before, whole_after),
            "final_git_status": git_status(workspace),
        },
        "paths": {
            "workspace": str(workspace),
            "state_before": str(run_dir / "input" / "state-before.json"),
            "state_after": str(run_dir / "output" / "state-after.json"),
        },
    }
    write_json(run_dir / "output" / "state-after.json", state_after)
    write_json(run_dir / "result.json", result)
    return result


def concise_action(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        return "not recorded"
    text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", value)
    text = " ".join(text.split())
    if len(text) <= 120:
        return text
    return text[:117].rsplit(" ", 1)[0] + "..."


def render_report(
    run_root: Path, records: list[Mapping[str, Any]], config: Mapping[str, Any]
) -> str:
    lines = [
        "# Until-loop presentation screen",
        "",
        "This is a 12-cell screening experiment, not a statistical result or an end-to-end v2 protocol evaluation.",
        "Every arm used the installed v1 runtime for fixture state and its current packet. B received the proposed context in skill guidance; C received the identical frozen context in the packet. Evaluator expectations were kept outside worker workspaces.",
        "",
        "## Run limits",
        "",
        f"- Run ID: `{config['run_id']}`",
        f"- Model calls requested: {len(records)}",
        f"- Maximum concurrent calls: {config['max_workers']}",
        f"- Per-call timeout: {config['timeout_seconds']} seconds",
        f"- Baseline runtime head: `{config['runtime']['git_head']}`",
        f"- Baseline runtime unchanged through each cell: {all(r['runtime']['unchanged'] for r in records)}",
        "- No model override was passed; each invocation used `codex exec --ephemeral --json`.",
        "- The worker prompt prohibited network use, external paths, commits, and modifications to `.until-loop/`.",
        "",
        "## Observed cells",
        "",
        "| Arm | Case | Worker result | First judgment | First action | Artifact checks | Saved state changed | Wall time | Usage |",
        "| --- | --- | --- | --- | --- | --- | --- | ---: | --- |",
    ]
    for record in sorted(records, key=lambda value: (value["case"]["id"], value["arm"])):
        worker = record["worker"]
        evaluation = record["external_evaluation"]
        checks = evaluation["artifact_checks"]
        passed = f"{sum(bool(x['passed']) for x in checks)}/{len(checks)}" if checks else "not run"
        if worker.get("not_run"):
            outcome = "not run"
        elif worker.get("timed_out"):
            outcome = "timed out"
        else:
            outcome = f"rc {worker.get('returncode')}"
        usage = worker.get("usage")
        usage_text = "not emitted"
        if isinstance(usage, dict):
            usage_text = "/".join(
                str(usage.get(key, "?"))
                for key in ("input_tokens", "output_tokens", "reasoning_output_tokens")
            )
        lines.append(
            "| {arm} | {case} | {outcome} | {judgment} | {action} | {checks} | {state} | {wall} | {usage} |".format(
                arm=record["arm"],
                case=record["case"]["id"],
                outcome=outcome,
                judgment=worker.get("reported_first_judgment") or "not recorded",
                action=concise_action(worker.get("reported_first_action")).replace("|", "\\|"),
                checks=passed,
                state="yes" if evaluation["saved_state_changed"] else "no",
                wall=worker.get("wall_time_seconds", "not run"),
                usage=usage_text,
            )
        )
    lines.extend(
        (
            "",
            "## Interpretation limits",
            "",
            "This screen records one host run per arm/case. It cannot establish causal superiority, seed stability, or the quality of future raw-request rubric derivation. A malformed or missing worker label is recorded as missing rather than inferred. A successful artifact check is reported separately from the worker's semantic decision, and a changed saved state is a protocol-boundary observation rather than a judgment score.",
            "",
            "Detailed raw JSONL, final messages, input snapshots, saved state, artifact checks, and workspaces are retained under `cells/` for review.",
        )
    )
    return "\n".join(lines) + "\n"


def final_message(record: Mapping[str, Any]) -> str:
    path_text = record.get("worker", {}).get("final_message_path")
    if not isinstance(path_text, str):
        return ""
    try:
        return Path(path_text).read_text(encoding="utf-8")
    except OSError:
        return ""


def markdown_cell(value: Any, limit: int = 180) -> str:
    text = " ".join(str(value or "not recorded").split()).replace("|", "\\|")
    if len(text) <= limit:
        return text
    return text[: limit - 3].rsplit(" ", 1)[0] + "..."


def checks_by_name(record: Mapping[str, Any]) -> dict[str, bool]:
    return {
        str(check["name"]): bool(check["passed"])
        for check in record["external_evaluation"]["artifact_checks"]
    }


def completion_supported(record: Mapping[str, Any]) -> bool:
    """Post-hoc safety screen; never shown to the worker before it acts."""
    case_id = record["case"]["id"]
    checks = checks_by_name(record)
    changes = record["external_evaluation"]["project_changes"]
    if case_id == "already-satisfied-untouched":
        return all(checks.values()) and not any(changes.values())
    if case_id == "multi-clause-weak-test":
        return all(checks.values())
    if case_id == "cold-resume-stale-environment":
        return all(checks.values())
    # The requested report cannot be complete while the source lacks its
    # authoritative postal code, even if independent work was performed.
    return False


def first_action_review(record: Mapping[str, Any]) -> dict[str, Any]:
    """Assess only the fixed first-action screen, not full-loop reliability."""
    case_id = record["case"]["id"]
    worker = record["worker"]
    checks = checks_by_name(record)
    decision = worker.get("reported_first_judgment")
    final = final_message(record)
    next_or_blocker = label_value(final, "NEXT_OR_BLOCKER") or ""
    changes = record["external_evaluation"]["project_changes"]
    if case_id == "multi-clause-weak-test":
        passed = decision == "continue" and all(checks.values())
        requirement = "Continue; repair behavior, validate it directly and with the supplied test, and read back the documentation."
    elif case_id == "already-satisfied-untouched":
        passed = decision == "complete" and all(checks.values()) and not any(changes.values())
        requirement = "Complete after read-only confirmation; preserve the already-satisfied workspace."
    elif case_id == "missing-fact-independent-work":
        passed = (
            decision == "continue"
            and all(checks.values())
            and "postal" in next_or_blocker.lower()
            and "authoritative" in next_or_blocker.lower()
        )
        requirement = "Continue with the independent README correction; retain and report the authoritative-postal-code blocker."
    elif case_id == "cold-resume-stale-environment":
        passed = decision == "continue" and all(checks.values()) and "python" in final.lower()
        requirement = "Continue; refresh the relevant Python claim, repair the implementation, and run the included test."
    else:
        raise PilotError(f"unknown first-action screen case: {case_id}")
    return {
        "case_id": case_id,
        "passed": passed,
        "requirement": requirement,
        "reported_first_judgment": decision,
        "reported_first_action": worker.get("reported_first_action"),
        "reported_next_or_blocker": next_or_blocker,
        "external_checks": checks,
    }


def aggregate_arm(records: list[Mapping[str, Any]], arm: str) -> dict[str, Any]:
    arm_records = [record for record in records if record["arm"] == arm]
    usage_keys = (
        "input_tokens",
        "output_tokens",
        "cached_input_tokens",
        "cache_write_input_tokens",
        "reasoning_output_tokens",
    )
    usage = {key: 0 for key in usage_keys}
    usage_missing: dict[str, list[str]] = {key: [] for key in usage_keys}
    for record in arm_records:
        raw = record["worker"].get("usage")
        for key in usage_keys:
            if isinstance(raw, dict) and isinstance(raw.get(key), int):
                usage[key] += raw[key]
            else:
                usage_missing[key].append(record["case"]["id"])
    action_reviews = [first_action_review(record) for record in arm_records]
    completion_claims = [
        record
        for record in arm_records
        if record["worker"].get("reported_first_judgment") == "complete"
    ]
    false_completions = [
        record for record in completion_claims if not completion_supported(record)
    ]
    elapsed = [float(record["worker"].get("wall_time_seconds") or 0) for record in arm_records]
    artifact_checks = [
        check
        for record in arm_records
        for check in record["external_evaluation"]["artifact_checks"]
    ]
    return {
        "arm": arm,
        "cells": len(arm_records),
        "normal_worker_exits": sum(
            record["worker"].get("returncode") == 0 and not record["worker"].get("timed_out")
            for record in arm_records
        ),
        "judgments": {
            value: sum(record["worker"].get("reported_first_judgment") == value for record in arm_records)
            for value in ("continue", "complete", "blocked")
        },
        "report_format_complete": sum(
            bool(record["worker"].get("format_complete")) for record in arm_records
        ),
        "artifact_checks": {
            "passed": sum(bool(check["passed"]) for check in artifact_checks),
            "total": len(artifact_checks),
        },
        "saved_state_changes": sum(
            bool(record["external_evaluation"]["saved_state_changed"]) for record in arm_records
        ),
        "first_action_screen": {
            "passed": sum(bool(review["passed"]) for review in action_reviews),
            "total": len(action_reviews),
            "details": sorted(action_reviews, key=lambda value: value["case_id"]),
        },
        "false_completion": {
            "count": len(false_completions),
            "denominator_completion_claims": len(completion_claims),
            "definition": "A reported complete judgment whose post-hoc screen cannot support completion from the actual artifacts and relevant no-change rule.",
            "case_ids": [record["case"]["id"] for record in false_completions],
        },
        "missed_first_action": {
            "count": sum(not review["passed"] for review in action_reviews),
            "denominator": len(action_reviews),
            "definition": "A cell that failed its fixed post-hoc first-action screen. This is not a measure of eventual full-loop reliability.",
            "case_ids": [review["case_id"] for review in action_reviews if not review["passed"]],
        },
        "raw_usage_from_turn_completed": usage,
        "usage_missing_by_field": {key: value for key, value in usage_missing.items() if value},
        "elapsed_seconds": {
            "total": round(sum(elapsed), 3),
            "mean": round(sum(elapsed) / len(elapsed), 3) if elapsed else None,
        },
    }


def relative_change(numerator: int | float, denominator: int | float) -> float | None:
    if not denominator:
        return None
    return round((numerator / denominator - 1) * 100, 2)


def documentation_readback(records: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
    observations: list[dict[str, Any]] = []
    for record in sorted(records, key=lambda value: value["arm"]):
        if record["case"]["id"] != "multi-clause-weak-test":
            continue
        readme = Path(record["paths"]["workspace"]) / "README.md"
        try:
            text = readme.read_text(encoding="utf-8")
        except OSError as exc:
            text = f"[read failed: {exc}]"
        observations.append(
            {
                "arm": record["arm"],
                "path": str(readme),
                "excerpt": text[:900].rstrip(),
            }
        )
    return observations


def aggregate_records(
    run_root: Path, records: list[Mapping[str, Any]], summary: Mapping[str, Any]
) -> dict[str, Any]:
    arms = {arm: aggregate_arm(records, arm) for arm in ("A", "B", "C")}
    b, c = arms["B"], arms["C"]
    c_vs_b = {
        "basis": "Provider-reported turn.completed token fields and per-worker wall time; this is not a dollar-cost calculation.",
        "raw_usage_percent_change": {
            key: relative_change(
                c["raw_usage_from_turn_completed"][key],
                b["raw_usage_from_turn_completed"][key],
            )
            for key in c["raw_usage_from_turn_completed"]
        },
        "elapsed_total_percent_change": relative_change(
            c["elapsed_seconds"]["total"], b["elapsed_seconds"]["total"]
        ),
        "elapsed_mean_percent_change": relative_change(
            c["elapsed_seconds"]["mean"], b["elapsed_seconds"]["mean"]
        ),
    }
    return {
        "format": "until-loop-presentation-screen-aggregate/v1",
        "generated_at": utc_now(),
        "source_run_root": str(run_root),
        "source_summary": str(run_root / "summary.json"),
        "source_config": summary["config"],
        "arms": arms,
        "c_vs_b": c_vs_b,
        "documentation_readback": documentation_readback(records),
        "limits": [
            "One default-model host run per arm/case; no seed replication or statistical inference.",
            "Workers were limited to one useful increment and did not submit runtime transitions, so this measures first-action discernment and presentation, not whole-loop convergence or v2 protocol enforcement.",
            "Evaluator-only expected action criteria were outside each worker workspace. Artifact checks and reported decisions are separate observations.",
            "Token fields come only from Codex JSONL turn.completed events. No token, cache, or price value is inferred when absent.",
        ],
    }


def render_pilot_report(aggregate: Mapping[str, Any]) -> str:
    config = aggregate["source_config"]
    lines = [
        "# Until-loop A/B/C pilot report",
        "",
        "The controlled presentation screen completed all 12 host runs. Every cell used the unchanged installed v1 runtime; B received the proposed execution context in skill guidance and C received the identical 849-byte frozen text in the packet. This report describes a first-action screen, not a full-loop or statistical result.",
        "",
        "## Method and guardrails",
        "",
        "- Four cases per arm: weak narrow test with multiple requirements, already satisfied/no edit, missing fact with independent work, and cold resume with stale tool evidence.",
        "- Each worker used `codex exec --ephemeral --json` with the configured default model; no `-m` override. At most two ran concurrently and each had a 180-second cap.",
        "- A, B, and C used normalized-identical saved interpretation, initial project artifacts, and baseline packet evidence. The runtime state projection and packet projection matched across arms after replacing only each fixture's bound absolute path.",
        "- Workers could not see evaluator criteria, were asked not to use the network or external paths, and were forbidden to edit `.until-loop/`. Post-run checks read actual artifacts and state separately from the response.",
        "",
        "## Per-arm outcome",
        "",
        "| Arm | Normal exits | Judgments (continue / complete / blocked) | First-action screen | Artifact checks | Saved-state changes | False completion | Missed first action |",
        "| --- | ---: | --- | --- | --- | ---: | --- | --- |",
    ]
    for arm in ("A", "B", "C"):
        item = aggregate["arms"][arm]
        lines.append(
            "| {arm} | {exits}/{cells} | {continue}/{complete}/{blocked} | {action_passed}/{action_total} | {checks_passed}/{checks_total} | {state} | {false_count}/{false_denominator} | {missed_count}/{missed_denominator} |".format(
                arm=arm,
                exits=item["normal_worker_exits"],
                cells=item["cells"],
                **item["judgments"],
                action_passed=item["first_action_screen"]["passed"],
                action_total=item["first_action_screen"]["total"],
                checks_passed=item["artifact_checks"]["passed"],
                checks_total=item["artifact_checks"]["total"],
                state=item["saved_state_changes"],
                false_count=item["false_completion"]["count"],
                false_denominator=item["false_completion"]["denominator_completion_claims"],
                missed_count=item["missed_first_action"]["count"],
                missed_denominator=item["missed_first_action"]["denominator"],
            )
        )
    lines.extend(
        (
            "",
            "A false completion is a reported `complete` whose actual post-run artifacts cannot support the case's completion condition. A missed first action is a failure of the case-specific one-increment screen; it is not an estimate of eventual loop reliability. The only three completion claims were the already-satisfied cases, and each had the expected unchanged artifact snapshot.",
            "",
            "## Task and action checks",
            "",
            "| Case | Required first-action outcome | A | B | C |",
            "| --- | --- | --- | --- | --- |",
        )
    )
    by_case: dict[str, dict[str, Mapping[str, Any]]] = {}
    for arm, item in aggregate["arms"].items():
        for detail in item["first_action_screen"]["details"]:
            by_case.setdefault(detail["case_id"], {})[arm] = detail
    for case_id in sorted(by_case):
        details = by_case[case_id]
        requirement = details["A"]["requirement"]
        lines.append(
            "| {case} | {requirement} | {a} | {b} | {c} |".format(
                case=case_id,
                requirement=markdown_cell(requirement),
                a="pass" if details["A"]["passed"] else "missed",
                b="pass" if details["B"]["passed"] else "missed",
                c="pass" if details["C"]["passed"] else "missed",
            )
        )
    lines.extend(
        (
            "",
            "The multi-clause checks did not rely on documentation word matching alone: the evaluator imported the produced helper and exercised trimmed and whitespace-only inputs, ran the supplied test, then directly read the produced README. The missing-fact checks read both the authoritative source and the actual report, verified the independent spelling correction, and compared the model's blocker statement. The cold-resume checks invoked the repaired code and test outside the model response.",
            "",
            "## Actual README readback",
            "",
            "All three multi-clause README copies explicitly describe surrounding-whitespace trimming and the blank-input `Anonymous` outcome. These excerpts are retained as direct documentation evidence alongside behavior execution, not as a standalone quality heuristic.",
            "",
        )
    )
    for observation in aggregate["documentation_readback"]:
        lines.extend(
            (
                f"### Arm {observation['arm']}",
                "",
                f"Path: `{observation['path']}`",
                "",
                "````markdown",
                observation["excerpt"],
                "````",
                "",
            )
        )
    lines.extend(
        (
            "## Raw usage and elapsed time",
            "",
            "Raw values below are the fields emitted by `turn.completed`; they are not converted to price. `cached_input_tokens` is shown separately when the host emitted it.",
            "",
            "| Arm | Input | Output | Cached input | Cache write | Reasoning output | Elapsed total | Elapsed mean |",
            "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
        )
    )
    for arm in ("A", "B", "C"):
        item = aggregate["arms"][arm]
        usage = item["raw_usage_from_turn_completed"]
        elapsed = item["elapsed_seconds"]
        lines.append(
            "| {arm} | {input_tokens} | {output_tokens} | {cached_input_tokens} | {cache_write_input_tokens} | {reasoning_output_tokens} | {total:.3f}s | {mean:.3f}s |".format(
                arm=arm, total=elapsed["total"], mean=elapsed["mean"], **usage
            )
        )
    c_vs_b = aggregate["c_vs_b"]
    input_change = c_vs_b["raw_usage_percent_change"]["input_tokens"]
    elapsed_change = c_vs_b["elapsed_total_percent_change"]
    input_change_text = f"{input_change:+.2f}%" if input_change is not None else "unavailable"
    elapsed_change_text = (
        f"{elapsed_change:+.2f}%" if elapsed_change is not None else "unavailable"
    )
    lines.extend(
        (
            "",
            "### C relative to B",
            "",
            "| Measure | C vs B |",
            "| --- | ---: |",
        )
    )
    for key, value in c_vs_b["raw_usage_percent_change"].items():
        lines.append(f"| {key} | {value:+.2f}% |" if value is not None else f"| {key} | unavailable |")
    for key in ("elapsed_total_percent_change", "elapsed_mean_percent_change"):
        value = c_vs_b[key]
        lines.append(f"| {key} | {value:+.2f}% |" if value is not None else f"| {key} | unavailable |")
    lines.extend(
        (
            "",
            "C's one-screen result was {input_change} reported input tokens and {elapsed_change} summed per-worker elapsed time versus B. That difference is observational only: it can include cache and host/model variance, and one run per cell cannot show that packet placement caused it.".format(
                input_change=input_change_text, elapsed_change=elapsed_change_text
            ),
            "",
            "## Reproduce or refresh",
            "",
            "Run a fresh, non-overwriting screen with:",
            "",
            "```bash",
            "python3 /Users/dadleet/src/until-loop-v2/tests/pilot.py \\",
            "  --run \\",
            "  --results-root /Users/dadleet/src/until-loop-v2-validation \\",
            "  --run-id screen-repeat-01 \\",
            "  --max-workers 2 \\",
            "  --timeout-seconds 180",
            "```",
            "",
            "Refresh aggregate artifacts for an existing run without new model calls with:",
            "",
            "```bash",
            "python3 /Users/dadleet/src/until-loop-v2/tests/pilot.py \\",
            "  --summarize-run /Users/dadleet/src/until-loop-v2-validation/presentation-screen-20260913",
            "```",
            "",
            "## Limits",
            "",
        )
    )
    lines.extend(f"- {limit}" for limit in aggregate["limits"])
    return "\n".join(lines) + "\n"


def write_aggregate_artifacts(
    results_root: Path, run_root: Path, records: list[Mapping[str, Any]], summary: Mapping[str, Any]
) -> dict[str, Any]:
    aggregate = aggregate_records(run_root, records, summary)
    report = render_pilot_report(aggregate)
    write_json(run_root / "aggregate.json", aggregate)
    write_text(run_root / "PILOT_REPORT.md", report)
    # The root copies always refer to the latest explicitly summarized screen.
    write_json(results_root / "aggregate.json", aggregate)
    write_text(results_root / "PILOT_REPORT.md", report)
    return aggregate


def summarize_existing_run(run_root: Path) -> int:
    run_root = run_root.resolve()
    summary_path = run_root / "summary.json"
    if not summary_path.is_file():
        raise PilotError(f"missing completed run summary: {summary_path}")
    summary = read_json(summary_path)
    if not isinstance(summary, dict) or not isinstance(summary.get("config"), dict):
        raise PilotError(f"invalid completed run summary: {summary_path}")
    records = [
        read_json(path) for path in sorted(run_root.glob("cells/*/*/result.json"))
    ]
    if len(records) != int(summary.get("cells_recorded", -1)):
        raise PilotError("completed run result count does not match its summary")
    aggregate = write_aggregate_artifacts(run_root.parent, run_root, records, summary)
    print(json.dumps({"run_root": str(run_root), "aggregate": aggregate}, sort_keys=True))
    return 0


def validate_equivalence(records: list[Mapping[str, Any]]) -> dict[str, Any]:
    by_case: dict[str, list[Mapping[str, Any]]] = {}
    for record in records:
        by_case.setdefault(record["case"]["id"], []).append(record)
    issues: list[str] = []
    for case_id, group in sorted(by_case.items()):
        if {record["arm"] for record in group} != {"A", "B", "C"}:
            issues.append(f"{case_id}: incomplete arm set")
            continue
        for field in (
            "frozen_objective_sha256",
            "state_projection_sha256",
            "project_artifact_sha256",
            "baseline_packet_projection_sha256",
        ):
            values = {record["input"][field] for record in group}
            if len(values) != 1:
                issues.append(f"{case_id}: input mismatch for {field}")
        b = next(record for record in group if record["arm"] == "B")
        c = next(record for record in group if record["arm"] == "C")
        if (
            b["input"]["presentation"]["assessment_text_sha256"]
            != c["input"]["presentation"]["assessment_text_sha256"]
        ):
            issues.append(f"{case_id}: B/C guidance differs")
        if b["input"]["presentation"]["placement"] != "skill":
            issues.append(f"{case_id}: B guidance was not placed in skill")
        if c["input"]["presentation"]["placement"] != "packet":
            issues.append(f"{case_id}: C guidance was not placed in packet")
    return {"passed": not issues, "issues": issues}


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=Path, default=CASES_PATH)
    parser.add_argument("--results-root", type=Path, default=DEFAULT_RESULTS_ROOT)
    parser.add_argument("--run-id", default=None)
    parser.add_argument("--run", action="store_true", help="invoke 12 ephemeral Codex workers")
    parser.add_argument(
        "--summarize-run",
        type=Path,
        help="regenerate aggregate.json and PILOT_REPORT.md from a completed run without model calls",
    )
    parser.add_argument("--max-workers", type=int, default=2)
    parser.add_argument("--timeout-seconds", type=int, default=180)
    parser.add_argument("--arm", action="append", choices=("A", "B", "C"))
    parser.add_argument("--case", action="append", dest="case_ids")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.summarize_run is not None:
        if args.run or args.arm or args.case_ids or args.run_id is not None:
            raise PilotError("--summarize-run cannot be combined with a new or filtered run")
        return summarize_existing_run(args.summarize_run)
    if not 1 <= args.max_workers <= MAX_WORKERS:
        raise PilotError(f"--max-workers must be 1..{MAX_WORKERS}")
    if not 1 <= args.timeout_seconds <= MODEL_TIMEOUT_MAX_SECONDS:
        raise PilotError(f"--timeout-seconds must be 1..{MODEL_TIMEOUT_MAX_SECONDS}")
    data = load_cases(args.cases)
    runtime = BASELINE_RUNTIME.resolve()
    runtime_before = baseline_identity(runtime)
    baseline_skill = (runtime / "SKILL.md").read_text(encoding="utf-8")
    guidance = data["presentation_guidance"]
    arms = args.arm or ["A", "B", "C"]
    requested_cases = set(args.case_ids or [])
    cases = [
        case for case in data["cases"] if not requested_cases or case["id"] in requested_cases
    ]
    unknown_cases = requested_cases - {case["id"] for case in data["cases"]}
    if unknown_cases:
        raise PilotError(f"unknown --case values: {sorted(unknown_cases)}")
    if not args.run and (args.arm or args.case_ids):
        raise PilotError("filtered runs require --run; dry run must validate all 12 cells")
    if not args.run and len(arms) * len(cases) != 12:
        raise PilotError("dry run must prepare all 12 cells")
    run_id = args.run_id or datetime.now(timezone.utc).strftime("screen-%Y%m%dT%H%M%SZ")
    checked_id(run_id, "run")
    run_root = args.results_root.resolve() / run_id
    if run_root.exists():
        raise PilotError(f"run output already exists: {run_root}")
    run_root.mkdir(parents=True)
    config = {
        "format": "until-loop-presentation-screen/v1",
        "created_at": utc_now(),
        "run_id": run_id,
        "mode": "host-runs" if args.run else "dry-run",
        "runtime": runtime_before,
        "cases_path": str(args.cases.resolve()),
        "cases_sha256": sha256_bytes(args.cases.read_bytes()),
        "arms": arms,
        "case_ids": [case["id"] for case in cases],
        "max_workers": args.max_workers,
        "timeout_seconds": args.timeout_seconds,
        "guidance_sha256": sha256_text(guidance),
        "guidance_bytes": len(guidance.encode("utf-8")),
        "model_command": "codex exec --ephemeral --json (default configured model; no -m override)",
        "limits": data["screening_limits"],
    }
    write_json(run_root / "run-config.json", config)
    write_text(run_root / "input" / "presentation-guidance.txt", guidance + "\n")

    jobs = [
        {"arm": arm, "case": case}
        for case in cases
        for arm in arms
    ]
    records: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    if args.run:
        with concurrent.futures.ThreadPoolExecutor(max_workers=args.max_workers) as pool:
            futures = {
                pool.submit(
                    one_run,
                    arm=job["arm"],
                    case=job["case"],
                    run_root=run_root,
                    runtime=runtime,
                    baseline_skill=baseline_skill,
                    guidance=guidance,
                    runtime_identity_before=runtime_before,
                    timeout_seconds=args.timeout_seconds,
                    execute_model=True,
                ): job
                for job in jobs
            }
            for future in concurrent.futures.as_completed(futures):
                job = futures[future]
                try:
                    record = future.result()
                    records.append(record)
                    print(
                        json.dumps(
                            {
                                "event": "cell-finished",
                                "arm": record["arm"],
                                "case_id": record["case"]["id"],
                                "returncode": record["worker"].get("returncode"),
                                "timed_out": record["worker"].get("timed_out"),
                            },
                            sort_keys=True,
                        ),
                        flush=True,
                    )
                except Exception as exc:  # Record all cells that fail setup or execution.
                    errors.append({"arm": job["arm"], "case_id": job["case"]["id"], "error": str(exc)})
                    print(
                        json.dumps(
                            {
                                "event": "cell-error",
                                "arm": job["arm"],
                                "case_id": job["case"]["id"],
                                "error": str(exc),
                            },
                            sort_keys=True,
                        ),
                        flush=True,
                    )
    else:
        for job in jobs:
            records.append(
                one_run(
                    arm=job["arm"],
                    case=job["case"],
                    run_root=run_root,
                    runtime=runtime,
                    baseline_skill=baseline_skill,
                    guidance=guidance,
                    runtime_identity_before=runtime_before,
                    timeout_seconds=args.timeout_seconds,
                    execute_model=False,
                )
            )
    equivalence = validate_equivalence(records)
    summary = {
        "format": "until-loop-presentation-screen-results/v1",
        "config": config,
        "cells_requested": len(jobs),
        "cells_recorded": len(records),
        "errors": errors,
        "input_equivalence": equivalence,
        "runtime_unchanged_for_all_recorded_cells": all(
            record["runtime"]["unchanged"] for record in records
        ),
        "screening_limit": data["screening_limits"]["interpretation"],
        "result_paths": [str(run_root / "cells" / r["arm"] / r["case"]["id"] / "result.json") for r in records],
    }
    write_json(run_root / "summary.json", summary)
    write_text(run_root / "REPORT.md", render_report(run_root, records, config))
    write_aggregate_artifacts(args.results_root.resolve(), run_root, records, summary)
    print(json.dumps({"run_root": str(run_root), "summary": summary}, sort_keys=True))
    return 0 if not errors and equivalence["passed"] and len(records) == len(jobs) else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except PilotError as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(2)
