# Improve

```mermaid
flowchart LR
    Request[Natural-language request] --> Contract[Improve derives one-cycle work, exit, and repeat rules]
    Contract --> Packet[Until Loop returns one active callback]
    Packet --> Cycle[Review history and candidate, plan, implement, check, record]
    Cycle --> Report[Report actual result through exact done callback]
    Report --> Decision{Script transition}
    Decision -->|active| Packet
    Decision -->|complete or stopped| End[Terminal result]
```

Improve reviews a repository candidate, makes worthwhile changes, checks the
result, and continues until two distinct consecutive review cycles find only
trivial issues or no changes. It reads the last seven full Git commit messages
as history for every review. A material finding or fix resets the count, even
when it is a one-line change and even when the fix succeeds.

This card is the standalone Improve parent bundled with Until Loop. It leaves
the user-facing interface in natural language. The bound Until Loop card owns
the internal script calls and returns an action packet; Improve carries out the
packet's assigned complete review cycle and reports what actually happened.

## Start with a normal request

Open the repository you want reviewed, then use a request such as:

```text
Dry-run $improve on these changes. Show the scope and stopping conditions without writing files.
Use $improve on the parser changes, preserving its public API.
Use $improve on formatter.py and its tests. Do not stage or commit anything.
Use $improve on these changes and make an audit commit for every completed iteration, including no-change reviews.
```

The request supplies the work and constraints. It does not require runtime
flags, JSON, a state-file path, or a manually managed counter. The bound Until
Loop card interprets it into three distinct pieces:

| Contract piece | What it means for Improve |
|---|---|
| Execution condition | One complete review cycle: read history and candidate, plan worthwhile work, implement authorized fixes, run applicable checks, retain evidence, and make any authorized commit. |
| Exit condition | Current evidence establishes the scoped objective, no unresolved material finding remains, all other requested conditions hold, and two distinct qualifying trivial/no-change reviews have completed. |
| Continuation condition | Another full cycle is allowed while useful authorized work or an evidence gap remains. A real blocker or triggered user stop ends incomplete; it does not turn into success. |

The runtime's numeric gate is two for this standalone skill. It does not turn a
request to run a test thirty times into thirty trivial reviews, and a passing
test suite does not replace the two-review requirement.

## What happens in one callback

An active packet is one assigned iteration, not the entire loop. Execute all of
its work before submitting the exact callback supplied by the script:

1. Read the current candidate and the latest seven reachable commit messages in
   full, or every available message if fewer exist.
2. Review the scoped behavior and adjacent consumers that are needed to assess
   it. Treat history and suggestions as context, not permission to change
   unrelated work.
3. Form a bounded plan. An empty plan is valid only after a substantive review
   finds no worthwhile change.
4. Apply an authorized fix when warranted and run meaningful, current checks.
   Do not stop after review or planning if implementation and checks are part of
   the work.
5. Retain the candidate, findings, plan, actual changes, commands and results,
   lessons, and any commit receipt in the host-visible task record. Make an
   authorized commit after applicable checks pass.
6. Send an honest result through the packet's exact `done_argv`, then consume
   the whole returned packet. If it is `active`, execute its new instruction;
   do not stop merely because a test passed or a review was trivial.

The internal report is not user input. It tells the script only what this
completed cycle found:

```json
{
  "classification": "trivial | non-trivial | unresolved",
  "exit_assessment": "satisfied | unsatisfied | unknown",
  "continuation_assessment": "allowed | blocked | cancelled",
  "evidence": "Concise, factual current observations and remaining gap"
}
```

`trivial` means a distinct complete review found only trivial or no changes,
with applicable current checks and no material finding. `non-trivial` means a
material finding or behavioral change occurred, even if the same cycle fixed
it. `unresolved` means required work, evidence, checks, or assessment is
incomplete. A callback, retry, diagnostic command, or repeated test is never a
separate review.

## State, evidence, and discernment

| Responsibility | Owner | Observable result |
|---|---|---|
| Interpret scope, natural-language conditions, and semantic materiality | Executing LLM | A stated contract and evidence-based assessment |
| Execute the current returned instruction | Improve/LLM | One complete review cycle before the callback |
| Preserve current work, conditions, latest report, and trivial streak | One private temporary state file | An active packet can be rehydrated while its file exists |
| Validate action identity, update the count, and select the successor | Until Loop script | An `active`, `complete`, or `stopped` packet |
| Retain detailed review evidence | Host-visible task record and ordinary command output | Candidate, checks, commit, and lessons available for recheck |

New standalone runs deliberately do **not** call the old collector or create
`.until-loop/working.md` or `.until-loop/evidence/`. The temporary state file
holds only the contract, latest concise report, and counter; it is not proof
that a command ran or that a review judgment is sound. The host record carries
the detailed observations. See [callback-evidence.md — required host record and
truthful callback summary](references/callback-evidence.md) for the exact
recording rule and examples.

