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

from rolo_claude.config.paths import from_posix, git_bash, to_posix
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
    "- Chain related commands with `&&` or `;` rather than making several separate tool calls.\n"
    "- Set `run_in_background=true` for a command expected to run for a while (a server, a watch/build "
    "loop, a long download); you get back a shell_id immediately and the command keeps running. Use "
    "the BashOutput tool to check on it (returns only the output produced since the last check) and "
    "TaskStop to stop it early. A command that runs in the FOREGROUND and hits its own timeout is "
    "moved to the background automatically instead of being killed -- check it the same way."
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
            "run_in_background": {"type": "boolean", "description": "Set to true to run this command in the background. Use BashOutput to read the output later."},
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

        env_file = getattr(ctx, "env_file", None)
        source_prefix = ""
        if env_file is not None:
            source_prefix = f'[ -f "{to_posix(str(env_file))}" ] && . "{to_posix(str(env_file))}"\n'
        env = dict(ctx.env) if isinstance(ctx.env, dict) else dict(os.environ)
        env["CLAUDECODE"] = "1"
        if env_file is not None:
            env["CLAUDE_ENV_FILE"] = str(env_file)
        # H9 whole-tree review finding 12: `bash -lc` is a LOGIN shell -- it
        # sources /etc/profile (and ~/.bash_profile/~/.profile) BEFORE ever
        # running our own `-c` script body. Debian/Kali's stock
        # /etc/profile unconditionally REASSIGNS PATH (`PATH="/usr/local/
        # sbin:...:/bin"; export PATH`), discarding whatever PATH this
        # harness computed -- settings.json's own `env.PATH`, a
        # SessionStart env-file `export PATH="$PATH:/x"`, or simply the
        # PATH rolo-claude itself was started with (often customized in
        # ~/.zshrc, which Kali uses by default and bash's own login files
        # never read). Verified on WSL: a PATH entry's own tool ran fine
        # under Ubuntu's PATH-preserving profile, then "command not found"
        # for that exact same env once the SAME session ran under a bind-
        # mounted Debian profile instead (PATH reset to the stock list).
        # Stash the INTENDED PATH under a harness-private var name the
        # login profile has no reason to touch, then restore it as the
        # wrapped script's own first line -- necessarily AFTER the profile
        # (an inherent property of how `bash -lc` starts up: the login
        # files always run before the `-c` command string does) but BEFORE
        # `source_prefix` (a CLAUDE_ENV_FILE's own `export PATH="$PATH:/x"`
        # must append onto the RESTORED PATH, not onto whatever the profile
        # just clobbered it to).
        #
        # POSIX only (`os.name != "nt"`): on win32 this shell is Git Bash,
        # whose OWN startup files add ITS bin dirs (where `sleep`/`ls`/...
        # actually live) to PATH -- an ADDITIVE step this harness's own
        # captured `ctx.env["PATH"]` (built before Git Bash ever ran, and
        # never containing those MSYS paths in the first place) has no
        # reason to already include. Forcibly restoring that captured value
        # would UNDO Git Bash's own necessary setup instead of fixing
        # anything (verified: broke `sleep`/every other MSYS coreutil on
        # this Windows build host once tried unconditionally). The
        # documented failure (a Debian/Kali /etc/profile that unconditionally
        # OVERWRITES PATH, discarding everything) is a POSIX-login-shell
        # behaviour with no Windows/Git-Bash equivalent to guard against.
        restore_path_prefix = ""
        if os.name != "nt":
            env["__ROLO_CLAUDE_SESSION_PATH"] = env.get("PATH", os.environ.get("PATH", ""))
            # `unset` right after restoring PATH from it -- a model running
            # `env`/`printenv` shouldn't see this harness-private plumbing var.
            restore_path_prefix = 'export PATH="$__ROLO_CLAUDE_SESSION_PATH"; unset __ROLO_CLAUDE_SESSION_PATH\n'

        job_registry = getattr(ctx, "job_registry", None)
        if isinstance(input, dict) and input.get("run_in_background") and job_registry is not None:
            # H8 scope A: spawn immediately, no foreground wait at all. No
            # exit/cwd marker wrapping here (unlike the foreground path
            # below) -- the user's own command stays the shell's LAST
            # command, so `proc.returncode` is already its real exit
            # status; a backgrounded job's own `cd` also never needs to
            # feed back into this session's persistent bash_state.
            plain_command = f"{restore_path_prefix}{source_prefix}{command}"
            record, err = job_registry.start_background(
                plain_command, description=input.get("description") or command,
                cwd=cwd, env=env, shell_path=str(shell_path),
            )
            if err is not None:
                return ToolResult(err, is_error=True)
            body = (cwd_notice if cwd_notice else "") + (
                f"Command running in the background (shell_id: {record.job_id}).\n"
                f"Use BashOutput with shell_id={record.job_id!r} to check its progress, or TaskStop to stop it."
            )
            return ToolResult(body)

        # finding 9: a per-call random nonce, never reused across calls or
        # guessable from the command text -- the marker lines this wrapper
        # appends can only be recognized under THIS nonce (see
        # _strip_markers), so a command's own output can never forge one
        # (verified exploit: `printf '__ROLO_CLAUDE_CWD__:/etc\n'` used to
        # hijack the session's cwd). finding 6: source_prefix/env were
        # already computed above (needed by the run_in_background branch
        # too) -- 2.1.281 sources CLAUDE_ENV_FILE as a real shell script
        # ("Session environment script ready"), not a literal NAME=value
        # parse, so `export PATH="$PATH:/x"` genuinely appends to the
        # shell's OWN inherited PATH via `$VAR` expansion.
        nonce = uuid.uuid4().hex
        wrapped = (
            f"{restore_path_prefix}"
            f"{source_prefix}"
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

        # H8 scope A (dsh): "a foreground command that hits its timeout is
        # moved to the background instead of killed" -- only wired when
        # this session actually has a job registry; `_on_timeout` hands the
        # still-live proc/queue/collector off to it (see agent/jobs.py's
        # `adopt_from_timeout`) instead of `run_streamed` killing them, and
        # records the resulting job here so the branch below can report
        # "moved to the background" instead of "killed".
        adopted = []

        def _on_timeout(proc, q, collector) -> None:
            record = job_registry.adopt_from_timeout(
                proc=proc, q=q, collector=collector, command=command,
                description=(input.get("description") if isinstance(input, dict) else None) or command,
                cwd=cwd, nonce=nonce,
            )
            adopted.append(record)

        raw_output, exit_code, timed_out, aborted = run_streamed(
            [str(shell_path), "-lc", wrapped], cwd=cwd, env=env, timeout_s=timeout_ms / 1000.0,
            abort=getattr(ctx, "abort", None), progress_cb=getattr(ctx, "progress_cb", None),
            on_timeout_handoff=(_on_timeout if job_registry is not None else None),
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
            if adopted:
                # H8 scope A: handed off, not killed -- still running, so
                # this is NOT an error result (the command didn't fail; it's
                # just not done yet).
                job_id = adopted[0].job_id
                note = (f"[command timed out after {timeout_ms}ms and was moved to the background as "
                        f"shell_id {job_id} -- use BashOutput with shell_id={job_id!r} to check on it, or "
                        f"TaskStop to stop it]")
                body = text.strip() + "\n" + note if text.strip() else note
                return ToolResult(body)
            body = (text.strip() + f"\n[command timed out after {timeout_ms}ms and was killed]"
                     if text.strip() else f"[command timed out after {timeout_ms}ms and was killed]")
            return ToolResult(body, is_error=True)

        body = text if text.strip() else "(no output)"
        if final_exit not in (0, None):
            body += f"\n[exit code {final_exit}]"
        if isinstance(input, dict) and input.get("run_in_background") and job_registry is None:
            body += "\n[note: background jobs aren't available in this session -- ran in the foreground instead]"
        return ToolResult(body)
