"""tests.test_agents_md -- H6 scope A: agent definition discovery precedence,
frontmatter parsing (tools string vs list, all fields), built-in tool sets,
the model-resolution chain, and `@agent-<name>` mention detection.
"""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.helpers.runner import Ctx, new_registry, print_results, run_all
from rolo_claude.config.agents_md import (
    AgentSpec, BUILTIN_NAMES, agent_mention_instruction, discover_agents, find_agent_mentions,
    load_spec_from_file, parse_agents_json, resolve_agent_model,
)

test, TESTS = new_registry()


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _agent_md(name: str, description: str, **extra) -> str:
    lines = ["---", f"name: {name}", f"description: {description}"]
    for k, v in extra.items():
        lines.append(f"{k}: {v}")
    lines.append("---")
    lines.append(f"Body for {name}.")
    return "\n".join(lines) + "\n"


# ---- AgentSpec helper methods ----------------------------------------------

@test
def test_skips_claude_md_omit_flag(ctx: Ctx):
    spec = AgentSpec(name="custom", description="d", omit_claude_md=True)
    ctx.check("omitClaudeMd true -> skips", spec.skips_claude_md() is True)


@test
def test_skips_claude_md_builtin_explore_plan(ctx: Ctx):
    ctx.check("Explore skips CLAUDE.md", AgentSpec(name="Explore", description="d").skips_claude_md() is True)
    ctx.check("Plan skips CLAUDE.md", AgentSpec(name="Plan", description="d").skips_claude_md() is True)
    ctx.check("general-purpose does not", AgentSpec(name="general-purpose", description="d").skips_claude_md() is False)
    ctx.check("a custom name does not", AgentSpec(name="custom", description="d").skips_claude_md() is False)


@test
def test_includes_memory_only_general_purpose(ctx: Ctx):
    ctx.check("general-purpose includes memory", AgentSpec(name="general-purpose", description="d").includes_memory())
    ctx.check("Explore excludes memory", not AgentSpec(name="Explore", description="d").includes_memory())
    ctx.check("custom name excludes memory", not AgentSpec(name="custom", description="d").includes_memory())


@test
def test_allowed_subagent_types_none_when_no_tools(ctx: Ctx):
    ctx.check("tools=None -> no restriction", AgentSpec(name="x", description="d").allowed_subagent_types() is None)


@test
def test_allowed_subagent_types_bare_grant_is_unrestricted(ctx: Ctx):
    spec = AgentSpec(name="x", description="d", tools=["Read", "Agent"])
    ctx.check("bare 'Agent' entry -> no restriction", spec.allowed_subagent_types() is None)


@test
def test_allowed_subagent_types_content_restricted(ctx: Ctx):
    spec = AgentSpec(name="x", description="d", tools=["Read", "Agent(general-purpose)", "Task(Explore)"])
    ctx.check("restriction set", spec.allowed_subagent_types() == {"general-purpose", "Explore"})


@test
def test_resolved_tools_none_means_everything_in_catalog(ctx: Ctx):
    spec = AgentSpec(name="x", description="d")
    ctx.check("all catalog names kept", spec.resolved_tools(["Read", "Write", "Bash"]) == ["Read", "Write", "Bash"])


@test
def test_resolved_tools_intersects_with_catalog(ctx: Ctx):
    spec = AgentSpec(name="x", description="d", tools=["Read", "Write", "NotInCatalog"])
    ctx.check("only catalog-present names kept", spec.resolved_tools(["Read", "Bash"]) == ["Read"])


@test
def test_resolved_tools_content_restricted_agent_grants_bare_name(ctx: Ctx):
    spec = AgentSpec(name="x", description="d", tools=["Agent(general-purpose)"])
    ctx.check("Agent(type) grants bare 'Agent'", spec.resolved_tools(["Agent", "Read"]) == ["Agent"])


@test
def test_resolved_tools_disallowed_removes_even_if_listed(ctx: Ctx):
    spec = AgentSpec(name="x", description="d", tools=["Read", "Write"], disallowed_tools=["Write"])
    ctx.check("disallowed strips", spec.resolved_tools(["Read", "Write"]) == ["Read"])


@test
def test_resolved_tools_general_purpose_excludes_agent(ctx: Ctx):
    spec = discover_agents(Path(tempfile.mkdtemp(prefix="rc-noproj-")))["general-purpose"]
    ctx.check("Agent/Task stripped from general-purpose", spec.resolved_tools(["Agent", "Task", "Read"]) == ["Read"])


