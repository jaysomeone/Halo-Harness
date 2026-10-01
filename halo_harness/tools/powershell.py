"""halo_harness.tools.powershell -- the PowerShell tool (H2 scope A),
win32 only (halo_harness/tools/registry.py only registers it when
`sys.platform == "win32"` -- Kali is the primary target and has no
PowerShell at all). Shares Bash's streamed-output/timeout/kill plumbing
(tools/_proc.py) but has no `cd`-persistence marker of its own: PowerShell
rules match by Cmdlet name with alias canonicalisation and are
case-insensitive (binary facts sec.4), which is `permissions.py`'s job, not
this tool's -- this tool just runs the command.
"""

from __future__ import annotations

import os

from halo_harness.tools._proc import run_streamed
from halo_harness.tools.base import Tool, ToolContext, ToolResult

DEFAULT_TIMEOUT_MS = 120_000
MAX_TIMEOUT_MS = 600_000

DESCRIPTION = (
    "Executes a PowerShell command (Windows only).\n\n"
    "Usage notes:\n"
    "- Runs via `powershell.exe -NoProfile -NonInteractive -Command`. Default timeout is 120000ms "
    "(2 minutes); pass `timeout` (milliseconds) for a longer wait, up to 600000ms (10 minutes).\n"
    "- Output is stdout and stderr merged together; very long output is truncated with a pointer to "
    "the full text.\n"
    "- Prefer this tool over Bash for anything Windows-specific (services, the registry, WMI/CIM, "
    "native PowerShell cmdlets); use Bash for everything POSIX-shaped.\n"
    "- Each call is a fresh PowerShell process -- working directory and variables do not persist "
    "between calls; `cd`/`Set-Location` inside the SAME command string works as usual."
)


class PowerShellTool(Tool):
    name = "PowerShell"
    description = DESCRIPTION
    is_destructive = True
    result_cap = 30_000
    input_schema = {
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "The PowerShell command to execute"},
            "description": {"type": "string", "description": "Clear, concise description of what this command does, 5-10 words, in active voice"},
            "timeout": {"type": "integer", "description": "Optional timeout in milliseconds (max 600000)"},
        },
        "required": ["command"],
    }

    def summary(self, input: dict) -> str:
        cmd = input.get("command", "") if isinstance(input, dict) else ""
        return f"PowerShell({cmd[:60]}{'...' if len(cmd) > 60 else ''})"

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

        env = dict(ctx.env) if isinstance(ctx.env, dict) else dict(os.environ)
        env["CLAUDECODE"] = "1"

        argv = ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command]
        raw_output, exit_code, timed_out, aborted = run_streamed(
            argv, cwd=ctx.cwd, env=env, timeout_s=timeout_ms / 1000.0,
            abort=getattr(ctx, "abort", None), progress_cb=getattr(ctx, "progress_cb", None),
        )

        if exit_code is None and not timed_out and not aborted:
            return ToolResult(raw_output, is_error=True)
        if aborted:
            return ToolResult((raw_output.strip() + "\n[command aborted]").strip(), is_error=True)
        if timed_out:
            return ToolResult(
                (raw_output.strip() + f"\n[command timed out after {timeout_ms}ms and was killed]").strip(),
                is_error=True,
            )

        body = raw_output if raw_output.strip() else "(no output)"
        if exit_code not in (0, None):
            body += f"\n[exit code {exit_code}]"
        return ToolResult(body)
