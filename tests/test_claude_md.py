"""tests.test_claude_md -- config/claude_md.py: instruction order, AGENTS.md
fallback, @import expansion incl. 4-hop and fenced skip, comment stripping,
excludes.
"""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.helpers.runner import Ctx, new_registry, print_results, run_all
from tests.helpers.fake_home import build_fake_home
from rolo_claude.config.claude_md import discover_instructions, strip_block_html_comments

test, TESTS = new_registry()


class FakeSettings:
    def __init__(self, excludes=None, instruction_files="claude-md-or-agents-md"):
        self.claude_md_excludes = excludes or []
        self.instruction_files = instruction_files


@test
def test_bare_mode_returns_empty(ctx: Ctx):
    fh = build_fake_home()
    bundle = discover_instructions(fh["proj"], FakeSettings(), trusted=True, bare=True)
    ctx.check("bare -> no files", bundle.files == [])
    ctx.check("bare -> no warnings", bundle.warnings == [])


@test
def test_project_claude_md_and_imports_included(ctx: Ctx):
    fh = build_fake_home()
    bundle = discover_instructions(fh["proj"], FakeSettings(), trusted=True)
    joined = "\n".join(f.text for f in bundle.files)
    ctx.check("root project text present", "Root project instructions" in joined)
    ctx.check("@AGENTS.md import pulled in", "Agents.md content" in joined)
    ctx.check("@docs/extra.md import pulled in", "Extra imported doc content" in joined)


@test
def test_ancestor_walk_excludes_subdirectories(ctx: Ctx):
    fh = build_fake_home()
    bundle = discover_instructions(fh["proj"], FakeSettings(), trusted=True)
    joined = "\n".join(f.text for f in bundle.files)
    ctx.check("sub/CLAUDE.md is a CHILD of proj, must NOT appear when cwd=proj",
              "Sub-directory instructions" not in joined)


@test
def test_ancestor_walk_includes_subdir_when_cwd_is_there(ctx: Ctx):
    fh = build_fake_home()
    bundle = discover_instructions(fh["sub"], FakeSettings(), trusted=True)
    joined = "\n".join(f.text for f in bundle.files)
    ctx.check("proj/CLAUDE.md is an ancestor of proj/sub", "Root project instructions" in joined)
    ctx.check("proj/sub/CLAUDE.md itself included", "Sub-directory instructions" in joined)


@test
def test_block_html_comment_stripped_but_fence_preserved(ctx: Ctx):
    fh = build_fake_home()
    bundle = discover_instructions(fh["proj"], FakeSettings(), trusted=True)
    proj_file = next(f for f in bundle.files if f.path == fh["proj"] / "CLAUDE.md")
    ctx.check("block HTML comment removed", "that spans two lines" not in proj_file.text)
    ctx.check("text after the comment survives", "Text after the comment" in proj_file.text)
    ctx.check("fenced @not-an-import survives untouched (not import-expanded)",
              "@not-an-import" in proj_file.text)


@test
def test_strip_block_html_comments_standalone(ctx: Ctx):
    text = "before\n<!-- gone\nstill gone -->\nafter\n```\n<!-- kept inside fence -->\n```\n"
    stripped = strip_block_html_comments(text)
    ctx.check("comment removed outside fence", "still gone" not in stripped)
    ctx.check("before/after text survive", "before" in stripped and "after" in stripped)
    ctx.check("comment inside fence survives", "kept inside fence" in stripped)


@test
def test_claude_md_excludes_glob(ctx: Ctx):
    fh = build_fake_home()
    pattern = str(fh["proj"] / "CLAUDE.md").replace("\\", "/")
    bundle = discover_instructions(fh["proj"], FakeSettings(excludes=[pattern]), trusted=True)
    joined_paths = [str(f.path) for f in bundle.files]
    ctx.check("excluded file's own path never appears in the files list",
              str(fh["proj"] / "CLAUDE.md") not in joined_paths)
    ctx.check("a warning was recorded for the exclusion",
              any("excluded" in w for w in bundle.warnings))


@test
def test_agents_md_fallback_only_when_no_claude_md(ctx: Ctx):
    d = Path(tempfile.mkdtemp(prefix="claude-md-agents-fallback-"))
    (d / "AGENTS.md").write_text("agents fallback content", encoding="utf-8")
    bundle = discover_instructions(d, FakeSettings(instruction_files="claude-md-or-agents-md"), trusted=True)
    joined = "\n".join(f.text for f in bundle.files)
    ctx.check("AGENTS.md used when no CLAUDE.md exists at that level", "agents fallback content" in joined)


@test
def test_agents_md_fallback_suppressed_when_claude_md_present(ctx: Ctx):
    d = Path(tempfile.mkdtemp(prefix="claude-md-agents-suppressed-"))
    (d / "CLAUDE.md").write_text("real claude md", encoding="utf-8")
    (d / "AGENTS.md").write_text("should not appear", encoding="utf-8")
    bundle = discover_instructions(d, FakeSettings(instruction_files="claude-md-or-agents-md"), trusted=True)
    joined = "\n".join(f.text for f in bundle.files)
    ctx.check("CLAUDE.md present -> AGENTS.md fallback suppressed", "should not appear" not in joined)
    ctx.check("CLAUDE.md itself included", "real claude md" in joined)


