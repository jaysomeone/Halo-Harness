"""rolo_claude.config.agents_md -- agent definitions (H6 scope A): discovery
precedence, frontmatter parsing, built-in specs (general-purpose/Explore/
Plan), the `tools:` string-or-list + `disallowedTools` resolution, and the
model-resolution chain (invocation -> frontmatter -> `CLAUDE_CODE_SUBAGENT_
MODEL`/settings -> parent).

Precedence (D-CFG "Instructions/.../agents", finding C "Sub-agents"), nearest
wins within a tier: managed -> `--agents` JSON/file -> `.claude/agents/**/
*.md` walking up from cwd to the git root (nearest directory wins) ->
`~/.claude/agents/**/*.md` -> plugin `agents/**/*.md` -> built-ins
(general-purpose, Explore, Plan). Identity is the `name` frontmatter field,
NOT the filename -- two files can define the same `name` and the higher-
precedence one wins outright (no merge); `name`+`description` are required,
a file missing either is skipped.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from rolo_claude.config.frontmatter import parse as parse_frontmatter
from rolo_claude.config.paths import find_git_root, home, managed_dir

BUILTIN_NAMES = ("general-purpose", "Explore", "Plan")


def _split_tools(value) -> Optional[list]:
    """`tools:`/`disallowedTools:`/`skills:` accept a comma-separated
    string OR a YAML list (brief A: "comma string or list"); None means
    the frontmatter key was absent ("every tool"/"no restriction"). An
    empty string/list is kept distinct from None (explicit "no tools")."""
    if value is None:
        return None
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    if isinstance(value, str):
        if not value.strip():
            return []
        return [v.strip() for v in value.split(",") if v.strip()]
    return None


@dataclass
class AgentSpec:
    name: str
    description: str
    tools: Optional[list] = None          # None = all tools; [] = none; entries may be "Agent(type)"-restricted
    disallowed_tools: Optional[list] = None
    model: Optional[str] = None           # sonnet|opus|haiku|inherit|<ref-or-alias>|None
    permission_mode: Optional[str] = None
    max_turns: Optional[int] = None
    skills: Optional[list] = None
    mcp_servers: Optional[object] = None
    hooks: Optional[dict] = None
    memory: Optional[str] = None          # accepted, ignored in v1 [D-CFG]
    background: bool = False
    omit_claude_md: bool = False
    effort: Optional[str] = None
    isolation: Optional[str] = None       # "worktree" accepted; v1 always runs in the same tree
    color: Optional[str] = None
    initial_prompt: Optional[str] = None
    body: str = ""
    source: str = ""                      # "managed" | "cli-agents" | "project:<dir>" | "user" | "plugin:<name>" | "built-in"
    path: Optional[Path] = None

    def skips_claude_md(self) -> bool:
        return bool(self.omit_claude_md) or self.name in ("Explore", "Plan")

    def includes_memory(self) -> bool:
        """D-CFG: "no memory index unless general-purpose"."""
        return self.name == "general-purpose"

    def allowed_subagent_types(self) -> "Optional[set]":
        """`tools: ["Agent(general-purpose)", "Agent(Explore)"]`-style
        content restricts WHICH subagent_type this agent's own Agent/Task
        calls may use (mirrors `Bash(git *)`'s content-restriction shape).
        None means "no restriction was written" -- still subject to the
        hard depth-1 cap agent/loop.py enforces regardless, and to whether
        "Agent"/"Task" survived `resolved_tools()` at all."""
        if not self.tools:
            return None
        out: set = set()
        found = False
        for t in self.tools:
            if t in ("Agent", "Task"):
                return None  # a bare grant -- no restriction
            for prefix in ("Agent(", "Task("):
                if t.startswith(prefix) and t.endswith(")"):
                    found = True
                    out.add(t[len(prefix):-1].strip())
        return out if found else None

    def resolved_tools(self, catalog_names) -> list:
        """The final tool-name allowlist for this agent, intersected with
        `catalog_names` (the frozen PARENT catalog -- H6 scope B: "never
        grows the parent's catalog"). `self.tools is None` means "every
        catalog tool" (Claude Code's own default); a bare "Agent"/"Task"
        entry or a content-restricted "Agent(name)"/"Task(name)" entry both
        grant the tool ITSELF (never "requires a tool literally named
        'Agent(name)'")."""
        catalog_names = list(catalog_names)
        if self.tools is None:
            names = list(catalog_names)
        else:
            names = []
            for t in self.tools:
                bare = t.split("(", 1)[0].strip()
                if bare and bare not in names:
                    names.append(bare)
            names = [n for n in names if n in catalog_names]
        if self.disallowed_tools:
            deny = {t.split("(", 1)[0].strip() for t in self.disallowed_tools}
            names = [n for n in names if n not in deny]
        return names


def _builtin_specs() -> "dict[str, AgentSpec]":
    explore_tools = ["Read", "Glob", "Grep", "Bash", "WebFetch", "ToolSearch"]
    return {
        "general-purpose": AgentSpec(
            name="general-purpose",
            description=(
                "General-purpose agent for researching complex questions, searching for code, and "
                "executing multi-step tasks. Has access to every tool except Agent/Task (depth-1 cap)."
            ),
            tools=None, disallowed_tools=["Agent", "Task"],
            body=(
                "You are a general-purpose sub-agent, spawned by another Claude session to handle one "
                "self-contained task. Complete it thoroughly, then report back a clear, self-contained "
                "final answer -- the caller only ever sees your LAST message, not the steps that "
                "produced it, so make that message stand on its own."
            ),
            source="built-in",
        ),
        "Explore": AgentSpec(
            name="Explore",
            description=(
                "Fast read-only search agent for locating code: files by pattern, symbols/keywords, "
                "\"where is X defined\". Never edits anything and skips CLAUDE.md."
            ),
            tools=list(explore_tools), omit_claude_md=True,
            body=(
                "You are a read-only exploration sub-agent. Use Read/Glob/Grep/Bash/WebFetch/ToolSearch "
                "to locate the requested code or answer. You have no Edit/Write tool -- never attempt "
                "to change anything. Report file paths and line numbers precisely in your final answer."
            ),
            source="built-in",
        ),
        "Plan": AgentSpec(
            name="Plan",
            description=(
                "Software architect agent for designing implementation plans: step-by-step plans, "
                "critical files, architectural trade-offs. Read-only (Explore's tool set) and skips "
                "CLAUDE.md."
            ),
            tools=list(explore_tools), omit_claude_md=True,
            body=(
                "You are a planning sub-agent. Research using Read/Glob/Grep/Bash/WebFetch/ToolSearch, "
                "then report a concrete, step-by-step implementation plan in your final answer -- "
                "critical files, order of changes, and trade-offs. Never edit or write anything."
            ),
            source="built-in",
        ),
    }


def _walk_up_agent_dirs(cwd: Path) -> list:
    """`.claude/agents` at `cwd` and every ancestor up to the git root (or
    the filesystem root when `cwd` isn't in a git worktree), NEAREST first."""
    cwd = Path(cwd).resolve()
    root = find_git_root(cwd) or cwd
    dirs = []
    current = cwd
    while True:
        dirs.append(current / ".claude" / "agents")
        if current == root or current.parent == current:
            break
        current = current.parent
    return dirs


def _iter_md_files(root: Path):
    root = Path(root)
    if not root.is_dir():
        return
    for p in sorted(root.rglob("*.md")):
        if p.is_file():
            yield p


def load_spec_from_file(path: Path, *, source: str) -> Optional[AgentSpec]:
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    fm, body = parse_frontmatter(text)
    name = fm.get("name")
    description = fm.get("description")
    if not name or not description:
        return None
    max_turns = fm.get("maxTurns")
    try:
        max_turns = int(max_turns) if max_turns is not None else None
    except (TypeError, ValueError):
        max_turns = None
    hooks = fm.get("hooks")
    return AgentSpec(
        name=str(name), description=str(description),
        tools=_split_tools(fm.get("tools")), disallowed_tools=_split_tools(fm.get("disallowedTools")),
        model=fm.get("model"), permission_mode=fm.get("permissionMode"), max_turns=max_turns,
        skills=_split_tools(fm.get("skills")), mcp_servers=fm.get("mcpServers"),
        hooks=hooks if isinstance(hooks, dict) else None, memory=fm.get("memory"),
        background=bool(fm.get("background", False)), omit_claude_md=bool(fm.get("omitClaudeMd", False)),
        effort=fm.get("effort"), isolation=fm.get("isolation"), color=fm.get("color"),
        initial_prompt=fm.get("initialPrompt"), body=(body or "").strip(),
        source=source, path=Path(path),
    )


def _load_dir_agents(root: Path, *, source: str) -> "dict[str, AgentSpec]":
    out: dict = {}
    for p in _iter_md_files(root):
        spec = load_spec_from_file(p, source=source)
        if spec is not None and spec.name not in out:
            out[spec.name] = spec  # first file wins WITHIN one directory tier
    return out


def parse_agents_json(text_or_path: str, *, cwd: Optional[Path] = None) -> "dict[str, AgentSpec]":
    """`--agents <json-or-file>` [D-CFG]: a literal JSON object string, or
    (in `-p`, per the brief) a path to a JSON file holding the same shape --
    `{name: {description, prompt, tools, model, permissionMode, ...}}`."""
    raw = text_or_path
    if not raw.strip().startswith("{"):
        try:
            candidate = Path(text_or_path)
            if candidate.is_file():
                raw = candidate.read_text(encoding="utf-8")
        except OSError:
            pass
    try:
        data = json.loads(raw)
    except ValueError:
        return {}
    if not isinstance(data, dict):
        return {}
    out: dict = {}
    for name, entry in data.items():
        if not isinstance(entry, dict):
            continue
        max_turns = entry.get("maxTurns")
        try:
            max_turns = int(max_turns) if max_turns is not None else None
        except (TypeError, ValueError):
            max_turns = None
        hooks = entry.get("hooks")
        out[name] = AgentSpec(
            name=str(name), description=str(entry.get("description", "")),
            tools=_split_tools(entry.get("tools")), disallowed_tools=_split_tools(entry.get("disallowedTools")),
            model=entry.get("model"), permission_mode=entry.get("permissionMode"), max_turns=max_turns,
            skills=_split_tools(entry.get("skills")), mcp_servers=entry.get("mcpServers"),
            hooks=hooks if isinstance(hooks, dict) else None, memory=entry.get("memory"),
            background=bool(entry.get("background", False)), omit_claude_md=bool(entry.get("omitClaudeMd", False)),
            effort=entry.get("effort"), isolation=entry.get("isolation"), color=entry.get("color"),
            initial_prompt=entry.get("initialPrompt"), body=str(entry.get("prompt", "") or ""),
            source="cli-agents",
        )
    return out


def discover_agents(cwd, settings=None, *, agents_flag: Optional[str] = None,
                     plugin_roots: Optional[list] = None) -> "dict[str, AgentSpec]":
    """The full precedence stack, nearest/highest-priority wins per `name`.
    Built bottom-up (lowest priority written first) so each later
    `.update()` from a higher-priority source overrides same-named entries."""
    cwd = Path(cwd)
    out: dict = {}
    out.update(_builtin_specs())
    for root in reversed(plugin_roots or []):
        out.update(_load_dir_agents(Path(root) / "agents", source=f"plugin:{root}"))
    out.update(_load_dir_agents(home() / ".claude" / "agents", source="user"))
    for d in reversed(_walk_up_agent_dirs(cwd)):
        out.update(_load_dir_agents(d, source=f"project:{d.parent}"))
    if agents_flag:
        out.update(parse_agents_json(agents_flag, cwd=cwd))
    try:
        managed = managed_dir()
    except Exception:
        managed = None
    if managed is not None:
        out.update(_load_dir_agents(Path(managed) / "agents", source="managed"))
    return out


def resolve_agent_model(*, invocation_model: Optional[str] = None, frontmatter_model: Optional[str] = None,
                         env: Optional[dict] = None, settings=None,
                         parent_ref, parent_profile, parent_small_ref=None, parent_small_profile=None,
                         state_dir, routes: Optional[dict] = None):
    """`(ModelRef, ModelProfile)` for a sub-agent/`--agent` session:
    invocation override -> frontmatter `model:` -> `CLAUDE_CODE_SUBAGENT_
    MODEL` (env) / `settings.subagent_model` -> the PARENT's own model,
    unchanged [D-CFG chain]. `haiku` resolves to the parent's SMALL model
    (brief: "haiku -> small model"); `inherit`, or nothing resolving at
    all, reuses the parent's ref/profile OBJECTS directly (never re-parsed)."""
    from rolo_claude.model import parse_model_ref, resolve_model_profile

    env = env or {}
    raw = (invocation_model or frontmatter_model or env.get("CLAUDE_CODE_SUBAGENT_MODEL")
           or (settings.subagent_model if settings is not None else None))
    if not raw or raw == "inherit":
        return parent_ref, parent_profile
    if raw == "haiku":
        if parent_small_ref is not None:
            profile = parent_small_profile or resolve_model_profile(parent_small_ref, state_dir, routes)
            return parent_small_ref, profile
        return parent_ref, parent_profile
    ref = parse_model_ref(raw, routes)
    profile = resolve_model_profile(ref, state_dir, routes)
    return ref, profile


_AGENT_MENTION_RE = re.compile(r"@agent-([A-Za-z0-9_-]+)")


def find_agent_mentions(text: str, agents: "dict[str, AgentSpec]") -> list:
    """`@agent-<name>` mentions in a prompt (brief A: "@agent-<name> in a
    prompt forces one") that match a DISCOVERED agent's `name`, in
    first-seen order, de-duplicated. A mention of an unknown name is
    silently ignored here (headless.py/agent/loop.py decide what, if
    anything, to tell the user about it) -- this function only answers
    "which real agents were named"."""
    seen: list = []
    for m in _AGENT_MENTION_RE.finditer(text or ""):
        name = m.group(1)
        if name in agents and name not in seen:
            seen.append(name)
    return seen


def agent_mention_instruction(names: list) -> str:
    """The snapshot text appended when `find_agent_mentions` found at least
    one real match -- tells the model to actually use the Agent/Task tool
    with that `subagent_type` rather than just reading `@agent-<name>` as
    plain text (which a model with no special handling would otherwise do)."""
    if not names:
        return ""
    if len(names) == 1:
        return (f"The user's message explicitly named the '{names[0]}' sub-agent (@agent-{names[0]}). "
                f"Use the Agent tool with subagent_type=\"{names[0]}\" for this request.")
    listed = ", ".join(f'"{n}"' for n in names)
    return (f"The user's message explicitly named these sub-agents: {listed}. Use the Agent tool with "
            f"the matching subagent_type for each part of the request that names one.")
