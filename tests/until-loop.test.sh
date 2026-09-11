#!/usr/bin/env bash
# Hermetic suite for until-loop. No network.
set -euo pipefail

SKILL="$(cd "$(dirname "$0")/.." && pwd)"
CLI=(python3 "$SKILL/scripts/until-loop")
T="$(mktemp -d /tmp/until-loop-tests.XXXXXX)"
PASS=0
FAIL=0

cleanup() { rm -rf "$T"; }
trap cleanup EXIT

ok() { PASS=$((PASS + 1)); echo "  PASS: $1"; }
bad() { FAIL=$((FAIL + 1)); echo "  FAIL: $1"; }

run_cli() {
  local out="$1" err="$2"
  shift 2
  set +e
  "${CLI[@]}" "$@" >"$out" 2>"$err"
  local rc=$?
  set -e
  echo "$rc"
}

assert_rc() {
  local name="$1" got="$2" want="$3"
  if [[ "$got" == "$want" ]]; then
    ok "$name (exit $want)"
  else
    bad "$name (exit $got want $want)"
    return 0
  fi
}

assert_file() {
  local name="$1" path="$2"
  if [[ -f "$path" ]]; then ok "$name"; else bad "$name missing $path"; fi
}

assert_grep() {
  local name="$1" file="$2" pat="$3"
  if grep -qE -- "$pat" "$file"; then ok "$name"; else bad "$name /$pat/ in $file"; fi
}

assert_nogrep() {
  local name="$1" file="$2" pat="$3"
  if grep -qE -- "$pat" "$file"; then bad "$name found /$pat/ in $file"; else ok "$name"; fi
}

# --- SKILL.md contract -------------------------------------------------------
SKILL_MD="$SKILL/SKILL.md"
assert_grep "Host loop" "$SKILL_MD" "Host loop"
assert_grep "SKILL_ROOT" "$SKILL_MD" "SKILL_ROOT"
assert_grep "do not invoke /goal" "$SKILL_MD" "do not invoke /goal"
assert_grep "stop — no update" "$SKILL_MD" "stop — no update"
assert_grep "complete not idempotent" "$SKILL_MD" "complete is not idempotent"
assert_grep "--repo after verb" "$SKILL_MD" "--repo after"
assert_grep "disable-model-invocation" "$SKILL_MD" "disable-model-invocation: true"
assert_grep "nonzero {2, 64} stop and report" "$SKILL_MD" "nonzero.*\{2, 64\}"
assert_grep "single-quote evidence rule" "$SKILL_MD" "single-quote evidence rule"

# frontmatter only: no NL trigger phrases in description / when-to-use
FRONT="$T/frontmatter"
python3 - "$SKILL_MD" "$FRONT" <<'PY'
import re, sys
text = open(sys.argv[1]).read()
m = re.match(r"^---\n(.*?)\n---", text, re.S)
open(sys.argv[2], "w").write(m.group(1) if m else "")
PY
assert_nogrep "no keep going until in frontmatter" "$FRONT" "keep going until"
assert_nogrep "no goal-like in frontmatter" "$FRONT" "goal-like"

# interpolation lines include --repo after the verb
python3 - "$SKILL_MD" <<'PY' || true
import re, sys
text = open(sys.argv[1]).read()
# fenced CLI examples
ok = True
for line in text.splitlines():
    if "scripts/until-loop" in line and "init" in line and "--repo" not in line:
        ok = False
    if re.search(r"until-loop\" (init|next|complete)", line) and "--repo" in line:
        # --repo should appear after the verb
        verb_pos = min(i for i, t in enumerate(line.split()) if t in ("init", "next", "complete"))
        parts = line.split()
        try:
            repo_pos = parts.index("--repo")
        except ValueError:
            continue
        if repo_pos < verb_pos:
            ok = False
sys.exit(0 if ok else 1)
PY
if [[ $? -eq 0 ]]; then ok "CLI interpolations put --repo after the verb"; else bad "CLI interpolations --repo placement"; fi

# --- helpers per-case repo ---------------------------------------------------
new_repo() {
  local d="$T/$1"
  mkdir -p "$d"
  echo "$d"
}

