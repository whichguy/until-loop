#!/usr/bin/env bash
# Hermetic suite for until-loop. No network.
set -euo pipefail

SKILL="$(cd "$(dirname "$0")/.." && pwd)"
PASS=0
FAIL=0

ok() { PASS=$((PASS + 1)); echo "  PASS: $1"; }
bad() { FAIL=$((FAIL + 1)); echo "  FAIL: $1"; }

assert_file() {
  local name="$1" path="$2"
  if [[ -f "$path" ]]; then ok "$name"; else bad "$name missing $path"; fi
}

assert_grep() {
  local name="$1" file="$2" pat="$3"
  if grep -qE -- "$pat" "$file"; then ok "$name"; else bad "$name /$pat/ in $file"; fi
}

assert_absent() {
  local name="$1" path="$2"
  if [[ -e "$path" ]]; then bad "$name still present: $path"; else ok "$name"; fi
}

# --- One runtime, one card ---------------------------------------------------
SKILL_MD="$SKILL/SKILL.md"
assert_file "skill card" "$SKILL_MD"
assert_file "callback runtime" "$SKILL/scripts/until_loop_ephemeral.py"
assert_file "callback adapter" "$SKILL/references/runtime-ephemeral.md"
assert_grep "preserve explicit invocation policy" "$SKILL_MD" "disable-model-invocation: true"
assert_grep "card binds the callback runtime" "$SKILL_MD" "scripts/until_loop_ephemeral\\.py"
assert_absent "no durable v1 CLI" "$SKILL/scripts/until-loop"
assert_absent "no durable v2 runtime" "$SKILL/scripts/until_loop_v2.py"
assert_absent "no v2 packet renderer" "$SKILL/scripts/until_loop_packet.py"
assert_absent "no bundled Improve plugin" "$SKILL/plugins/improve"
assert_absent "no bundled Improve example" "$SKILL/examples"

python3 -m unittest discover -s "$SKILL/tests" -p 'test_*.py'

echo
echo "PASS=$PASS FAIL=$FAIL"
if [[ "$FAIL" -ne 0 ]]; then exit 1; fi
exit 0
