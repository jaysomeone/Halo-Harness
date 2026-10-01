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

_HARNESS_SELF_DESCRIPTION_HEAD = """## How this harness works
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
delivered to you as a user-role message."""

_HARNESS_SELF_DESCRIPTION_TAIL = """- **Skills** -- reusable, packaged instructions for a particular kind of task (the user's own, a \
project's, or ones synced from Anthropic), reachable through the Skill tool and, when not marked \
disable-model-invocation, as a `/name` slash command too; listed when any are configured.
- **Custom slash commands** -- project- or user-defined `/name` shortcuts that expand into a prompt \
(optionally after running one pre-approved shell command first); available in this session's prompt \
box, not directly callable by you.
- **Hooks** -- shell commands, HTTP calls, or small-model checks the user or project configured to \
run automatically around specific events (before/after a tool call, before a prompt is sent, when a \
turn is about to end, and more); they can add context, rewrite a tool call's input, or block an \
action outright. You cannot see or bypass a hook -- its effect (a rewritten input, added context, a \
block with a reason) is simply what actually happened, not a suggestion.
- **Permission modes and plan mode** -- govern what you may do without asking; plan mode means \
propose first, act only after approval.
- **Sub-agents** -- specialised agents this harness (or Claude Code) can delegate a task to; not \
available in this build."""


def _mcp_servers_sentence(mcp_servers: Optional[list]) -> str:
    """H3 scope C: one line per configured/connected MCP server (name +
    its own `instructions`, when the server provided one) plus the "use
    ToolSearch" pointer -- the system prompt NEVER lists individual MCP
    TOOLS (finding 4/D-CFG: "the system prompt lists MCP servers with
    their instructions one-liners + 'use ToolSearch', never the tool
    list"), only server identity, so a session with many/large servers
    stays cache-stable and small regardless of how many tools any one of
    them exposes. `mcp_servers` is `[{"name", "instructions"}, ...]`
    (agent/assemble.py builds this from the McpManager, or None/empty for
    a build with no MCP client -- see also H2's own MCP-callout in
    _HARNESS_SELF_DESCRIPTION_TAIL, now replaced by this dynamic section)."""
    if not mcp_servers:
        return ("- **MCP servers** -- external tool providers exposed as additional tools named "
                "`mcp__<server>__<tool>`; none are configured/connected in this session (this is a "
                "statement about what's available RIGHT NOW, not a permanent limitation -- do not "
                "tell the user MCP support doesn't exist).")
    lines = ["- **MCP servers** -- external tool providers exposed as additional tools named "
             "`mcp__<server>__<tool>`. Most of their tools are NOT in your tool list yet (frozen-"
             "catalog + lazy load, for cost/cache stability): call `ToolSearch` with `\"select:"
             "mcp__<server>__<tool>\"` or a keyword query to load one before calling it. Configured "
             "servers this session:"]
    for srv in mcp_servers:
        name = srv.get("name", "?") if isinstance(srv, dict) else str(srv)
        instructions = (srv.get("instructions") or "").strip() if isinstance(srv, dict) else ""
        one_liner = instructions.splitlines()[0] if instructions else "(no server-provided description)"
        lines.append(f"  - `{name}`: {one_liner}")
    return "\n".join(lines)


def _memory_capability_sentence(has_write: bool) -> str:
    """finding 14: H2 has no Write tool, so the pre-H2 "write it ONLY
    inside that memory directory" promise was false -- a model that
    believed it could write memory, and told the user so, was lying on
    this harness's behalf. Registry-driven so this becomes true again
    automatically once a Write tool is registered, with no prose to
    remember to update by hand."""
    if has_write:
        return ("- **Auto-memory** -- a MEMORY.md index plus topic files under a per-project memory "
                "directory (its path is included in the memory snapshot below), delivered as a "
                "user-role snapshot. If asked to remember something for next time, write it ONLY "
                "inside that memory directory, never elsewhere.")
    return ("- **Auto-memory** -- a MEMORY.md index plus topic files under a per-project memory "
            "directory (its path is included in the memory snapshot below), delivered as a "
            "user-role snapshot. This build has no file-writing tool, so you cannot save new memory "
            "yourself -- never tell the user you saved or updated it.")


