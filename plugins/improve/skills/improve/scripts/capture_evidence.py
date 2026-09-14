#!/usr/bin/env python3
"""Capture factual, candidate-bound Improve evidence without changing a run.

The helper is intentionally an internal aid for an LLM or fixture host.  It
does not run checks, submit an until-loop action, revise a contract, classify a
review, or count a streak.  Instead it writes one JSON record that separates
facts observed by this process from claims supplied by the host.
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import io
import json
import os
import stat
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from types import ModuleType
from typing import Any, Dict, List, Optional, Sequence, Tuple


FORMAT = "until-loop-improve-evidence/v1"
CATALOGUE_FORMAT = "until-loop-improve-history-catalogue/v1"
CATALOGUE_NAME = "history-catalogue.json"
MAX_STATE_BYTES = 256 * 1024
MAX_RECORD_BYTES = 4 * 1024 * 1024
MAX_ARTIFACT_BYTES = 64 * 1024 * 1024
_V2_RUNTIME: Optional[Any] = None


class EvidenceCaptureError(RuntimeError):
    """A capture boundary that cannot be safely or accurately inspected."""


def canonical_json(value: Any) -> bytes:
    try:
        return json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, UnicodeError, ValueError) as error:
        raise EvidenceCaptureError(f"record is not JSON-safe: {error}") from error


def sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def absolute_path(value: Path) -> Path:
    return Path(os.path.abspath(os.fspath(value)))


def require_nonempty(value: Optional[str], label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise EvidenceCaptureError(f"{label} must be nonempty")
    return value


def lstat_or_error(path: Path, label: str) -> os.stat_result:
    try:
        return path.lstat()
    except OSError as error:
        raise EvidenceCaptureError(f"cannot inspect {label}: {path}: {error}") from error


def require_directory(path: Path, label: str) -> os.stat_result:
    item = lstat_or_error(path, label)
    if path.is_symlink() or not stat.S_ISDIR(item.st_mode):
        raise EvidenceCaptureError(f"unsafe {label}: {path}")
    return item


def ensure_safe_child_directory(base: Path, target: Path, label: str) -> Path:
    """Create a directory only by walking safe, non-symlink descendants."""
    base = absolute_path(base)
    target = absolute_path(target)
    try:
        relative = target.relative_to(base)
    except ValueError as error:
        raise EvidenceCaptureError(f"{label} must remain below {base}: {target}") from error
    require_directory(base, "repository directory")
    current = base
    for component in relative.parts:
        current = current / component
        try:
            item = current.lstat()
        except FileNotFoundError:
            try:
                current.mkdir(mode=0o700)
            except OSError as create_error:
                raise EvidenceCaptureError(
                    f"cannot create {label}: {current}: {create_error}"
                ) from create_error
            item = lstat_or_error(current, label)
        except OSError as error:
            raise EvidenceCaptureError(f"cannot inspect {label}: {current}: {error}") from error
        if current.is_symlink() or not stat.S_ISDIR(item.st_mode):
            raise EvidenceCaptureError(f"unsafe {label}: {current}")
    return target


def ensure_explicit_directory(target: Path, label: str) -> Path:
    """Create an explicitly authorized evidence directory without following it."""
    target = absolute_path(target)
    missing: List[Path] = []
    current = target
    while not current.exists() and not current.is_symlink():
        missing.append(current)
        if current.parent == current:
            raise EvidenceCaptureError(f"cannot locate parent for {label}: {target}")
        current = current.parent
    require_directory(current, label)
    for path in reversed(missing):
        try:
            path.mkdir(mode=0o700)
        except FileExistsError:
            pass
        except OSError as error:
            raise EvidenceCaptureError(f"cannot create {label}: {path}: {error}") from error
        require_directory(path, label)
    return target


def checked_owned_file(path: Path, label: str, *, missing_ok: bool = False,
                       limit: int = MAX_RECORD_BYTES) -> Optional[bytes]:
    """Read a helper-owned file only when it is a single-link regular file."""
    try:
        before = path.lstat()
    except FileNotFoundError:
        if missing_ok:
            return None
        raise EvidenceCaptureError(f"missing {label}: {path}")
    except OSError as error:
        raise EvidenceCaptureError(f"cannot inspect {label}: {path}: {error}") from error
    if path.is_symlink() or not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
        raise EvidenceCaptureError(f"unsafe {label}: {path}")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise EvidenceCaptureError(f"cannot read {label}: {path}: {error}") from error
    try:
        after = os.fstat(descriptor)
        if (
            not stat.S_ISREG(after.st_mode)
            or after.st_nlink != 1
            or after.st_dev != before.st_dev
            or after.st_ino != before.st_ino
        ):
            raise EvidenceCaptureError(f"unsafe {label}: {path}")
        chunks: List[bytes] = []
        total = 0
        while True:
            chunk = os.read(descriptor, min(65536, limit + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > limit:
                raise EvidenceCaptureError(f"{label} exceeds {limit} bytes: {path}")
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def write_new_owned_file(path: Path, raw: bytes, label: str) -> None:
    """Create a new helper-owned record and retain a restrictive mode."""
    if path.exists() or path.is_symlink():
        raise EvidenceCaptureError(f"{label} already exists: {path}")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags, 0o600)
    except OSError as error:
        raise EvidenceCaptureError(f"cannot create {label}: {path}: {error}") from error
    try:
        remaining = memoryview(raw)
        while remaining:
            written = os.write(descriptor, remaining)
            remaining = remaining[written:]
        os.fsync(descriptor)
        item = os.fstat(descriptor)
        if not stat.S_ISREG(item.st_mode) or item.st_nlink != 1:
            raise EvidenceCaptureError(f"unsafe {label}: {path}")
    except BaseException:
        try:
            os.unlink(path)
        except OSError:
            pass
        raise
    finally:
        os.close(descriptor)


def replace_owned_file(path: Path, raw: bytes, label: str) -> None:
    """Atomically replace a previously checked helper-owned evidence record."""
    if path.exists() or path.is_symlink():
        checked_owned_file(path, label)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    write_new_owned_file(temporary, raw, f"temporary {label}")
    try:
        os.replace(temporary, path)
    except OSError as error:
        try:
            temporary.unlink()
        except OSError:
            pass
        raise EvidenceCaptureError(f"cannot replace {label}: {path}: {error}") from error
    checked_owned_file(path, label)


def git_environment() -> Dict[str, str]:
    environment = {
        key: value for key, value in os.environ.items() if not key.startswith("GIT_")
    }
    environment.update(
        {
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_TERMINAL_PROMPT": "0",
            "LC_ALL": "C",
            "LANG": "C",
        }
    )
    return environment


def git_call(repo: Path, *arguments: str) -> Tuple[Optional[subprocess.CompletedProcess[bytes]], Optional[str]]:
    try:
        return (
            subprocess.run(
                ["git", *arguments],
                cwd=str(repo),
                env=git_environment(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
                timeout=30,
            ),
            None,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        return None, f"{type(error).__name__}: {error}"


def process_detail(result: Optional[subprocess.CompletedProcess[bytes]], error: Optional[str]) -> str:
    if error is not None:
        return error
    assert result is not None
    detail = result.stderr.decode("utf-8", errors="replace").strip()
    return detail or f"exit {result.returncode}"


def discover_git(repo: Path) -> Dict[str, Any]:
    result, error = git_call(repo, "rev-parse", "--show-toplevel")
    if result is None or result.returncode != 0:
        return {"status": "unavailable", "detail": process_detail(result, error)}
    root_text = result.stdout.decode("utf-8", errors="replace").strip()
    root = Path(root_text).resolve()
    if root != repo.resolve():
        return {
            "status": "unavailable",
            "detail": f"--repo is not the Git top-level ({root})",
        }
    head_result, head_error = git_call(repo, "rev-parse", "--verify", "HEAD^{commit}")
    if head_result is None:
        head: Dict[str, Any] = {"status": "unavailable", "detail": process_detail(head_result, head_error)}
    elif head_result.returncode != 0:
        head = {"status": "absent", "reason": "unborn_or_no_reachable_head"}
    else:
        head = {
            "status": "present",
            "commit": head_result.stdout.decode("ascii", errors="replace").strip(),
        }
    return {"status": "available", "root": str(root), "head": head}


def safe_scope(value: str) -> str:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise EvidenceCaptureError("scope path must be a nonempty relative path")
    path = Path(value)
    if path.is_absolute() or value in (".", "..") or any(part == ".." for part in path.parts):
        raise EvidenceCaptureError(f"unsafe scope path: {value!r}")
    return str(path)


def observed_file(path: Path) -> Dict[str, Any]:
    """Describe an external artifact without treating it as helper-owned."""
    try:
        before = path.lstat()
    except FileNotFoundError:
        return {"status": "missing"}
    except OSError as error:
        return {"status": "unavailable", "detail": str(error)}
    if stat.S_ISLNK(before.st_mode):
        try:
            target = os.readlink(path)
        except OSError:
            target = None
        return {"status": "not_regular", "kind": "symlink", "target": target}
    if stat.S_ISDIR(before.st_mode):
        return {"status": "not_regular", "kind": "directory"}
    if not stat.S_ISREG(before.st_mode):
        return {"status": "not_regular", "kind": "special"}
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        return {"status": "unavailable", "detail": str(error)}
    try:
        after = os.fstat(descriptor)
        if not stat.S_ISREG(after.st_mode) or (after.st_dev, after.st_ino) != (before.st_dev, before.st_ino):
            return {"status": "changed_during_capture"}
        chunks: List[bytes] = []
        total = 0
        while True:
            chunk = os.read(descriptor, min(65536, MAX_ARTIFACT_BYTES + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > MAX_ARTIFACT_BYTES:
                return {"status": "too_large", "limit": MAX_ARTIFACT_BYTES}
        raw = b"".join(chunks)
        return {
            "status": "present",
            "kind": "file",
            "bytes": len(raw),
            "sha256": sha256(raw),
            "identity": {
                "device": after.st_dev,
                "inode": after.st_ino,
                "mode": stat.S_IMODE(after.st_mode),
                "nlink": after.st_nlink,
            },
        }
    finally:
        os.close(descriptor)


def index_identity(repo: Path, scope: str, git_info: Dict[str, Any]) -> Dict[str, Any]:
    if git_info["status"] != "available":
        return {"status": "unavailable", "detail": git_info["detail"]}
    result, error = git_call(repo, "ls-files", "--stage", "-z", "--", scope)
    if result is None or result.returncode != 0:
        return {"status": "unavailable", "detail": process_detail(result, error)}
    entries: List[Dict[str, Any]] = []
    for raw in result.stdout.split(b"\0"):
        if not raw:
            continue
        metadata, separator, path_bytes = raw.partition(b"\t")
        fields = metadata.decode("ascii", errors="replace").split()
        if not separator or len(fields) != 3:
            return {"status": "unavailable", "detail": "unparseable git index record"}
        entries.append(
            {
                "mode": fields[0],
                "object_id": fields[1],
                "stage": fields[2],
                "path": path_bytes.decode("utf-8", errors="replace"),
            }
        )
    return {"status": "present" if entries else "absent", "entries": entries}


def diff_identity(repo: Path, scope: str, git_info: Dict[str, Any], *, cached: bool) -> Dict[str, Any]:
    if git_info["status"] != "available":
        return {"status": "unavailable", "detail": git_info["detail"]}
    arguments = ["diff", "--binary", "--no-ext-diff", "--no-textconv"]
    if cached:
        arguments.append("--cached")
    arguments.extend(("--", scope))
    result, error = git_call(repo, *arguments)
    if result is None or result.returncode != 0:
        return {"status": "unavailable", "detail": process_detail(result, error)}
    raw = result.stdout
    return {
        "status": "observed",
        "changed": bool(raw),
        "bytes": len(raw),
        "sha256": sha256(raw),
    }


def collect_scope(repo: Path, scopes: Sequence[str], git_info: Dict[str, Any]) -> List[Dict[str, Any]]:
    records: List[Dict[str, Any]] = []
    for scope in scopes:
        working = observed_file(repo / scope)
        if working.get("status") == "not_regular" and working.get("kind") == "directory":
            raise EvidenceCaptureError(f"--scope must name a file, not a directory: {scope}")
        records.append({
            "path": scope,
            "working": working,
            "index": index_identity(repo, scope, git_info),
            "staged": diff_identity(repo, scope, git_info, cached=True),
            "unstaged": diff_identity(repo, scope, git_info, cached=False),
        })
    return records


def checked_runtime_state(repo: Path) -> Dict[str, Any]:
    run_dir = repo / ".until-loop"
    if not run_dir.exists() and not run_dir.is_symlink():
        return {"status": "absent", "kind": None, "action_id": None, "contract_revision": None}
    require_directory(run_dir, "run directory")
    state_path = run_dir / "state.json"
    raw = checked_owned_file(state_path, "runtime state", missing_ok=True, limit=MAX_STATE_BYTES)
    if raw is None:
        return {"status": "absent", "kind": None, "action_id": None, "contract_revision": None}
    try:
        state = json.loads(raw.decode("utf-8"))
    except (UnicodeError, ValueError) as error:
        raise EvidenceCaptureError(f"invalid runtime state: {state_path}: {error}") from error
    if not isinstance(state, dict) or type(state.get("version")) is not int:
        raise EvidenceCaptureError(f"invalid runtime state: {state_path}")
    base = {
        "status": "present",
        "state_path": str(state_path),
        "state_sha256": sha256(raw),
    }
    if state["version"] == 1:
        base.update(
            {
                "kind": "v1",
                "validation": "v1_state_identified_no_action_binding",
                "action_id": None,
                "contract_revision": None,
            }
        )
        return base
    if state["version"] != 2:
        raise EvidenceCaptureError(f"unsupported runtime state version: {state['version']}")
    validate_v2_state_with_runtime(state, run_dir)
    revision = state["contract"]["revision"]
    action = state["action"]
    if state["phase"] == "active":
        assert isinstance(action, dict)
        action_id = action["id"]
    else:
        if action is not None:
            raise EvidenceCaptureError("non-active v2 runtime has a current action")
        action_id = None
    base.update(
        {
            "kind": "v2",
            "validation": "v2_action_revision_and_result_path_checked",
            "phase": state["phase"],
            "action_id": action_id,
            "contract_revision": revision,
        }
    )
    return base


def maintained_v2_runtime() -> Any:
    """Load the maintained v2 validator instead of duplicating its schema."""
    global _V2_RUNTIME
    if _V2_RUNTIME is not None:
        return _V2_RUNTIME
    source = Path(__file__).resolve().parents[3] / "scripts" / "until_loop_v2.py"
    raw = checked_owned_file(source, "maintained v2 runtime", limit=MAX_STATE_BYTES)
    assert raw is not None
    module = ModuleType("_improve_evidence_v2_runtime")
    module.__file__ = str(source)
    try:
        exec(compile(raw, str(source), "exec"), module.__dict__)
    except (ImportError, OSError, RuntimeError, SyntaxError) as error:
        raise EvidenceCaptureError(f"cannot load maintained v2 runtime: {error}") from error
    _V2_RUNTIME = module
    return module


def validate_v2_state_with_runtime(state: Dict[str, Any], run_dir: Path) -> None:
    runtime = maintained_v2_runtime()
    rejected = io.StringIO()
    previous_bytecode = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        with contextlib.redirect_stderr(rejected):
            runtime.validate_state(state, run_dir)
    except BaseException as error:
        detail = getattr(error, "message", "") or str(error) or rejected.getvalue().strip()
        raise EvidenceCaptureError(f"invalid v2 runtime state: {detail}") from error
    finally:
        sys.dont_write_bytecode = previous_bytecode


def check_supplied_runtime_binding(runtime: Dict[str, Any], action_id: Optional[str],
                                   revision: Optional[int]) -> None:
    if action_id is not None:
        if runtime.get("kind") != "v2" or runtime.get("action_id") is None:
            raise EvidenceCaptureError("--action-id requires a current validated v2 action")
        if action_id != runtime["action_id"]:
            raise EvidenceCaptureError("--action-id does not match the current v2 action")
    if revision is not None:
        if runtime.get("kind") != "v2":
            raise EvidenceCaptureError("--contract-revision requires a validated v2 state")
        if revision != runtime["contract_revision"]:
            raise EvidenceCaptureError("--contract-revision does not match the v2 state")


def load_catalogue(evidence_dir: Path) -> Tuple[Path, Dict[str, Any], bool, Optional[bytes]]:
    path = evidence_dir / CATALOGUE_NAME
    raw = checked_owned_file(path, "history catalogue", missing_ok=True)
    if raw is None:
        return path, {"format": CATALOGUE_FORMAT, "commits": {}}, True, None
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeError, ValueError) as error:
        raise EvidenceCaptureError(f"invalid history catalogue: {error}") from error
    if not isinstance(value, dict) or set(value) != {"format", "commits"} or value.get("format") != CATALOGUE_FORMAT:
        raise EvidenceCaptureError("unsupported history catalogue")
    commits = value.get("commits")
    if not isinstance(commits, dict):
        raise EvidenceCaptureError("invalid history catalogue commits")
    for commit_id, entry in commits.items():
        if not isinstance(commit_id, str) or not isinstance(entry, dict):
            raise EvidenceCaptureError("invalid history catalogue entry")
        if set(entry) != {"full_message", "message_sha256"} or not isinstance(entry["full_message"], str):
            raise EvidenceCaptureError("invalid history catalogue message")
        if entry["message_sha256"] != sha256(entry["full_message"].encode("utf-8")):
            raise EvidenceCaptureError("history catalogue message hash mismatch")
    return path, value, False, raw


def git_history(repo: Path, window: int, git_info: Dict[str, Any]) -> Tuple[str, List[Tuple[str, str]]]:
    if git_info["status"] != "available":
        return "unavailable", []
    if git_info["head"]["status"] != "present":
        return "absent", []
    head = git_info["head"]["commit"]
    result, error = git_call(repo, "log", "-z", "--format=%H%x00%B", "-n", str(window), head)
    if result is None or result.returncode != 0:
        raise EvidenceCaptureError(f"cannot read Git history: {process_detail(result, error)}")
    # ``-z`` supplies the record terminator.  Keep empty fields because Git
    # permits an --allow-empty-message commit, whose %B field is empty.
    fields = result.stdout.split(b"\0")
    if fields and fields[-1] == b"":
        fields.pop()
    if len(fields) % 2:
        raise EvidenceCaptureError("unparseable full Git commit messages")
    entries: List[Tuple[str, str]] = []
    for position in range(0, len(fields), 2):
        commit_id = fields[position].decode("ascii", errors="replace")
        message = fields[position + 1].decode("utf-8", errors="replace")
        if not commit_id:
            raise EvidenceCaptureError("empty Git history commit id")
        entries.append((commit_id, message))
    return "present", entries


def prepare_history(repo: Path, window: int, git_info: Dict[str, Any],
                    evidence_dir: Path) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    catalogue_path, catalogue, is_new, existing_raw = load_catalogue(evidence_dir)
    status, entries = git_history(repo, window, git_info)
    commits = catalogue["commits"]
    added: List[str] = []
    reused: List[str] = []
    window_entries: List[Dict[str, str]] = []
    for commit_id, message in entries:
        message_hash = sha256(message.encode("utf-8"))
        known = commits.get(commit_id)
        if known is None:
            commits[commit_id] = {"full_message": message, "message_sha256": message_hash}
            added.append(commit_id)
        else:
            if known["full_message"] != message or known["message_sha256"] != message_hash:
                raise EvidenceCaptureError(f"history catalogue conflicts for {commit_id}")
            reused.append(commit_id)
        window_entries.append(
            {
                "commit_id": commit_id,
                "catalogue_key": commit_id,
                "full_message_sha256": message_hash,
            }
        )
    catalogue_raw = json.dumps(catalogue, sort_keys=True, indent=2, ensure_ascii=False).encode("utf-8") + b"\n"
    expected_raw = catalogue_raw if is_new or added else existing_raw
    assert expected_raw is not None
    history = {
        "source": "tool_observed",
        "requested_window": window,
        "status": status,
        "window": window_entries,
        "catalogue": {
            "path": str(catalogue_path),
            "sha256": sha256(expected_raw),
            "added_commit_ids": added,
            "reused_commit_ids": reused,
        },
    }
    return history, {
        "path": catalogue_path,
        "raw": catalogue_raw,
        "is_new": is_new,
        "changed": bool(added),
        "expected_sha256": sha256(expected_raw),
    }


def persist_history(history: Dict[str, Any], pending: Dict[str, Any]) -> None:
    catalogue_path = pending["path"]
    if pending["is_new"]:
        write_new_owned_file(catalogue_path, pending["raw"], "history catalogue")
    elif pending["changed"]:
        replace_owned_file(catalogue_path, pending["raw"], "history catalogue")
    final_raw = checked_owned_file(catalogue_path, "history catalogue")
    assert final_raw is not None
    if sha256(final_raw) != pending["expected_sha256"]:
        raise EvidenceCaptureError("history catalogue changed while being persisted")


def artifact_path(repo: Path, value: str) -> Path:
    candidate = Path(value).expanduser()
    if not candidate.is_absolute():
        candidate = repo / candidate
    return absolute_path(candidate)


def parse_claims(raw_claims: Sequence[str], expected: int, label: str) -> List[Optional[Dict[str, Any]]]:
    if not raw_claims:
        return [None] * expected
    if len(raw_claims) != expected:
        raise EvidenceCaptureError(f"{label} count must match its paths")
    parsed: List[Optional[Dict[str, Any]]] = []
    for raw in raw_claims:
        try:
            claim = json.loads(raw)
        except ValueError as error:
            raise EvidenceCaptureError(f"invalid {label}: {error}") from error
        if not isinstance(claim, dict):
            raise EvidenceCaptureError(f"{label} must be a JSON object")
        parsed.append(claim)
    return parsed


def artifact_scope_identity(scope: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Keep only artifact-relevant fields; full metadata stays in tool_facts."""
    identity: List[Dict[str, Any]] = []
    for entry in scope:
        working = entry["working"]
        compact_working: Dict[str, Any] = {"status": working.get("status")}
        if "kind" in working:
            compact_working["kind"] = working["kind"]
        if working.get("status") == "present":
            for key in ("bytes", "sha256"):
                compact_working[key] = working.get(key)
            metadata = working.get("identity")
            if isinstance(metadata, dict) and type(metadata.get("mode")) is int:
                compact_working["executable"] = bool(metadata["mode"] & 0o111)
        elif "target" in working:
            compact_working["target"] = working["target"]
        identity.append(
            {
                "path": entry["path"],
                "working": compact_working,
                "index": {
                    "status": entry["index"].get("status"),
                    "entries": entry["index"].get("entries", []),
                },
                "staged": {
                    key: entry["staged"].get(key)
                    for key in ("status", "changed", "sha256")
                },
                "unstaged": {
                    key: entry["unstaged"].get(key)
                    for key in ("status", "changed", "sha256")
                },
            }
        )
    return identity


