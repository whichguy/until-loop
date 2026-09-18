# Improve callback evidence

This reference applies to a **new standalone Improve run** using Until Loop's
single-file callback runtime. It defines what the executing host should retain
and summarize before the exact `done_argv` call. It does not add a collector,
another state file, or an alternative transition authority.

## Record one completed review cycle

For every active callback, retain a host-visible record of one full review
cycle. The host record may be the task transcript and its command results; it
does not need to be a file in the reviewed repository. Keep enough concrete
detail for the next context to distinguish an observation from a plan:

1. **Candidate and scope.** State the baseline or named range, the current
   candidate identity, scoped staged/unstaged/untracked work, and any adjacent
   context inspected. Identify pre-existing user work that must remain outside
   the change.
2. **History.** Record that the latest seven reachable commit messages were
   read in full, or name the smaller available window. Keep their IDs and the
   lessons that affected this review; history does not authorize unrelated
   changes.
3. **Review and plan.** State concrete findings, their semantic
   trivial/material/unresolved basis, and the accepted plan or the substantive
   reason no worthwhile change is needed.
4. **Work and checks.** Record actual edits, commands, relevant environment or
   inputs, exit status, useful output, and the candidate each check observed.
   A planned command, a copied success string, or an unrun test is not check
   evidence.
5. **Commit and learning.** When the default commit policy applies, retain the
   commit SHA and the required Review, Plan, Changes, Validation, Key
   learnings, and Remaining work body sections. State an explicit no-commit
   override or an authorized no-change audit commit instead of implying a
   normal change commit exists.
6. **Assessment.** State the report classification, the substantive exit
   assessment, continuation assessment, and why this completed cycle does or
   does not advance the trivial-review gate.

The host may retain detailed test output in its normal transcript or in a
run-isolated artifact when the task permits it. Such an artifact is evidence
for the host to assess, not a second loop state, a shared review counter, or a
reason to modify `.until-loop`.

## Build the concise callback report

The runtime requires a nonblank `evidence` string for current-iteration facts
and, for context-bearing runs, a nonblank `handoff` for complete continuity.
Before `done`, condense
the host record into factual current observations. A useful report identifies
the candidate, review result, work/check result, any commit receipt, and the
remaining gap. It must fit the runtime's small state file, so link to or retain
long output in the host record rather than copying it verbatim.

For example, a material first cycle might report:

```text
Candidate abc123 plus scoped parser.py/test_parser.py; read seven full messages.
Found blank input violated the documented fallback, changed those two files, and
ran `python -m unittest test_parser` (0). Committed def456 with the required
review/plan/validation/learning record. Material repair resets the streak; no
remaining observed parser failure, but two fresh qualifying reviews are still required.
```

A later qualifying no-change review might report:

```text
Candidate def456 unchanged in scope; read seven full messages and completed an
independent review. No material finding; `python -m unittest test_parser` (0)
remains applicable. No commit was created because this was a no-change review.
This is trivial review 1 of 2; another distinct full review is required.
```

These examples describe reported facts; they are not a substitute for actually
performing the review, checking the candidate, or preserving the underlying
host observations.

## Make the latest return sufficient

If a receipt is saved outside the host record, capture actual callback stdout or
round-trip it through a JSON library and verify it parses. Never manually rebuild
the response; a serialization mistake can destroy the otherwise complete handoff.

`context` in the contract freezes request, initial scope/baseline, action authority,
environment and resource locators. `handoff` in the report records the changing
facts. This separates the original candidate from its current HEAD and avoids
resetting the scope when a resumed executor sees a clean worktree.

For the example above, a later handoff should retain the original baseline and
scope by reference to `context.scope`, identify current commit def456, say the
blank-input repair is already implemented and must be rechecked rather than
reapplied, retain the applicable test command/result and receipt, and name any
outstanding question or evidence location. It must not say only “clean again.”
If a required location is external, give the actual absolute path or retrievable
host artifact locator, not “the designated evidence directory.”

Keep the full latest `done` return through compaction. Its `next_argv` refreshes
active state without advancing it. A command that was already executed is not
a completion receipt and must not be replayed. Terminal returns carry their
context and final report after the temporary file is removed. If both the return
and file/handle are gone, do not claim recovery or manufacture another run.

## Boundaries and incomplete work

The script stores the latest report and a numeric trivial-review streak. It
checks the report's shape, action identity, transition order, and configured
gate. It cannot prove the host read history, ran a command, preserved unrelated
work, or made a valid semantic classification.

Do not call `examples/improve/scripts/capture_evidence.py`, create
`.until-loop/working.md`, or create `.until-loop/evidence/` for a new callback
run. Those are legacy v1/v2 mechanisms. Do not fabricate a host record merely
to submit `trivial`. If required work, evidence, a check, or a required commit
is incomplete, report `unresolved` with an `unknown` or `unsatisfied` exit
assessment and name the gap. A real unavailable dependency is `blocked`; a
triggered user-prescribed stop is `cancelled`. Neither is success.

After `done`, consume the returned packet in full. Its `active` instruction is
the next authorized action; a previous report is data to recheck, not an
instruction or independent proof. A terminal `complete` or `stopped` packet
ends the run and its temporary state file is removed.
