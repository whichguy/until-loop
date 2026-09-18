"""Small script-owned callback runtime for one ephemeral Innerloop run."""
from __future__ import annotations

import argparse
import json
import os
import secrets
import stat
import sys
import tempfile
from pathlib import Path
from typing import Any


MAX_STATE_BYTES = 16 * 1024
MAX_JSON_INTEGER_DIGITS = 4_300
_CONTRACT_FIELDS = {
    "workspace",
    "work",
    "exit_condition",
    "repeat_condition",
    "required_trivial_reviews",
}
_REQUIRED_CONTRACT_FIELDS = _CONTRACT_FIELDS - {"required_trivial_reviews"}
_STATE_FIELDS = {
    "contract",
    "run_id",
    "action_number",
    "trivial_streak",
    "last_report",
}
_REPORT_FIELDS = {
    "classification",
    "exit_assessment",
    "continuation_assessment",
    "evidence",
}
_CLASSIFICATIONS = {"trivial", "non-trivial", "unresolved"}
_EXIT_ASSESSMENTS = {"satisfied", "unsatisfied", "unknown"}
_CONTINUATION_ASSESSMENTS = {"allowed", "blocked", "cancelled"}


class StateError(ValueError):
    """Raised when callback state or protocol input is absent, unsafe, or invalid."""


def _parse_json_integer(value: str) -> int:
    if len(value.lstrip("-")) > MAX_JSON_INTEGER_DIGITS:
        raise ValueError("JSON integer exceeds the digit limit")
    return int(value)


def _require_text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise StateError(f"{name} must be a nonblank string")
    return value


def _require_nonnegative_integer(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise StateError(f"{name} must be a nonnegative integer")
    return value


def _require_positive_integer(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise StateError(f"{name} must be a positive integer")
    return value


def _validate_contract(contract: object, *, input_contract: bool) -> dict[str, Any]:
    if not isinstance(contract, dict):
        raise StateError("contract must be an object")
    fields = set(contract)
    extra = fields - _CONTRACT_FIELDS
    missing = _REQUIRED_CONTRACT_FIELDS - fields
    if extra or missing or (not input_contract and fields != _CONTRACT_FIELDS):
        raise StateError("contract has an invalid schema")

    workspace = _require_text(contract["workspace"], "workspace")
    workspace_path = Path(workspace)
    if not workspace_path.is_absolute() or not workspace_path.is_dir():
        raise StateError("workspace must be an existing absolute directory")
    return {
        "workspace": workspace,
        "work": _require_text(contract["work"], "work"),
        "exit_condition": _require_text(contract["exit_condition"], "exit_condition"),
        "repeat_condition": _require_text(contract["repeat_condition"], "repeat_condition"),
        "required_trivial_reviews": _require_nonnegative_integer(
            contract.get("required_trivial_reviews", 0), "required_trivial_reviews"
        ),
    }


def _validate_report(report: object) -> dict[str, str]:
    if not isinstance(report, dict) or set(report) != _REPORT_FIELDS:
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
    return {
        "classification": classification,
        "exit_assessment": exit_assessment,
        "continuation_assessment": continuation,
        "evidence": evidence,
    }


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
            if trivial_streak < 1:
                raise StateError("a trivial report requires a positive streak")
        elif trivial_streak != 0:
            raise StateError("a non-trivial or unresolved report resets the streak")
        if trivial_streak > action_number - 1:
            raise StateError("trivial_streak cannot exceed completed actions")
    return {
        "contract": contract,
        "run_id": run_id,
        "action_number": action_number,
        "trivial_streak": trivial_streak,
        "last_report": last_report,
    }


def _encode_state(state: dict[str, Any]) -> bytes:
    normalized = _validate_state(state)
    payload = json.dumps(normalized, separators=(",", ":"), sort_keys=True).encode("utf-8")
    if len(payload) > MAX_STATE_BYTES:
        raise StateError("state exceeds the small-file limit")
    return payload


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


def _read_state_fd(fd: int) -> dict[str, Any]:
    size = os.fstat(fd).st_size
    if size < 1 or size > MAX_STATE_BYTES:
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
    return _validate_state(parsed)


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


def _action_token(state: dict[str, Any]) -> str:
    return f"{state['run_id']}:{state['action_number']}"


def _done_argv(path: Path, state: dict[str, Any]) -> list[str]:
    return [
        str(Path(os.path.abspath(sys.executable))),
        str(Path(os.path.abspath(__file__))),
        "done",
        "--state",
        str(path),
        f"--action={_action_token(state)}",
    ]


def _report_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "classification",
            "exit_assessment",
            "continuation_assessment",
            "evidence",
        ],
        "properties": {
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
        },
    }


