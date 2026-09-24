"""rolo_claude.tools.bash -- the Bash tool (H2 scope A). `/bin/bash -lc` on
POSIX, Git Bash on win32 (config.paths.git_bash()); a session-persistent
`cd` (via a trailing marker line reporting the shell's final $PWD, stripped
back out of the displayed output); a trailing `[exit code N]` note on a
non-zero exit; process-GROUP kill on timeout/abort.
"""

from __future__ import annotations

import os
import sys
import uuid
from pathlib import Path

from rolo_claude.config.paths import from_posix, git_bash
from rolo_claude.tools._proc import run_streamed
from rolo_claude.tools.base import Tool, ToolContext, ToolResult

DEFAULT_TIMEOUT_MS = 120_000
MAX_TIMEOUT_MS = 600_000
_EXIT_MARK = "__ROLO_CLAUDE_EXIT__"
_CWD_MARK = "__ROLO_CLAUDE_CWD__"

DESCRIPTION = (
    "Executes a shell command in a persistent session.\n\n"
    "Usage notes:\n"
    "- Runs via bash -lc (Git Bash on Windows). Default timeout is 120000ms (2 minutes); pass "
    "`timeout` (milliseconds) for a longer wait, up to a maximum of 600000ms (10 minutes).\n"
    "- Output is stdout and stderr merged together, in the order produced; very long output is "
    "truncated with a pointer to the full text.\n"
    "- Avoid using this tool to run find, grep, cat, head, tail, ls, or sed, unless explicitly "
    "instructed or after you have verified that a dedicated tool (Glob, Grep, Read, Edit) cannot "
    "accomplish your task.\n"
    "- The working directory persists across calls within one session: a `cd` in one command is "
    "still in effect for the next one. Try to maintain your current working directory throughout "
    "the session by using absolute paths and avoiding usage of `cd`, unless the user explicitly "
    "requests it.\n"
    "- Always quote file paths that contain spaces (e.g. cd \"path with spaces/file.txt\").\n"
    "- Chain related commands with `&&` or `;` rather than making several separate tool calls."
)


def _strip_markers(output: str, nonce: str) -> "tuple[str, object, object]":
    """Split `output` into (displayed_text, exit_code_or_None,
    reported_cwd_or_None), removing the trailing marker lines a wrapped
    command appends (and the blank separator line printf's own leading \\n
    introduces) so neither ever reaches the model.

    finding 9: `nonce` is a fresh random token generated for THIS call
    only -- the marker prefixes actually written to the shell are
    `{_EXIT_MARK}_{nonce}`/`{_CWD_MARK}_{nonce}`, so a command whose own
    OUTPUT happens to contain a plain `__ROLO_CLAUDE_CWD__:...` line
    (verified exploit: `printf '__ROLO_CLAUDE_CWD__:/etc\\n'; exit 0` used
    to move the whole session to /etc) can never forge one without first
    guessing this call's nonce. Only the LAST occurrence of each
    nonce'd marker is honoured (the wrapper always appends them once, at
    the very end -- taking the last one is a second, cheap guard against
    an earlier line that coincidentally collides)."""
    exit_prefix = f"{_EXIT_MARK}_{nonce}:"
    cwd_prefix = f"{_CWD_MARK}_{nonce}:"
    exit_code = None
    cwd = None
    kept = []
    for line in output.splitlines(keepends=True):
        stripped = line.rstrip("\r\n")
        if stripped.startswith(exit_prefix):
            try:
                exit_code = int(stripped[len(exit_prefix):].strip())
            except ValueError:
                pass
            continue
        if stripped.startswith(cwd_prefix):
            cwd = stripped[len(cwd_prefix):].strip()
            continue
        kept.append(line)
    return "".join(kept).rstrip("\n"), exit_code, cwd


