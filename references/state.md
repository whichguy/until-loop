# Run-dir schema

Run dir: `<repo>/.until-loop/` (not inside the skill package).

## `state.json` (closed set)

```text
version          1
phase            active | done | halted | blocked
objective        string
done_when        string
verify_cmd       string | null
max_cycles       int >= 1
cycle            int >= 0
repo_root        absolute path
last_evidence    string | null
last_verify      {ok: bool, exit: int, tail: string} | null
```

`blocked` is reserved; v1 does not write it.

Flock: exclusive `fcntl.flock` on `<run-dir>/.lock` around every
read-modify-write of `state.json` plus history append.

## `prompt.md`

Frozen objective (same string as `state.objective`).

## `history.jsonl`

Append-only. Survives `init --force`.

- One object per accepted `complete`:
  `{cycle, evidence, done_claim, verify_ok}`
- `init --force` appends:
  `{"event":"restart","prev_cycle":N,"prev_phase":P,"objective":"<new>"}`

`next` reads `state.json`, not history.

## Locate

`--repo PATH` → `PATH/.until-loop`. Present-but-empty `--repo` (`""` or
whitespace) is usage 64 (no mkdir, no exclude write). If `--repo` omitted
(CLI/tests only): `git rev-parse --show-toplevel` from process cwd, else cwd.