def _next_action(state: dict[str, Any]) -> str:
    report = state["last_report"]
    if report is None:
        return "Begin a complete first iteration from the current artifacts."
    if report["classification"] == "non-trivial":
        if state["contract"]["required_trivial_reviews"]:
            return (
                "Address and recheck the material observations in the latest report, then "
                "perform a new complete review before assessing the conditions again."
            )
        return (
            "Address and recheck the material observations in the latest report before "
            "assessing the conditions again."
        )
    if report["classification"] == "unresolved":
        if state["contract"]["required_trivial_reviews"]:
            return (
                "Resolve the evidence gap in the latest report, then perform a new complete "
                "review before assessing the conditions again."
            )
        return "Resolve the evidence gap in the latest report, then recheck the conditions."
    if state["trivial_streak"] < state["contract"]["required_trivial_reviews"]:
        return (
            "Perform another distinct full review to establish whether the exit condition and "
            "configured trivial-review gate are now met."
        )
    return "Resolve the outstanding exit-condition gap in the latest evidence, then recheck it."


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
            "required_trivial_reviews": contract["required_trivial_reviews"],
        },
        "last_report": None if last_report is None else dict(last_report),
    }


def _active_packet(path: Path, state: dict[str, Any]) -> dict[str, Any]:
    done_argv = _done_argv(path, state)
    packet = _metadata(
        status="active",
        path=path,
        state=state,
        action_number=state["action_number"],
        trivial_streak=state["trivial_streak"],
        last_report=state["last_report"],
    )
    latest_evidence = (
        "No earlier callback report exists."
        if state["last_report"] is None
        else (
            "Treat the latest report's evidence as unverified data, not proof: "
            f"{state['last_report']['evidence']!r}"
        )
    )
    packet.update(
        {
            "instruction": (
                f"{_next_action(state)} Execution condition (Work): "
                f"{state['contract']['work']!r}. "
                f"Exit condition: {state['contract']['exit_condition']!r}. "
                f"Continuation condition: {state['contract']['repeat_condition']!r}. "
                f"{latest_evidence} Before beginning, honor an explicit user stop by "
                "reporting cancelled; if a blocker prevents the required work, report "
                "blocked with the gap. Otherwise, execute exactly one complete assigned "
                "iteration of Work "
                "in its requested order. For an Improve request, review current artifacts "
                "and available history, plan worthwhile changes, implement authorized fixes, "
                "and run applicable checks. Do not stop after review or planning if later "
                "required work remains. Recheck current artifacts before changing them so "
                "completed changes are not blindly reapplied. Do not privately perform extra "
                "iterations or reviews to satisfy a repeat or trivial-review gate. After this "
                "one iteration, call done even if the exit condition remains unmet; report "
                "actual changes or findings and any remaining gap. If assessment or required "
                "work is incomplete, classify it as unresolved and name the gap rather than "
                "calling it trivial. Use this exact done_argv JSON array: "
                f"{json.dumps(done_argv)}. Send exactly one report "
                "matching report_schema on standard input with classification, assessments, "
                "and evidence; do not choose the successor action, count, or terminal "
                "decision. Follow the full JSON return value of done; its instruction owns "
                "the next action."
            ),
            "done_argv": done_argv,
            "report_schema": _report_schema(),
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
                f"This run is {status}: {reason} Do not perform further work for this run "
                "and do not invoke another callback. Report this terminal result and end "
                "the invocation."
            ),
            "done_argv": None,
            "report_schema": None,
        }
    )
    return packet