# ---- built-ins --------------------------------------------------------------

@test
def test_builtin_names_constant(ctx: Ctx):
    ctx.check("BUILTIN_NAMES", set(BUILTIN_NAMES) == {"general-purpose", "Explore", "Plan"})


@test
def test_builtins_always_present(ctx: Ctx):
    cwd = Path(tempfile.mkdtemp(prefix="rc-builtins-"))
    agents = discover_agents(cwd)
    for name in BUILTIN_NAMES:
        ctx.check(f"{name} discovered", name in agents)


@test
def test_explore_and_plan_are_readonly_tool_sets(ctx: Ctx):
    cwd = Path(tempfile.mkdtemp(prefix="rc-ro-"))
    agents = discover_agents(cwd)
    for name in ("Explore", "Plan"):
        spec = agents[name]
        ctx.check(f"{name} has no Edit", "Edit" not in (spec.tools or []))
        ctx.check(f"{name} has no Write", "Write" not in (spec.tools or []))
        ctx.check(f"{name} has Read", "Read" in (spec.tools or []))
        ctx.check(f"{name} omits CLAUDE.md", spec.skips_claude_md())


@test
def test_general_purpose_has_all_tools_minus_agent(ctx: Ctx):
    cwd = Path(tempfile.mkdtemp(prefix="rc-gp-"))
    spec = discover_agents(cwd)["general-purpose"]
    ctx.check("tools is None (all)", spec.tools is None)
    ctx.check("Agent/Task disallowed", set(spec.disallowed_tools or []) == {"Agent", "Task"})


# ---- frontmatter parsing (load_spec_from_file) ------------------------------

@test
def test_load_spec_from_file_all_fields(ctx: Ctx):
    tmp = Path(tempfile.mkdtemp(prefix="rc-spec-"))
    path = _write(tmp / "reviewer.md", (
        "---\n"
        "name: reviewer\n"
        "description: Reviews code for bugs\n"
        "tools: Read, Grep, Bash\n"
        "disallowedTools: Write, Edit\n"
        "model: opus\n"
        "permissionMode: acceptEdits\n"
        "maxTurns: 12\n"
        "skills: lint, format\n"
        "background: true\n"
        "omitClaudeMd: true\n"
        "effort: high\n"
        "isolation: worktree\n"
        "color: blue\n"
        "initialPrompt: Start reviewing now\n"
        "---\n"
        "You are a code reviewer.\n"
    ))
    spec = load_spec_from_file(path, source="project:x")
    ctx.check("name", spec.name == "reviewer")
    ctx.check("description", spec.description == "Reviews code for bugs")
    ctx.check("tools string -> list", spec.tools == ["Read", "Grep", "Bash"])
    ctx.check("disallowedTools string -> list", spec.disallowed_tools == ["Write", "Edit"])
    ctx.check("model", spec.model == "opus")
    ctx.check("permissionMode", spec.permission_mode == "acceptEdits")
    ctx.check("maxTurns coerced to int", spec.max_turns == 12 and isinstance(spec.max_turns, int))
    ctx.check("skills string -> list", spec.skills == ["lint", "format"])
    ctx.check("background bool", spec.background is True)
    ctx.check("omitClaudeMd bool", spec.omit_claude_md is True)
    ctx.check("effort", spec.effort == "high")
    ctx.check("isolation", spec.isolation == "worktree")
    ctx.check("color", spec.color == "blue")
    ctx.check("initialPrompt", spec.initial_prompt == "Start reviewing now")
    ctx.check("body captured", spec.body == "You are a code reviewer.")
    ctx.check("source recorded", spec.source == "project:x")


@test
def test_load_spec_from_file_tools_as_yaml_list(ctx: Ctx):
    tmp = Path(tempfile.mkdtemp(prefix="rc-spec2-"))
    path = _write(tmp / "a.md", (
        "---\nname: lister\ndescription: d\ntools:\n  - Read\n  - Write\n---\nBody\n"
    ))
    spec = load_spec_from_file(path, source="user")
    ctx.check("tools list form parsed", spec.tools == ["Read", "Write"])


@test
def test_load_spec_from_file_missing_name_or_description_skipped(ctx: Ctx):
    tmp = Path(tempfile.mkdtemp(prefix="rc-spec3-"))
    no_name = _write(tmp / "no_name.md", "---\ndescription: d\n---\nBody\n")
    no_desc = _write(tmp / "no_desc.md", "---\nname: x\n---\nBody\n")
    ctx.check("missing name -> None", load_spec_from_file(no_name, source="user") is None)
    ctx.check("missing description -> None", load_spec_from_file(no_desc, source="user") is None)


