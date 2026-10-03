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
import os
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


# ---- W5 (carried from W4a): tracked files a command modified -------------

@test
def test_git_status_dirty_paths_reports_untracked_and_modified_tracked(ctx: Ctx):
    from halo_harness.shadow import git_status_dirty_paths

    repo = Path(tempfile.mkdtemp(prefix="w5-gitdirty-"))
    subprocess.run(["git", "init", "-q"], cwd=str(repo), check=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=str(repo), check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=str(repo), check=True)
    tracked = repo / "tracked.txt"
    tracked.write_text("v1", encoding="utf-8")
    subprocess.run(["git", "add", "tracked.txt"], cwd=str(repo), check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=str(repo), check=True)

    clean = git_status_dirty_paths(repo)
    ctx.check(f"freshly committed repo has nothing dirty, got {clean}", clean == {})

    tracked.write_text("v2", encoding="utf-8")
    (repo / "untracked.txt").write_text("new", encoding="utf-8")
    dirty = git_status_dirty_paths(repo)
    ctx.check(f"the modified TRACKED file is reported, got {dirty}",
              dirty.get(str(tracked.resolve())) in ("M ", " M", "MM"))
    ctx.check(f"the untracked file is ALSO reported, got {dirty}",
              dirty.get(str((repo / "untracked.txt").resolve())) == "??")


@test
def test_bash_shadow_captures_both_a_new_file_and_a_modified_tracked_file(ctx: Ctx):
    """The actual W5 fix, end to end through tui/dispatch.py's own Bash
    shadow-copy pair: a command that both creates a file AND modifies an
    already-tracked one gets BOTH captured into the same shadow step, but
    only the brand-new one is marked `created` (so a later undo restores
    the tracked file's PRIOR content instead of deleting it)."""
    from halo_harness.shadow import ShadowStore, git_status_dirty_paths
    from halo_harness.tui.dispatch import _maybe_record_bash_shadow_step

    class _FakeController:
        pass

    class _FakeApp:
        def __init__(self, cwd, shadow_dir):
            self.cwd = cwd
            self.controller = _FakeController()
            self.controller.shadow_dir = shadow_dir

        def run_worker(self, fn, thread=False, name=None):
            fn()  # synchronous in this test -- no real Textual event loop

    repo = Path(tempfile.mkdtemp(prefix="w5-bashshadow-"))
    subprocess.run(["git", "init", "-q"], cwd=str(repo), check=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=str(repo), check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=str(repo), check=True)
    tracked = repo / "tracked.txt"
    tracked.write_text("v1", encoding="utf-8")
    subprocess.run(["git", "add", "tracked.txt"], cwd=str(repo), check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=str(repo), check=True)

    shadow_dir = Path(tempfile.mkdtemp(prefix="w5-bashshadow-store-"))
    app = _FakeApp(repo, shadow_dir)

    # finding 9 (W6a): `before` is now taken synchronously by the CALLER
    # (agent/loop.py, on the session worker, right before the real command
    # runs) and handed to `_maybe_record_bash_shadow_step` directly --
    # simulated here with a plain direct call, no separate worker/race.
    before = git_status_dirty_paths(repo)
    # The "Bash command" this step represents: modifies the tracked file
    # AND creates a new one, exactly the combination v1 (untracked-only)
    # could never fully capture.
    tracked.write_text("v2", encoding="utf-8")
    new_file = repo / "brand_new.txt"
    new_file.write_text("hi", encoding="utf-8")
    _maybe_record_bash_shadow_step(app, "Bash", {"command": "echo"}, True, before)

    store = ShadowStore(shadow_dir)
    ctx.check(f"exactly one shadow step was recorded, got {store.steps}", len(store.steps) == 1)
    step = store.steps[0]
    ctx.check(f"the MODIFIED tracked file is captured, got {step['files']}",
              str(tracked.resolve()) in step["files"])
    ctx.check(f"the brand-new file is ALSO captured, got {step['files']}",
              str(new_file.resolve()) in step["files"])
    ctx.check(f"only the brand-new file is marked created, got {step['created']}",
              step["created"] == [str(new_file.resolve())])

    # /rewind actually restores the tracked file's PRIOR content (v2, this
    # step's own snapshot), not just deletes it -- the whole point of
    # capturing it at all. `rewind_to` (not `undo`, which needs a step
    # BEFORE the cursor and there's only this one) re-applies this exact
    # step's tree directly.
    tracked.write_text("v3 (further edited after the shadow step)", encoding="utf-8")
    result = store.rewind_to(step["id"])
    ctx.check(f"rewind restored tracked.txt's own v2 content, got {tracked.read_text(encoding='utf-8')!r}",
              result is not None and tracked.read_text(encoding="utf-8") == "v2")


