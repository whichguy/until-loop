#!/usr/bin/env python3
"""Standalone relocated-plugin end-to-end harness for Until Loop.

Copies only `plugins/until-loop` into an isolated temporary path with spaces,
scrubs repository PYTHONPATH and session overrides, parses the generated hook
manifests, and runs five end-to-end scenarios across Codex, Claude Code, and
Grok hooks, check hardening, nested unwinding, and mid-verify Git mutations.
"""
from __future__ import annotations

import json
import os
import select
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
PLUGIN_SRC = ROOT / "plugins" / "until-loop"
IDENTITY_ENV = (
    "PYTHONPATH",
    "UNTIL_LOOP_SESSION_ID",
    "UNTIL_LOOP_STATE_DIR",
    "CODEX_THREAD_ID",
    "CODEX_SESSION_ID",
    "CLAUDE_CODE_SESSION_ID",
    "GROK_SESSION_ID",
    "GROK_HOOK_EVENT",
)
PACKET_MARKER = "Until Loop recovery packet:\n"
FALLBACK_PROMPT = (
    "execute this exact read-only argv to retrieve the COMPLETE current packet; "
    "do not start a new loop or use callbacks from the summary:\n"
)
TIMEOUT = 20


def _clean_env(state_dir: Path, **extra: str) -> dict[str, str]:
    env = dict(os.environ)
    for name in IDENTITY_ENV:
        env.pop(name, None)
    env["PATH"] = str(Path(sys.executable).resolve().parent) + os.pathsep + env.get("PATH", os.defpath)
    env["LANG"] = "C"
    env["LC_ALL"] = "C"
    env["UNTIL_LOOP_STATE_DIR"] = str(state_dir)
    env.update(extra)
    return env


def _run(argv: list[str], *, cwd: Path, env: dict[str, str], payload: Any = None, expected_rc: int = 0) -> subprocess.CompletedProcess:
    stdin_data = None
    if payload is not None:
        stdin_data = payload if isinstance(payload, str) else json.dumps(payload)
    proc = subprocess.run(
        [str(x) for x in argv],
        input=stdin_data,
        capture_output=True,
        text=True,
        cwd=cwd,
        env=env,
        timeout=TIMEOUT,
        check=False,
    )
    if proc.returncode != expected_rc:
        raise AssertionError(
            f"command {argv!r} exited {proc.returncode} (expected {expected_rc}):\n"
            f"stdout={proc.stdout}\nstderr={proc.stderr}"
        )
    return proc


def _git_init(workspace: Path) -> None:
    for args in (
        ["init", "-q"],
        ["add", "-A"],
        ["-c", "user.name=harness", "-c", "user.email=h@example.invalid", "commit", "-qm", "seed", "--allow-empty"],
    ):
        subprocess.run(["git", *args], cwd=workspace, check=True, capture_output=True)


def _contract(workspace: Path, work: str, *, checks: list[dict[str, str]] | None = None,
              request: str = "Complete the task cleanly.", reviews: int = 0) -> dict[str, Any]:
    return {
        "workspace": str(workspace),
        "work": work,
        "exit_condition": "All checks and requirements pass.",
        "repeat_condition": "Continue while authorized work remains.",
        "required_trivial_reviews": reviews,
        "context": {
            "request": request,
            "scope": f"Only {workspace}.",
            "authority": "Local edits and checks only.",
            "environment": "Relocated plugin package.",
            "resources": [],
        },
        "exit_checks": [] if checks is None else checks,
    }


def _report(*, classification: str = "non-trivial", exit_assessment: str = "satisfied",
            continuation: str = "allowed", evidence: str = "Verified.", handoff: str = "State retained.",
            repairs: list[dict[str, str]] | None = None) -> dict[str, Any]:
    body: dict[str, Any] = {
        "classification": classification,
        "exit_assessment": exit_assessment,
        "continuation_assessment": continuation,
        "evidence": evidence,
        "handoff": handoff,
    }
    if repairs is not None:
        body["check_repairs"] = repairs
    return body