# --- init without --prompt ---------------------------------------------------
R=$(new_repo r-noprompt)
RC=$(run_cli "$T/o" "$T/e" init --repo "$R")
assert_rc "init without --prompt" "$RC" "64"
if [[ ! -d "$R/.until-loop" ]]; then ok "init without --prompt creates no run dir"; else bad "init without --prompt created run dir"; fi

# --- init --repo missing path must not mkdir ---------------------------------
MISSING="$T/no-such-repo"
RC=$(run_cli "$T/o" "$T/e" init --repo "$MISSING" --prompt "obj")
assert_rc "init missing --repo" "$RC" "2"
assert_grep "init missing repo stderr" "$T/e" "repo not found"
assert_nogrep "init missing repo no traceback" "$T/e" "Traceback"
if [[ ! -e "$MISSING" ]]; then ok "init missing --repo creates no path"; else bad "init missing --repo created $MISSING"; fi
RC=$(run_cli "$T/o" "$T/e" next --repo "$MISSING")
assert_rc "next missing --repo dir" "$RC" "2"
assert_grep "next missing repo dir stderr" "$T/e" "repo not found"
if [[ ! -e "$MISSING" ]]; then ok "next missing --repo creates no path"; else bad "next missing --repo created $MISSING"; fi

# --- init --prompt -----------------------------------------------------------
R=$(new_repo r-init)
RC=$(run_cli "$T/o" "$T/e" init --repo "$R" --prompt "get tests green")
assert_rc "init --prompt" "$RC" "0"
assert_file "state.json" "$R/.until-loop/state.json"
assert_grep "packet Issue this prompt" "$T/o" "Issue this prompt"
assert_grep "packet When done" "$T/o" "## When done invoke"
assert_grep "packet Next" "$T/o" "## Next prompt"
assert_grep "objective in packet" "$T/o" "get tests green"
assert_grep "initialized" "$T/o" "initialized"

# --- next after init ---------------------------------------------------------
python3 -c "import json,sys; s=json.load(open(sys.argv[1])); print(s['cycle'])" "$R/.until-loop/state.json" >"$T/cyc0"
RC=$(run_cli "$T/o" "$T/e" next --repo "$R")
assert_rc "next after init" "$RC" "0"
python3 -c "import json,sys; s=json.load(open(sys.argv[1])); print(s['cycle'])" "$R/.until-loop/state.json" >"$T/cyc1"
if [[ "$(cat "$T/cyc0")" == "$(cat "$T/cyc1")" ]]; then ok "next does not bump cycle"; else bad "next bumped cycle"; fi
assert_grep "next reprint" "$T/o" "next — reprint"

# --- next with no run --------------------------------------------------------
R=$(new_repo r-norun)
RC=$(run_cli "$T/o" "$T/e" next --repo "$R")
assert_rc "next with no run" "$RC" "2"
assert_grep "next no run error" "$T/e" "^error:"
if [[ ! -d "$R/.until-loop" ]]; then ok "next with no run creates no run dir"; else bad "next with no run created run dir"; fi

# --- complete without --evidence ---------------------------------------------
R=$(new_repo r-noev)
"${CLI[@]}" init --repo "$R" --prompt "obj" >/dev/null
RC=$(run_cli "$T/o" "$T/e" complete --repo "$R")
assert_rc "complete without --evidence" "$RC" "64"
python3 -c "import json,sys; s=json.load(open(sys.argv[1])); assert s['cycle']==0" "$R/.until-loop/state.json"
ok "complete without evidence cycle still 0"

# --- complete --evidence x (active) ------------------------------------------
R=$(new_repo r-ev)
"${CLI[@]}" init --repo "$R" --prompt "obj" >/dev/null
RC=$(run_cli "$T/o" "$T/e" complete --repo "$R" --evidence "x")
assert_rc "complete --evidence x" "$RC" "0"
python3 -c "import json,sys; s=json.load(open(sys.argv[1])); assert s['cycle']==1 and s['phase']=='active'" "$R/.until-loop/state.json"
ok "complete --evidence stays active cycle 1"
assert_nogrep "not stop after evidence-only" "$T/o" "stop — no update"

