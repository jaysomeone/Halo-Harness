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
    ctx.check("mentions `halo init`", "halo init" in quick_start)
    ctx.check("mentions an install command (uv tool install or pipx)",
              "uv tool install" in quick_start or "pipx install" in quick_start)
    ctx.check("covers Windows too, in the same leading section", "Windows" in quick_start)
    windows_lines = [l for l in quick_start.split("```powershell", 1)[-1].split("```", 1)[0].splitlines() if l.strip()]
    ctx.check(f"the Windows recipe is a real multi-line walkthrough, got {windows_lines}", len(windows_lines) >= 4)


@test
def test_install_md_points_to_init_near_the_top(ctx: Ctx):
    text = (REPO_DIR / "docs" / "harness" / "INSTALL.md").read_text(encoding="utf-8")
    head = text.split("## Kali / Linux", 1)[0]
    ctx.check("INSTALL.md mentions `halo init` before the first real OS section",
              "halo init" in head)


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
    """H12's own pinning -- kept exact (checked against the CHANGELOG's own
    still-present [0.4.1] entry, not the CURRENT version) now that H13 has
    bumped past it; see `test_version_bumped_to_0_5_0_and_changelog_has_an_
    entry` below for H13's own version-bump pin."""
    changelog = (REPO_DIR / "CHANGELOG.md").read_text(encoding="utf-8")
    ctx.check("CHANGELOG.md still has a [0.4.1] entry", "[0.4.1]" in changelog)
    # 2.0.0 rename: CHANGELOG history keeps the name it was WRITTEN under
    # (brief: "history in CHANGELOG keeps old names for old releases") --
    # the [0.4.1] entry predates the rename, so it still says the old
    # command name verbatim; only entries at/after [2.0.0] say `halo`.
    ctx.check("the [0.4.1] entry mentions rolo-claude init",
              "rolo-claude init" in changelog.split("[0.4.1]", 1)[1].split("[0.4.0]", 1)[0])


@test
def test_version_bumped_to_0_5_0_and_changelog_has_an_entry(ctx: Ctx):
    """H13's own pinning -- kept exact (checked against the CHANGELOG's own
    still-present [0.5.0] entry, not the CURRENT version) now that H14 has
    bumped past it; see `test_version_bumped_to_0_6_0_and_changelog_has_an_
    entry` below for H14's own version-bump pin."""
    changelog = (REPO_DIR / "CHANGELOG.md").read_text(encoding="utf-8")
    ctx.check("CHANGELOG.md still has a [0.5.0] entry", "[0.5.0]" in changelog)
    entry = changelog.split("[0.5.0]", 1)[1].split("\n## [", 1)[0]
    for phrase in ("lazy", "inline", "/resume"):
        ctx.check(f"the [0.5.0] entry mentions {phrase!r}", phrase in entry.lower())


@test
def test_version_bumped_to_0_6_0_and_changelog_has_an_entry(ctx: Ctx):
    """H14's own pinning -- kept exact (checked against the CHANGELOG's own
    still-present [0.6.0] entry, not the CURRENT version) now that V2a has
    bumped past it; see `test_version_bumped_to_0_7_0_and_changelog_has_an_
    entry` below for V2a's own version-bump pin."""
    changelog = (REPO_DIR / "CHANGELOG.md").read_text(encoding="utf-8")
    ctx.check("CHANGELOG.md still has a [0.6.0] entry", "[0.6.0]" in changelog)
    entry = changelog.split("[0.6.0]", 1)[1].split("\n## [", 1)[0]
    for phrase in ("databricks", "work", "team.json"):
        ctx.check(f"the [0.6.0] entry mentions {phrase!r}", phrase in entry.lower())