@test
def test_f9_w6a_git_status_dirty_paths_from_a_subdirectory_resolves_correctly(ctx: Ctx):
    """finding 9 (W6a): `git status --porcelain` paths are relative to the
    REPO ROOT, never to `cwd` -- joining straight to `cwd` (the old code)
    doubled the leading segment for any subdirectory cwd (`sub/new.txt`
    under `cwd=<repo>/sub` became `<repo>/sub/sub/new.txt`), so nothing a
    command created from a subdirectory was ever actually found."""
    from halo_harness.shadow import git_status_dirty_paths

    repo = Path(tempfile.mkdtemp(prefix="w6a-f9-repo-"))
    subprocess.run(["git", "init", "-q"], cwd=str(repo), check=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=str(repo), check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=str(repo), check=True)
    (repo / "README.md").write_text("hi\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=str(repo), check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=str(repo), check=True)
    sub = repo / "sub"
    sub.mkdir()
    (sub / "new.txt").write_text("hi", encoding="utf-8")

    dirty = git_status_dirty_paths(sub)
    expected = str((sub / "new.txt").resolve())
    ctx.check(f"the real path is reported (not doubled), got {dirty}", dirty.get(expected) == "??")
    ctx.check(f"no doubled sub{os.sep}sub segment leaked into any reported path, got {dirty}",
              not any((f"sub{os.sep}sub") in p for p in dirty))


@test
def test_f8_w6a_bash_shadow_round_trips_a_binary_file_byte_exact(ctx: Ctx):
    """finding 8 (W6a): a Bash command that writes a BINARY file (a PNG
    here) must round-trip byte-exact through the shadow store -- reading
    it as text with `errors="replace"` (the old, unconditional behavior)
    mangled it before `record_step` ever got a chance to store it right,
    and `rewind_to` used to re-mangle it a second time on the way back out
    even if the first mangling were somehow fixed."""
    from halo_harness.shadow import ShadowStore, git_status_dirty_paths
    from halo_harness.tui.dispatch import _maybe_record_bash_shadow_step

    class _FakeController:
        pass

    class _FakeApp:
        def __init__(self, cwd, shadow_dir):
            self.cwd = cwd
            self.controller = _FakeController()
            self.controller.shadow_dir = shadow_dir

        def run_worker(self, fn, thread=False, name=None):
            fn()

    repo = Path(tempfile.mkdtemp(prefix="w6a-f8-repo-"))
    subprocess.run(["git", "init", "-q"], cwd=str(repo), check=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=str(repo), check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=str(repo), check=True)
    (repo / "README.md").write_text("hi\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=str(repo), check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=str(repo), check=True)

    shadow_dir = Path(tempfile.mkdtemp(prefix="w6a-f8-store-"))
    app = _FakeApp(repo, shadow_dir)

    before = git_status_dirty_paths(repo)
    png = repo / "image.png"
    # A tiny but genuinely binary payload: a PNG signature plus NUL and
    # high bytes that are NOT valid UTF-8 on their own.
    original_bytes = b"\x89PNG\r\n\x1a\n" + bytes(range(256)) + b"\x00\xff\xfe\x00more binary data"
    png.write_bytes(original_bytes)
    _maybe_record_bash_shadow_step(app, "Bash", {"command": "echo"}, True, before)

    store = ShadowStore(shadow_dir)
    ctx.check(f"exactly one shadow step was recorded, got {store.steps}", len(store.steps) == 1)
    step = store.steps[0]
    ctx.check(f"the binary file is captured, got {step['files']}", str(png.resolve()) in step["files"])

    png.write_bytes(b"corrupted after the shadow step")
    result = store.rewind_to(step["id"])
    ctx.check(f"rewind restored the ORIGINAL bytes exactly, got {len(png.read_bytes())} bytes "
              f"(wanted {len(original_bytes)})", result is not None and png.read_bytes() == original_bytes)


@test
def test_f8_w6a_bash_shadow_round_trips_crlf_text_without_doubling_it(ctx: Ctx):
    """Regression pin for a real bug this round's own first attempt at
    finding 8 introduced: a text file with NATIVE CRLF line endings (an
    ordinary file on Windows, or any file that just happens to use them)
    read via `read_bytes()` (never `read_text()`, so a BINARY file round-
    trips byte-exact too) carries a literal "\\r\\n" in the resulting
    string -- writing that back out through `write_text` (which itself
    translates every bare "\\n" to "\\r\\n" on Windows) doubled the "\\r"
    ("\\r\\n" -> "\\r\\r\\n"). The whole pipeline must stay byte-exact
    with NO text-mode translation anywhere in it."""
    from halo_harness.shadow import ShadowStore, git_status_dirty_paths
    from halo_harness.tui.dispatch import _maybe_record_bash_shadow_step

    class _FakeController:
        pass

    class _FakeApp:
        def __init__(self, cwd, shadow_dir):
            self.cwd = cwd
            self.controller = _FakeController()
            self.controller.shadow_dir = shadow_dir

        def run_worker(self, fn, thread=False, name=None):
            fn()

    repo = Path(tempfile.mkdtemp(prefix="w6a-f8-crlf-repo-"))
    subprocess.run(["git", "init", "-q"], cwd=str(repo), check=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=str(repo), check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=str(repo), check=True)
    (repo / "README.md").write_text("hi\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=str(repo), check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=str(repo), check=True)

    shadow_dir = Path(tempfile.mkdtemp(prefix="w6a-f8-crlf-store-"))
    app = _FakeApp(repo, shadow_dir)

    before = git_status_dirty_paths(repo)
    doc = repo / "doc.txt"
    original_bytes = b"line one\r\nline two\r\n"  # NATIVE CRLF, written raw (never via write_text)
    doc.write_bytes(original_bytes)
    _maybe_record_bash_shadow_step(app, "Bash", {"command": "echo"}, True, before)

    store = ShadowStore(shadow_dir)
    ctx.check(f"exactly one shadow step was recorded, got {store.steps}", len(store.steps) == 1)
    step = store.steps[0]

    doc.write_bytes(b"corrupted after the shadow step")
    result = store.rewind_to(step["id"])
    ctx.check(f"rewind restored the ORIGINAL CRLF bytes exactly (never doubled), got {doc.read_bytes()!r} "
              f"(wanted {original_bytes!r})", result is not None and doc.read_bytes() == original_bytes)


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