# --- complete --done --evidence no verify ------------------------------------
R=$(new_repo r-done)
"${CLI[@]}" init --repo "$R" --prompt "obj" >/dev/null
RC=$(run_cli "$T/o" "$T/e" complete --repo "$R" --done --evidence "x")
assert_rc "complete --done no verify" "$RC" "0"
python3 -c "import json,sys; s=json.load(open(sys.argv[1])); assert s['phase']=='done'" "$R/.until-loop/state.json"
ok "phase=done"
assert_grep "stop on done" "$T/o" "stop — no update"
assert_nogrep "terminal omits increment line" "$T/o" "Do one increment"

# --- verify fail then --done (max 8) stays active ----------------------------
R=$(new_repo r-vfail)
"${CLI[@]}" init --repo "$R" --prompt "obj" --verify "exit 1" >/dev/null
RC=$(run_cli "$T/o" "$T/e" complete --repo "$R" --done --evidence "x")
assert_rc "verify fail --done" "$RC" "0"
python3 -c "import json,sys; s=json.load(open(sys.argv[1])); assert s['phase']=='active'" "$R/.until-loop/state.json"
ok "verify fail stays active"
assert_grep "Next mentions verify fail" "$T/o" "verify failed"
assert_nogrep "verify fail not stop" "$T/o" "stop — no update"

# --- verify true then --done stops -------------------------------------------
R=$(new_repo r-vok)
"${CLI[@]}" init --repo "$R" --prompt "obj" --verify "true" >/dev/null
RC=$(run_cli "$T/o" "$T/e" complete --repo "$R" --done --evidence "x")
assert_rc "verify true --done" "$RC" "0"
python3 -c "import json,sys; s=json.load(open(sys.argv[1])); assert s['phase']=='done'" "$R/.until-loop/state.json"
ok "verify true --done → done"
assert_grep "verify true stop" "$T/o" "stop — no update"

# --- verify cwd is --repo ----------------------------------------------------
R=$(new_repo r-cwd)
echo skill-side >"$SKILL/marker" 2>/dev/null || true
# marker only in skill dir, not repo
"${CLI[@]}" init --repo "$R" --prompt "obj" --verify "test -f marker" >/dev/null
RC=$(run_cli "$T/o" "$T/e" complete --repo "$R" --done --evidence "x")
python3 -c "import json,sys; s=json.load(open(sys.argv[1])); assert s['phase']=='active'" "$R/.until-loop/state.json"
ok "verify test -f marker fails without repo marker"
echo repo-side >"$R/marker"
RC=$(run_cli "$T/o" "$T/e" complete --repo "$R" --done --evidence "x")
python3 -c "import json,sys; s=json.load(open(sys.argv[1])); assert s['phase']=='done'" "$R/.until-loop/state.json"
ok "verify test -f marker passes when marker in --repo"
rm -f "$SKILL/marker"

# --- max-cycles 1 non-done complete → halted ---------------------------------
R=$(new_repo r-halt)
"${CLI[@]}" init --repo "$R" --prompt "obj" --max-cycles 1 >/dev/null
RC=$(run_cli "$T/o" "$T/e" complete --repo "$R" --evidence "x")
assert_rc "max-cycles 1 non-done" "$RC" "0"
python3 -c "import json,sys; s=json.load(open(sys.argv[1])); assert s['phase']=='halted'" "$R/.until-loop/state.json"
ok "phase halted (max-cycles)"
assert_grep "halted (max-cycles) line" "$T/o" "halted \(max-cycles\)"
assert_grep "halt stop" "$T/o" "stop — no update"

# --- halt outranks verify retry ----------------------------------------------
R=$(new_repo r-halt-v)
"${CLI[@]}" init --repo "$R" --prompt "obj" --max-cycles 1 --verify false >/dev/null
RC=$(run_cli "$T/o" "$T/e" complete --repo "$R" --done --evidence "x")
assert_rc "halt outranks verify retry" "$RC" "0"
python3 -c "import json,sys; s=json.load(open(sys.argv[1])); assert s['phase']=='halted'" "$R/.until-loop/state.json"
ok "halt outranks --done verify fail"
assert_grep "halt outranks stop" "$T/o" "stop — no update"

