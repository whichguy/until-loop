"""Script-owned frame policy with automatic session nesting and isolated compatibility."""
from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import stat
import sys
import tempfile
from pathlib import Path
from typing import Any

from until_loop_packets import (
    _active_packet as _render_active_packet,
    _action_token,
    _done_argv as _render_done_argv,
    _error_packet as _render_error_packet,
    _next_argv as _render_next_argv,
    _required_trivial_reviews,
    _terminal_packet,
    _uncertain_error_packet as _render_uncertain_error_packet,
)
from until_loop_verify import (
    CHANGED_DURING_VERIFY_ERROR,
    CHECK_OUTPUT_CHARS,
    MAX_REPAIRS_PER_CHECK,
    StateError,
    _REPAIR_FIELDS,
    _TREE_BEFORE,
    _TREE_ID,
    _bounded_lines,
    _check_command_syntax,
    _check_repair_not_vacuous,
    _parse_verify_output,
    _require_text,
    _validate_checks,
    _verify_script as _render_verify_script,
    _workspace_tree,
    finalize_verify_record,
    format_record_summary,
)


SCRIPT_PATH = Path(os.path.abspath(__file__))
MAX_JSON_INTEGER_DIGITS = 4_300
_CONTRACT_FIELDS = {
    "workspace",
    "work",
    "exit_condition",
    "repeat_condition",
    "required_trivial_reviews",
    "context",
    "exit_checks",
}
_REQUIRED_INPUT_CONTRACT_FIELDS = _CONTRACT_FIELDS - {"required_trivial_reviews", "exit_checks"}
_CONTEXT_FIELDS = {"request", "scope", "authority", "environment", "resources"}
_RESOURCE_FIELDS = {"purpose", "locator"}
_STATE_FIELDS = {
    "contract",
    "run_id",
    "action_number",
    "trivial_streak",
    "last_report",
    "start_tree",
    "receipt",
    "last_checks",
    "check_repairs",
    "verify_record",
}
_REPORT_FIELDS = {
    "classification",
    "exit_assessment",
    "continuation_assessment",
    "evidence",
    "handoff",
}
_CLASSIFICATIONS = {"trivial", "non-trivial", "unresolved"}
_EXIT_ASSESSMENTS = {"satisfied", "unsatisfied", "unknown"}
_CONTINUATION_ASSESSMENTS = {"allowed", "blocked", "cancelled"}


def _parse_json_integer(value: str) -> int:
    if len(value.lstrip("-")) > MAX_JSON_INTEGER_DIGITS:
        raise ValueError("JSON integer exceeds the digit limit")
    return int(value)


