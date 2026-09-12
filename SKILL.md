---
name: until-loop
description: >-
  Interpret a natural-language request as work to pursue, a continuation
  condition, and an evidence-based exit condition. Adapt each next action
  until the outcome is established or a real blocker requires attention.
  User slash: /until-loop. Parent skills load this card. Same-turn execution
  with durable state; does not invoke /goal.
allowed-tools: all
disable-model-invocation: true
user-invocable: true
argument-hint: "<what to pursue, and when to stop> | next"
version: 0.2.0
license: MIT
platforms:
  - linux
  - macos
metadata:
  skill_craft:
    kind: script-backed
---

# until-loop

Take an ordinary request and drive it to a defensible stopping point. The
agent interprets intent, chooses the work, and evaluates completion. The
runtime records accepted increments and checks any executable verifier.
You do not invoke /goal or create a background scheduler with this skill.

## Natural-language entry

The user or parent provides intent, not a form to fill in:

- `/until-loop Keep reviewing and fixing this until no substantive issues remain.`
- `/until-loop Get the import working end to end, including the malformed rows.`
- `/until-loop Tighten this proposal until the recommendation is clear and every claim is supported.`
- `/until-loop While there are unprocessed reports, reconcile the next one. Stop when all are accounted for or a source is missing.`

No named arguments are required. Use the request, prior conversation,
repository instructions and actual artifacts to infer the contract below.
Do not ask the user to translate their request into flags. Option-like text
inside an objective is ordinary content. Exact legacy command forms
remain supported through an explicit verb and option/value syntax; see
`references/runtime.md` for that compatibility path.

## Interpret the contract

Before work, inspect enough context to distinguish these three decisions:

1. **Execute:** What result is wanted, within what scope, and what kind of
   action should each increment choose? Include dependencies, exclusions and
   any requested review or delivery. A goal is not a fixed list of commands.
2. **Continue:** What observable gap or unresolved question makes another
   increment useful, and what preconditions permit it? Pick work that closes
   the most important gap or resolves a blocking uncertainty. This is more
   specific than merely saying the task is not done.
3. **Exit:** What evidence would establish the requested outcome? Preserve
   every required clause, including negative constraints. Distinguish success
   from an explicit stop condition such as missing input, cancellation,
   an expired budget, or an external dependency.

Interpret `until`, `while`, `unless`, `and`, and `or` in context. A `while`
condition is checked before acting; an `until` condition may already hold.
No work increment is mandatory when current evidence establishes success.
For ambiguous conjunctions, choose the reading supported by the user's
intended outcome; do not turn a blocker into success just because it follows
the word "or". Honor an explicitly requested do-once-then-check sequence.

Turn broad quality words into a task-specific rubric. For example, a review
can stop after the requested scope is covered, material findings are fixed,
relevant checks pass and the requested residual review is clean. For an
open-ended improve-until-clean request without a pass count, use two
consecutive substantive review passes with no material findings; any material
change resets that count. Do not impose this review loop on simple finite work.
For prose, judge the actual audience, clarity, accuracy and requested content;
file existence or a word count cannot establish those qualities alone.

Make reasonable, reversible assumptions and state them briefly. Ask only when
missing intent, permission, or input materially prevents choosing valid work
or evaluating completion; continue independent work while a question is open.
Do not invent requirements to prolong the loop or weaken the exit condition
to obtain a pass. An explicit user correction may revise scope; a failed check
does not authorize removing its requirement.

Tell the user the interpreted work, continuation and exit in a few plain
sentences. This is an explanation, not a required questionnaire or approval
gate. Read `references/runtime.md` before executing the internal commands.
Persist the original request and interpreted contract together in the frozen
prompt so a fresh context can recover the reasoning that guides the loop.

## Host loop

For each active packet:

1. **Reassess before acting.** Read the current artifacts, accepted evidence
   and remaining criteria. Do not rerun completed work from a stale plan.
   On resume, first use the runtime's `next` and read the full frozen prompt
   and state, plus `working.md` if present. Treat working notes as leads to
   recheck, not proof of success; recheck any recorded blocker before choosing work.
2. **Decide:** finish if every success criterion has current evidence; pause
   if a real blocker prevents all useful authorized progress; otherwise pick
   the next bounded action from the gaps. When a precondition is false but
   success is unproven, resolve that uncertainty or report the blocker.
3. **Execute and observe.** Do the selected work, inspect its result, and
   record what changed, what was tested or reviewed, and what remains. A
   diagnostic that rules out a cause is progress; repeating an unchanged
   failing action without new information is not. Change strategy when stuck;
   do not burn cycles to create an appearance of progress.
4. **Evaluate the whole exit condition.** Check every clause against the
   current candidate. Passing a narrow test is only evidence for what it
   exercises. Missing evidence stays unresolved. A material edit invalidates
   affected prior checks and any clean-review streak. For broad or subjective
   work, use a separate read-only evaluator when available and authorized;
   otherwise disclose that the review was a self-check.
5. **Record one transition.** Use the internal continuation or success closer
   based on that evaluation. Evidence should say what changed, which criteria
   now hold, and the remaining gap or final proof. Inspect the returned phase
   and verifier result; a rejected success claim means reassess, not assume
   completion. Continue in this turn while useful work remains.

Keep a compact `.until-loop/working.md` for multi-step work: original scope,
current criteria/evidence, unresolved gaps, next action, accepted cycle and
any blocker. It is an agent-maintained notebook, not runtime state. Before
reading or writing it, inspect file metadata without following links (for
example, `lstat`); refuse a symlink/non-file and do not read its target.
If unsafe, report the limitation and use the frozen contract and actual
artifacts instead. Compare notes to the frozen
contract and current state on resume; discard stale conclusions and recheck
artifacts. Never edit state/history/prompt files to manufacture a transition.

Before leaving an initialized active run incomplete, write a checked
`working.md` record of the stop reason, accepted cycle, remaining criteria,
and what would permit resumption. This record is required even for a short
run; if it cannot be saved safely, report that limitation. On a real blocker,
report incomplete without a success closer. The v1 runtime remains
active for a later resume; it does not have a blocked phase. If the user
specified a legitimate early-stop condition, honor it and distinguish the
unfinished result from achievement. A terminal `halted` is also incomplete.
Do not force-restart to evade a budget. A new user request can authorize a
new run or revised contract; preserve earlier history via the runtime.

## Communication and completion

Read runtime packets internally. Do not echo whole packets, command rails or
verifier tails by default. Give concise updates explaining the actual next
action, new evidence, changed assumptions or blocker. Show raw output only
when requested or when an error needs it. A parent requesting packet echo
may override this presentation choice.

Stop runtime updates at `stop — no update`. Report what was achieved,
decisive evidence, relevant limits and remaining work, if any. The runtime
cannot independently prove semantic completion or wake a host after the
turn ends; those are host capabilities, not promises of this skill.

## Parent skills

A parent must read this file in full and hand over the objective and
constraints in natural language. From then on the agent follows this card,
including its internal runtime adapter. This card is the only CLI caller.
The parent must not type `/until-loop` or invoke `/goal` to inject another
skill. Existing parents referencing Exact interpolation, Evidence quoting
or Error contract should read those sections in `references/runtime.md`.
Keep the parent's constraints when deriving the contract.

## Validation

Run `bash tests/until-loop.test.sh` for deterministic runtime and packaging
checks. Read `tests/intent-evals.md` for realistic interpretation/resume
evaluations; prose matching does not validate the agent's decisions.
`references/state.md` and `references/packet.md` define the internal protocol.
`AUDIT.md` records the runtime audit; `INTENT_REVIEW.md` records this redesign.