For example, consider a formatter whose documented blank fallback is broken:

| Review cycle | Actual outcome | Classification | Streak after `done` |
|---|---|---:|---:|
| 1 | Finds the broken behavior, fixes it, adds a relevant regression, and checks it | `non-trivial` | 0 |
| 2 | Completes a new review with no material finding and current checks | `trivial` | 1 |
| 3 | Completes another distinct review with no material finding and current checks | `trivial` | 2, then terminal if the full exit condition is satisfied |

The script chooses the successor after each row. It does not trust a report as
proof, decide whether a test was adequate, or let an agent privately count
several reviews in one callback.

## Scope, commits, and completion

| Question | Default behavior |
|---|---|
| What gets reviewed? | An explicit file list, branch range, or baseline takes precedence. Otherwise freeze initial HEAD and review initial staged, unstaged, and relevant untracked work together with this run's later edits. In a clean tree, disclose the latest commit's change as the default unless context specifies another candidate. |
| What do the seven messages mean? | Read their IDs, subjects, and bodies every cycle. They inform the plan but do not define the diff range, prove current truth, or authorize unrelated changes. |
| What is material? | Behavior fixes, public-contract changes, security or data-integrity corrections, and missing required regression coverage are material. Non-semantic spelling, formatting, or explanatory polish can be trivial only with evidence behavior is unchanged. Investigate uncertainty. |
| When are commits made? | After applicable checks pass, commit authorized changed work with Review, Plan, Changes, Validation, Key learnings, and Remaining work in the body. Preserve unrelated staged and unstaged hunks, including in scoped files. |
| What if a review changes nothing? | Keep an honest host record. Do not manufacture an edit or empty commit. An explicit audit-commit-every-iteration request permits an identified no-change audit commit without unrelated staged content. |
| Can I prohibit commits? | Yes. An explicit no-commit request overrides the default while retaining the review record. Pushing or publishing needs separate authorization. |
| When is it done? | The exit condition, current relevant checks, and every other requested condition are satisfied, with two distinct consecutive qualifying trivial/no-change reviews and no unresolved material finding. Only the runtime accepts the terminal transition. |

If an explicit user stop is triggered, report `cancelled`. If an obstacle blocks
the assigned work without a triggered user stop, report `blocked`. Both outcomes
are incomplete. If required evidence is missing, use `unresolved` rather than
calling the cycle trivial just to advance the count.

## Preview, interruption, and concurrency

“Dry run,” “preview,” “show how you interpret this,” and “do not execute” show
the proposed scope, history window, one-cycle work, exit rule, continuation
rule, incomplete stops, assumptions, evidence needed, and first action. Preview
does not start or resume a run, create the temporary state file, write loop
notes, edit files, execute task checks, stage, or commit. A later execution
request rechecks the current context.

Every new logical run has its own private temporary state file. Separate runs
can therefore have separate callbacks at the same time. They do not coordinate
project edits, test outputs, Git index changes, or commits within one checkout;
use separate worktrees or deliberate coordination for shared repository work.
One file has one caller at a time. If it is missing or corrupt after an
interruption, the new runtime does not reconstruct it or replay work.

An existing durable v1/v2 Improve run is a separate compatibility path. Continue
it only when explicitly selected, using [legacy-standalone.md — old collector
and checked-record binding](references/legacy-standalone.md) and [legacy-skill.md
— matching durable Until Loop adapter](../../references/legacy-skill.md). New
runs do not migrate into or out of that state.

## Installation, release, and verification

This repository bundles Improve as a tested integration example with Until
Loop. Its package-relative card loads the bound `../../SKILL.md`, references,
and scripts, so copying only `SKILL.md` is insufficient. The canonical
`improve@skill-craft-market` package is maintained separately in
[skill-craft](https://github.com/whichguy/skill-craft/blob/improve-v0.1.0-rc.1/skills/improve/README.md);
updating this repository does not itself publish that marketplace package.

From the package root, run:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_improve_policy -v
```

The focused test checks the standalone binding and its policy dependency. The
repository's full deterministic suite additionally checks package generation,
the callback protocol, compatibility adapters, and their isolated consumers. It
does not establish universal model judgment or prove a particular user's
repository has been improved.

- [SKILL.md — standalone binding: full cycle and exact callback](SKILL.md)
- [review-policy.md — reusable review obligations and convergence](references/review-policy.md)
- [callback-evidence.md — host record and callback evidence limits](references/callback-evidence.md)
- [legacy-standalone.md — explicit durable-run compatibility](references/legacy-standalone.md)
- [README.md — Until Loop runtime and release details](../../README.md)