@test
def test_instruction_files_claude_md_only_never_falls_back(ctx: Ctx):
    d = Path(tempfile.mkdtemp(prefix="claude-md-only-mode-"))
    (d / "AGENTS.md").write_text("must not appear", encoding="utf-8")
    bundle = discover_instructions(d, FakeSettings(instruction_files="claude-md"), trusted=True)
    joined = "\n".join(f.text for f in bundle.files)
    ctx.check("instruction_files='claude-md' never reads AGENTS.md", "must not appear" not in joined)


@test
def test_four_hop_import_limit(ctx: Ctx):
    d = Path(tempfile.mkdtemp(prefix="claude-md-4hop-"))
    (d / "CLAUDE.md").write_text("root\n@hop1.md\n", encoding="utf-8")
    (d / "hop1.md").write_text("hop1\n@hop2.md\n", encoding="utf-8")
    (d / "hop2.md").write_text("hop2\n@hop3.md\n", encoding="utf-8")
    (d / "hop3.md").write_text("hop3\n@hop4.md\n", encoding="utf-8")
    (d / "hop4.md").write_text("hop4-should-be-excluded\n@hop5.md\n", encoding="utf-8")
    (d / "hop5.md").write_text("hop5-must-never-appear\n", encoding="utf-8")
    bundle = discover_instructions(d, FakeSettings(), trusted=True)
    joined = "\n".join(f.text for f in bundle.files)
    ctx.check("hop1 (1st import) included", "hop1" in joined)
    ctx.check("hop2 (2nd import) included", "hop2" in joined)
    ctx.check("hop3 (3rd import) included", "hop3" in joined)
    ctx.check("hop4 (4th import, at the limit) included", "hop4-should-be-excluded" in joined)
    ctx.check("hop5 (5th import, beyond the 4-hop limit) excluded", "hop5-must-never-appear" not in joined)


@test
def test_import_cycle_is_safe(ctx: Ctx):
    d = Path(tempfile.mkdtemp(prefix="claude-md-cycle-"))
    (d / "CLAUDE.md").write_text("root\n@a.md\n", encoding="utf-8")
    (d / "a.md").write_text("a-content\n@b.md\n", encoding="utf-8")
    (d / "b.md").write_text("b-content\n@a.md\n", encoding="utf-8")  # cycle back to a.md
    try:
        bundle = discover_instructions(d, FakeSettings(), trusted=True)
        joined = "\n".join(f.text for f in bundle.files)
        ctx.check("a-content present exactly once", joined.count("a-content") == 1)
        ctx.check("b-content present", "b-content" in joined)
    except RecursionError:
        ctx.check("import cycle must not cause infinite recursion", False)


@test
def test_external_import_needs_trust(ctx: Ctx):
    outside_root = Path(tempfile.mkdtemp(prefix="claude-md-external-"))
    outside_file = outside_root / "outside.md"
    outside_file.write_text("outside content", encoding="utf-8")
    proj = Path(tempfile.mkdtemp(prefix="claude-md-proj-for-external-"))
    (proj / "CLAUDE.md").write_text(f"root\n@{outside_file}\n", encoding="utf-8")

    untrusted = discover_instructions(proj, FakeSettings(), trusted=False)
    joined_untrusted = "\n".join(f.text for f in untrusted.files)
    ctx.check("untrusted cwd: external (outside cwd, outside ~/.claude) import skipped",
              "outside content" not in joined_untrusted)
    ctx.check("a warning was recorded", len(untrusted.warnings) >= 1)

    trusted = discover_instructions(proj, FakeSettings(), trusted=True)
    joined_trusted = "\n".join(f.text for f in trusted.files)
    ctx.check("trusted cwd: external import allowed", "outside content" in joined_trusted)


@test
def test_missing_file_never_raises(ctx: Ctx):
    d = Path(tempfile.mkdtemp(prefix="claude-md-empty-"))
    try:
        bundle = discover_instructions(d, FakeSettings(), trusted=True)
        ctx.check("no CLAUDE.md anywhere -> empty (or near-empty) bundle, no crash", isinstance(bundle.files, list))
    except Exception as e:
        ctx.check(f"discover_instructions must never raise on a missing tree, got {e!r}", False)


@test
def test_render_joins_with_headers(ctx: Ctx):
    fh = build_fake_home()
    bundle = discover_instructions(fh["proj"], FakeSettings(), trusted=True)
    rendered = bundle.render()
    ctx.check("render() includes a '## <path>' header per file", rendered.count("## ") == len(bundle.files))


if __name__ == "__main__":
    ctx = Ctx()
    results, passed, failed, skipped = run_all(TESTS, ctx)
    sys.exit(print_results(results, passed, failed, skipped))
