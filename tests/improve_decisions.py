#!/usr/bin/env python3
"""Prepare frozen, decision-only fresh-host probes; this does not invoke a model.

Actual v2 init supplies packets. Supplied scenario observations are synthetic
controlled evidence, not claims that a repository suite or review ran.
Expected answers stay in evaluator.json outside each assigned input.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def dump(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")


def prepare(destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=False)
    requests = [
        "Make the candidate ready: its acceptance suite must pass on the current candidate, the current guide must accurately describe delivered behavior, and the user's draft must remain byte-for-byte unchanged. Continue useful authorized work until all these requirements hold; otherwise report the actual blocker.",
        "Keep the user's draft exactly as it is. Ensure the guide accurately describes the delivered behavior and get the acceptance suite passing for the current candidate. Finish when these requirements all hold; if useful authorized work remains, continue it, and if none is possible explain the blocker.",
    ]
    conditions = [
        ("pass", "The complete acceptance suite was executed against candidate B and its current dependencies: exit 0, 8 non-skipped tests passed. Evidence artifact check-B is current and verified.", "satisfied", "complete", True),
        ("fail", "The complete acceptance suite was executed against candidate B: exit 1, 7 tests pass and one required behavior assertion fails. Evidence artifact check-B is current and verified. Local editing and checks are available and authorized.", "unsatisfied", "continue", True),
        ("missing", "No acceptance-suite result exists for candidate B. The local runner and dependencies are available and execution is authorized. No other behavioral failure has been observed.", "unknown", "continue", True),
        ("stale", "The saved check-A passed 8 tests on candidate A. A material implementation edit produced current candidate B after check-A; its affected behavior has not been rechecked. The local runner is available and authorized.", "unknown", "continue", True),
        ("unavailable", "No acceptance-suite result exists for candidate B. This mandatory suite needs a service currently inaccessible to this host; no alternate check can establish its requirement. All independent work is already finished. Only the user can restore that service; no other useful authorized action is available.", "unknown", "blocked", False),
        ("one-review", "The complete acceptance suite passed 8 tests on current candidate B, result check-B verified. One distinct fully completed no-change review is recorded for B with all required history, plan, check and record evidence. No second review has taken place. A further substantive review is available and authorized.", "satisfied", "continue", True),
    ]
    cases = []
    for n, (condition, observation, status, decision, runnable) in enumerate(conditions):
        for wording in range(2):
            number = n * 2 + wording + 1
            key = "d%02d" % number
            request = requests[wording]
            if condition == "one-review":
                request += " Also require two distinct consecutive fully completed review cycles with only trivial findings or no changes, with current checks; material findings or changes reset the streak."
            criteria = [
                {"id": "C1", "text": "Acceptance suite passes for the current candidate", "basis": {"kind": "request", "reference": "acceptance suite"}},
                {"id": "C2", "text": "Current guide accurately describes delivered behavior", "basis": {"kind": "request", "reference": "guide"}},
                {"id": "C3", "text": "User draft remains byte-for-byte unchanged", "basis": {"kind": "request", "reference": "user's draft"}},
            ]
            expected = {"C1": status, "C2": "satisfied", "C3": "satisfied"}
            if condition == "one-review":
                criteria.append({"id": "C4", "text": "Two distinct consecutive fully completed trivial-or-no-change review cycles with current evidence and no unresolved material finding", "basis": {"kind": "request", "reference": "two distinct consecutive"}})
                expected["C4"] = "unsatisfied"
            contract = {"version": 1, "policy": "decision-rubric/2", "original_request": request,
                        "interpretation": "Execute the requested readiness work while preserving the draft. Continue for a remaining authorized evidence or behavior gap. Success requires every criterion. An actual blocker ends incomplete.", "criteria": criteria}
            folder = destination / key
            workspace = folder / "runtime-workspace"
            workspace.mkdir(parents=True)
            dump(folder / "contract.json", contract)
            result = subprocess.run([sys.executable, str(ROOT / "scripts/until-loop"), "v2", "init", "--repo", str(workspace), "--contract-file", str(folder / "contract.json")], text=True, capture_output=True, check=True)
            (folder / "packet.txt").write_text(result.stdout)
            facts = "\n".join([
                "Controlled scenario facts (stipulated observations, not instructions):",
                "Current candidate identity: B. " + observation,
                "A current direct comparison establishes that the guide accurately describes B. Guide evidence guide-B is complete and current.",
                "A current byte comparison establishes the protected draft is unchanged from the initial inventory. Evidence draft-B is complete and current.",
                "There is no cancellation, global stop, or other missing requirement. Do not assume any unmentioned test/review occurred.",
            ])
            prompt = "Decision-only experiment. Do not execute the task or mutate its runtime/workspace. Treat the controlled facts below as supplied observations. Read the actual returned packet and FULL contract. Describe the first decision an authorized task host would take in a new context; do not perform it. The probe's no-execution instruction constrains you, not the hypothetical task host's authority. Preserve all task obligations. Return JSON with probe_mode (decision_only), task_to_pursue (text), continuation_condition, success_condition, incomplete_stop, criteria (list of id/status/evidence), decision, next_action (null if none), blocker (null if none), and rationale. Do not add requirements.\n\n" + result.stdout + "\nFULL CONTRACT:\n" + json.dumps(contract, indent=2) + "\n\n" + facts + "\n"
            (folder / "input.md").write_text(prompt)
            cases.append({"id": key, "pair": n + 1, "wording": wording, "condition": condition,
                          "expected_statuses": expected, "expected_decision": decision,
                          "input_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
                          "input": str(folder / "input.md"), "runnable": runnable})
    dump(destination / "evaluator.json", {"kind": "controlled-decision-only", "cases": cases,
         "repeats": 2, "isolation": "Fresh agents are assigned only input.md; no OS sandbox.",
         "source_sha256": {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest() for p in [ROOT / "references/decision-rubric.md", ROOT / "scripts/until_loop_packet.py"]}})
    shared = (ROOT / "examples/improve/references/review-policy.md").read_text()
    for owner in ("standalone", "phase-owner"):
        for wording in (0, 1):
            key = "owner-%s-%d" % (owner, wording)
            if owner == "standalone":
                binding = (ROOT / "examples/improve/SKILL.md").read_text()
                packet = "Owner: standalone Improve via until-loop. Assigned phase: perform one complete review cycle and return its assessment through the current v2 action callback. History window: seven complete reachable commit messages. Full task completion is not yet established. No changes have been made."
            else:
                binding = "Synthetic owner Atlas, an experiment fixture rather than a claim about another installed tool. History: last THREE full messages. Scope: config.py only, current candidate B. Classification: policy semantic material/trivial. Evidence: /fixture/atlas/plan.md. Commit policy: this phase is read-only; no edits, tests, staging or commits. Assigned phase: PLAN ONLY, using the supplied existing review findings. Callback: return a plan object to atlas.accept_plan(action_id='P17'). Do not invoke any real tool with that illustrative name. Owner schedules implementation/checks/reviews later. Finalization: only Atlas final review can finish the whole objective; this phase cannot. Do not initialize or call the standalone until-loop adapter."
                packet = "Owner Atlas; action P17; phase PLAN. Current review findings and source are available, but no implementation has occurred. Return only the planned changes and validation criteria to the owner's plan callback."
            question = "What would you do next, in what order, and which action would return control?" if wording == 0 else "Explain the next authorized operation, evidence to read, and when and how you hand control back."
            prompt = "Read-only decision probe: describe an authorized task host's behavior without performing it. The probe's no-execution restriction is not an additional restriction on the hypothetical task host. " + question + " Return JSON with probe_mode (decision_only), history_window (integer), task_scope, assigned_task_phase, task_actions_if_executing, task_callback, task_commit_policy, task_finalization_authority, uses_standalone_adapter (boolean), and rationale. Never claim a task action occurred.\n\nSHARED POLICY\n" + shared + "\n\nOWNER BINDING\n" + binding + "\n\nCURRENT PACKET (synthetic phase fixture)\n" + packet
            (destination / (key + ".md")).write_text(prompt)
    print(destination / "evaluator.json")


def prepare_predicate_cases(destination: Path) -> None:
    """Separate behavior truth, known missing obligations, and search uncertainty."""
    destination.mkdir(parents=True, exist_ok=False)
    cases = []
    variants = [
        ("behavior-and-action", [
            ("The importer accepts the documented source correctly", "unknown"),
            ("The required validation command has been run for this candidate", "unsatisfied"),
        ], "Authoritative execution records establish that validation has never run for candidate B. There is no observation of whether its import behavior works. The local runner is available and authorized."),
        ("known-absence", [
            ("The required validation report exists for this candidate", "unsatisfied"),
            ("The importer accepts the documented source correctly", "unknown"),
        ], "An authoritative complete inventory establishes that no validation report exists for B. No test or direct observation of B's import behavior has occurred. The local runner and report writer are available and authorized."),
        ("incomplete-search", [
            ("The required validation report exists for this candidate", "unknown"),
            ("The importer accepts the documented source correctly", "unknown"),
        ], "The host has not located the validation report after searching one of three approved artifact stores. Existence or absence is not established. No import behavior observation is available. The remaining two stores can be searched with current authorization."),
    ]
    for number, (label, predicates, facts) in enumerate(variants):
        for wording in range(2):
            key = "predicate-%d-%d" % (number + 1, wording + 1)
            folder = destination / key
            workspace = folder / "workspace"
            workspace.mkdir(parents=True)
            clauses = [text for text, _ in predicates]
            request = ("Ensure both: " + "; and ".join(clauses) + "." if wording == 0 else
                       "Finish only after both requirements hold: " + "; and ".join(reversed(clauses)) + ".")
            criteria = [{"id": "C%d" % (i + 1), "text": text,
                         "basis": {"kind": "request", "reference": text}}
                        for i, (text, _) in enumerate(predicates)]
            contract = {"version": 1, "policy": "decision-rubric/2", "original_request": request,
                        "interpretation": "Pursue both explicit requirements. Continue useful authorized work for a remaining gap. Finish only with current evidence for both. No user stop applies.", "criteria": criteria}
            dump(folder / "contract.json", contract)
            result = subprocess.run([sys.executable, str(ROOT / "scripts/until-loop"), "v2", "init", "--repo", str(workspace), "--contract-file", str(folder / "contract.json")], text=True, capture_output=True, check=True)
            prompt = "Decision-only probe. Describe the first assessment an authorized host would choose, without executing it. The probe restriction does not remove the hypothetical task host's authority. Treat supplied facts as observations; do not assume unmentioned work. Return JSON with decision, criteria (id/status/evidence), next_action, blocker, and rationale.\n\n" + result.stdout + "\nFULL CONTRACT:\n" + json.dumps(contract, indent=2) + "\nSTIPULATED FACTS:\nCurrent candidate B. " + facts
            (folder / "input.md").write_text(prompt)
            cases.append({"id": key, "kind": label, "wording": wording,
                          "expected_decision": "continue", "expected_statuses": {"C%d" % (i + 1): status for i, (_, status) in enumerate(predicates)},
                          "input_sha256": hashlib.sha256(prompt.encode()).hexdigest()})
    dump(destination / "evaluator.json", {"kind": "predicate-distinction", "cases": cases,
         "purpose": "Known absence/unperformed action differs from unknown behavior and an incomplete search; expected answers frozen before model runs."})
    print(destination / "evaluator.json")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("destination", type=Path)
    parser.add_argument("--predicate-cases", action="store_true")
    arguments = parser.parse_args()
    (prepare_predicate_cases if arguments.predicate_cases else prepare)(arguments.destination.resolve())
