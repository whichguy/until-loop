#!/usr/bin/env python3
"""Run raw-request, end-to-end candidate v2 hosts without evaluator hints.

This is an evidence harness, not a semantic judge.  It gives each Codex host a
bound workspace, the candidate skill location, and the manifest's raw request.
It preserves event JSONL, stderr, final response, timing, usage projections,
and non-following workspace/state snapshots outside the individual fixture.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import os
import signal
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


HERE = Path(__file__).resolve()
CANDIDATE_ROOT = HERE.parents[1]
VALIDATION_ROOT = CANDIDATE_ROOT.parent / "until-loop-v2-validation"
INTENT_CASES_PATH = HERE.with_name("intent-cases.json")
SKILL_PATH = CANDIDATE_ROOT / "SKILL.md"
TIMEOUT_SECONDS = 240
NORMAL_CASE_IDS = {
    "multi-clause-weak-test",
    "prechecked-while-until",
    "qualitative-draft-no-verifier",
    "blocked-missing-input",
}
COLD_CASE_ID = "cold-resume-two-increments"
BOOKKEEPING_VARIANT_ID = "prechecked-while-until-bookkeeping-permitted"
BOOKKEEPING_VARIANT_REQUEST = (
    "While the feature flag or changelog entry is missing, make the smallest "
    "correction. Stop once settings.json has `\"enabled\": true` and "
    "CHANGELOG.md has exactly one `Feature enabled` entry. Check first: if "
    "both already hold, keep product files byte-for-byte unchanged. You may "
    "create until-loop bookkeeping to record completion."
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def write_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding="utf-8")


def write_json(path: Path, value: Any) -> None:
    write_text(path, json.dumps(value, indent=2, sort_keys=True) + "\n")


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def state_projection(raw: bytes) -> dict[str, Any]:
    result: dict[str, Any] = {
        "present": True,
        "safe_regular_single_link": True,
        "bytes": len(raw),
        "sha256": hashlib.sha256(raw).hexdigest(),
    }
    try:
        decoded = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        result["json_error"] = str(error)
        return result
    if not isinstance(decoded, dict):
        result["json_error"] = "state JSON is not an object"
        return result
    result["version"] = decoded.get("version")
    result["phase"] = decoded.get("phase")
    result["cycle"] = decoded.get("cycle")
    contract = decoded.get("contract")
    result["contract_revision"] = contract.get("revision") if isinstance(contract, dict) else None
    return result


def snapshot_workspace(workspace: Path) -> dict[str, Any]:
    """Record entries without following symlinks or inspecting their targets."""
    entries: dict[str, Any] = {}
    for path in sorted(workspace.rglob("*")):
        relative = str(path.relative_to(workspace))
        stat_result = path.lstat()
        if path.is_symlink():
            entries[relative] = {
                "kind": "symlink",
                "target": os.readlink(path),
                "mode": oct(stat_result.st_mode & 0o777),
            }
        elif path.is_dir():
            entries[relative] = {"kind": "directory", "mode": oct(stat_result.st_mode & 0o777)}
        elif path.is_file():
            entries[relative] = {
                "kind": "file",
                "bytes": stat_result.st_size,
                "sha256": digest(path),
                "nlink": stat_result.st_nlink,
                "mode": oct(stat_result.st_mode & 0o777),
            }
        else:
            entries[relative] = {"kind": "other", "mode": oct(stat_result.st_mode & 0o777)}
    return entries


def snapshot_state(workspace: Path, destination: Path) -> dict[str, Any]:
    state_path = workspace / ".until-loop" / "state.json"
    try:
        stat_result = state_path.lstat()
    except FileNotFoundError:
        return {"present": False}
    if state_path.is_symlink() or not state_path.is_file() or stat_result.st_nlink != 1:
        return {
            "present": True,
            "safe_regular_single_link": False,
            "mode": oct(stat_result.st_mode & 0o777),
            "nlink": stat_result.st_nlink,
        }
    raw = state_path.read_bytes()
    destination.write_bytes(raw)
    return state_projection(raw)


def prompt_for(request: str, workspace: Path, *, fresh_next: bool) -> str:
    continuation = ""
    if fresh_next:
        continuation = (
            "\nThis is a fresh host context. Resume the persisted loop by following "
            "`/until-loop next`; do not edit runtime state manually.\n"
        )
    return (
        "Use the candidate until-loop skill at "
        f"{SKILL_PATH} for this task. Its bound workspace is {workspace}. "
        "Read the skill and follow its protocol. Work only in that workspace; "
        "do not access or modify another project, and do not use network or external actions.\n"
        f"{continuation}\n"
        "Raw user request:\n"
        f"{request}\n"
    )


def terminate_process(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    try:
        if os.name != "nt":
            os.killpg(process.pid, signal.SIGTERM)
        else:
            process.terminate()
        process.wait(timeout=10)
        return
    except (OSError, subprocess.TimeoutExpired):
        pass
    try:
        if os.name != "nt":
            os.killpg(process.pid, signal.SIGKILL)
        else:
            process.kill()
    except OSError:
        process.kill()
    process.wait(timeout=10)


def usage_projection(events_path: Path) -> dict[str, Any]:
    """Keep raw events authoritative while extracting common usage-shaped data."""
    counts = {"lines": 0, "json_lines": 0}
    event_types: list[str] = []
    usage_values: list[Any] = []
    for line in events_path.read_text(encoding="utf-8", errors="replace").splitlines():
        counts["lines"] += 1
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        counts["json_lines"] += 1
        if isinstance(record, dict):
            event_type = record.get("type")
            if isinstance(event_type, str):
                event_types.append(event_type)
            stack = [record]
            while stack:
                item = stack.pop()
                if isinstance(item, dict):
                    for key, value in item.items():
                        if key in {"usage", "token_usage"}:
                            usage_values.append(value)
                        elif isinstance(value, (dict, list)):
                            stack.append(value)
                elif isinstance(item, list):
                    stack.extend(item)
    return {
        **counts,
        "event_types": event_types,
        "usage_fields": usage_values,
        "note": "Raw events.jsonl is authoritative; this is a best-effort projection.",
    }


def run_host(
    *,
    case_id: str,
    workspace: Path,
    request: str,
    run_dir: Path,
    fresh_next: bool,
    timeout_seconds: int,
) -> dict[str, Any]:
    if run_dir.exists():
        raise RuntimeError(f"refusing to overwrite existing run directory: {run_dir}")
    run_dir.mkdir(parents=True)
    prompt = prompt_for(request, workspace, fresh_next=fresh_next)
    write_text(run_dir / "prompt.txt", prompt)
    write_json(run_dir / "workspace-before.json", snapshot_workspace(workspace))
    before_state = snapshot_state(workspace, run_dir / "state-before.json")
    write_json(run_dir / "state-before-meta.json", before_state)

    events_path = run_dir / "events.jsonl"
    stderr_path = run_dir / "stderr.txt"
    final_path = run_dir / "final.md"
    argv = [
        "codex",
        "exec",
        "--ephemeral",
        "--json",
        "--color",
        "never",
        "--skip-git-repo-check",
        "--sandbox",
        "workspace-write",
        "--cd",
        str(workspace),
        "--output-last-message",
        str(final_path),
        prompt,
    ]
    started_utc = utc_now()
    started = time.monotonic()
    timed_out = False
    with events_path.open("wb") as stdout_handle, stderr_path.open("wb") as stderr_handle:
        process = subprocess.Popen(
            argv,
            cwd=workspace,
            stdout=stdout_handle,
            stderr=stderr_handle,
            start_new_session=True,
        )
        try:
            returncode = process.wait(timeout=timeout_seconds)
        except subprocess.TimeoutExpired:
            timed_out = True
            terminate_process(process)
            returncode = process.returncode
    elapsed = round(time.monotonic() - started, 3)
    after_state = snapshot_state(workspace, run_dir / "state-after.json")
    write_json(run_dir / "state-after-meta.json", after_state)
    write_json(run_dir / "workspace-after.json", snapshot_workspace(workspace))
    write_json(run_dir / "usage.json", usage_projection(events_path))
    result = {
        "case_id": case_id,
        "fresh_next": fresh_next,
        "workspace": str(workspace),
        "skill_path": str(SKILL_PATH),
        "argv": argv[:-1] + ["<recorded in prompt.txt>"],
        "started_utc": started_utc,
        "ended_utc": utc_now(),
        "timeout_seconds": timeout_seconds,
        "wall_time_seconds": elapsed,
        "returncode": returncode,
        "timed_out": timed_out,
        "final_message_present": final_path.is_file(),
        "state_before": before_state,
        "state_after": after_state,
    }
    write_json(run_dir / "run.json", result)
    return result


def load_cases(manifest_path: Path) -> dict[str, dict[str, str]]:
    raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise ValueError("live manifest must be a JSON array")
    cases: dict[str, dict[str, str]] = {}
    for item in raw:
        if not isinstance(item, dict):
            raise ValueError("live manifest items must be objects")
        case_id = item.get("id")
        repo = item.get("repo")
        request = item.get("request")
        if not all(isinstance(value, str) and value for value in (case_id, repo, request)):
            raise ValueError(f"invalid live manifest item: {item!r}")
        if case_id in cases:
            raise ValueError(f"duplicate live case id: {case_id}")
        cases[case_id] = {"repo": repo, "request": request}
    expected = NORMAL_CASE_IDS | {COLD_CASE_ID}
    if set(cases) != expected:
        raise ValueError(f"unexpected live case ids: {sorted(cases)}")
    return cases


def safe_fixture_path(value: str) -> Path:
    candidate = Path(value)
    if candidate.is_absolute() or not candidate.parts or ".." in candidate.parts:
        raise ValueError(f"unsafe fixture path: {value!r}")
    return candidate


def prepare_root(prepare_root_path: Path) -> Path:
    """Create uninitialized selected fixtures and a manifest from packaged input."""
    if prepare_root_path.exists():
        raise RuntimeError(f"refusing to overwrite existing preparation root: {prepare_root_path}")
    raw = json.loads(INTENT_CASES_PATH.read_text(encoding="utf-8"))
    if raw.get("format") != "until-loop-host-intent-evals/v1":
        raise ValueError("unsupported packaged intent-case format")
    all_cases = {case.get("id"): case for case in raw.get("cases", []) if isinstance(case, dict)}
    expected = NORMAL_CASE_IDS | {COLD_CASE_ID}
    if set(all_cases).isdisjoint(expected) or not expected.issubset(all_cases):
        raise ValueError("packaged intent cases do not contain the selected live cases")

    prepare_root_path.mkdir(parents=True)
    workspaces_root = prepare_root_path / "workspaces"
    manifest: list[dict[str, str]] = []
    for case_id in sorted(expected):
        case = all_cases[case_id]
        request = case.get("request")
        files = case.get("files")
        if not isinstance(request, str) or not request or not isinstance(files, dict):
            raise ValueError(f"invalid packaged case: {case_id}")
        workspace = workspaces_root / case_id
        workspace.mkdir(parents=True)
        for name, content in files.items():
            if not isinstance(name, str) or not isinstance(content, str):
                raise ValueError(f"invalid file record in packaged case: {case_id}")
            target = workspace / safe_fixture_path(name)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        manifest.append({"id": case_id, "repo": str(workspace.resolve()), "request": request})
    manifest_path = prepare_root_path / "live-manifest.json"
    write_json(manifest_path, manifest)
    write_text(
        prepare_root_path / "PREPARED.md",
        "This root was created from tests/intent-cases.json. It contains only raw product "
        "fixtures and no .until-loop state. Run live_candidate.py with live-manifest.json "
        "and a separate results root.\n",
    )
    return manifest_path


def projection_from_saved_state(path: Path, prior: Any) -> dict[str, Any]:
    if not path.exists():
        return {"present": False} if isinstance(prior, dict) and not prior.get("present") else {
            "present": False,
            "observer_note": "saved state snapshot is absent",
        }
    stat_result = path.lstat()
    if path.is_symlink() or not path.is_file() or stat_result.st_nlink != 1:
        return {
            "present": True,
            "safe_regular_single_link": False,
            "mode": oct(stat_result.st_mode & 0o777),
            "nlink": stat_result.st_nlink,
        }
    return state_projection(path.read_bytes())


def refresh_observer_metadata(results_root: Path) -> int:
    """Repair derived state projections from retained snapshots, not live workspaces."""
    if not results_root.is_dir():
        raise RuntimeError(f"results root does not exist: {results_root}")
    refreshed: list[str] = []
    records: dict[tuple[str, str], dict[str, Any]] = {}
    for run_path in sorted(results_root.glob("*/*/run.json")):
        run_dir = run_path.parent
        record = json.loads(run_path.read_text(encoding="utf-8"))
        record["state_before"] = projection_from_saved_state(
            run_dir / "state-before.json", record.get("state_before")
        )
        record["state_after"] = projection_from_saved_state(
            run_dir / "state-after.json", record.get("state_after")
        )
        write_json(run_path, record)
        write_json(run_dir / "state-before-meta.json", record["state_before"])
        write_json(run_dir / "state-after-meta.json", record["state_after"])
        refreshed.append(str(run_path.relative_to(results_root)))
        records[(str(record.get("case_id")), run_dir.name)] = record

    normal = [
        records[(case_id, "single")]
        for case_id in sorted(NORMAL_CASE_IDS)
        if (case_id, "single") in records
    ]
    cold = [
        records[(COLD_CASE_ID, phase)]
        for phase in ("initial", "fresh-next")
        if (COLD_CASE_ID, phase) in records
    ]
    summary_path = results_root / "summary.json"
    if summary_path.is_file():
        old_summary = json.loads(summary_path.read_text(encoding="utf-8"))
        write_json(
            summary_path,
            {
                "normal": normal,
                "cold_resume": cold,
                "note": old_summary.get(
                    "note", "Raw transcripts and snapshots are retained per run; this harness does not decide semantic correctness."
                ),
            },
        )
    variant_key = (BOOKKEEPING_VARIANT_ID, "single")
    variant_summary_path = results_root / "bookkeeping-variant-summary.json"
    if variant_key in records and variant_summary_path.is_file():
        old_variant = json.loads(variant_summary_path.read_text(encoding="utf-8"))
        old_variant["result"] = records[variant_key]
        write_json(variant_summary_path, old_variant)
    write_json(
        results_root / "observer-metadata-refresh.json",
        {
            "refreshed_utc": utc_now(),
            "runs": refreshed,
            "correction": "contract_revision is projected from state.contract.revision; raw events, final messages, and saved state snapshots were not changed.",
        },
    )
    return 0


def create_bookkeeping_variant(source_workspace: Path, variant_workspace: Path) -> None:
    """Make a fresh product fixture without copying runtime state or artifacts."""
    if variant_workspace.exists():
        raise RuntimeError(f"refusing to overwrite variant workspace: {variant_workspace}")
    variant_workspace.mkdir(parents=True)
    for name in ("settings.json", "CHANGELOG.md"):
        source = source_workspace / name
        stat_result = source.lstat()
        if source.is_symlink() or not source.is_file() or stat_result.st_nlink != 1:
            raise RuntimeError(f"unsafe variant source file: {source}")
        shutil.copyfile(source, variant_workspace / name, follow_symlinks=False)


def run_bookkeeping_variant(
    cases: dict[str, dict[str, str]], results_root: Path, timeout_seconds: int
) -> int:
    source_workspace = Path(cases["prechecked-while-until"]["repo"])
    variant_workspace = source_workspace.parent / BOOKKEEPING_VARIANT_ID
    create_bookkeeping_variant(source_workspace, variant_workspace)
    result = run_host(
        case_id=BOOKKEEPING_VARIANT_ID,
        workspace=variant_workspace,
        request=BOOKKEEPING_VARIANT_REQUEST,
        run_dir=results_root / BOOKKEEPING_VARIANT_ID / "single",
        fresh_next=False,
        timeout_seconds=timeout_seconds,
    )
    write_json(
        results_root / "bookkeeping-variant-summary.json",
        {
            "case_id": BOOKKEEPING_VARIANT_ID,
            "source_workspace": str(source_workspace),
            "workspace": str(variant_workspace),
            "result": result,
            "note": "The host received the raw request with explicit permission for runtime bookkeeping, not evaluator criteria or a target verdict.",
        },
    )
    return 1 if result.get("returncode") != 0 or result.get("timed_out") else 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        epilog=(
            "Prepare fresh fixtures first: PYTHONDONTWRITEBYTECODE=1 python3 "
            "tests/live_candidate.py --prepare-root /tmp/until-loop-v2-live. Then run with "
            "--manifest /tmp/until-loop-v2-live/live-manifest.json --results-root "
            "/tmp/until-loop-v2-live-results."
        ),
    )
    parser.add_argument("--manifest", type=Path, help="manifest produced by --prepare-root")
    parser.add_argument("--results-root", type=Path, help="new root for transcripts and snapshots")
    parser.add_argument("--timeout-seconds", type=int, default=TIMEOUT_SECONDS)
    parser.add_argument(
        "--prepare-root",
        type=Path,
        help="create uninitialized selected fixtures and live-manifest.json from tests/intent-cases.json",
    )
    parser.add_argument(
        "--refresh-observer-metadata",
        action="store_true",
        help="regenerate derived run state metadata from retained state snapshots",
    )
    parser.add_argument(
        "--bookkeeping-variant",
        action="store_true",
        help="run the separate already-satisfied fixture with bookkeeping explicitly permitted",
    )
    args = parser.parse_args()
    if args.timeout_seconds <= 0 or args.timeout_seconds > TIMEOUT_SECONDS:
        parser.error(f"--timeout-seconds must be between 1 and {TIMEOUT_SECONDS}")
    if args.prepare_root:
        if args.manifest or args.results_root or args.bookkeeping_variant or args.refresh_observer_metadata:
            parser.error("--prepare-root cannot be combined with run or metadata options")
        try:
            print(prepare_root(args.prepare_root))
        except (OSError, ValueError, RuntimeError) as error:
            parser.error(str(error))
        return 0
    if args.refresh_observer_metadata:
        if args.manifest or args.bookkeeping_variant or not args.results_root:
            parser.error("--refresh-observer-metadata requires only --results-root")
        try:
            return refresh_observer_metadata(args.results_root)
        except (OSError, ValueError, RuntimeError) as error:
            parser.error(str(error))
    if not args.manifest or not args.results_root:
        parser.error("--manifest and --results-root are required; use --prepare-root for fresh fixtures")
    if not SKILL_PATH.is_file():
        parser.error(f"candidate skill is missing: {SKILL_PATH}")

    cases = load_cases(args.manifest)
    results_root = args.results_root
    if args.bookkeeping_variant:
        if not results_root.is_dir():
            parser.error(f"results root must already exist for the variant: {results_root}")
        return run_bookkeeping_variant(cases, results_root, args.timeout_seconds)
    if results_root.exists():
        parser.error(f"refusing to overwrite existing results root: {results_root}")
    results_root.mkdir(parents=True)
    write_json(
        results_root / "run-manifest.json",
        {
            "created_utc": utc_now(),
            "manifest": str(args.manifest),
            "candidate_skill_sha256": digest(SKILL_PATH),
            "timeout_seconds": args.timeout_seconds,
            "normal_parallelism": 2,
            "note": "No evaluator expectations or expected verdicts were supplied to hosts.",
        },
    )

    normal_results: list[dict[str, Any]] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        futures = {
            executor.submit(
                run_host,
                case_id=case_id,
                workspace=Path(cases[case_id]["repo"]),
                request=cases[case_id]["request"],
                run_dir=results_root / case_id / "single",
                fresh_next=False,
                timeout_seconds=args.timeout_seconds,
            ): case_id
            for case_id in sorted(NORMAL_CASE_IDS)
        }
        for future in concurrent.futures.as_completed(futures):
            case_id = futures[future]
            try:
                normal_results.append(future.result())
            except Exception as error:  # Preserve other hosts' evidence before reporting this one.
                normal_results.append({"case_id": case_id, "harness_error": repr(error)})

    cold = cases[COLD_CASE_ID]
    cold_results: list[dict[str, Any]] = []
    for phase, fresh_next in (("initial", False), ("fresh-next", True)):
        try:
            cold_results.append(
                run_host(
                    case_id=COLD_CASE_ID,
                    workspace=Path(cold["repo"]),
                    request=cold["request"],
                    run_dir=results_root / COLD_CASE_ID / phase,
                    fresh_next=fresh_next,
                    timeout_seconds=args.timeout_seconds,
                )
            )
        except Exception as error:
            cold_results.append({"phase": phase, "harness_error": repr(error)})

    summary = {
        "normal": sorted(normal_results, key=lambda result: result["case_id"]),
        "cold_resume": cold_results,
        "note": "Raw transcripts and snapshots are retained per run; this harness does not decide semantic correctness.",
    }
    write_json(results_root / "summary.json", summary)
    failed = any(
        result.get("returncode") != 0 or result.get("timed_out") or result.get("harness_error")
        for result in normal_results + cold_results
    )
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
