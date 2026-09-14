# Improve review policy
Policy ID: improve/review-policy/v1

This declarative policy defines the reusable obligations of an improvement
review cycle. It is not an execution engine, command interface, or finalization
authority. A consumer applies it only with an explicit owner binding.

## Required owner binding

Before a consumer applies this policy, its owner must state:

- the history window to inspect and how its contents inform the review;
- the precise candidate scope, baseline, and treatment of adjacent context;
- the semantic classification rule for trivial, material, and uncertain
  findings;
- the durable evidence locations and records that establish a completed
  review;
- the commit policy, including no-change handling and any explicit override;
- the phase boundary and callback that receive each completed phase; and
- the authority and conditions that may finalize the whole requested work.

The binding must preserve explicit user constraints. History informs a plan; it
does not silently expand scope, establish present truth, authorize unrelated
work, or override a user constraint.

## Owner-managed consumer entrypoint

An owner-managed consumer reads this policy in full together with its owner's
binding before acting. If an owner splits a cycle into phases, execute only the
assigned phase and return the owner callback. Never run the entire cycle in one
action. The owner remains responsible for invoking later phases and deciding
whether their evidence can support finalization.

## Review-cycle obligations

For each distinct review cycle, preserve the following ordered obligations:

1. **Review.** Inspect the current in-scope candidate, relevant affected
   consumers, prior review evidence, and the owner-provided history window.
   Treat reviewer suggestions as findings to triage, not automatic edits.
   Accept a suggestion only when current evidence shows a failure, violated
   requirement, or concrete in-scope benefit; record the basis for accepting
   or declining it. Investigate uncertain impact before classifying it.
2. **History-informed plan.** Select only worthwhile, authorized work and
   state its expected behavior and verification criteria before applying it.
   An empty plan is valid when a substantive review finds no worthwhile
   change. Do not invent cosmetic edits, speculative rewrites, or redundant
   tests merely to manufacture a review result.
3. **Apply.** Apply the accepted bounded plan while preserving unrelated user
   work and the owner's explicit constraints. A material finding or material
   edit resets the clean-review streak to zero, even when repaired in the
   same cycle.
4. **Checks.** Establish relevant cases during planning, then use learned code
   context to author or refine meaningful tests where needed. Retain adequate
   existing tests when they cover the intended behavior. Judge failures against
   that behavior; correct an invalid test with an explicit basis and never
   weaken assertions merely to obtain a passing result. Failed, stale, blocked,
   or incomplete checks never advance the streak.
5. **Record.** Retain evidence for the resulting candidate: scope and identity,
   findings and classification, plan or no-change reason, applied changes,
   checks and results, learnings, any owner-required commit receipt, and
   streak before and after. Material changes invalidate affected evidence and
   require current evidence for the changed candidate.
6. **Assess.** Count a pass only after a fully completed, distinct review
   cycle with current evidence. A callback, retry, diagnostic step,
   verification run, repeated claim, or recovery action is not another review.
   Final convergence requires two distinct, consecutive, fully completed
   review cycles with only trivial findings and fixes, or no changes, no
   unresolved material finding, and current relevant checks. This practical
   stopping rule is not proof that no possible defect remains. A blocker,
   requested stop, or exhausted budget remains incomplete and never
   substitutes for convergence.

## Interrupted work and final inventory

When work is interrupted after a commit or other recorded action, reconcile the
receipt with the actual candidate, checks, and review record before continuing.
Reuse verified work without reapplying a fix or duplicating a commit. If the
evidence needed to count a review is absent, perform the missing review or
checks and record the uncertainty; a receipt alone does not invent a completed
cycle.

At final assessment, compare the resulting candidate with the initial
ownership-aware inventory, declared outputs, and scoped change receipts. Keep
pre-existing user files, unrelated changes, and artifacts whose ownership or
disposability is not established. Remove an artifact only after establishing
that the work created it, it is disposable, and it contains no user work.
Inspect unexpected artifacts from tests or tools, including ignored generated
files, before assigning their ownership or removing them.
Refresh affected checks after a cleanup that changes the candidate.

## Independent review

Use a fresh, read-only independent reviewer when available for a meaningful
second pass. Give that reviewer the scope, current artifacts, evidence, and
criteria without telling it that the desired result is trivial. Treat its
observations through the same triage rule. If independent review is unavailable,
disclose the self-review limitation in the record.
