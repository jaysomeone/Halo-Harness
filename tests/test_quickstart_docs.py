"""tests.test_quickstart_docs -- H12 brief Part D: README's new "Quick start
(Kali / Linux)" first section, INSTALL.md pointing to `init`, the CHANGELOG
entry, and the version bump. Pure text/metadata checks -- no subprocess, no
network.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.helpers.runner import Ctx, new_registry, print_results, run_all

REPO_DIR = Path(__file__).resolve().parent.parent
test, TESTS = new_registry()

_SECTION_RE = re.compile(r"^##\s+(.+)$", re.MULTILINE)


@test
def test_readme_first_section_is_quick_start(ctx: Ctx):
    text = (REPO_DIR / "README.md").read_text(encoding="utf-8")
    sections = _SECTION_RE.findall(text)
    ctx.check(f"README has at least one ## section, got {sections[:3]}", sections)
    ctx.check(f"the FIRST ## section is the Kali/Linux quick start, got {sections[0]!r}",
              sections[0].strip().lower().startswith("quick start"))
    ctx.check("it names Kali/Linux", "kali" in sections[0].lower() or "linux" in sections[0].lower())


@test
def test_readme_quick_start_mentions_init_and_windows(ctx: Ctx):
    text = (REPO_DIR / "README.md").read_text(encoding="utf-8")
    quick_start = text.split("## Quick start", 1)[1].split("\n## ", 1)[0]
    ctx.check("mentions `rolo-claude init`", "rolo-claude init" in quick_start)
    ctx.check("mentions an install command (uv tool install or pipx)",
              "uv tool install" in quick_start or "pipx install" in quick_start)
    ctx.check("covers Windows too, in the same leading section", "Windows" in quick_start)
    windows_lines = [l for l in quick_start.split("```powershell", 1)[-1].split("```", 1)[0].splitlines() if l.strip()]
    ctx.check(f"the Windows recipe is a real multi-line walkthrough, got {windows_lines}", len(windows_lines) >= 4)


@test
def test_install_md_points_to_init_near_the_top(ctx: Ctx):
    text = (REPO_DIR / "docs" / "harness" / "INSTALL.md").read_text(encoding="utf-8")
    head = text.split("## Kali / Linux", 1)[0]
    ctx.check("INSTALL.md mentions `rolo-claude init` before the first real OS section",
              "rolo-claude init" in head)


@test
def test_rest_of_readme_unchanged_below_quick_start(ctx: Ctx):
    """The brief: "the rest of the README unchanged below it" -- proven by
    every pre-existing top-level section still being present, in the same
    relative order, just pushed down by the new Quick start section."""
    text = (REPO_DIR / "README.md").read_text(encoding="utf-8")
    for heading in ("## What it is", "## Install", "## Models and providers", "## Tests", "## Licence"):
        ctx.check(f"{heading!r} is still present", heading in text)
    idx_quick_start = text.index("## Quick start")
    idx_what_it_is = text.index("## What it is")
    ctx.check("Quick start comes before What it is", idx_quick_start < idx_what_it_is)


@test
def test_version_bumped_to_0_4_1_and_changelog_has_an_entry(ctx: Ctx):
    from rolo_claude import __version__
    ctx.check(f"__version__ is 0.4.1, got {__version__!r}", __version__ == "0.4.1")
    changelog = (REPO_DIR / "CHANGELOG.md").read_text(encoding="utf-8")
    ctx.check("CHANGELOG.md has a [0.4.1] entry", "[0.4.1]" in changelog)
    ctx.check("the [0.4.1] entry mentions rolo-claude init",
              "rolo-claude init" in changelog.split("[0.4.1]", 1)[1].split("[0.4.0]", 1)[0])


if __name__ == "__main__":
    ctx = Ctx()
    results, passed, failed, skipped = run_all(TESTS, ctx)
    sys.exit(print_results(results, passed, failed, skipped))
