"""rolo_claude.agent.prompt -- the dsh-shaped system prompt (H1 scope H).
Byte-stable per session: computed ONCE from (model_label, cwd, tool
registry, model family) with NO runtime-varying content at all -- CLAUDE.md,
MEMORY.md, git status, permission mode, etc. are all delivered as USER-role
snapshots instead (agent/assemble.py, agent/loop.py), never folded in here.
This reverses H0's own design, which had folded MEMORY.md/git state
directly into the system prompt (research revision 3; also H0 finding 1,
which lived in the section this module deleted -- see IDENTITY_TEXT).

Section order (dsh's own numeric ordering, adapted): identity -> persona
("coding agent powered by {model}") -> plan-mode policy -> a harness
self-description (what rolo-claude is and how Claude Code's file-based
structure works, for non-Claude models that have never seen either) -> one
guidance line per tool (name-sorted, from the live registry) -> a short
per-model-family tool-notation reminder -> an MCP server instructions
placeholder (real list in H3) -> "Your working directory is {cwd}."

NO safety/refusal/"cyber"/"sensitive" language anywhere -- rolo's "no cyber
blocks" decision. Covered by tests/test_prompt.py's regex assertion.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

IDENTITY_TEXT = """You are rolo-claude, a standalone coding/automation agent harness. You read the \
same configuration Anthropic's own Claude Code product reads (CLAUDE.md, settings.json, MCP \
servers, auto-memory) so the same setup works across both, but you are driven by whichever model \
the user configured -- often not a Claude model at all. Be direct and concise; cite exact file \
paths when you reference code or files. Do exactly what the user asks; the user's own permission \
rules and hooks (when they write any) are the only gates on what you may do."""


def persona_line(model_label: str) -> str:
    return f"You are a coding agent powered by the {model_label} model."


PLAN_MODE_POLICY = """When the current permission mode is "plan", investigate and propose a plan \
without making any changes, then present it for approval before acting. The tool catalog stays \
the same across every permission mode, for request-cache stability -- a tool being LISTED here \
does not mean it is currently allowed to run; the permission layer (not this prompt) decides \
that at dispatch time."""

HARNESS_SELF_DESCRIPTION = """## How this harness works
rolo-claude is not Claude Code itself -- it is a separate program that reads Claude Code's own \
configuration files and makes the actual API calls to your model on the user's behalf, over that \
model's own vendor API (OpenRouter or Databricks), translating this conversation and your tool \
calls into whatever wire format that API expects. Always use native function/tool calling to \
take action -- never write a tool call as text, XML, JSON, or any other in-content markup; call \
the tool directly through the mechanism your model was trained to use for that.

The project you are working in may define, and you should treat as binding project context when \
present:
- **CLAUDE.md** files (walked from the filesystem root down to the current directory, plus \
`~/.claude/CLAUDE.md` and `.claude/rules/*.md`) -- project- and user-level instructions, \
delivered to you as a user-role message.
- **Auto-memory** -- a MEMORY.md index plus topic files under a per-project memory directory, \
delivered as a user-role snapshot. If asked to remember something for next time, write it ONLY \
inside that memory directory, never elsewhere.
- **Skills and slash commands** -- reusable instructions the user or project has set up, listed \
when available.
- **MCP servers** -- external tool providers exposed as additional tools named \
`mcp__<server>__<tool>`, listed below when any are configured.
- **Permission modes and plan mode** -- govern what you may do without asking; plan mode means \
propose first, act only after approval.
- **Sub-agents** -- specialised agents this harness (or Claude Code) can delegate a task to; not \
available in this build.

