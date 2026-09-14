#!/usr/bin/env python3
"""Read-only outcome and within-run comparison for frozen Improve trials.

This never invokes a model, executes candidate tests, repairs a candidate, or
changes a judgment. It compares retained evidence and reports missing evidence.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any


# These are the documented top-level token counters emitted in retained Codex
# turn receipts.  Timing, identifiers, and provider-specific metadata are not
# token usage and must not be summed as though they were.
TOKEN_USAGE_KEYS = frozenset((
    "input_tokens",
    "cached_input_tokens",
    "cache_write_input_tokens",
    "output_tokens",
    "reasoning_output_tokens",
))


def population(case_id: str | None, mode: str) -> str:
    if mode == "controlled_resume":
        return "controlled_resume"
    if case_id in ("clean_control", "decline_suggestion"):
        return "correct_control"
    if case_id == "invalid_test":
        return "test_repair"
    if case_id in ("blank_fallback", "csv_contract", "tenant_cache", "commit_retry", "weak_tests"):
        return "behavior_repair"
    return "unclassified"


def read_json(path: Path, default: Any = None) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text())


def assertion_values(oracle: dict[str, Any]) -> dict[str, bool | None]:
    values = {}
    for name, record in oracle.get("assertions", {}).items():
        value = record.get("passed") if isinstance(record, dict) else record
        values[name] = value if type(value) is bool else None
    return values


def compare_oracles(first: dict[str, Any], final: dict[str, Any]) -> dict[str, Any]:
    before, after = assertion_values(first), assertion_values(final)
    names = sorted(set(before) | set(after))
    unknown = [name for name in names if before.get(name) is None or after.get(name) is None]
    return {
        "comparable": bool(names) and not unknown,
        "newly_passing": [name for name in names if before.get(name) is False and after.get(name) is True],
        "regressed": [name for name in names if before.get(name) is True and after.get(name) is False],
        "unknown": unknown,
        "first_passed": first.get("passed"),
        "final_passed": final.get("passed"),
    }


def unavailable_comparison(reasons: list[str]) -> dict[str, Any]:
    """Represent a missing bound comparison without treating it as a verdict."""
    return {
        "comparable": False,
        "newly_passing": [],
        "regressed": [],
        "unknown": [],
        "first_passed": None,
        "final_passed": None,
        "incomplete_reasons": reasons,
    }


def bound_snapshot_comparison(sequence: dict[str, Any], first: dict[str, Any] | None,
                              snapshots: dict[str, dict[str, Any]],
                              oracles: dict[str, Any],
                              selected_snapshot_catalog: bool) -> dict[str, Any]:
    """Compare only stable snapshot-oracles bound by the selected grade.

    A base ``final-oracle.json`` is a useful retained artifact but is not a
    replacement for a final snapshot oracle keyed to the selected audit's
    final snapshot.  This makes stale base results unable to create a
    first-to-final comparison after an audit selection changes.
    """
    reasons: list[str] = []
    first_id = first.get("snapshot_id") if isinstance(first, dict) else None
    final_id = sequence.get("final_observed_snapshot_id")
    first_snapshot = snapshots.get(first_id) if isinstance(first_id, str) else None
    final_snapshot = snapshots.get(final_id) if isinstance(final_id, str) else None

    if not selected_snapshot_catalog:
        reasons.append("selected grade's observed snapshot catalog is unavailable")

    if not isinstance(first_id, str) or not first_id:
        reasons.append("selected grade has no completed review snapshot")
    elif not isinstance(first_snapshot, dict):
        reasons.append("selected grade's first completed review snapshot is unavailable")
    else:
        if first_snapshot.get("stable") is not True:
            reasons.append("selected grade's first completed review snapshot is not stable")
        if first.get("snapshot_stable") is not True:
            reasons.append("selected grade does not bind its first completed review to a stable snapshot")
        first_digest = first.get("candidate_digest")
        if not isinstance(first_digest, str) or not first_digest:
            reasons.append("selected grade has no first completed review candidate digest")
        elif first_snapshot.get("candidate_digest") != first_digest:
            reasons.append("selected grade's first completed review digest does not match its snapshot")

    if not isinstance(final_id, str) or not final_id:
        reasons.append("selected grade has no final observed snapshot")
    elif not isinstance(final_snapshot, dict):
        reasons.append("selected grade's final observed snapshot is unavailable")
    else:
        if final_snapshot.get("stable") is not True:
            reasons.append("selected grade's final observed snapshot is not stable")
        if sequence.get("final_observed_snapshot_stable") is not True:
            reasons.append("selected grade does not bind its final observed snapshot as stable")
        final_digest = sequence.get("final_observed_candidate_digest")
        if not isinstance(final_digest, str) or not final_digest:
            reasons.append("selected grade has no final observed candidate digest")
        elif final_snapshot.get("candidate_digest") != final_digest:
            reasons.append("selected grade's final observed digest does not match its snapshot")
        if sequence.get("final_candidate_bound") is not True:
            reasons.append("selected grade does not bind its final candidate")

    first_oracle = oracles.get(first_id) if isinstance(first_id, str) else None
    final_oracle = oracles.get(final_id) if isinstance(final_id, str) else None
    if not isinstance(first_oracle, dict):
        reasons.append("first completed review snapshot oracle is unavailable")
    if not isinstance(final_oracle, dict):
        reasons.append("final observed snapshot oracle is unavailable")
    if reasons:
        return unavailable_comparison(reasons)

    comparison = compare_oracles(first_oracle, final_oracle)
    comparison["incomplete_reasons"] = ([] if comparison["comparable"] else
                                        ["stable snapshot oracle assertions are incomplete or unmatched"])
    return comparison


def token_usage(evidence: Path) -> dict[str, Any]:
    """Only aggregate observed top-level turn receipts, not inferred child costs."""
    totals: dict[str, int] = {}
    ignored_keys: set[str] = set()
    receipts = 0
    path = evidence / "events.jsonl"
    if path.exists():
        for line in path.read_text().splitlines():
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if not isinstance(event, dict) or event.get("type") != "turn.completed":
                continue
            usage = event.get("usage")
            if not isinstance(usage, dict):
                continue
            receipts += 1
            for key, value in usage.items():
                if key in TOKEN_USAGE_KEYS and type(value) is int and value >= 0:
                    totals[key] = totals.get(key, 0) + value
                elif isinstance(key, str):
                    ignored_keys.add(key)
    return {"observed_receipts": receipts, "totals": totals,
            "ignored_non_token_usage_keys": sorted(ignored_keys),
            "boundary": "Recorded primary-host turn usage only for documented token fields; child usage may be absent. Cached tokens are not new input or monetary cost."}


def analyze_trial(root: Path) -> dict[str, Any]:
    original = read_json(root / "trial.json", {})
    if not original:
        return {"trial": root.name, "status": "not_prepared", "execution_mode": "unknown"}
    mode = original.get("execution_mode", "autonomous")
    selected_root = root
    if mode == "controlled_resume":
        combined = root / "controlled-resume-combined"
        if (combined / "trial.json").exists():
            selected_root = combined
    manifest = read_json(selected_root / "trial.json")
    evidence = Path(manifest["evidence"])
    observed = read_json(evidence / "observed.json", {})
    selection = read_json(evidence / "audit-selection.json", {"directory": "audit"})
    audit = evidence / selection["directory"]
    grade_path = audit / selection.get("grade_file", "grade.json")
    grade = read_json(grade_path, {})
    if not isinstance(grade, dict):
        grade = {}
    judgment = read_json(audit / "judgment.json", {})
    # Audit observations include the contemporaneous mechanical rechecks.
    selected_observed_path = grade_path.parent / "observed.json"
    selected_audited = read_json(selected_observed_path, None)
    selected_snapshot_catalog = isinstance(selected_audited, dict)
    audited = selected_audited
    if audited is None:
        audited = read_json(audit / "observed.json", observed)
    snapshot_rows = audited.get("snapshots", []) if isinstance(audited, dict) else []
    if not isinstance(snapshot_rows, list):
        snapshot_rows = []
    snapshots = {s["id"]: s for s in snapshot_rows if isinstance(s, dict) and isinstance(s.get("id"), str)}
    snapshot_order = [s["id"] for s in snapshot_rows if isinstance(s, dict) and isinstance(s.get("id"), str)]
    sequence = grade.get("sequence", {}) if isinstance(grade.get("sequence", {}), dict) else {}
    states = sequence.get("states", []) if isinstance(sequence.get("states", []), list) else []
    first = next((s for s in states if isinstance(s, dict) and s.get("completed") is True), None)
    first_id = first.get("snapshot_id") if first else None
    first_snapshot = snapshots.get(first_id) if isinstance(first_id, str) else None
    final_id = sequence.get("final_observed_snapshot_id")
    final_snapshot = snapshots.get(final_id) if isinstance(final_id, str) else None
    oracles = read_json(evidence / "snapshot-oracles.json", {})
    if not isinstance(oracles, dict):
        oracles = {}
    comparison = bound_snapshot_comparison(sequence, first, snapshots, oracles, selected_snapshot_catalog)
    comparison.update({
        "first_completed_review": first.get("review_id") if first else None,
        "first_snapshot_id": first_id,
        "final_snapshot_id": final_id,
        "oracle_sources": {
            "first": "snapshot-oracles:%s" % first_id if isinstance(oracles.get(first_id), dict) else None,
            "final": "snapshot-oracles:%s" % final_id if isinstance(oracles.get(final_id), dict) else None,
        },
        "source": "Stable snapshot oracle results keyed to the selected grade; this is within-run observation, not a causal baseline comparison.",
    })
    scope = manifest.get("fixture", {}).get("scope_paths", [])
    comparison["changed_scope_paths"] = None
    if comparison["comparable"] and first_snapshot and final_snapshot:
        a, b = first_snapshot.get("manifest", {}), final_snapshot.get("manifest", {})
        comparison["changed_scope_paths"] = [name for name in scope if a.get(name) != b.get(name)]
    comparison["all_observed_failing_snapshot_ids"] = sorted(
        name for name, oracle in oracles.items()
        if isinstance(name, str) and isinstance(oracle, dict) and oracle.get("passed") is False
    )
    later = (snapshot_order[snapshot_order.index(first_id) + 1:]
             if isinstance(first_id, str) and first_id in snapshot_order else [])
    post_first_failures = [name for name in later if isinstance(oracles.get(name), dict) and oracles[name].get("passed") is False]
    comparison["observed_post_first_review_failing_snapshot_ids"] = post_first_failures
    if mode == "controlled_resume":
        comparison["post_first_review_failure_cohort"] = "evaluator_intervention_controlled_resume"
        comparison["post_first_review_failure_boundary"] = (
            "These are observations from the evaluator-intervention controlled-resume cohort; "
            "they are not attributed to self-created agent regressions.")
    else:
        comparison["post_first_review_failure_cohort"] = "autonomous_observed"
        comparison["post_first_review_failure_boundary"] = (
            "These are observed later snapshots in the autonomous cohort; this list alone does not establish cause.")
    errors = grade.get("errors", []) + grade.get("fail_reasons", []) + grade.get("incomplete_reasons", [])
    status = grade.get("status")
    if status is None:
        status = "incomplete" if observed.get("trial_status") == "incomplete" else "not_audited"
    record = {
        "trial": root.name, "case_id": original.get("case_id"), "execution_mode": mode,
        "population": population(original.get("case_id"), mode),
        "status": status, "evidence": str(evidence), "reasons": errors,
        "review_count": sequence.get("completed_review_count"),
        "sequence": [s.get("classification") for s in states],
        "streaks": [s.get("streak_after") for s in states],
        "outcome": grade.get("outcome", {}), "judgment": judgment,
        "comparison": comparison, "usage": token_usage(evidence),
        "elapsed_seconds": observed.get("elapsed_seconds"),
        "evidence_sha256": {},
    }
    if mode == "controlled_resume":
        phases = {
            "initial": read_json(Path(original["evidence"]) / "observed.json", {}).get("elapsed_seconds"),
            "continuation": read_json(root / "controlled-resume-continuation/evidence/observed.json", {}).get("elapsed_seconds"),
        }
        record["elapsed_phases"] = phases
        record["elapsed_seconds"] = sum(phases.values()) if all(type(v) in (int, float) for v in phases.values()) else None
    for path in (root / "trial.json", evidence / "observed.json", selected_observed_path, evidence / "events.jsonl", grade_path,
                 audit / "judgment.json", evidence / "snapshot-oracles.json", evidence / "final-oracle.json"):
        if path.is_file():
            record["evidence_sha256"][str(path)] = hashlib.sha256(path.read_bytes()).hexdigest()
    return record


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    cohorts: dict[str, Any] = {}
    for mode in sorted({r["execution_mode"] for r in rows}):
        selected = [r for r in rows if r["execution_mode"] == mode]
        cases = {}
        for case in sorted({str(r.get("case_id")) for r in selected}):
            matching = [r for r in selected if str(r.get("case_id")) == case]
            cases[case] = {"count": len(matching), "statuses": [r["status"] for r in matching],
                           "review_counts": [r.get("review_count") for r in matching]}
        populations = {}
        for kind in sorted({population(r.get("case_id"), mode) for r in selected}):
            group = [r for r in selected if population(r.get("case_id"), mode) == kind]
            comparable = [r for r in group if r.get("comparison", {}).get("comparable") is True]
            populations[kind] = {"count": len(group),
                "statuses": {status: sum(r["status"] == status for r in group) for status in sorted({r["status"] for r in group})},
                "comparable_first_to_final": len(comparable),
                "runs_with_newly_passing_assertions_after_first_review": sum(bool(r.get("comparison", {}).get("newly_passing")) for r in comparable),
                "runs_with_regressed_assertions_after_first_review": sum(bool(r.get("comparison", {}).get("regressed")) for r in comparable)}
        cohorts[mode] = {"count": len(selected), "by_case": cases, "by_population": populations,
                         "statuses": {status: sum(r["status"] == status for r in selected)
                                      for status in sorted({r["status"] for r in selected})}}
    return {"format": "improve-repeat-analysis/v1", "cohorts": cohorts, "trials": rows,
            "boundary": "All discovered trial roots retained; queued unprepared roots require the batch schedule. Cohorts are separate. No causal or production reliability claim."}


def read_dispatch_receipts(root: Path) -> tuple[dict[str, list[dict[str, Any]]], list[str], str | None]:
    """Read the append-only coordinator log without turning it into a grade."""
    path = root / "attempts.jsonl"
    if not path.is_file():
        return {}, [], None
    raw = path.read_bytes()
    by_trial: dict[str, list[dict[str, Any]]] = {}
    errors: list[str] = []
    for number, line in enumerate(raw.decode("utf-8", errors="replace").splitlines(), 1):
        try:
            receipt = json.loads(line)
        except ValueError:
            errors.append("invalid dispatch receipt at line %d" % number)
            continue
        if not isinstance(receipt, dict) or not isinstance(receipt.get("id"), str):
            errors.append("invalid dispatch receipt at line %d" % number)
            continue
        by_trial.setdefault(receipt["id"], []).append(receipt)
    return by_trial, errors, hashlib.sha256(raw).hexdigest()


def coordinator_incomplete_receipt(receipts: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Return the terminal dispatch failure, without calling it a semantic verdict."""
    finished = [receipt for receipt in receipts if receipt.get("kind") == "finished"]
    if not finished:
        return None
    terminal = finished[-1]
    return terminal if terminal.get("status") == "incomplete" else None


