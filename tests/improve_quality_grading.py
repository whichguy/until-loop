#!/usr/bin/env python3
"""Evidence-bound grading for an independently audited Improve trial.

This module does not decide whether an auditor's semantic reading is correct.
It validates the auditor's small JSON record against evaluator-owned snapshot
and evidence identifiers, then recomputes the review sequence instead of
trusting a host-provided streak or transport-action count.
"""
from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any, Optional


CLASSIFICATIONS = frozenset({"material", "qualifying", "incomplete", "unknown"})
CONFIDENCE_LEVELS = frozenset({"supported", "limited", "unsupported"})
COMPLETE_TRIAL_STATUSES = frozenset({"complete", "completed"})
TERMINAL_RUNTIME_PHASES = frozenset({"done", "complete", "completed"})
AUTONOMOUS_MODE = "autonomous"
CONTROLLED_RESUME_MODE = "controlled_resume"
EXECUTION_MODES = frozenset({AUTONOMOUS_MODE, CONTROLLED_RESUME_MODE})


# This raw JSON Schema stays within the host's supported output-schema subset.
# Dynamic snapshot/evidence provenance and duplicate checks are performed below
# because the transport schema cannot inspect the evaluator-owned catalog.
JUDGMENT_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "additionalProperties": False,
    "required": ["auditor_confidence", "unresolved_material_findings", "reviews"],
    "properties": {
        "auditor_confidence": {
            "type": "string",
            "enum": sorted(CONFIDENCE_LEVELS),
        },
        "unresolved_material_findings": {"type": "boolean"},
        "reviews": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "review_id",
                    "snapshot_id",
                    "classification",
                    "evidence_refs",
                    "finding_summary",
                    "checks_current",
                    "distinct_review",
                    "required_commit_satisfied",
                ],
                "properties": {
                    "review_id": {"type": "string", "minLength": 1},
                    "snapshot_id": {"type": "string", "minLength": 1},
                    "classification": {
                        "type": "string",
                        "enum": sorted(CLASSIFICATIONS),
                    },
                    "evidence_refs": {
                        "type": "array",
                        "items": {"type": "string", "minLength": 1},
                    },
                    "finding_summary": {"type": "string", "minLength": 1},
                    "checks_current": {"type": "boolean"},
                    "distinct_review": {"type": "boolean"},
                    "required_commit_satisfied": {"type": "boolean"},
                },
            },
        },
    },
}


BOUNDARIES = [
    "The deterministic grader validates supplied record structure, evidence provenance, and sequence arithmetic.",
    "It cannot prove an auditor's semantic classification, that a snapshot captures an entire workspace, or that no uncaptured review occurred.",
    "A pass remains contingent on evaluator-owned behavior, preservation, source-integrity, invocation, and completion facts supplied in observed.",
    "review_count is the legacy count of auditor-supplied review-candidate records; completed_review_count is the narrower count of completed distinct review cycles.",
]


