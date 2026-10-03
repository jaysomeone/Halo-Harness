"""halo_harness.config.agents_md -- agent definitions (H6 scope A): discovery
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

from halo_harness.config.frontmatter import parse as parse_frontmatter
from halo_harness.config.paths import find_git_root, home, managed_dir

BUILTIN_NAMES = ("general-purpose", "Explore", "Plan", "Coder", "Reviewer", "Researcher", "Judge", "Tester")


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
    # V2c (H15): a `roles.py::ROLE_NAMES` entry this agent's model resolves
    # from (config.json/team.json's own table, or the cost-aware default)
    # whenever `model` above is unset -- a built-in sets this directly
    # (below); a custom `.claude/agents/*.md` file sets it via a `role:`
    # frontmatter key; an `Agent(role=...)` tool-call argument overrides
    # either one for that ONE call only (see agent/subagent.py).
    role: Optional[str] = None
    permission_mode: Optional[str] = None
    max_turns: Optional[int] = None
    skills: Optional[list] = None
    mcp_servers: Optional[object] = None
    hooks: Optional[dict] = None
    memory: Optional[str] = None          # "user"|"project"|"local" scope; see agent/subagent.py's own memory_scope
    background: bool = False
    omit_claude_md: bool = False
    effort: Optional[str] = None
    isolation: Optional[str] = None       # "worktree" accepted; v1 always runs in the same tree
    color: Optional[str] = None
    # Halo 2.0.2 round 2 (brief B): an organization position's own
    # `reports` -- when set, THIS agent's own Agent/Task calls may only
    # use one of these subagent_types (`agent/subagent.py::
    # _build_child_session` reads it and passes it as the child's own
    # `agent_type_restriction`, the SAME check a `tools: ["Agent(name)"]`
    # content-restricted agent file already enforces via `allowed_
    # subagent_types()`). `None` (every built-in and `.claude/agents/
    # *.md` file) means no extra restriction beyond the depth cap; an
    # EMPTY set (an org leaf position with no `reports`) means this
    # position may not spawn anything at all.
    delegate_restriction: Optional[set] = None
    # Halo 2.0.2 round 2 (brief B): "the dock shows each running position
    # as `<title> (<role>)`" -- the label `agent/subagent.py::
    # run_agent_call` puts on the `subagent_start`/`subagent_end` events
    # (and therefore the TUI's own `SubAgentCard`) instead of the bare
    # `name` above, when set. `None` (every built-in and `.claude/agents/
    # *.md` file) keeps the dock showing the plain name, unchanged --
    # this is purely a DISPLAY label; `name` itself stays the identity
    # every lookup/task-resume/hook payload keys on.
    dock_label: Optional[str] = None
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
    research_tools = explore_tools + ["WebSearch"]
    # V2c (H15)/Halo 2.0.2 (brief A.1): every built-in gets a `role=` so it
    # resolves its model from `roles.py`'s table (config.json/team.json, or
    # the cost-aware default) unless its OWN file sets `model:` (none of
    # these do). `general-purpose` gets "orchestrator" (an absent/empty
    # table entry for it just means "the session model", its documented
    # default anyway); `Explore` and `Researcher` share "researcher"
    # (cheap/exploration); `Coder` gets its own "coder" role; `Reviewer`
    # keeps "reviewer" (strong/review). 2.0.2: `Plan` moves to its OWN new
    # "planner" role (was "reviewer") -- brief A.1 "the Plan agent type
    # resolves through planner"; `Judge` and `Tester` are new built-ins on
    # the two new matching roles.
    return {
        "general-purpose": AgentSpec(
            name="general-purpose",
            description=(
                "General-purpose agent for researching complex questions, searching for code, and "
                "executing multi-step tasks. Has access to every tool except Agent/Task (depth-1 cap)."
            ),
            tools=None, disallowed_tools=["Agent", "Task"], role="orchestrator",
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
            tools=list(explore_tools), omit_claude_md=True, role="researcher",
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
            tools=list(explore_tools), omit_claude_md=True, role="planner",
            body=(
                "You are a planning sub-agent. Research using Read/Glob/Grep/Bash/WebFetch/ToolSearch, "
                "then report a concrete, step-by-step implementation plan in your final answer -- "
                "critical files, order of changes, and trade-offs. Never edit or write anything."
            ),
            source="built-in",
        ),
        "Coder": AgentSpec(
            name="Coder",
            description=(
                "Implementation agent for writing and editing code: has the full read/write tool set "
                "(minus Agent/Task, depth-1 cap) and reads project CLAUDE.md/conventions like a normal "
                "session does."
            ),
            tools=None, disallowed_tools=["Agent", "Task"], role="coder",
            body=(
                "You are a coding sub-agent, spawned to implement a self-contained change. Read enough "
                "of the surrounding code first to match its conventions, make the change, and verify it "
                "(run the relevant tests/build when practical). Report back exactly what you changed "
                "(file paths) and how you verified it -- your caller only sees your LAST message."
            ),
            source="built-in",
        ),
        "Reviewer": AgentSpec(
            name="Reviewer",
            description=(
                "Read-only code-review agent: correctness, reuse/simplification, and consistency with "
                "the surrounding codebase. Never edits anything."
            ),
            tools=list(explore_tools), omit_claude_md=True, role="reviewer",
            body=(
                "You are a code-review sub-agent. Use Read/Glob/Grep/Bash/WebFetch/ToolSearch to examine "
                "the code in question -- never edit or write anything. Report concrete findings (file "
                "paths and line numbers), each with why it matters and, where useful, a suggested fix "
                "described in words."
            ),
            source="built-in",
        ),
        "Researcher": AgentSpec(
            name="Researcher",
            description=(
                "Read-only research agent for open-ended questions across the codebase and the web: "
                "Explore's tool set plus WebSearch. Never edits anything and skips CLAUDE.md."
            ),
            tools=list(research_tools), omit_claude_md=True, role="researcher",
            body=(
                "You are a research sub-agent. Use Read/Glob/Grep/Bash/WebFetch/WebSearch/ToolSearch to "
                "investigate the question thoroughly -- never edit or write anything. Report a clear, "
                "self-contained answer; when the task asks for a specific fact (a count, a file path, a "
                "yes/no), lead with exactly that."
            ),
            source="built-in",
        ),
        # Halo 2.0.2 brief A.1: "`Judge` and `Tester` join the built-in
        # agent map with one-paragraph system prompts" -- judge adjudicates
        # candidate outputs or verifies results against acceptance
        # criteria (organization flows in a later round, and a direct
        # `Agent(role="judge")`/`Agent(subagent_type="Judge")` call today);
        # tester runs the named suites/live checks and reports verbatim.
        "Judge": AgentSpec(
            name="Judge",
            description=(
                "Adjudication agent: given one or more candidate outputs (or a result plus acceptance "
                "criteria), decides pass/fail with reasons. Read-only (Explore's tool set) and skips "
                "CLAUDE.md."
            ),
            tools=list(explore_tools), omit_claude_md=True, role="judge",
            body=(
                "You are a judging sub-agent. Adjudicate the candidate outputs you were given, or verify "
                "the result you were given against the stated acceptance criteria -- use Read/Glob/Grep/"
                "Bash/WebFetch/ToolSearch to check any claim you are not already certain of, never edit or "
                "write anything. Report a clear pass/fail (or which candidate wins) and your reasons."
            ),
            source="built-in",
        ),
        "Tester": AgentSpec(
            name="Tester",
            description=(
                "Verification agent: runs the named test suite(s) and any live checks asked for, and "
                "reports the results. Has Bash plus Explore's read-only tools; skips CLAUDE.md."
            ),
            tools=list(explore_tools), omit_claude_md=True, role="tester",
            body=(
                "You are a testing sub-agent. Run exactly the suites and live checks you were asked to run "
                "(Bash), and report their results verbatim -- pass/fail counts, the first failure's output "
                "in full, and anything that errored before producing a result. Never edit or write "
                "anything, and never soften or summarize away a failure."
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
        model=fm.get("model"), role=fm.get("role"), permission_mode=fm.get("permissionMode"), max_turns=max_turns,
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
            model=entry.get("model"), role=entry.get("role"),
            permission_mode=entry.get("permissionMode"), max_turns=max_turns,
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
                         role_name: Optional[str] = None, role_table: Optional[dict] = None,
                         cli_role_overrides: Optional[dict] = None,
                         env: Optional[dict] = None, settings=None,
                         parent_ref, parent_profile, parent_small_ref=None, parent_small_profile=None,
                         state_dir, routes: Optional[dict] = None):
    """`(ModelRef, ModelProfile)` for a sub-agent/`--agent` session. Full
    chain (V2c/H15 roles inserted into the pre-existing D-CFG chain):
    invocation override -> a CLI `--role` override for THIS agent's own
    role (`role_name`: an `Agent(role=...)` call-time override, else the
    agent's own `role:` frontmatter/built-in default) -> frontmatter
    `model:` -> `role_table` (config.json/team.json, or the cost-aware
    default -- `roles.py::resolve_role_table`) for that same role ->
    `CLAUDE_CODE_SUBAGENT_MODEL` (env) / `settings.subagent_model` -> the
    PARENT's own model, unchanged. A CLI `--role` override deliberately
    wins even over the agent file's own `model:` -- it is a freshly-typed,
    run-only instruction the user gets to trump a shared/managed agent file
    with; short of that, "resolve from the role table UNLESS the agent file
    sets model:" (brief) is exactly what slotting the role-table lookup
    BELOW frontmatter_model in this same `or` chain gives us for free.
    `haiku` resolves to the parent's SMALL model (brief: "haiku -> small
    model"); `inherit`, or nothing resolving at all, reuses the parent's
    ref/profile OBJECTS directly (never re-parsed) -- unaffected by any of
    the role plumbing above when no role applies (every new parameter here
    defaults to None/{}, so an old caller that passes none of them behaves
    byte-for-byte as before)."""
    from halo_harness.model import ModelRef, parse_model_ref, resolve_model_profile
    from halo_harness.roles import role_value_parts

    env = env or {}
    role_table = role_table or {}
    cli_role_overrides = cli_role_overrides or {}
    # Halo 2.0.2 (brief A.2): a role-table/CLI-override VALUE may now be
    # `{"model", "effort"}` rather than a bare string -- `role_value_parts`
    # is the one place that unpacks either shape; only the model half
    # belongs in this function's own chain (the effort half is applied
    # separately, alongside this call, by `agent/subagent.py`'s own
    # `roles.role_effort_for` -- keeping this function's return shape a
    # stable 2-tuple for every existing caller).
    role_cli_model, _cli_effort = role_value_parts(cli_role_overrides.get(role_name) if role_name else None)
    role_table_model, _table_effort = role_value_parts(role_table.get(role_name) if role_name else None)
    # Halo 2.0.2 (brief A.1): `subagent_default` is the last rung before
    # the parent/session model, and ONLY for a sub-agent with NO role
    # name at all (`role_name` falsy) -- a role-bearing agent with
    # nothing configured for ITS OWN role still means "the session
    # model" unchanged; `subagent_default` is deliberately never consulted
    # for it (that would blur "this role has no override" with "no role
    # at all").
    subagent_default_cli, _ = role_value_parts(cli_role_overrides.get("subagent_default"))
    subagent_default_table, _ = role_value_parts(role_table.get("subagent_default"))
    subagent_default_model = subagent_default_cli or subagent_default_table
    raw = (invocation_model or role_cli_model or frontmatter_model or role_table_model
           or env.get("CLAUDE_CODE_SUBAGENT_MODEL")
           or (settings.subagent_model if settings is not None else None)
           or (subagent_default_model if not role_name else None))
    if not raw or raw == "inherit":
        return parent_ref, parent_profile
    if raw == "haiku":
        if parent_small_ref is not None:
            profile = parent_small_profile or resolve_model_profile(parent_small_ref, state_dir, routes)
            return parent_small_ref, profile
        return parent_ref, parent_profile
    # H11b finding 25: `parse_model_ref`'s bare-alias auto-routing (a bare
    # "sonnet"/"opus"/... resolves to cc: or ant: depending on what's
    # available) is scoped to a HUMAN's own `--model`/`/model` request
    # (model.py's own docstring) -- a sub-agent's frontmatter `model:` or
    # an `Agent(model=...)` override must never silently jump to a
    # DIFFERENT provider than the parent's own just because one of these
    # nine words happens to match and `claude` happens to be logged in on
    # this machine (verified live: a DeepSeek/Kimi session's plugin agents
    # with `model: sonnet` started spawning nested claude subprocesses on
    # the subscription once H11 shipped; before it they correctly failed
    # with InvalidModelError, the same way a made-up model name would). A
    # Claude-family PARENT (cc:/ant:) resolves the alias on its OWN
    # already-chosen route (never re-deciding cc: vs ant: independently);
    # any other parent keeps the bare word as a literal model id on the
    # parent's own provider/dialect -- the same explicit failure a
    # not-really-a-model-id string always produced pre-H11.
    from halo_harness.providers.cc_models import BARE_ALIAS_NAMES
    base_raw = raw[:-len("[1m]")] if raw.endswith("[1m]") else raw
    if base_raw in BARE_ALIAS_NAMES:
        if parent_ref.provider == "cc":
            ref = parse_model_ref(f"cc:{raw}", routes)
            return ref, resolve_model_profile(ref, state_dir, routes)
        if parent_ref.provider == "anthropic" and parent_ref.dialect == "anthropic-passthrough":
            ref = parse_model_ref(f"ant:{raw}", routes)
            return ref, resolve_model_profile(ref, state_dir, routes)
        ref = ModelRef(raw=raw, provider=parent_ref.provider, model=raw, dialect=parent_ref.dialect)
        return ref, resolve_model_profile(ref, state_dir, routes)
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