def _hook_command(package: Path, host: str, event_name: str) -> tuple[str, str]:
    if host == "codex":
        data = json.loads((package / "hooks" / "codex.json").read_text(encoding="utf-8"))
        root_var = "PLUGIN_ROOT"
    else:
        data = json.loads((package / "hooks" / "hooks.json").read_text(encoding="utf-8"))
        root_var = "CLAUDE_PLUGIN_ROOT"
    for entry in data["hooks"][event_name]:
        for hook in entry["hooks"]:
            cmd = hook["command"]
            if f"--host {host}" in cmd:
                return cmd, root_var
    raise AssertionError(f"missing {host} {event_name} hook command")


def _extract_packet(hook_stdout: str, *, env: dict[str, str], cwd: Path) -> dict[str, Any]:
    payload = json.loads(hook_stdout)
    hook_out = payload["hookSpecificOutput"]
    text = hook_out.get("permissionDecisionReason", hook_out.get("additionalContext", ""))
    idx = text.find(PACKET_MARKER)
    if idx >= 0:
        return json.loads(text[idx + len(PACKET_MARKER):].strip())
    assert FALLBACK_PROMPT in text, text
    encoded = text.split(FALLBACK_PROMPT, 1)[1].split("\nRead the returned", 1)[0]
    argv = json.loads(encoded)
    return json.loads(_run(argv, cwd=cwd, env=env).stdout)


class RelocatedSandbox:
    def __init__(self, base: Path, name: str):
        self.root = base / name
        self.root.mkdir()
        self.package = self.root / "plugin cache with spaces" / "until-loop"
        self.package.parent.mkdir()
        shutil.copytree(PLUGIN_SRC, self.package)
        self.runtime = self.package / "skills" / "until-loop" / "scripts" / "until_loop_ephemeral.py"
        self.workspace = self.root / "work dir"
        self.other_cwd = self.root / "unrelated cwd"
        self.state_dir = self.root / "state dir"
        for d in (self.workspace, self.other_cwd, self.state_dir):
            d.mkdir()

    def assert_clean_package(self) -> None:
        caches = list(self.package.rglob("__pycache__"))
        if caches:
            raise AssertionError(f"unexpected __pycache__ in relocated plugin: {caches}")


def scenario_codex_stop_guard_and_multi_check_chain(base: Path) -> str:
    sb = RelocatedSandbox(base, "s1-codex")
    names = [f"c{i}" for i in range(1, 6)]
    for n in names[:3]:
        (sb.workspace / n).write_text("ok", encoding="utf-8")
    _git_init(sb.workspace)
    env = _clean_env(sb.state_dir, CODEX_THREAD_ID="e2e-codex-1", PYTHONDONTWRITEBYTECODE="1")
    checks = [{"id": n, "clause": f"{n} exists", "command": f"test -f {n}"} for n in names]
    pk = json.loads(_run([sys.executable, str(sb.runtime), "start"], cwd=sb.other_cwd, env=env,
                         payload=_contract(sb.workspace, "Create c1..c5.", checks=checks)).stdout)
    assert str(sb.runtime) in pk["done_argv"][1]

    stop_cmd, stop_var = _hook_command(sb.package, "codex", "Stop")
    hook_env = _clean_env(sb.state_dir, CODEX_THREAD_ID="e2e-codex-1", **{stop_var: str(sb.package)})
    stop1 = json.loads(_run(["/bin/sh", "-c", stop_cmd], cwd=sb.other_cwd, env=hook_env,
                            payload={"hook_event_name": "Stop", "session_id": "e2e-codex-1",
                                     "stop_hook_active": False}).stdout)
    assert stop1["decision"] == "block"
    assert "no recorded run yet" in stop1["reason"]

    stop2 = _run(["/bin/sh", "-c", stop_cmd], cwd=sb.other_cwd, env=hook_env,
                 payload={"hook_event_name": "Stop", "session_id": "e2e-codex-1", "stop_hook_active": True})
    assert stop2.stdout == ""

    _run(["/bin/sh", "-c", pk["verify_script"]], cwd=sb.other_cwd, env=env)
    pk2 = json.loads(_run(pk["done_argv"], cwd=sb.other_cwd, env=env, payload=_report()).stdout)
    assert pk2["status"] == "active"

    start_cmd, start_var = _hook_command(sb.package, "codex", "SessionStart")
    start_env = _clean_env(sb.state_dir, CODEX_THREAD_ID="e2e-codex-1", **{start_var: str(sb.package)})
    recovered = _extract_packet(
        _run(["/bin/sh", "-c", start_cmd], cwd=sb.other_cwd, env=start_env,
             payload={"hook_event_name": "SessionStart", "source": "compact", "session_id": "e2e-codex-1"}).stdout,
        env=env, cwd=sb.other_cwd,
    )
    assert recovered["progress"]["action_number"] == 2
    assert [r["id"] for r in recovered["last_check_results"] if not r["passed"]] == ["c4", "c5"]

    for n in names[3:]:
        (sb.workspace / n).write_text("ok", encoding="utf-8")
    _run(["/bin/sh", "-c", recovered["verify_script"]], cwd=sb.other_cwd, env=env)
    terminal = json.loads(_run(recovered["done_argv"], cwd=sb.other_cwd, env=env, payload=_report()).stdout)
    assert terminal["status"] == "complete"
    assert _run(["/bin/sh", "-c", stop_cmd], cwd=sb.other_cwd, env=hook_env,
                payload={"hook_event_name": "Stop", "session_id": "e2e-codex-1",
                         "stop_hook_active": False}).stdout == ""
    sb.assert_clean_package()
    return "5 checks, Stop block/guard, SessionStart compact recovery, and clean terminal"


