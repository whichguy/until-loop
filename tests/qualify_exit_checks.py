"""Opt-in native audit and frozen-check replay. See tests/ACCEPTANCE.md."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile

from acceptance_cases import CASES, REQUESTS, STOP_REASONS, render

ROOT = Path(__file__).resolve().parents[1]
MODEL = "gpt-6.2-sol"
EFFORT = "medium"


def write_json(path, value):
    path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def inventory(work):
    entries = {}
    for path in work.rglob("*"):
        mode = path.lstat().st_mode
        name = str(path.relative_to(work))
        if stat.S_ISDIR(mode):
            entries[name] = "directory"
        elif stat.S_ISREG(mode):
            entries[name] = (stat.S_IMODE(mode), hashlib.sha256(path.read_bytes()).hexdigest())
        else:
            raise ValueError(f"unexpected file type in fixture: {name}")
    return entries


def execute(argv, *, cwd, env, log, timeout):
    """Bound the whole process group, including a shell pipeline or a child model tool."""
    with log.open("wb") as out:
        process = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT,
                                   cwd=cwd, env=env, start_new_session=True)
        try:
            return process.wait(timeout=timeout)
        except (subprocess.TimeoutExpired, KeyboardInterrupt):
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()
            raise


def live(family, case, run):
    if case not in CASES[family]:
        raise ValueError(f"unknown {family} case {case!r}; choose from {', '.join(CASES[family])}")
    run.mkdir(mode=0o700)  # This command owns only a new evidence directory.
    work = run / "trial" / "work"
    work.mkdir(parents=True)
    render(work, family, case)
    original = inventory(work)
    state = run / "state"
    state.mkdir(mode=0o700)
    card = ROOT / "SKILL.md"
    prompt = (f"Use the Until Loop source card at {card}. Start one ordinary session root, "
              "use this workspace for the audit and keep the project read-only. Use only this "
              "fixture and the card's resources.\n\n" + REQUESTS[family])
    env = dict(os.environ)
    for key in ("UNTIL_LOOP_SESSION_ID", "CODEX_THREAD_ID", "CLAUDE_CODE_SESSION_ID", "GROK_SESSION_ID"):
        env.pop(key, None)
    settings = {"UNTIL_LOOP_STATE_DIR": str(state), "GIT_CEILING_DIRECTORIES": str(work.parent),
                "PYTHONDONTWRITEBYTECODE": "1"}
    env.update(settings)
    args = ["codex", "exec", "--model", MODEL, "-c", f'model_reasoning_effort="{EFFORT}"',
            "--sandbox", "workspace-write", "--ephemeral", "--skip-git-repo-check",
            "-C", str(work), "--add-dir", str(state), "-o", str(run / "answer.md")]
    for key, value in settings.items():
        args.extend(["-c", "shell_environment_policy.set." + key + "=" + json.dumps(value)])
    (run / "request.txt").write_text(prompt, encoding="utf-8")
    log = run / "codex.log"
    rc = execute([*args, prompt], cwd=work, env=env, log=log, timeout=420)
    if rc:
        raise ValueError(f"Codex exited {rc}; see {log}")
    with log.open(encoding="utf-8", errors="replace") as stream:
        header = "".join(line for _, line in zip(range(120), stream))
    if not all(re.search(r"^" + key + r":\s*" + re.escape(value) + r"\s*$", header, re.M)
               for key, value in (("model", MODEL), ("reasoning effort", EFFORT))):
        raise ValueError(f"could not confirm the requested model and effort in {log}")
    if inventory(work) != original:
        raise ValueError(f"the read-only subject changed its fixture; retained it at {work}")
    files = list(state.glob("innerloop-session-*.json"))
    if len(files) != 1:
        raise ValueError(f"expected one native session file; found {len(files)}")
    terminal = read_json(files[0]).get("terminal")
    if not terminal or terminal.get("status") not in ("complete", "stopped") or terminal.get("workspace") != str(work):
        raise ValueError("no terminal root for the expected audit workspace")
    reason = CASES[family][case]
    expected = "stopped" if reason else "complete"
    report = terminal["last_report"]
    matches = terminal["status"] == expected and (
        reason not in STOP_REASONS or report["continuation_assessment"] == "cancelled")
    capture = {"family": family, "case": case, "workspace": str(work), "model": MODEL, "effort": EFFORT,
               "card_sha256": hashlib.sha256(card.read_bytes()).hexdigest(), "expected_status": expected,
               "expected_reason": reason, "actual_status": terminal["status"], "status_matches": matches,
               "last_report": report, "exit_checks": terminal["exit_checks"], "repairs": terminal["check_repairs"]}
    work.rename(run / "audited")
    work.parent.rmdir()
    write_json(run / "capture.json", capture)
    print(f"{family}/{case}: expected {expected}, got {terminal['status']}; {len(terminal['exit_checks'])} frozen checks")
    print(f"Evidence: {run / 'capture.json'}")
    return 0 if matches else 1


@contextmanager
def replay_workspace(run, captured):
    """Use the original absolute cwd for frozen commands; never replace an existing workspace."""
    trial = run / "trial"
    work = trial / "work"
    if str(work) != captured["workspace"]:
        raise ValueError("replay needs the original evidence directory path")
    trial.mkdir()  # Atomic reservation. Leave an interrupted or concurrent trial alone.
    try:
        yield work
    finally:
        shutil.rmtree(trial)


def run_check(work, check, log, scratch):
    # A writable scratch directory permits fixture-local temporary imports and test CSVs.
    # The original shell text runs from the original fixture path, including for absolute-path checks.
    env = {**os.environ, "TMPDIR": str(scratch)}
    args = ["codex", "sandbox", "-P", ":workspace", "-C", str(scratch), "--", "env", "-i",
            "PATH=" + os.environ["PATH"], "HOME=" + str(scratch), "TMPDIR=" + str(scratch),
            "PYTHONDONTWRITEBYTECODE=1", "GIT_CEILING_DIRECTORIES=" + str(work.parent),
            "/bin/sh", "-c", 'cd "$1" && exec /bin/sh -c "$2"', "replay", str(work), check["command"]]
    try:
        code = execute(args, cwd=scratch, env=env, log=log, timeout=30)
    except subprocess.TimeoutExpired:
        code = None
    return {"id": check["id"], "exit_code": code, "log": str(log.name)}


def verdicts(cases, rows):
    """Only count a differential response when that same check passed the valid controls."""
    readiness_cases = [name for name, reason in cases.items() if reason is None or reason in STOP_REASONS]
    calibrated = set.intersection(*[{c["id"] for c in rows[name] if c["exit_code"] == 0}
                                    for name in readiness_cases])
    verdict = {}
    for name, checks in rows.items():
        failed = [c for c in checks if c["exit_code"] != 0]
        if not checks:
            result = "UNCOVERED"
        elif any(c["exit_code"] is None or c["exit_code"] in (126, 127) or c["exit_code"] < 0 for c in failed):
            result = "CHECK_ERROR"
        elif name in readiness_cases:
            result = "FALSE_REFUSAL" if failed else "PASS"
        elif not failed:
            result = "FALSE_PASS"
        elif any(c["id"] in calibrated for c in failed):
            result = "DIFFERENTIATED"
        else:
            result = "UNRESOLVED"
        verdict[name] = result
    return verdict


def replay(run):
    captured = read_json(run / "capture.json")
    cases = CASES[captured["family"]]
    checks = captured["exit_checks"]
    rows = {}
    with replay_workspace(run, captured) as work, tempfile.TemporaryDirectory(prefix="until-loop-check-") as tmp:
        scratch = Path(tmp).resolve()
        logs = Path(tempfile.mkdtemp(prefix="check-logs-", dir=run))
        # Do not misreport a missing sandbox or unusable permission profile as a business failure.
        probe = run_check(scratch, {"id": "preflight", "command": "exit 0"}, logs / "preflight.log", scratch)
        if probe["exit_code"] != 0:
            raise ValueError(f"Codex sandbox preflight failed; see {logs / 'preflight.log'}")
        for case in cases:
            work.mkdir()
            render(work, captured["family"], case)
            original = inventory(work)
            if case == captured["case"] and original != inventory(run / "audited"):
                raise ValueError("the seed fixture differs from the archived audit; replay with its source revision")
            rows[case] = []
            for i, check in enumerate(checks):
                with tempfile.TemporaryDirectory(dir=scratch) as check_tmp:
                    rows[case].append(run_check(work, check, logs / f"{case}-{i}.log", Path(check_tmp)))
                if inventory(work) != original:
                    raise ValueError(f"{check['id']} modified the read-only {case} fixture; see {logs}")
            shutil.rmtree(work)
        verdict = verdicts(cases, rows)
        summary = {name: {"expected_failure": cases[name], "verdict": verdict[name], "checks": results}
                   for name, results in rows.items()}
        write_json(logs / "replay.json", summary)
    for name, result in verdict.items():
        print(f"{name:25} {result}")
    print(f"Evidence: {logs / 'replay.json'}")
    return int(any(result not in {"PASS", "DIFFERENTIATED"} for result in verdict.values()))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_subparsers(dest="mode", required=True)
    new = modes.add_parser("live", help="run a read-only audit with a fresh Codex Sol session")
    new.add_argument("family", choices=REQUESTS)
    new.add_argument("case", help="seed fixture, e.g. valid, docs-last-wins, wrong-build or distribution-hold")
    new.add_argument("directory", type=Path, help="new evidence directory under an existing parent")
    again = modes.add_parser("replay", help="test the recorded checks against every variant, without a model call")
    again.add_argument("directory", type=Path, help="existing live evidence directory")
    args = parser.parse_args()
    run = args.directory.resolve()
    try:
        return live(args.family, args.case, run) if args.mode == "live" else replay(run)
    except (OSError, ValueError, subprocess.TimeoutExpired) as error:
        print(f"INCOMPLETE: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