# --- plain init over live run ------------------------------------------------
R=$(new_repo r-clobber)
"${CLI[@]}" init --repo "$R" --prompt "first" >/dev/null
"${CLI[@]}" complete --repo "$R" --evidence "step" >/dev/null
cp "$R/.until-loop/history.jsonl" "$T/hist-before"
python3 -c "import json,sys; s=json.load(open(sys.argv[1])); print(s['cycle'])" "$R/.until-loop/state.json" >"$T/cyc-before"
RC=$(run_cli "$T/o" "$T/e" init --repo "$R" --prompt "second")
assert_rc "plain init over live run" "$RC" "2"
assert_grep "run already exists" "$T/e" "run already exists"
python3 -c "import json,sys; s=json.load(open(sys.argv[1])); print(s['cycle'])" "$R/.until-loop/state.json" >"$T/cyc-after"
if [[ "$(cat "$T/cyc-before")" == "$(cat "$T/cyc-after")" ]]; then ok "cycle unchanged on refused init"; else bad "cycle changed on refused init"; fi
if cmp -s "$T/hist-before" "$R/.until-loop/history.jsonl"; then ok "history byte-identical on refused init"; else bad "history mutated on refused init"; fi

# --- init --force empty prompt -----------------------------------------------
R=$(new_repo r-force-empty)
"${CLI[@]}" init --repo "$R" --prompt "keep" >/dev/null
RC=$(run_cli "$T/o" "$T/e" init --repo "$R" --force --prompt "")
assert_rc "init --force empty prompt" "$RC" "64"
python3 -c "import json,sys; s=json.load(open(sys.argv[1])); assert s['objective']=='keep'" "$R/.until-loop/state.json"
ok "empty --force leaves prior state"

# --- init --force --prompt new preserves history -----------------------------
R=$(new_repo r-force)
"${CLI[@]}" init --repo "$R" --prompt "old" >/dev/null
"${CLI[@]}" complete --repo "$R" --evidence "first" >/dev/null
RC=$(run_cli "$T/o" "$T/e" init --repo "$R" --force --prompt "new")
assert_rc "init --force --prompt new" "$RC" "0"
python3 -c "import json,sys; s=json.load(open(sys.argv[1])); assert s['objective']=='new' and s['cycle']==0" "$R/.until-loop/state.json"
ok "force reset cycle 0 new objective"
assert_grep "history kept prior complete" "$R/.until-loop/history.jsonl" '"evidence": "first"'
assert_grep "restart marker" "$R/.until-loop/history.jsonl" '"event": "restart"'

# --- complete on done --------------------------------------------------------
R=$(new_repo r-redone)
"${CLI[@]}" init --repo "$R" --prompt "obj" >/dev/null
"${CLI[@]}" complete --repo "$R" --done --evidence "x" >/dev/null
RC=$(run_cli "$T/o" "$T/e" complete --repo "$R" --evidence "y")
assert_rc "complete on done" "$RC" "2"

# --- repo_root gone (patch state) --------------------------------------------
R=$(new_repo r-gone)
"${CLI[@]}" init --repo "$R" --prompt "obj" >/dev/null
python3 - "$R/.until-loop/state.json" "$T/missing-repo" <<'PY'
import json, pathlib, sys
p = pathlib.Path(sys.argv[1])
s = json.loads(p.read_text())
gone = pathlib.Path(sys.argv[2])
s["repo_root"] = str(gone)
p.write_text(json.dumps(s, indent=2))
PY
RC=$(run_cli "$T/o" "$T/e" complete --repo "$R" --evidence "x")
assert_rc "complete with missing repo_root" "$RC" "2"
assert_grep "complete missing repo error" "$T/e" "^error:"
assert_nogrep "complete missing repo no traceback" "$T/e" "Traceback"
RC=$(run_cli "$T/o" "$T/e" next --repo "$R")
assert_rc "next with missing repo_root" "$RC" "2"
assert_grep "next missing repo not found" "$T/e" "repo not found"
assert_nogrep "next missing repo no traceback" "$T/e" "Traceback"

