# Packet contract (stdout)

Every successful CLI run (exit 0) prints a packet. Stdout is never empty on 0.

Order after the outcome line and banner `until-loop — session harness`:

```text
## You are here
## Next prompt
## When done invoke
```

No other H2s in v1. Activity body inside Next uses `###` or below — never `## `.
`--help` is argparse help (not a packet) and may contain other headings.

## Render sanitization

User-supplied `objective`, `done-when`, evidence, and verify tail are sanitized **on
print only**. `state.json` keeps the raw strings.

- `objective`, `done-when` and evidence collapse to one line, then display at
  most 4096 UTF8 bytes plus a visible truncation marker. Full raw text stays
  in state/history; when frozen text is truncated the packet requires reading
  `.until-loop/prompt.md` and `.until-loop/state.json` under the selected repo.
- Nonprinting controls are removed from displayed text. Last accepted evidence
  is printed so recovery can identify which completion landed.
- Verify tail lines are prefixed with four spaces so they cannot open an H2
  (`^## `) or emit the stop rail (`^stop — no update$`).

## Outcome line

| Command | Line |
|---|---|
| `init` | `initialized (<phase>)` |
| `next` | `next — reprint (<phase>)` |
| `complete` (active) | `completed cycle <n> (<phase>)` |
| stop (`done` / `halted`) | `completed cycle <n> (<phase>)` then stop When done |

## You are here

- `phase: active \| done \| halted`
- `cycle: k/max`
- `verify: yes \| no`
- frozen `done-when:` one-liner
- Terminal: `done` or `halted (max-cycles)`

## Next prompt

First line: `Issue this prompt.`

Non-stop then:

```text
The new stdout is the next prompt to issue. Repeat until When done says stop.
```

Then the work: frozen objective, frozen done-when, this cycle number, last
evidence and bounded verify tail if any, “do one increment; do not invoke /goal”.

Stop packets omit the continue line and omit “Do one increment; do not invoke /goal.”

## When done invoke

Non-stop, two labeled lines (heart picks exactly one). `<one line>` is an
**unquoted placeholder** the heart substitutes with POSIX single quotes:

```text
default: invoke /until-loop complete --evidence <one line>
if done-when holds: invoke /until-loop complete --done --evidence <one line>
```

Stop packets:

```text
stop — no update
```

The script does not splice `SKILL_ROOT` or `REPO` into stdout.
