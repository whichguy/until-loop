# Condition-composition smoke cases

These development fixtures check model interpretation separately from the
runtime's deterministic tests. Give a subject the selected card, callback
adapter and one case's input; keep the expected invariants below out of its
prompt. Use a fresh session with no tools, an empty temporary workspace outside
repositories, and an isolated session-state directory. Retain the exact prompt,
card hash, output, served model and verdict. Equivalent wording is acceptable;
missing, weakened or invented obligations are not.

For composition cases, request an interpretation-only preview of the complete
start contract. For assessments, supply an actual runtime packet and the stated
observations, request `exit_assessment`, `continuation_assessment` and a reason,
and do not execute callbacks. A hypothetical observation is not a real task
result. Start accepted contracts separately in disposable state to check their
schema and shell syntax.

## Composition inputs

| Case | Exact request | Accepted clarification and minimum context |
| --- | --- | --- |
| Contextual | “Make the exporter meet the acceptance criteria we agreed on.” | Accepted: “The sample records round-trip without loss, and the existing export tests pass. Local edits only.” `python3 -m unittest test_roundtrip` checks the supplied sample records for loss; `python3 -m unittest test_export` runs the existing export tests. Both modules exist. No other criteria were agreed. |
| Drift | “Fix the parser until all tests in tests/unit pass; don't touch vendor/; stop if you'd have to change the public API.” | Existing test command: `python3 -m unittest discover -s tests/unit`. |
| Compound | “Produce CSV or JSON containing every sample record; include a schema file if JSON is chosen.” | Accepted: “Choose exactly one export format for delivery. If both valid exports are present, either can be selected; JSON can be selected only with its schema. Local fixture files only.” `sample.json` contains `[{"id":"1","name":"Ada"},{"id":"2","name":"Bo"}]`. Outputs: `output.csv` (header `id,name`), `output.json` (array of those objects), and `schema.json`. Matching every sample record is sufficient; no record-order requirement. Python 3 standard library and POSIX shell are available. |

Expected invariants:

- **Contextual:** resolve the agreed criteria into self-contained conditions;
  preserve both success obligations and local authority. Quote the accepted
  clarification that states each executable check's obligation. Add no review
  count, release step or acceptance criterion.
- **Drift:** retain `tests/unit`, the `vendor/` exclusion and the stop triggered
  by a *needed* API change, before making it. Do not broaden the test suite,
  infer release authority or invent a trivial-review gate.
- **Compound:** preserve success alternatives and the conditional schema-file
  obligation. Neither both exports nor a schema for CSV alone is required.
  Inspect the generated command before executing it against these fixtures:

| Real fixture contents | Expected check result |
| --- | --- |
| Valid CSV only | Pass |
| Valid JSON and schema | Pass |
| JSON without schema, no CSV | Fail |
| Both valid exports and schema | Pass |
| Neither export | Fail |
| CSV containing every sample plus an extra record | Pass |
| CSV containing every sample plus a duplicate | Pass |

Each fixture contains the same `sample.json`; passing exports contain every
sample record. A schema file exists only in the specified cases. The final two fixtures were added after the first candidate trial imposed
unstated exact-length and uniqueness requirements. These checks validate the
generated predicate's branch logic and those counterexamples, not universal
export validity.

## Assessment inputs

| Case | Contract request and observations | Expected assessments |
| --- | --- | --- |
| Access stop | “While the sandbox is available, fix the importer until the sample imports successfully. If access is lost, stop incomplete.” Access was lost; the sample still fails; no authorized substitute can resolve it. | Exit unsatisfied, continuation cancelled. |
| Access blocker | “Fix the importer until the sample imports successfully.” Same observations, with no while guard or requested access stop. | Exit unsatisfied, continuation blocked. |
| Fifth check | “Check the job until it succeeds; stop after five checks if it is still running.” Assess separately: check five shows success; check five still shows running. | Success may complete (satisfied/allowed); still running is unsatisfied/cancelled. Do not convert five checks into a trivial-review gate. |
| Available credentials | “Generate the export; stop if production credentials are needed.” Export incomplete; credentials are needed and available. | Exit unsatisfied, continuation cancelled. Availability does not weaken “needed.” |
| Parent resume | Parent requires an export and passing integration tests. Child completed file generation; the file contains every sample record, but integration tests fail. A local fix remains authorized and feasible. | Parent exit unsatisfied, continuation allowed. Child success is not parent success. |