def _closing_guidance_sentence(has_write: bool, has_bash: bool) -> str:
    """finding 14: "running a test" implied a Bash tool that H2 doesn't
    have; a model that claimed to have run one was, again, lying on this
    harness's behalf. Built from the registry so the verification clause
    only ever names something this build can actually do."""
    verify_options = ["re-reading a file"]
    if has_bash:
        verify_options.append("running a test")
    verify = " or ".join(verify_options)
    edit_clause = "use the Read tool on a file before editing it; " if has_write else ""
    return (f"Work the way Claude Code itself does: answer concisely; take action with your tools "
            f"rather than describing what you would do; {edit_clause}use absolute paths; verify your "
            f"own work ({verify}) rather than assuming it worked; follow the existing conventions of "
            f"whatever project you are in; and never report a tool result you did not actually get "
            f"back from a real tool call.")


def _websearch_capability_sentence(has_websearch: bool) -> Optional[str]:
    """H4 scope F: registry-driven (finding 14's own rule) -- WebSearch is
    ONLY ever registered on an OpenRouter-backed session (tools/websearch.
    py's own `build_websearch_tool`), so this line is simply absent
    otherwise rather than claiming a capability this build/session
    doesn't have right now."""
    if not has_websearch:
        return None
    return ("- **WebSearch** -- search the web for current information and return an answer grounded "
            "in real results, with source URLs; available in this session (only ever offered when the "
            "configured model provider is OpenRouter, which runs it as a side call to its own search "
            "plugin).")


def build_harness_self_description(tool_definitions: list, mcp_servers: Optional[list] = None) -> str:
    """The whole '## How this harness works' section, built from the
    REGISTRY (`tool_definitions`, name-sorted per rolo_claude.tools.
    registry.ToolRegistry.definitions()) instead of hardcoded prose that
    assumed tools (Write, Bash) this build may not actually have --
    finding 14. `mcp_servers` (H3 scope C) is threaded through to
    `_mcp_servers_sentence`, same rule: only real server names, never a
    tool list."""
    names = {td.get("name") for td in tool_definitions if isinstance(td, dict)}
    has_write = "Write" in names
    has_bash = "Bash" in names
    sections = [
        _HARNESS_SELF_DESCRIPTION_HEAD,
        _memory_capability_sentence(has_write),
        _HARNESS_SELF_DESCRIPTION_TAIL,
        _mcp_servers_sentence(mcp_servers),
    ]
    websearch_line = _websearch_capability_sentence("WebSearch" in names)
    if websearch_line:
        sections.append(websearch_line)
    sections.append("")
    sections.append(_closing_guidance_sentence(has_write, has_bash))
    return "\n".join(sections)

_FAMILY_NOTATION = {
    "deepseek": "Model notes: use native tool/function calling only. Prior reasoning from this "
                "conversation is preserved and replayed back to you automatically on tool-call "
                "turns -- you do not need to repeat it.",
    # H8 scope D: adapted from OpenCode's own kimi.txt (deep review.md
    # "Wire formats"/item 18) -- lines that measurably improve Kimi's own
    # tool-calling behaviour, byte-stable per session like every other
    # family block here (never per-model, never dynamic).
    "kimi": "Model notes: use native tool/function calling only. Tool-call ids in this "
            "conversation are preserved exactly as issued; never expect them to be renumbered. "
            "When calling tools, do not add explanations -- the tool calls themselves are "
            "self-explanatory; follow each tool's own description and parameters exactly. When "
            "you anticipate making multiple non-interfering tool calls, making them in parallel "
            "is highly recommended. Always use a tool to make a change rather than describing it. "
            "Make minimal changes to achieve the goal -- this matters. When a request could be read "
            "as either a question to answer or a task to complete, treat it as a task. Do not run "
            "`git commit` unless explicitly asked to.",
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
    append_system_prompt: Optional[str] = None, mcp_servers: Optional[list] = None,
) -> str:
    """Build the full system prompt string. Byte-stable for a given input
    -- call this exactly ONCE per session (agent/loop.py stores the result
    and never calls this again for that session). `mcp_servers` (H3 scope
    C) is resolved once, at the SAME "before the session's first request"
    point tool_definitions itself is -- see agent/assemble.py."""
    tool_definitions = tool_definitions or []
    sections = [
        IDENTITY_TEXT,
        persona_line(model_label),
        PLAN_MODE_POLICY,
        build_harness_self_description(tool_definitions, mcp_servers),  # finding 14: registry-driven, no false promises
        "## Tools available in this session\n" + tool_guidance_lines(tool_definitions),
        family_notation(family),
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