@test
def test_load_spec_from_file_bad_max_turns_falls_back_to_none(ctx: Ctx):
    tmp = Path(tempfile.mkdtemp(prefix="rc-spec4-"))
    path = _write(tmp / "a.md", "---\nname: x\ndescription: d\nmaxTurns: not-a-number\n---\nBody\n")
    spec = load_spec_from_file(path, source="user")
    ctx.check("bad maxTurns -> None, no crash", spec.max_turns is None)


@test
def test_load_spec_from_file_missing_path_returns_none(ctx: Ctx):
    ctx.check("nonexistent file -> None",
              load_spec_from_file(Path(tempfile.mkdtemp()) / "nope.md", source="user") is None)


# ---- parse_agents_json (--agents) -------------------------------------------

@test
def test_parse_agents_json_literal_string(ctx: Ctx):
    raw = '{"reviewer": {"description": "reviews", "prompt": "You review code.", "tools": "Read,Grep"}}'
    agents = parse_agents_json(raw)
    ctx.check("one agent parsed", "reviewer" in agents)
    ctx.check("description", agents["reviewer"].description == "reviews")
    ctx.check("prompt -> body", agents["reviewer"].body == "You review code.")
    ctx.check("tools string parsed", agents["reviewer"].tools == ["Read", "Grep"])
    ctx.check("source is cli-agents", agents["reviewer"].source == "cli-agents")


@test
def test_parse_agents_json_from_file_path(ctx: Ctx):
    tmp = Path(tempfile.mkdtemp(prefix="rc-agentsjson-"))
    path = tmp / "agents.json"
    path.write_text('{"deployer": {"description": "deploys"}}', encoding="utf-8")
    agents = parse_agents_json(str(path))
    ctx.check("agent from file", "deployer" in agents)


@test
def test_parse_agents_json_malformed_returns_empty(ctx: Ctx):
    ctx.check("malformed JSON -> {}", parse_agents_json("{not json") == {})


@test
def test_parse_agents_json_non_dict_root_returns_empty(ctx: Ctx):
    ctx.check("array root -> {}", parse_agents_json("[1,2,3]") == {})


# ---- discover_agents precedence ---------------------------------------------

@test
def test_discover_agents_project_overrides_builtin_name(ctx: Ctx):
    cwd = Path(tempfile.mkdtemp(prefix="rc-proj-"))
    _write(cwd / ".claude" / "agents" / "gp.md", _agent_md("general-purpose", "Custom override"))
    agents = discover_agents(cwd)
    ctx.check("project definition overrides built-in", agents["general-purpose"].description == "Custom override")
    ctx.check("source is project", agents["general-purpose"].source.startswith("project:"))


@test
def test_discover_agents_nearest_project_dir_wins(ctx: Ctx):
    root = Path(tempfile.mkdtemp(prefix="rc-nearest-"))
    (root / ".git").mkdir()
    sub = root / "sub"
    _write(root / ".claude" / "agents" / "r.md", _agent_md("reviewer", "far description"))
    _write(sub / ".claude" / "agents" / "r.md", _agent_md("reviewer", "near description"))
    agents = discover_agents(sub)
    ctx.check("nearest (sub) wins over farther (root)", agents["reviewer"].description == "near description")


@test
def test_discover_agents_user_dir_picked_up(ctx: Ctx):
    tmp_home = Path(tempfile.mkdtemp(prefix="rc-userhome-"))
    cwd = Path(tempfile.mkdtemp(prefix="rc-usercwd-"))
    _write(tmp_home / ".claude" / "agents" / "u.md", _agent_md("custom-user-agent", "from user dir"))
    old = os.environ.get("BRIDGE_TEST_HOME")
    os.environ["BRIDGE_TEST_HOME"] = str(tmp_home)
    try:
        agents = discover_agents(cwd)
        ctx.check("user agent discovered", "custom-user-agent" in agents)
        ctx.check("source is user", agents["custom-user-agent"].source == "user")
    finally:
        if old is None:
            os.environ.pop("BRIDGE_TEST_HOME", None)
        else:
            os.environ["BRIDGE_TEST_HOME"] = old


