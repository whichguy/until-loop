---
name: until-loop
description: >-
  Same-turn until-loop without /goal. User slash: /until-loop.
  Parent skills load this card; this card execs the CLI. Parents must
  not type /until-loop or /goal and must not exec the script themselves.
  The script is the state machine. Do not invoke /goal.
allowed-tools: all
disable-model-invocation: true
user-invocable: true
argument-hint: "<one-liner> | next | complete --evidence '…' [--done]"
version: 0.1.4
license: MIT
platforms:
  - linux
  - macos
metadata:
  skill_craft:
    kind: script-backed
---

# until-loop

Same-turn until-loop without `/goal`. This card is the loop. The script owns
state and the next prompt. Packet/state contracts:
`references/packet.md`, `references/state.md`.

## When to use

- User typed `/until-loop` (or `/until-loop next` / `/until-loop complete …`).
- A parent skill has loaded this card (read this file) and is following
  Host loop. This card execs the CLI; the parent does not.

## Not for

ShipLoop, DevLoop, improve-loop, review-coverage, `/goal`, worktrees, DAGs.
Those products must not type `/until-loop` either — if they want this loop,
they load this card (see Parent skills). This card is the only CLI caller.

## Setup

`SKILL_ROOT` = directory containing this `SKILL.md`. Use the full Python
commands below; no shell command variable is required. Requires Python 3.9+,
Bash and Git on macOS or Linux.
If needed, inspect `--help` after loading this card. Help is read-only syntax
inspection, outside Host loop, and prints argparse help rather than a packet.

`REPO` = user `--repo` if present, else `git rev-parse --show-toplevel` from the
session workspace at invoke; outside Git, use that workspace's absolute path.
Pass `--repo "$REPO"` on every CLI call. Do not
omit `--repo` (Grok sticky cwd). Present-but-empty `--repo` (`""` or whitespace)
is CLI exit 64.

## Verbs

- empty / `next` → `next`
- `complete …` → exactly one closer from the current packet’s When done
- else → `init`

## Exact interpolation

--repo after the verb. The host interprets the user's request; the CLI does not
parse a slash command. Extract explicitly supplied `--repo`, `--done-when`,
`--verify`, `--max-cycles` and `--force` options, respecting quoted values.
Remaining text is a **one-liner objective**. Use explicit `--prompt '…'` when
the objective itself contains option-like text; that entire value is literal.
Explicit options win over inference. Reject missing option values.

**Before init**, discern and print (do not skip):

```text
terminal: <done-when predicate>
continue while: <when to take another increment>
verify: <cmd> | none
```

- `terminal` → `--done-when` (when the loop should stop).
- `continue while` is not a CLI flag. It is when to take another increment
  (usually the negation of terminal). It picks `complete --evidence` vs
  `complete --done`.
- `verify` is a known, authorized command checking the terminal or its
  machine-checkable part. Inspect the repository before selecting it. A small
  file/content assertion is valid when its commands and target paths are known.
  Do not invent `pytest`, `npm test`, or a path. Passing verification never
  substitutes for the remaining human/agent judgment in the terminal predicate.

If `terminal` or `continue while` cannot be discerned from the one-liner,
**stop and ask** the user. Do not init. Do not invent a vague terminal
("looks good", "improved").

Then:

```text
python3 "$SKILL_ROOT/scripts/until-loop" init --repo "$REPO" --prompt '<one-liner>' --done-when '<terminal>' [--verify '<command>'] [--max-cycles N] [--force]
python3 "$SKILL_ROOT/scripts/until-loop" next --repo "$REPO"
python3 "$SKILL_ROOT/scripts/until-loop" complete --repo "$REPO" --evidence '…' [--done]
```

**Closer transformation.** Packet line `invoke /until-loop <verb> <args>` becomes:

```text
python3 "$SKILL_ROOT/scripts/until-loop" <verb> --repo "$REPO" <args>
```

`--repo` after `<verb>`. Copy `<args>` verbatim except the `<one line>` placeholder.

