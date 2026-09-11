# Review Converge: until-loop skill (KISS/YAGNI residual×2)

**Target paths:** `SKILL.md`, `references/packet.md`, `references/state.md`, `scripts/until-loop`, `tests/until-loop.test.sh`, `REVIEW_CONVERGE.md`
**Test command:** `bash tests/until-loop.test.sh`
**Started:** 2026-09-11          **Status:** complete
**Round counter:** 3
**Consecutive clean rounds:** 2
**Repo:** `/Users/dadleet/.grok/skills/until-loop`
**Plan contract:** `/Users/dadleet/.grok/sessions/%2FUsers%2Fdadleet%2Fsrc%2Ftic-tac-toe-oneshot/01a08ddb-af7b-77b1-94b6-d248e796f144/goal/plan.md`
**Plan hash:** `5e2638c359ab98bd9dab6ecca77424f610ad82c860030bedb998959c23ba2060`
**Base ref:** `7f30ed90c372bf1721efead3d0eb0db01e6609c6`

## Stop-condition tracking
- consecutive-no-progress: 0
- consecutive-same-error: 0 (signature: none)

## Log
### Round 1 — 2026-09-11
**Review:** 1 material, 4 minor
**Material findings:**
- Default verify timeout 30s (from `63ebe8e`) inverts AC1 verify-ok: a succeeding `--verify` slower than 30s becomes `ok=False` `exit=124`, so `complete --done` stays `active` and does not print `stop — no update`. Host never sets `UNTIL_LOOP_VERIFY_TIMEOUT`.
**Deferred (minor/P2):**
- [ ] P2: unreachable `empty --force` branch after `missing --prompt`
- [ ] P2: `cmd_init` double-mkdir of the run dir (`with_lock` already creates it)
- [ ] P2: most stop-rail assertions are substring grep, not `^stop — no update$`
- [ ] P2: D2 parked — corrupt state.json, whitespace `--evidence`, printing `last_evidence`
**Git-history check:** unaudited range `7f30ed9..HEAD` is `63ebe8e`; packet sanitize, empty `--repo` 64, login re-anchor, stdin DEVNULL, and the timeout *mechanism* stay (sufficient complexity, not YAGNI)
**Plan:** raise default to 300s; pin CLI `sleep 2` --done → done with env unset; pin shipped `verify_timeout_sec()` ≥ 300
**Plan review:** n/a (native)
**Implementation:** `verify_timeout_sec` default 300.0; `references/state.md` documents the env; suite cases `default timeout allows 2s verify` / `shipped verify_timeout_sec default >= 300`
**Lint:** skipped (none configured)
**Test result:** PASS
**Outcome:** fixed
**Error signature:** none
**Learnings:** A hang bound is required because verify holds the exclusive flock, but a 30s default is not a Host-loop parameter — it silently turns slow-ok into fail. Raising the default keeps the bound without inverting `complete --done` + verify ok. Do not delete packet sanitization or empty-`--repo` 64; those protect AC1 H2/stop and dest workflows.
**Anchor evidence:**
- verify-ok timeout default → tests/until-loop.test.sh `default timeout allows 2s verify` / `shipped verify_timeout_sec default >= 300`
**Consecutive clean rounds after this entry:** 0
**Committed:** yes
**Notes:** streak 0 after material; archive of plan hash 204f13b1 is `REVIEW_CONVERGE.204f13b1.archive.md` (not this stop)

### Round 2 — 2026-09-11
**Review:** 0 material, 4 minor
**Material findings:**
- none
**Deferred (minor/P2):**
- [ ] P2: unreachable `empty --force` branch after `missing --prompt` (probed: `--force --prompt ""` prints `missing --prompt`, exit 64)
- [ ] P2: `cmd_init` double-mkdir of the run dir
- [ ] P2: most stop-rail assertions are substring grep (the cycle-1 2s verify pin uses `^stop — no update$`)
- [ ] P2: D2 parked — corrupt state.json, whitespace `--evidence`, printing `last_evidence`
**Git-history check:** reverse `7f30ed9..HEAD` is `63ebe8e` + `2252f04`. No AC1 workflow removed. Omitted `--repo` still falls back (settled dest; Host always passes `--repo`). `--help` exits 0 without a packet (documented).
**Plan:** n/a (clean)
**Plan review:** n/a
**Implementation:** none (ledger-only)
**Lint:** skipped (none configured)
**Test result:** N/A (clean round; no product change)
**Outcome:** clean
**Error signature:** none
**Learnings:** After 2252f04 the 300s default, packet sanitize, empty `--repo` 64, login re-anchor, stdin DEVNULL, and hang timeout are the minimum that keeps the twelve AC1 workflows honest. Remaining items are dead code or assertion tightness, not workflow inversions. Do not implement D2 recovery paths; they are extra Host-loop surface. First consecutive zero-material round; do not stop (need two).
**Anchor evidence:**
- A15 streak 1 → this entry
**Consecutive clean rounds after this entry:** 1
**Committed:** yes
**Notes:** first clean after material round 1; archived 204f13b1 ledger is not the stop

### Round 3 — 2026-09-11
**Review:** 0 material, 4 minor
**Material findings:**
- none
**Deferred (minor/P2):**
- [ ] P2: unreachable `empty --force` branch after `missing --prompt`
- [ ] P2: `cmd_init` double-mkdir of the run dir
- [ ] P2: most stop-rail assertions are substring grep
- [ ] P2: D2 parked — corrupt state.json, whitespace `--evidence`, printing `last_evidence`
**Git-history check:** reverse `7f30ed9..HEAD` still includes `63ebe8e` and `2252f04`. Probed `next` after complete (cycle stays 1), `next` after done (stop rail, no bump), `next` after halt (stop rail). print_packet H2s match packet.md. Heart still says do not invoke /goal.
**Plan:** n/a (clean)
**Plan review:** n/a
**Implementation:** none (ledger-only)
**Lint:** skipped (none configured)
**Test result:** PASS (terminal clean, PASS=126 FAIL=0)
**Outcome:** clean
**Error signature:** none
**Learnings:** Second consecutive zero-material round. `next` after done/halt already reprints the stop rail (lost-context path works without extra code). Parked trivials stay listed — they do not block stop and must not grow D2 recovery paths. Residual×2 met for plan hash 5e2638c3. Archived `REVIEW_CONVERGE.204f13b1.archive.md` Status complete is not this evidence.
**Anchor evidence:**
- A8 → `{SCRATCH}/suite-terminal.log` FAIL=0
- A15 streak 2 → this entry
**Consecutive clean rounds after this entry:** 2
**Committed:** yes
**Notes:** residual×2 complete; second clean suite PASS=126 FAIL=0
