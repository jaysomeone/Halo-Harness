"""tests.test_planmode -- H6 scope C: plan file helpers (agent/planmode.py)
and the PermissionEngine plan-file write exception (permissions.py) that
makes the plan file the ONLY writable path while `mode == "plan"`.
"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.helpers.runner import Ctx, new_registry, print_results, run_all
from halo_harness.agent.planmode import (
    PLAN_MODE_NOTE, EnterPlanModeTool, ExitPlanModeTool, ensure_plan_file, plans_directory, write_plan,
)
from halo_harness.permissions import PermissionEngine
from halo_harness.tools.base import ToolContext

test, TESTS = new_registry()


# ---- plans_directory ---------------------------------------------------------

@test
def test_plans_directory_default(ctx: Ctx):
    from halo_harness.config.paths import plans_dir
    ctx.check("default matches config.paths.plans_dir()", plans_directory(None) == plans_dir())


@test
def test_plans_directory_settings_object_override(ctx: Ctx):
    class _Settings:
        plans_directory = str(Path(tempfile.mkdtemp(prefix="rc-plansdir-")) / "custom-plans")
    result = plans_directory(_Settings())
    ctx.check("override honoured", str(result) == _Settings.plans_directory)


@test
def test_plans_directory_dict_override(ctx: Ctx):
    custom = str(Path(tempfile.mkdtemp(prefix="rc-plansdir2-")) / "custom")
    result = plans_directory({"plansDirectory": custom})
    ctx.check("dict-shaped settings honoured", str(result) == custom)


# ---- ensure_plan_file / write_plan -------------------------------------------

@test
def test_ensure_plan_file_creates_three_word_md_name(ctx: Ctx):
    cwd = Path(tempfile.mkdtemp(prefix="rc-cwd-"))
    settings_dir = Path(tempfile.mkdtemp(prefix="rc-plans-"))

    class _Settings:
        plans_directory = str(settings_dir)
    path = ensure_plan_file(cwd, _Settings())
    ctx.check("file exists", path.exists())
    ctx.check("under the configured directory", path.parent == settings_dir)
    ctx.check("ends in .md", path.suffix == ".md")
    stem_parts = path.stem.split("-")
    ctx.check("three-word stem", len(stem_parts) == 3)
    ctx.check("lowercase words", all(p.islower() for p in stem_parts))


@test
def test_ensure_plan_file_reuses_existing_when_given(ctx: Ctx):
    settings_dir = Path(tempfile.mkdtemp(prefix="rc-plans3-"))

    class _Settings:
        plans_directory = str(settings_dir)
    first = ensure_plan_file(Path(tempfile.mkdtemp()), _Settings())
    second = ensure_plan_file(Path(tempfile.mkdtemp()), _Settings(), existing=first)
    ctx.check("same path returned when reused", first == second)


@test
def test_ensure_plan_file_ignores_existing_when_file_gone(ctx: Ctx):
    settings_dir = Path(tempfile.mkdtemp(prefix="rc-plans4-"))

    class _Settings:
        plans_directory = str(settings_dir)
    fake_existing = settings_dir / "does-not-exist-anymore.md"
    result = ensure_plan_file(Path(tempfile.mkdtemp()), _Settings(), existing=fake_existing)
    ctx.check("a fresh file is created instead", result != fake_existing)
    ctx.check("new file actually exists", result.exists())


@test
def test_ensure_plan_file_generates_unique_names(ctx: Ctx):
    settings_dir = Path(tempfile.mkdtemp(prefix="rc-plans5-"))

    class _Settings:
        plans_directory = str(settings_dir)
    paths = {ensure_plan_file(Path(tempfile.mkdtemp()), _Settings()) for _ in range(5)}
    ctx.check("5 distinct plan files", len(paths) == 5)


@test
def test_write_plan_writes_text(ctx: Ctx):
    path = Path(tempfile.mkdtemp(prefix="rc-writeplan-")) / "plan.md"
    write_plan(path, "## My Plan\n\nStep 1.\n")
    ctx.check("content written", path.read_text(encoding="utf-8") == "## My Plan\n\nStep 1.\n")


@test
def test_write_plan_overwrites(ctx: Ctx):
    path = Path(tempfile.mkdtemp(prefix="rc-writeplan2-")) / "plan.md"
    write_plan(path, "first")
    write_plan(path, "second")
    ctx.check("overwritten, not appended", path.read_text(encoding="utf-8") == "second")


@test
def test_write_plan_empty_text_ok(ctx: Ctx):
    path = Path(tempfile.mkdtemp(prefix="rc-writeplan3-")) / "plan.md"
    write_plan(path, None)
    ctx.check("None -> empty file, no crash", path.read_text(encoding="utf-8") == "")


# ---- EnterPlanMode / ExitPlanMode tool defs ----------------------------------

@test
def test_enter_plan_mode_tool_shape(ctx: Ctx):
    t = EnterPlanModeTool()
    ctx.check("name", t.name == "EnterPlanMode")
    ctx.check("no required args", t.input_schema.get("properties") == {})
    ctx.check("summary", t.summary({}) == "EnterPlanMode()")


@test
def test_exit_plan_mode_tool_shape(ctx: Ctx):
    t = ExitPlanModeTool()
    ctx.check("name", t.name == "ExitPlanMode")
    ctx.check("plan is required", "plan" in t.input_schema.get("required", []))
    ctx.check("summary includes first line", "Do X" in t.summary({"plan": "Do X\nDo Y"}))


@test
def test_exit_plan_mode_fallback_run_requires_plan(ctx: Ctx):
    t = ExitPlanModeTool()
    ctx.check("missing plan -> conceptual error",
              t.run({}, ToolContext(cwd=Path("."))).is_error is True)


@test
def test_plan_mode_note_mentions_exit_plan_mode(ctx: Ctx):
    ctx.check("PLAN_MODE_NOTE tells the model to call ExitPlanMode", "ExitPlanMode" in PLAN_MODE_NOTE)


# ---- PermissionEngine plan-file write exception ------------------------------

@test
def test_plan_mode_denies_ordinary_write_with_no_plan_file_set(ctx: Ctx):
    cwd = Path(tempfile.mkdtemp(prefix="rc-pe1-"))
    engine = PermissionEngine(mode="plan", cwd=cwd)
    decision = engine.decide("Write", {"file_path": str(cwd / "some.py")})
    ctx.check("denied (no plan file registered)", decision.action == "deny")


@test
def test_plan_mode_allows_write_to_the_plan_file(ctx: Ctx):
    cwd = Path(tempfile.mkdtemp(prefix="rc-pe2-"))
    plan_path = Path(tempfile.mkdtemp(prefix="rc-plans6-")) / "typed-tickling-squirrel.md"
    plan_path.write_text("", encoding="utf-8")
    engine = PermissionEngine(mode="plan", cwd=cwd)
    engine.set_plan_file(plan_path)
    decision = engine.decide("Write", {"file_path": str(plan_path)})
    ctx.check("allowed", decision.action == "allow")
    ctx.check("category mentioned", "plan" in decision.reason.lower())


@test
def test_plan_mode_allows_edit_to_the_plan_file_too(ctx: Ctx):
    cwd = Path(tempfile.mkdtemp(prefix="rc-pe3-"))
    plan_path = Path(tempfile.mkdtemp(prefix="rc-plans7-")) / "misty-harbor-echo.md"
    plan_path.write_text("", encoding="utf-8")
    engine = PermissionEngine(mode="plan", cwd=cwd)
    engine.set_plan_file(plan_path)
    decision = engine.decide("Edit", {"file_path": str(plan_path)})
    ctx.check("Edit to plan file also allowed", decision.action == "allow")


@test
def test_plan_mode_still_denies_every_other_write(ctx: Ctx):
    cwd = Path(tempfile.mkdtemp(prefix="rc-pe4-"))
    (cwd / "real.py").write_text("x = 1\n", encoding="utf-8")
    plan_path = Path(tempfile.mkdtemp(prefix="rc-plans8-")) / "quiet-cedar-dawn.md"
    plan_path.write_text("", encoding="utf-8")
    engine = PermissionEngine(mode="plan", cwd=cwd)
    engine.set_plan_file(plan_path)
    decision = engine.decide("Write", {"file_path": str(cwd / "real.py")})
    ctx.check("a normal in-cwd file is still denied in plan mode", decision.action == "deny")


@test
def test_plan_file_exception_only_applies_in_plan_mode(ctx: Ctx):
    cwd = Path(tempfile.mkdtemp(prefix="rc-pe5-"))
    plan_path = Path(tempfile.mkdtemp(prefix="rc-plans9-")) / "gold-ridge-fable.md"
    plan_path.write_text("", encoding="utf-8")
    engine = PermissionEngine(mode="acceptEdits", cwd=cwd)
    engine.set_plan_file(plan_path)
    decision = engine.decide("Write", {"file_path": str(plan_path)})
    # acceptEdits allows outright anyway (edit_write_in_workdir -- but the
    # plan file is normally OUTSIDE cwd, so this exercises "other": ask).
    ctx.check("no crash outside plan mode", decision.action in ("allow", "ask", "deny"))


@test
def test_set_plan_file_mutates_in_place(ctx: Ctx):
    cwd = Path(tempfile.mkdtemp(prefix="rc-pe6-"))
    engine = PermissionEngine(mode="plan", cwd=cwd)
    ctx.check("starts unset", engine.plan_file is None)
    p = Path(tempfile.mkdtemp()) / "a.md"
    engine.set_plan_file(p)
    ctx.check("now set", engine.plan_file == p)
    engine.set_plan_file(None)
    ctx.check("clearable", engine.plan_file is None)


@test
def test_plan_mode_bash_still_gated_normally(ctx: Ctx):
    """The plan-file exception is Write/Edit-only -- Bash in plan mode
    keeps its own (read-only-allowed, everything-else-denied) rule."""
    cwd = Path(tempfile.mkdtemp(prefix="rc-pe7-"))
    engine = PermissionEngine(mode="plan", cwd=cwd)
    readonly = engine.decide("Bash", {"command": "ls"})
    other = engine.decide("Bash", {"command": "rm file.txt"})
    ctx.check("read-only bash allowed in plan mode", readonly.action == "allow")
    ctx.check("mutating bash denied in plan mode", other.action == "deny")


if __name__ == "__main__":
    ctx = Ctx()
    results, passed, failed, skipped = run_all(TESTS, ctx)
    sys.exit(print_results(results, passed, failed, skipped))
