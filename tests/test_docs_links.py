"""tests.test_docs_links -- H14b brief: "README renders on GitHub (no
broken relative links -- add a link-check test)". Scans every Markdown
`[text](path)` link in README.md and docs/**/*.md; a relative (non-URL,
non-mailto, non-pure-anchor) link must resolve to a real file on disk
(a `#fragment` is stripped before checking -- this does not verify the
fragment names a real heading, only that the FILE exists). Pure text scan,
no network, no subprocess.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.helpers.runner import Ctx, new_registry, print_results, run_all

REPO_DIR = Path(__file__).resolve().parent.parent
test, TESTS = new_registry()

# `[text](target)` -- text may contain nested `[...]` (e.g. an inline-code
# span with brackets is rare in these docs, so a non-greedy match is fine);
# target is everything up to the first unescaped `)` or whitespace (a
# Markdown title suffix, `"..."`, is not used anywhere in this repo's own
# docs, so it isn't specially handled here).
_LINK_RE = re.compile(r"\[[^\]]*\]\(([^)\s]+)\)")


def _markdown_files() -> list:
    files = [REPO_DIR / "README.md"]
    files += sorted((REPO_DIR / "docs").rglob("*.md"))
    return [f for f in files if f.is_file() and "tui-snapshots" not in str(f)]


def _is_external_or_special(target: str) -> bool:
    if target.startswith("#"):
        return True  # a same-file anchor -- no separate file to check
    if target.startswith("mailto:"):
        return True
    scheme = urlparse(target).scheme
    return scheme in ("http", "https", "ftp", "mailto")


def _check_file_links(path: Path) -> "list[str]":
    text = path.read_text(encoding="utf-8", errors="replace")
    problems: list = []
    for m in _LINK_RE.finditer(text):
        target = m.group(1)
        if _is_external_or_special(target):
            continue
        # Strip a trailing #fragment (checked against the FILE existing,
        # not the fragment/heading -- see module docstring).
        file_part = target.split("#", 1)[0]
        if not file_part:
            continue  # "#fragment" already handled above; an empty
            # file_part here would only happen for a target that was
            # JUST "#..." (already caught) -- defensive, not reachable.
        resolved = (path.parent / file_part).resolve()
        # A link to a directory (e.g. "[docs/](..)") is valid on GitHub too
        # -- it renders that directory's own file listing.
        if not resolved.is_file() and not resolved.is_dir():
            line_no = text.count("\n", 0, m.start()) + 1
            problems.append(f"{path.relative_to(REPO_DIR)}:{line_no}: broken relative link {target!r} "
                             f"-> {resolved} does not exist")
    return problems


@test
def test_no_broken_relative_links_in_readme_and_docs(ctx: Ctx):
    files = _markdown_files()
    ctx.check("at least one Markdown file was found to scan", files)
    all_problems: list = []
    for path in files:
        all_problems.extend(_check_file_links(path))
    ctx.check("no broken relative links found:\n  " + "\n  ".join(all_problems), not all_problems)


@test
def test_readme_and_harness_index_actually_contain_links(ctx: Ctx):
    """A cheap canary against this test silently checking nothing: the
    README's own Documentation table and the docs/harness/ index are both
    expected to contain real Markdown links, not just backtick mentions."""
    readme = (REPO_DIR / "README.md").read_text(encoding="utf-8")
    ctx.check("README.md contains at least 5 Markdown links", len(_LINK_RE.findall(readme)) >= 5)
    harness_index = (REPO_DIR / "docs" / "harness" / "README.md").read_text(encoding="utf-8")
    ctx.check("docs/harness/README.md contains at least 5 Markdown links",
              len(_LINK_RE.findall(harness_index)) >= 5)


if __name__ == "__main__":
    ctx = Ctx()
    results, passed, failed, skipped = run_all(TESTS, ctx)
    sys.exit(print_results(results, passed, failed, skipped))