@test
def test_version_bumped_to_0_7_0_and_changelog_has_an_entry(ctx: Ctx):
    """H14's own pinning shape, kept exact (checked against the CHANGELOG's
    own still-present [0.7.0] entry, not the CURRENT version) now that V2b/
    V2c has bumped past it; see `test_version_bumped_to_0_8_0_and_changelog_
    has_an_entry` below for THIS milestone's own version-bump pin."""
    changelog = (REPO_DIR / "CHANGELOG.md").read_text(encoding="utf-8")
    ctx.check("CHANGELOG.md still has a [0.7.0] entry", "[0.7.0]" in changelog)
    entry = changelog.split("[0.7.0]", 1)[1].split("\n## [", 1)[0]
    for phrase in ("v2a", "reasoning replay", "route split"):
        ctx.check(f"the [0.7.0] entry mentions {phrase!r}", phrase in entry.lower())


@test
def test_version_bumped_to_0_8_0_and_changelog_has_an_entry(ctx: Ctx):
    """V2 brief (docs/harness/V2-brief.md) V2b+V2c's own pinning shape, kept
    exact (checked against the CHANGELOG's own still-present [0.8.0] entry,
    not the CURRENT version) now that the 1.0.0 docs pass has bumped past
    it; see `test_version_bumped_to_1_0_0_and_changelog_has_an_entry` below
    for THIS release's own version-bump pin."""
    changelog = (REPO_DIR / "CHANGELOG.md").read_text(encoding="utf-8")
    ctx.check("CHANGELOG.md still has a [0.8.0] entry", "[0.8.0]" in changelog)
    entry = changelog.split("[0.8.0]", 1)[1].split("\n## [", 1)[0]
    for phrase in ("work-matrix", "roles", "researcher"):
        ctx.check(f"the [0.8.0] entry mentions {phrase!r}", phrase in entry.lower())


@test
def test_version_bumped_to_1_0_0_and_changelog_has_an_entry(ctx: Ctx):
    """rolo-claude 1.0.0 (the name this release actually shipped under --
    see the [2.0.0] CHANGELOG entry for the rename itself): the stable
    general harness release -- a docs pass
    summarising the 0.7.0/0.8.0 Databricks-correctness-and-tooling line,
    NOT a rename and NOT a new console-script alias (a separate
    `databricks-claude` repository was spun off instead; see the
    CHANGELOG's own [1.0.0] entry for why the version jumped straight past
    `2.0.0`). H14's own pinning shape, kept exact (checked against the
    CHANGELOG's own still-present [1.0.0] entry, not the CURRENT version)
    now that the 1.0.1 hotfix has bumped past it; see
    `test_version_bumped_to_1_0_1_and_changelog_has_an_entry` below for
    THIS release's own version-bump pin."""
    changelog = (REPO_DIR / "CHANGELOG.md").read_text(encoding="utf-8")
    ctx.check("CHANGELOG.md has a [1.0.0] entry", "[1.0.0]" in changelog)
    entry = changelog.split("[1.0.0]", 1)[1].split("\n## [", 1)[0]
    for phrase in ("stable", "databricks-claude", "roles"):
        ctx.check(f"the [1.0.0] entry mentions {phrase!r}", phrase in entry.lower())


@test
def test_version_bumped_to_1_0_1_and_changelog_has_an_entry(ctx: Ctx):
    """rolo-claude 1.0.1 (the name this release actually shipped under):
    the hotfix release from the owner's first real 1.0.0 run on the Kali
    work VM (DNS down, then a live `/model` screenshot from it once
    fixed) -- thirteen fixes, no behavior changes beyond them; see the
    CHANGELOG's own [1.0.1] entry for the full list. H14c's own pinning
    shape, kept exact (checked against the CHANGELOG's own still-present
    [1.0.1] entry, not the CURRENT version) now that the 2.0.0 rename has
    bumped past it; see `test_version_bumped_to_2_0_0_and_changelog_has_
    an_entry` below for THIS release's own version-bump pin."""
    changelog = (REPO_DIR / "CHANGELOG.md").read_text(encoding="utf-8")
    ctx.check("CHANGELOG.md has a [1.0.1] entry", "[1.0.1]" in changelog)
    entry = changelog.split("[1.0.1]", 1)[1].split("\n## [", 1)[0]
    for phrase in ("fail fast", "provider", "x509_strict"):
        ctx.check(f"the [1.0.1] entry mentions {phrase!r}", phrase in entry.lower())


