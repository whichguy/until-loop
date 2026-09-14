# Improve and until-loop experiment execution plan

```mermaid
flowchart LR
    Freeze[Freeze inputs and expected properties] --> Run[Run isolated trials]
    Run --> Judge[Compare decisions and actual artifacts]
    Judge --> Triage[Separate defects from fixture limitations]
    Triage --> Fix[Implement supported improvements]
    Fix --> Verify[Run regressions and independent review]
```

This executes `IMPROVE_NEXT_EXPERIMENTS.md` at a bounded screening scope. All
seven families will run in this task. A failed hypothesis is a useful result,
not permission to erase a trial or weaken its expected behavior. The script
continues to enforce protocol and collect facts; the LLM judges semantic truth.

## Baseline and ownership

- Candidate: this checkout, branch `codex/until-loop-rubric-v2`, initial HEAD
  `7d24bbca20576fad673768ca0562027a6c3cd563`, including existing uncommitted work.
- Freeze input hashes and original files before changes. Preserve the concurrent
  shared-policy extraction and the separate installed Grok baseline.
- External evidence root:
  `/Users/dadleet/src/until-loop-v2-validation/experiments-20260914/`.
- Root coordination follows the existing v1 run's authorized new-task restart.
  New execution fixtures use the actual v2 adapter. Decision-only probes never
  claim to have executed an improvement loop.
- Model probes receive frozen input and no expected answer. Oracle files live
  outside their assigned workspace. This is instruction-level separation, not
  an operating-system sandbox. Provider failures and invalid fixtures remain
  visible in the result ledger.

## Work and acceptance matrix

| Workstream | Planned execution | Decisive observations |
|---|---|---|
| Trusted test results | Replace summary parsing with structured `TestResult` output; regress ordinary, empty, skipped, noisy, failed, missing/malformed and interrupted results | Diagnostic text cannot change acceptance; absent or invalid structured evidence fails |
| Evidence capture | Small Improve helper collecting action/revision, candidate and index identity, full owner-selected history, check references and declared reviewer role | Capture precedes assessments; stale/missing/unbound facts remain explicit; no semantic counter or runtime mutation |
| 1. Uncertainty and paraphrases | Twelve cases (six equivalent prompt pairs) twice: 24 separate fresh model contexts | Per-criterion satisfied/unsatisfied/unknown, justified next work, preserved obligations; retain first response |
| 2. Commit before notes | Real repair commit; controlled stop before iteration notes or assessment; fresh host resumes | Original commit reused, missing review evidence not invented, recovery not counted as a clean review |
| 3. Overlapping user work | Stop after a repair plan; evaluator introduces identifiable staged and unstaged hunks in the same scoped file; fresh host continues | Both user hunks and staged content preserved; only agent-owned changes committed; candidate evidence refreshed |
| 4. Blinded discovery | Three defective/clean pairs across CSV, config and persistence; fresh read-only review and plan | Requirement-backed defect detection and false material findings reported separately; visible tests and external oracles verify fixture ground truth |
| 5. Reviewer value | Matched self-review and blinded-review/triage arms across defect and clean cases, plus controlled redundant and wrong advice | Validated detection gain versus unsupported edits/resets; genuine reviewer observations distinguished from evaluator-authored advice |
| 6. Runtime sequences | Reproducible seeded combinations plus isolated fault injection around durable transition/verification boundaries | Replay and stale action invariants, authorized resume, no duplicate verifier side effect, recovery survives combined operations |
| 7. Consumer bindings | Two owner bindings with differing history windows and phase/callback/finalization authority; two phrasings each | No standalone-adapter or history-window leakage into another owner's phase |
| Clean full-workflow control | Actual new v2 Improve run on an already correct candidate | Two substantive no-change reviews, no manufactured edits or empty commits, current evidence |

## Sequence and implementation gates

1. Run the current baseline, freeze inputs, prepare fixtures, and repair the
   already reproduced test-counting defect in parallel with evidence-helper and
   generated-runtime work.
2. Freeze the decision/discovery inputs before viewing model responses. Run the
   fresh-context probes and real Git workflow checkpoints. Preserve outputs and
   artifacts before evaluator feedback. Pilot the evidence helper in execution
   trials when available; record any case that predates it separately.
3. Grade factual properties mechanically and review semantic decisions against
   the requirements. Accept multiple safe next actions. Separate solver claims
   from independently observed state. Record ambiguity instead of manufacturing
   a binary pass.
4. Make narrow improvements for reproduced failure classes. Re-run the failing
   example and a held-out variation when changing instruction behavior. Do not
   add production states, numeric semantic scores, mandatory reviewer teams or
   new dependencies without evidence of benefit.
5. Run meaningful affected tests, then the package suite and independent review.
   Recheck installed bindings, source hashes, baseline preservation and current
   documentation. Finish the root loop only when every row has a retained
   outcome and all adopted changes are verified.

## Reporting

Record raw numerator/denominator results, false completion, defect misses,
unsupported material work, correct uncertainty, unnecessary blockers, duplicate
side effects, user-work loss, and retrospective evidence repairs. Report time,
tool and token use only if exposed; otherwise use null. Distinguish model
decisions, actual execution, synthetic checker fixtures and runtime fault tests.
These small samples identify failure classes; they do not estimate reliability
across hosts, models, repositories or arbitrary hostile code.

Final artifacts will include an experiment report, machine-readable results
index, current source hashes, regression results and adopted/deferred decisions.
Historical reports retain their original results and limitations.
