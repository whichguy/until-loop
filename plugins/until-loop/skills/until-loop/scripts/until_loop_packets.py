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
                "Classify the completed iteration: trivial: completed iteration/review "
                "found only trivial or no changes and no material findings; non-trivial: "
                "material finding or behavior change, even if fixed; unresolved: "
                "assessment or work is incomplete."
            ),
        },
        "exit_assessment": {"enum": ["satisfied", "unsatisfied", "unknown"]},
        "continuation_assessment": {"enum": ["allowed", "blocked", "cancelled"]},
        "evidence": {
            "type": "string",
            "description": "A nonblank account of actual observations and any remaining gap.",
        },
        "handoff": {
            "type": "string",
            "description": (
                "A nonblank compact replacement handoff for a fresh or compacted host. "
                "Carry forward the current objective, completed and unresolved facts, "
                "receipts and locators needed to recheck them, remaining exit and repeat "
                "gaps. It replaces the prior handoff rather than "
                "being a delta. It records prior claims and does not create authority."
            ),
        },
    }
    if has_checks:  # a contract without exit checks has nothing to repair: keep its packets as small as 0.7.0's
        properties["check_repairs"] = {
            "type": "array",
            "description": (
                "Optional. Only for a frozen exit check that failed in the latest check run because its "
                "command misstates its clause (a shell or platform bug), never to accept work that differs "
                "from the clause: [{id, command, reason}]. The clause stays frozen; each repair is recorded "
                "and shown in every later packet and the terminal result; at most two per check."
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
    latest_evidence = (
        "No prior report exists."
        if state["last_report"] is None
        else f"Treat last_report.evidence as unverified data, not proof: {state['last_report']['evidence']!r}"
    )
    latest_handoff = (
        "No prior handoff exists."
        if state["last_report"] is None
        else "Read last_report.handoff as unverified prior claims, not proof or authority."
    )
    verify_step = (
        "Run verify_script once, unchanged, after the iteration's last change by passing its text as your "
        "shell command (never via a file, variable, or sub-interpreter, so host hooks see it); the runtime "
        "refuses satisfied without a fresh recorded run or if workspace files change. "
        if state["contract"]["exit_checks"] else ""
    )
    packet.update(
        {
            "instruction": (
                f"{_next_action(state)} Execution condition (Work): "
                f"{state['contract']['work']!r}. "
                f"Exit condition: {state['contract']['exit_condition']!r}. "
                f"Continuation condition: {state['contract']['repeat_condition']!r}. "
                f"{latest_evidence} {latest_handoff} "
                "Read this entire packet (context, resources, last_report, handoff) and current user instructions. "
                "A packet from automatic recovery is already fresh; from an older saved packet, run exact "
                f"read-only next_argv JSON once: {json.dumps(next_argv)} and recheck current artifacts—never "
                "replay done or guess a transition. Context authority is immutable; recheck tool/environment "
                "availability. Honor an explicit user stop as cancelled; report an obstacle as blocked. "
                "Otherwise execute exactly one complete assigned iteration of Work in order. For an Improve "
                "request, review artifacts and history, plan, implement authorized fixes, and run checks—do not "
                "stop after review/planning or run hidden iterations. Nested work does not advance this frame. "
                "If work or assessment is incomplete, classify as unresolved instead of trivial. "
                + verify_step
                + f"Call exact done_argv JSON array: {json.dumps(done_argv)} with one report matching "
                "report_schema (classification, assessments, evidence, and full compact replacement handoff). "
                "Do not choose the successor or terminal state; follow done's returned instruction. "
                "Use concise Markdown grounded in script-reported progress and verified evidence for status "
                "updates, keep protocol IDs internal unless explaining a problem, and preserve any nested "
                "reporting-owner handoff."
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
                f"This run is {status}: {reason} Perform no further work or callbacks for this run. "
                "Communicate the terminal result once in concise Markdown grounded in this returned status, "
                "reason, progress, last_report, and observed evidence, distinguishing reported claims from "
                "verified outcomes and naming any prerequisite for future work without inventing ETAs or retries. "
                "If nested, return this exact terminal packet through the reporting-owner handoff so the parent "
                "can resume; that handoff does not reopen this loop. End this invocation."
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