@test
def test_version_bumped_to_2_0_0_and_changelog_has_an_entry(ctx: Ctx):
    """Halo Harness 2.0.0: the rename release -- rolo-claude 1.0.1
    continues unchanged as its own repository; this one is where every
    later feature lands. H14c's own pinning shape, kept exact (checked
    against the CHANGELOG's own still-present [2.0.0] entry, not the
    CURRENT version) now that the 2.0.1 "run from any directory" release
    has bumped past it; see `test_version_bumped_to_2_0_1_and_changelog_
    has_an_entry` below for THIS release's own version-bump pin (the
    single `__version__ == ...` exact-match check lives in the
    newest-added test only, per this file's own established convention)."""
    changelog = (REPO_DIR / "CHANGELOG.md").read_text(encoding="utf-8")
    ctx.check("CHANGELOG.md has a [2.0.0] entry", "[2.0.0]" in changelog)
    entry = changelog.split("[2.0.0]", 1)[1].split("\n## [", 1)[0]
    for phrase in ("halo", "migration", "rename", "intro"):
        ctx.check(f"the [2.0.0] entry mentions {phrase!r}", phrase in entry.lower())


@test
def test_version_bumped_to_2_0_1_and_changelog_has_an_entry(ctx: Ctx):
    """Halo Harness 2.0.1: the "run from any directory" release -- makes
    the install unmistakable and proves (by test) that no behavior
    depends on `halo` being started from inside the checkout. H14c's own
    pinning shape, kept exact (checked against the CHANGELOG's own
    still-present [2.0.1] entry, not the CURRENT version) now that 2.0.2
    has bumped past it; see `test_version_bumped_to_2_0_2_and_changelog_
    has_an_entry` below for THIS release's own version-bump pin."""
    changelog = (REPO_DIR / "CHANGELOG.md").read_text(encoding="utf-8")
    ctx.check("CHANGELOG.md has a [2.0.1] entry", "[2.0.1]" in changelog)
    entry = changelog.split("[2.0.1]", 1)[1].split("\n## [", 1)[0]
    for phrase in ("install.md", "pythonpath fallback", "scratch", "unique sentence"):
        ctx.check(f"the [2.0.1] entry mentions {phrase!r}", phrase in entry.lower())


@test
def test_version_bumped_to_2_0_2_and_changelog_has_an_entry(ctx: Ctx):
    """Halo Harness 2.0.2 (W7 round 1): terminal tab title re-assertion
    and roles v2 (per-role effort, custom role names, tab/shell
    completion, role templates). H14c's own pinning shape, kept exact
    (checked against the CHANGELOG's own still-present [2.0.2] entry, not
    the CURRENT version) now that 2.0.3 has bumped past it; see
    `test_version_bumped_to_2_0_3_and_changelog_has_an_entry` below for
    THIS release's own version-bump pin."""
    changelog = (REPO_DIR / "CHANGELOG.md").read_text(encoding="utf-8")
    ctx.check("CHANGELOG.md has a [2.0.2] entry", "[2.0.2]" in changelog)
    entry = changelog.split("[2.0.2]", 1)[1].split("\n## [", 1)[0]
    for phrase in ("terminal", "role"):
        ctx.check(f"the [2.0.2] entry mentions {phrase!r}", phrase in entry.lower())


@test
def test_version_bumped_to_2_0_3_and_changelog_has_an_entry(ctx: Ctx):
    """Halo Harness 2.0.3 round 2 (`plans/2.0.3-ollama-round2-brief.md`):
    the `ol:` provider on Ollama's native `/api/chat` API. THIS release's
    own version-bump pin (2.0.3 is still in progress -- rounds 3-6 of the
    same brief land as later commits against the SAME [2.0.3] CHANGELOG
    entry, not a new version number each time)."""
    from halo_harness import __version__
    ctx.check(f"__version__ is 2.0.3, got {__version__!r}", __version__ == "2.0.3")
    changelog = (REPO_DIR / "CHANGELOG.md").read_text(encoding="utf-8")
    ctx.check("CHANGELOG.md has a [2.0.3] entry", "[2.0.3]" in changelog)
    entry = changelog.split("[2.0.3]", 1)[1].split("\n## [", 1)[0]
    for phrase in ("ollama", "num_ctx"):
        ctx.check(f"the [2.0.3] entry mentions {phrase!r}", phrase in entry.lower())


