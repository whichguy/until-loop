# Test Until Loop’s exit criteria

## Run the offline regressions

From the Until Loop repository root on macOS or Linux, run the existing test entry point with Python 3.9 or later, Bash and Git on your path.

```sh
PYTHONDONTWRITEBYTECODE=1 bash tests/until-loop.test.sh
```

Expect exit 0, the Python suite’s `OK`, and `PASS=11 FAIL=0` from the shell checks. The suite uses temporary workspaces. Its five-criterion test runs all 32 combinations through the real verifier and a `done` callback that claims success every time. For each of the 31 incomplete combinations it asserts the exact ordered results and an `active` packet. Only five passing checks produce `complete`. [Truth-table test](test_exit_checks.py)

| Scenario | Expected observation |
| --- | --- |
| Five registered criteria in ordinary session and receipt modes | Three fail initially. Repairing two leaves one failure. Repairing that one while breaking a previously passing check still yields `active`. After the final repair, an early `done` is refused. Fresh verification followed by `done` returns `complete` with no further action. |
| Eight or twelve checks, including a failing final check | Every scattered failure appears in the recorded results. A fresh all-pass run completes. |
| Five permitted alternatives or five reasons to stop | Any one alternative satisfies its OR check. With all readiness checks green, each explicit cancellation yields `stopped`. The same fixture completes when the caller reports `allowed`, so detecting the stop belongs to the executor. |
| Child finishes a prerequisite | The child's callback returns the active parent. The parent's remaining manifest check holds it open until repaired and verified. |
| Codex compaction and resume during partial repair | The recovery adapter returns the five recorded results and current action without changing the session file. After completion, resume emits a terminal notice. The verifier and `done` own the completion decision. |
| Six release checks bound to a digest, plus a delivery selection check | A failing field or a different selected revision prevents completion. The fixture also checks identity at delivery, rejecting changed bytes or a changed selection after completion. |

The chain, alternative and hook assertions are in the [session tests](test_session_stack.py), [condition composition tests](test_condition_composition.py) and [recovery tests](test_recovery_hooks.py). Artifact selection and delivery in the [release fixture](test_artifact_delivery.py) are protections supplied by that fixture.

## Measure checks written by a real Codex session

Use a signed-in Codex CLI with access to `gpt-6.2-sol` and support for `codex sandbox -P :workspace`. Each `live` invocation starts a fresh session at medium effort, confirms the model and effort from the CLI header, and saves the terminal decision, evidence, repairs and exact final commands. Each `replay` executes those same commands on **every variant in that family** using the original workspace path. Replay uses the local sandbox and makes no model call.

```sh
evidence_dir=$(mktemp -d)
python3 -B tests/qualify_exit_checks.py live contacts valid "$evidence_dir/a"
python3 -B tests/qualify_exit_checks.py replay "$evidence_dir/a"
python3 -B tests/qualify_exit_checks.py live release distribution-hold "$evidence_dir/b"
python3 -B tests/qualify_exit_checks.py replay "$evidence_dir/b"
```

Choose a fresh, neutrally named directory for each live audit so its path gives the model no hint about the expected outcome. Keep it at its original location for replay. Each replay writes separate logs and a `replay.json`. Use `capture.json` for the live decision and the model's evidence. A correctly stopped audit can still have a gap in its recorded checks.

| Family | Expected live decision | Seed cases |
| --- | --- | --- |
| Contacts | `complete` | `valid`, `paraphrased` |
| Contacts | `stopped` and evidence naming the incorrect behavior or documentation | `docs-last-wins`, `code-last-wins`, `code-strips-phone`, `code-skips-missing` |
| Release | `complete` | `valid`, `yaml-only`, `disabled-no-report`, `paraphrased-rollback`, `different-matching-build` |
| Release | `stopped` and evidence naming the unmet readiness requirement | `wrong-build`, `enabled-no-report`, `wrong-digest`, `rollback-negated`, `rollback-omitted`, `critical-finding` |
| Release | `stopped` with `cancelled`, even though readiness passes | `distribution-hold` |

The [fixture catalog](acceptance_cases.py) declares each expected reason. `live` compares the terminal status and checks cancellation for the explicit hold. Read `last_report.evidence` alongside the expected reason to confirm the decision was reached on the right grounds.

| Replay verdict | Interpretation |
| --- | --- |
| `PASS` | Every registered command passed a valid readiness control. The distribution hold is a separate decision exercised by the live audit. |
| `DIFFERENTIATED` | At least one command rejected this defective variant and passed every valid readiness control. Inspect its log to confirm it failed for the intended reason. |
| `FALSE_PASS` / `FALSE_REFUSAL` | Every command accepted a defective fixture, or a command rejected a valid one. A live executor may still catch an omitted clause by inspection. |
| `UNRESOLVED` / `UNCOVERED` | Only commands that also failed a valid control rejected the defect, or there were no commands to evaluate. |
| `CHECK_ERROR` | A command timed out, died by signal, or returned a shell status indicating it could not be started. Consult its log. |

For example, a duplicate-email check that crashes on a nonexistent CSV field in both the working and broken importer receives no credit for catching the duplicate defect. If it only crashes on the defective fixture, the differential result still needs a diagnostic review. The [qualification tests](test_check_qualification.py) exercise the grader with broken and overly strict checks.

Both commands return 0 when their automated expectations are met, 1 for a measured mismatch or coverage gap, and 2 for an incomplete harness run. Preserve the evidence from an incomplete run. Start a new live audit in a fresh directory, or repeat replay after resolving the reported issue.

## Probe a workspace that changes during verification

```sh
python3 -B tests/test_artifact_delivery.py --probe --strict
```

The probe coordinates a writer and the real verifier with two FIFOs. The sixth criterion starts false. After five commands have read their inputs, the writer makes the first criterion false **before** fixing the sixth. At least one criterion is false at every stage.

With Until Loop 0.11.0, the output shows `invalid_by_stage` as `[["C6"], ["C1", "C6"], ["C1"]]`, `verify_stdout` ending with `UNTIL-LOOP-RECORD-ERROR` after `C1`–`C6` exited 0, `recorded_passes: []`, `verify_exit` `2`, `status: error`, and `complete_implies_all_clauses_held_together: true`. Strict mode exits **0** for that recognized refusal, `1` if completion ever occurs despite the mutation, and `2` for an unexpected response or incomplete probe. Omit `--strict` to collect the same JSON observation without failing on a regression.

## Run the standalone relocated-plugin E2E harness

```sh
python3 -B tests/harness_e2e.py
```

The harness copies only the generated `plugins/until-loop` directory into a fresh temporary path containing spaces, strips repository `PYTHONPATH` and session overrides, and runs five end-to-end scenarios across Codex, Claude Code, and Grok hook manifests, multi-statement checks, vacuous repair rejection, nested parent/child unwinding, and mid-verify Git mutations. `tests/qualify_exit_checks.py live` also accepts `--host` (`codex`, `claude`, `grok`), `--model`, and `--effort` to evaluate different drivers.
