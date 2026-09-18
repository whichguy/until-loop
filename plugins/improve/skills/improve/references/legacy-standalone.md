# Legacy standalone Improve binding

Use this reference only to continue an explicitly selected standalone Improve
run created by the durable v1 or v2 adapter. It preserves the former collector
and checked-record binding for that saved run. It is not an entrypoint for a new
Improve request, and it must not be mixed with the single-file callback runtime.

Read the shared [Improve review policy](review-policy.md), this binding, and the
matching [legacy Until Loop instructions](../../../references/legacy-skill.md)
in full. The legacy adapter remains the only CLI caller for that old run. Do not
initialize over, migrate, delete, or silently resume saved durable state.

## Legacy owner binding

- **History window:** at the start of every completed review cycle, read the
  last seven reachable Git commit messages in full (IDs, subjects, and bodies),
  or all available messages if fewer exist. Re-read that window each cycle and
  retain useful prior lessons in the review record. History is evidence of prior
  intent, not the current diff range, current truth, or permission to undo a
  change.
- **Scope:** retain any named branch range, files, or baseline. Otherwise freeze
  the initial Git HEAD and review the initial staged, unstaged, and relevant
  untracked changes together with this run's later edits. If clean, use the
  latest commit's change as a disclosed default unless context establishes a
  more specific candidate. Inspect adjacent consumers only as needed to assess
  the candidate. In an unborn repository, disclose that history is absent;
  execution that requires commits remains incomplete until that constraint is
  resolved.
- **Trivial classification:** classify impact semantically. Trivial work is
  non-semantic spelling, formatting, or explanatory polish with evidence that
  behavior is unchanged. A one-line bug fix, public-contract change,
  security/data-integrity correction, or missing required regression coverage
  is material. Uncertain impact remains unresolved until investigated.
- **Evidence location:** retain the policy-required record for each distinct
  review in the selected durable run's checked `.until-loop/working.md`. Treat
  it as candidate-bound evidence to recheck, not a runtime-enforced streak
  counter. Follow [factual evidence capture](evidence-capture.md): use the
  bundled `../scripts/capture_evidence.py` helper to retain candidate/history
  facts before review and check references before assessment. Read the captured
  full messages, label reviewer identity and check claims honestly, and keep
  semantic judgments in the review record. Report and investigate collection
  failures without inventing evidence.
- **Commit policy:** after required checks pass, commit authorized scoped files
  changed by a completed iteration with the required learning-oriented record.
  Its body must include Review, Plan, Changes, Validation, Key learnings, and
  Remaining work, including the classification and resulting streak. A no-change
  review gets a durable note, not a manufactured edit or empty commit. An
  explicit audit-commit-every-iteration request requires one authorized audit
  record commit for every completed review; a no-change review uses an identified
  empty audit commit with no unrelated staged content. An explicit no-commit
  request preserves the records without committing. Never reset the user's index
  or absorb unrelated staged or unstaged work.
- **Phase and callback:** the selected durable adapter owns the current packet,
  retries, and legal phase transitions. Complete only the packet's assigned
  phase and return its evidence-based assessment through that adapter; do not
  treat a callback, retry, or verification run as another review.
- **Finalization:** only the selected durable adapter may accept completion of
  the whole bound contract. A blocker, requested stop, exhausted budget, failed
  required commit, stale check, or unresolved evidence remains incomplete rather
  than satisfying the review policy.

## Legacy preview and handoff

For an explicit preview of the selected durable workflow, inspect permitted
repository context read-only, derive the shared policy plus this binding into
the legacy adapter's proposed contract, and follow that adapter's preview
procedure. Stop after presenting it. Do not initialize, resume, revise, or
submit a loop; write loop notes; edit product files; execute task tests or
verifiers; stage changes; or create a commit. A preview neither achieves the
objective nor authorizes later execution.

For Git inspection during preview, use the documented no-optional-locks,
no-index-refresh, no-external-diff, and no-text-conversion options so status
and working-tree reads do not change the index or invoke configured external
processors. Preserve the distinction between the chosen edit scope and adjacent
files inspected for context.

When continuing an old run, preserve the shared policy and every binding above
in its existing durable contract, including conditional commit overrides and
negative constraints. Follow its current packet and submit only the selected
legacy adapter's evidence-based assessment. Do not replace its collector,
working record, contract, or state with callback-era behavior. The newer
`callback-evidence.md` reference is for new runs only.