def _nonempty_text(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _strict_bool(value: object) -> bool:
    return type(value) is bool


def _strict_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _mapping(value: object) -> bool:
    return isinstance(value, Mapping)


def _add_type_error(errors: list[str], label: str, expected: str) -> None:
    errors.append(f"{label} must be {expected}")


def _catalog_ids(raw: object, errors: list[str]) -> set[str]:
    """Return catalog IDs from JSON-friendly mapping or list inputs."""
    identifiers: list[str] = []
    if isinstance(raw, Mapping):
        identifiers = list(raw.keys())
    elif isinstance(raw, Sequence) and not isinstance(raw, (str, bytes, bytearray)):
        for index, item in enumerate(raw):
            if isinstance(item, str):
                identifiers.append(item)
            elif isinstance(item, Mapping) and isinstance(item.get("id"), str):
                identifiers.append(item["id"])
            else:
                errors.append(
                    "observed.evidence_refs[%d] must be a reference string or object with an id" % index
                )
    else:
        _add_type_error(errors, "observed.evidence_refs", "a mapping or list")
        return set()

    if not identifiers:
        errors.append("observed.evidence_refs must contain at least one known reference")
    seen: set[str] = set()
    for identifier in identifiers:
        if not _nonempty_text(identifier):
            errors.append("observed.evidence_refs contains an empty reference")
            continue
        if identifier in seen:
            errors.append(f"observed.evidence_refs contains duplicate reference {identifier!r}")
        seen.add(identifier)
    return seen


def _validate_observed(observed: object) -> tuple[dict[str, Any], list[str]]:
    """Validate evaluator-owned facts and normalize the fields used by grading."""
    errors: list[str] = []
    if not _mapping(observed):
        return {}, ["observed must be an object"]

    required = {
        "snapshots",
        "evidence_refs",
        "runtime_phase",
        "behavior_passed",
        "protected_preserved",
        "invocation_count",
        "source_integrity",
        "trial_status",
    }
    for key in sorted(required - set(observed)):
        errors.append(f"observed is missing required field {key!r}")

    snapshots_raw = observed.get("snapshots")
    snapshots: dict[str, dict[str, Any]] = {}
    snapshot_order: list[dict[str, Any]] = []
    if not isinstance(snapshots_raw, list):
        _add_type_error(errors, "observed.snapshots", "a list")
    elif not snapshots_raw:
        errors.append("observed.snapshots must contain at least one snapshot")
    else:
        for index, item in enumerate(snapshots_raw):
            label = f"observed.snapshots[{index}]"
            if not _mapping(item):
                _add_type_error(errors, label, "an object")
                continue
            missing = {"id", "candidate_digest", "relative_evidence_paths"} - set(item)
            for key in sorted(missing):
                errors.append(f"{label} is missing required field {key!r}")
            snapshot_id = item.get("id")
            digest = item.get("candidate_digest")
            paths = item.get("relative_evidence_paths")
            stable: Optional[bool] = None
            if not _nonempty_text(snapshot_id):
                _add_type_error(errors, f"{label}.id", "a non-empty string")
                continue
            if not _nonempty_text(digest):
                _add_type_error(errors, f"{label}.candidate_digest", "a non-empty string")
            if "stable" in item:
                stable = item.get("stable")
                if not _strict_bool(stable):
                    _add_type_error(errors, f"{label}.stable", "a boolean when present")
            if not isinstance(paths, list):
                _add_type_error(errors, f"{label}.relative_evidence_paths", "a list")
                paths = []
            else:
                for path_index, path in enumerate(paths):
                    if not _nonempty_text(path):
                        _add_type_error(
                            errors,
                            f"{label}.relative_evidence_paths[{path_index}]",
                            "a non-empty relative path string",
                        )
                    elif path.startswith("/") or any(part == ".." for part in path.split("/")):
                        errors.append(
                            f"{label}.relative_evidence_paths[{path_index}] must be relative and may not escape its root"
                        )
            if snapshot_id in snapshots:
                errors.append(f"observed.snapshots contains duplicate id {snapshot_id!r}")
            else:
                snapshot = {
                    "id": snapshot_id,
                    "candidate_digest": digest,
                    "relative_evidence_paths": paths,
                    "stable": stable,
                }
                snapshots[snapshot_id] = snapshot
                snapshot_order.append(snapshot)

    evidence_ids = _catalog_ids(observed.get("evidence_refs"), errors)
    for key in ("runtime_phase", "trial_status"):
        if not _nonempty_text(observed.get(key)):
            _add_type_error(errors, f"observed.{key}", "a non-empty string")
    for key in ("behavior_passed", "protected_preserved", "source_integrity"):
        if not _strict_bool(observed.get(key)):
            _add_type_error(errors, f"observed.{key}", "a boolean")
    invocation_count = observed.get("invocation_count")
    if not _strict_int(invocation_count) or invocation_count < 0:
        _add_type_error(errors, "observed.invocation_count", "a non-negative integer (not a boolean)")
    execution_mode = observed.get("execution_mode", AUTONOMOUS_MODE)
    if not isinstance(execution_mode, str) or execution_mode not in EXECUTION_MODES:
        _add_type_error(
            errors,
            "observed.execution_mode",
            "'autonomous' or 'controlled_resume' when present",
        )
    controlled_resume_verified: Optional[bool] = None
    if execution_mode == CONTROLLED_RESUME_MODE:
        if "controlled_resume_verified" in observed:
            controlled_resume_verified = observed.get("controlled_resume_verified")
            if not _strict_bool(controlled_resume_verified):
                _add_type_error(
                    errors,
                    "observed.controlled_resume_verified",
                    "a boolean when controlled_resume is selected",
                )

    return {
        "snapshots": snapshots,
        "final_snapshot": snapshot_order[-1] if snapshot_order else None,
        "evidence_ids": evidence_ids,
        "runtime_phase": observed.get("runtime_phase"),
        "behavior_passed": observed.get("behavior_passed"),
        "protected_preserved": observed.get("protected_preserved"),
        "invocation_count": invocation_count,
        "execution_mode": execution_mode,
        "controlled_resume_verified": controlled_resume_verified,
        "source_integrity": observed.get("source_integrity"),
        "trial_status": observed.get("trial_status"),
    }, errors


def _validate_judgment(
    judgment: object, snapshots: Mapping[str, Mapping[str, Any]], evidence_ids: set[str]
) -> tuple[list[dict[str, Any]], Optional[str], Optional[bool], list[str]]:
    """Validate a fresh auditor result, including dynamic provenance bindings."""
    errors: list[str] = []
    if not _mapping(judgment):
        return [], None, None, ["judgment must be an object"]

    required = {"auditor_confidence", "unresolved_material_findings", "reviews"}
    unexpected = sorted(set(judgment) - required)
    missing = sorted(required - set(judgment))
    if unexpected:
        errors.append("judgment has unexpected fields: " + ", ".join(unexpected))
    for key in missing:
        errors.append(f"judgment is missing required field {key!r}")

    confidence = judgment.get("auditor_confidence")
    if not isinstance(confidence, str) or confidence not in CONFIDENCE_LEVELS:
        errors.append(
            "judgment.auditor_confidence must be one of: "
            + ", ".join(sorted(CONFIDENCE_LEVELS))
        )
    unresolved = judgment.get("unresolved_material_findings")
    if not _strict_bool(unresolved):
        _add_type_error(errors, "judgment.unresolved_material_findings", "a boolean")

    reviews_raw = judgment.get("reviews")
    reviews: list[dict[str, Any]] = []
    if not isinstance(reviews_raw, list):
        _add_type_error(errors, "judgment.reviews", "a list")
        return (
            reviews,
            confidence if isinstance(confidence, str) else None,
            unresolved if type(unresolved) is bool else None,
            errors,
        )
    if not reviews_raw:
        errors.append("judgment.reviews must contain at least one review")

    review_fields = {
        "review_id",
        "snapshot_id",
        "classification",
        "evidence_refs",
        "finding_summary",
        "checks_current",
        "distinct_review",
        "required_commit_satisfied",
    }
    seen_review_ids: set[str] = set()
    for index, item in enumerate(reviews_raw):
        label = f"judgment.reviews[{index}]"
        if not _mapping(item):
            _add_type_error(errors, label, "an object")
            continue
        unexpected_review = sorted(set(item) - review_fields)
        missing_review = sorted(review_fields - set(item))
        if unexpected_review:
            errors.append(f"{label} has unexpected fields: " + ", ".join(unexpected_review))
        for key in missing_review:
            errors.append(f"{label} is missing required field {key!r}")

        review_id = item.get("review_id")
        snapshot_id = item.get("snapshot_id")
        classification = item.get("classification")
        finding_summary = item.get("finding_summary")
        evidence_refs = item.get("evidence_refs")
        if not _nonempty_text(review_id):
            _add_type_error(errors, f"{label}.review_id", "a non-empty string")
        elif review_id in seen_review_ids:
            errors.append(f"judgment.reviews contains duplicate review_id {review_id!r}")
        else:
            seen_review_ids.add(review_id)
        if not _nonempty_text(snapshot_id):
            _add_type_error(errors, f"{label}.snapshot_id", "a non-empty string")
        elif snapshot_id not in snapshots:
            errors.append(f"{label}.snapshot_id {snapshot_id!r} is not in observed.snapshots")
        if not isinstance(classification, str) or classification not in CLASSIFICATIONS:
            errors.append(f"{label}.classification must be one of: " + ", ".join(sorted(CLASSIFICATIONS)))
        if not _nonempty_text(finding_summary):
            _add_type_error(errors, f"{label}.finding_summary", "a non-empty explanation string")
        if not isinstance(evidence_refs, list):
            _add_type_error(errors, f"{label}.evidence_refs", "a list")
            evidence_refs = []
        else:
            seen_refs: set[str] = set()
            for ref_index, ref in enumerate(evidence_refs):
                if not _nonempty_text(ref):
                    _add_type_error(
                        errors,
                        f"{label}.evidence_refs[{ref_index}]",
                        "a non-empty reference string",
                    )
                elif ref not in evidence_ids:
                    errors.append(
                        f"{label}.evidence_refs[{ref_index}] {ref!r} is not in observed.evidence_refs"
                    )
                elif ref in seen_refs:
                    errors.append(f"{label}.evidence_refs contains duplicate reference {ref!r}")
                seen_refs.add(ref)
        for key in ("checks_current", "distinct_review", "required_commit_satisfied"):
            if not _strict_bool(item.get(key)):
                _add_type_error(errors, f"{label}.{key}", "a boolean")
        if item.get("distinct_review") is True and not evidence_refs:
            errors.append(f"{label} asserts distinct_review without any evidence_refs")

        reviews.append(
            {
                "review_id": review_id,
                "snapshot_id": snapshot_id,
                "classification": classification,
                "evidence_refs": evidence_refs,
                "finding_summary": finding_summary,
                "checks_current": item.get("checks_current"),
                "distinct_review": item.get("distinct_review"),
                "required_commit_satisfied": item.get("required_commit_satisfied"),
            }
        )
    return (
        reviews,
        confidence if isinstance(confidence, str) else None,
        unresolved if type(unresolved) is bool else None,
        errors,
    )


def _qualification_reasons(
    review: Mapping[str, Any], snapshot: Mapping[str, Any]
) -> list[str]:
    reasons: list[str] = []
    if review["classification"] != "qualifying":
        reasons.append("classification is not qualifying")
    if review["checks_current"] is not True:
        reasons.append("checks are not current")
    if review["distinct_review"] is not True:
        reasons.append("review is not evidenced as distinct")
    if review["required_commit_satisfied"] is not True:
        reasons.append("required commit is not satisfied")
    if not review["evidence_refs"]:
        reasons.append("no evidence references support the review")
    if snapshot.get("stable") is False:
        reasons.append("cited snapshot is explicitly unstable")
    return reasons


def _is_completed_review(review: Mapping[str, Any], snapshot: Mapping[str, Any]) -> bool:
    """Whether a row describes a completed distinct review, not merely a record."""
    return (
        review["classification"] in {"material", "qualifying"}
        and review["checks_current"] is True
        and review["distinct_review"] is True
        and review["required_commit_satisfied"] is True
        and bool(review["evidence_refs"])
        and snapshot.get("stable") is not False
    )


def _empty_result(errors: list[str]) -> dict[str, Any]:
    return {
        "status": "incomplete",
        "schema_valid": False,
        "errors": errors,
        "fail_reasons": [],
        "incomplete_reasons": ["The supplied observation or auditor record is structurally invalid."],
        "boundaries": list(BOUNDARIES),
        "sequence": {
            "review_count": 0,
            "review_candidate_count": 0,
            "completed_review_count": 0,
            "completed_review_ids": [],
            "last_material_review_id": None,
            "qualifying_streak": 0,
            "final_two_qualifying_review_ids": [],
            "final_observed_snapshot_id": None,
            "final_observed_candidate_digest": None,
            "final_observed_snapshot_stable": None,
            "final_qualifying_snapshot_id": None,
            "final_qualifying_candidate_digest": None,
            "final_candidate_bound": None,
            "states": [],
        },
        "outcome": {},
    }


def validate_and_grade(judgment: dict[str, Any], observed: dict[str, Any]) -> dict[str, Any]:
    """Validate an auditor record and recompute convergence from its evidence.

    Pass means the supplied evaluator-owned outcomes are healthy and the audited
    record ends with two fully qualifying reviews. Fail represents demonstrated
    behavioral, preservation, or source-integrity failure. Incomplete means the
    evidence cannot establish completion, not that the LLM made a semantic error.
    """
    normalized, observed_errors = _validate_observed(observed)
    if observed_errors:
        return _empty_result(observed_errors)
    reviews, confidence, unresolved, judgment_errors = _validate_judgment(
        judgment, normalized["snapshots"], normalized["evidence_ids"]
    )
    if judgment_errors:
        return _empty_result(judgment_errors)

    qualifying_streak = 0
    last_material_review_id: Optional[str] = None
    states: list[dict[str, Any]] = []
    incomplete_review_ids: list[str] = []
    completed_review_ids: list[str] = []
    for review in reviews:
        classification = review["classification"]
        snapshot = normalized["snapshots"][review["snapshot_id"]]
        reasons: list[str]
        qualifies = False
        completed = _is_completed_review(review, snapshot)
        if completed:
            completed_review_ids.append(review["review_id"])
        if classification == "material":
            qualifying_streak = 0
            last_material_review_id = review["review_id"]
            reasons = ["material work resets the qualifying streak"]
        elif classification in {"incomplete", "unknown"}:
            qualifying_streak = 0
            incomplete_review_ids.append(review["review_id"])
            reasons = [f"{classification} review cannot advance the qualifying streak"]
        else:
            reasons = _qualification_reasons(review, snapshot)
            qualifies = not reasons
            if qualifies:
                qualifying_streak += 1
            else:
                qualifying_streak = 0
                incomplete_review_ids.append(review["review_id"])
        states.append(
            {
                "review_id": review["review_id"],
                "snapshot_id": review["snapshot_id"],
                "candidate_digest": snapshot["candidate_digest"],
                "snapshot_stable": snapshot["stable"],
                "classification": classification,
                "completed": completed,
                "qualifies": qualifies,
                "streak_after": qualifying_streak,
                "reasons": reasons,
            }
        )

    final_window = states[-qualifying_streak:] if qualifying_streak else []
    final_two = [state["review_id"] for state in final_window][-2:]
    final_snapshot = normalized["final_snapshot"]
    final_qualifying = states[-1] if states and states[-1]["qualifies"] else None
    final_candidate_bound: Optional[bool] = None
    if (
        final_qualifying is not None
        and final_snapshot is not None
        and final_snapshot["stable"] is not False
    ):
        final_candidate_bound = (
            final_qualifying["candidate_digest"] == final_snapshot["candidate_digest"]
        )
    semantic_gaps: list[str] = []
    if confidence != "supported":
        semantic_gaps.append("auditor confidence is not supported")
    if unresolved is True:
        semantic_gaps.append("auditor reports unresolved material findings")
    if qualifying_streak < 2:
        semantic_gaps.append("fewer than two consecutive fully qualifying reviews end the record")
    elif final_snapshot is not None and final_snapshot["stable"] is False:
        semantic_gaps.append("final observed snapshot is explicitly unstable")
    elif final_candidate_bound is not True:
        semantic_gaps.append(
            "final qualifying review is not bound to the final observed candidate digest"
        )

    fail_reasons: list[str] = []
    if normalized["behavior_passed"] is False:
        fail_reasons.append("independent behavioral acceptance evidence failed")
    if normalized["protected_preserved"] is False:
        fail_reasons.append("protected behavior or user work was not preserved")
    if normalized["source_integrity"] is False:
        fail_reasons.append("frozen source or evaluator integrity check failed")
    if normalized["trial_status"] == "failed":
        fail_reasons.append("trial status is failed")

    incomplete_reasons: list[str] = list(semantic_gaps)
    if normalized["trial_status"] not in COMPLETE_TRIAL_STATUSES:
        incomplete_reasons.append("trial did not reach a completed status")
    if normalized["runtime_phase"] not in TERMINAL_RUNTIME_PHASES:
        incomplete_reasons.append("runtime did not reach a terminal completion phase")
    invocation_protocol_satisfied = (
        normalized["invocation_count"] == 1
        if normalized["execution_mode"] == AUTONOMOUS_MODE
        else (
            normalized["invocation_count"] == 2
            and normalized["controlled_resume_verified"] is True
        )
    )
    if not invocation_protocol_satisfied:
        if normalized["execution_mode"] == AUTONOMOUS_MODE:
            incomplete_reasons.append("autonomous trial was not established from exactly one Improve invocation")
        elif normalized["invocation_count"] != 2:
            incomplete_reasons.append("controlled resume requires exactly two Improve invocations")
        else:
            incomplete_reasons.append(
                "controlled resume lacks an evaluator-owned verified controller receipt"
            )

    if fail_reasons:
        status = "fail"
    elif incomplete_reasons:
        status = "incomplete"
    else:
        status = "pass"

    boundaries = list(BOUNDARIES)
    if normalized["execution_mode"] == CONTROLLED_RESUME_MODE:
        boundaries.append(
            "A controlled_resume result is controller-assisted and must be reported separately from autonomous trials."
        )

    return {
        "status": status,
        "schema_valid": True,
        "errors": [],
        "fail_reasons": fail_reasons,
        "incomplete_reasons": incomplete_reasons,
        "boundaries": boundaries,
        "sequence": {
            "review_count": len(reviews),
            "review_candidate_count": len(reviews),
            "completed_review_count": len(completed_review_ids),
            "completed_review_ids": completed_review_ids,
            "last_material_review_id": last_material_review_id,
            "qualifying_streak": qualifying_streak,
            "final_two_qualifying_review_ids": final_two,
            "final_observed_snapshot_id": final_snapshot["id"] if final_snapshot else None,
            "final_observed_candidate_digest": final_snapshot["candidate_digest"] if final_snapshot else None,
            "final_observed_snapshot_stable": final_snapshot["stable"] if final_snapshot else None,
            "final_qualifying_snapshot_id": final_qualifying["snapshot_id"] if final_qualifying else None,
            "final_qualifying_candidate_digest": final_qualifying["candidate_digest"] if final_qualifying else None,
            "final_candidate_bound": final_candidate_bound,
            "incomplete_or_unknown_review_ids": incomplete_review_ids,
            "states": states,
        },
        "outcome": {
            "behavior_passed": normalized["behavior_passed"],
            "protected_preserved": normalized["protected_preserved"],
            "source_integrity": normalized["source_integrity"],
            "trial_completed": normalized["trial_status"] in COMPLETE_TRIAL_STATUSES,
            "runtime_terminal": normalized["runtime_phase"] in TERMINAL_RUNTIME_PHASES,
            "single_invocation": normalized["invocation_count"] == 1,
            "execution_mode": normalized["execution_mode"],
            "autonomous": normalized["execution_mode"] == AUTONOMOUS_MODE,
            "invocation_protocol_satisfied": invocation_protocol_satisfied,
            "controlled_resume_verified": normalized["controlled_resume_verified"],
            "auditor_confidence": confidence,
            "unresolved_material_findings": unresolved,
            "final_candidate_bound": final_candidate_bound,
        },
    }


def _prompt_observations(observed: object) -> dict[str, Any]:
    """Whitelist audit-catalog facts; full snapshot details stay in evidence files."""
    if not _mapping(observed):
        return {"invalid_observed": True}
    keys = (
        "snapshots",
        "evidence_refs",
        "runtime_phase",
        "behavior_passed",
        "protected_preserved",
        "invocation_count",
        "execution_mode",
        "controlled_resume_verified",
        "source_integrity",
        "trial_status",
    )
    prompt_observations = {key: observed.get(key) for key in keys if key in observed}
    prompt_observations.setdefault("execution_mode", AUTONOMOUS_MODE)
    snapshots = observed.get("snapshots")
    if not isinstance(snapshots, list):
        return prompt_observations

    snapshot_keys = ("id", "candidate_digest", "stable", "relative_evidence_paths", "head")
    prompt_observations["snapshots"] = [
        {key: snapshot[key] for key in snapshot_keys if key in snapshot}
        if _mapping(snapshot)
        else {"invalid_snapshot": True}
        for snapshot in snapshots
    ]
    return prompt_observations


def build_judge_prompt(observed: dict[str, Any]) -> str:
    """Build a fresh, read-only auditor prompt without leaking fixture labels."""
    record = json.dumps(_prompt_observations(observed), indent=2, sort_keys=True)
    schema = json.dumps(JUDGMENT_SCHEMA, indent=2, sort_keys=True)
    return f"""You are a fresh, read-only auditor of an Improve trial.

Use only the supplied snapshots and evidence catalog. Reconstruct distinct
completed reviews from the actual record. The reviews array contains only
review-cycle candidates: omit a proven callback, retry, diagnostic step, or
recovery action from that array and cite it only as supporting evidence for a
real review when relevant. If a claimed review lacks distinctness evidence, keep
it as a review candidate with distinct_review false; it remains incomplete.
A repeated candidate digest is allowed for separate no-change reviews when the
cited record supports their distinctness. Do not infer a desired review count
or expected fixture outcome.

For each review, cite only exact evidence_refs from the supplied catalog and an
exact snapshot_id from the supplied snapshots. Classify a supported material
finding or edit as material, a fully current and distinct clean review as
qualifying, and ambiguity or missing evidence as unknown or incomplete. Do not
repeat a host-provided streak; the evaluator will recompute it. Set auditor
confidence to supported only when the record supports the semantic assessment.
The supplied invocation_count is factual. When execution_mode is
controlled_resume, this is a controller-assisted pause-and-continuation probe,
not an autonomous trial and not evidence of extra reviews; the controller
receipt does not substitute for review evidence.
When a snapshot includes stable: false, it may document a finding but cannot
support a qualifying review; a missing stable field is accepted for legacy
records. Make the final qualifying review describe the final observed candidate
digest, while allowing an earlier qualifying review to precede a genuinely
trivial change that the record supports.

Return only one JSON object matching the schema below. Give concise
finding_summary text and evidence references. Do not provide private reasoning,
hidden chain-of-thought, or instructions for changing the candidate.

OUTPUT SCHEMA:
{schema}

SUPPLIED OBSERVATIONS:
{record}
"""
