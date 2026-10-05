"""tests.test_w4a_hooks_b -- W4a item 1, part 2: Setup/DirectoryAdded/
InstructionsLoaded (all three fire together at session startup),
ConfigChange, UserPromptExpansion and WorktreeCreated. Companion to
test_w4a_hooks.py (split to stay under the house 250-line-per-file
guideline) -- same `dump_payload` hook-script pattern, a couple of these
drive the relevant `Session._fire_*`/`fire_user_prompt_expansion` method
directly rather than a full mock-upstream turn, where that is the simpler,
still-real way to pin the hook's own payload/trigger logic.
"""
import json
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.helpers.runner import Ctx, new_registry, print_results, run_all
from tests.helpers.provider_env_defaults import ensure_default_provider_credentials

ensure_default_provider_credentials()
REPO_DIR = Path(__file__).resolve().parent.parent
_HOOK_SCRIPTS_PATH = str(REPO_DIR / "tests" / "helpers" / "hook_scripts.py")

test, TESTS = new_registry()


def _dump_lines(dump_file: Path) -> list:
    if not dump_file.exists():
        return []
    out = []
    for line in dump_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                out.append(json.loads(line))
            except ValueError:
                pass
    return out


def _events_named(lines: list, name: str) -> list:
    return [L for L in lines if L.get("hook_event_name") == name]


def _dump_hook_runner(dump_file: Path, events: "list[str]", *, cwd):
    from halo_harness.hooks import HookDef, HookRunner
    env = dict(os.environ)
    env["HOOK_DUMP_FILE"] = str(dump_file)
    hooks_by_event = {ev: [HookDef(type="command", args=[sys.executable, _HOOK_SCRIPTS_PATH, "dump_payload"])]
                       for ev in events}
    return HookRunner(hooks_by_event, cwd=cwd, session_id="w4a-sess-b", transcript_path="t.jsonl", effective_env=env)


def _bare_session(cwd, hook_runner, *, extra_dirs=None):
    from halo_harness.agent.assemble import SessionContext
    from halo_harness.agent.loop import Session
    from halo_harness.model import ModelProfile, parse_model_ref
    from halo_harness.permissions import PermissionEngine
    os.environ["BRIDGE_TEST_HOME"] = str(Path(tempfile.mkdtemp(prefix="w4a-hooksb-home-")))
    ctx = SessionContext(cwd=cwd, model_label="or:mock/w4a-b", bare=True)
    engine = PermissionEngine(mode="auto", cwd=cwd, extra_dirs=extra_dirs or [])
    return Session(cwd=cwd, model_ref=parse_model_ref("or:mock/w4a-b"), model_profile=ModelProfile(), creds=None,
                   state_dir=Path(tempfile.mkdtemp(prefix="w4a-hooksb-state-")), model_label="or:mock/w4a-b",
                   session_context=ctx, max_turns=5, permission_engine=engine, agents={}, routes={},
                   hook_runner=hook_runner)


@test
def test_setup_directoryadded_instructionsloaded_fire_at_startup(ctx: Ctx):
    """All three fire from `Session.__init__`'s own "startup" branch -- a
    NON-bare session (a real CLAUDE.md on disk) with an extra trusted dir."""
    from halo_harness.agent.assemble import SessionContext
    from halo_harness.agent.loop import Session
    from halo_harness.model import ModelProfile, parse_model_ref
    from halo_harness.permissions import PermissionEngine

    dump = Path(tempfile.mkdtemp(prefix="w4a-dump-")) / "dump.jsonl"
    cwd = Path(tempfile.mkdtemp(prefix="w4a-cwd-"))
    (cwd / "CLAUDE.md").write_text("# project notes\nUse tabs.", encoding="utf-8")
    extra = Path(tempfile.mkdtemp(prefix="w4a-extra-"))
    hook_runner = _dump_hook_runner(dump, ["Setup", "DirectoryAdded", "InstructionsLoaded"], cwd=cwd)
    os.environ["BRIDGE_TEST_HOME"] = str(Path(tempfile.mkdtemp(prefix="w4a-hooksb-home2-")))
    session_ctx = SessionContext(cwd=cwd, model_label="or:mock/w4a-b", bare=False)
    engine = PermissionEngine(mode="auto", cwd=cwd, extra_dirs=[extra])
    Session(cwd=cwd, model_ref=parse_model_ref("or:mock/w4a-b"), model_profile=ModelProfile(), creds=None,
            state_dir=Path(tempfile.mkdtemp(prefix="w4a-hooksb-state2-")), model_label="or:mock/w4a-b",
            session_context=session_ctx, max_turns=5, permission_engine=engine, agents={}, routes={},
            hook_runner=hook_runner)

    lines = _dump_lines(dump)
    ctx.check(f"Setup fired once, got {len(_events_named(lines, 'Setup'))}",
              len(_events_named(lines, "Setup")) == 1)
    da = _events_named(lines, "DirectoryAdded")
    ctx.check(f"DirectoryAdded fired naming the extra dir, got {da}",
              da and str(extra) in da[0].get("path", ""))
    il = _events_named(lines, "InstructionsLoaded")
    ctx.check(f"InstructionsLoaded fired with a non-zero char_count, got {il}",
              il and il[0].get("char_count", 0) > 0)