def dispatch_reason(receipt: dict[str, Any]) -> str:
    stage = receipt.get("process_failure")
    if isinstance(stage, str) and stage:
        return "coordinator terminal incomplete during " + stage
    return "coordinator terminal incomplete without an audit verdict"


def analyze_batch(root: Path) -> dict[str, Any]:
    manifest = read_json(root / "batch.json")
    receipts_by_trial, receipt_errors, attempts_digest = read_dispatch_receipts(root)
    rows = []
    for entry in manifest["schedule"]:
        row = analyze_trial(root / entry["root"])
        # The immutable schedule owns identity and cohort, even before a
        # controlled wrapper has prepared its root.
        row.update(trial=entry["id"], case_id=entry["case_id"], execution_mode=entry["execution_mode"],
                   repetition=entry["repeat"], population=population(entry["case_id"], entry["execution_mode"]))
        receipts = receipts_by_trial.get(entry["id"], [])
        row["dispatch_receipts"] = receipts
        row["evidence_status"] = row["status"]
        terminal = coordinator_incomplete_receipt(receipts)
        row["coordinator_terminal_failure"] = terminal
        if terminal is not None and row["evidence_status"] in ("not_prepared", "not_audited"):
            row["status"] = "incomplete"
            row["reasons"] = list(row.get("reasons", [])) + [dispatch_reason(terminal)]
            row["status_boundary"] = (
                "Incomplete comes from a coordinator dispatch receipt; no semantic audit verdict was available.")
        rows.append(row)
    result = summarize(rows)
    result["boundary"] = "All scheduled trials retained, including unprepared and unaudited roots. Cohorts are separate. Within-run comparisons do not establish causal or production reliability."
    result["batch_manifest_sha256"] = hashlib.sha256((root / "batch.json").read_bytes()).hexdigest()
    result["attempts_sha256"] = attempts_digest
    result["attempt_log_errors"] = receipt_errors
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--trials", type=Path, help="Directory containing direct trial roots")
    source.add_argument("--batch", type=Path, help="Batch root with complete immutable schedule")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = analyze_batch(args.batch) if args.batch else summarize(
        [analyze_trial(p.parent) for p in sorted(args.trials.glob("*/trial.json"))])
    # A new analysis artifact must not overwrite an original judgment or a
    # previous report. Choose a new output path for each observation.
    with args.output.open("x") as handle:
        handle.write(json.dumps(result, indent=2, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