@test
def test_discover_agents_project_overrides_user(ctx: Ctx):
    tmp_home = Path(tempfile.mkdtemp(prefix="rc-userhome2-"))
    cwd = Path(tempfile.mkdtemp(prefix="rc-usercwd2-"))
    _write(tmp_home / ".claude" / "agents" / "shared.md", _agent_md("shared", "user version"))
    _write(cwd / ".claude" / "agents" / "shared.md", _agent_md("shared", "project version"))
    old = os.environ.get("BRIDGE_TEST_HOME")
    os.environ["BRIDGE_TEST_HOME"] = str(tmp_home)
    try:
        agents = discover_agents(cwd)
        ctx.check("project wins over user", agents["shared"].description == "project version")
    finally:
        if old is None:
            os.environ.pop("BRIDGE_TEST_HOME", None)
        else:
            os.environ["BRIDGE_TEST_HOME"] = old


@test
def test_discover_agents_agents_flag_overrides_project(ctx: Ctx):
    cwd = Path(tempfile.mkdtemp(prefix="rc-flagoverride-"))
    _write(cwd / ".claude" / "agents" / "a.md", _agent_md("reviewer", "project version"))
    agents = discover_agents(cwd, agents_flag='{"reviewer": {"description": "flag version"}}')
    ctx.check("--agents overrides project file", agents["reviewer"].description == "flag version")


@test
def test_discover_agents_managed_overrides_everything(ctx: Ctx):
    cwd = Path(tempfile.mkdtemp(prefix="rc-managed-"))
    managed = Path(tempfile.mkdtemp(prefix="rc-manageddir-"))
    _write(cwd / ".claude" / "agents" / "a.md", _agent_md("reviewer", "project version"))
    _write(managed / "agents" / "a.md", _agent_md("reviewer", "managed version"))
    old = os.environ.get("BRIDGE_TEST_MANAGED_DIR")
    os.environ["BRIDGE_TEST_MANAGED_DIR"] = str(managed)
    try:
        agents = discover_agents(cwd, agents_flag='{"reviewer": {"description": "flag version"}}')
        ctx.check("managed wins over --agents/project too", agents["reviewer"].description == "managed version")
    finally:
        if old is None:
            os.environ.pop("BRIDGE_TEST_MANAGED_DIR", None)
        else:
            os.environ["BRIDGE_TEST_MANAGED_DIR"] = old


@test
def test_discover_agents_identity_is_name_not_filename(ctx: Ctx):
    cwd = Path(tempfile.mkdtemp(prefix="rc-identity-"))
    _write(cwd / ".claude" / "agents" / "whatever-filename.md", _agent_md("real-name", "d"))
    agents = discover_agents(cwd)
    ctx.check("keyed by name frontmatter, not filename", "real-name" in agents)
    ctx.check("filename itself is not a key", "whatever-filename" not in agents)


# ---- resolve_agent_model chain ----------------------------------------------

class _FakeRef:
    def __init__(self, raw):
        self.raw = raw


class _FakeProfile:
    pass


@test
def test_resolve_agent_model_invocation_wins(ctx: Ctx):
    from rolo_claude.model import parse_model_ref
    routes = {}
    ref, _profile = resolve_agent_model(
        invocation_model="or:vendor/explicit", frontmatter_model="or:vendor/frontmatter",
        parent_ref=_FakeRef("or:vendor/parent"), parent_profile=_FakeProfile(),
        state_dir=Path(tempfile.mkdtemp()), routes=routes,
    )
    ctx.check("invocation overrides frontmatter", ref.model == "vendor/explicit")


@test
def test_resolve_agent_model_frontmatter_used_when_no_invocation(ctx: Ctx):
    ref, _profile = resolve_agent_model(
        invocation_model=None, frontmatter_model="or:vendor/frommeta",
        parent_ref=_FakeRef("or:vendor/parent"), parent_profile=_FakeProfile(),
        state_dir=Path(tempfile.mkdtemp()), routes={},
    )
    ctx.check("frontmatter model used", ref.model == "vendor/frommeta")


@test
def test_resolve_agent_model_env_fallback(ctx: Ctx):
    ref, _profile = resolve_agent_model(
        invocation_model=None, frontmatter_model=None, env={"CLAUDE_CODE_SUBAGENT_MODEL": "or:vendor/envmodel"},
        parent_ref=_FakeRef("or:vendor/parent"), parent_profile=_FakeProfile(),
        state_dir=Path(tempfile.mkdtemp()), routes={},
    )
    ctx.check("env var used as fallback", ref.model == "vendor/envmodel")