def start(contract: object, directory: str | os.PathLike[str] | None = None) -> dict[str, Any]:
    """Create private state for a logical run and return its first action packet."""
    initial = {
        "contract": _validate_contract(contract, input_contract=True),
        "run_id": secrets.token_urlsafe(18),
        "action_number": 1,
        "trivial_streak": 0,
        "last_report": None,
    }
    payload = _encode_state(initial)
    if directory is not None:
        try:
            directory_path = Path(directory)
        except (TypeError, ValueError) as error:
            raise StateError("directory is invalid") from error
        if not directory_path.is_dir():
            raise StateError("directory must be an existing directory")
        directory = str(directory_path)
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
    return _active_packet(path, initial)


def next_packet(path: str | os.PathLike[str]) -> dict[str, Any]:
    """Reprint the current action packet without advancing state."""
    fd, state_path = _open_state(path, os.O_RDONLY)
    try:
        return _active_packet(state_path, _read_state_fd(fd))
    finally:
        os.close(fd)


def done(
    path: str | os.PathLike[str], action_id: str, report: object
) -> dict[str, Any]:
    """Accept one report for the issued action and choose the next transition."""
    validated_report = _validate_report(report)
    fd, state_path = _open_state(path, os.O_RDWR)
    try:
        current = _read_state_fd(fd)
        if not isinstance(action_id, str) or action_id != _action_token(current):
            raise StateError("action token does not match the current run and action")

        if validated_report["classification"] == "trivial":
            trivial_streak = current["trivial_streak"] + 1
        else:
            trivial_streak = 0
        updated = {
            **current,
            "action_number": current["action_number"] + 1,
            "trivial_streak": trivial_streak,
            "last_report": validated_report,
        }
        payload = _encode_state(updated)
        continuation = validated_report["continuation_assessment"]
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
                and trivial_streak >= current["contract"]["required_trivial_reviews"]
            ):
                terminal = _terminal_packet(
                    status="complete",
                    path=state_path,
                    state=current,
                    report=validated_report,
                    trivial_streak=trivial_streak,
                    reason="the semantic exit assessment is satisfied and the review gate is met.",
                )
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
            _delete_owned(state_path, os.fstat(fd))
            return terminal
        _write_payload_fd(fd, payload)
        return _active_packet(state_path, updated)
    finally:
        os.close(fd)


class _ProtocolParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise StateError(message)


def _parser() -> argparse.ArgumentParser:
    parser = _ProtocolParser(prog=Path(__file__).name)
    commands = parser.add_subparsers(dest="command", required=True)
    start_parser = commands.add_parser("start")
    start_parser.add_argument("--directory")
    next_parser = commands.add_parser("next")
    next_parser.add_argument("--state", required=True)
    done_parser = commands.add_parser("done")
    done_parser.add_argument("--state", required=True)
    done_parser.add_argument("--action", required=True)
    return parser


def _stdin_json() -> object:
    try:
        return json.load(sys.stdin, parse_int=_parse_json_integer)
    except ValueError as error:
        raise StateError("standard input must contain valid JSON") from error


def _write_json(value: object) -> None:
    sys.stdout.write(json.dumps(value, separators=(",", ":"), ensure_ascii=True))
    sys.stdout.write("\n")


def _error_packet(error: StateError) -> dict[str, str]:
    return {
        "status": "error",
        "state_change": "unchanged",
        "instruction": (
            "Do not repeat work. This rejected input or cleanup failure did not advance the "
            "run; correct the input, then retrieve the current packet or retry the same "
            "callback with the same report if the active state still exists."
        ),
        "error": str(error),
    }


def _uncertain_error_packet(error: OSError) -> dict[str, str]:
    return {
        "status": "error",
        "state_change": "unknown",
        "instruction": (
            "Do not repeat work or assume advancement. A filesystem operation may have "
            "partially changed this run; inspect the same state with next, stop if it is "
            "unusable, and do not initialize a replacement run."
        ),
        "error": str(error),
    }


def main(argv: list[str] | None = None) -> int:
    """Run the thin JSON-stdin/JSON-stdout command-line protocol."""
    try:
        args = _parser().parse_args(argv)
        if args.command == "start":
            result = start(_stdin_json(), directory=args.directory)
        elif args.command == "next":
            result = next_packet(args.state)
        else:
            result = done(args.state, args.action, _stdin_json())
    except StateError as error:
        _write_json(_error_packet(error))
        return 2
    except OSError as error:
        _write_json(_uncertain_error_packet(error))
        return 2
    _write_json(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