def incomplete_scope_identity(scope: Sequence[Dict[str, Any]]) -> List[str]:
    """Name only facts that prevent a complete file-level identity."""
    incomplete: List[str] = []
    for entry in scope:
        path = entry["path"]
        working = entry["working"]
        if working.get("status") in ("unavailable", "too_large", "changed_during_capture"):
            incomplete.append(f"{path}:working")
        elif working.get("status") == "not_regular" and working.get("kind") != "symlink":
            incomplete.append(f"{path}:working")
        if entry["index"].get("status") not in ("present", "absent"):
            incomplete.append(f"{path}:index")
        for key in ("staged", "unstaged"):
            if entry[key].get("status") != "observed":
                incomplete.append(f"{path}:{key}")
    return incomplete


def candidate_from_facts(git_info: Dict[str, Any], scope: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    incomplete = incomplete_scope_identity(scope)
    observed = {
        "head": git_info.get("head"),
        "scope": artifact_scope_identity(scope),
        "complete": not incomplete,
    }
    if incomplete:
        observed["incomplete"] = incomplete
    observed["digest"] = sha256(canonical_json(observed))
    return observed


def candidate_binding(artifact: Dict[str, Any], claim: Optional[Dict[str, Any]],
                      candidate: Dict[str, Any]) -> Dict[str, Any]:
    if artifact.get("status") != "present":
        return {"status": "missing", "reason": "artifact is not a present regular file"}
    if not candidate.get("complete"):
        return {"status": "unbound", "reason": "candidate identity is incomplete"}
    if claim is None or "candidate" not in claim:
        return {"status": "unbound", "reason": "no host-declared candidate digest"}
    declared = claim["candidate"]
    if not isinstance(declared, dict) or not isinstance(declared.get("digest"), str):
        return {"status": "unbound", "reason": "host candidate digest is absent or malformed"}
    if declared["digest"] != candidate["digest"]:
        return {
            "status": "stale",
            "reason": "host-declared candidate digest differs from this observed snapshot",
            "declared_digest": declared["digest"],
            "observed_digest": candidate["digest"],
        }
    return {"status": "current", "candidate_digest": candidate["digest"]}


def collect_artifacts(repo: Path, paths: Sequence[str], claims: Sequence[Optional[Dict[str, Any]]],
                      candidate: Dict[str, Any], *, checks: bool) -> List[Dict[str, Any]]:
    records: List[Dict[str, Any]] = []
    for index, (value, claim) in enumerate(zip(paths, claims)):
        path = artifact_path(repo, value)
        artifact = observed_file(path)
        record: Dict[str, Any] = {
            "requested_path": value,
            "path": str(path),
            "artifact": artifact,
            "host_claim_index": index if claim is not None else None,
            "candidate_binding": candidate_binding(artifact, claim, candidate),
        }
        if checks:
            if claim is not None and "returncode" in claim:
                returncode = claim["returncode"]
                if type(returncode) is not int:
                    raise EvidenceCaptureError("check host-declared returncode must be an integer")
                record["returncode"] = {"source": "host_declared", "value": returncode}
            else:
                record["returncode"] = {"source": "absent", "value": None}
        records.append(record)
    return records


def runtime_binding(runtime: Dict[str, Any]) -> Dict[str, Any]:
    return {
        key: runtime.get(key)
        for key in ("kind", "phase", "action_id", "contract_revision", "state_sha256")
    }


def load_prior_record(path_value: str, repo: Path, evidence_dir: Path) -> Dict[str, Any]:
    supplied = Path(path_value).expanduser()
    path = absolute_path(supplied if supplied.is_absolute() else repo / supplied)
    try:
        path.relative_to(evidence_dir)
    except ValueError as error:
        raise EvidenceCaptureError("--prior-record must remain in the evidence directory") from error
    raw = checked_owned_file(path, "prior evidence record")
    assert raw is not None
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeError, ValueError) as error:
        raise EvidenceCaptureError(f"invalid prior evidence record: {error}") from error
    if not isinstance(value, dict) or value.get("format") != FORMAT or not isinstance(value.get("candidate"), dict):
        raise EvidenceCaptureError("unsupported prior evidence record")
    tool_facts = value.get("tool_facts")
    runtime = tool_facts.get("runtime") if isinstance(tool_facts, dict) else {}
    return {
        "path": str(path),
        "sha256": sha256(raw),
        "candidate": value["candidate"],
        "runtime": runtime if isinstance(runtime, dict) else {},
    }