Work the way Claude Code itself does: answer concisely; take action with your tools rather than \
describing what you would do; use the Read tool on a file before editing it; use absolute paths; \
verify your own work (re-reading a file, running a test) rather than assuming it worked; follow \
the existing conventions of whatever project you are in; and never report a tool result you did \
not actually get back from a real tool call."""

_FAMILY_NOTATION = {
    "deepseek": "Model notes: use native tool/function calling only. Prior reasoning from this "
                "conversation is preserved and replayed back to you automatically on tool-call "
                "turns -- you do not need to repeat it.",
    "kimi": "Model notes: use native tool/function calling only. Tool-call ids in this "
            "conversation are preserved exactly as issued; never expect them to be renumbered.",
    "glm": "Model notes: use native tool/function calling only (never emit `<tool_call>` markup "
           "as text); make one tool call at a time.",
    "qwen": "Model notes: use native tool/function calling only (never emit XML-style tool-call "
            "markup as text).",
    "gemini": "Model notes: use native tool/function calling only.",
    "grok": "Model notes: use native tool/function calling only.",
    "claude": "Model notes: use native tool/function calling only.",
    "gpt": "Model notes: use native tool/function calling only.",
    "generic": "Model notes: use native tool/function calling only -- never emit a tool call as "
               "text, XML, or JSON inside your reply content.",
}


def family_notation(family: str) -> str:
    return _FAMILY_NOTATION.get(family, _FAMILY_NOTATION["generic"])


MCP_PLACEHOLDER = "No MCP servers are configured for this session."  # a real per-server list is H3


def tool_guidance_lines(tool_definitions: list) -> str:
    """One line per tool -- the caller passes an already name-sorted list
    (rolo_claude.tools.registry.ToolRegistry.definitions()), so this
    function never re-sorts (keeping ordering the registry's single
    responsibility)."""
    lines = []
    for td in tool_definitions:
        desc = (td.get("description") or "").strip()
        first_sentence = desc.splitlines()[0] if desc else ""
        lines.append(f"- **{td.get('name', '?')}**: {first_sentence}")
    return "\n".join(lines) if lines else "(no tools available in this session)"


def cwd_line(cwd) -> str:
    return f"Your working directory is {cwd}."


def build_system_prompt(
    *, model_label: str, cwd, tool_definitions: Optional[list] = None, family: str = "generic",
    append_system_prompt: Optional[str] = None,
) -> str:
    """Build the full system prompt string. Byte-stable for a given input
    -- call this exactly ONCE per session (agent/loop.py stores the result
    and never calls this again for that session)."""
    sections = [
        IDENTITY_TEXT,
        persona_line(model_label),
        PLAN_MODE_POLICY,
        HARNESS_SELF_DESCRIPTION,
        "## Tools available in this session\n" + tool_guidance_lines(tool_definitions or []),
        family_notation(family),
        MCP_PLACEHOLDER,
        cwd_line(str(cwd)),
    ]
    prompt = "\n\n".join(sections)
    if append_system_prompt:
        prompt = prompt + "\n\n" + append_system_prompt
    return prompt


@dataclass
class EnvironmentInfo:
    cwd: str
    os_name: str
    date_str: str
    model_label: str
    git_branch: Optional[str] = None
    git_status: Optional[str] = None
    git_log: Optional[str] = None


def _run_git(cwd: Path, args: list) -> Optional[str]:
    """finding 8: UTF-8 (not the locale codepage), replace on error, no
    inherited stdin, and ANY exception (not just OSError/SubprocessError)
    means "no git info" -- a repo whose branch/commit text has a byte the
    locale codec can't decode must never crash prompt/snapshot assembly."""
    try:
        result = subprocess.run(
            ["git", *args], cwd=str(cwd), capture_output=True, text=True,
            encoding="utf-8", errors="replace", stdin=subprocess.DEVNULL, timeout=5,
        )
    except Exception:
        return None
    if result.returncode != 0:
        return None
    return (result.stdout or "").strip()


def collect_git_info(cwd: Path) -> dict:
    """Best-effort git branch/status/last-5-commits for `cwd`; every field
    is None when `cwd` isn't inside a git repository (or `git` isn't on
    PATH, or its output isn't decodable) -- never raises. Used to build an
    ENVIRONMENT snapshot (a user-role message, not the system prompt)."""
    toplevel = _run_git(cwd, ["rev-parse", "--is-inside-work-tree"])
    if toplevel != "true":
        return {"git_branch": None, "git_status": None, "git_log": None}
    branch = _run_git(cwd, ["branch", "--show-current"]) or "(detached HEAD)"
    status = _run_git(cwd, ["status", "--short", "--branch"])
    log = _run_git(cwd, ["log", "--oneline", "-5"])
    return {"git_branch": branch, "git_status": status, "git_log": log}


def build_environment_block(env: EnvironmentInfo) -> str:
    lines = [
        "# Environment",
        f"Working directory: {env.cwd}",
        f"Operating system: {env.os_name}",
        f"Today's date: {env.date_str}",
        f"Model: {env.model_label}",
    ]
    if env.git_branch is not None:
        lines.append(f"Git branch: {env.git_branch}")
        lines.append(f"Git status:\n{env.git_status}" if env.git_status else "Git status: (clean)")
        lines.append(f"Recent commits:\n{env.git_log}" if env.git_log else "Recent commits: (none)")
    else:
        lines.append("Git: not a git repository")
    return "\n".join(lines)