@test
def test_review_finding_36_a_sub_agent_never_refires_setup_directoryadded_instructionsloaded(ctx: Ctx):
    """Release review finding 36: `_fire_session_start` already skips a
    sub-agent (`self.agent_id is not None`) outright -- Setup,
    DirectoryAdded and InstructionsLoaded did not, so spawning a child
    Session re-ran all three again, tagged to whichever session (main or
    child) happened to trigger each one. A child's own `__init__` must
    fire NONE of them."""
    from halo_harness.agent.assemble import SessionContext
    from halo_harness.agent.loop import Session
    from halo_harness.model import ModelProfile, parse_model_ref
    from halo_harness.permissions import PermissionEngine

    dump = Path(tempfile.mkdtemp(prefix="w4a-dump-child-")) / "dump.jsonl"
    cwd = Path(tempfile.mkdtemp(prefix="w4a-cwd-child-"))
    (cwd / "CLAUDE.md").write_text("# project notes\nUse tabs.", encoding="utf-8")
    extra = Path(tempfile.mkdtemp(prefix="w4a-extra-child-"))
    hook_runner = _dump_hook_runner(dump, ["Setup", "DirectoryAdded", "InstructionsLoaded"], cwd=cwd)
    os.environ["BRIDGE_TEST_HOME"] = str(Path(tempfile.mkdtemp(prefix="w4a-hooksb-home-child-")))
    session_ctx = SessionContext(cwd=cwd, model_label="or:mock/w4a-b", bare=False)
    engine = PermissionEngine(mode="auto", cwd=cwd, extra_dirs=[extra])
    Session(cwd=cwd, model_ref=parse_model_ref("or:mock/w4a-b"), model_profile=ModelProfile(), creds=None,
            state_dir=Path(tempfile.mkdtemp(prefix="w4a-hooksb-state-child-")), model_label="or:mock/w4a-b",
            session_context=session_ctx, max_turns=5, permission_engine=engine, agents={}, routes={},
            hook_runner=hook_runner, agent_id="child-1")

    lines = _dump_lines(dump)
    ctx.check(f"Setup never fires for a sub-agent, got {_events_named(lines, 'Setup')}",
              _events_named(lines, "Setup") == [])
    ctx.check(f"DirectoryAdded never fires for a sub-agent, got {_events_named(lines, 'DirectoryAdded')}",
              _events_named(lines, "DirectoryAdded") == [])
    ctx.check(f"InstructionsLoaded never fires for a sub-agent, got {_events_named(lines, 'InstructionsLoaded')}",
              _events_named(lines, "InstructionsLoaded") == [])


@test
def test_configchange_fires_when_a_settings_file_mtime_moves(ctx: Ctx):
    from halo_harness.config.settings import Settings, SettingsLayer

    dump = Path(tempfile.mkdtemp(prefix="w4a-dump-")) / "dump.jsonl"
    cwd = Path(tempfile.mkdtemp(prefix="w4a-cwd-"))
    settings_path = cwd / "settings.json"
    settings_path.write_text("{}", encoding="utf-8")
    hook_runner = _dump_hook_runner(dump, ["ConfigChange"], cwd=cwd)
    settings = Settings({}, [SettingsLayer(name="user", path=settings_path, base_dir=cwd, data={})], [])

    class _FakeCtx:
        pass
    fake_ctx = _FakeCtx()
    fake_ctx.settings = settings
    session = _bare_session(cwd, hook_runner)
    session.session_context = fake_ctx
    session._config_mtimes = session._snapshot_config_mtimes()

    time.sleep(0.05)
    settings_path.write_text('{"x": 1}', encoding="utf-8")
    os.utime(settings_path, (time.time() + 5, time.time() + 5))  # force a clearly-different mtime
    session._check_config_change()

    lines = _dump_lines(dump)
    cc = _events_named(lines, "ConfigChange")
    ctx.check(f"ConfigChange fired naming the changed file, got {cc}",
              cc and str(settings_path) in cc[0].get("path", ""))


@test
def test_userpromptexpansion_fires_only_when_the_text_actually_changed(ctx: Ctx):
    dump = Path(tempfile.mkdtemp(prefix="w4a-dump-")) / "dump.jsonl"
    cwd = Path(tempfile.mkdtemp(prefix="w4a-cwd-"))
    hook_runner = _dump_hook_runner(dump, ["UserPromptExpansion"], cwd=cwd)
    session = _bare_session(cwd, hook_runner)

    unchanged = session.fire_user_prompt_expansion("/foo", "/foo")
    ctx.check("no hook event for identical text", unchanged is None and _dump_lines(dump) == [])

    session.fire_user_prompt_expansion("/mycmd arg", "expanded body with arg substituted")
    lines = _dump_lines(dump)
    upe = _events_named(lines, "UserPromptExpansion")
    ctx.check(f"UserPromptExpansion fired once the text actually changed, got {upe}",
              upe and upe[0].get("original_prompt") == "/mycmd arg"
              and upe[0].get("prompt") == "expanded body with arg substituted")


@test
def test_worktreecreated_fires_when_cli_flags_carries_the_path(ctx: Ctx):
    dump = Path(tempfile.mkdtemp(prefix="w4a-dump-")) / "dump.jsonl"
    cwd = Path(tempfile.mkdtemp(prefix="w4a-cwd-"))
    hook_runner = _dump_hook_runner(dump, ["WorktreeCreated"], cwd=cwd)
    session = _bare_session(cwd, hook_runner)
    session.cli_flags = {"_worktree_created_path": str(cwd / "wt")}
    session._fire_worktree_created()
    lines = _dump_lines(dump)
    wc = _events_named(lines, "WorktreeCreated")
    ctx.check(f"WorktreeCreated fired naming the path, got {wc}",
              wc and wc[0].get("path") == str(cwd / "wt"))


if __name__ == "__main__":
    ctx = Ctx()
    results, passed, failed, skipped = run_all(TESTS, ctx)
    sys.exit(print_results(results, passed, failed, skipped))
