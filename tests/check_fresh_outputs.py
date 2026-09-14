#!/usr/bin/env python3
"""Structural validation for retained fresh-context model outputs.

The checker intentionally validates protocol shape and frozen-adapter
admissibility only.  It never executes a model-proposed command, verifier, or
assessment, and it does not decide whether a model's evidence is true.
"""
from __future__ import annotations

import argparse
import contextlib
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
from importlib.machinery import SourceFileLoader
from pathlib import Path
from typing import Any, Mapping, Sequence

import fresh_context as fresh


HERE = Path(__file__).resolve()
DEFAULT_REVIEW_ROOT = fresh.DEFAULT_REVIEW_ROOT


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def load_runtime(skill_root: Path) -> Any:
    """Load the frozen runtime only for its pure validators."""
    name = f"_fresh_output_validator_{os.getpid()}_{id(skill_root)}"
    source = skill_root / "scripts" / "until_loop_v2.py"
    loader = SourceFileLoader(name, str(source))
    spec = importlib.util.spec_from_loader(name, loader)
    if spec is None:
        raise RuntimeError(f"cannot load frozen runtime: {source}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    prior_no_bytecode = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        loader.exec_module(module)
    finally:
        sys.dont_write_bytecode = prior_no_bytecode
    return module


def read_json_text(path: Path) -> tuple[Any | None, str | None]:
    if not path.is_file():
        return None, "final output is absent"
    try:
        return json.loads(path.read_text(encoding="utf-8")), None
    except (OSError, json.JSONDecodeError) as error:
        return None, f"final output is not JSON: {error}"


def read_state(workspace: Path) -> dict[str, Any]:
    value = json.loads((workspace / ".until-loop" / "state.json").read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise RuntimeError(f"state is not an object: {workspace}")
    return value


def options(arguments: list[str]) -> tuple[dict[str, str] | None, str | None]:
    if len(arguments) % 2:
        return None, "options must be name/value pairs"
    result: dict[str, str] = {}
    for index in range(0, len(arguments), 2):
        name, value = arguments[index:index + 2]
        if not name.startswith("--") or name in result or not isinstance(value, str) or not value:
            return None, "options must be unique nonempty --name/value pairs"
        result[name] = value
    return result, None


def command_shape(argv: Any, snapshot: Path, state: Mapping[str, Any]) -> dict[str, Any]:
    """Accept only literal frozen-adapter argv forms; never invoke them."""
    if argv is None:
        return {"status": "incomplete", "reason": "no runtime command was supplied"}
    if not isinstance(argv, list) or not argv or not all(isinstance(item, str) for item in argv):
        return {"status": "invalid", "reason": "runtime_command_argv must be a string array or null"}
    if any("\x00" in item for item in argv):
        return {"status": "invalid", "reason": "runtime_command_argv contains a NUL byte"}
    trusted_python = str(Path(sys.executable).resolve())
    try:
        interpreter = Path(argv[0]).resolve()
        script = Path(argv[1]).resolve() if len(argv) > 1 else None
    except (OSError, ValueError) as error:
        return {"status": "invalid", "reason": f"command path is invalid: {error}"}
    if interpreter != Path(trusted_python):
        return {"status": "invalid", "reason": "interpreter is not the trusted sys.executable realpath"}
    direct = str((snapshot / "scripts" / "until_loop_v2.py").resolve())
    wrapper = str((snapshot / "scripts" / "until-loop").resolve())
    if len(argv) >= 3 and str(script) == direct:
        command_index = 2
        form = "direct-v2"
    elif len(argv) >= 4 and str(script) == wrapper and argv[2] == "v2":
        command_index = 3
        form = "wrapper-v2"
    else:
        return {"status": "invalid", "reason": "command is not a frozen v2 adapter argv form"}
    command = argv[command_index]
    if command not in {"next", "submit", "resume", "resolve-verifier"}:
        return {"status": "invalid", "reason": "command is not allowlisted"}
    parsed, error = options(argv[command_index + 1:])
    if error:
        return {"status": "invalid", "reason": error}
    assert parsed is not None
    expected = {
        "next": {"--repo"},
        "submit": {"--repo", "--action-id"},
        "resume": {"--repo", "--provenance-file"},
        "resolve-verifier": {"--repo", "--action-id", "--resolution-file"},
    }[command]
    if set(parsed) != expected:
        return {"status": "invalid", "reason": f"{command} options do not match the fixed allowlist"}
    if parsed["--repo"] != state.get("repo_root"):
        return {"status": "invalid", "reason": "--repo is not the exact bound workspace"}
    action = state.get("action") if isinstance(state.get("action"), dict) else {}
    recovery = state.get("recovery") if isinstance(state.get("recovery"), dict) else {}
    if command == "submit":
        if state.get("phase") != "active" or recovery or parsed["--action-id"] != action.get("id"):
            return {"status": "invalid", "reason": "submit does not target the current active action"}
    if command == "resume" and state.get("phase") != "paused":
        return {"status": "invalid", "reason": "resume is not applicable to this phase"}
    if command == "resolve-verifier":
        if not recovery or parsed["--action-id"] != recovery.get("action_id"):
            return {"status": "invalid", "reason": "resolve-verifier does not target the recovery action"}
    return {"status": "accepted", "form": form, "command": command, "options": parsed}


def decode_input(raw: Any, command: str | None) -> dict[str, Any]:
    if raw is None:
        return {
            "status": "not-required" if command == "next" else "incomplete",
            "reason": "no input record is required for next" if command == "next" else "no runtime input JSON was supplied",
        }
    if not isinstance(raw, str):
        return {"status": "invalid", "reason": "runtime_input_json must be a JSON string or null"}
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as error:
        return {"status": "invalid", "reason": f"runtime_input_json is invalid JSON: {error}"}
    if not isinstance(value, dict):
        return {"status": "invalid", "reason": "runtime input must decode to an object"}
    return {"status": "decoded", "value": value}


def pure_validate(runtime: Any, command: str | None, payload: Mapping[str, Any], state: Mapping[str, Any]) -> dict[str, Any]:
    """Use frozen pure validators, catching their intentional protocol exits."""
    stderr = io.StringIO()
    try:
        with contextlib.redirect_stderr(stderr):
            if command == "submit":
                action = state.get("action") or {}
                runtime.validate_assessment(payload, state["contract"], expected_action_id=action.get("id"))
            elif command == "resume":
                pause = state.get("pause") or {}
                if set(payload) != {"provenance"}:
                    raise ValueError("resume input must contain only provenance")
                runtime.validate_provenance(payload["provenance"], "provenance", (pause.get("resume_on"),))
            elif command == "resolve-verifier":
                recovery = state.get("recovery") or {}
                if set(payload) != {"action_id", "provenance", "resolution"}:
                    raise ValueError("resolution input has unsupported fields")
                if payload.get("action_id") != recovery.get("action_id"):
                    raise ValueError("resolution action_id is not the recovery action")
                if payload.get("resolution") != "abandon":
                    raise ValueError("resolution must abandon the uncertain attempt")
                runtime.validate_provenance(payload["provenance"], "resolution.provenance", ("user_instruction", "host_observation"))
            else:
                return {"status": "not-applicable"}
    except (SystemExit, ValueError, KeyError, TypeError) as error:
        message = stderr.getvalue().strip() or str(error)
        return {"status": "invalid", "reason": message}
    return {"status": "accepted"}


def check_packet(case: Mapping[str, Any], final: Mapping[str, Any], snapshot: Path, runtime: Any) -> dict[str, Any]:
    workspace = Path(str(case["workspace"]))
    state = read_state(workspace)
    command = command_shape(final.get("runtime_command_argv"), snapshot, state)
    command_name = command.get("command") if command.get("status") == "accepted" else None
    input_record = decode_input(final.get("runtime_input_json"), command_name)
    payload_check: dict[str, Any]
    if input_record.get("status") == "decoded" and command_name is not None:
        payload_check = pure_validate(runtime, command_name, input_record["value"], state)
    elif input_record.get("status") == "decoded":
        payload_check = {"status": "incomplete", "reason": "input exists but no accepted command selects its schema"}
    else:
        payload_check = {"status": input_record["status"], "reason": input_record.get("reason")}
    return {
        "mode": "packet",
        "workspace": str(workspace),
        "phase": state.get("phase"),
        "command": command,
        "input": {key: value for key, value in input_record.items() if key != "value"},
        "payload_validation": payload_check,
        "note": "This checks argv and JSON protocol shape only; it does not execute the proposed command or judge evidence.",
    }


def observe_contract(case: Mapping[str, Any], final: Mapping[str, Any], snapshot: Path) -> dict[str, Any]:
    raw = final.get("contract_json")
    request = case.get("request")
    result: dict[str, Any] = {
        "mode": "raw_nl",
        "original_request_matches_host_request": False,
    }
    if not isinstance(raw, str):
        result["contract_parse"] = {"status": "incomplete", "reason": "contract_json is absent"}
        return result
    try:
        contract = json.loads(raw)
    except json.JSONDecodeError as error:
        result["contract_parse"] = {"status": "invalid", "reason": str(error)}
        return result
    if not isinstance(contract, dict):
        result["contract_parse"] = {"status": "invalid", "reason": "contract_json is not an object"}
        return result
    result["contract_parse"] = {"status": "decoded"}
    result["original_request_matches_host_request"] = contract.get("original_request") == request
    with tempfile.TemporaryDirectory(prefix="until-loop-fresh-observer-") as temporary:
        observer = Path(temporary)
        contract_file = observer / "contract.json"
        write_json(contract_file, contract)
        argv = [
            str(Path(sys.executable).resolve()), str(snapshot / "scripts" / "until-loop"), "v2", "init",
            "--repo", str(observer), "--contract-file", str(contract_file),
        ]
        completed = subprocess.run(
            argv,
            cwd=observer,
            env=fresh.cli_environment(),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=30,
        )
        phase = None
        state_path = observer / ".until-loop" / "state.json"
        if state_path.is_file():
            try:
                phase = json.loads(state_path.read_text(encoding="utf-8")).get("phase")
            except (OSError, json.JSONDecodeError):
                phase = "unreadable"
        result["frozen_cli_init"] = {
            "argv": argv,
            "returncode": completed.returncode,
            "phase": phase,
            "stdout": completed.stdout.decode("utf-8", errors="replace"),
            "stderr": completed.stderr.decode("utf-8", errors="replace"),
            "observer_workspace": "temporary and removed after observation",
        }
    return result


def check_case(case: Mapping[str, Any], run_root: Path, snapshot: Path, runtime: Any) -> dict[str, Any]:
    case_id = str(case["id"])
    final, error = read_json_text(run_root / case_id / "final.json")
    base: dict[str, Any] = {"case_id": case_id, "mode": case.get("mode")}
    if error or not isinstance(final, dict):
        base["status"] = "incomplete"
        base["final"] = {"status": "invalid", "reason": error or "final output is not an object"}
        return base
    base["final"] = {"status": "decoded", "keys": sorted(final)}
    if case.get("mode") == "raw_nl":
        base["observation"] = observe_contract(case, final, snapshot)
    else:
        base["observation"] = check_packet(case, final, snapshot, runtime)
    return base


def check(review_root: Path, label: str, run_id: str, output_name: str) -> Path:
    label = fresh.safe_component(label, "label")
    run_id = fresh.safe_component(run_id, "run-id")
    output_name = fresh.safe_component(output_name, "output-name")
    manifest, snapshot = fresh.load_prepared(review_root, label)
    run_root = review_root / "runs" / label / run_id
    if not run_root.is_dir():
        raise RuntimeError(f"run root is missing: {run_root}")
    summary_path = run_root / output_name
    case_root = run_root / f"{Path(output_name).stem}-cases"
    if summary_path.exists() or case_root.exists():
        raise RuntimeError("refusing to overwrite retained output validation; choose a fresh --output-name")
    runtime = load_runtime(snapshot)
    cases = [case for case in manifest.get("cases", []) if isinstance(case, dict) and (run_root / str(case.get("id")) / "run.json").is_file()]
    if not cases:
        raise RuntimeError("run root has no retained host results matching the prepared manifest")
    records = [check_case(case, run_root, snapshot, runtime) for case in sorted(cases, key=lambda item: str(item["id"]))]
    for record in records:
        write_json(case_root / f"{record['case_id']}.json", record)
    summary = {
        "format": "until-loop-v2-fresh-output-check/v1",
        "label": label,
        "run_id": run_id,
        "source_snapshot": str(snapshot),
        "host_count": len(records),
        "cases": records,
        "note": "Structural observations only. No model-proposed callback, verifier, or semantic-evidence judgment was executed.",
    }
    write_json(summary_path, summary)
    return summary_path


def self_test() -> None:
    state = {"repo_root": "/tmp/bound", "phase": "active", "action": {"id": "a" * 32}, "recovery": None}
    snapshot = HERE.parents[1]
    direct = [str(Path(sys.executable).resolve()), str(snapshot / "scripts" / "until_loop_v2.py"), "next", "--repo", "/tmp/bound"]
    assert command_shape(direct, snapshot, state)["status"] == "accepted"
    wrapped = [str(Path(sys.executable).resolve()), str(snapshot / "scripts" / "until-loop"), "v2", "submit", "--repo", "/tmp/bound", "--action-id", "a" * 32]
    assert command_shape(wrapped, snapshot, state)["status"] == "accepted"
    assert command_shape(["/bin/sh", "-c", "echo bad"], snapshot, state)["status"] == "invalid"
    assert decode_input(None, "submit")["status"] == "incomplete"
    runtime = load_runtime(snapshot)
    contract = {"revision": 1, "criteria": [{"id": "C1"}]}
    assessment = {
        "action_id": "a" * 32,
        "contract_revision": 1,
        "decision": "continue",
        "criteria": [{"id": "C1", "status": "unknown", "evidence": "not yet observed"}],
        "next_action": "Inspect the current artifact.",
        "blocker": None,
    }
    active = {**state, "contract": contract}
    assert pure_validate(runtime, "submit", assessment, active)["status"] == "accepted"
    paused = {"pause": {"resume_on": "condition_observed"}}
    assert pure_validate(runtime, "resume", {"provenance": {"kind": "condition_observed", "reference": "observed"}}, paused)["status"] == "accepted"
    recovery = {"recovery": {"action_id": "a" * 32}}
    resolution = {"action_id": "a" * 32, "resolution": "abandon", "provenance": {"kind": "host_observation", "reference": "observed"}}
    assert pure_validate(runtime, "resolve-verifier", resolution, recovery)["status"] == "accepted"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--review-root", type=Path, default=DEFAULT_REVIEW_ROOT)
    parser.add_argument("--label")
    parser.add_argument("--run-id")
    parser.add_argument("--output-name", default="fresh-output-check.json")
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.self_test:
            if args.label or args.run_id:
                parser.error("--self-test does not accept --label or --run-id")
            self_test()
            return 0
        if not args.label or not args.run_id:
            parser.error("--label and --run-id are required")
        print(check(args.review_root, args.label, args.run_id, args.output_name))
    except (OSError, RuntimeError, ValueError, subprocess.TimeoutExpired) as error:
        parser.error(str(error))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
