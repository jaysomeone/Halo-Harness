"""tests.test_skill_tool -- H4 scope C: the real Skill tool (tools/skill.py)
+ commands/skills.py's `discover_all_skills`/`find_skill` refactor. Every
test builds its own throwaway `~/.claude/skills` (or `<proj>/.claude/skills`)
tree under a tempdir -- never the real machine's.
"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.helpers.runner import Ctx, new_registry, print_results, run_all
from tests.helpers.fake_home import add_fake_skill
from halo_harness.commands.skills import discover_all_skills, find_skill
from halo_harness.tools.base import ToolContext
from halo_harness.tools.skill import SkillTool

test, TESTS = new_registry()


def _project_skills_setup(**skill_kwargs):
    root = Path(tempfile.mkdtemp(prefix="halo-skilltool-"))
    proj = root / "proj"
    home = root / "home"
    (proj / ".claude").mkdir(parents=True, exist_ok=True)
    (home / ".claude").mkdir(parents=True, exist_ok=True)
    add_fake_skill(proj / ".claude" / "skills", **skill_kwargs)
    return proj, home


@test
def test_find_skill_resolves_project_scoped_skill(ctx: Ctx):
    proj, home = _project_skills_setup()
    cmd = find_skill("deploy", proj, home=home)
    ctx.check("skill found", cmd is not None)
    ctx.check("kind is prompt", cmd.kind == "prompt")
    ctx.check("source is skill", cmd.source == "skill")


@test
def test_discover_all_skills_returns_dict_keyed_by_name(ctx: Ctx):
    proj, home = _project_skills_setup()
    skills = discover_all_skills(proj, home=home)
    ctx.check(f"deploy present, got {list(skills)}", "deploy" in skills)


@test
def test_skill_tool_runs_and_substitutes_arguments(ctx: Ctx):
    proj, home = _project_skills_setup()
    tool = SkillTool()
    ctx_obj = ToolContext(cwd=proj)
    # patch home resolution via BRIDGE_TEST_HOME so find_skill (which calls
    # config.paths.home() when `home=` isn't threaded through ToolContext)
    # resolves the SAME fake home -- tools/skill.py calls find_skill(name, cwd)
    # with no explicit home, so it falls back to config.paths.home().
    import os
    old = os.environ.get("BRIDGE_TEST_HOME")
    os.environ["BRIDGE_TEST_HOME"] = str(home)
    try:
        result = tool.run({"skill": "deploy", "args": "staging v2"}, ctx_obj)
    finally:
        if old is None:
            os.environ.pop("BRIDGE_TEST_HOME", None)
        else:
            os.environ["BRIDGE_TEST_HOME"] = old
    ctx.check(f"not an error, got {result.content!r}", not result.is_error)
    ctx.check(f"$0 substituted, got {result.content!r}", "Deploy environment staging" in result.content)
    ctx.check(f"$1 substituted, got {result.content!r}", "at version v2" in result.content)
    ctx.check(f"$ARGUMENTS substituted, got {result.content!r}", "All args: staging v2" in result.content)
    ctx.check(f"!`echo pre` pre-executed (allowed-tools grants it), got {result.content!r}",
               "Pre-exec output: pre" in result.content)


@test
def test_skill_tool_unknown_skill_is_error(ctx: Ctx):
    proj, home = _project_skills_setup()
    tool = SkillTool()
    import os
    os.environ["BRIDGE_TEST_HOME"] = str(home)
    try:
        result = tool.run({"skill": "does-not-exist"}, ToolContext(cwd=proj))
    finally:
        os.environ.pop("BRIDGE_TEST_HOME", None)
    ctx.check("unknown skill is an error result", result.is_error)


@test
def test_skill_tool_missing_skill_param_is_error(ctx: Ctx):
    tool = SkillTool()
    result = tool.run({}, ToolContext(cwd=Path(".")))
    ctx.check("missing skill param is an error", result.is_error)


@test
def test_skill_tool_disable_model_invocation_hides_from_tool_not_registry(ctx: Ctx):
    proj, home = _project_skills_setup(name="hidden", frontmatter_extra={"disable-model-invocation": True})
    # still discoverable (the `/` slash surface can still see it)
    cmd = find_skill("hidden", proj, home=home)
    ctx.check("still discoverable by find_skill", cmd is not None)
    ctx.check("model_invocable is False", cmd.model_invocable is False)

    tool = SkillTool()
    import os
    os.environ["BRIDGE_TEST_HOME"] = str(home)
    try:
        result = tool.run({"skill": "hidden"}, ToolContext(cwd=proj))
    finally:
        os.environ.pop("BRIDGE_TEST_HOME", None)
    ctx.check(f"tool call errors, got {result.content!r}", result.is_error)
    ctx.check("error names disable-model-invocation", "disable-model-invocation" in result.content)


@test
def test_skill_tool_context_fork_is_deferred_error(ctx: Ctx):
    proj, home = _project_skills_setup(name="forked", frontmatter_extra={"context": "fork"})
    tool = SkillTool()
    import os
    os.environ["BRIDGE_TEST_HOME"] = str(home)
    try:
        result = tool.run({"skill": "forked"}, ToolContext(cwd=proj))
    finally:
        os.environ.pop("BRIDGE_TEST_HOME", None)
    ctx.check(f"deferred error, got {result.content!r}", result.is_error and "deferred" in result.content.lower())


@test
def test_skill_tool_lists_sibling_files(ctx: Ctx):
    proj, home = _project_skills_setup()
    skill_dir = proj / ".claude" / "skills" / "deploy"
    (skill_dir / "scripts").mkdir(parents=True, exist_ok=True)
    (skill_dir / "scripts" / "run.sh").write_text("#!/bin/sh\necho hi\n", encoding="utf-8")
    (skill_dir / "template.txt").write_text("a template\n", encoding="utf-8")

    tool = SkillTool()
    import os
    os.environ["BRIDGE_TEST_HOME"] = str(home)
    try:
        result = tool.run({"skill": "deploy"}, ToolContext(cwd=proj))
    finally:
        os.environ.pop("BRIDGE_TEST_HOME", None)
    ctx.check(f"not an error, got {result.content!r}", not result.is_error)
    # finding 12 (h4-h5-h3c review): siblings are listed as ABSOLUTE paths
    # now, not cwd/skill-dir-relative ones.
    run_sh_abs = str((skill_dir / "scripts" / "run.sh").resolve())
    template_abs = str((skill_dir / "template.txt").resolve())
    ctx.check(f"scripts/run.sh listed as an absolute path, got {result.content!r}", run_sh_abs in result.content)
    ctx.check(f"template.txt listed as an absolute path, got {result.content!r}", template_abs in result.content)
    ctx.check(f"the base-directory line is present, got {result.content!r}",
              result.content.startswith(f"Base directory for this skill: {skill_dir.resolve()}"))


@test
def test_skill_tool_reports_allowed_tools(ctx: Ctx):
    proj, home = _project_skills_setup()
    tool = SkillTool()
    import os
    os.environ["BRIDGE_TEST_HOME"] = str(home)
    try:
        result = tool.run({"skill": "deploy"}, ToolContext(cwd=proj))
    finally:
        os.environ.pop("BRIDGE_TEST_HOME", None)
    ctx.check(f"allowed-tools echoed, got {result.content!r}", "Bash(echo pre)" in result.content)


@test
def test_skill_tool_summary_and_permission_content(ctx: Ctx):
    tool = SkillTool()
    ctx.check("summary", tool.summary({"skill": "deploy"}) == "Skill(deploy)")
    ctx.check("permission_content", tool.permission_content({"skill": "deploy"}) == "deploy")


@test
def test_h5b_f12_claude_skill_dir_and_session_id_and_project_dir_substituted(ctx: Ctx):
    """finding 12 (major, h4-h5-h3c review): `${CLAUDE_SKILL_DIR}`,
    `${CLAUDE_SESSION_ID}` and `${CLAUDE_PROJECT_DIR}` are substituted in
    the skill's own body -- a skill referencing its own scripts by
    `${CLAUDE_SKILL_DIR}/scripts/x.py` used to send the model to a
    literal, unresolved string."""
    root = Path(tempfile.mkdtemp(prefix="halo-skilltool-vars-"))
    proj = root / "proj"
    home = root / "home"
    (proj / ".claude").mkdir(parents=True, exist_ok=True)
    (home / ".claude").mkdir(parents=True, exist_ok=True)
    add_fake_skill(
        proj / ".claude" / "skills", name="varskill",
        body="Skill dir: ${CLAUDE_SKILL_DIR}\nSession: ${CLAUDE_SESSION_ID}\nProject: ${CLAUDE_PROJECT_DIR}\n",
    )
    skill_dir = (proj / ".claude" / "skills" / "varskill").resolve()

    import os
    os.environ["BRIDGE_TEST_HOME"] = str(home)
    try:
        ctx_obj = ToolContext(cwd=proj, session_dir=Path("/tmp/halo-sessions/proj-slug/abc123session"))
        result = SkillTool().run({"skill": "varskill"}, ctx_obj)
    finally:
        os.environ.pop("BRIDGE_TEST_HOME", None)
    ctx.check(f"not an error, got {result.content!r}", not result.is_error)
    ctx.check(f"CLAUDE_SKILL_DIR substituted to the real absolute dir, got {result.content!r}",
              f"Skill dir: {skill_dir}" in result.content)
    ctx.check(f"CLAUDE_SESSION_ID substituted from ctx.session_dir's basename, got {result.content!r}",
              "Session: abc123session" in result.content)
    ctx.check(f"CLAUDE_PROJECT_DIR substituted to cwd, got {result.content!r}",
              f"Project: {proj}" in result.content)
    ctx.check("no literal ${CLAUDE_...} placeholder survives", "${CLAUDE_" not in result.content)


@test
def test_h5b_f12_unresolved_var_left_untouched_when_no_session_dir(ctx: Ctx):
    """A variable this build genuinely can't resolve (no `session_dir` on
    a bare ToolContext, e.g. a unit test) is left as the literal
    `${CLAUDE_SESSION_ID}` text rather than silently becoming an empty
    string -- so a skill author can tell "not wired up" from "genuinely
    blank"."""
    proj, home = _project_skills_setup(body="Session: ${CLAUDE_SESSION_ID}\n")
    import os
    os.environ["BRIDGE_TEST_HOME"] = str(home)
    try:
        result = SkillTool().run({"skill": "deploy"}, ToolContext(cwd=proj))  # no session_dir at all
    finally:
        os.environ.pop("BRIDGE_TEST_HOME", None)
    ctx.check(f"left unsubstituted, got {result.content!r}", "${CLAUDE_SESSION_ID}" in result.content)


