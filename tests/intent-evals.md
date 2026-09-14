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

## Candidate v2 and presentation screening

The existing cases above are v1 historical fixtures. For v2, retain their raw
user requests and product assertions, then inspect the structured contract and
assessments instead of requiring v1 state field names. The missing-input case
should become `paused` after a supported blocked assessment, with no verifier
or work-cycle increment. Keep the recorded user constraints unchanged.

`pilot.py` and `pilot-cases.json` provide the separate A/B/C presentation
screen. Those arms retain the same v1 mechanical behavior and saved task
interpretation, varying placement of equivalent context/rubric guidance.
Do not credit v2's stronger rejection rules as improved model discernment.
The initial screen is four cases per arm, not statistical proof or a complete
semantic-completion benchmark. Record actual artifacts, first model decision,
accepted state, usage and elapsed time when available.

Evaluate v2 from raw requests as well. A correct hand-written contract in the
presentation screen does not validate the host's ability to derive criteria.
Inspect original wording and negative constraints, evidence coverage, actual
artifacts, unnecessary edits and blocked/resume behavior independently of the
worker's final response. Retain results outside the package, then record their
paths and interpretation in the implementation report.


## Fresh-context intent and packet evaluation

`fresh_context.py` prepares 11 real CLI state packets and 10 raw-language cases.
It freezes the skill and scripts, uses opaque worker workspaces, withholds test
expectations, records fresh read-only model transcripts, and checks source and
workspace drift. `prepare` performs no model calls; `run` uses up to two fresh
hosts with a 180-second per-host timeout and no model override. Use a new source
label and run ID rather than overwriting retained evidence.

```sh
python3 tests/fresh_context.py prepare --label candidate-check --skill-root /absolute/path/to/until-loop
python3 tests/fresh_context.py run --label candidate-check --run-id first-screen
python3 tests/fresh_context.py summarize --label candidate-check --run-id first-screen
python3 tests/check_fresh_outputs.py --label candidate-check --run-id first-screen
```

Packet workers predict the next permitted operation and serialized input from
an actual script response, with raw workspace reads allowed. They do not receive
the skill or expected answer. Natural-language workers receive the skill and
raw request, and derive a serialized contract; the probe's no-write restriction
is not an extra condition to add to that contract. Their predictions are not
proof of actual task execution. Review semantic decisions independently, check
transport records with the runtime, and retain partial or failed cases.

The deterministic `test_packet_decisions.py` suite separately extracts printed
resume/recovery records, fills only the observation reference and file path,
executes the printed command, and checks state, identity and verifier counts.
It also covers nonactive replay, wrong provenance type, failed-verifier versus
rejection identity, quoted paths and frozen-policy compatibility.

See FRESH_CONTEXT_REVIEW.md for the observed comparisons, corrections and limits.
