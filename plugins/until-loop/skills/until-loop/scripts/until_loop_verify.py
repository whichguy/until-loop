"""Verification helpers, exit-check validation, and workspace tree snapshots."""
from __future__ import annotations

import os
import re
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any


CHECK_OUTPUT_CHARS = 300
MAX_REPAIRS_PER_CHECK = 2
_CHECK_FIELDS = {"id", "clause", "command"}
_CHECK_ID = re.compile(r"[A-Za-z0-9._-]{1,64}")
_REPAIR_FIELDS = {"id", "command", "reason"}
_VERIFY_DELIMITER = "__UNTIL_LOOP_VERIFY__"
_CHECK_HEADER = "=== until-loop check "
_CHECK_RESULT = "UNTIL-LOOP-CHECK"
_TREE_BEFORE = "UNTIL-LOOP-TREE-BEFORE"
# Anchored at the end: a check whose output lacks a final newline still yields its result line.
_RESULT_LINE = re.compile(r"UNTIL-LOOP-CHECK (\S+) exit=(\d+)$")
_TREE_BEFORE_LINE = re.compile(r"^UNTIL-LOOP-TREE-BEFORE ([0-9a-f]{40}(?:[0-9a-f]{24})?|none)$")
# Runtime and review-note locations that never count as a change to the workspace.
_RUNTIME_EXCLUDES = (".shiploop-improve", ".shiploop", ".until-loop", ".git")
_TREE_ID = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?")
_TRIVIAL_PASS_STATEMENTS = frozenset({"true", ":", "exit 0"})
CHANGED_DURING_VERIFY_ERROR = (
    "the workspace changed during the verify run: run this packet's "
    "verify_script again on a quiet workspace"
)


class StateError(ValueError):
    """Raised when callback state or protocol input is absent, unsafe, or invalid."""


def _require_text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise StateError(f"{name} must be a nonblank string")
    return value


def _check_command_syntax(check_id: str, command: str) -> None:
    """Refuse a command /bin/sh cannot parse (it would stop the whole verify script before anything is recorded)."""
    if "\x00" in command:
        raise StateError("exit check commands cannot contain NUL")
    if any(line.strip() == _VERIFY_DELIMITER for line in command.splitlines()):
        raise StateError(f"exit check {check_id!r} cannot contain the line {_VERIFY_DELIMITER}")
    try:
        parsed = subprocess.run(
            ["/bin/sh", "-n", "-c", command],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=10,
        )  # -n parses only; nothing runs
    except (OSError, subprocess.TimeoutExpired):
        return
    if parsed.returncode != 0:
        raise StateError(f"exit check {check_id!r} is not valid sh syntax: {parsed.stderr.strip()[-200:]}")


def _strip_outer_group(text: str) -> str:
    """Unwrap matching outer (...) or { ...; } when it spans the whole command."""
    while len(text) >= 2 and ((text[0] == "(" and text[-1] == ")") or (text[0] == "{" and text[-1] == "}")):
        opener, closer = text[0], text[-1]
        depth = 0
        quote: str | None = None
        escaped = False
        spans_all = True
        for idx, ch in enumerate(text):
            if escaped:
                escaped = False
                continue
            if quote == "'":
                if ch == "'":
                    quote = None
                continue
            if ch == "\\" and quote != "'":
                escaped = True
                continue
            if quote is not None:
                if ch == quote:
                    quote = None
                continue
            if ch in ("'", '"'):
                quote = ch
                continue
            if ch == opener:
                depth += 1
            elif ch == closer:
                depth -= 1
                if depth == 0 and idx < len(text) - 1:
                    spans_all = False
                    break
        if not spans_all or depth != 0:
            break
        text = text[1:-1].strip()
        if opener == "{":
            text = text.rstrip(";").strip()
    return text


def _split_top_level_clauses(text: str) -> list[tuple[str, str]]:
    """Split a shell snippet on top-level ;, &&, ||, or newlines, returning (preceding_op, clause)."""
    clauses: list[tuple[str, str]] = []
    buf: list[str] = []
    prev_op = ""
    quote: str | None = None
    escaped = False
    paren_depth = 0
    brace_depth = 0
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if escaped:
            buf.append(ch)
            escaped = False
            i += 1
            continue
        if quote == "'":
            buf.append(ch)
            if ch == "'":
                quote = None
            i += 1
            continue
        if ch == "\\" and quote != "'":
            buf.append(ch)
            escaped = True
            i += 1
            continue
        if quote is not None:
            buf.append(ch)
            if ch == quote:
                quote = None
            i += 1
            continue
        if ch in ("'", '"'):
            quote = ch
            buf.append(ch)
            i += 1
            continue
        if ch == "(":
            paren_depth += 1
            buf.append(ch)
            i += 1
            continue
        if ch == ")" and paren_depth > 0:
            paren_depth -= 1
            buf.append(ch)
            i += 1
            continue
        if ch == "{":
            brace_depth += 1
            buf.append(ch)
            i += 1
            continue
        if ch == "}" and brace_depth > 0:
            brace_depth -= 1
            buf.append(ch)
            i += 1
            continue
        if paren_depth == 0 and brace_depth == 0:
            two = text[i:i + 2]
            if two in ("&&", "||"):
                part = "".join(buf).strip()
                if part:
                    clauses.append((prev_op, part))
                buf = []
                prev_op = two
                i += 2
                continue
            if ch in (";", "\n"):
                part = "".join(buf).strip()
                if part:
                    clauses.append((prev_op, part))
                buf = []
                prev_op = ch
                i += 1
                continue
        buf.append(ch)
        i += 1
    tail = "".join(buf).strip()
    if tail:
        clauses.append((prev_op, tail))
    return clauses