def compare_prior(prior: Optional[Dict[str, Any]], candidate: Dict[str, Any],
                  runtime: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if prior is None:
        return None
    previous = prior["candidate"]
    before = previous.get("digest")
    after = candidate["digest"]
    if before == after:
        status = "unchanged"
        changed: List[str] = []
    else:
        status = "drifted"
        changed = []
        for key in ("head", "scope"):
            if previous.get(key) != candidate.get(key):
                changed.append(key)
    return {
        "prior_record": prior["path"],
        "prior_record_sha256": prior["sha256"],
        "status": status,
        "before_candidate_digest": before,
        "after_candidate_digest": after,
        "changed_components": changed,
        "runtime_binding_changed": runtime_binding(prior["runtime"]) != runtime_binding(runtime),
    }


def evidence_directory(repo: Path, explicit: Optional[str]) -> Path:
    if explicit is None:
        return ensure_safe_child_directory(
            repo, repo / ".until-loop" / "evidence", "default evidence directory"
        )
    supplied = Path(explicit).expanduser()
    if not supplied.is_absolute():
        supplied = repo / supplied
    if supplied.is_symlink():
        raise EvidenceCaptureError(f"unsafe explicit evidence directory: {supplied}")
    return ensure_explicit_directory(supplied.resolve(strict=False), "explicit evidence directory")


def record_name() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"improve-evidence-{stamp}-{uuid.uuid4().hex[:12]}.json"


def observe_candidate(repo: Path, scopes: Sequence[str]) -> Dict[str, Any]:
    runtime = checked_runtime_state(repo)
    git_info = discover_git(repo)
    scope = collect_scope(repo, scopes, git_info)
    return {
        "runtime": runtime,
        "git": git_info,
        "scope": scope,
        "candidate": candidate_from_facts(git_info, scope),
    }


def require_stable_capture(before: Dict[str, Any], after: Dict[str, Any]) -> None:
    changed: List[str] = []
    if before["git"].get("head") != after["git"].get("head"):
        changed.append("HEAD")
    if before["scope"] != after["scope"]:
        changed.append("scope")
    if runtime_binding(before["runtime"]) != runtime_binding(after["runtime"]):
        changed.append("runtime binding")
    if changed:
        raise EvidenceCaptureError(
            "candidate changed during capture (" + ", ".join(changed) + "); retry"
        )


def capture(args: argparse.Namespace) -> Path:
    repo = Path(args.repo).expanduser().resolve()
    if not repo.is_dir():
        raise EvidenceCaptureError(f"repository directory does not exist: {repo}")
    owner = require_nonempty(args.owner, "--owner")
    if type(args.history_window) is not int or args.history_window < 1:
        raise EvidenceCaptureError("--history-window must be a positive integer")
    scopes = [safe_scope(value) for value in args.scope]
    if len(set(scopes)) != len(scopes):
        raise EvidenceCaptureError("--scope values must be distinct")
    evidence_dir = evidence_directory(repo, args.evidence_dir)
    before = observe_candidate(repo, scopes)
    check_supplied_runtime_binding(before["runtime"], args.action_id, args.contract_revision)
    history, pending_history = prepare_history(
        repo, args.history_window, before["git"], evidence_dir
    )
    reference_claims = parse_claims(args.reference_claim, len(args.reference), "--reference-claim")
    check_claims = parse_claims(args.check_claim, len(args.check), "--check-claim")
    references = collect_artifacts(
        repo, args.reference, reference_claims, before["candidate"], checks=False
    )
    checks = collect_artifacts(repo, args.check, check_claims, before["candidate"], checks=True)
    after = observe_candidate(repo, scopes)
    require_stable_capture(before, after)
    prior = load_prior_record(args.prior_record, repo, evidence_dir) if args.prior_record else None
    record = {
        "format": FORMAT,
        "captured_at": utc_now(),
        "capture_mode": args.command,
        "owner_binding": {
            "source": "host_declared",
            "owner": owner,
            "history_window": args.history_window,
            "scope_paths": scopes,
        },
        "candidate": before["candidate"],
        "tool_facts": {
            "git": before["git"],
            "runtime": before["runtime"],
            "scope": before["scope"],
            "history": history,
            "references": references,
            "checks": checks,
        },
        "host_claims": {
            "reviewer": {
                "source": "host_declared",
                "identity": args.reviewer_identity,
                "role": args.reviewer_role,
            },
            "references": [
                {
                    "source": "host_declared" if claim is not None else "absent",
                    "value": claim,
                }
                for claim in reference_claims
            ],
            "checks": [
                {
                    "source": "host_declared" if claim is not None else "absent",
                    "value": claim,
                }
                for claim in check_claims
            ],
        },
        "comparison": compare_prior(prior, before["candidate"], before["runtime"]),
        "limits": {
            "does_not_execute_checks": True,
            "returncodes_are_host_declared_unless_a_runner_is_added": True,
            "candidate_binding_does_not_establish_check_semantics": True,
            "does_not_submit_or_revise_runtime": True,
            "does_not_count_review_cycles_or_streaks": True,
        },
    }
    raw = json.dumps(record, sort_keys=True, indent=2, ensure_ascii=False).encode("utf-8") + b"\n"
    if len(raw) > MAX_RECORD_BYTES:
        raise EvidenceCaptureError(f"evidence record exceeds {MAX_RECORD_BYTES} bytes")
    destination = evidence_dir / record_name()
    persist_history(history, pending_history)
    write_new_owned_file(destination, raw, "evidence record")
    return destination


class Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        print(f"error: {message}", file=sys.stderr)
        raise SystemExit(64)


def add_capture_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--repo", required=True)
    parser.add_argument("--owner", required=True)
    parser.add_argument("--history-window", required=True, type=int)
    parser.add_argument("--scope", action="append", required=True)
    parser.add_argument("--evidence-dir")
    parser.add_argument("--prior-record")
    parser.add_argument("--action-id")
    parser.add_argument("--contract-revision", type=int)
    parser.add_argument("--reviewer-identity")
    parser.add_argument("--reviewer-role")
    parser.add_argument("--reference", action="append", default=[])
    parser.add_argument("--reference-claim", action="append", default=[])
    parser.add_argument("--check", action="append", default=[])
    parser.add_argument("--check-claim", action="append", default=[])


def build_parser() -> Parser:
    parser = Parser(prog="capture_evidence.py")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("snapshot", "capture"):
        add_capture_arguments(commands.add_parser(name))
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    try:
        args = build_parser().parse_args(argv)
        print(capture(args))
        return 0
    except EvidenceCaptureError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    except SystemExit as error:
        if error.code is None:
            return 0
        return int(error.code) if isinstance(error.code, int) else 64


if __name__ == "__main__":
    sys.exit(main())