def scenario_claude_stop_guard_and_budget_fallback(base: Path) -> str:
    sb = RelocatedSandbox(base, "s2-claude")
    env = _clean_env(sb.state_dir, CLAUDE_CODE_SESSION_ID="e2e-claude-1", PYTHONDONTWRITEBYTECODE="1")
    checks = [{"id": "out", "clause": "out.txt exists", "command": "test -f out.txt"}]
    pk = json.loads(_run(
        [sys.executable, str(sb.runtime), "start"],
        cwd=sb.other_cwd,
        env=env,
        payload=_contract(sb.workspace, "Produce out.txt.", checks=checks, request="R " + ("x" * 12_000)),
    ).stdout)
    _run(["/bin/sh", "-c", pk["verify_script"]], cwd=sb.other_cwd, env=env)
    pk2 = json.loads(_run(pk["done_argv"], cwd=sb.other_cwd, env=env, payload=_report()).stdout)
    assert pk2["status"] == "active"

    stop_cmd, root_var = _hook_command(sb.package, "claude", "Stop")
    hook_env = _clean_env(sb.state_dir, CLAUDE_CODE_SESSION_ID="e2e-claude-1", **{root_var: str(sb.package)})
    blocked = json.loads(_run(["/bin/sh", "-c", stop_cmd], cwd=sb.other_cwd, env=hook_env,
                              payload={"hook_event_name": "Stop", "session_id": "e2e-claude-1",
                                       "stop_hook_active": False}).stdout)
    assert blocked["decision"] == "block"
    assert "out (exit 1)" in blocked["reason"]

    start_cmd, _ = _hook_command(sb.package, "claude", "SessionStart")
    start_out = _run(["/bin/sh", "-c", start_cmd], cwd=sb.other_cwd, env=hook_env,
                     payload={"hook_event_name": "SessionStart", "source": "compact",
                              "session_id": "e2e-claude-1"}).stdout
    assert PACKET_MARKER not in start_out
    fetched = _extract_packet(start_out, env=env, cwd=sb.other_cwd)
    assert str(sb.runtime) in fetched["done_argv"][1]

    (sb.workspace / "out.txt").write_text("done", encoding="utf-8")
    _run(["/bin/sh", "-c", fetched["verify_script"]], cwd=sb.other_cwd, env=env)
    terminal = json.loads(_run(fetched["done_argv"], cwd=sb.other_cwd, env=env, payload=_report()).stdout)
    assert terminal["status"] == "complete"
    assert _run(["/bin/sh", "-c", stop_cmd], cwd=sb.other_cwd, env=hook_env,
                payload={"hook_event_name": "Stop", "session_id": "e2e-claude-1",
                         "stop_hook_active": False}).stdout == ""
    sb.assert_clean_package()
    return "Claude Stop block, 9.5KB fallback fetch, completion, and post-terminal silence"