class BashTool(Tool):
    name = "Bash"
    description = DESCRIPTION
    is_destructive = True
    result_cap = 30_000
    input_schema = {
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "The command to execute"},
            "description": {"type": "string", "description": "Clear, concise description of what this command does, 5-10 words, in active voice"},
            "timeout": {"type": "integer", "description": "Optional timeout in milliseconds (max 600000)"},
            "run_in_background": {"type": "boolean", "description": "Run this command in the background (not yet implemented in this build)"},
        },
        "required": ["command"],
    }

    def summary(self, input: dict) -> str:
        cmd = input.get("command", "") if isinstance(input, dict) else ""
        return f"Bash({cmd[:60]}{'...' if len(cmd) > 60 else ''})"

    def permission_content(self, input: dict) -> str:
        return input.get("command", "") if isinstance(input, dict) else ""

    def run(self, input: dict, ctx: ToolContext) -> ToolResult:
        command = input.get("command") if isinstance(input, dict) else None
        if not command or not isinstance(command, str):
            return ToolResult("The command parameter is required", is_error=True)

        timeout_ms = input.get("timeout")
        if not isinstance(timeout_ms, int) or isinstance(timeout_ms, bool) or timeout_ms <= 0:
            timeout_ms = DEFAULT_TIMEOUT_MS
        timeout_ms = min(timeout_ms, MAX_TIMEOUT_MS)

        bash_state = ctx.bash_state if isinstance(getattr(ctx, "bash_state", None), dict) else {}
        session_cwd = bash_state.get("cwd") or ctx.cwd

        # finding 9: the persisted cwd is reused with NO existence check --
        # a `cd build` followed by `rm -rf build` used to fail EVERY later
        # call for the rest of the session ("Error launching process:
        # [Errno 2]"), `cd /` included, since subprocess.Popen(cwd=...)
        # can't launch into a directory that no longer exists. Fall back to
        # the session's own starting cwd, with a one-line notice, instead.
        cwd_notice = ""
        try:
            cwd_ok = Path(session_cwd).is_dir()
        except OSError:
            cwd_ok = False
        if cwd_ok:
            cwd = session_cwd
        else:
            cwd = ctx.cwd
            bash_state["cwd"] = cwd
            cwd_notice = f"[note: the working directory {session_cwd} no longer exists -- reset to {cwd}]\n"

        shell_path = git_bash()
        if shell_path is None:
            return ToolResult("No POSIX shell (bash) is available on this system to run Bash commands.", is_error=True)

        # finding 9: a per-call random nonce, never reused across calls or
        # guessable from the command text -- the marker lines this wrapper
        # appends can only be recognized under THIS nonce (see
        # _strip_markers), so a command's own output can never forge one
        # (verified exploit: `printf '__ROLO_CLAUDE_CWD__:/etc\n'` used to
        # hijack the session's cwd).
        nonce = uuid.uuid4().hex
        wrapped = (
            f"{command}\n"
            f"__rc=$?\n"
            # `pwd -W` (Git Bash/MSYS only -- a real POSIX bash rejects -W
            # and the `|| pwd` fallback fires) prints the NATIVE Windows
            # form (C:/Users/...); plain $PWD would print MSYS's internal
            # view instead, which for a mount like the Windows temp dir
            # (MSYS conventionally maps it to /tmp) is NOT a form
            # `subprocess.Popen(cwd=...)` can parse on the next call.
            f'__cwd=$(pwd -W 2>/dev/null || pwd)\n'
            f'printf "\\n{_EXIT_MARK}_{nonce}:%s\\n{_CWD_MARK}_{nonce}:%s\\n" "$__rc" "$__cwd"\n'
        )
        env = dict(ctx.env) if isinstance(ctx.env, dict) else dict(os.environ)
        env["CLAUDECODE"] = "1"

        raw_output, exit_code, timed_out, aborted = run_streamed(
            [str(shell_path), "-lc", wrapped], cwd=cwd, env=env, timeout_s=timeout_ms / 1000.0,
            abort=getattr(ctx, "abort", None), progress_cb=getattr(ctx, "progress_cb", None),
        )

        if exit_code is None and not timed_out and not aborted:
            return ToolResult(cwd_notice + raw_output if cwd_notice else raw_output, is_error=True)  # the process never launched at all

        text, reported_exit, reported_cwd = _strip_markers(raw_output, nonce)
        if cwd_notice:
            text = cwd_notice + text
        if reported_cwd:
            # Git Bash reports $PWD in MSYS/POSIX form (/c/Users/...);
            # subprocess.Popen(cwd=...) on Windows needs a native path
            # (C:\Users\...) or it fails to launch the NEXT call outright.
            # finding 9: from_posix is only meaningful for THAT Windows/
            # Git-Bash translation -- applied unconditionally it also
            # rewrites a genuine POSIX path on real Linux/WSL bash whose
            # single-letter top-level dir happens to look like a drive
            # form (e.g. `/a/bcd`), corrupting it into `A:/bcd`.
            bash_state["cwd"] = from_posix(reported_cwd) if sys.platform == "win32" else reported_cwd
        final_exit = reported_exit if reported_exit is not None else exit_code

        if aborted:
            body = text.strip() + "\n[command aborted]" if text.strip() else "[command aborted]"
            return ToolResult(body, is_error=True)
        if timed_out:
            body = (text.strip() + f"\n[command timed out after {timeout_ms}ms and was killed]"
                     if text.strip() else f"[command timed out after {timeout_ms}ms and was killed]")
            return ToolResult(body, is_error=True)

        body = text if text.strip() else "(no output)"
        if final_exit not in (0, None):
            body += f"\n[exit code {final_exit}]"
        if isinstance(input, dict) and input.get("run_in_background"):
            body += "\n[note: run_in_background isn't implemented yet in this build -- ran in the foreground instead]"
        return ToolResult(body)