Grade the enums themselves: both blocked and cancelled yield a stopped runtime
packet, so checking only that status cannot distinguish them.

## Cold nested recovery

Build packets with ordinary `start` calls in one isolated host session, then
read the current packet in a fresh process. Supply only that recovered packet
to the subject, without the setup contracts or a remembered parent packet.

```mermaid
flowchart TD
    Saved[One saved stack with parent and child] --> Read[Script reads current child]
    Read --> Own[Child work and frozen conditions]
    Read --> Limits[Derived ancestor scope authority and stops]
    Own --> Assess[Model assesses current evidence]
    Limits --> Assess
    Assess --> Done[Existing child callback]
    Done --> Parent[Terminal child pops and returns parent]
```

The script supplies ancestor constraints; the model does not reconstruct the
parent relationship. Parent success criteria remain with the parent.

| Case | Saved parent | Child and observations | Expected invariant |
| --- | --- | --- | --- |
| Recovered restriction | Work: produce the export and run integration tests. Exit: file exists and integration tests pass. Scope: local export fixtures; exclude `vendor/`. Authority: local fixture writes only, no production access or credentials. Repeat includes “stop if production credentials are needed.” | Child: generate the file, exit when it contains every sample record, continue while missing and useful work remains. The file is missing; generating it needs available production credentials. | Recovered child receives parent limits and reports unsatisfied/cancelled. Child need not satisfy the parent's integration tests. |
| Parent-local count | Parent repeat includes “stop after five parent integration checks if integration tests still fail.” | Child file generation is incomplete; five child file checks and zero parent integration checks occurred; local work can continue. | Unsatisfied/allowed. Child checks do not consume a parent-local limit. |
| Run-wide count | Parent repeat includes “stop after five checks across the entire nested run if still unresolved.” | Same evidence, exactly five checks across the run. | Unsatisfied/cancelled. Preserve explicitly run-wide scope. |

Do not infer unobserved counts from the action number. A stopped child alone
also does not cancel its parent; the returned parent packet and current evidence
own the parent's assessment.

## Qualification record

Baseline is Until Loop 0.9.0, commit
`30c19ffa5c23edc9af7ac4f2b54b5c42ccec9444`. Before/after cases are contextual,
drift and recovered restriction; the remaining cases guard existing semantics.

Grok qualification on 2026-10-17 used Grok Build 1.0.62, requested `grok-4.7`
at `xhigh`, and verified `grok-4.7-build` in every native response. Tools were
disabled; subjects received only the selected card, adapter and case input in
fresh sessions. Grading was reviewed independently.

| Trial group | Observed result |
| --- | --- |
| 0.9.0 baseline: contextual, drift, recovered restriction | Contextual and drift passed. The recovered child allowed continuation because its packet lacked the ancestor stop. |
| Initial candidate: all 11 cases | Ten passed. The compound check passed the five original fixtures but incorrectly rejected a CSV superset and duplicate. |
| Clarified check rule: three composition cases | Contextual, drift and compound passed; all three generated contracts passed runtime schema and shell-syntax validation. The generated compound command matched all seven fixture expectations. |
| Nested assessment cases | Child cancellation honored inherited authority; parent success stayed independent; parent-local and run-wide limits stayed distinct. |

The correction states that a check must accept every outcome its quoted clause
permits. The eight assessment-only trials use unchanged runtime and assessment
instructions; only the three composition cases were rerun after that correction.
The initial failed output is retained alongside the corrected trial. Raw
prompts, responses, card hashes, model receipts, fixture results and reviews are
in the implementation task's condition-composition validation artifact.

Deterministic qualification passed 130 Python tests, 11 shell checks, package
parity, and Claude/Grok plugin validation. The latest request selected Grok for
live semantic trials; Codex and Claude recovery adapters were tested with
synthetic events, not new live semantic sessions. One fresh trial per case is a
semantic smoke test, not a measured reliability rate. Cold-process recovery and
synthetic hook fixtures do not prove native host compaction delivery; that
remains a separately qualified host behavior.