def scenario_grok_active_stop_and_compaction_gate(base: Path) -> str:
    sb = RelocatedSandbox(base, "s3-grok")
    sid = "e2e-grok-1"
    env = _clean_env(sb.state_dir, GROK_SESSION_ID=sid, PYTHONDONTWRITEBYTECODE="1")
    checks = [{"id": "g1", "clause": "g1 exists", "command": "test -f g1"}]
    pk1 = json.loads(_run([sys.executable, str(sb.runtime), "start"], cwd=sb.other_cwd, env=env,
                          payload=_contract(sb.workspace, "Write g1.", checks=checks)).stdout)

    stop_cmd, root_var = _hook_command(sb.package, "grok", "Stop")
    stop_env = _clean_env(sb.state_dir, GROK_SESSION_ID=sid, GROK_HOOK_EVENT="stop", **{root_var: str(sb.package)})
    stop_payload = {"hookEventName": "stop", "hook_event_name": "Stop", "sessionId": sid, "session_id": sid,
                    "timestamp": "2026-11-24T10:00:00Z", "reason": "end_turn"}
    nudge1 = json.loads(_run(["/bin/sh", "-c", stop_cmd], cwd=sb.other_cwd, env=stop_env, payload=stop_payload).stdout)
    assert "action 1" in nudge1["hookSpecificOutput"]["additionalContext"]
    assert _run(["/bin/sh", "-c", stop_cmd], cwd=sb.other_cwd, env=stop_env, payload=stop_payload).stdout == ""

    pk2 = json.loads(_run(pk1["done_argv"], cwd=sb.other_cwd, env=env,
                          payload=_report(exit_assessment="unsatisfied")).stdout)
    nudge2 = json.loads(_run(["/bin/sh", "-c", stop_cmd], cwd=sb.other_cwd, env=stop_env, payload=stop_payload).stdout)
    assert "action 2" in nudge2["hookSpecificOutput"]["additionalContext"]

    arm_cmd, _ = _hook_command(sb.package, "grok", "PreCompact")
    arm_env = _clean_env(sb.state_dir, GROK_SESSION_ID=sid, GROK_HOOK_EVENT="pre_compact", **{root_var: str(sb.package)})
    _run(["/bin/sh", "-c", arm_cmd], cwd=sb.other_cwd, env=arm_env,
         payload={"hookEventName": "pre_compact", "hook_event_name": "PreCompact", "sessionId": sid,
                  "session_id": sid, "timestamp": "2026-11-24T10:01:00Z", "source": "manual"})

    tool_cmd, _ = _hook_command(sb.package, "grok", "PreToolUse")
    tool_env = _clean_env(sb.state_dir, GROK_SESSION_ID=sid, GROK_HOOK_EVENT="pre_tool_use", **{root_var: str(sb.package)})
    tool_res = _run(["/bin/sh", "-c", tool_cmd], cwd=sb.other_cwd, env=tool_env,
                    payload={"hookEventName": "pre_tool_use", "hook_event_name": "PreToolUse", "sessionId": sid,
                             "session_id": sid, "timestamp": "2026-11-24T10:01:01Z", "toolName": "sh"})
    recovered = _extract_packet(tool_res.stdout, env=env, cwd=sb.other_cwd)
    assert recovered["progress"]["action_number"] == 2

    (sb.workspace / "g1").write_text("ok", encoding="utf-8")
    _run(["/bin/sh", "-c", recovered["verify_script"]], cwd=sb.other_cwd, env=env)
    terminal = json.loads(_run(recovered["done_argv"], cwd=sb.other_cwd, env=env, payload=_report()).stdout)
    assert terminal["status"] == "complete"
    assert _run(["/bin/sh", "-c", stop_cmd], cwd=sb.other_cwd, env=stop_env, payload=stop_payload).stdout == ""
    sb.assert_clean_package()
    return "Grok active Stop nudges once per action, PreCompact+PreToolUse recovers, terminal stays quiet"


