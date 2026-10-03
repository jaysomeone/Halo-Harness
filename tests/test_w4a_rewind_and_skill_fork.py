"""tests.test_w4a_rewind_and_skill_fork -- W4a items 3 and 5:
- `ShadowStore.record_step(..., created=[...])`/`rewind_to` actually
  DELETES a file created by a step once the cursor moves back past it
  (pure, no Session needed -- halo_harness.shadow's own API).
- `git_status_untracked_paths` (the Bash shadow-copy half) on a real git
  repo.
- `tools.skill.SkillTool` with `context: fork`/`agent` runs through the
  existing sub-agent machinery instead of the old "not implemented" error.
"""
import json
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.helpers.runner import Ctx, new_registry, print_results, run_all

test, TESTS = new_registry()


@test
def test_rewind_deletes_a_file_created_by_a_step_once_undone_past(ctx: Ctx):
    from halo_harness.shadow import ShadowStore

    session_dir = Path(tempfile.mkdtemp(prefix="w4a-shadow-"))
    store = ShadowStore(session_dir)
    real_dir = Path(tempfile.mkdtemp(prefix="w4a-shadow-real-"))
    existing = real_dir / "existing.txt"
    existing.write_text("v1", encoding="utf-8")
    new_file = real_dir / "brand_new.txt"

    step1 = store.record_step({str(existing): "v1"}, label="Edit(existing)", trigger="tool", created=[])
    ctx.check("step1 recorded", bool(step1))
    new_file.write_text("created by write", encoding="utf-8")
    step2 = store.record_step({str(new_file): "created by write"}, label="Write(new)", trigger="tool",
                               created=[str(new_file)])
    ctx.check(f"step2 marks brand_new.txt as created, got {step2.get('created')}",
              step2.get("created") == [str(new_file)])

    ctx.check("brand_new.txt exists on disk before undo", new_file.is_file())
    result = store.undo()
    ctx.check("undo returned a result", result is not None)
    ctx.check(f"brand_new.txt was deleted by undo, got deleted={result.get('deleted')}",
              str(new_file) in (result.get("deleted") or []) and not new_file.exists())
    ctx.check("existing.txt (tracked since step1) is untouched", existing.is_file() and existing.read_text() == "v1")

    redo_result = store.redo()
    ctx.check(f"redo recreates brand_new.txt (back to step2's own tree), got exists={new_file.exists()}",
              redo_result is not None and new_file.is_file() and new_file.read_text(encoding="utf-8") == "created by write")


@test
def test_git_status_untracked_paths_finds_a_new_file_in_a_real_repo(ctx: Ctx):
    from halo_harness.shadow import git_status_untracked_paths

    repo = Path(tempfile.mkdtemp(prefix="w4a-gitstatus-"))
    subprocess.run(["git", "init", "-q"], cwd=str(repo), check=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=str(repo), check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=str(repo), check=True)
    before = git_status_untracked_paths(repo)
    ctx.check(f"empty repo has no untracked paths, got {before}", before == set())

    new_file = repo / "new_from_bash.txt"
    new_file.write_text("hi", encoding="utf-8")
    after = git_status_untracked_paths(repo)
    ctx.check(f"the new file is reported untracked, got {after}", str(new_file.resolve()) in (after or set()))


@test
def test_git_status_untracked_paths_none_outside_a_repo(ctx: Ctx):
    from halo_harness.shadow import git_status_untracked_paths
    not_a_repo = Path(tempfile.mkdtemp(prefix="w4a-notrepo-"))
    ctx.check("None (the documented limit) for a non-git directory", git_status_untracked_paths(not_a_repo) is None)


@test
def test_skill_tool_context_fork_runs_through_a_subagent(ctx: Ctx):
    from halo_harness.agent.subagent import AgentRuntime
    from halo_harness.config.agents_md import AgentSpec
    from halo_harness.tools.base import ToolContext
    from halo_harness.tools.skill import SkillTool

    cwd = Path(tempfile.mkdtemp(prefix="w4a-skillfork-"))
    skills_dir = cwd / ".claude" / "skills" / "my-skill"
    skills_dir.mkdir(parents=True)
    (skills_dir / "SKILL.md").write_text(
        "---\nname: my-skill\ndescription: a test skill\ncontext: fork\n---\nDo the thing: $ARGUMENTS\n",
        encoding="utf-8",
    )

    captured = {}

    def _fake_run_agent_call(*, runtime, tool_id, tool_input, tool_name, on_event=None):
        captured["tool_input"] = tool_input
        from halo_harness.tools.base import ToolResult
        return [], ToolResult("fork ran with: " + tool_input["prompt"])

    import halo_harness.agent.subagent as subagent_mod
    original = subagent_mod.run_agent_call
    subagent_mod.run_agent_call = _fake_run_agent_call
    try:
        runtime = AgentRuntime(parent=None, agents={"general-purpose": AgentSpec(name="general-purpose", description="gp")})
        ctx_obj = ToolContext(cwd=cwd, agent_runtime=runtime, tool_use_id="toolu_1")
        result = SkillTool().run({"skill": "my-skill", "args": "hello"}, ctx_obj)
        ctx.check(f"result not an error, got {result.content!r}", not result.is_error)
        ctx.check(f"dispatched through run_agent_call with the expanded body, got {captured.get('tool_input')}",
                  captured.get("tool_input", {}).get("subagent_type") == "general-purpose"
                  and "hello" in captured.get("tool_input", {}).get("prompt", ""))
        ctx.check(f"tool result is the sub-agent's own result, got {result.content!r}",
                  result.content == "fork ran with: Do the thing: hello")
    finally:
        subagent_mod.run_agent_call = original


@test
def test_skill_tool_context_fork_without_agent_runtime_is_a_clean_error(ctx: Ctx):
    from halo_harness.tools.base import ToolContext
    from halo_harness.tools.skill import SkillTool

    cwd = Path(tempfile.mkdtemp(prefix="w4a-skillfork2-"))
    skills_dir = cwd / ".claude" / "skills" / "my-skill2"
    skills_dir.mkdir(parents=True)
    (skills_dir / "SKILL.md").write_text(
        "---\nname: my-skill2\ndescription: a test skill\ncontext: agent\n---\nBody text\n", encoding="utf-8")
    ctx_obj = ToolContext(cwd=cwd, agent_runtime=None)
    result = SkillTool().run({"skill": "my-skill2"}, ctx_obj)
    ctx.check(f"honest error naming the real constraint, got {result.content!r}",
              result.is_error and "sub-agents are not available" in result.content.lower())


if __name__ == "__main__":
    ctx = Ctx()
    results, passed, failed, skipped = run_all(TESTS, ctx)
    sys.exit(print_results(results, passed, failed, skipped))