def _is_vacuous_command(command: str) -> bool:
    """Return True if a repair command always passes unconditionally or masks failure via trailing pass."""
    stripped_lines = [line for line in command.splitlines() if line.strip() and not line.lstrip().startswith("#")]
    cleaned = _strip_outer_group("\n".join(stripped_lines).strip())
    if not cleaned:
        return True
    clauses = _split_top_level_clauses(cleaned)
    if not clauses:
        return True

    def is_trivial_pass(stmt: str, *, sole: bool) -> bool:
        inner = _strip_outer_group(stmt)
        if not inner:
            return True
        sub = _split_top_level_clauses(inner)
        if len(sub) > 1:
            return _is_vacuous_command(inner)
        normalized = " ".join(inner.split())
        if normalized in _TRIVIAL_PASS_STATEMENTS:
            return True
        return sole and normalized == "exit"

    if len(clauses) == 1:
        return is_trivial_pass(clauses[0][1], sole=True)
    if all(is_trivial_pass(stmt, sole=False) for _, stmt in clauses):
        return True
    last_op, last_stmt = clauses[-1]
    return last_op in ("||", ";", "\n") and is_trivial_pass(last_stmt, sole=False)


def _check_repair_not_vacuous(check_id: str, command: str) -> None:
    """Reject repairs that turn an exit check into a vacuous always-pass command."""
    if _is_vacuous_command(command):
        raise StateError(
            f"check {check_id!r} cannot be repaired to a vacuous always-pass command; "
            "fix the work or report blocked instead"
        )


def _validate_checks(checks: object, *, parse: bool = False) -> list[dict[str, str]]:
    """Frozen exit checks: shell commands written from the request before any work."""
    if not isinstance(checks, list):
        raise StateError("exit_checks must be a list of checks")
    out, seen = [], set()
    for check in checks:
        if not isinstance(check, dict) or set(check) != _CHECK_FIELDS:
            raise StateError("each exit check must have exactly id, clause and command")
        item = {k: _require_text(check[k], f"exit_checks.{k}") for k in ("id", "clause", "command")}
        if not _CHECK_ID.fullmatch(item["id"]):
            raise StateError("exit check ids must be 1-64 characters of letters, digits, '.', '_' or '-'")
        if "\x00" in item["command"]:
            raise StateError("exit check commands cannot contain NUL")
        if parse:  # new contracts only; frozen state was parsed when it was created
            _check_command_syntax(item["id"], item["command"])
        if item["id"] in seen:
            raise StateError("exit check ids must be unique")
        seen.add(item["id"])
        out.append(item)
    return out


def _verify_script(path: Path, state: dict[str, Any], *, script_path: Path, action_token: str) -> str | None:
    """The shell script the executor runs with its own shell tool: every frozen check, piped into `record`."""
    contract = state["contract"]
    if not contract["exit_checks"]:
        return None
    workspace = shlex.quote(contract["workspace"])
    exe = str(Path(os.path.abspath(sys.executable)))
    script = str(Path(os.path.abspath(script_path)))
    snapshot = shlex.join([exe, script, "snapshot", "--workspace", contract["workspace"]]) + " </dev/null"
    lines = [f"/bin/sh <<'{_VERIFY_DELIMITER}'", "{", snapshot]
    for check in contract["exit_checks"]:
        lines += [
            f"printf '%s\\n' {shlex.quote(_CHECK_HEADER + check['id'])}",
            f"(cd -- {workspace} || exit 125",
            "unset UNTIL_LOOP_SESSION_ID CODEX_THREAD_ID CODEX_SESSION_ID CLAUDE_CODE_SESSION_ID GROK_SESSION_ID",
            "set -e",
            check["command"],
            ") </dev/null 2>&1",
            f"printf '\\n{_CHECK_RESULT} %s exit=%s\\n' {shlex.quote(check['id'])} \"$?\"",
        ]
    record_cmd = shlex.join([exe, script, "record", "--state", str(path), f"--action={action_token}"])
    lines += ["} 2>&1 | " + record_cmd, _VERIFY_DELIMITER]
    return "\n".join(lines) + "\n"


