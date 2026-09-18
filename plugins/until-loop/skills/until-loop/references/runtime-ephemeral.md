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
  "required_trivial_reviews": 2
}
```

`work` is one iteration, not the entire loop. Preserve requested scope, negative
constraints and order there; retain all outcome and stop predicates in their
respective conditions. The generic gate defaults to zero. A test repetition
count is not a trivial-review count. All four text fields must be nonblank;
workspace must be an existing absolute directory. No extra fields are accepted.

Execute `python <absolute-script> start` with structured arguments and JSON stdin.
The optional `--directory <existing-temp-parent>` controls the tempfile's parent;
normally omit it. Keep the resulting `state_file` and current packet in this run's
task context. Start creates one mode-0600 `innerloop-*.json` file. It does not
create `.until-loop` in the workspace or look for another run's state.

## Execute, report, consume

Read the entire active packet: instruction, workspace, work, conditions,
progress, last report, report schema and exact `done_argv`. Execute the full
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

The report has exactly these fields:

```json
{
  "classification": "non-trivial",
  "exit_assessment": "unsatisfied",
  "continuation_assessment": "allowed",
  "evidence": "Replace this illustrative text with actual candidate identity, findings, applied changes, check results, commit receipt when required, and remaining gaps."
}
```

| Field | Accepted values | Meaning |
|---|---|---|
| classification | trivial, non-trivial, unresolved | Completed iteration's actual findings/changes; incomplete work cannot count as trivial |
| exit_assessment | satisfied, unsatisfied, unknown | Evidence for all substantive exit clauses; the runtime also enforces the numeric gate |
| continuation_assessment | allowed, blocked, cancelled | Useful authorized continuation, actual obstacle, or triggered requested stop |
| evidence | Nonblank text | Observed evidence and gaps, never intended work or fabricated facts |

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

## Read and failure handling

`python <absolute-script> next --state <exact-state-file>` reads without advancing
or writing. Use it only with this run's known handle when its current packet must
be retrieved. There is no global latest-run lookup, resume discovery, state
migration or contract-edit command. Missing state does not recover a run.

Invalid schema, stale/cross-run tokens, unsafe linked files and over-16-KiB state
are rejected. Report input rejection/cleanup failure returns `state_change:
unchanged`. Correct the input using actual evidence; if needed, inspect the same
file with `next` before retrying the callback. A filesystem write failure can
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