@test
def test_changelog_2_0_0_mentions_the_fixpass_additions(ctx: Ctx):
    """2.0.0 fixpass finding 3: the state-dir migration (and the deliberate
    choice to leave no link behind -- item B), the env-file copy-forward,
    the team.json fallback, the kept-recognizing-old markers, and the
    stream-json version alias must all be mentioned somewhere in the
    [2.0.0] entry -- not just fixed in code with no record of why."""
    changelog = (REPO_DIR / "CHANGELOG.md").read_text(encoding="utf-8")
    entry = changelog.split("## [2.0.0]", 1)[1].split("\n## [", 1)[0].lower()
    for phrase in ("no link is created", "content forward", "team.json", "provenance marker",
                   "rolo_claude_version"):
        ctx.check(f"the [2.0.0] entry mentions {phrase!r}", phrase in entry)


@test
def test_upgrading_section_exists_in_readme_and_install_md(ctx: Ctx):
    """2.0.0 fixpass finding 3 (superseded by 2.0.1, see CHANGELOG): 2.0.0
    briefly had an upgrader's install command break outright against an
    already-installed rolo-claude 1.0.1 console script (uv aborted, pipx
    silently refused, pip silently overwrote) -- as of 2.0.1, `halo`'s own
    distribution ships exactly one console script and never conflicts with
    it at all, so there is no `--force` escape hatch left to document; both
    README.md and INSTALL.md must still say plainly that uninstalling the
    old tool first is recommended (not required), with the exact uninstall
    command for each of the three installers."""
    readme = (REPO_DIR / "README.md").read_text(encoding="utf-8")
    install = (REPO_DIR / "docs" / "harness" / "INSTALL.md").read_text(encoding="utf-8")
    # The front page no longer talks about the previous name at all (2026-10-02
    # README redo); the upgrade path lives in docs/INSTALL.md, which the
    # README's Quick start links to.
    ctx.check("README.md links to docs/INSTALL.md for the upgrade path", "docs/INSTALL.md" in readme)
    ctx.check("docs/INSTALL.md names the upgrade path", "upgrading from" in install.lower()
              and "rolo-claude 1.0.1" in install.lower())
    ctx.check("INSTALL.md has a real '## Upgrading from rolo-claude 1.0.1' section",
              "## Upgrading from rolo-claude 1.0.1" in install)
    install_section = install.split("## Upgrading from rolo-claude 1.0.1", 1)[1].split("\n## ", 1)[0]
    for cmd in ("uv tool uninstall rolo-claude", "pipx uninstall rolo-claude", "pip uninstall rolo-claude"):
        ctx.check(f"INSTALL.md's upgrade section names the exact command {cmd!r}", cmd in install_section)
    ctx.check("INSTALL.md's upgrade section says plainly that uninstalling is recommended, not required",
              "recommended" in install_section.lower())
    ctx.check("INSTALL.md's upgrade section says plainly that no link is left at the old location",
              "no link is created" in install_section.lower())
    ctx.check("...and that the separate 1.0.1 install must not be used again",
              "1.0.1" in install_section and "must not" in install_section.lower())
    ctx.check("INSTALL.md points an upgrader at the section before the install commands run",
              install.index("## Upgrading from rolo-claude 1.0.1") < install.index("uv tool install --editable .\n```")
              if "uv tool install --editable .\n```" in install
              else install.index("## Upgrading from rolo-claude 1.0.1") < install.index("## Kali / Linux"))


