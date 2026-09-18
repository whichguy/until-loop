---
name: improve
description: Review and improve a repository candidate using the last seven Git commit messages, meaningful tests, and two consecutive trivial-only review iterations. Use for a deliberate improvement loop or an interpretation preview of that loop.
---

# improve

Use the shared [Improve review policy](references/review-policy.md) to turn
“improve these changes” into a bounded review-and-improvement contract. This
standalone card binds that reusable policy to Until Loop's single-file callback
runtime. It does not create a second state machine or invoke `/goal`.

Read the shared policy in full before interpreting or executing this skill. It
defines the reusable review-cycle obligations. The sections below supply this
standalone consumer's binding, preview behavior, callback evidence, and legacy
continuation boundary.

This parent is maintained inside the Until Loop package. Resolve symlinks to
this card's physical directory before resolving relative links. Read [the bound
Until Loop card](../../SKILL.md) in full and follow its current adapter. That
card is the only CLI caller: it translates the natural-language contract,
starts or resumes the selected runtime, and consumes the exact callback it
returns. Do not make the user supply JSON or fixed runtime arguments. A new
Improve run uses the bound card's per-run callback state, never an ambient
`.until-loop` directory or a shared “current run.”

## Owner-managed consumer entrypoint

An owner-managed consumer must read [the shared review
policy](references/review-policy.md) and that owner's explicit binding in full.
It must not run this standalone card or this card's Until Loop adapter. The
other owner supplies its own history window, scope, classification rule,
evidence location, commit policy, phase/callback, and finalization authority.

## Standalone owner binding

- **History window:** at the start of every active callback, read the last
  seven reachable Git commit messages in full (IDs, subjects, and bodies), or
  all available messages if fewer exist. Re-read the window for every distinct
  review and retain useful prior lessons in that review's host record. History
  is evidence of prior intent, not the current diff range, current truth, or
  permission to undo a change.
- **Scope:** retain any named branch range, files, or baseline. Otherwise
  freeze the initial Git HEAD and review the initial staged, unstaged, and
  relevant untracked changes together with this run's later edits. If clean,
  use the latest commit's change as a disclosed default unless context
  establishes a more specific candidate. Inspect adjacent consumers only as
  needed to assess the candidate. In an unborn repository, disclose that
  history is absent; execution that requires commits remains incomplete until
  that constraint is resolved.
- **Trivial classification:** classify impact semantically. Trivial work is
  non-semantic spelling, formatting, or explanatory polish with evidence that
  behavior is unchanged. A one-line bug fix, public-contract change,
  security/data-integrity correction, or missing required regression coverage
  is material. Uncertain impact remains unresolved until investigated.
- **Evidence location:** retain each review in the host-visible task record:
  the candidate identity and ownership-aware scope, seven-message history read,
  findings, plan or no-change reason, actual changes, commands and results,
  lessons, and commit receipt when required. Before `done`, summarize those
  observations truthfully in its concise `evidence` field and retain complete
  continuation facts in `handoff`, as described in
  [callback evidence](references/callback-evidence.md). New runs must not call
  `capture_evidence.py`, create `.until-loop/working.md`, or create
  `.until-loop/evidence/`. The script retains only its current contract,
  latest report, and trivial-review counter; it cannot turn a callback claim
  into proof.
- **Commit policy:** after required checks pass, commit authorized scoped files
  changed by a completed iteration with the required learning-oriented record.
  Its body must include Review, Plan, Changes, Validation, Key learnings, and
  Remaining work, including the classification and resulting streak. A
  no-change review gets an honest host record, not a manufactured edit or empty
  commit. An explicit audit-commit-every-iteration request requires one
  authorized audit record commit for every completed review; a no-change review
  uses an identified empty audit commit with no unrelated staged content. An
  explicit no-commit request preserves the host record without committing.
  Never reset the user's index or absorb unrelated staged or unstaged work.
- **One callback is one full cycle:** for every active packet, complete one
  ordered review cycle before the exact `done_argv`: read the history and
  candidate, plan worthwhile authorized work, implement it when warranted, run
  applicable meaningful checks, retain the record, and make any authorized
  commit. An empty plan is valid only after that substantive review finds no
  worthwhile change. A callback, retry, diagnostic command, or repeated test
  is not another review. Do not privately perform several reviews before one
  callback.
