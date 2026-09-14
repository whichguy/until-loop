# Decision rubric policy

This is the small, versioned policy frozen into a v2 run.  The runtime stores
the JSON object in `state.policy_snapshot`; the renderer selects state-specific
emphasis but does not interpret the task or judge whether evidence is true.

```json
{
  "version": "decision-rubric/2",
  "questions": [
    {
      "id": "scope",
      "instruction": "Preserve every clause, negative constraint, success alternative, conditional obligation and ordering rule in the full frozen contract. Do not turn an early stop into success."
    },
    {
      "id": "evidence",
      "instruction": "For each criterion, distinguish current evidence from an unverified host claim and assess its actual predicate. Use unknown when evidence cannot determine it; use unsatisfied when current evidence establishes it is unmet, including a required action known not to have occurred or a required record known to be absent. An unrun check does not establish that the underlying behavior fails; a record not yet located is not necessarily absent."
    },
    {
      "id": "change",
      "instruction": "After a material change, recheck the criteria and checks it could affect."
    },
    {
      "id": "continuation",
      "instruction": "Check explicit stop overrides and work preconditions first. Otherwise choose useful authorized work from a current gap, or name the blocker if none is possible. A missing fact alone does not forbid independent work."
    },
    {
      "id": "exit",
      "instruction": "Claim completion only when every required criterion has adequate current evidence and applicable checks pass."
    }
  ],
  "emphasis": {
    "active": "Establish current evidence before choosing the next decision.",
    "verifier_failed": "The configured verifier failed. Diagnose the failure and reassess every affected criterion before a completion claim.",
    "recheck_prior_evidence": "A prior host assessment is a claim to recheck against the current artifacts.",
    "assess_remaining_criteria": "Assess every remaining criterion separately; a configured verifier, when it passes, may cover only part of the contract.",
    "paused": "The run is paused. Do not resume work until the adapter records authorized resumption.",
    "terminal": "This run is terminal. Read status or recovery guidance; do not start work through this packet.",
    "verifier_uncertain": "Verifier effects are uncertain. Recover or inspect the effects before any replay."
  },
  "decisions": {
    "continue": "Continue for a specific remaining gap or diagnostic need, then reassess the whole condition. If all criteria are satisfied, identify the failed verifier or unresolved acceptance check that still warrants continuation; do not prolong a finished task.",
    "complete": "Complete when every required criterion has current evidence, applicable checks pass, and no explicit stop override applies. Existing success normally needs no product edit.",
    "blocked": "Pause incomplete when an explicit user stop applies or no useful authorized progress is possible. Name the reason and resumption condition. Use condition_observed for a dependency pause, even when only the user can restore the dependency. Use user_instruction for an explicit user pause or stop without already-authorized conditional resumption. Who can repair a dependency does not determine resume authority."
  }
}
```