class _FakeSessionForSlash:
    """Just enough of a real `agent.loop.Session` for `commands/skills.py`'s
    `_make_run` to build the same `claude_vars` `tools/skill.py` does."""

    def __init__(self, *, session_id: str, effort: str = "high"):
        class _Log:
            pass

        self.log = _Log()
        self.log.session_id = session_id
        self.effort = effort
        self.permission_engine = None
        self.tool_env = {}


class _FakeFacadeForSlash:
    def __init__(self, cwd: Path, session):
        self.cwd = cwd
        self.session = session


@test
def test_h5c_f15_slash_skill_path_builds_the_same_claude_vars_base_dir_and_siblings(ctx: Ctx):
    """H5c finding 15: `/skill-name` (`commands/skills.py`'s `_make_run`)
    used to build NONE of what the model-invoked Skill TOOL gives a skill
    -- no `claude_vars` substitution (`${CLAUDE_SKILL_DIR}` reached a
    `` !`...` `` pre-exec as a literal, empty-expanding string), no "Base
    directory for this skill:" line, no sibling-file listing. Drives the
    REAL `SlashCommand.run` a `/varskill` invocation would call (via
    `discover_all_skills`, never `SkillTool` at all) and asserts it now
    matches the tool path's own three behaviors exactly."""
    root = Path(tempfile.mkdtemp(prefix="halo-slash-skill-vars-"))
    proj = root / "proj"
    home = root / "home"
    (proj / ".claude").mkdir(parents=True, exist_ok=True)
    (home / ".claude").mkdir(parents=True, exist_ok=True)
    add_fake_skill(
        proj / ".claude" / "skills", name="varskill",
        body="Skill dir: ${CLAUDE_SKILL_DIR}\nSession: ${CLAUDE_SESSION_ID}\nProject: ${CLAUDE_PROJECT_DIR}\n",
    )
    skill_dir = (proj / ".claude" / "skills" / "varskill").resolve()
    (skill_dir / "scripts").mkdir(parents=True, exist_ok=True)
    (skill_dir / "scripts" / "run.sh").write_text("#!/bin/sh\necho hi\n", encoding="utf-8")

    skills = discover_all_skills(proj, home=home)
    cmd = skills.get("varskill")
    ctx.check(f"the skill is discovered as a slash command, got {list(skills)}", cmd is not None)

    facade = _FakeFacadeForSlash(proj, _FakeSessionForSlash(session_id="abc123session"))
    text = cmd.run("", facade)

    ctx.check(f"CLAUDE_SKILL_DIR substituted to the real absolute dir, got {text!r}",
              f"Skill dir: {skill_dir}" in text)
    ctx.check(f"CLAUDE_SESSION_ID substituted from the real session's log.session_id, got {text!r}",
              "Session: abc123session" in text)
    ctx.check(f"CLAUDE_PROJECT_DIR substituted to cwd, got {text!r}", f"Project: {proj}" in text)
    ctx.check("no literal ${CLAUDE_...} placeholder survives", "${CLAUDE_" not in text)
    ctx.check(f"the base-directory line is present and FIRST, got {text!r}",
              text.startswith(f"Base directory for this skill: {skill_dir}"))
    run_sh_abs = str((skill_dir / "scripts" / "run.sh").resolve())
    ctx.check(f"the sibling script is listed as an absolute path, got {text!r}", run_sh_abs in text)


if __name__ == "__main__":
    ctx = Ctx()
    results, passed, failed, skipped = run_all(TESTS, ctx)
    sys.exit(print_results(results, passed, failed, skipped))
