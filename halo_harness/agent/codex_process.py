"""halo_harness.agent.codex_process -- Halo 2.0.3 round 5i part 2: the
`codex exec` subprocess wrapper + argv builder for the `cx:` route.

Architecturally simpler than `agent.cc_process`/`ClaudeCodeProcess`: Codex
has no stdin-streaming protocol (CODEX-RESEARCH.md section 7) -- each Halo
turn is ONE bounded `codex exec [resume <id>] --json <prompt>` invocation
that runs to completion and exits, never a long-held process fed one stdin
line per turn. `CodexExecProcess` therefore has no `send_user_line`; the
prompt rides on argv (or, for an oversized prompt, stdin -- `codex exec -`
reads it), and the whole lifecycle is start/read-stdout-lines/wait.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
from collections import deque
from pathlib import Path
from typing import Optional

from halo_harness.providers.codex_models import resolve_codex_launch_argv

# CODEX-RESEARCH.md section 3: Halo's permission-mode -> (approval_policy,
# sandbox_mode) mapping. bypass/auto share one row (never asks, full
# access); default/manual sandbox values are Halo's own documented choice
# where the brief only specified the approval value.
_PERMISSION_MODE_TO_CODEX = {
    "bypass": ("never", "danger-full-access"),
    "auto": ("never", "danger-full-access"),
    "default": ("on-request", "workspace-write"),
    "manual": ("untrusted", "read-only"),
}


def codex_approval_and_sandbox(permission_mode: str) -> "tuple[str, str]":
    return _PERMISSION_MODE_TO_CODEX.get(permission_mode, _PERMISSION_MODE_TO_CODEX["default"])


def _repo_root_for_pythonpath() -> str:
    import halo_harness
    return str(Path(halo_harness.__file__).resolve().parent.parent)


def _toml_array_literal(values: "list[str]") -> str:
    return "[" + ", ".join(json.dumps(v) for v in values) + "]"


def build_mcp_override_args(bridge_env_keys: "list[str]") -> "list[str]":
    """One `-c mcp_servers.halo.<field>=<value>` flag per field (CODEX-
    RESEARCH.md section 4) -- the Codex counterpart of `cc_process.
    build_mcp_config`'s inline `--mcp-config` JSON, pointing at the SAME
    `python -m halo_harness.ccbridge` child + `ToolBridgeServer` `cc:`
    already uses.

    Privacy (matches `cc_process.build_mcp_config`'s own H11b finding 16):
    `bridge_env_keys` is a list of VARIABLE NAMES ONLY (the socket path var
    on posix, host/port/TOKEN vars on Windows -- `ToolBridgeServer.
    child_env()`'s own keys), never their VALUES -- those go straight into
    the `codex exec` SUBPROCESS's own environment instead (`turn_body_cx`'s
    `env=` on `CodexExecProcess`, which the MCP child then simply inherits),
    exactly like `cc:` folds `bridge.child_env()` into the claude
    subprocess's env rather than spelling it out in `--mcp-config`'s own
    JSON. `mcp_servers.<id>.env_vars` ("Variables to forward", confirmed
    shape in CODEX-RESEARCH.md section 4) is Codex's own documented
    mechanism for "forward these NAMES from my own env down to the MCP
    child" -- using it means the bridge's per-session token never appears
    in `-c ...=<value>` text at all, so it never shows up in a process
    listing (`Win32_Process.CommandLine`/`ps`) the way a baked-in value
    would."""
    args = [
        "-c", f"mcp_servers.halo.command={json.dumps(sys.executable)}",
        "-c", f"mcp_servers.halo.args={_toml_array_literal(['-m', 'halo_harness.ccbridge'])}",
        "-c", f"mcp_servers.halo.env_vars={_toml_array_literal(['PYTHONPATH'] + list(bridge_env_keys))}",
    ]
    return args


def cx_subprocess_env(base_env: dict, bridge_env: dict) -> dict:
    """The actual env dict for the `codex exec` subprocess itself (never
    for a `-c` override -- see `build_mcp_override_args`'s own docstring):
    `base_env` (the session's stripped child env, `providers.config.
    cc_child_env`) plus `PYTHONPATH` (repo root, so the MCP child can
    `import halo_harness`) plus every `bridge_env` key/value
    (`ToolBridgeServer.child_env()`), which `mcp_servers.halo.env_vars`
    then forwards by name down to that child."""
    env = dict(base_env)
    existing_pp = env.get("PYTHONPATH", "")
    repo_root = _repo_root_for_pythonpath()
    env["PYTHONPATH"] = os.pathsep.join([repo_root, existing_pp]) if existing_pp else repo_root
    env.update({str(k): str(v) for k, v in bridge_env.items()})
    return env


def build_cx_argv(*, model: str, prompt: str, resume_id: Optional[str], permission_mode: str,
                   mcp_override_args: "list[str]", ephemeral: bool = False,
                   image_paths: Optional[list] = None) -> list:
    """`resume_id=None` -> a fresh `codex exec ...`; otherwise `codex exec
    resume <id> ...`. `--skip-git-repo-check` always (a Halo session cwd
    need not be a git repo). `--ephemeral` only for the stateless one-shot
    small-model call (mirrors `--no-session-persistence` on `cc:`) --
    `turn_body_cx`'s own multi-turn flow never sets it, so Codex keeps a
    resumable session on disk."""
    approval_policy, sandbox_mode = codex_approval_and_sandbox(permission_mode)
    argv = resolve_codex_launch_argv() + ["exec"]
    if resume_id:
        argv += ["resume", resume_id]
    argv += [
        "--json", "--skip-git-repo-check", "-m", model,
        "-c", f"approval_policy={approval_policy}", "-s", sandbox_mode,
    ]
    argv += mcp_override_args
    if ephemeral:
        argv.append("--ephemeral")
    for path in (image_paths or []):
        argv += ["-i", str(path)]
    argv.append(prompt)
    return argv


class CodexExecProcess:
    """One `codex exec` invocation -- started, read to completion (or
    interrupted), then discarded; a fresh instance is created per Halo turn
    (see `agent/codex_turn.py::turn_body_cx`). POSIX: its own process group
    (`start_new_session=True`) so `interrupt`/`kill` reach every descendant
    (the MCP child it spawns for `mcp_servers.halo` included) with no
    orphans, mirroring `agent.cc_process.ClaudeCodeProcess` exactly."""

    def __init__(self, argv: list, *, cwd: "Path | str", env: Optional[dict] = None):
        self.argv = list(argv)
        popen_kwargs = dict(
            cwd=str(cwd), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace", bufsize=1,
        )
        if env is not None:
            popen_kwargs["env"] = dict(env)
        if os.name == "nt":
            popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            popen_kwargs["start_new_session"] = True
        self._proc = subprocess.Popen(argv, **popen_kwargs)
        self._stderr_buf: "deque[str]" = deque(maxlen=200)
        import threading
        self._stderr_thread = threading.Thread(target=self._drain_stderr, daemon=True,
                                                 name=f"cx-stderr-{self._proc.pid}")
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

    def read_event(self) -> Optional[dict]:
        """One parsed JSONL line from stdout, or None on EOF. A blank or
        unparseable line is skipped, never raised -- same contract as
        `ClaudeCodeProcess.read_event`."""
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

    def _posix_signal_group(self, sig) -> None:
        pid = self._proc.pid
        try:
            pgid = os.getpgid(pid)
        except (ProcessLookupError, OSError):
            return
        if pgid != os.getpgid(0):
            os.killpg(pgid, sig)
        else:
            os.kill(pid, sig)

    def interrupt(self) -> None:
        try:
            if os.name == "nt":
                self._proc.send_signal(signal.CTRL_BREAK_EVENT)
            else:
                self._posix_signal_group(signal.SIGTERM)
        except (ProcessLookupError, OSError):
            pass

    def kill(self) -> None:
        try:
            if os.name == "nt":
                self._proc.kill()
            else:
                self._posix_signal_group(signal.SIGKILL)
        except (ProcessLookupError, OSError):
            pass

    def wait(self, timeout: Optional[float] = None) -> Optional[int]:
        try:
            rc = self._proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            return None
        from halo_harness.termtitle import reassert_after_claude_child
        reassert_after_claude_child()
        return rc

    def stderr_tail(self, max_chars: int = 2000) -> str:
        text = "\n".join(self._stderr_buf)
        return text[-max_chars:]
