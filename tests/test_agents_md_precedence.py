"""tests.test_agents_md_precedence -- config/agents_md.py: discovery
precedence (managed > --agents > project walk-up nearest-wins > user >
plugin > built-ins), `parse_agents_json`, `Agent(type)` spawn restriction,
and `resolve_agent_model`'s chain.
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.helpers.runner import Ctx, new_registry, print_results, run_all
from rolo_claude.config import agents_md as am

test, TESTS = new_registry()


def _write_agent(agents_dir: Path, filename: str, *, name: str, description: str = "d") -> Path:
    path = agents_dir / filename
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\nname: {name}\ndescription: {description}\n---\nbody\n", encoding="utf-8")
    return path


@test
def test_project_beats_user(ctx: Ctx):
    root = Path(tempfile.mkdtemp(prefix="am-prec-"))
    proj = root / "proj"
    user_home = root / "home"
    proj.mkdir(parents=True, exist_ok=True)
    _write_agent(proj / ".claude" / "agents", "reviewer.md", name="reviewer", description="project version")
    _write_agent(user_home / ".claude" / "agents", "reviewer.md", name="reviewer", description="user version")
    specs = am.discover_agents(proj, settings=None)
    # discover_agents' own `home()` call reads real HOME unless BRIDGE_TEST_HOME
    # is set -- point it at our fixture so the "user" tier is actually THIS one.
    import os
    old = os.environ.get("BRIDGE_TEST_HOME")
    try:
        os.environ["BRIDGE_TEST_HOME"] = str(user_home)
        specs = am.discover_agents(proj, settings=None)
    finally:
        if old is None:
            os.environ.pop("BRIDGE_TEST_HOME", None)
        else:
            os.environ["BRIDGE_TEST_HOME"] = old
    ctx.check("project entry wins over same-named user entry", specs["reviewer"].description == "project version")


@test
def test_nearest_project_directory_wins(ctx: Ctx):
    root = Path(tempfile.mkdtemp(prefix="am-prec-"))
    far = root / "far"
    near = far / "near"
    near.mkdir(parents=True, exist_ok=True)
    _write_agent(far / ".claude" / "agents", "scout.md", name="scout", description="far ancestor")
    _write_agent(near / ".claude" / "agents", "scout.md", name="scout", description="nearest directory")
    specs = am.discover_agents(near, settings=None)
    ctx.check("nearest directory's definition wins", specs["scout"].description == "nearest directory")


@test
def test_agents_flag_beats_project_files(ctx: Ctx):
    root = Path(tempfile.mkdtemp(prefix="am-prec-"))
    proj = root / "proj"
    _write_agent(proj / ".claude" / "agents", "reviewer.md", name="reviewer", description="from a file")
    flag_json = json.dumps({"reviewer": {"description": "from --agents", "prompt": "p"}})
    specs = am.discover_agents(proj, settings=None, agents_flag=flag_json)
    ctx.check("--agents JSON overrides a project file of the same name", specs["reviewer"].description == "from --agents")


@test
def test_custom_agent_can_override_a_builtin_name(ctx: Ctx):
    root = Path(tempfile.mkdtemp(prefix="am-prec-"))
    proj = root / "proj"
    _write_agent(proj / ".claude" / "agents", "explore.md", name="Explore", description="custom Explore override")
    specs = am.discover_agents(proj, settings=None)
    ctx.check("a project agent named 'Explore' overrides the built-in", specs["Explore"].description == "custom Explore override")


@test
def test_parse_agents_json_direct_string_and_file(ctx: Ctx):
    direct = am.parse_agents_json(json.dumps({"reviewer": {"description": "reviews code", "prompt": "Review it."}}))
    ctx.check("direct JSON string parses", direct["reviewer"].description == "reviews code")
    ctx.check("direct JSON prompt -> body", direct["reviewer"].body == "Review it.")

    root = Path(tempfile.mkdtemp(prefix="am-prec-"))
    json_file = root / "agents.json"
    json_file.write_text(json.dumps({"tester": {"description": "runs tests"}}), encoding="utf-8")
    from_file = am.parse_agents_json(str(json_file))
    ctx.check("a path to a JSON file is also accepted (print-mode form)", from_file["tester"].description == "runs tests")


@test
def test_parse_agents_json_malformed_is_empty_not_a_crash(ctx: Ctx):
    ctx.check("malformed JSON -> {}", am.parse_agents_json("{not valid json") == {})
    ctx.check("a JSON array (not an object) -> {}", am.parse_agents_json("[1, 2, 3]") == {})


@test
def test_agent_type_restriction_parsed_from_tools(ctx: Ctx):
    spec_restricted = am.AgentSpec(name="reviewer", description="d", tools=["Read", "Agent(Explore)", "Agent(Plan)"])
    ctx.check("Agent(name) entries collected", spec_restricted.allowed_subagent_types() == {"Explore", "Plan"})

    spec_bare = am.AgentSpec(name="unrestricted", description="d", tools=["Read", "Agent"])
    ctx.check("a bare 'Agent' entry means no restriction", spec_bare.allowed_subagent_types() is None)

    spec_none = am.AgentSpec(name="no-tools-key", description="d", tools=None)
    ctx.check("tools=None -> no restriction recorded", spec_none.allowed_subagent_types() is None)


@test
def test_resolved_tools_never_grows_the_parent_catalog(ctx: Ctx):
    spec = am.AgentSpec(name="x", description="d", tools=["Read", "Bash", "SomethingNotInCatalog"])
    resolved = spec.resolved_tools(["Read", "Bash", "Write"])
    ctx.check("only names ALSO in the parent catalog survive", set(resolved) == {"Read", "Bash"})
    ctx.check("a tool the parent doesn't have is never added", "SomethingNotInCatalog" not in resolved)


# ---- resolve_agent_model -------------------------------------------------

class _FakeModelRef:
    def __init__(self, raw):
        self.raw = raw


class _FakeModelProfile:
    pass


@test
def test_resolve_agent_model_invocation_beats_frontmatter(ctx: Ctx):
    from rolo_claude.model import parse_model_ref, resolve_model_profile  # noqa: F401 (sanity import)

    parent_ref, parent_profile = _FakeModelRef("or:parent/model"), _FakeModelProfile()
    ref, profile = am.resolve_agent_model(
        invocation_model="inherit", frontmatter_model="or:frontmatter/model",
        parent_ref=parent_ref, parent_profile=parent_profile, state_dir=Path(tempfile.mkdtemp()),
    )
    ctx.check("invocation 'inherit' wins outright and reuses the parent ref object", ref is parent_ref)


@test
def test_resolve_agent_model_haiku_maps_to_small_model(ctx: Ctx):
    parent_ref, parent_profile = _FakeModelRef("or:parent/model"), _FakeModelProfile()
    small_ref, small_profile = _FakeModelRef("or:small/model"), _FakeModelProfile()
    ref, profile = am.resolve_agent_model(
        frontmatter_model="haiku", parent_ref=parent_ref, parent_profile=parent_profile,
        parent_small_ref=small_ref, parent_small_profile=small_profile, state_dir=Path(tempfile.mkdtemp()),
    )
    ctx.check("'haiku' resolves to the parent's SMALL model ref", ref is small_ref)
    ctx.check("'haiku' with no small model falls back to the parent", True)  # covered by the next case

    ref2, _ = am.resolve_agent_model(
        frontmatter_model="haiku", parent_ref=parent_ref, parent_profile=parent_profile,
        parent_small_ref=None, state_dir=Path(tempfile.mkdtemp()),
    )
    ctx.check("'haiku' with NO small model configured falls back to parent", ref2 is parent_ref)


@test
def test_resolve_agent_model_env_beats_nothing_else_given(ctx: Ctx):
    parent_ref, parent_profile = _FakeModelRef("or:parent/model"), _FakeModelProfile()
    ref, _ = am.resolve_agent_model(
        env={"CLAUDE_CODE_SUBAGENT_MODEL": "inherit"},
        parent_ref=parent_ref, parent_profile=parent_profile, state_dir=Path(tempfile.mkdtemp()),
    )
    ctx.check("env var consulted when invocation/frontmatter give nothing", ref is parent_ref)


@test
def test_resolve_agent_model_default_falls_back_to_parent(ctx: Ctx):
    parent_ref, parent_profile = _FakeModelRef("or:parent/model"), _FakeModelProfile()
    ref, profile = am.resolve_agent_model(parent_ref=parent_ref, parent_profile=parent_profile,
                                           state_dir=Path(tempfile.mkdtemp()))
    ctx.check("nothing given at all -> parent ref/profile reused verbatim",
              ref is parent_ref and profile is parent_profile)


if __name__ == "__main__":
    ctx = Ctx()
    results, passed, failed, skipped = run_all(TESTS, ctx)
    sys.exit(print_results(results, passed, failed, skipped))