@test
def test_resolve_agent_model_settings_fallback(ctx: Ctx):
    class _Settings:
        subagent_model = "or:vendor/fromsettings"
    ref, _profile = resolve_agent_model(
        invocation_model=None, frontmatter_model=None, env={}, settings=_Settings(),
        parent_ref=_FakeRef("or:vendor/parent"), parent_profile=_FakeProfile(),
        state_dir=Path(tempfile.mkdtemp()), routes={},
    )
    ctx.check("settings.subagent_model used last", ref.model == "vendor/fromsettings")


@test
def test_resolve_agent_model_inherit_reuses_parent_objects(ctx: Ctx):
    parent_ref, parent_profile = _FakeRef("or:vendor/parent"), _FakeProfile()
    ref, profile = resolve_agent_model(
        invocation_model="inherit", parent_ref=parent_ref, parent_profile=parent_profile,
        state_dir=Path(tempfile.mkdtemp()), routes={},
    )
    ctx.check("inherit reuses the SAME ref object", ref is parent_ref)
    ctx.check("inherit reuses the SAME profile object", profile is parent_profile)


@test
def test_resolve_agent_model_nothing_given_also_inherits(ctx: Ctx):
    parent_ref, parent_profile = _FakeRef("or:vendor/parent"), _FakeProfile()
    ref, profile = resolve_agent_model(parent_ref=parent_ref, parent_profile=parent_profile,
                                        state_dir=Path(tempfile.mkdtemp()), routes={})
    ctx.check("no override at all -> parent ref", ref is parent_ref)
    ctx.check("no override at all -> parent profile", profile is parent_profile)


@test
def test_resolve_agent_model_haiku_uses_small_ref(ctx: Ctx):
    parent_small_ref, parent_small_profile = _FakeRef("or:vendor/small"), _FakeProfile()
    ref, profile = resolve_agent_model(
        invocation_model="haiku", parent_ref=_FakeRef("or:vendor/parent"), parent_profile=_FakeProfile(),
        parent_small_ref=parent_small_ref, parent_small_profile=parent_small_profile,
        state_dir=Path(tempfile.mkdtemp()), routes={},
    )
    ctx.check("haiku -> small ref object", ref is parent_small_ref)
    ctx.check("haiku -> small profile object", profile is parent_small_profile)


@test
def test_resolve_agent_model_haiku_falls_back_to_parent_without_small(ctx: Ctx):
    parent_ref, parent_profile = _FakeRef("or:vendor/parent"), _FakeProfile()
    ref, profile = resolve_agent_model(
        invocation_model="haiku", parent_ref=parent_ref, parent_profile=parent_profile,
        parent_small_ref=None, state_dir=Path(tempfile.mkdtemp()), routes={},
    )
    ctx.check("haiku with no small ref -> parent ref", ref is parent_ref)
    ctx.check("haiku with no small ref -> parent profile", profile is parent_profile)


# ---- @agent-<name> mentions --------------------------------------------------

@test
def test_find_agent_mentions_matches_known_names_only(ctx: Ctx):
    agents = {"reviewer": None, "Explore": None}
    names = find_agent_mentions("please ask @agent-reviewer and @agent-nonexistent to look", agents)
    ctx.check("only known name matched", names == ["reviewer"])


@test
def test_find_agent_mentions_dedupes_preserving_order(ctx: Ctx):
    agents = {"reviewer": None, "Explore": None}
    names = find_agent_mentions("@agent-Explore then @agent-reviewer then @agent-Explore again", agents)
    ctx.check("de-duped, first-seen order", names == ["Explore", "reviewer"])


@test
def test_find_agent_mentions_none_found(ctx: Ctx):
    ctx.check("no mentions -> []", find_agent_mentions("just a normal prompt", {"reviewer": None}) == [])


@test
def test_agent_mention_instruction_empty(ctx: Ctx):
    ctx.check("empty list -> empty string", agent_mention_instruction([]) == "")


@test
def test_agent_mention_instruction_single(ctx: Ctx):
    text = agent_mention_instruction(["reviewer"])
    ctx.check("mentions subagent_type", 'subagent_type="reviewer"' in text)


@test
def test_agent_mention_instruction_multiple(ctx: Ctx):
    text = agent_mention_instruction(["reviewer", "Explore"])
    ctx.check("both names present", '"reviewer"' in text and '"Explore"' in text)


if __name__ == "__main__":
    ctx = Ctx()
    results, passed, failed, skipped = run_all(TESTS, ctx)
    sys.exit(print_results(results, passed, failed, skipped))
