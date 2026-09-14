# Run-dir schema

The schema below is the preserved **version-1** contract. Candidate version 2
uses the same run-directory name but an independently validated closed schema;
see `runtime-v2.md` and the candidate README for its records and transitions.
Version 2 does not use `prompt.md` as a second contract. Never append v2 fields
to a v1 state or run one version's recovery over the other's journal.

Run dir: `<repo>/.until-loop/` (not inside the skill package).

## `state.json` (closed set)

```text
version          1
phase            active | done | halted
objective        string
done_when        string
verify_cmd       string | null
max_cycles       int >= 1
cycle            int >= 0
repo_root        absolute path
last_evidence    string | null
last_verify      {ok: bool, exit: int, tail: string} | null
```

`blocked` is reserved; v1 neither writes nor accepts it as runnable state.
The loader rejects unknown fields/versions, invalid types (including booleans
as cycle integers), inconsistent phase/cycle/verification and a `repo_root`
that does not resolve to the selected `--repo`. Invalid state exits 2 before
verification. Do not bypass invalid state with `--force`.

Flock: exclusive `fcntl.flock` on `<run-dir>/.lock` around every
read-modify-write of `state.json` plus history append. Verify runs under
that lock: login `bash -lc`, `cd --` re-anchor, stdin `DEVNULL`, timeout
`UNTIL_LOOP_VERIFY_TIMEOUT` seconds (finite positive value; otherwise default
300; timeout → `ok=false`, `exit=124`, CLI still 0). Stdout and stderr are
drained together; retain at most 60,000 bytes before the final 20 nonempty
lines and a truncation/timeout marker. Non-UTF8 bytes decode with replacement.
New pending state refuses tails over 61,000 UTF8 bytes. Settled version-1
state from older releases may contain larger tails; `next` preserves those
bytes and renders only a bounded tail, so an upgrade neither floods the
packet nor prevents an explicit force restart.
Process-group termination is attempted on timeout; if the host denies group
signaling, the owned child is terminated. The verifier is an authorized shell
command, not an OS sandbox: detached descendants and external side effects
cannot be rolled back by this state machine.

Metadata paths must be ordinary files in a real `.until-loop` directory;
symlinks, multiply-linked files and non-file entries are refused before
reads/writes. These checks
prevent preexisting path redirection, not concurrent hostile filesystem edits.
Git's optional `.until-loop/` exclude update skips redirected or unsafe paths;
the loop can run without adding that convenience entry.

## Interrupted writes

`state.json` remains authoritative for a settled run. A transient
`.pending.json` redo record contains the next state, optional history event,
prior history byte length and optional frozen prompt. It is atomically saved
and fsynced before changing state/history. Under the same lock, finish the
state/prompt writes, finish one history line, fsync, then remove the record.
`next` or another CLI invocation rolls a pending transition forward without
rerunning verification; it refuses unexpected history contents. Partial lines
are completed and a fully written event is not duplicated. Ordinary `next`
does not rewrite settled state/history.

If the host dies during verification or before the redo record is durable,
the previous cycle remains current. No exactly-once guarantee applies to the
verifier's external effects. Inspect those effects and use `next` after an
uncertain completion; do not blindly retry `complete`.

## `prompt.md`

Frozen objective (same string as `state.objective`). In skill version 0.2 the
agent stores the original request and its natural-language Execute, Continue,
Success and Early-stop interpretation here; multiline text is supported.
After any pending recovery, a settled run requires this file to equal the
saved objective plus the newline written at initialization. Missing, invalid
UTF-8 or conflicting prompt text is refused before work can be verified or
another transition recorded. `next` reports the problem; it does not silently
replace either copy of the contract.
The CLI still treats it as opaque text, not executable instructions or parsed
conditions. `done_when` holds the inferred success predicate; it is not a
machine guarantee that every semantic criterion was evaluated.

Optional `working.md` is an agent-maintained notebook containing current
criteria/evidence, gaps and the next action. It is not runtime-owned and is not
part of the atomic state/history transaction. Reconcile it with accepted
cycle/contract and actual artifacts after resume; stale notes never override
state or supply success proof. The skill checks its path without following
links before either reading or writing and refuses symlinks/non-files and
multiply-linked files. This notebook check is a host instruction, not a
runtime-enforced guard.
Before an initialized active run stops incomplete, the skill requires a
notebook record of the stop reason and resumption needs (or reports why it
could not save one safely). Resume reads it and rechecks the blocker.
Blockers are reported without a success closer; v1 stays active until a
later resume or an explicitly authorized new/revised run.

## `history.jsonl`

Accepted events are append-only and survive `init --force`. Interrupted-write
recovery may replace/truncate only the pending event's incomplete suffix;
earlier accepted history bytes are preserved.

- One object per accepted `complete`:
  `{cycle, evidence, done_claim, verify_ok}`
- `init --force` appends:
  `{"event":"restart","prev_cycle":N,"prev_phase":P,"objective":"<new>"}`

`next` reads `state.json` after any pending recovery, not history as loop state.
Settled history is not reparsed or authenticated on every call. Recovery
validates its prepared suffix, but arbitrary edits to earlier history are
outside that guarantee; use current state and artifact evidence for decisions.

## Locate

`--repo PATH` → `PATH/.until-loop`. Present-but-empty `--repo` (`""` or
whitespace) is usage 64 (no mkdir, no exclude write). Nonblank paths preserve
leading/trailing spaces. Blank explicit `--done-when`/`--verify`, invalid
`--max-cycles` and nonblank-evidence violations are also usage errors before
mutation. CLI-supplied evidence is nonblank, one line of printable ASCII,
at most 4096 bytes; new pending records enforce the same limit.
legacy saved evidence is accepted as a nonempty string (including whitespace)
and sanitized/bounded to a 4096-byte preview on display. Full legacy evidence
remains in state/history. Whitespace-only legacy evidence is visibly labeled;
it cannot be supplied for a new completion. Original v1 compatibility fixtures
cover read-only resume and force restart without losing prior history.
Legacy whitespace-only predicates/verifiers also have a read/restart path;
completion refuses them before executing verification, requiring an explicit
restart with valid inputs. They are never silently interpreted as success.
If `--repo` omitted
(CLI/tests only): `git rev-parse --show-toplevel` from process cwd, else cwd.
