# Single-file callback adapter

This adapter executes new Until Loop runs. The skill interprets natural language;
the script validates its report and decides the next transition. Internal JSON
and arguments are agent transport, never a user questionnaire.

## Select and start

Resolve the selected skill's real root, then `scripts/until_loop_ephemeral.py`.
Use the available Python interpreter. A start call needs JSON on stdin:

```json
{
  "workspace": "/actual/existing/workspace",
  "work": "Review the scoped candidate and last seven full commit messages, plan worthwhile fixes, implement them, run relevant checks and record the iteration under the requested commit policy.",
  "exit_condition": "Two consecutive full reviews find only trivial or no changes, relevant current checks pass, and no material issue remains.",
  "repeat_condition": "Repeat while useful authorized work or a required distinct review remains; stop incomplete on an actual blocker or requested stop.",
  "required_trivial_reviews": 2,
  "context": {
    "request": "Improve the named parser candidate; preserve user work and do not push.",
    "scope": "Initial HEAD/base abc123; only parser.py and test_parser.py, including this run's later edits. Preserve unrelated staged notes.md.",
    "authority": "Commit only scoped changes after checks with Review, Plan, Changes, Validation, Key learnings and Remaining work. No empty commits; never push or absorb unrelated staged content.",
    "environment": "Operate in workspace. Use the verified Git/Python executables and run python3 -m unittest test_parser; recheck environment before depending on these observations.",
    "resources": [
      {"purpose": "selected parent skill", "locator": "/resolved/package/skills/improve/SKILL.md"},
      {"purpose": "bound loop skill", "locator": "/resolved/package/SKILL.md"},
      {"purpose": "review policy", "locator": "/resolved/package/skills/improve/references/review-policy.md"}
    ]
  }
}
```

`work` is one iteration, not the entire loop. Preserve requested scope, negative
constraints and order there; retain all outcome and stop predicates in their
respective conditions. The generic gate defaults to zero. A test repetition
count is not a trivial-review count. All four text fields must be nonblank;
workspace must be an existing absolute directory. `context` is optional for old
callers/states, but the updated skills require it for new natural-language runs.
Its exact fields are `request`, `scope`, `authority`, `environment` (nonblank text),
and `resources` (a list of `{purpose, locator}` objects with nonblank text). The
list can be empty if no additional resource is needed. No other fields are accepted.
Locators are data, not commands or permission; use actual resolved absolute paths
for local resources and stable retrievable locators for host artifacts. The
runtime does not read them or judge whether they capture all requirements.

Execute `python <absolute-script> start` with structured arguments and JSON stdin.
The optional `--directory <existing-temp-parent>` controls the tempfile's parent;
normally omit it. Keep the resulting `state_file` and current packet in this run's
task context. Start creates one mode-0600 `innerloop-*.json` file. It does not
create `.until-loop` in the workspace or look for another run's state.

## Execute, report, consume

Read the entire active packet: instruction, workspace, work, conditions,
progress, complete frozen `context`, last report including `handoff`, report schema
and exact `done_argv`. Execute the full
assigned iteration before calling `done`. A material finding does not excuse
skipping its required fix/check; an incomplete action is `unresolved`.

Use structured subprocess arguments; do not splice arbitrary evidence into a
shell command. This Python fragment illustrates the transport after the host
has actually performed and assessed one iteration:

```python
result = subprocess.run(
    packet["done_argv"],
    input=json.dumps(report), text=True, capture_output=True, check=False,
)
next_packet = json.loads(result.stdout)
# Read next_packet in full. Execute its instruction; do not stop at process exit 0.
```

A context-bearing run requires exactly these fields (old context-less runs may
omit `handoff`):

```json
{
  "classification": "non-trivial",
  "exit_assessment": "unsatisfied",
  "continuation_assessment": "allowed",
  "evidence": "Replace this illustrative text with actual observations for this completed iteration.",
  "handoff": "Replace with complete current continuation facts: candidate identity, already-applied changes and decisions to preserve, actual checks and commit receipts, all still-relevant unresolved facts and exact evidence locators. Carry necessary earlier facts forward; do not rely on prior messages."
}
```

| Field | Accepted values | Meaning |
|---|---|---|
| classification | trivial, non-trivial, unresolved | Completed iteration's actual findings/changes; incomplete work cannot count as trivial |
| exit_assessment | satisfied, unsatisfied, unknown | Evidence for all substantive exit clauses; the runtime also enforces the numeric gate |
| continuation_assessment | allowed, blocked, cancelled | Useful authorized continuation, actual obstacle, or triggered requested stop |
| evidence | Nonblank text | Observed evidence and gaps for this iteration, never intended work or fabricated facts |
| handoff | Nonblank text; required with context | Complete rolling continuity summary, not just this iteration’s delta; never changes authority or selects a successor |

