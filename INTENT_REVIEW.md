# Natural-language until-loop review

Status: implementation and behavioral validation complete; no unresolved
material findings from two independent final reviews. Skill version 0.2.0.
Baseline: `b329cbb32739bf6643ae4e26489cee8cc927019e` (skill 0.1.4).
Requested change: interpret continuation work and exit conditions from ordinary
language, like the user's goal workflow, without requiring fixed user flags.
The earlier runtime audit in AUDIT.md remains scoped to its recorded revision.

## Prompt audit and decisions

### Q1 — Does inference control the work or merely populate arguments?

Info-gain: 0.95. Baseline SKILL.md:60–116 concentrates on interpolation, while
Host loop at 133–150 says to echo a packet and do one increment. The runtime's
packet at scripts/until-loop:493 then told the host to retry a success closer
when verification failed, without reassessing the rest of the condition.

Decision: adopt an explicit Execute / Continue / Success / Early-stop
interpretation, followed by evidence-based action selection and whole-condition
evaluation. The skill interprets, the existing runtime persists. Move argument
mechanics into references/runtime.md. Remove automatic success-retry guidance.

### Q2 — What does a false continuation condition mean?

Info-gain: 0.9. Baseline SKILL.md:78–80 generally treats continuation as the
negation of the terminal predicate. That loses the difference between completed
work and work that cannot proceed because an input is missing.

Decision: check preconditions before work; distinguish success, useful progress,
and incomplete early stops. An already-satisfied request causes no unnecessary
artifact edits. A missing source is not success. Runtime v1 deliberately has no
blocked phase, so the host reports the blocker and leaves the run resumable.

### Q3 — How does interpretation survive a fresh context?

Info-gain: 0.85. Baseline references/state.md stores the original objective but
not the inferred continuation policy. A packet repeats a flattened objective;
relying on the old chat would lose why the next action was selected.

Decision: retain the original request plus the interpreted natural-language
contract as the existing frozen multiline prompt. Store evolving evidence and
gaps in an optional working notebook. Recheck all notebook conclusions against
actual artifacts and accepted state after resume. No new schema or parser.

### Q4 — Should this become a second implementation of /goal?

Info-gain: 0.8. Local goal session artifacts show objective interpretation,
classifier judgments and durable plans, but source for the host lifecycle was
not found in the editable local repositories. The runtime here has no mechanism
to schedule a new turn or independently enforce an LLM's semantic judgment.

Decision: adopt the interaction and evaluation pattern; retain the audited
state machine and finite guard. Do not claim automatic host continuation,
semantic enforcement, or equivalence to a built-in goal lifecycle. No new
plugin, model configuration, service, dependency, or background integration.

## Evidence and alternatives

- Local proof: the existing runtime already preserves arbitrary multiline
  objective text and checks an explicit completion claim separately from the
  verifier. Its locks, recovery, validation and legacy-state handling remain.
- Local goal artifacts: `~/.grok/sessions/%2FUsers%2Fdadleet%2Fsrc/01a05319-b2d8-74a3-9ae2-cb9d3231cdbf/goal/`
  contains a plan, recorded classifier decisions and state. These are observed
  behavior, not a current source-level guarantee about every /goal execution.
