# until-loop host-intent evaluations

`intent-cases.json` defines six disposable-workspace evaluations for the host
behavior around the until-loop card. They test interpretation and durable state,
not the standalone runtime's argument parser.

## Run protocol

1. For each case, create a fresh empty directory and materialize the exact
   `files` mapping as UTF-8 files.
2. Use an installed host that loads the until-loop card, then submit only the
   case's `request` as the user objective. For `cold-resume-two-increments`,
   follow its `stages` instead: submit the setup request, then the listed fresh
   invocation. Do not add flags, a verifier command, an inferred terminal
   condition, or a prewritten completion decision.
3. Save the full host transcript and inspect the resulting workspace,
   `.until-loop/state.json`, `.until-loop/prompt.md`, and
   `.until-loop/history.jsonl` against that case's assertions and transcript
   rubric.
4. For `cold-resume-two-increments`, end the first host context after its first
   accepted incomplete increment. Start a fresh host context in the same
   workspace and use the stage's normal resume invocation. Do not resubmit the
   setup objective, restart the run, or manufacture state between stages.

These are live host evaluations. Do not replace them with a prose-regex check,
and do not count a passing fixture test as evidence that the host interpreted a
multi-part request correctly.

## Cheapest meaningful live subset

Run `multi-clause-weak-test`, `prechecked-while-until`,
`blocked-missing-input`, and `cold-resume-two-increments` first. Together they
cover a normal successful completion, an already-satisfied pre-check, a
required-input stop that must not become success, and persisted continuation in
a fresh context. Add `qualitative-draft-no-verifier` and `literal-option-text`
when checking prose-only completion and literal interpolation behavior.
