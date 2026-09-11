---
name: until-loop
description: >-
  Same-turn until-loop without /goal. User slash: /until-loop.
  Parent skills exec scripts/until-loop and follow this Host loop;
  they must not type /until-loop or /goal. The script is the state
  machine. Do not invoke /goal.
allowed-tools: all
disable-model-invocation: true
user-invocable: true
argument-hint: "<objective> | next | complete --evidence '…' [--done]"
version: 0.1.1
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
- A parent skill is execing this package's CLI and following Host loop.

## Not for

ShipLoop, DevLoop, improve-loop, review-coverage, `/goal`, worktrees, DAGs.
Those products must not type `/until-loop` either — if they want this loop,
they exec this CLI (see Parent skills).

## Setup

`SKILL_ROOT` = directory containing this `SKILL.md`.

```text
CLI=python3 "$SKILL_ROOT/scripts/until-loop"
```

`REPO` = user `--repo` if present, else `git rev-parse --show-toplevel` from the
session workspace at invoke. Pass `--repo "$REPO"` on every CLI call. Do not
omit `--repo` (Grok sticky cwd). Present-but-empty `--repo` (`""` or whitespace)
is CLI exit 64.

## Verbs

- empty / `next` → `next`
- `complete …` → exactly one closer from the current packet’s When done
- else → `init`

## Exact interpolation

--repo after the verb. Remaining text is `--prompt` only.

```text
python3 "$SKILL_ROOT/scripts/until-loop" init --repo "$REPO" --prompt "<objective>" [--verify CMD] [--done-when TEXT] [--max-cycles N] [--force]
python3 "$SKILL_ROOT/scripts/until-loop" next --repo "$REPO"
python3 "$SKILL_ROOT/scripts/until-loop" complete --repo "$REPO" --evidence '…' [--done]
```

**Closer transformation.** Packet line `invoke /until-loop <verb> <args>` becomes:

```text
python3 "$SKILL_ROOT/scripts/until-loop" <verb> --repo "$REPO" <args>
```

`--repo` after `<verb>`. Copy `<args>` verbatim except the `<one line>` placeholder.

**Evidence quoting (single-quote evidence rule).** `<one line>` is a placeholder
you substitute, not literal text. Wrap the one-line evidence summary in **single
quotes**; escape an embedded `'` as `'\''`. Never double-quote evidence. Strip
control characters and newlines. Printable ASCII, one line.

complete is not idempotent. Uncertain whether complete landed → next,
never retry complete.

## Three-branch

- No `state.json` → `init` (refuse empty prompt).
- Existing run + `next` or no new prompt → `next`.
- New nonempty prompt on an existing run → `init --force --prompt "…"`.
  Empty `--force` is refused. Plain `init` over an existing run is CLI exit 2 —
  do not retry without `--force`.

## Host loop

Same turn, until stop:

1. Echo `## Next prompt` and `## When done invoke` in full.
2. Issue the Next (do that work here). do not invoke /goal.
3. Satisfy any printed precondition. From When done, exec **exactly one** of
   the two labeled commands: default `complete --evidence '…'`, or
   `if done-when holds` with `--done`. Apply the closer transformation and
   single-quote evidence rule.
4. New stdout is the next prompt. Repeat.

Stop only when When done is `stop — no update`.

## Error contract

Exit 0 → packet. Exit 2 → print stderr, do not invent a next step. Exit 64 →
usage, stop. Any **nonzero** exit not in `{2, 64}` → **stop and report** the
raw stderr; do not invent a next step and do not retry. A Python `Traceback` in
stderr is a script bug, not a loop state.

Lost context without completing → exec `next` (user types `/until-loop next`;
parent execs the next CLI with `--repo`), then Host loop.

## Parent skills

A parent starts this loop in the **same turn** by reading this card and
execing `$CLI` (`--repo` after the verb). It must not type `/until-loop`
and must not invoke `/goal`. `disable-model-invocation: true` means Grok
will not auto-load this card; the parent must read this file.