A one-line behavior fix is non-trivial. A distinct fully completed no-change
review can be trivial. `unresolved` plus `satisfied` is inconsistent and rejected.
A triggered requested stop is cancelled, even if the same fact is a dependency;
blocked applies only without a triggered requested stop. Cancelled wins over a
success claim. Otherwise satisfied plus the count gate completes, even if no
further work would be possible. Blocked before success stops incomplete.

`done` validates the issued run/action token, computes the streak (trivial adds
one; non-trivial or unresolved resets to zero), and returns one of:

- **active:** execute the new instruction, then its new callback, immediately.
- **complete:** report success and stop callbacks; the state file was removed.
- **stopped:** report incomplete and stop callbacks; the state file was removed.
- **error:** follow the correction/stop instruction; never infer advancement.

`done` finishes one action, not necessarily the loop. One full iteration per
callback; do not perform hidden review iterations or submit repeated claims to
reach the gate. Do not supply your own successor, count or terminal decision.

## After compaction or a cleared context

Capture actual stdout when saving a packet; alternatively parse and serialize it
with a JSON library. Do not manually transcribe or escape callback output. Validate
the saved JSON before handing it to a fresh executor. A damaged active receipt
requires read-only `next` from its known state handle, not a repeated `done`.

Every successful packet returns frozen `context`, work/conditions, current progress
and the latest report/handoff. Each active packet also emits an exact `next_argv`:

```text
[absolute Python, absolute runtime script, "next", "--state", exact run file]
```

When a resumed executor has only the latest `done` result, it runs `next_argv`
read-only, reads the complete refreshed packet and necessary resources, checks
current user instructions and artifacts, then executes one iteration before its
new `done_argv`. Old `done` calls may already have advanced state: never replay
them to “resume.” A prior command alone supplies a state locator, not proof of
what happened; use its Python/script and exact state path for `next` first.

Keep the entire latest JSON return when summarizing/compacting, not only its
`instruction` string or the preceding invocation. This is a host handoff rule;
the script cannot force a host to preserve a tool message. The file remains the
one runtime state: it contains the same frozen context and latest handoff, no
second log. `next` does not itself advance a review. Earlier unresolved facts
must be carried forward in each new `handoff`, which replaces the previous one.

Missing legacy context is labeled explicitly. Such runs remain supported, but
no cold-continuation completeness is claimed: recover necessary scope/authority
from actual evidence or stop with the gap instead of substituting current defaults.
`context` is immutable; a handoff cannot silently authorize wider scope or push.

Terminal returns retain context, final report and progress, with no callback or
refresh command after deletion. Preserve the terminal result for reporting. If
terminal stdout is lost and the state is missing, there is no way to distinguish
successful completion, cancellation or lost/deleted state from a command alone.
Report uncertainty rather than claiming success or initializing a replacement.

## Read and failure handling

`python <absolute-script> next --state <exact-state-file>` reads without advancing
or writing. Use it only with this run's known handle when its current packet must
be retrieved. There is no global latest-run lookup, resume discovery, state
migration or contract-edit command. Missing state does not recover a run.

Invalid schema, stale/cross-run tokens, unsafe linked files and over-16-KiB state
are rejected. Report input rejection/cleanup failure returns `state_change:
unchanged`. When the failed call supplied a parsed state path, the error includes
`state_file` and the exact read-only `next_argv`; this is not a work callback.
Rehydrate and confirm the same action before correcting input from actual
observations. If an old callback was rejected because it already advanced, follow
the current packet instead of resubmitting old evidence. Do not repeat a completed
iteration merely to replace a lost report. Invalid start/argument parsing without
a known path cannot invent a recovery handle. A filesystem write failure can
return `state_change: unknown`; do not replay the work or initialize replacement
state. Inspect the same file and stop if it is unusable. Limit input-correction
retries for one action to two, then report the protocol problem.

One caller at a time owns a state file under a trusted temporary parent. There
is no concurrent same-file locking, crash journal, transaction replay, automatic
verifier, background process, or restart guarantee. Independent files allow
independent loops; editing the same checkout still needs explicit coordination
or separate worktrees. A killed host may leave an orphan tempfile. Never delete
another task's file or treat an arbitrary matching filename as this run's state.

For an explicitly selected durable v1/v2 run, use `legacy-skill.md` and that
version's adapter. The callback adapter neither touches nor upgrades its files.