**Literal quoting (including the single-quote evidence rule).** Placeholders
are data, not shell source. Pass structured argv when available. When using a
shell, wrap every literal value (objective, terminal, verifier command, repo
path and evidence) in **single quotes**; escape an embedded `'` as `'\''`.
The double-quoted `$SKILL_ROOT` and `$REPO` above are already assigned shell
variables, never interpolated raw user text. Never double-quote evidence or
paste raw objective/verifier text into double quotes. This preserves `$()`,
backticks and dollar signs literally until the verifier intentionally runs.
Evidence must be nonblank printable ASCII, one line, at most 4096 bytes;
summarize multiline or Unicode results into that form. The CLI refuses blank,
oversized or control-bearing evidence. If a packet marks frozen text truncated,
read `.until-loop/prompt.md` and `.until-loop/state.json` under `REPO` for the
full objective and predicate before working; previews do not replace them.

complete is not idempotent. Uncertain whether complete landed → next,
never retry complete.

## Three-branch

- No `state.json` → `init` (refuse empty prompt).
- Existing run + `next` or no new prompt → `next`.
- New nonempty prompt on an existing run → `init --force --prompt '…'`.
  Empty `--force` is refused. Plain `init` over an existing run is CLI exit 2 —
  do not retry without `--force`.

## Host loop

Same turn, until stop:

1. Echo `## Next prompt` and `## When done invoke` in full.
2. Issue the Next (do that work here). do not invoke /goal.
3. Satisfy any printed precondition. From When done, exec **exactly one** of
   the two labeled commands: default `complete --evidence '…'`, or
   `if done-when holds` with `--done`. Apply the closer transformation and
   single-quote evidence rule. A printed slash closer is a label to transform
   into the Python command, never a slash command to inject into the host.
4. New stdout is the next prompt. Repeat.

On a successful CLI call, stop when When done is `stop — no update`.
`done` means the claimed predicate and optional verification passed;
`halted` means the cycle budget ended, so report the unfinished work.
User cancellation, required missing information, or the error contract also
stops work; the loop does not grant additional authorization.

## Error contract

Exit 0 → packet (except help). Exit 2 → print stderr, do not invent a next step. Exit 64 →
usage, stop. Any **nonzero** exit not in `{2, 64}` → **stop and report** the
raw stderr; do not invent a next step and do not retry. A Python `Traceback` in
stderr or `error: internal:` is a script bug, not a loop state. Preserve the
diagnostic; do not repeatedly execute a command that may already have run.
Malformed/unsupported state or a repository identity mismatch is exit 2 and
must be investigated, not bypassed with `--force`.

Lost context or an uncertain completion → load this card and exec `next` with
the same `--repo`, then Host loop. `next` recovers a prepared interrupted write
before printing the last accepted cycle and evidence. It does not rerun the
verifier. If interruption happened before the result was prepared, inspect
any verifier side effects before deciding what work remains.

## Parent skills

Grok cannot pager-inject `/until-loop` from inside another skill (same
hole as `/goal`). Same-turn call:

1. Parent **loads this skill**: read this file in full and follow it as
   the active card. Do not summarize it away.
2. **This card** supplies the Python command (`--repo` after the verb).
   The same agent now acts under this card, derives the predicate/verifier
   here, and executes its interpolation. The parent supplies only the objective
   and constraints; it must not invent its own `python3 …/scripts/until-loop`
   lines. The same ownership applies after lost context.

The parent must not type `/until-loop` and must not invoke `/goal`.
`disable-model-invocation: true` means Grok will not auto-load this card;
the parent must read this file. This card is the only CLI caller.

## Validation

Run `bash tests/until-loop.test.sh` from this skill's directory. It includes
the deterministic runtime regressions and requires no sibling skill.
For installed-parent prose checks, set `UNTIL_LOOP_DEMO_SKILL` to the parent
card's path (prefer absolute for stable resolution). Separately run the parent
smoke under the intended host and retain its transcript; static text checks
cannot prove a host handoff.
The current audit findings and evidence are recorded in `AUDIT.md`.