@test
def test_install_md_top_level_exists_is_linked_from_readme_and_leads_with_one_liners(ctx: Ctx):
    """2.0.1 "run from any directory" release: a NEW top-level docs/
    INSTALL.md (distinct from the existing docs/harness/INSTALL.md, which
    keeps its own exhaustive walkthrough and its own "Upgrading from
    rolo-claude 1.0.1" section pinned above) leads with one install line
    per platform and is linked from the README, not just sitting there
    unreferenced."""
    readme = (REPO_DIR / "README.md").read_text(encoding="utf-8")
    install_path = REPO_DIR / "docs" / "INSTALL.md"
    ctx.check("docs/INSTALL.md exists", install_path.is_file())
    install = install_path.read_text(encoding="utf-8")
    ctx.check("README links to docs/INSTALL.md", "[docs/INSTALL.md](docs/INSTALL.md)" in readme)
    ctx.check("docs/INSTALL.md names the --reinstall form (the one command for first install AND "
              "every post-`git pull` reinstall)", "uv tool install --reinstall ." in install)
    ctx.check("docs/INSTALL.md names the pipx alternative", "pipx install --force -e ." in install)
    ctx.check("docs/INSTALL.md mentions the PEP 668 externally-managed-environment note",
              "externally-managed-environment" in install)
    ctx.check("docs/INSTALL.md names the direct-from-GitHub install",
              "uv tool install git+https://github.com/roloVibes/Halo-Harness" in install)
    ctx.check("docs/INSTALL.md says to cd anywhere and type halo",
              "cd anywhere" in install.lower() and "type `halo`" in install.lower())
    ctx.check("docs/INSTALL.md says the clone is for git pull only",
              "git pull` only" in install.lower() or "git pull only" in install.lower())
    ctx.check("docs/INSTALL.md keeps its own Upgrading-from-rolo-claude section",
              "## Upgrading from rolo-claude 1.0.1" in install)
    ctx.check("docs/INSTALL.md links onward to the exhaustive docs/harness/INSTALL.md",
              "harness/INSTALL.md" in install)


@test
def test_round6_update_docs_are_in_place(ctx: Ctx):
    """Halo 2.0.2 round 6: clear install/update steps -- README's Install
    (three one-liners) and new Update section, docs/INSTALL.md's Windows
    PowerShell/Uninstall/Update sections, HANDBOOK/COMMANDS naming `halo
    update`/`/update`, and the CHANGELOG [2.0.2] entry."""
    readme = (REPO_DIR / "README.md").read_text(encoding="utf-8")
    ctx.check("README has an ## Update section", "## Update" in readme)
    ctx.check("README's Install section names all three one-liners",
              "install-halo.sh" in readme and "uv tool install git+" in readme and "pipx install git+" in readme)
    ctx.check("README's Update section names halo update and /update",
              "halo update" in readme and "/update" in readme)

    install = (REPO_DIR / "docs" / "INSTALL.md").read_text(encoding="utf-8")
    ctx.check("docs/INSTALL.md has a Windows PowerShell one-liner naming install-halo.ps1",
              "install-halo.ps1" in install and "### Windows" in install)
    ctx.check("docs/INSTALL.md has its own ## Update section", "## Update" in install)
    ctx.check("docs/INSTALL.md has an ## Uninstall section", "## Uninstall" in install)
    ctx.check("docs/INSTALL.md's Update section names halo --force and the Windows order",
              "--force" in install.split("## Update", 1)[1].split("## ", 1)[0])

    handbook = (REPO_DIR / "docs" / "HANDBOOK.md").read_text(encoding="utf-8")
    ctx.check("HANDBOOK.md documents halo update and /update",
              "halo update" in handbook and "/update" in handbook)

    commands = (REPO_DIR / "docs" / "COMMANDS.md").read_text(encoding="utf-8")
    ctx.check("COMMANDS.md has a ## `halo update` heading", "## `halo update`" in commands)

    slash = (REPO_DIR / "docs" / "SLASH-COMMANDS.md").read_text(encoding="utf-8")
    ctx.check("SLASH-COMMANDS.md has a ### `/update` heading", "### `/update`" in slash)

    changelog = (REPO_DIR / "CHANGELOG.md").read_text(encoding="utf-8")
    entry = changelog.split("## [2.0.2]", 1)[1].split("\n## [", 1)[0]
    ctx.check("the [2.0.2] entry mentions halo update and /update", "halo update" in entry and "/update" in entry)


if __name__ == "__main__":
    ctx = Ctx()
    results, passed, failed, skipped = run_all(TESTS, ctx)
    sys.exit(print_results(results, passed, failed, skipped))