def _require_nonnegative_integer(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise StateError(f"{name} must be a nonnegative integer")
    return value


def _require_positive_integer(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise StateError(f"{name} must be a positive integer")
    return value


def _validate_context(context: object) -> dict[str, Any]:
    if not isinstance(context, dict) or set(context) != _CONTEXT_FIELDS:
        raise StateError("context has an invalid schema")
    resources = context["resources"]
    if not isinstance(resources, list):
        raise StateError("context.resources must be a list")
    normalized_resources: list[dict[str, str]] = []
    for index, resource in enumerate(resources):
        if not isinstance(resource, dict) or set(resource) != _RESOURCE_FIELDS:
            raise StateError(f"context.resources[{index}] has an invalid schema")
        normalized_resources.append(
            {
                "purpose": _require_text(resource["purpose"], f"context.resources[{index}].purpose"),
                "locator": _require_text(resource["locator"], f"context.resources[{index}].locator"),
            }
        )
    return {
        "request": _require_text(context["request"], "context.request"),
        "scope": _require_text(context["scope"], "context.scope"),
        "authority": _require_text(context["authority"], "context.authority"),
        "environment": _require_text(context["environment"], "context.environment"),
        "resources": normalized_resources,
    }


def _next_argv(path: Path) -> list[str]:
    return _render_next_argv(path, SCRIPT_PATH)


def _done_argv(path: Path, state: dict[str, Any]) -> list[str]:
    return _render_done_argv(path, state, SCRIPT_PATH)


def _verify_script(path: Path, state: dict[str, Any]) -> str | None:
    return _render_verify_script(path, state, script_path=SCRIPT_PATH, action_token=_action_token(state))


def _active_packet(path: Path, state: dict[str, Any]) -> dict[str, Any]:
    return _render_active_packet(path, state, SCRIPT_PATH)


def _error_packet(error: StateError, state_path: Path | None = None) -> dict[str, Any]:
    return _render_error_packet(error, state_path, script_path=SCRIPT_PATH)


def _uncertain_error_packet(error: OSError, state_path: Path | None = None) -> dict[str, Any]:
    return _render_uncertain_error_packet(error, state_path, script_path=SCRIPT_PATH)


def _validate_contract(contract: object, *, input_contract: bool) -> dict[str, Any]:
    if not isinstance(contract, dict):
        raise StateError("contract must be an object")
    fields = set(contract)
    if "context" not in fields:
        raise StateError(
            "contract.context is required; runs without immutable continuity context "
            "are not supported, so start a new run with a complete context"
        )
    extra = fields - _CONTRACT_FIELDS
    missing = (_REQUIRED_INPUT_CONTRACT_FIELDS if input_contract else _CONTRACT_FIELDS - {"exit_checks"}) - fields
    if extra or missing:
        raise StateError("contract has an invalid schema")

    workspace = _require_text(contract["workspace"], "workspace")
    workspace_path = Path(workspace)
    if not workspace_path.is_absolute() or (input_contract and not workspace_path.is_dir()):
        raise StateError("workspace must be an existing absolute directory")
    normalized: dict[str, Any] = {
        "workspace": workspace,
        "work": _require_text(contract["work"], "work"),
        "exit_condition": _require_text(contract["exit_condition"], "exit_condition"),
        "repeat_condition": _require_text(contract["repeat_condition"], "repeat_condition"),
    }
    normalized["required_trivial_reviews"] = _require_nonnegative_integer(
        contract.get("required_trivial_reviews", 0), "required_trivial_reviews"
    )
    normalized["context"] = _validate_context(contract["context"])
    normalized["exit_checks"] = _validate_checks(contract.get("exit_checks", []), parse=input_contract)
    return normalized


def _validate_report(report: object) -> dict[str, Any]:
    if not isinstance(report, dict):
        raise StateError("report has an invalid schema")
    fields = set(report)
    if "handoff" not in fields:
        raise StateError(
            "report.handoff is required; reports without a continuation handoff are not supported"
        )
    if fields - {"check_repairs"} != _REPORT_FIELDS:
        raise StateError("report has an invalid schema")
    classification = report["classification"]
    if not isinstance(classification, str) or classification not in _CLASSIFICATIONS:
        raise StateError("report.classification is invalid")
    exit_assessment = report["exit_assessment"]
    if not isinstance(exit_assessment, str) or exit_assessment not in _EXIT_ASSESSMENTS:
        raise StateError("report.exit_assessment is invalid")
    continuation = report["continuation_assessment"]
    if not isinstance(continuation, str) or continuation not in _CONTINUATION_ASSESSMENTS:
        raise StateError("report.continuation_assessment is invalid")
    evidence = _require_text(report["evidence"], "report.evidence")
    if classification == "unresolved" and exit_assessment == "satisfied":
        raise StateError("an unresolved report cannot claim a satisfied exit")
    validated: dict[str, Any] = {
        "classification": classification,
        "exit_assessment": exit_assessment,
        "continuation_assessment": continuation,
        "evidence": evidence,
        "handoff": _require_text(report["handoff"], "report.handoff"),
    }
    if "check_repairs" in report:
        repairs = report["check_repairs"]
        if not isinstance(repairs, list) or not all(isinstance(r, dict) and set(r) == _REPAIR_FIELDS for r in repairs):
            raise StateError("report.check_repairs must be a list of {id, command, reason}")
        validated["check_repairs"] = [
            {k: _require_text(r[k], f"check_repairs.{k}") for k in ("id", "command", "reason")}
            for r in repairs
        ]
    return validated


def _validate_state(state: object) -> dict[str, Any]:
    if not isinstance(state, dict) or set(state) != _STATE_FIELDS:
        raise StateError("state has an invalid schema")
    contract = _validate_contract(state["contract"], input_contract=False)
    run_id = _require_text(state["run_id"], "run_id")
    action_number = _require_positive_integer(state["action_number"], "action_number")
    trivial_streak = _require_nonnegative_integer(state["trivial_streak"], "trivial_streak")
    last_report = state["last_report"]
    if last_report is None:
        if action_number != 1 or trivial_streak != 0:
            raise StateError("initial state must have action number one and no streak")
    else:
        last_report = _validate_report(last_report)
        if action_number < 2:
            raise StateError("state with a report must have a successor action")
        if last_report["classification"] == "trivial":
            # A failed frozen check resets the streak even for a report that called itself trivial; later
            # records replace last_checks, so with checks a trivial report may legitimately carry no streak.
            if trivial_streak < 1 and not contract["exit_checks"]:
                raise StateError("a trivial report requires a positive streak")
        elif trivial_streak != 0:
            raise StateError("a non-trivial or unresolved report resets the streak")
        if trivial_streak > action_number - 1:
            raise StateError("trivial_streak cannot exceed completed actions")
    start_tree = state["start_tree"]
    if start_tree is not None and (not isinstance(start_tree, str) or not _TREE_ID.fullmatch(start_tree)):
        raise StateError("start_tree must be a Git tree ID or null")
    receipt = state["receipt"]
    if receipt is not None:
        receipt = _require_text(receipt, "receipt")
        if not Path(receipt).is_absolute():
            raise StateError("receipt must be an absolute path")
    last_checks = state["last_checks"]
    if last_checks is not None and not (
        isinstance(last_checks, list)
        and all(isinstance(r, dict) and set(r) == {"id", "passed", "exit_code", "output_tail"} for r in last_checks)
    ):
        raise StateError("last_checks has an invalid schema")
    check_repairs = state["check_repairs"]
    if not isinstance(check_repairs, list):
        raise StateError("check_repairs has an invalid schema")
    verify_record = state["verify_record"]
    if verify_record is not None and not (
        isinstance(verify_record, dict)
        and {"action_number", "tree"} <= set(verify_record) <= {"action_number", "tree", "action_token", "changed"}
        and ("action_token" not in verify_record or isinstance(verify_record["action_token"], str))
        and ("changed" not in verify_record or type(verify_record["changed"]) is bool)
        and type(verify_record["action_number"]) is int
        and verify_record["action_number"] >= 1
        and (verify_record["tree"] is None or (isinstance(verify_record["tree"], str)
                                               and _TREE_ID.fullmatch(verify_record["tree"])))
    ):
        raise StateError("verify_record has an invalid schema")
    return {
        "contract": contract,
        "run_id": run_id,
        "action_number": action_number,
        "trivial_streak": trivial_streak,
        "last_report": last_report,
        "start_tree": start_tree,
        "receipt": receipt,
        "last_checks": last_checks,
        "check_repairs": check_repairs,
        "verify_record": verify_record,
    }


def _receipt_path(value: object) -> Path:
    """An absolute receipt file path whose parent exists and which is not a directory or link."""
    if not isinstance(value, str) or not value.strip():
        raise StateError("receipt must be a nonblank absolute path")
    path = Path(value)
    if not path.is_absolute():
        raise StateError("receipt must be an absolute path")
    if not path.parent.is_dir():
        raise StateError("receipt's parent must be an existing directory")
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise StateError("receipt must be a regular file path, not a link or directory")
    return path


def _check_start_receipt(path: Path) -> None:
    """A new run may replace a terminal receipt, never another run's live files."""
    if not path.exists():
        return
    try:
        previous = json.loads(path.read_text(encoding="utf-8"), parse_int=_parse_json_integer)
    except (OSError, UnicodeError, ValueError, RecursionError):
        previous = None
    if (isinstance(previous, dict) and previous.get("status") in ("complete", "stopped")
            and previous.get("receipt") == str(path)
            and isinstance(previous.get("state_file"), str)
            and Path(previous["state_file"]).is_absolute()
            and not os.path.lexists(previous["state_file"])
            and all(key in previous and previous[key] is None
                    for key in ("next_argv", "done_argv", "report_schema"))):
        return
    raise StateError(
        f"receipt already exists and is not a finished Until Loop receipt: {path}. "
        "Nothing was started and the file is unchanged. For this run's active receipt, "
        "read its next_argv to resume; for a separate child run, use a distinct receipt path."
    )


def _write_receipt(state: dict[str, Any], packet: dict[str, Any]) -> None:
    """Atomically save the packet at the run's receipt path before it is printed."""
    if state.get("receipt") is None:
        return
    path = _receipt_path(state["receipt"])
    payload = (json.dumps(packet, separators=(",", ":"), ensure_ascii=True) + "\n").encode("ascii")
    fd, temporary = tempfile.mkstemp(prefix=".receipt-", suffix=".json", dir=str(path.parent))
    try:
        try:
            os.fchmod(fd, 0o600)
            _write_payload_fd(fd, payload)
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(temporary, path)
    except BaseException:
        if os.path.exists(temporary):
            os.unlink(temporary)
        raise


def _encode_state(state: dict[str, Any]) -> bytes:
    normalized = _validate_state(state)
    # No size cap (owner, 2026-10-11): a long-form request, its checks and a full handoff must never be refused for
    # length. The state does not grow with iterations: each report and handoff replaces the last.
    return json.dumps(normalized, separators=(",", ":"), sort_keys=True).encode("utf-8")


def _state_path(path: str | os.PathLike[str]) -> Path:
    try:
        candidate = Path(path)
    except (TypeError, ValueError) as error:
        raise StateError("state path is invalid") from error
    return candidate if candidate.is_absolute() else Path(os.path.abspath(candidate))


def _open_state(path: str | os.PathLike[str], flags: int) -> tuple[int, Path]:
    candidate = _state_path(path)
    try:
        before = os.lstat(candidate)
    except OSError as error:
        raise StateError(f"state file is unavailable: {candidate}") from error
    if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
        raise StateError("state file must be a regular single-link file")
    try:
        fd = os.open(candidate, flags | getattr(os, "O_NOFOLLOW", 0))
    except OSError as error:
        raise StateError(f"cannot open state file: {candidate}") from error
    try:
        after = os.fstat(fd)
        if (
            not stat.S_ISREG(after.st_mode)
            or after.st_nlink != 1
            or (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino)
        ):
            raise StateError("state file changed or is unsafe")
    except BaseException:
        os.close(fd)
        raise
    return fd, candidate


def _read_document_fd(fd: int) -> object:
    size = os.fstat(fd).st_size
    if size < 1:
        raise StateError("state file has an invalid size")
    os.lseek(fd, 0, os.SEEK_SET)
    payload = bytearray()
    while len(payload) < size:
        chunk = os.read(fd, size - len(payload))
        if not chunk:
            raise StateError("state file changed while being read")
        payload.extend(chunk)
    try:
        parsed = json.loads(payload.decode("utf-8"), parse_int=_parse_json_integer)
    except ValueError as error:
        raise StateError("state file is not valid JSON") from error
    return parsed


def _read_state_fd(fd: int) -> dict[str, Any]:
    return _validate_state(_read_document_fd(fd))


def _write_payload_fd(fd: int, payload: bytes) -> None:
    os.lseek(fd, 0, os.SEEK_SET)
    os.ftruncate(fd, 0)
    written = 0
    while written < len(payload):
        count = os.write(fd, payload[written:])
        if count <= 0:
            raise OSError("could not write state file")
        written += count


def _write_state_fd(fd: int, state: dict[str, Any]) -> None:
    _write_payload_fd(fd, _encode_state(state))


def _delete_owned(path: Path, identity: os.stat_result) -> None:
    try:
        current = os.lstat(path)
    except OSError as error:
        raise StateError("terminal state file is unavailable for deletion") from error
    if (
        not stat.S_ISREG(current.st_mode)
        or current.st_nlink != 1
        or (current.st_dev, current.st_ino) != (identity.st_dev, identity.st_ino)
    ):
        raise StateError("terminal state file changed before deletion")
    try:
        path.unlink()
    except OSError as error:
        raise StateError("could not delete terminal state file") from error


def _discard_created_file(path: Path, identity: os.stat_result | None) -> None:
    if identity is None:
        return
    try:
        current = os.lstat(path)
        if (
            stat.S_ISREG(current.st_mode)
            and current.st_nlink == 1
            and (current.st_dev, current.st_ino) == (identity.st_dev, identity.st_ino)
        ):
            path.unlink()
    except OSError:
        pass


def read_state(path: str | os.PathLike[str]) -> dict[str, Any]:
    """Read active state without creating, repairing, or advancing it."""
    fd, _ = _open_state(path, os.O_RDONLY)
    try:
        return _read_state_fd(fd)
    finally:
        os.close(fd)


def record(path: str | os.PathLike[str], action_id: str, stream: Any, echo: Any) -> str:
    """Record one verify_script run for the current action; return a one-line summary."""
    mismatch = ("action token does not match the current run and action; run the current packet's "
                "verify_script")
    fd, _ = _open_state(path, os.O_RDONLY)
    try:
        before = _read_state_fd(fd)
    finally:
        os.close(fd)
    if not isinstance(action_id, str) or action_id != _action_token(before):
        raise StateError(mismatch)
    results, initial_tree = _parse_verify_output(
        stream,
        before["contract"],
        echo,
        before_fallback=lambda: _workspace_tree(before["contract"]["workspace"]),
    )
    # Commit only if the run is still at the same action: a late recorder never overwrites a newer action.
    fd, _ = _open_state(path, os.O_RDWR)
    try:
        current = _read_state_fd(fd)
        if action_id != _action_token(current) or current["contract"] != before["contract"]:
            raise StateError(mismatch)
        verify_record, changed = finalize_verify_record(
            current["contract"]["workspace"],
            current["action_number"],
            initial_tree,
        )
        updated = {**current, "last_checks": results, "verify_record": verify_record}
        _write_payload_fd(fd, _encode_state(updated))
    finally:
        os.close(fd)
    if changed:
        raise StateError(CHANGED_DURING_VERIFY_ERROR)
    return format_record_summary(results)


def _initial_state(contract: object, receipt: str | None = None) -> dict[str, Any]:
    initial = {
        "contract": _validate_contract(contract, input_contract=True),
        "run_id": secrets.token_urlsafe(18),
        "action_number": 1,
        "trivial_streak": 0,
        "last_report": None,
        "start_tree": None,
        "receipt": None if receipt is None else str(_receipt_path(receipt)),
        "last_checks": None,
        "check_repairs": [],
        "verify_record": None,
    }
    # A gate of two or more reviews can close on one trivial pass that changed nothing:
    # record the workspace so the runtime, not the report, decides that.
    if initial["contract"]["required_trivial_reviews"] >= 2:
        initial["start_tree"] = _workspace_tree(initial["contract"]["workspace"])
    return initial


def start(
    contract: object,
    directory: str | os.PathLike[str] | None = None,
    receipt: str | None = None,
) -> dict[str, Any]:
    """Create private state for a logical run and return its first action packet."""
    initial = _initial_state(contract, receipt)
    payload = _encode_state(initial)
    if directory is not None:
        try:
            directory_path = Path(directory)
        except (TypeError, ValueError) as error:
            raise StateError("directory is invalid") from error
        if not directory_path.is_dir():
            raise StateError("directory must be an existing directory")
        directory = str(directory_path)
    if initial["receipt"] is not None:
        _check_start_receipt(_receipt_path(initial["receipt"]))
    try:
        fd, raw_path = tempfile.mkstemp(prefix="innerloop-", suffix=".json", dir=directory)
    except OSError as error:
        raise StateError("could not create state file") from error
    path = _state_path(raw_path)
    identity: os.stat_result | None = None
    try:
        identity = os.fstat(fd)
        os.fchmod(fd, 0o600)
        _write_payload_fd(fd, payload)
    except BaseException:
        _discard_created_file(path, identity)
        raise
    finally:
        os.close(fd)
    packet = _active_packet(path, initial)
    try:
        _write_receipt(initial, packet)
    except BaseException:
        # A run whose receipt cannot be written would not have its durable copy.
        _discard_created_file(path, identity)
        raise
    return packet


def next_packet(path: str | os.PathLike[str]) -> dict[str, Any]:
    """Reprint the current action packet without advancing state."""
    fd, state_path = _open_state(path, os.O_RDONLY)
    try:
        state = _read_state_fd(fd)
        packet = _active_packet(state_path, state)
    finally:
        os.close(fd)
    # Refresh the durable copy too; this never advances the run.
    _write_receipt(state, packet)
    return packet


def _transition(current: dict[str, Any], state_path: Path, report: object) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Decide one frame transition; storage and return routing belong to the caller."""
    validated_report = _validate_report(report)
    repairs = validated_report.pop("check_repairs", [])
    repair_log = list(current["check_repairs"])
    if repairs:
        failed = {r["id"] for r in (current["last_checks"] or []) if not r["passed"]}
        checks = [dict(c) for c in current["contract"]["exit_checks"]]
        by_id = {c["id"]: c for c in checks}
        for r in repairs:
            if r["id"] not in by_id:
                raise StateError(f"check_repairs names unknown check {r['id']!r}")
            if r["id"] not in failed:
                raise StateError(f"check {r['id']!r} can be repaired only after it failed in the latest check run")
            if sum(1 for x in repair_log if x["id"] == r["id"]) >= MAX_REPAIRS_PER_CHECK:
                raise StateError(f"check {r['id']!r} has used its {MAX_REPAIRS_PER_CHECK} repairs; report blocked instead")
            _check_command_syntax(r["id"], r["command"])
            _check_repair_not_vacuous(r["id"], r["command"])
            repair_log.append({
                "id": r["id"],
                "clause": by_id[r["id"]]["clause"],
                "old_command": by_id[r["id"]]["command"],
                "new_command": r["command"],
                "reason": r["reason"],
                "action_number": current["action_number"],
            })
            by_id[r["id"]]["command"] = r["command"]
        current = {**current, "contract": {**current["contract"], "exit_checks": checks}}
    current = {**current, "check_repairs": repair_log}
    continuation = validated_report["continuation_assessment"]
    recorded = current["verify_record"]
    fresh = (recorded is not None and recorded["action_number"] == current["action_number"]
             and ("action_token" not in recorded or recorded["action_token"] == _action_token(current)))
    check_results = current["last_checks"] if fresh else None
    if (validated_report["exit_assessment"] == "satisfied" and continuation != "cancelled"
            and current["contract"]["exit_checks"]):
        if repairs:
            raise StateError("check_repairs cannot accompany a satisfied exit: report the repair with an "
                             "unsatisfied exit, then run the next packet's verify_script")
        if not fresh:
            raise StateError("no verify run is recorded for this action: run this packet's verify_script with "
                             "your own shell tool after the iteration's last change, then report again")
        if recorded.get("changed"):
            raise StateError("the workspace changed during the recorded verify run: run this packet's "
                             "verify_script again on a quiet workspace, then report again")
        if recorded["tree"] is not None and _workspace_tree(current["contract"]["workspace"]) != recorded["tree"]:
            raise StateError("the workspace changed after the recorded verify run: run this packet's "
                             "verify_script again after the last change, then report again")
    checks_pass = (
        check_results is None
        or (not (fresh and recorded.get("changed")) and all(r["passed"] for r in check_results))
    )

    if validated_report["classification"] == "trivial" and checks_pass:
        trivial_streak = current["trivial_streak"] + 1
    else:
        # A failed frozen check contradicts a clean assessment, so it never counts toward the gate.
        trivial_streak = 0
    # Owner decision 2026-09-26: a trivial first pass that left the workspace
    # byte-for-byte unchanged (checked here from Git, not taken from the report)
    # meets the review gate; a second pass would review the same tree again.
    unchanged_first_pass = (
        validated_report["classification"] == "trivial"
        and checks_pass
        and current["action_number"] == 1
        and current["start_tree"] is not None
        and _workspace_tree(current["contract"]["workspace"]) == current["start_tree"]
    )
    updated = {
        **current,
        "action_number": current["action_number"] + 1,
        "trivial_streak": trivial_streak,
        "last_report": validated_report,
        "last_checks": current["last_checks"],
    }
    # The contract and report must fit on every path (terminal receipts stay bounded); runtime check
    # output and the repair log count only against the state an active run keeps.
    _encode_state({
        **updated,
        "check_repairs": [],
        "last_checks": None if updated["last_checks"] is None
        else [{**r, "output_tail": ""} for r in updated["last_checks"]],
    })
    if continuation == "cancelled":
        terminal = _terminal_packet(
            status="stopped",
            path=state_path,
            state=current,
            report=validated_report,
            trivial_streak=trivial_streak,
            reason="the callback explicitly cancelled it.",
        )
    else:
        if (
            validated_report["exit_assessment"] == "satisfied"
            and checks_pass
            and (trivial_streak >= _required_trivial_reviews(current) or unchanged_first_pass)
        ):
            terminal = _terminal_packet(
                status="complete",
                path=state_path,
                state=current,
                report=validated_report,
                trivial_streak=trivial_streak,
                reason=("the semantic exit assessment is satisfied and the first pass was trivial with "
                        "the workspace unchanged, which meets the review gate."
                        if unchanged_first_pass and trivial_streak < _required_trivial_reviews(current)
                        else "the semantic exit assessment is satisfied and the review gate is met."),
            )
            if unchanged_first_pass and trivial_streak < _required_trivial_reviews(current):
                terminal["progress"]["unchanged_first_pass"] = True
        elif continuation == "blocked":
            terminal = _terminal_packet(
                status="stopped",
                path=state_path,
                state=current,
                report=validated_report,
                trivial_streak=trivial_streak,
                reason="the continuation assessment is blocked before completion.",
            )
        else:
            terminal = None

    if terminal is not None:
        terminal["last_check_results"] = check_results
        if current["check_repairs"]:
            terminal["instruction"] += (
                " This run repaired frozen exit checks: list each repair (clause, old and new command, "
                "reason) in the terminal report so the user can confirm none loosened its clause."
            )
    return updated, terminal


def done(
    path: str | os.PathLike[str], action_id: str, report: object
) -> dict[str, Any]:
    """Accept one report for the issued action and choose the next transition."""
    fd, state_path = _open_state(path, os.O_RDWR)
    try:
        current = _read_state_fd(fd)
        if not isinstance(action_id, str) or action_id != _action_token(current):
            raise StateError("action token does not match the current run and action")
        updated, terminal = _transition(current, state_path, report)
        if terminal is not None:
            # Persist the terminal packet before its state disappears.
            _write_receipt(current, terminal)
            _delete_owned(state_path, os.fstat(fd))
            return terminal
        # Only an active run keeps state; the terminal path above has already returned.
        _write_payload_fd(fd, _encode_state(updated))
        packet = _active_packet(state_path, updated)
        _write_receipt(updated, packet)
        return packet
    finally:
        os.close(fd)


class _ProtocolParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise StateError(message)


def _parser() -> argparse.ArgumentParser:
    parser = _ProtocolParser(prog=Path(__file__).name)
    commands = parser.add_subparsers(dest="command", required=True)
    start_parser = commands.add_parser("start")
    start_parser.add_argument("--directory", help="legacy isolated state directory")
    start_parser.add_argument("--isolated", action="store_true", help="legacy isolated run, without session nesting")
    start_parser.add_argument("--receipt", help="absolute file the runtime writes every returned packet to")
    next_parser = commands.add_parser("next")
    next_parser.add_argument("--state", help="omit to recover the host session stack")
    done_parser = commands.add_parser("done")
    done_parser.add_argument("--state", required=True)
    done_parser.add_argument("--action", required=True)
    record_parser = commands.add_parser("record", help="read a verify_script run on stdin and record its results")
    record_parser.add_argument("--state", required=True)
    record_parser.add_argument("--action", required=True)
    snapshot_parser = commands.add_parser("snapshot", help="print the current workspace Git tree for verify_script")
    snapshot_parser.add_argument("--workspace", required=True)
    checkpoint_parser = commands.add_parser("checkpoint", help="save partial progress without advancing")
    checkpoint_parser.add_argument("--state")
    checkpoint_parser.add_argument("--action", required=True)
    return parser


def _stdin_json() -> object:
    try:
        return json.load(sys.stdin, parse_int=_parse_json_integer)
    except ValueError as error:
        raise StateError("standard input must contain valid JSON") from error


def _write_json(value: object) -> None:
    sys.stdout.write(json.dumps(value, separators=(",", ":"), ensure_ascii=True))
    sys.stdout.write("\n")


def _known_state_path(args: argparse.Namespace | None) -> Path | None:
    if args is None or getattr(args, "command", None) not in {"start", "next", "done", "record", "checkpoint"}:
        return None
    try:
        return _state_path(args.state) if args.state is not None else None
    except (AttributeError, StateError):
        return None


def main(argv: list[str] | None = None) -> int:
    """Run the thin JSON-stdin/JSON-stdout command-line protocol."""
    args: argparse.Namespace | None = None
    try:
        args = _parser().parse_args(argv)
        if args.command == "snapshot":
            tree = _workspace_tree(args.workspace)
            sys.stdout.write(f"{_TREE_BEFORE} {tree or 'none'}\n")
            return 0
        # Import here so the legacy Python API remains usable as one standalone module.
        import until_loop_session as session
        automatic_start = args.command == "start" and not (
            args.directory is not None or args.receipt is not None or args.isolated
        )
        session_path = None
        if automatic_start or (args.command in {"next", "checkpoint"} and args.state is None):
            session_path, session_key = session.location()
            args.state = str(session_path)  # Also supplies recovery on uncertain writes.
        session_command = automatic_start or (
            args.command != "start"
            and (session_path is not None or session.is_session(_state_path(args.state)))
        )
        # A fork can retain a valid parent's --state and action token in history.
        # Bind session mutations to the caller as well as the current action.
        mutation_key = None
        if session_command and args.command in {"done", "checkpoint", "record"}:
            _, mutation_key = session.location(create=False)
        if args.command == "record":
            try:
                summary = (
                    session.record(_state_path(args.state), args.action, sys.stdin.buffer, sys.stdout, key=mutation_key)
                    if session_command
                    else record(args.state, args.action, sys.stdin.buffer, sys.stdout)
                )
            except (StateError, OSError) as error:
                # An atomic commit may precede a failed fsync: never assert 'nothing recorded' here.
                sys.stdout.write(f"UNTIL-LOOP-RECORD-ERROR {error}: recover next before reporting success\n")
                return 2
            sys.stdout.write(summary + "\n")
            return 0
        if args.command == "start":
            contract = _stdin_json()
            result = (
                session.start(session_path, session_key, contract)
                if automatic_start
                else start(contract, directory=args.directory, receipt=args.receipt)
            )
        elif args.command == "next":
            result = (
                session.next_packet(
                    _state_path(args.state),
                    key=session_key if session_path is not None else None,
                )
                if session_command
                else next_packet(args.state)
            )
        elif args.command == "checkpoint":
            if not session_command:
                raise StateError("checkpoint requires a session stack")
            result = session.checkpoint(_state_path(args.state), args.action, _stdin_json(), key=mutation_key)
        else:
            result = (
                session.done(_state_path(args.state), args.action, _stdin_json(), key=mutation_key)
                if session_command
                else done(args.state, args.action, _stdin_json())
            )
    except StateError as error:
        _write_json(_error_packet(error, _known_state_path(args)))
        return 2
    except OSError as error:
        _write_json(_uncertain_error_packet(error, _known_state_path(args)))
        return 2
    _write_json(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