def _bounded_lines(stream: Any, limit: int = 8192) -> Any:
    """Yield decoded lines from a byte stream in bounded memory; an over-long line keeps only its end."""
    pending = b""
    while True:
        chunk = stream.read(65536)
        if not chunk:
            break
        pending += chunk
        *lines, pending = pending.split(b"\n")
        for line in lines:
            yield line[-limit:].decode("utf-8", errors="replace") + "\n"
        pending = pending[-limit:]
    if pending:
        yield pending.decode("utf-8", errors="replace")


def _parse_verify_output(
    stream: Any,
    contract: dict[str, Any],
    echo: Any,
    *,
    before_fallback: Any = None,
) -> tuple[list[dict[str, Any]], str | None]:
    """Read verify_script output (echoing it), keeping each check's exit code and a bounded output tail."""
    ids = [c["id"] for c in contract["exit_checks"]]
    codes: dict[str, int] = {}
    tails: dict[str, str] = {i: "" for i in ids}
    current = None
    saw_check = False
    saw_before_tree = False
    before_tree: str | None = None
    for line in _bounded_lines(stream):
        stripped = line.rstrip("\n")
        if not saw_check and not saw_before_tree:
            tree_match = _TREE_BEFORE_LINE.fullmatch(stripped)
            if tree_match:
                saw_before_tree = True
                before_tree = None if tree_match.group(1) == "none" else tree_match.group(1)
                continue
        if not saw_before_tree:
            saw_before_tree = True
            if before_fallback is not None:
                before_tree = before_fallback()
        echo.write(line)
        if stripped.startswith(_CHECK_HEADER) and stripped[len(_CHECK_HEADER):] in tails:
            current = stripped[len(_CHECK_HEADER):]
            saw_check = True
            continue
        match = _RESULT_LINE.search(stripped)
        if match and match.group(1) in tails:
            codes[match.group(1)] = int(match.group(2))
            current = None
            continue
        if current is not None:
            tails[current] = (tails[current] + line)[-4 * CHECK_OUTPUT_CHARS:]
    if not saw_before_tree and before_fallback is not None:
        before_tree = before_fallback()
    results = []
    for i in ids:
        code = codes.get(i)
        passed = code == 0
        tail = "" if passed else (
            tails[i].strip()[-CHECK_OUTPUT_CHARS:]
            if code is not None
            else "no result line: the verify script did not finish this check"
        )
        results.append({"id": i, "passed": passed, "exit_code": code, "output_tail": tail})
    return results, before_tree


def _workspace_tree(workspace: str) -> str | None:
    """A content tree of the workspace (tracked and untracked, never ignored or runtime files).

    Built in a fresh private index so stale `.git/index` stat-cache entries
    cannot cause racy-Git mismatches and the real index stays untouched.
    ``None`` outside Git or on any Git error: then no pass can be shown unchanged.
    """
    def git(*args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["git", "-C", workspace, *args],
            capture_output=True,
            text=True,
            env=env,
            timeout=60,
            check=False,
        )

    try:
        located = git("rev-parse", "--is-inside-work-tree")
        if located.returncode or located.stdout.strip() != "true":
            return None
        fd, index = tempfile.mkstemp(prefix="until-loop-tree-", suffix=".index")
        os.close(fd)
        try:
            # Start empty so Git hashes non-ignored files cleanly without index stat-cache races.
            os.unlink(index)
            env = {**os.environ, "GIT_INDEX_FILE": index, "GIT_OPTIONAL_LOCKS": "0"}
            excludes = [":(exclude,glob)**/" + part + "/**" for part in _RUNTIME_EXCLUDES]
            excludes.extend((":(exclude,glob)**/innerloop-*.json", ":(exclude,glob)**/innerloop-*.json.lock"))
            if git("add", "-A", "--", ".", *excludes, env=env).returncode:
                return None
            written = git("write-tree", env=env)
            tree = written.stdout.strip()
            return tree if written.returncode == 0 and _TREE_ID.fullmatch(tree) else None
        finally:
            if os.path.exists(index):
                os.unlink(index)
    except (OSError, subprocess.SubprocessError):
        return None


def finalize_verify_record(
    workspace: str,
    action_number: int,
    initial_tree: str | None,
    *,
    action_token: str | None = None,
) -> tuple[dict[str, Any], bool]:
    """Compare pre- and post-check Git trees and return (verify_record, changed)."""
    after_tree = _workspace_tree(workspace)
    changed = initial_tree != after_tree
    verify_record: dict[str, Any] = {"action_number": action_number, "tree": after_tree}
    if action_token is not None:
        verify_record["action_token"] = action_token
    if changed:
        verify_record["changed"] = True
    return verify_record, changed


def format_record_summary(results: list[dict[str, Any]]) -> str:
    """Format the one-line summary printed by `record`."""
    failed = [r["id"] for r in results if not r["passed"]]
    return (
        f"UNTIL-LOOP-RECORDED {len(results)} checks for this action: "
        + (f"{len(failed)} failed ({', '.join(failed)})" if failed else "all passed")
    )
