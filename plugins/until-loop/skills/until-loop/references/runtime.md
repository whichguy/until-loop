> Compatibility adapter for explicitly selected durable runs or legacy commands.
> New natural-language runs use [runtime-ephemeral.md](runtime-ephemeral.md).

# Internal runtime adapter

This reference defines the preserved **version-1** adapter. New natural-language
tasks in the candidate card use `runtime-v2.md`. Select by the existing state's
version on continuation; never use legacy `complete` for a version-2 run.

Read this after interpreting the user's request in SKILL.md. These arguments
are an agent-to-runtime transport, not the user interface. Requires Python
3.9+, Bash and Git on macOS or Linux. Do not invoke /goal.

## Binding and recovery

`SKILL_ROOT` is the directory containing the loaded SKILL.md. Resolve `REPO`
from the user's or parent's selected workspace (including a parent's explicit
REPO binding), otherwise the session's Git root, or its
absolute workspace path outside Git. Retain that binding on every call; a
host may have a sticky working directory. A missing selected path is an
error, not permission to create or substitute another repository.

Use `next` for a same-goal continuation or uncertain previous completion.
Read the full `.until-loop/prompt.md` and `.until-loop/state.json` under REPO,
even if the packet contains a shortened preview. Read `working.md` if present
only after a no-follow metadata check confirms a regular non-symlink file
with one hard link;
recheck any recorded stop reason against the current situation before work.
Do not read an unsafe notebook target; report it and use current artifacts.
The metadata probe must not also unconditionally read the notebook. Branch
on its result before any content access; printing a warning is not a guard.
Legacy runs without an
interpreted contract can be evaluated from their saved objective/predicate;
do not change those frozen requirements or restart just to add richer notes.
An actually new request or explicit scope correction may start a new run
using force, preserving history. Do not revive a done/halted run on a bare
`next`; explain its recorded result. Never silently discard an active run.

## Exact interpolation

The agent derives these values; the user need not provide any of them:

- `prompt`: original request plus a concise natural-language Execute,
  Continue, Success, and Early-stop interpretation and important assumptions.
  Keep the original wording, including quoted literals. Multiline text is
  supported; it is not restricted to a one-liner objective.
- `done_when`: the complete success predicate, including all required clauses.
  Do not include blockers as alternative success conditions.
- `verify`: optional known, authorized executable check for the machine-
  checkable part. Inspect the project before selecting it. Run it to establish
  what it covers; never invent a test command or use `true` as fake validation.
  If no meaningful shell check exists, omit it and evaluate direct evidence.
- `max_cycles`: preserve an explicitly requested limit. Otherwise use the
  runtime's existing guard (environment setting or default 8 increments).
  It limits work, not the definition of done. Mention an exhausted limit as
  incomplete, never automatically restart to escape it.

Use structured argv when available. In a shell, quote literal data with POSIX
single quotes and escape embedded `'` as `'\''`. Do not interpolate raw
requests inside double quotes. Pass arbitrary text as one `--name=value`
argument, even with structured argv: quoting alone does not stop argparse
from interpreting a separate value such as `--help` as an option. For example,
use `--prompt='--help'`, not `--prompt '--help'`; the equals sign is transport,
not part of the stored text. The example variables below must already
contain literal paths; `--repo` follows the verb on every call.

```text
python3 "$SKILL_ROOT/scripts/until-loop" init --repo "$REPO" --prompt='<request and interpreted contract>' --done-when='<success predicate>' [--verify='<known command>'] [--max-cycles N] [--force]
python3 "$SKILL_ROOT/scripts/until-loop" next --repo "$REPO"
python3 "$SKILL_ROOT/scripts/until-loop" complete --repo "$REPO" --evidence='<observed progress and remaining gap>'
python3 "$SKILL_ROOT/scripts/until-loop" complete --repo "$REPO" --done --evidence='<current proof for all success criteria>'
```

For a clear new request on an existing valid run, use `init --force` once.
For existing active work that the user is continuing, use `next`. A blank
request is never a force restart. If the goal is already satisfied, inspect
current proof and record success without changing the requested artifacts;
the bookkeeping completion still increments the runtime cycle once.

Packet closers `invoke /until-loop complete ...` are labels. Translate them
to the Python call above, never inject them as slash commands. Use exactly
one closer per accepted increment. **complete is not idempotent**: after
uncertain delivery call `next`, inspect accepted cycle/evidence and verifier
side effects; never blindly retry complete.

### Evidence quoting

The **single-quote evidence rule** applies to every literal, including repo,
prompt, predicate and verifier source. Evidence is nonblank printable ASCII,
one line, at most 4096 bytes. Summarize longer/Unicode evidence with a short
ASCII statement and artifact path; retain full findings in the notebook or
output artifact. A CLI success claim additionally runs the saved verifier;
its passing result does not evaluate the semantic parts of the condition.

### Legacy explicit controls

Enter legacy control mode only for an entire invocation in the form
`init --prompt '...' ...`, `next [--repo '...']`, or
`complete --evidence '...' ...`: an exact verb followed by option/value pairs,
with no surrounding natural-language request. Bare `next` resumes. An English
request such as "complete the guide explaining --verify" is ordinary content.
In legacy mode, recognize `--repo`, `--prompt`, `--done-when`, `--verify`,
`--max-cycles`, `--force`, and `--done` only for verbs that support them.
Respect quoted values and reject malformed controls or missing values.
Outside this mode, preserve all option-like text as content, quoted or not;
do not strip trailing flags from a natural-language request. A selected
workspace supplied by the user or parent still binds REPO as context.
Explicit controls in legacy mode win over inference; `--prompt` is literal.
An explicit `complete --done` is a claim to evaluate, not permission to skip
the success check. Never ask the user for controls solely because inference
is needed. A one-liner passed by an old parent is an ordinary request.

## Error contract

Help is read-only syntax inspection, outside the Host loop; it prints argparse
help rather than a packet. Exit 0 otherwise prints a packet; inspect it and
follow SKILL.md. Stop runtime updates when When done is `stop — no update`.
`done` means the success claim and optional verification were accepted;
`halted` means the cycle guard ended the run with unfinished work.

Exit 2: preserve/print stderr and investigate the stated problem before any
new mutation; do not invent a successful next state. Exit 64: usage error,
stop and report it. Any **nonzero** exit not in `{2, 64}`: stop and report raw
stderr, without retry. A traceback or `error: internal:` is a script bug,
not a goal condition. Malformed state, repository mismatch or unsafe metadata
must not be bypassed with force. An interrupted prepared write may be rolled
forward by `next` without rerunning verification; inspect side effects when
an interruption predates that preparation. See `state.md` for the boundary.