- [Anthropic: Building effective agents](https://www.anthropic.com/engineering/building-effective-agents)
  supports model-selected next steps, feedback and explicit stopping boundaries.
- [Anthropic: Effective harnesses for long-running agents](https://www.anthropic.com/engineering/effective-harnesses-for-long-running-agents)
  supports durable progress and testing the actual result rather than trusting
  an agent's early completion claim.
- [Anthropic's harness primitives](https://github.com/anthropics/cwc-long-running-agents/blob/main/README.md)
  illustrate evidence contracts, fresh-context evaluation and handoff notes.
  Contrary evidence: the repository calls itself an unmaintained demo, and
  explicitly distinguishes convention from hook enforcement. We copy no hooks
  and make no claim that instructions alone enforce all semantic judgments.
- Adopt the prompt-layer change: fits the requested interaction with minimal
  runtime churn. Reject a flag-only cosmetic rewrite: it would not improve
  action selection or exit evaluation. Defer a new host scheduler: it adds
  lifecycle complexity outside this request and requires different validation.

## Learnings

1. A semantic continuation condition is not always the negation of success.
2. A passing verifier only establishes its own coverage, not the user's whole
   request. Every required clause needs evidence on the current candidate.
3. Natural-language contracts need durable storage as much as cycle counts do.
4. Repeated packet echoes expose implementation detail without improving the
   user's ability to assess progress; report decisions and evidence instead.
5. A finite safety cap and an interpreted exit condition serve different roles.

## Remediation

| Priority | Change | Status |
|---|---|---|
| High | Natural-language entry and execute/continue/exit reasoning | Implemented |
| High | Whole-condition reassessment; no unconditional success retry | Implemented |
| High | Check before action; separate incomplete early stops from success | Implemented |
| High | Durable contract and evidence-aware cold resume | Implemented |
| Medium | Move CLI details to internal reference; preserve legacy parents | Implemented |
| Medium | Concise progress instead of full packet echo | Implemented |
| High | Real interpretation and resume evaluations, with artifact read-back | All six primary outcome cases passed |

## Validation

Evidence directory: `/Users/dadleet/src/until-loop-intent-20260912/`.
Deterministic runtime checks establish state-machine regressions only. Behavioral
cases in tests/intent-cases.json exercise the revised skill in isolated workspaces;
assertions and reviewer rubrics are withheld from the executing agent. Host smoke
must inspect actual state, files and transcript, not just narrative success.

- All 25 existing runtime tests pass. Default shell suite: 126 checks; optional
  installed-parent suite: 137 checks. Fresh isolated package also passes.
  The shell count changed because brittle prompt-wording assertions were
  replaced by package-route checks and live semantic evaluations; runtime
  behavioral coverage was retained. Native YAML metadata and routes validate.
- Already-satisfied while/until: done at cycle 1, product files byte-unchanged,
  only runtime bookkeeping created.
- Missing required facts: active at cycle 0, no fabricated values, no success
  event, unchanged product files and durable blocker notes.
- Literal option text: exact fenced command matches its reference byte for
  byte; the example's flags did not become runtime controls; done at cycle 1.
- Cold resume: one fresh agent created an active cycle 1 and stopped at the
  requested context boundary; another agent with no inherited conversation
  resumed from disk and reached done at cycle 2. Frozen prompt/predicate
  remained unchanged and history is exactly incomplete then complete, with
  no restart and no duplicated Purpose section.
- Independent review identified missing mandatory blocker notes, an ambiguous
  legacy-control boundary, and a no-follow check needed before notebook reads.
  All were repaired. The follow-up review found no remaining material doc/code
  issues. These checks do not enforce the model's semantic judgments.

Observed fidelity limits are retained, not hidden: one forward agent dropped
the possessive apostrophe in `customer's` while copying the blocked request,
and removed Markdown backticks around section names in the cold setup request.
The agent confirmed these were unnecessary quoting/formatting shortcuts.
Neither changed the intended condition or output, and the literal-command case
preserved every quoted byte, but these runs do not establish universal verbatim
request copying. The saved runtime never rewrote the supplied strings.

Native Grok acceptance on the final source:

- Compound goal: the provided happy-path test was green before changes. The
  host recognized its limited coverage, implemented whitespace trimming and
  the blank-input fallback, updated documentation, and recorded a predicate
  covering all four clauses. Direct calls independently verified whitespace,
  tab, newline and ordinary inputs. Final phase done, cycle 1, verifier exit 0,
  and native process exit 0.
- Qualitative draft: 111 words, blame-free failed-deployment acknowledgment,
  both recovery steps accurately reflected, and a direct deployment-ID request
  as the final sentence. No runtime verifier was configured. The host used
  code-assisted word counts while authoring and qualitative self-review;
  independent read-back checked the result. Final phase done, cycle 1, native
  process exit 0. This is not a claim of purely manual word counting.
- The first compound attempt reached runtime done but its host process exited
  1 at the evaluation harness's 14-turn limit before finishing its response.
  That attempt is retained. A fresh-fixture run with a 32-turn harness allowance
  finished cleanly; the skill's own cycle guard was not changed or bypassed.
- The legacy parent demo reached done in two increments with exact `hi` and
  `2` files. Its source changed during the run, so it is earlier-source
  compatibility evidence only. Both final natural-language cases instead have
  unchanged pre/post hashes for the actual card, adapter, runtime and tests.
  Existing host startup warnings and a post-success demo cleanup warning are
  retained in raw stderr; no model/tool/configuration changes were made.

Evidence paths: `native/runs/`, `forward/verified-results.json`, and
`validation-manifest.json` under the evidence directory. The manifest binds
results to source hashes and the final commit. Raw streams and local host
session traces accompany the native runs; forward artifacts and a pre-resume
snapshot preserve the fresh-context test.

The revised skill also drives this redesign's final closure. Its new run
preserves the prior audit history, stores the natural-language request and
interpreted contract, and requires the scoped source/report commit before the
success closer. Final state/history and packet are retained under `driver-final/`
and `driver-final-packet.txt` after that closer's verifier runs.

Limits: semantic judgments remain model behavior, not mechanically enforced
proof. These six samples do not establish universal language understanding.
The runtime is still same-turn with its existing finite guard; it adds no
background wakeup or host classifier. This change was exercised on macOS;
the earlier AUDIT.md records the broader runtime-platform test history.