def scenario_check_execution_hardening(base: Path) -> str:
    sb = RelocatedSandbox(base, "s4-hardening")
    env = _clean_env(sb.state_dir, UNTIL_LOOP_SESSION_ID="e2e-hardening", PYTHONDONTWRITEBYTECODE="1")
    pk = json.loads(_run(
        [sys.executable, str(sb.runtime), "start"],
        cwd=sb.other_cwd,
        env=env,
        payload=_contract(sb.workspace, "Check hardening.", checks=[
            {"id": "H1", "clause": "both succeed", "command": "false; true"},
            {"id": "H2", "clause": "file exists", "command": "test -f wrong_name"},
        ]),
    ).stdout)
    _run(["/bin/sh", "-c", pk["verify_script"]], cwd=sb.other_cwd, env=env)
    pk2 = json.loads(_run(pk["done_argv"], cwd=sb.other_cwd, env=env, payload=_report()).stdout)
    assert pk2["status"] == "active"
    assert [(r["id"], r["passed"]) for r in pk2["last_check_results"]] == [("H1", False), ("H2", False)]

    for bad_repair in ("true", "test -f real_name || true"):
        err = json.loads(_run(
            pk2["done_argv"],
            cwd=sb.other_cwd,
            env=env,
            payload=_report(
                exit_assessment="unsatisfied",
                repairs=[{"id": "H2", "command": bad_repair, "reason": "bypass"}],
            ),
            expected_rc=2,
        ).stdout)
        assert err["status"] == "error"
        assert "vacuous" in err["error"]

    repaired = json.loads(_run(
        pk2["done_argv"],
        cwd=sb.other_cwd,
        env=env,
        payload=_report(
            exit_assessment="unsatisfied",
            repairs=[
                {"id": "H1", "command": "test -f h1.txt", "reason": "match clause"},
                {"id": "H2", "command": "test -f real_name", "reason": "fix target filename"},
            ],
        ),
    ).stdout)
    (sb.workspace / "h1.txt").write_text("1", encoding="utf-8")
    (sb.workspace / "real_name").write_text("1", encoding="utf-8")
    _run(["/bin/sh", "-c", repaired["verify_script"]], cwd=sb.other_cwd, env=env)
    terminal = json.loads(_run(repaired["done_argv"], cwd=sb.other_cwd, env=env, payload=_report()).stdout)
    assert terminal["status"] == "complete"
    assert len(terminal["check_repairs"]) == 2
    sb.assert_clean_package()
    return "Multi-statement set -e failure, vacuous repair refusal, and genuine repair audit"


def scenario_mid_verify_mutation_and_nested_unwind(base: Path) -> str:
    sb = RelocatedSandbox(base, "s5-nested-race")
    f1 = sb.workspace / "f1.txt"
    f2 = sb.workspace / "f2.txt"
    f1.write_text("ready", encoding="utf-8")
    f2.write_text("pending", encoding="utf-8")
    _git_init(sb.workspace)
    env = _clean_env(sb.state_dir, UNTIL_LOOP_SESSION_ID="e2e-nested-race", PYTHONDONTWRITEBYTECODE="1")

    waiting, proceed = sb.root / "waiting", sb.root / "proceed"
    os.mkfifo(waiting)
    os.mkfifo(proceed)
    parent_checks = [
        {"id": "P1", "clause": "f1 is ready", "command": 'test "$(cat f1.txt)" = ready'},
        {"id": "P2", "clause": "f2 is ready",
         "command": (
             f'printf "waiting\\n" > {shlex.quote(str(waiting))} && '
             f'IFS= read -r _ev < {shlex.quote(str(proceed))} && '
             'test "$(cat f2.txt)" = ready'
         )},
    ]
    parent = json.loads(_run([sys.executable, str(sb.runtime), "start"], cwd=sb.other_cwd, env=env,
                              payload=_contract(sb.workspace, "Parent work.", checks=parent_checks)).stdout)
    child = json.loads(_run([sys.executable, str(sb.runtime), "start"], cwd=sb.other_cwd, env=env,
                             payload=_contract(sb.workspace, "Child builds f2.", checks=[
                                 {"id": "CH1", "clause": "f2 exists", "command": "test -f f2.txt"}
                             ])).stdout)
    assert child["stack"]["depth"] == 2
    _run(["/bin/sh", "-c", child["verify_script"]], cwd=sb.other_cwd, env=env)
    resumed = json.loads(_run(child["done_argv"], cwd=sb.other_cwd, env=env, payload=_report()).stdout)
    assert resumed["stack"]["depth"] == 1

    with os.fdopen(os.open(waiting, os.O_RDWR | os.O_NONBLOCK), "rb", buffering=0) as rd, \
            os.fdopen(os.open(proceed, os.O_RDWR | os.O_NONBLOCK), "wb", buffering=0) as wr, \
            subprocess.Popen(["/bin/sh", "-c", resumed["verify_script"]], cwd=sb.other_cwd, env=env,
                             stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             text=True, start_new_session=True) as proc:
        try:
            assert select.select([rd], [], [], TIMEOUT)[0] and rd.read(8) == b"waiting\n"
            f1.write_text("pending", encoding="utf-8")
            f2.write_text("ready", encoding="utf-8")
            wr.write(b"go\n")
            stdout, stderr = proc.communicate(timeout=TIMEOUT)
        finally:
            if proc.poll() is None:
                os.killpg(proc.pid, signal.SIGKILL)
        assert proc.returncode == 2, (proc.returncode, stdout, stderr)
        assert "UNTIL-LOOP-RECORD-ERROR" in stdout

    refused = json.loads(_run(resumed["done_argv"], cwd=sb.other_cwd, env=env,
                              payload=_report(), expected_rc=2).stdout)
    assert refused["status"] == "error"
    assert "changed during the recorded verify run" in refused["error"]

    # Stabilize both files and replace FIFO check cleanly via quiet re-verify after restoring f1.
    f1.write_text("ready", encoding="utf-8")
    with os.fdopen(os.open(waiting, os.O_RDWR | os.O_NONBLOCK), "rb", buffering=0) as rd, \
            os.fdopen(os.open(proceed, os.O_RDWR | os.O_NONBLOCK), "wb", buffering=0) as wr, \
            subprocess.Popen(["/bin/sh", "-c", resumed["verify_script"]], cwd=sb.other_cwd, env=env,
                             stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             text=True, start_new_session=True) as proc:
        try:
            assert select.select([rd], [], [], TIMEOUT)[0] and rd.read(8) == b"waiting\n"
            wr.write(b"go\n")
            stdout, stderr = proc.communicate(timeout=TIMEOUT)
        finally:
            if proc.poll() is None:
                os.killpg(proc.pid, signal.SIGKILL)
        assert proc.returncode == 0, (proc.returncode, stdout, stderr)

    terminal = json.loads(_run(resumed["done_argv"], cwd=sb.other_cwd, env=env, payload=_report()).stdout)
    assert terminal["status"] == "complete"
    assert len(terminal["stack"]["child_results"]) == 1
    sb.assert_clean_package()
    return "Nested unwind, FIFO mid-verify mutation refusal, and quiet re-verify completion"


