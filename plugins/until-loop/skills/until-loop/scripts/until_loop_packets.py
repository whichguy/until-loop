"""Packet, schema, and error-envelope rendering for Until Loop."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

from until_loop_verify import StateError, _verify_script


def _action_token(state: dict[str, Any]) -> str:
    return f"{state['run_id']}:{state['action_number']}"


def _required_trivial_reviews(state: dict[str, Any]) -> int:
    return state["contract"]["required_trivial_reviews"]


def _next_argv(path: Path, script_path: Path) -> list[str]:
    return [
        str(Path(os.path.abspath(sys.executable))),
        str(Path(os.path.abspath(script_path))),
        "next",
        "--state",
        str(path),
    ]


def _done_argv(path: Path, state: dict[str, Any], script_path: Path) -> list[str]:
    return [
        str(Path(os.path.abspath(sys.executable))),
        str(Path(os.path.abspath(script_path))),
        "done",
        "--state",
        str(path),
        f"--action={_action_token(state)}",
    ]


def _report_schema(has_checks: bool = True) -> dict[str, Any]:
    required = [
        "classification",
        "exit_assessment",
        "continuation_assessment",
        "evidence",
        "handoff",
    ]
    properties: dict[str, Any] = {
        "classification": {
            "enum": ["trivial", "non-trivial", "unresolved"],
            "description": (
                "trivial: completed pass with no material findings/changes; "
                "non-trivial: material finding or behavior change; "
                "unresolved: work or assessment incomplete."
            ),
        },
        "exit_assessment": {"enum": ["satisfied", "unsatisfied", "unknown"]},
        "continuation_assessment": {"enum": ["allowed", "blocked", "cancelled"]},
        "evidence": {"type": "string", "description": "Observed checks, changes, and remaining gaps."},
        "handoff": {
            "type": "string",
            "description": "Complete compact replacement summary (facts, checks, gaps, locators) for a fresh host.",
        },
    }
    if has_checks:  # a contract without exit checks has nothing to repair: keep its packets as small as 0.7.0's
        properties["check_repairs"] = {
            "type": "array",
            "description": (
                "Optional with unsatisfied exit only: [{id, command, reason}] for a failed check whose "
                "command misstates its clause (non-vacuous, max 2/check)."
            ),
        }
    return {
        "type": "object",
        "additionalProperties": False,
        "required": required,
        "properties": properties,
    }


def _next_action(state: dict[str, Any]) -> str:
    report = state["last_report"]
    if report is None:
        return "Begin a complete first iteration from the current artifacts."
    failed = [r for r in (state.get("last_checks") or []) if not r["passed"]]
    if failed and report["exit_assessment"] == "satisfied":
        detail = "; ".join(f"{r['id']} (exit {r['exit_code']}): {r['output_tail']!r}" for r in failed)
        return (
            "The latest report claimed a satisfied exit, but its verify-script results show these frozen exit "
            f"checks failed: {detail}. The exit condition is not met. Fix the work, not the checks, then "
            "recheck every exit clause. If a failed check's command misstates its own clause (a shell or "
            "platform bug), repair it in this report's check_repairs with the reason; never repair a check "
            "to accept work that differs from its clause."
        )
    if report["classification"] == "non-trivial":
        if _required_trivial_reviews(state):
            return (
                "Address and recheck the material observations in the latest report, then "
                "perform a new complete review before assessing the conditions again."
            )
        return (
            "Address and recheck the material observations in the latest report before "
            "assessing the conditions again."
        )
    if report["classification"] == "unresolved":
        if _required_trivial_reviews(state):
            return (
                "Resolve the evidence gap in the latest report, then perform a new complete "
                "review before assessing the conditions again."
            )
        return "Resolve the evidence gap in the latest report, then recheck the conditions."
    if state["trivial_streak"] < _required_trivial_reviews(state):
        return (
            "Perform another distinct full review to establish whether the exit condition and "
            "configured trivial-review gate are now met."
        )
    return "Resolve the outstanding exit-condition gap in the latest evidence, then recheck it."


def _packet_context(state: dict[str, Any]) -> dict[str, Any]:
    context = state["contract"]["context"]
    return {
        "request": context["request"],
        "scope": context["scope"],
        "authority": context["authority"],
        "environment": context["environment"],
        "resources": [dict(resource) for resource in context["resources"]],
    }


def _status_semantics() -> dict[str, str]:
    return {
        "active": "Execute exactly one complete returned work iteration, then submit its exact done callback.",
        "complete": "The exit assessment and numeric review gate were accepted; report once with no callback.",
        "stopped": "The run ended incomplete because it was cancelled or blocked; report once with no callback.",
        "error": "No success or advancement may be inferred; use only a returned read-only next command when available.",
    }


def _metadata(
    *,
    status: str,
    path: Path,
    state: dict[str, Any],
    action_number: int,
    trivial_streak: int,
    last_report: dict[str, str] | None,
) -> dict[str, Any]:
    contract = state["contract"]
    report = None if last_report is None else dict(last_report)
    return {
        "status": status,
        "state_file": str(path),
        "workspace": contract["workspace"],
        "work": contract["work"],
        "conditions": {
            "exit": contract["exit_condition"],
            "repeat": contract["repeat_condition"],
        },
        "progress": {
            "action_number": action_number,
            "trivial_streak": trivial_streak,
            "required_trivial_reviews": _required_trivial_reviews(state),
        },
        "context": _packet_context(state),
        "status_semantics": _status_semantics(),
        "last_report": report,
        "receipt": state["receipt"],
        "exit_checks": contract["exit_checks"],
        "last_check_results": state.get("last_checks"),
        "check_repairs": state.get("check_repairs", []),
    }


def _active_packet(path: Path, state: dict[str, Any], script_path: Path) -> dict[str, Any]:
    token = _action_token(state)
    done_argv = _done_argv(path, state, script_path)
    next_argv = _next_argv(path, script_path)
    packet = _metadata(
        status="active",
        path=path,
        state=state,
        action_number=state["action_number"],
        trivial_streak=state["trivial_streak"],
        last_report=state["last_report"],
    )
    verify_step = (
        "Run verify_script unchanged in your shell tool after the last change (never via file/var/subshell); "
        "satisfied is refused without a fresh quiet run. "
        if state["contract"]["exit_checks"] else ""
    )
    packet.update(
        {
            "instruction": (
                f"{_next_action(state)} Execution condition (Work): {state['contract']['work']!r}. "
                "Assess conditions, context, last_report (evidence/handoff are unverified claims), and current "
                "user instructions. Already fresh if auto-recovered; from an older packet, run read-only next_argv "
                "once and recheck current artifact state—never replay done or guess transitions. Honor user stops as "
                "cancelled and obstacles as blocked. Otherwise execute exactly one complete assigned iteration in "
                "order (For an Improve request, review artifacts/history, plan, implement authorized fixes, run "
                "checks; classify incomplete work/assessment as unresolved). Nested calls do not advance this frame. "
                + verify_step
                + "Submit the done_argv JSON array with stdin matching report_schema, then follow its returned packet. "
                "Use concise Markdown grounded in verified progress/evidence."
            ),
            "next_argv": next_argv,
            "done_argv": done_argv,
            "report_schema": _report_schema(bool(state["contract"]["exit_checks"])),
            "verify_script": _verify_script(path, state, script_path=script_path, action_token=token),
        }
    )
    return packet


def _terminal_packet(
    *,
    status: str,
    path: Path,
    state: dict[str, Any],
    report: dict[str, str],
    trivial_streak: int,
    reason: str,
) -> dict[str, Any]:
    packet = _metadata(
        status=status,
        path=path,
        state=state,
        action_number=state["action_number"],
        trivial_streak=trivial_streak,
        last_report=report,
    )
    packet.update(
        {
            "instruction": (
                f"Run {status}: {reason} No further callbacks. Report outcome, progress, and verified vs. "
                "claimed evidence in concise Markdown (no invented ETAs/retries); if nested, return this terminal "
                "packet to the reporting owner."
            ),
            "next_argv": None,
            "done_argv": None,
            "report_schema": None,
            "verify_script": None,
        }
    )
    return packet


def _error_packet(error: StateError, state_path: Path | None = None, *, script_path: Path) -> dict[str, Any]:
    packet: dict[str, Any] = {
        "status": "error",
        "state_change": "unchanged",
        "instruction": (
            "Do not repeat work or infer a terminal outcome. This rejected input or cleanup "
            "failure did not advance the run. Correct input only from actual evidence. If the "
            "state is missing, it cannot distinguish lost terminal output from corruption or "
            "cancellation; never infer success or initialize a replacement run. If a status update "
            "is needed, use clear Markdown grounded only in this error and state_change: say that "
            "the result is unverified and correction or recovery is pending. Do not turn reporting "
            "into a retry, an advancement claim, or a new run."
        ),
        "error": str(error),
    }
    if state_path is not None:
        next_argv = _next_argv(state_path, script_path)
        packet.update(
            {
                "state_file": str(state_path),
                "next_argv": next_argv,
                "done_argv": None,
                "report_schema": None,
                "instruction": (
                    "Do not repeat work or invoke a callback from this error packet. This input "
                    "was rejected without advancing the run. Use this exact read-only next_argv "
                    "JSON array at most once to retrieve and validate a current packet before any correction: "
                    f"{json.dumps(next_argv)}. If that read cannot return a valid current packet, stop "
                    "incomplete and report uncertainty. Do not repeatedly retry next, replay work or done, "
                    "or initialize a replacement run. If a status update is needed, use clear Markdown "
                    "grounded only in this error, unchanged state, and any successful read-only recovery. "
                    "Keep protocol details internal unless they explain the problem, distinguish a rejected "
                    "claim from a verified outcome, and do not turn reporting into a retry or advancement."
                ),
            }
        )
    return packet


def _uncertain_error_packet(error: OSError, state_path: Path | None = None, *, script_path: Path) -> dict[str, Any]:
    packet: dict[str, Any] = {
        "status": "error",
        "state_change": "unknown",
        "instruction": (
            "Do not replay work, retry the callback, or assume advancement. A filesystem "
            "operation may have partially changed this run. A missing state cannot distinguish "
            "lost terminal output from corruption or cancellation; never infer success or "
            "initialize a replacement run. If a status update is needed, use clear Markdown grounded "
            "only in this error and unknown state change: report uncertainty rather than completion, "
            "and do not turn reporting into a retry, an advancement claim, or a new run."
        ),
        "error": str(error),
    }
    if state_path is not None:
        next_argv = _next_argv(state_path, script_path)
        packet.update(
            {
                "state_file": str(state_path),
                "next_argv": next_argv,
                "done_argv": None,
                "report_schema": None,
                "instruction": (
                    "Do not replay work, retry the callback, or assume advancement. A filesystem "
                    "operation may have partially changed this run. Use this exact read-only "
                    f"next_argv JSON array at most once to retrieve and validate a current packet: "
                    f"{json.dumps(next_argv)}. If that read cannot return a valid current packet, stop "
                    "incomplete and report uncertainty. Do not repeatedly retry next, replay work or done, "
                    "or initialize a replacement run. If a status update is needed, use clear Markdown "
                    "grounded only in this error, unknown state change, and any successful read-only recovery. "
                    "Report uncertainty rather than completion, keep protocol details internal unless they "
                    "explain the problem, and do not turn reporting into a retry or advancement."
                ),
            }
        )
    return packet
