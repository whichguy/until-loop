# Review Converge: until-loop skill (this campaign)

**Target paths:** `SKILL.md`, `references/packet.md`, `references/state.md`, `scripts/until-loop`, `tests/until-loop.test.sh`, `REVIEW_CONVERGE.md`
**Test command:** `bash tests/until-loop.test.sh`
**Started:** 2026-09-11          **Status:** active
**Round counter:** 4
**Consecutive clean rounds:** 0
**Repo:** `/Users/dadleet/.grok/skills/until-loop`
**Plan contract:** `/Users/dadleet/.grok/sessions/%2FUsers%2Fdadleet%2Fsrc%2Ftic-tac-toe-oneshot/01a08ddb-af7b-77b1-94b6-d248e796f144/goal/plan.md`
**Plan hash:** `204f13b1adfe1f548a092fcc43402bd1b9e1f1b16b502b8523058175d044c7dc`
**Base ref:** `1135eda3aee1a141118868d5c6440b4e9563742b`

## Stop-condition tracking
- consecutive-no-progress: 0
- consecutive-same-error: 0 (signature: none)

## Log
### Round 3 — 2026-09-11
**Review:** 1 material, 2 minor (carried)
**Material findings:**
- `84d98cd251d2c541cbe42f8e0b5ffa1f266a9e66`: `init --repo PATH` mkdir'd a missing PATH and exited 0 (typo creates a tree). Landed after prior residual×2; streak-breaking.
**Deferred (minor/P2):**
- [ ] P2: terminal Next still says "Do one increment" after stop — packet contract keeps Issue this prompt; When done is the stop rail (applied in `1135eda`, still listed until wrap-up)
- [ ] P2: `next`/`complete` with no run still `mkdir` the run dir via with_lock (applied in `1135eda`, still listed until wrap-up)
**Git-history check:** archived ledger `REVIEW_CONVERGE.2061b278.archive.md` (prior complete); unaudited range `1135eda..HEAD` is `84d98cd`
**Plan:** pin missing-`--repo` fail-closed (already landed in `84d98cd` + `init missing --repo` test)
**Plan review:** n/a (preflight streak-break record)
**Implementation:** `scripts/until-loop` `resolve_repo` is_dir check — already in `84d98cd`
**Lint:** skipped (none configured)
**Test result:** N/A (preflight record of already-landed material)
**Outcome:** fixed
**Error signature:** none
**Learnings:** A material product fix after a declared residual×2 must reset consecutive-clean to 0. This round exists so this campaign cannot inherit the old complete Status.
**Anchor evidence:**
- A7 → this Round 3 names `84d98cd` under Material findings
**Consecutive clean rounds after this entry:** 0
**Committed:** yes
**Notes:** streak-break for plan hash 204f13b1; prior campaign archived not deleted

### Round 4 — 2026-09-11
**Review:** 1 material, 2 minor
**Material findings:**
- `ensure_exclude()` ran `git rev-parse --git-path info/exclude` with cwd=repo_root, so `init --repo` of a nested non-git dir appended `.until-loop/` to the enclosing checkout (invisible to `git status --porcelain`).
**Deferred (minor/P2):**
- [ ] P2: terminal Next still says "Do one increment" after stop (applied in `1135eda`)
- [ ] P2: `next`/`complete` with no run still `mkdir` the run dir (applied in `1135eda`)
**Git-history check:** OPEN triage from this plan; reproduced on throwaway outer git + nested throwaway; worktree exclude still required
**Plan:** skip exclude unless `git rev-parse --show-toplevel` equals `repo_root`; pin with nested `--repo` under a git outer
**Plan review:** n/a (native)
**Implementation:** `scripts/until-loop` `ensure_exclude` toplevel equality; `tests/until-loop.test.sh` `init nested in enclosing git` / `enclosing repo exclude untouched`
**Lint:** skipped (none configured)
**Test result:** PASS
**Outcome:** fixed
**Error signature:** none
**Learnings:** git from a nested path is the enclosing repo. Worktrees still match because show-toplevel is the worktree path. Material reset consecutive-clean to 0.
**Anchor evidence:**
- enclosing-exclude → tests/until-loop.test.sh `enclosing repo exclude untouched`; commit `5bc53e8537e590c3e3f2a2dd14889ead90b239f6`
**Consecutive clean rounds after this entry:** 0
**Committed:** yes
**Notes:** round-1 OPEN resolved as material; suite PASS=95