def scenario_empty_git_repo_codex_lifecycle(base: Path) -> str:
    sb = RelocatedSandbox(base, "s6-empty-repo-codex")
    subprocess.run(["git", "init", "-q"], cwd=sb.workspace, check=True, capture_output=True)
    assert [p.name for p in sb.workspace.iterdir()] == [".git"]
    env = _clean_env(sb.state_dir, CODEX_THREAD_ID="e2e-empty-repo-codex-1", PYTHONDONTWRITEBYTECODE="1")
    checks = [
        {"id": "files", "clause": "app.py and test_app.py exist", "command": "test -f app.py && test -f test_app.py"},
        {"id": "api", "clause": "add works", "command": "python3 -B -c 'import app; assert app.add(2, 3) == 5'"},
        {"id": "cli", "clause": "CLI exits 0", "command": "python3 -B app.py 2 3 | grep -qx '5'"},
        {"id": "tests", "clause": "unittest passes", "command": "python3 -B -m unittest -q test_app.py"},
        {"id": "readme", "clause": "README exists", "command": "test -s README.md"},
    ]
    pk1 = json.loads(_run(
        [sys.executable, str(sb.runtime), "start"],
        cwd=sb.other_cwd,
        env=env,
        payload=_contract(sb.workspace, "Build CLI calc in empty repo.", checks=checks),
    ).stdout)

    stop_cmd, stop_var = _hook_command(sb.package, "codex", "Stop")
    hook_env = _clean_env(sb.state_dir, CODEX_THREAD_ID="e2e-empty-repo-codex-1", **{stop_var: str(sb.package)})
    stop_early = json.loads(_run(["/bin/sh", "-c", stop_cmd], cwd=sb.other_cwd, env=hook_env,
                                 payload={"hook_event_name": "Stop", "session_id": "e2e-empty-repo-codex-1",
                                          "stop_hook_active": False}).stdout)
    assert "no recorded run yet" in stop_early["reason"]

    (sb.workspace / "app.py").write_text(
        "import sys\n"
        "def add(a, b): return a + b\n"
        "if __name__ == '__main__': print(add(int(sys.argv[1]), int(sys.argv[2])))\n",
        encoding="utf-8",
    )
    _run(["/bin/sh", "-c", pk1["verify_script"]], cwd=sb.other_cwd, env=env)
    pk2 = json.loads(_run(pk1["done_argv"], cwd=sb.other_cwd, env=env, payload=_report()).stdout)
    assert pk2["status"] == "active"
    assert [r["id"] for r in pk2["last_check_results"] if not r["passed"]] == ["files", "tests", "readme"]

    (sb.workspace / "test_app.py").write_text(
        "import unittest, app\n"
        "class T(unittest.TestCase):\n"
        "    def test_add(self): self.assertEqual(app.add(2, 3), 5)\n",
        encoding="utf-8",
    )
    (sb.workspace / "README.md").write_text("# app\n", encoding="utf-8")
    _run(["/bin/sh", "-c", pk2["verify_script"]], cwd=sb.other_cwd, env=env)
    done_build = json.loads(_run(pk2["done_argv"], cwd=sb.other_cwd, env=env, payload=_report()).stdout)
    assert done_build["status"] == "complete"
    assert _run(["/bin/sh", "-c", stop_cmd], cwd=sb.other_cwd, env=hook_env,
                payload={"hook_event_name": "Stop", "session_id": "e2e-empty-repo-codex-1",
                         "stop_hook_active": False}).stdout == ""

    # Second empty Git repo with an immediate stop trigger -> clean cancelled exit.
    stop_ws = sb.root / "empty stop repo"
    stop_ws.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=stop_ws, check=True, capture_output=True)
    (stop_ws / "distribution-hold.txt").write_text("HOLD: do not proceed\n", encoding="utf-8")
    cancel_env = _clean_env(sb.state_dir, CODEX_THREAD_ID="e2e-empty-repo-codex-2", PYTHONDONTWRITEBYTECODE="1")
    pk_cancel = json.loads(_run(
        [sys.executable, str(sb.runtime), "start"],
        cwd=sb.other_cwd,
        env=cancel_env,
        payload=_contract(stop_ws, "Check hold and proceed only if clear.",
                          checks=[{"id": "out", "clause": "output exists", "command": "test -f out.txt"}]),
    ).stdout)
    term_cancel = json.loads(_run(
        pk_cancel["done_argv"],
        cwd=sb.other_cwd,
        env=cancel_env,
        payload=_report(classification="non-trivial", exit_assessment="unsatisfied",
                        continuation="cancelled", evidence="distribution-hold.txt present."),
    ).stdout)
    assert term_cancel["status"] == "stopped"
    cancel_hook_env = _clean_env(sb.state_dir, CODEX_THREAD_ID="e2e-empty-repo-codex-2", **{stop_var: str(sb.package)})
    assert _run(["/bin/sh", "-c", stop_cmd], cwd=sb.other_cwd, env=cancel_hook_env,
                payload={"hook_event_name": "Stop", "session_id": "e2e-empty-repo-codex-2",
                         "stop_hook_active": False}).stdout == ""
    sb.assert_clean_package()
    return "Empty Git repos (unborn HEAD) complete and cancel cleanly under relocated Codex plugin"


SCENARIOS = (
    ("codex_stop_guard_and_multi_check_chain", scenario_codex_stop_guard_and_multi_check_chain),
    ("claude_stop_guard_and_budget_fallback", scenario_claude_stop_guard_and_budget_fallback),
    ("grok_active_stop_and_compaction_gate", scenario_grok_active_stop_and_compaction_gate),
    ("check_execution_hardening", scenario_check_execution_hardening),
    ("mid_verify_mutation_and_nested_unwind", scenario_mid_verify_mutation_and_nested_unwind),
    ("empty_git_repo_codex_lifecycle", scenario_empty_git_repo_codex_lifecycle),
)


def run_all() -> dict[str, Any]:
    results = []
    with tempfile.TemporaryDirectory(prefix="until-loop-e2e-harness-") as tmp:
        base = Path(tmp).resolve()
        for name, fn in SCENARIOS:
            detail = fn(base)
            results.append({"name": name, "passed": True, "detail": detail})
    return {"passed": True, "scenarios": results}


def main() -> int:
    summary = run_all()
    print(json.dumps(summary, indent=2))
    return 0 if summary["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
