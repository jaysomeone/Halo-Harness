"""rolo_claude.agent.cc_process -- H11 Part B: the `claude` subprocess
wrapper (`ClaudeCodeProcess`) and its argv builder (`build_cc_argv`). One
subprocess per rolo-claude `cc:` session, lazily started on the session's
first `cc:` turn (see `agent/cc_runtime.py`), stdin held open across
rolo-claude turns.

Command line (verified LIVE against the installed claude 2.1.281/2.1.284,
this milestone's report has the exact transcripts): `-p --output-format
stream-json --input-format stream-json --verbose --include-partial-
messages --model <id> --session-id <uuid5 of the rolo session id>
--permission-mode bypassPermissions --tools "" --strict-mcp-config
--mcp-config <inline JSON: one stdio server "rolo"> --settings
'{"disableAllHooks": true}' [--append-system-prompt <addendum>]`.
`--tools ""` disables Claude Code's OWN built-ins while an MCP-provided
tool (ours) still works -- live-verified (`system.init.tools` contained
only `mcp__probe__Ping`, and a `--tools ""` + `--strict-mcp-config`
session still completed a real tool_use/tool_result round trip). No
`--disallowedTools` fallback has been needed in practice; `cc_disallowed_
tools_fallback_argv` below exists for `ensure_cc_state` to retry with if a
future claude version ever stops honouring `--tools ""` the same way.
`bypassPermissions` is correct here because rolo-claude's own engine
(agent/cc_runtime.py's `bridge_call_tool`) gates every bridged call BEFORE
it runs; Claude Code's own prompts are unanswerable headlessly anyway.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
import uuid
from collections import deque
from pathlib import Path
from typing import Optional

from rolo_claude.providers.cc_models import resolve_claude_launch_argv

# Fixed, arbitrary namespace UUID for uuid5 -- any constant works; it only
# needs to be STABLE across processes so the same rolo session id always
# maps to the same `--session-id`.
_CC_NAMESPACE = uuid.UUID("6f2f5f2e-8c1a-4e9d-9a2e-1b7a2c9d4e6f")

CC_APPEND_SYSTEM_PROMPT = (
    "You are running inside rolo-claude, a harness that bridges tools through one MCP server "
    "named \"rolo\" (mcp__rolo__<Name>). Your own built-in Read/Write/Edit/Bash/Glob/Grep/WebFetch/"
    "WebSearch/TodoWrite/Agent/AskUserQuestion tools are disabled for this session -- use the "
    "mcp__rolo__ tools instead; they behave the same way and are already listed for you."
)


def cc_session_uuid(rolo_session_id: str) -> str:
    """A canonical UUID for `--session-id`/`--resume` -- rolo-claude's own
    session id (agent/log.py: `uuid.uuid4().hex`, no dashes) is not one.
    Live-verified: `claude -p --session-id not-a-uuid` -> "Error: Invalid
    session ID. Must be a valid UUID." `uuid5` is deterministic (no state
    to persist): the SAME rolo session id always maps to the SAME cc
    session id, in this process and any later one that resumes it."""
    return str(uuid.uuid5(_CC_NAMESPACE, rolo_session_id))


def _repo_root_for_pythonpath() -> str:
    import rolo_claude
    return str(Path(rolo_claude.__file__).resolve().parent.parent)


def build_mcp_config(server_env: dict) -> dict:
    """The inline `--mcp-config` JSON: one stdio server named "rolo" (so
    Claude Code exposes every bridged tool as `mcp__rolo__<Name>` -- bin
    sec.9's `mcp__${wn(server)}__${wn(tool)}` naming) running the child
    side of the bridge, `python -m rolo_claude.ccbridge`. `PYTHONPATH` is
    always set so the child can `import rolo_claude` whether this process
    is an editable/repo checkout or an installed package."""
    env = dict(server_env)
    existing_pp = os.environ.get("PYTHONPATH", "")
    repo_root = _repo_root_for_pythonpath()
    env["PYTHONPATH"] = os.pathsep.join([repo_root, existing_pp]) if existing_pp else repo_root
    return {
        "mcpServers": {
            "rolo": {"type": "stdio", "command": sys.executable,
                      "args": ["-m", "rolo_claude.ccbridge"], "env": env}
        }
    }


def build_cc_argv(*, model: str, session_id: str, resume: bool, mcp_config: dict,
                   append_system_prompt: Optional[str] = CC_APPEND_SYSTEM_PROMPT,
                   permission_mode: str = "bypassPermissions",
                   tools_flag: str = "") -> list:
    argv = resolve_claude_launch_argv() + [
        "-p", "--model", model,
        "--output-format", "stream-json", "--input-format", "stream-json",
        "--verbose", "--include-partial-messages",
        "--tools", tools_flag, "--strict-mcp-config", "--mcp-config", json.dumps(mcp_config),
        "--settings", json.dumps({"disableAllHooks": True}),
        "--permission-mode", permission_mode,
    ]
    argv += (["--resume", session_id] if resume else ["--session-id", session_id])
    if append_system_prompt:
        argv += ["--append-system-prompt", append_system_prompt]
    return argv


def cc_disallowed_tools_fallback_argv(argv: list, disallowed_names: list) -> list:
    """Defensive fallback (brief: "fall back to --disallowedTools <all
    built-ins> if --tools \"\" does not disable them") -- swaps the
    `--tools ""` pair for `--disallowedTools <comma list>` in an already-
    built argv. Never exercised on 2.1.281/2.1.284 (`--tools ""` verified
    live to fully disable Claude Code's built-ins while leaving an
    MCP-provided tool callable), kept for a future claude version that
    might stop honouring it the same way."""
    out = []
    i = 0
    while i < len(argv):
        if argv[i] == "--tools" and i + 1 < len(argv):
            out += ["--disallowedTools", ",".join(disallowed_names)]
            i += 2
            continue
        out.append(argv[i])
        i += 1
    return out


class ClaudeCodeProcess:
    """One `claude -p --input-format stream-json --output-format
    stream-json` subprocess, stdin held open across turns. POSIX: its own
    process GROUP (`start_new_session=True`), so `interrupt`/`kill` reach
    every descendant with no orphans (pgrep-verifiable). Windows: a new
    process group (`CREATE_NEW_PROCESS_GROUP`) so `CTRL_BREAK_EVENT` can
    target it alone."""

    def __init__(self, argv: list, *, cwd: "Path | str", env: Optional[dict] = None):
        self.argv = list(argv)
        popen_kwargs = dict(
            cwd=str(cwd), stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace", bufsize=1,
        )
        if env is not None:
            popen_kwargs["env"] = dict(env)
        if os.name == "nt":
            popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            popen_kwargs["start_new_session"] = True
        self._proc = subprocess.Popen(argv, **popen_kwargs)
        self._stdin_lock = threading.Lock()
        self._stderr_buf: "deque[str]" = deque(maxlen=200)
        self._stderr_thread = threading.Thread(target=self._drain_stderr, daemon=True,
                                                 name=f"cc-stderr-{self._proc.pid}")
        self._stderr_thread.start()

    def _drain_stderr(self) -> None:
        try:
            stream = self._proc.stderr
            if stream is None:
                return
            for line in stream:
                self._stderr_buf.append(line.rstrip("\n"))
        except (OSError, ValueError):
            pass

    @property
    def pid(self) -> int:
        return self._proc.pid

    @property
    def alive(self) -> bool:
        return self._proc.poll() is None

    def send_user_line(self, text: str) -> None:
        line = json.dumps({"type": "user", "message": {"role": "user", "content": [{"type": "text", "text": text}]}},
                           ensure_ascii=False)
        with self._stdin_lock:
            stdin = self._proc.stdin
            if stdin is None or stdin.closed:
                raise BrokenPipeError("claude subprocess stdin is closed")
            stdin.write(line + "\n")
            stdin.flush()

    def read_event(self) -> Optional[dict]:
        """One parsed stream-json line from stdout, or None on EOF. A
        blank or unparseable line is skipped, never raised."""
        stdout = self._proc.stdout
        if stdout is None:
            return None
        while True:
            line = stdout.readline()
            if line == "":
                return None
            line = line.strip()
            if not line:
                continue
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                continue

    def close_stdin(self) -> None:
        with self._stdin_lock:
            try:
                stdin = self._proc.stdin
                if stdin is not None and not stdin.closed:
                    stdin.close()
            except OSError:
                pass

    def interrupt(self) -> None:
        """Esc / quit / SIGHUP -- process-GROUP kill, no orphans."""
        try:
            if os.name == "nt":
                self._proc.send_signal(signal.CTRL_BREAK_EVENT)
            else:
                os.killpg(os.getpgid(self._proc.pid), signal.SIGTERM)
        except (ProcessLookupError, OSError):
            pass

    def kill(self) -> None:
        try:
            if os.name == "nt":
                self._proc.kill()
            else:
                os.killpg(os.getpgid(self._proc.pid), signal.SIGKILL)
        except (ProcessLookupError, OSError):
            pass

    def wait(self, timeout: Optional[float] = None) -> Optional[int]:
        try:
            return self._proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            return None

    def stderr_tail(self, max_chars: int = 2000) -> str:
        text = "\n".join(self._stderr_buf)
        return text[-max_chars:]