- **Assessment and finalization:** after that full cycle, call the returned
  `done_argv` with a truthful classification: `trivial` only for a distinct
  complete trivial/no-change review with current applicable checks;
  `non-trivial` for a material finding or behavior change, even if fixed; and
  `unresolved` if work, evidence, required commit, or assessment is incomplete.
  Report the substantive exit assessment and whether continuation is allowed,
  blocked, or cancelled. The configured gate is two consecutive qualifying
  trivial reviews; a material or unresolved report resets it. Only the runtime
  can accept a terminal transition. A blocker, requested stop, exhausted
  budget, failed required commit, stale check, or unresolved evidence remains
  incomplete rather than satisfying the review policy.

New runs are ephemeral across an abandoned host session: the per-run file
survives only while it exists and is deleted at a terminal transition. Separate
run files permit separate callbacks, but they do not coordinate edits, tests,
or Git commits in one checkout. Use separate worktrees or otherwise coordinate
that shared project work.

## Continue after context loss

Before `start`, freeze the actual initial HEAD/base or named range, the exact
included files and staged/unstaged/untracked ownership boundaries in
`context.scope`. A later commit or clean worktree does not select a new candidate.
Put the actual commit/no-commit, push/no-push and audit-commit rules in
`context.authority`. Preserve these record sections and the full ordered cycle
in the contract; reloading a changed card must not replace accepted user rules.
Name the selected Improve card, bound Until Loop card, review policy and any
required evidence/output locations in `context.resources` with resolved locators.
Record environment-specific Git/Python paths and actual check commands in
`context.environment` when needed. The canonical request goes in `context.request`.

Every `done` report must include a complete compact `handoff`: current candidate
HEAD and scoped edits, all still-relevant changes/decisions, actual check results,
commit receipts or why no commit exists, pending issues, and evidence locators.
Copy necessary findings from earlier cycles forward; never assume the next
executor can read earlier conversation. The script's returned progress remains
the authority for the streak, not a model-written number in the handoff.
The task record can still hold detailed output; essential continuity facts belong
in the packet, without introducing another state file or requiring a new log.

Resume from the bound card's latest full return and exact `next_argv`. Recheck
current artifacts and instructions. Preserve the original scope even when this
run already made a commit; do not blindly repeat fixes, commits or callbacks.
If required context or evidence cannot be recovered, report the gap as unresolved
and stop incomplete when it prevents useful authorized progress.

## Preview before execution when requested

“Dry run,” “preview,” “show how you interpret this,” and “do not execute” select
interpretation only when they refer to the improvement workflow; a prohibition
on executing a particular command remains a constraint on that action. Inspect
permitted repository context read-only, derive the shared policy plus this
standalone binding into a natural-language execution, exit, and continuation
contract, and use the bound Until Loop card's preview procedure. Stop after
presenting it. Do not start or resume a run, create a temporary state file or
loop note, edit product files, execute tests/verifiers, stage changes, or create
a commit. If the user forbids all writes, return the interpretation in the
conversation; saving an artifact elsewhere is optional only when permitted. A
preview neither achieves the proposed objective nor authorizes later execution.

For Git inspection during preview, use the documented no-optional-locks,
no-index-refresh, no-external-diff, and no-text-conversion options so status
and working-tree reads do not change the index or invoke configured external
processors. Preserve the distinction between the chosen edit scope and adjacent
files inspected for context.

Show the scope and history window, planned work, continuation and success
rules, incomplete stops, assumptions with their basis, evidence needed for each
rule, and the first action that would be taken. Explain the proposed commit
policy. Label unknown evidence and hypothetical decisions; do not claim tests
or review iterations occurred. An actual execution request must recheck current
context.

## Execution handoff and legacy continuation

Preserve the shared policy and every standalone binding above in the interpreted
contract, including conditional commit overrides and negative constraints. The
execution condition must contain the complete ordered review cycle; the exit
condition must include current evidence plus two consecutive qualifying
reviews; and the continuation condition must retain useful authorized work and
incomplete stops. Then follow the latest returned packet exactly. Execute its
full work before calling `done`; consume the whole result and execute the next
returned instruction while it is active. Do not add a nested plan-convergence
loop or invent a successor, counter, or terminal decision.

The runtime validates action identity and applies reported state transitions,
including the two-review gate. It does not independently verify that a commit
was made, a test ran, or a semantic judgment is correct. The callback evidence
is a concise handoff record, not fabricated proof.

An explicit continuation of a pre-existing version-1 or version-2 Improve run
uses [the legacy standalone binding](references/legacy-standalone.md) and the
matching legacy Until Loop adapter. Do not discover old state and silently use
it for a new Improve request, and do not migrate a legacy run into a new
temporary callback file.
