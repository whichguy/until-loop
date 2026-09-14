#!/usr/bin/env python3
"""Opt-in structured protocol v2 for the until-loop candidate.

The v1 runtime deliberately remains in ``scripts/until-loop``.  This module
uses the same run directory only after an explicit ``v2`` dispatch and rejects
every non-v2 state before it can run a verifier.
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import importlib.util
import json
import os
import re
import stat
import sys
import tempfile
from importlib.machinery import SourceFileLoader
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, TextIO, Tuple


STATE_VERSION = 2
CONTRACT_VERSION = 1
RUN_DIRNAME = ".until-loop"
USAGE_EXIT = 64
BLOCKED_EXIT = 2
MAX_RECORD_BYTES = 64 * 1024
MAX_STATE_BYTES = 256 * 1024
MAX_CRITERIA = 128
MAX_TEXT_BYTES = 4 * 1024
MAX_HISTORY_BYTES = 16 * 1024 * 1024
# Rendered control-path values have a 1024-byte literal-data budget.  The
# bound repository is lower so every state/history/result derivative remains
# complete inside that budget without relying on renderer truncation.
MAX_RENDER_PATH_BYTES = 1024
MAX_REPO_ROOT_BYTES = 768
ACTION_RE = re.compile(r"[0-9a-f]{32}")
SHA256_RE = re.compile(r"[0-9a-f]{64}")
CRITERION_ID_RE = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,63}")
PENDING_NAME = ".pending-v2.json"
RESULTS_DIRNAME = "results"

_legacy_runtime: Optional[Any] = None
_packet_runtime: Optional[Any] = None


class RuntimeExit(SystemExit):
    """A normal protocol exit that retains its precise rejection reason."""

    def __init__(self, code: int, message: str) -> None:
        super().__init__(code)
        self.message = message


class AssessmentRejected(Exception):
    """A safe, current-action record was structurally or semantically rejected."""


class UsageParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        print(f"error: {message}", file=sys.stderr)
        raise SystemExit(USAGE_EXIT)


def die(code: int, message: str) -> None:
    print(f"error: {message}", file=sys.stderr)
    raise RuntimeExit(code, message)


def legacy_runtime() -> Any:
    """Load v1 helpers without importing the v2 dispatcher recursively."""
    global _legacy_runtime
    if _legacy_runtime is not None:
        return _legacy_runtime
    source = Path(__file__).with_name("until-loop")
    name = "_until_loop_v1_helpers_for_v2"
    loader = SourceFileLoader(name, str(source))
    spec = importlib.util.spec_from_loader(name, loader)
    if spec is None:
        die(BLOCKED_EXIT, "cannot load v1 runtime helpers")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    loader.exec_module(module)
    _legacy_runtime = module
    return module


def packet_runtime() -> Any:
    """Load the colocated renderer even when tests SourceFileLoad this module."""
    global _packet_runtime
    if _packet_runtime is not None:
        return _packet_runtime
    source = Path(__file__).with_name("until_loop_packet.py")
    name = "_until_loop_v2_packet_for_protocol"
    loader = SourceFileLoader(name, str(source))
    spec = importlib.util.spec_from_loader(name, loader)
    if spec is None:
        die(BLOCKED_EXIT, "cannot load v2 packet helpers")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    loader.exec_module(module)
    _packet_runtime = module
    return module


def canonical_json(value: Any) -> bytes:
    try:
        return json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as exc:
        die(BLOCKED_EXIT, f"record is not JSON-safe: {exc}")
    raise AssertionError("unreachable")


def pretty_json(value: Any) -> str:
    try:
        return json.dumps(value, sort_keys=True, indent=2, ensure_ascii=False,
                          allow_nan=False) + "\n"
    except (TypeError, ValueError, UnicodeError) as exc:
        die(BLOCKED_EXIT, f"record is not JSON-safe: {exc}")
    raise AssertionError("unreachable")


def digest(value: Any) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()


def require_size(raw: bytes, limit: int, label: str) -> None:
    if len(raw) > limit:
        die(BLOCKED_EXIT, f"{label} exceeds {limit} bytes")


def require_text(value: Any, label: str, *, nonempty: bool = True) -> str:
    if not isinstance(value, str):
        die(BLOCKED_EXIT, f"{label} must be a string")
    if nonempty and not value.strip():
        die(BLOCKED_EXIT, f"{label} must be nonempty")
    if len(value.encode("utf-8", errors="replace")) > MAX_TEXT_BYTES:
        die(BLOCKED_EXIT, f"{label} exceeds {MAX_TEXT_BYTES} bytes")
    return value


def require_int(value: Any, label: str) -> int:
    if type(value) is not int:
        die(BLOCKED_EXIT, f"{label} must be an integer")
    return value


def require_action_id(value: Any, label: str = "action_id") -> str:
    if not isinstance(value, str) or not ACTION_RE.fullmatch(value):
        die(BLOCKED_EXIT, f"{label} must be 32 lowercase hexadecimal characters")
    return value


def require_sha256(value: Any, label: str) -> str:
    if not isinstance(value, str) or not SHA256_RE.fullmatch(value):
        die(BLOCKED_EXIT, f"{label} must be a lowercase sha256")
    return value


def run_dir_for(repo: Path) -> Path:
    return repo / RUN_DIRNAME


def result_path_for(run_dir: Path, action_id: str) -> Path:
    require_action_id(action_id)
    return run_dir / RESULTS_DIRNAME / f"{action_id}.json"


def has_symlink_parent(path: Path) -> bool:
    current = Path(path.anchor)
    for part in path.parts[1:-1]:
        current /= part
        try:
            if current.is_symlink():
                return True
        except OSError:
            return True
    return False


def require_runtime_owner(item: os.stat_result, label: str, path: Path) -> None:
    if hasattr(os, "geteuid") and item.st_uid != os.geteuid():
        die(BLOCKED_EXIT, f"unsafe {label} owner: {path}")


def check_regular(path: Path, label: str, *, missing_ok: bool = False,
                  require_owner: bool = False) -> Optional[os.stat_result]:
    if has_symlink_parent(path):
        die(BLOCKED_EXIT, f"unsafe {label} parent: {path}")
    try:
        item = path.lstat()
    except FileNotFoundError:
        if missing_ok:
            return None
        die(BLOCKED_EXIT, f"missing {label}: {path}")
    except OSError as exc:
        die(BLOCKED_EXIT, f"unsafe {label}: {path}: {exc}")
    if not stat.S_ISREG(item.st_mode) or item.st_nlink != 1:
        die(BLOCKED_EXIT, f"unsafe {label}: {path}")
    if require_owner:
        require_runtime_owner(item, label, path)
    return item


def read_regular(path: Path, label: str, *, limit: int, missing_ok: bool = False,
                 require_owner: bool = False,
                 expected_identity: Optional[Tuple[int, int]] = None) -> Optional[bytes]:
    before = check_regular(path, label, missing_ok=missing_ok, require_owner=require_owner)
    if before is None:
        return None
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        die(BLOCKED_EXIT, f"cannot read {label}: {path}: {exc}")
    try:
        after = os.fstat(fd)
        if (not stat.S_ISREG(after.st_mode) or after.st_nlink != 1 or
                after.st_dev != before.st_dev or after.st_ino != before.st_ino):
            die(BLOCKED_EXIT, f"unsafe {label}: {path}")
        if require_owner:
            require_runtime_owner(after, label, path)
        if expected_identity is not None and (after.st_dev, after.st_ino) != expected_identity:
            die(BLOCKED_EXIT, f"{label} changed while resolving its external path")
        chunks: List[bytes] = []
        remaining = limit + 1
        while remaining:
            chunk = os.read(fd, min(65536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        require_size(raw, limit, label)
        return raw
    finally:
        os.close(fd)


def read_json_file(path: Path, label: str, *, limit: int,
                   require_owner: bool = False,
                   expected_identity: Optional[Tuple[int, int]] = None) -> Any:
    raw = read_regular(path, label, limit=limit, require_owner=require_owner,
                       expected_identity=expected_identity)
    assert raw is not None
    return parse_json_bytes(raw, label)


def parse_json_bytes(raw: bytes, label: str) -> Any:
    """Decode a bounded JSON record supplied by a checked input channel."""
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeError, ValueError) as exc:
        die(BLOCKED_EXIT, f"invalid {label}: {exc}")
    raise AssertionError("unreachable")


def read_stdin_record(label: str, *, limit: int) -> bytes:
    """Read one bounded UTF-8 record from standard input without a temp file."""
    stream = getattr(sys.stdin, "buffer", None)
    if stream is None:
        die(BLOCKED_EXIT, f"cannot read {label} from stdin: binary stream unavailable")
    try:
        raw = stream.read(limit + 1)
    except (OSError, ValueError) as exc:
        die(BLOCKED_EXIT, f"cannot read {label} from stdin: {exc}")
    if not isinstance(raw, bytes):
        die(BLOCKED_EXIT, f"cannot read {label} from stdin: expected bytes")
    require_size(raw, limit, label)
    return raw


def external_input_path(value: str, label: str) -> Tuple[Path, Tuple[int, int]]:
    """Canonicalize an existing adapter input without accepting a link leaf.

    macOS commonly exposes temporary files through `/var`, itself a system
    symlink to `/private/var`.  The runtime-owned inbox never relies on that
    relaxation, but external contract/provenance records must remain usable
    through normal host temporary-file APIs.
    """
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    try:
        leaf = path.lstat()
    except FileNotFoundError:
        die(BLOCKED_EXIT, f"missing {label}: {path}")
    except OSError as exc:
        die(BLOCKED_EXIT, f"unsafe {label}: {path}: {exc}")
    if stat.S_ISLNK(leaf.st_mode) or not stat.S_ISREG(leaf.st_mode) or leaf.st_nlink != 1:
        die(BLOCKED_EXIT, f"unsafe {label}: {path}")
    try:
        canonical = path.resolve(strict=True)
    except OSError as exc:
        die(BLOCKED_EXIT, f"cannot resolve {label}: {path}: {exc}")
    return canonical, (leaf.st_dev, leaf.st_ino)


def ensure_results_dir(run_dir: Path) -> Path:
    path = run_dir / RESULTS_DIRNAME
    if has_symlink_parent(path):
        die(BLOCKED_EXIT, f"unsafe results directory parent: {path}")
    try:
        path.mkdir(mode=0o700, exist_ok=True)
        item = path.lstat()
    except OSError as exc:
        die(BLOCKED_EXIT, f"cannot create results directory: {path}: {exc}")
    if path.is_symlink() or not stat.S_ISDIR(item.st_mode):
        die(BLOCKED_EXIT, f"unsafe results directory: {path}")
    require_runtime_owner(item, "results directory", path)
    return path


def check_metadata(run_dir: Path) -> None:
    if run_dir.is_symlink() or (run_dir.exists() and not run_dir.is_dir()):
        die(BLOCKED_EXIT, f"unsafe run directory: {run_dir}")
    if run_dir.exists():
        try:
            require_runtime_owner(run_dir.lstat(), "run directory", run_dir)
        except OSError as exc:
            die(BLOCKED_EXIT, f"unsafe run directory: {run_dir}: {exc}")
    for name in (".lock", "state.json", "history.jsonl", PENDING_NAME):
        check_regular(run_dir / name, "metadata path", missing_ok=True, require_owner=True)
    # V2 must never interpret or overwrite an interrupted v1 initialization.
    # Check these legacy-owned paths before locking, v2 recovery, or directory
    # creation. A valid v1 `.pending.json` remains recoverable only by v1.
    for name in (".pending.json", "prompt.md"):
        path = run_dir / name
        try:
            marker = path.lstat()
        except FileNotFoundError:
            continue
        except OSError as exc:
            die(BLOCKED_EXIT, f"unsafe foreign v1 marker: {path}: {exc}")
        if not stat.S_ISREG(marker.st_mode) or marker.st_nlink != 1:
            die(BLOCKED_EXIT, f"unsafe foreign v1 marker: {path}")
        die(BLOCKED_EXIT, f"foreign v1 marker at {path}; recover it with the v1 adapter")
    results = run_dir / RESULTS_DIRNAME
    if results.exists() or results.is_symlink():
        if has_symlink_parent(results):
            die(BLOCKED_EXIT, f"unsafe results directory parent: {results}")
        try:
            item = results.lstat()
        except OSError as exc:
            die(BLOCKED_EXIT, f"unsafe results directory: {results}: {exc}")
        if results.is_symlink() or not stat.S_ISDIR(item.st_mode):
            die(BLOCKED_EXIT, f"unsafe results directory: {results}")
        require_runtime_owner(item, "results directory", results)


def atomic_write(path: Path, text: str) -> None:
    # The v1 helper supplies the existing fsync/replace transaction primitive.
    legacy_runtime().atomic_write(path, text)


def save_state(run_dir: Path, state: Dict[str, Any]) -> None:
    validate_state(state, run_dir)
    payload = pretty_json(state)
    require_size(payload.encode("utf-8"), MAX_STATE_BYTES, "state")
    atomic_write(run_dir / "state.json", payload)


def load_state(run_dir: Path) -> Optional[Dict[str, Any]]:
    raw = read_regular(run_dir / "state.json", "state", limit=MAX_STATE_BYTES,
                       missing_ok=True, require_owner=True)
    if raw is None:
        return None
    try:
        state = json.loads(raw.decode("utf-8"))
    except (UnicodeError, ValueError) as exc:
        die(BLOCKED_EXIT, f"invalid state at {run_dir}: {exc}")
    return validate_state(state, run_dir)


def validate_basis(value: Any, label: str) -> Dict[str, str]:
    if not isinstance(value, dict) or set(value) != {"kind", "reference"}:
        die(BLOCKED_EXIT, f"{label} must contain kind and reference")
    kind = value["kind"]
    if kind not in ("request", "assumption"):
        die(BLOCKED_EXIT, f"{label}.kind must be request or assumption")
    reference = require_text(value["reference"], f"{label}.reference")
    return {"kind": kind, "reference": reference}


def validate_criteria(value: Any, label: str) -> List[Dict[str, Any]]:
    if not isinstance(value, list) or not 1 <= len(value) <= MAX_CRITERIA:
        die(BLOCKED_EXIT, f"{label} must contain 1..{MAX_CRITERIA} criteria")
    seen = set()
    result: List[Dict[str, Any]] = []
    for index, criterion in enumerate(value):
        prefix = f"{label}[{index}]"
        if not isinstance(criterion, dict) or set(criterion) != {"id", "text", "basis"}:
            die(BLOCKED_EXIT, f"{prefix} has unsupported fields")
        ident = criterion["id"]
        if not isinstance(ident, str) or not CRITERION_ID_RE.fullmatch(ident):
            die(BLOCKED_EXIT, f"{prefix}.id is invalid")
        if ident in seen:
            die(BLOCKED_EXIT, f"{label} contains duplicate criterion id: {ident}")
        seen.add(ident)
        result.append({
            "id": ident,
            "text": require_text(criterion["text"], f"{prefix}.text"),
            "basis": validate_basis(criterion["basis"], f"{prefix}.basis"),
        })
    return result


def validate_contract(value: Any, *, include_revision: bool,
                      policy_version: Optional[str] = None) -> Dict[str, Any]:
    expected = {"version", "policy", "original_request", "interpretation", "criteria"}
    if include_revision:
        expected.add("revision")
    if not isinstance(value, dict) or set(value) != expected:
        die(BLOCKED_EXIT, "contract has unsupported fields")
    if require_int(value["version"], "contract.version") != CONTRACT_VERSION:
        die(BLOCKED_EXIT, "unsupported contract.version")
    policy = require_text(value["policy"], "contract.policy")
    if policy_version is not None and policy != policy_version:
        die(BLOCKED_EXIT, "contract.policy does not match the loaded policy")
    result: Dict[str, Any] = {
        "version": CONTRACT_VERSION,
        "policy": policy,
        "original_request": require_text(value["original_request"], "contract.original_request"),
        "interpretation": require_text(value["interpretation"], "contract.interpretation"),
        "criteria": validate_criteria(value["criteria"], "contract.criteria"),
    }
    if include_revision:
        revision = require_int(value["revision"], "contract.revision")
        if revision < 1:
            die(BLOCKED_EXIT, "contract.revision must be >= 1")
        result["revision"] = revision
    return result


def validate_policy_snapshot(value: Any) -> Dict[str, Any]:
    try:
        snapshot = packet_runtime().validate_policy(value)
    except (ValueError, TypeError) as exc:
        die(BLOCKED_EXIT, f"invalid policy_snapshot: {exc}")
    require_size(canonical_json(snapshot), MAX_RECORD_BYTES, "policy snapshot")
    return snapshot


def validate_verify(value: Any) -> Optional[Dict[str, Any]]:
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != {"ok", "exit", "tail"}:
        die(BLOCKED_EXIT, "last_verify has unsupported fields")
    if type(value["ok"]) is not bool or type(value["exit"]) is not int:
        die(BLOCKED_EXIT, "last_verify has invalid types")
    tail = value["tail"]
    if not isinstance(tail, str):
        die(BLOCKED_EXIT, "last_verify.tail must be a string")
    if len(tail.encode("utf-8", errors="replace")) > 61_000:
        die(BLOCKED_EXIT, "last_verify.tail exceeds byte limit")
    if value["ok"] != (value["exit"] == 0):
        die(BLOCKED_EXIT, "last_verify.ok disagrees with exit")
    return value


def validate_provenance(value: Any, label: str, allowed: Tuple[str, ...]) -> Dict[str, str]:
    if not isinstance(value, dict) or set(value) != {"kind", "reference"}:
        die(BLOCKED_EXIT, f"{label} must contain kind and reference")
    kind = value["kind"]
    if kind not in allowed:
        die(BLOCKED_EXIT, f"{label}.kind is not permitted")
    return {"kind": kind, "reference": require_text(value["reference"], f"{label}.reference")}


def validate_assessment(value: Any, contract: Dict[str, Any], *,
                        expected_action_id: Optional[str] = None) -> Dict[str, Any]:
    fields = {"action_id", "contract_revision", "decision", "criteria", "next_action", "blocker"}
    if not isinstance(value, dict) or set(value) != fields:
        die(BLOCKED_EXIT, "assessment has unsupported fields")
    action_id = require_action_id(value["action_id"], "assessment.action_id")
    if expected_action_id is not None and action_id != expected_action_id:
        die(BLOCKED_EXIT, "assessment.action_id does not match the current action")
    if require_int(value["contract_revision"], "assessment.contract_revision") != contract["revision"]:
        die(BLOCKED_EXIT, "assessment.contract_revision is stale")
    decision = value["decision"]
    if decision not in ("continue", "complete", "blocked"):
        die(BLOCKED_EXIT, "assessment.decision is invalid")
    if not isinstance(value["criteria"], list):
        die(BLOCKED_EXIT, "assessment.criteria must be an array")
    expected_ids = [criterion["id"] for criterion in contract["criteria"]]
    actual_ids: List[str] = []
    normalized_criteria: List[Dict[str, str]] = []
    for index, item in enumerate(value["criteria"]):
        label = f"assessment.criteria[{index}]"
        if not isinstance(item, dict) or set(item) != {"id", "status", "evidence"}:
            die(BLOCKED_EXIT, f"{label} has unsupported fields")
        ident = item["id"]
        if not isinstance(ident, str):
            die(BLOCKED_EXIT, f"{label}.id must be a string")
        status = item["status"]
        if status not in ("satisfied", "unsatisfied", "unknown"):
            die(BLOCKED_EXIT, f"{label}.status is invalid")
        actual_ids.append(ident)
        normalized_criteria.append({
            "id": ident,
            "status": status,
            "evidence": require_text(item["evidence"], f"{label}.evidence"),
        })
    if len(actual_ids) != len(expected_ids) or set(actual_ids) != set(expected_ids):
        die(BLOCKED_EXIT, "assessment must cover every recorded criterion exactly once")
    if len(set(actual_ids)) != len(actual_ids):
        die(BLOCKED_EXIT, "assessment contains duplicate criterion ids")
    next_action = value["next_action"]
    blocker = value["blocker"]
    if decision == "continue":
        next_text = require_text(next_action, "assessment.next_action")
        if blocker is not None:
            die(BLOCKED_EXIT, "continue assessment must not contain a blocker")
        normalized_blocker = None
    elif decision == "complete":
        if next_action is not None or blocker is not None:
            die(BLOCKED_EXIT, "complete assessment must not contain next_action or blocker")
        if any(item["status"] != "satisfied" for item in normalized_criteria):
            die(BLOCKED_EXIT, "complete assessment requires every criterion to be satisfied")
        next_text = None
        normalized_blocker = None
    else:
        if next_action is not None:
            die(BLOCKED_EXIT, "blocked assessment must not contain next_action")
        if (not isinstance(blocker, dict) or
                set(blocker) != {"reason", "resumption_condition", "resume_on"}):
            die(BLOCKED_EXIT, "blocked assessment requires reason, resumption_condition, and resume_on")
        if blocker["resume_on"] not in ("user_instruction", "condition_observed"):
            die(BLOCKED_EXIT, "assessment.blocker.resume_on is invalid")
        normalized_blocker = {
            "reason": require_text(blocker["reason"], "assessment.blocker.reason"),
            "resumption_condition": require_text(
                blocker["resumption_condition"], "assessment.blocker.resumption_condition"
            ),
            "resume_on": blocker["resume_on"],
        }
        next_text = None
    normalized = {
        "action_id": action_id,
        "contract_revision": contract["revision"],
        "decision": decision,
        "criteria": normalized_criteria,
        "next_action": next_text,
        "blocker": normalized_blocker,
    }
    require_size(canonical_json(normalized), MAX_RECORD_BYTES, "assessment")
    return normalized


def validate_action(value: Any, run_dir: Path, revision: int) -> Dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {"id", "result_path", "contract_revision"}:
        die(BLOCKED_EXIT, "action has unsupported fields")
    action_id = require_action_id(value["id"], "action.id")
    if require_int(value["contract_revision"], "action.contract_revision") != revision:
        die(BLOCKED_EXIT, "action.contract_revision is stale")
    expected = str(result_path_for(run_dir, action_id))
    if len(expected.encode("utf-8", errors="replace")) > MAX_RENDER_PATH_BYTES:
        die(BLOCKED_EXIT, "action.result_path exceeds packet-safe path limit")
    if value["result_path"] != expected:
        die(BLOCKED_EXIT, "action.result_path is not runtime-owned")
    return value


def validate_recovery(value: Any, action: Dict[str, Any], cycle: int) -> Dict[str, Any]:
    expected = {"kind", "action_id", "digest", "cycle", "message"}
    if not isinstance(value, dict) or set(value) != expected:
        die(BLOCKED_EXIT, "recovery has unsupported fields")
    if value["kind"] != "verifier_uncertain":
        die(BLOCKED_EXIT, "recovery.kind is invalid")
    if require_action_id(value["action_id"], "recovery.action_id") != action["id"]:
        die(BLOCKED_EXIT, "recovery.action_id does not match action")
    require_sha256(value["digest"], "recovery.digest")
    if require_int(value["cycle"], "recovery.cycle") != cycle + 1:
        die(BLOCKED_EXIT, "recovery.cycle is invalid")
    require_text(value["message"], "recovery.message")
    return value


def validate_pause(value: Any) -> Dict[str, Any]:
    expected = {"reason", "resumption_condition", "resume_on", "provenance"}
    if not isinstance(value, dict) or set(value) != expected:
        die(BLOCKED_EXIT, "pause has unsupported fields")
    if value["resume_on"] not in ("user_instruction", "condition_observed"):
        die(BLOCKED_EXIT, "pause.resume_on is invalid")
    return {
        "reason": require_text(value["reason"], "pause.reason"),
        "resumption_condition": require_text(value["resumption_condition"], "pause.resumption_condition"),
        "resume_on": value["resume_on"],
        "provenance": validate_provenance(value["provenance"], "pause.provenance", ("host_assessment",)),
    }


def validate_state(state: Any, run_dir: Path) -> Dict[str, Any]:
    fields = {
        "version", "phase", "repo_root", "max_cycles", "cycle", "contract",
        "policy_snapshot", "verify_cmd", "last_verify", "recovery", "action",
        "last_assessment", "pause",
    }
    if not isinstance(state, dict) or set(state) != fields:
        die(BLOCKED_EXIT, f"invalid v2 state at {run_dir}: unsupported fields")
    if require_int(state["version"], "state.version") != STATE_VERSION:
        die(BLOCKED_EXIT, f"invalid v2 state at {run_dir}: unsupported version")
    phase = state["phase"]
    if phase not in ("active", "paused", "done", "halted"):
        die(BLOCKED_EXIT, "invalid v2 state: unsupported phase")
    repo_root = state["repo_root"]
    if not isinstance(repo_root, str) or not repo_root:
        die(BLOCKED_EXIT, "invalid v2 state: repo_root")
    if len(repo_root.encode("utf-8", errors="replace")) > MAX_REPO_ROOT_BYTES:
        die(BLOCKED_EXIT, "invalid v2 state: repo_root exceeds packet-safe path limit")
    root = Path(repo_root)
    if not root.is_absolute() or not root.is_dir() or root.resolve() != run_dir.parent.resolve():
        die(BLOCKED_EXIT, "invalid v2 state: repo_root does not match --repo")
    for control_path in (run_dir / "state.json", run_dir / "history.jsonl"):
        if len(str(control_path).encode("utf-8", errors="replace")) > MAX_RENDER_PATH_BYTES:
            die(BLOCKED_EXIT, "invalid v2 state: control path exceeds packet-safe path limit")
    maximum = require_int(state["max_cycles"], "state.max_cycles")
    cycle = require_int(state["cycle"], "state.cycle")
    if maximum < 1 or not 0 <= cycle <= maximum:
        die(BLOCKED_EXIT, "invalid v2 state: cycle outside bounds")
    if phase in ("active", "paused") and cycle >= maximum:
        die(BLOCKED_EXIT, "invalid v2 state: active or paused cycle cap")
    if phase == "halted" and cycle != maximum:
        die(BLOCKED_EXIT, "invalid v2 state: halted cycle")
    if phase == "done" and cycle == 0:
        die(BLOCKED_EXIT, "invalid v2 state: done without assessment")
    policy = validate_policy_snapshot(state["policy_snapshot"])
    contract = validate_contract(state["contract"], include_revision=True,
                                 policy_version=policy["version"])
    verify_cmd = state["verify_cmd"]
    if verify_cmd is not None:
        require_text(verify_cmd, "state.verify_cmd")
    last_verify = validate_verify(state["last_verify"])
    if verify_cmd is None and last_verify is not None:
        die(BLOCKED_EXIT, "invalid v2 state: verifier result without verifier")
    if cycle == 0 and last_verify is not None:
        die(BLOCKED_EXIT, "invalid v2 state: verifier result at cycle zero")
    action = state["action"]
    if phase == "active":
        if action is None:
            die(BLOCKED_EXIT, "invalid v2 state: active action missing")
        action = validate_action(action, run_dir, contract["revision"])
    elif action is not None:
        die(BLOCKED_EXIT, "invalid v2 state: terminal or paused action present")
    recovery = state["recovery"]
    if recovery is not None:
        if phase != "active" or action is None:
            die(BLOCKED_EXIT, "invalid v2 state: recovery without active action")
        validate_recovery(recovery, action, cycle)
    pause = state["pause"]
    if phase == "paused":
        if pause is None:
            die(BLOCKED_EXIT, "invalid v2 state: paused reason missing")
        validate_pause(pause)
    elif pause is not None:
        die(BLOCKED_EXIT, "invalid v2 state: pause outside paused phase")
    assessment = state["last_assessment"]
    if assessment is not None:
        assessment = validate_assessment(assessment, contract)
    if phase == "paused" and (assessment is None or assessment["decision"] != "blocked"):
        die(BLOCKED_EXIT, "invalid v2 state: paused requires blocked assessment")
    if phase == "done":
        if assessment is None or assessment["decision"] != "complete":
            die(BLOCKED_EXIT, "invalid v2 state: done requires complete assessment")
        if verify_cmd is not None and (last_verify is None or not last_verify["ok"]):
            die(BLOCKED_EXIT, "invalid v2 state: done requires passing verifier")
    require_size(canonical_json(state), MAX_STATE_BYTES, "state")
    return state


def action_for(run_dir: Path, revision: int) -> Dict[str, Any]:
    action_id = os.urandom(16).hex()
    return {
        "id": action_id,
        "result_path": str(result_path_for(run_dir, action_id)),
        "contract_revision": revision,
    }


def state_digest(state: Dict[str, Any]) -> str:
    return digest(state)


def validate_history_event(value: Any) -> Dict[str, Any]:
    if not isinstance(value, dict) or not isinstance(value.get("type"), str):
        die(BLOCKED_EXIT, "invalid history event")
    require_sha256(value.get("state_digest"), "history state_digest")
    event_type = value["type"]
    if event_type == "assessment":
        needed = {"type", "state_digest", "action_id", "digest", "decision", "assessment", "cycle", "phase", "verify_ok"}
        if set(value) != needed:
            die(BLOCKED_EXIT, "invalid assessment history event")
        require_action_id(value["action_id"], "history action_id")
        require_sha256(value["digest"], "history digest")
        if not isinstance(value["assessment"], dict) or value["digest"] != digest(value["assessment"]):
            die(BLOCKED_EXIT, "history assessment does not match its receipt digest")
        if value["decision"] not in ("continue", "complete", "blocked"):
            die(BLOCKED_EXIT, "invalid history decision")
        if type(value["cycle"]) is not int or value["phase"] not in ("active", "paused", "done", "halted"):
            die(BLOCKED_EXIT, "invalid history phase or cycle")
        if value["verify_ok"] is not None and type(value["verify_ok"]) is not bool:
            die(BLOCKED_EXIT, "invalid history verifier result")
    elif event_type == "rejection":
        needed = {"type", "state_digest", "action_id", "contract_revision", "reason"}
        if set(value) != needed:
            die(BLOCKED_EXIT, "invalid rejection history event")
        require_action_id(value["action_id"], "history action_id")
        if require_int(value["contract_revision"], "history contract_revision") < 1:
            die(BLOCKED_EXIT, "invalid rejection history contract revision")
        require_text(value["reason"], "history rejection reason")
    elif event_type == "revision":
        needed = {
            "type", "state_digest", "from_revision", "to_revision", "provenance",
            "previous_contract", "contract", "contract_digest",
        }
        if set(value) != needed:
            die(BLOCKED_EXIT, "invalid revision history event")
        from_revision = require_int(value["from_revision"], "history from_revision")
        to_revision = require_int(value["to_revision"], "history to_revision")
        validate_provenance(value["provenance"], "history provenance", ("user_correction",))
        previous = validate_contract(value["previous_contract"], include_revision=True)
        contract = validate_contract(value["contract"], include_revision=True)
        if previous["revision"] != from_revision or contract["revision"] != to_revision:
            die(BLOCKED_EXIT, "invalid revision history contract revisions")
        require_sha256(value["contract_digest"], "history contract_digest")
        if value["contract_digest"] != digest(contract):
            die(BLOCKED_EXIT, "invalid revision history contract digest")
    elif event_type == "restart":
        needed = {
            "type", "state_digest", "previous_phase", "previous_cycle",
            "previous_contract", "previous_policy_snapshot",
        }
        if set(value) != needed:
            die(BLOCKED_EXIT, "invalid restart history event")
        if value["previous_phase"] not in ("active", "paused", "done", "halted"):
            die(BLOCKED_EXIT, "invalid restart history phase")
        if require_int(value["previous_cycle"], "history previous_cycle") < 0:
            die(BLOCKED_EXIT, "invalid restart history cycle")
        validate_contract(value["previous_contract"], include_revision=True)
        validate_policy_snapshot(value["previous_policy_snapshot"])
    elif event_type in ("resume", "recovery_resolution"):
        if "state_digest" not in value:
            die(BLOCKED_EXIT, "invalid history transition")
    else:
        die(BLOCKED_EXIT, "invalid history event type")
    return value


def history_entries(run_dir: Path) -> List[Dict[str, Any]]:
    raw = read_regular(run_dir / "history.jsonl", "history", limit=MAX_HISTORY_BYTES,
                       missing_ok=True, require_owner=True)
    if raw is None or not raw:
        return []
    try:
        lines = raw.decode("utf-8").splitlines()
    except UnicodeError as exc:
        die(BLOCKED_EXIT, f"invalid history: {exc}")
    entries: List[Dict[str, Any]] = []
    for line in lines:
        if not line:
            continue
        try:
            entries.append(validate_history_event(json.loads(line)))
        except ValueError as exc:
            die(BLOCKED_EXIT, f"invalid history: {exc}")
    return entries


def ensure_history_capacity(run_dir: Path, event: Dict[str, Any]) -> None:
    """Fail before an external verifier if its eventual receipt cannot fit."""
    planned = dict(event)
    planned.setdefault("state_digest", "0" * 64)
    # The fixed digest has the same encoded size as a final digest. Validation
    # here catches an impossible event shape before any verifier can run.
    validate_history_event(planned)
    existing = read_regular(run_dir / "history.jsonl", "history", limit=MAX_HISTORY_BYTES,
                            missing_ok=True, require_owner=True) or b""
    projected = len(existing) + len(canonical_json(planned)) + 1
    if projected > MAX_HISTORY_BYTES:
        die(BLOCKED_EXIT, "history capacity would be exceeded before this transition")


def commit_transition(run_dir: Path, state: Dict[str, Any], event: Dict[str, Any]) -> None:
    check_metadata(run_dir)
    validate_state(state, run_dir)
    event = dict(event)
    event["state_digest"] = state_digest(state)
    validate_history_event(event)
    ensure_history_capacity(run_dir, event)
    history = run_dir / "history.jsonl"
    existing = read_regular(history, "history", limit=MAX_HISTORY_BYTES, missing_ok=True,
                            require_owner=True)
    offset = len(existing or b"")
    pending = {
        "version": 1,
        "state": state,
        "event": event,
        "history_size": offset,
    }
    require_size(canonical_json(pending), MAX_STATE_BYTES + MAX_RECORD_BYTES, "pending transition")
    atomic_write(run_dir / PENDING_NAME, pretty_json(pending))
    recover_transition(run_dir)


def recover_transition(run_dir: Path) -> None:
    raw = read_regular(run_dir / PENDING_NAME, "pending transition",
                       limit=MAX_STATE_BYTES + MAX_RECORD_BYTES, missing_ok=True,
                       require_owner=True)
    if raw is None:
        return
    try:
        pending = json.loads(raw.decode("utf-8"))
    except (UnicodeError, ValueError) as exc:
        die(BLOCKED_EXIT, f"invalid pending transition: {exc}")
    if not isinstance(pending, dict) or set(pending) != {"version", "state", "event", "history_size"}:
        die(BLOCKED_EXIT, "invalid pending transition fields")
    if require_int(pending["version"], "pending.version") != 1:
        die(BLOCKED_EXIT, "unsupported pending transition version")
    state = validate_state(pending["state"], run_dir)
    event = validate_history_event(pending["event"])
    if event["state_digest"] != state_digest(state):
        die(BLOCKED_EXIT, "pending transition state digest disagrees")
    offset = require_int(pending["history_size"], "pending.history_size")
    if offset < 0:
        die(BLOCKED_EXIT, "pending.history_size is negative")
    expected = canonical_json(event) + b"\n"
    history = run_dir / "history.jsonl"
    existing = read_regular(history, "history", limit=MAX_HISTORY_BYTES, missing_ok=True,
                            require_owner=True) or b""
    if not offset <= len(existing) <= offset + len(expected):
        die(BLOCKED_EXIT, "history size disagrees with pending transition")
    suffix = existing[offset:]
    if not expected.startswith(suffix):
        die(BLOCKED_EXIT, "history differs from pending transition")
    save_state(run_dir, state)
    # State is durable before the journal line, so retry recovery can safely
    # complete either a partial or a fully appended line exactly once.
    fd = os.open(history, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        with os.fdopen(fd, "r+b") as fh:
            item = os.fstat(fh.fileno())
            if not stat.S_ISREG(item.st_mode) or item.st_nlink != 1:
                die(BLOCKED_EXIT, f"unsafe history: {history}")
            fh.seek(offset)
            fh.write(expected)
            fh.truncate()
            fh.flush()
            os.fsync(fh.fileno())
    finally:
        # fd is closed by fdopen on the normal path; only close on early error.
        try:
            os.close(fd)
        except OSError:
            pass
    Path(run_dir / PENDING_NAME).unlink()
    legacy_runtime().sync_directory(run_dir)


def with_lock(run_dir: Path, fn: Callable[[], int], *, create: bool = True) -> int:
    check_metadata(run_dir)
    if create:
        if not run_dir.parent.is_dir():
            die(BLOCKED_EXIT, f"repo not found: {run_dir.parent}")
        try:
            run_dir.mkdir(mode=0o700, exist_ok=True)
        except OSError as exc:
            die(BLOCKED_EXIT, f"cannot create run directory: {exc}")
    elif not run_dir.is_dir():
        return fn()
    check_metadata(run_dir)
    lock_path = run_dir / ".lock"
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(fd, "a+") as lock:
        item = os.fstat(lock.fileno())
        if not stat.S_ISREG(item.st_mode) or item.st_nlink != 1:
            die(BLOCKED_EXIT, f"unsafe lock: {lock_path}")
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        check_metadata(run_dir)
        recover_transition(run_dir)
        return fn()


def load_policy_snapshot() -> Dict[str, Any]:
    try:
        policy = packet_runtime().load_policy()
    except (ImportError, ValueError, OSError) as exc:
        die(BLOCKED_EXIT, f"v2 packet policy is unavailable: {exc}")
    return validate_policy_snapshot(policy)


def print_packet(state: Dict[str, Any], outcome: str, out: TextIO = sys.stdout,
                 *, rejection: Optional[str] = None) -> None:
    try:
        renderer = packet_runtime().print_packet
    except (ImportError, OSError) as exc:
        die(BLOCKED_EXIT, f"v2 packet renderer is unavailable: {exc}")
    renderer(state, outcome, out=out, rejection=rejection)


def new_state(repo: Path, contract: Dict[str, Any], policy_snapshot: Dict[str, Any],
              verify_cmd: Optional[str], max_cycles: int) -> Dict[str, Any]:
    run_dir = run_dir_for(repo)
    state: Dict[str, Any] = {
        "version": STATE_VERSION,
        "phase": "active",
        "repo_root": str(repo),
        "max_cycles": max_cycles,
        "cycle": 0,
        "contract": contract,
        "policy_snapshot": policy_snapshot,
        "verify_cmd": verify_cmd,
        "last_verify": None,
        "recovery": None,
        "action": action_for(run_dir, contract["revision"]),
        "last_assessment": None,
        "pause": None,
    }
    validate_state(state, run_dir)
    return state


def read_contract_input(path_value: str, policy_version: str, *,
                        allow_stdin: bool = False) -> Dict[str, Any]:
    if not isinstance(path_value, str) or not path_value.strip():
        die(USAGE_EXIT, "missing --contract-file")
    if allow_stdin and path_value == "-":
        payload = parse_json_bytes(
            read_stdin_record("contract file", limit=MAX_RECORD_BYTES),
            "contract file",
        )
    else:
        path, identity = external_input_path(path_value, "contract file")
        payload = read_json_file(path, "contract file", limit=MAX_RECORD_BYTES,
                                 expected_identity=identity)
    contract = validate_contract(payload, include_revision=False, policy_version=policy_version)
    contract["revision"] = 1
    require_size(canonical_json(contract), MAX_RECORD_BYTES, "contract")
    return contract


def find_receipt(run_dir: Path, action_id: str) -> Optional[Dict[str, Any]]:
    for event in reversed(history_entries(run_dir)):
        if event.get("type") == "assessment" and event.get("action_id") == action_id:
            return event
    return None


def read_assessment_for_action(run_dir: Path, action_id: str,
                               contract: Dict[str, Any], *,
                               record_rejection: bool = False) -> Tuple[Dict[str, Any], str]:
    path = result_path_for(run_dir, action_id)
    # File/link/owner failures remain fail-closed and are never turned into a
    # normal rejection receipt. Once the safe bytes are read, malformed JSON
    # and protocol contradictions are recordable rejections for this action.
    raw = read_regular(path, "assessment result", limit=MAX_RECORD_BYTES,
                       require_owner=True)
    assert raw is not None
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeError, ValueError) as exc:
        if record_rejection:
            raise AssessmentRejected(f"invalid assessment result: {exc}") from exc
        die(BLOCKED_EXIT, f"invalid assessment result: {exc}")
    try:
        assessment = validate_assessment(payload, contract, expected_action_id=action_id)
    except RuntimeExit as exc:
        if record_rejection:
            raise AssessmentRejected(exc.message) from exc
        raise
    return assessment, digest(assessment)


def read_raw_result_digest(run_dir: Path, action_id: str) -> str:
    """Hash a retained inbox record for an accepted-action replay check.

    The receipt carries the canonical, validated result.  A later state may
    have a newer contract revision or be terminal, so replay deliberately
    compares this record to its durable receipt without reinterpreting it
    under today's contract.
    """
    payload = read_json_file(result_path_for(run_dir, action_id), "assessment result",
                             limit=MAX_RECORD_BYTES, require_owner=True)
    return digest(payload)


def event_for_assessment(state: Dict[str, Any], assessment: Dict[str, Any],
                         assessment_digest: str, verify_ok: Optional[bool]) -> Dict[str, Any]:
    return {
        "type": "assessment",
        "action_id": assessment["action_id"],
        "digest": assessment_digest,
        "decision": assessment["decision"],
        "assessment": assessment,
        "cycle": state["cycle"],
        "phase": state["phase"],
        "verify_ok": verify_ok,
    }


def latest_rejection(run_dir: Path, action_id: str) -> Optional[str]:
    for event in reversed(history_entries(run_dir)):
        if event.get("type") == "rejection" and event.get("action_id") == action_id:
            reason = event.get("reason")
            return reason if isinstance(reason, str) else None
    return None


def record_rejection(run_dir: Path, state: Dict[str, Any], action_id: str,
                     reason: str) -> None:
    """Durably record a safe current-action rejection without advancing work."""
    bounded = require_text(reason, "rejection reason")
    commit_transition(run_dir, state, {
        "type": "rejection",
        "action_id": action_id,
        "contract_revision": state["contract"]["revision"],
        "reason": bounded,
    })


def cmd_init(args: argparse.Namespace) -> int:
    repo = legacy_runtime().resolve_repo(args.repo)
    run_dir = run_dir_for(repo)
    if args.max_cycles is None:
        maximum = legacy_runtime().default_max_cycles()
    else:
        maximum = args.max_cycles
    if type(maximum) is not int or maximum < 1:
        die(USAGE_EXIT, "--max-cycles must be >= 1")
    if args.verify is not None:
        require_text(args.verify, "--verify")
    policy = load_policy_snapshot()
    contract = read_contract_input(args.contract_file, policy["version"])

    def body() -> int:
        existing = load_state(run_dir)
        if existing is not None and not args.force:
            die(BLOCKED_EXIT, f"v2 run already exists at {run_dir} (use --force to restart)")
        if existing is not None and existing["version"] != STATE_VERSION:
            die(BLOCKED_EXIT, "existing run is not v2; migration is not supported")
        if existing is not None and existing["recovery"] is not None:
            die(BLOCKED_EXIT, "verifier outcome is uncertain; resolve-verifier before force restart")
        if existing is None:
            orphan_history = read_regular(run_dir / "history.jsonl", "history",
                                         limit=MAX_HISTORY_BYTES, missing_ok=True,
                                         require_owner=True)
            if orphan_history:
                die(BLOCKED_EXIT, "orphaned nonempty history without v2 state")
        ensure_results_dir(run_dir)
        state = new_state(repo, contract, policy, args.verify, maximum)
        legacy_runtime().ensure_exclude(repo)
        if existing is None:
            # Initialization itself is state creation; no acceptance receipt exists yet.
            save_state(run_dir, state)
        else:
            commit_transition(run_dir, state, {
                "type": "restart",
                "previous_phase": existing["phase"],
                "previous_cycle": existing["cycle"],
                "previous_contract": existing["contract"],
                "previous_policy_snapshot": existing["policy_snapshot"],
            })
        print_packet(state, f"initialized v2 ({state['phase']})")
        return 0

    return with_lock(run_dir, body)


def cmd_preview(args: argparse.Namespace) -> int:
    """Render a validated, normalized contract without binding a repository."""
    previous_bytecode = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        policy = load_policy_snapshot()
    finally:
        sys.dont_write_bytecode = previous_bytecode
    contract = read_contract_input(args.contract_file, policy["version"], allow_stdin=True)
    print(pretty_json({
        "mode": "preview",
        "status": "not_initialized",
        "execution": "not_executed",
        "contract": contract,
        "contract_digest": digest(contract),
        "policy_snapshot": policy,
    }), end="")
    return 0


def cmd_next(args: argparse.Namespace) -> int:
    repo = legacy_runtime().resolve_repo(args.repo)
    run_dir = run_dir_for(repo)

    def body() -> int:
        state = load_state(run_dir)
        if state is None:
            die(BLOCKED_EXIT, f"no v2 run at {run_dir} (init first)")
        outcome = f"next — reprint ({state['phase']})"
        action = state.get("action")
        reason = None
        if isinstance(action, dict) and state["recovery"] is None:
            reason = latest_rejection(run_dir, action["id"])
            if reason is not None:
                outcome += f"; last rejection: {reason}"
        print_packet(state, outcome, rejection=reason)
        return 0

    return with_lock(run_dir, body, create=False)


def checked_cli_action_id(value: Optional[str]) -> str:
    if value is None:
        die(USAGE_EXIT, "missing --action-id")
    if not isinstance(value, str) or ACTION_RE.fullmatch(value) is None:
        die(USAGE_EXIT, "--action-id must be 32 lowercase hexadecimal characters")
    return value


def cmd_submit(args: argparse.Namespace) -> int:
    action_id = checked_cli_action_id(args.action_id)
    repo = legacy_runtime().resolve_repo(args.repo)
    run_dir = run_dir_for(repo)

    def body() -> int:
        state = load_state(run_dir)
        if state is None:
            die(BLOCKED_EXIT, f"no v2 run at {run_dir} (init first)")
        action = state["action"] if state["phase"] == "active" else None
        if action is None or action_id != action["id"]:
            receipt = find_receipt(run_dir, action_id)
            if receipt is not None:
                if receipt["digest"] != read_raw_result_digest(run_dir, action_id):
                    die(BLOCKED_EXIT, "conflicting replay for accepted action_id")
                print_packet(state, f"replay — accepted action {action_id}")
                return 0
            if state["phase"] != "active":
                die(BLOCKED_EXIT, f"v2 run is {state['phase']}; no assessment is accepted")
            die(BLOCKED_EXIT, "stale action_id")
        if state["recovery"] is not None:
            die(BLOCKED_EXIT, "verifier outcome is uncertain; resolve-verifier before submitting again")
        try:
            assessment, assessment_digest = read_assessment_for_action(
                run_dir, action_id, state["contract"], record_rejection=True
            )
        except AssessmentRejected as exc:
            record_rejection(run_dir, state, action_id, str(exc))
            print_packet(state, f"assessment rejected: {exc}", rejection=str(exc))
            return BLOCKED_EXIT
        decision = assessment["decision"]
        if decision == "blocked":
            state["phase"] = "paused"
            state["action"] = None
            state["last_assessment"] = assessment
            state["pause"] = {
                "reason": assessment["blocker"]["reason"],
                "resumption_condition": assessment["blocker"]["resumption_condition"],
                "resume_on": assessment["blocker"]["resume_on"],
                "provenance": {"kind": "host_assessment", "reference": f"action {action_id}"},
            }
            event = event_for_assessment(state, assessment, assessment_digest, None)
            commit_transition(run_dir, state, event)
            print_packet(state, "assessment accepted (paused)")
            return 0

        # Reserve enough journal space before writing the uncertainty marker or
        # launching a verifier. The final state digest does not change this
        # receipt's encoded size; `halted`/false cover the longest values.
        ensure_history_capacity(run_dir, {
            "type": "assessment",
            "action_id": action_id,
            "digest": assessment_digest,
            "decision": decision,
            "assessment": assessment,
            "cycle": state["cycle"] + 1,
            "phase": "halted",
            "verify_ok": False,
        })
        verify_result: Optional[Dict[str, Any]] = None
        if state["verify_cmd"] is not None:
            # Persist the exact proposed action before launching an external
            # verifier. A crash here is recoverable only by explicit host action.
            prepared = json.loads(json.dumps(state))
            prepared["recovery"] = {
                "kind": "verifier_uncertain",
                "action_id": action_id,
                "digest": assessment_digest,
                "cycle": state["cycle"] + 1,
                "message": "Verifier may have started; inspect effects and resolve explicitly. Do not rerun automatically.",
            }
            save_state(run_dir, prepared)
            verify_result = legacy_runtime().run_verify(state["verify_cmd"], repo)
            if os.environ.get("UNTIL_LOOP_V2_CRASH_AFTER_VERIFIER") == "1":
                raise SystemExit(70)
        state["cycle"] += 1
        state["last_verify"] = verify_result
        state["last_assessment"] = assessment
        state["recovery"] = None
        state["pause"] = None
        verify_ok = None if verify_result is None else bool(verify_result["ok"])
        verifier_passes = verify_ok is not False
        if decision == "complete" and verifier_passes:
            state["phase"] = "done"
            state["action"] = None
        elif state["cycle"] >= state["max_cycles"]:
            state["phase"] = "halted"
            state["action"] = None
        else:
            state["phase"] = "active"
            state["action"] = action_for(run_dir, state["contract"]["revision"])
        event = event_for_assessment(state, assessment, assessment_digest, verify_ok)
        commit_transition(run_dir, state, event)
        print_packet(state, f"assessment accepted ({state['phase']})")
        return 0

    return with_lock(run_dir, body, create=False)


def read_provenance_file(path_value: Optional[str], label: str,
                         allowed: Tuple[str, ...]) -> Dict[str, str]:
    if path_value is None or not path_value.strip():
        die(USAGE_EXIT, f"missing --{label}-file")
    path, identity = external_input_path(path_value, f"{label} file")
    payload = read_json_file(path, f"{label} file", limit=MAX_RECORD_BYTES,
                             expected_identity=identity)
    if not isinstance(payload, dict) or set(payload) != {"provenance"}:
        die(BLOCKED_EXIT, f"{label} file has unsupported fields")
    return validate_provenance(payload["provenance"], f"{label}.provenance", allowed)


def cmd_resume(args: argparse.Namespace) -> int:
    provenance = read_provenance_file(args.provenance_file, "provenance",
                                      ("user_instruction", "condition_observed"))
    repo = legacy_runtime().resolve_repo(args.repo)
    run_dir = run_dir_for(repo)

    def body() -> int:
        state = load_state(run_dir)
        if state is None:
            die(BLOCKED_EXIT, f"no v2 run at {run_dir} (init first)")
        if state["phase"] != "paused":
            die(BLOCKED_EXIT, "resume requires a paused v2 run")
        pause = state["pause"]
        assert pause is not None
        if provenance["kind"] != pause["resume_on"]:
            die(BLOCKED_EXIT, "resume provenance does not satisfy the recorded resumption authority")
        state["phase"] = "active"
        state["pause"] = None
        state["action"] = action_for(run_dir, state["contract"]["revision"])
        event = {"type": "resume", "provenance": provenance, "cycle": state["cycle"]}
        commit_transition(run_dir, state, event)
        print_packet(state, "resumed v2 run")
        return 0

    return with_lock(run_dir, body, create=False)


def cmd_revise(args: argparse.Namespace) -> int:
    if args.revision_file is None or not args.revision_file.strip():
        die(USAGE_EXIT, "missing --revision-file")
    path, identity = external_input_path(args.revision_file, "revision file")
    payload = read_json_file(path, "revision file", limit=MAX_RECORD_BYTES,
                             expected_identity=identity)
    repo = legacy_runtime().resolve_repo(args.repo)
    run_dir = run_dir_for(repo)

    def body() -> int:
        state = load_state(run_dir)
        if state is None:
            die(BLOCKED_EXIT, f"no v2 run at {run_dir} (init first)")
        if state["phase"] != "active" or state["recovery"] is not None:
            die(BLOCKED_EXIT, "revision requires an active run without verifier uncertainty")
        expected = {"base_revision", "provenance", "interpretation", "criteria"}
        if not isinstance(payload, dict) or set(payload) != expected:
            die(BLOCKED_EXIT, "revision has unsupported fields")
        if require_int(payload["base_revision"], "revision.base_revision") != state["contract"]["revision"]:
            die(BLOCKED_EXIT, "revision.base_revision is stale")
        provenance = validate_provenance(payload["provenance"], "revision.provenance", ("user_correction",))
        revised = {
            "version": CONTRACT_VERSION,
            "policy": state["contract"]["policy"],
            "original_request": state["contract"]["original_request"],
            "interpretation": require_text(payload["interpretation"], "revision.interpretation"),
            "criteria": validate_criteria(payload["criteria"], "revision.criteria"),
            "revision": state["contract"]["revision"] + 1,
        }
        require_size(canonical_json(revised), MAX_RECORD_BYTES, "revised contract")
        previous_contract = state["contract"]
        old_revision = previous_contract["revision"]
        state["contract"] = revised
        state["last_assessment"] = None
        state["action"] = action_for(run_dir, revised["revision"])
        event = {
            "type": "revision",
            "from_revision": old_revision,
            "to_revision": revised["revision"],
            "provenance": provenance,
            "previous_contract": previous_contract,
            "contract": revised,
            "contract_digest": digest(revised),
        }
        commit_transition(run_dir, state, event)
        print_packet(state, "contract revision accepted")
        return 0

    return with_lock(run_dir, body, create=False)


def cmd_resolve_verifier(args: argparse.Namespace) -> int:
    action_id = checked_cli_action_id(args.action_id)
    if args.resolution_file is None or not args.resolution_file.strip():
        die(USAGE_EXIT, "missing --resolution-file")
    path, identity = external_input_path(args.resolution_file, "resolution file")
    resolution = read_json_file(path, "resolution file", limit=MAX_RECORD_BYTES,
                                expected_identity=identity)
    repo = legacy_runtime().resolve_repo(args.repo)
    run_dir = run_dir_for(repo)

    def body() -> int:
        state = load_state(run_dir)
        if state is None:
            die(BLOCKED_EXIT, f"no v2 run at {run_dir} (init first)")
        recovery = state["recovery"]
        if recovery is None or recovery["action_id"] != action_id:
            die(BLOCKED_EXIT, "no matching verifier uncertainty to resolve")
        expected = {"action_id", "provenance", "resolution"}
        if not isinstance(resolution, dict) or set(resolution) != expected:
            die(BLOCKED_EXIT, "resolution has unsupported fields")
        if require_action_id(resolution["action_id"], "resolution.action_id") != action_id:
            die(BLOCKED_EXIT, "resolution.action_id does not match")
        if resolution["resolution"] != "abandon":
            die(BLOCKED_EXIT, "resolution must abandon the uncertain attempt")
        provenance = validate_provenance(resolution["provenance"], "resolution.provenance",
                                         ("user_instruction", "host_observation"))
        state["recovery"] = None
        state["action"] = action_for(run_dir, state["contract"]["revision"])
        event = {
            "type": "recovery_resolution",
            "abandoned_action_id": action_id,
            "provenance": provenance,
            "cycle": state["cycle"],
        }
        commit_transition(run_dir, state, event)
        print_packet(state, "verifier uncertainty resolved; re-assess before a new submission")
        return 0

    return with_lock(run_dir, body, create=False)


def build_parser() -> UsageParser:
    parser = UsageParser(prog="until-loop v2")
    sub = parser.add_subparsers(dest="cmd", required=True)
    preview = sub.add_parser("preview")
    preview.add_argument("--contract-file", default=None)
    preview.set_defaults(func=cmd_preview)
    init = sub.add_parser("init")
    init.add_argument("--repo", default=None)
    init.add_argument("--contract-file", default=None)
    init.add_argument("--verify", default=None)
    init.add_argument("--max-cycles", type=int, default=None)
    init.add_argument("--force", action="store_true")
    init.set_defaults(func=cmd_init)
    nxt = sub.add_parser("next")
    nxt.add_argument("--repo", default=None)
    nxt.set_defaults(func=cmd_next)
    submit = sub.add_parser("submit")
    submit.add_argument("--repo", default=None)
    submit.add_argument("--action-id", default=None)
    submit.set_defaults(func=cmd_submit)
    resume = sub.add_parser("resume")
    resume.add_argument("--repo", default=None)
    resume.add_argument("--provenance-file", default=None)
    resume.set_defaults(func=cmd_resume)
    revise = sub.add_parser("revise")
    revise.add_argument("--repo", default=None)
    revise.add_argument("--revision-file", default=None)
    revise.set_defaults(func=cmd_revise)
    resolve = sub.add_parser("resolve-verifier")
    resolve.add_argument("--repo", default=None)
    resolve.add_argument("--action-id", default=None)
    resolve.add_argument("--resolution-file", default=None)
    resolve.set_defaults(func=cmd_resolve_verifier)
    return parser


def _main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args))


def main(argv: Optional[List[str]] = None) -> int:
    try:
        return _main(argv)
    except SystemExit as exc:
        if exc.code is None:
            return 0
        return int(exc.code) if isinstance(exc.code, int) else BLOCKED_EXIT
    except Exception as exc:
        print(f"error: internal v2: {type(exc).__name__}: {exc}", file=sys.stderr)
        return BLOCKED_EXIT


if __name__ == "__main__":
    sys.exit(main())