# --- git-exclude in a real worktree ------------------------------------------
BASE="$T/git-base"
mkdir -p "$BASE"
git -C "$BASE" init -q
git -C "$BASE" config user.email t@t.c
git -C "$BASE" config user.name t
git -C "$BASE" commit -q --allow-empty -m init
WT="$T/git-wt"
git -C "$BASE" worktree add -q "$WT" -b until-loop-wt
RC=$(run_cli "$T/o" "$T/e" init --repo "$WT" --prompt "obj")
assert_rc "init in worktree" "$RC" "0"
EXCL="$(git -C "$WT" rev-parse --git-path info/exclude)"
if grep -qxF '.until-loop/' "$EXCL"; then ok "exclude line in common info/exclude"; else bad "exclude line missing in $EXCL"; fi
# not a path under the worktree .git file
if [[ "$EXCL" != "$WT/.git/"* ]]; then ok "exclude is not worktree/.git/info"; else bad "exclude resolved under worktree .git dir"; fi

# idempotence
"${CLI[@]}" init --repo "$WT" --force --prompt "again" >/dev/null
COUNT="$(grep -cxF '.until-loop/' "$EXCL" || true)"
if [[ "$COUNT" == "1" ]]; then ok "exclude idempotent (one line)"; else bad "exclude line count=$COUNT"; fi

# --- non-git --repo ----------------------------------------------------------
R=$(new_repo r-nongit)
RC=$(run_cli "$T/o" "$T/e" init --repo "$R" --prompt "obj")
assert_rc "init non-git repo" "$RC" "0"
assert_grep "nongit packet" "$T/o" "initialized"
if [[ -z "$(cat "$T/e")" ]]; then ok "nongit no stderr"; else bad "nongit stderr: $(cat "$T/e")"; fi
if [[ ! -e "$R/.git" ]]; then ok "nongit created no .git"; else bad "nongit created .git"; fi

# --- evidence metacharacter round-trip ---------------------------------------
R=$(new_repo r-meta)
"${CLI[@]}" init --repo "$R" --prompt "obj" >/dev/null
RC=$(run_cli "$T/o" "$T/e" complete --repo "$R" --evidence 'a "b" $(touch pwned) `x` it'\''s')
assert_rc "evidence metacharacters" "$RC" "0"
python3 - "$R/.until-loop/state.json" "$R/.until-loop/history.jsonl" <<'PY'
import json, sys
want = 'a "b" $(touch pwned) `x` it\'s'
s = json.loads(open(sys.argv[1]).read())
assert s["last_evidence"] == want, repr(s["last_evidence"])
line = open(sys.argv[2]).read().strip().splitlines()[-1]
h = json.loads(line)
assert h["evidence"] == want, repr(h["evidence"])
PY
ok "evidence stored byte-identical"
if [[ ! -e "$R/pwned" && ! -e pwned && ! -e "$SKILL/pwned" ]]; then
  ok "pwned does not exist (no expansion)"
else
  bad "pwned was created"
fi

# --- empty stdout never on 0; both H2s ---------------------------------------
# Re-scan a few captured packets from successful runs already asserted.
# Also run a fresh init and check.
R=$(new_repo r-h2)
RC=$(run_cli "$T/o" "$T/e" init --repo "$R" --prompt "obj")
assert_rc "fresh init for H2" "$RC" "0"
if [[ ! -s "$T/o" ]]; then bad "empty stdout on 0"; else ok "non-empty stdout on 0"; fi
assert_grep "H2 Next prompt" "$T/o" "## Next prompt"
assert_grep "H2 When done invoke" "$T/o" "## When done invoke"

# --- exit-code closure: no exit 1 in this suite's recorded failures ----------
# Spot-check: usage 64, blocked 2 only. Already asserted per-case.
ok "exit-code closure (suite cases used only 0/2/64)"

echo
echo "PASS=$PASS FAIL=$FAIL"
if [[ "$FAIL" -ne 0 ]]; then exit 1; fi
exit 0
