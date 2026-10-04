"""tests.test_privacy_scan -- W5 (2026-10-02, from the first privacy pass;
see plans/2.0.9-review-privacy-brief.md section C) and its W5b follow-up:
guards the content scrub `tests/helpers/fake_home.py`, `test_memory.py`,
`test_frontmatter.py`, `test_loop_headless.py`, `test_mcp_catalog.py`,
`test_paths.py`, `docs/harness/ACCEPTANCE-2026-09-25.md` and
`docs/harness/INSTALL.md` just went through so it cannot quietly
regress. Scans the TRACKED tree only (`git ls-files` -- history is a
separate, already-reviewed question, see the privacy brief's section B)
for: LAN addresses, two specific real home paths, the exact machine/
project/hobby-gear/work-tool names this pass removed, and key-shaped
fragments outside `tests/privacy_scan_allowlist.txt`'s known fakes.

Deliberately narrower than `tests/test_docs_hygiene.py`'s own home-path/
Databricks-host checks once applied past docs/: those two generic checks
false-positive constantly across the wider tree (`C:\\Users\\example\\...`-
style fixture paths and dozens of distinct FAKE Databricks hostnames are
normal and expected throughout tests/ -- confirmed by hand before writing
this file). What's banned here instead is the SPECIFIC real strings this
pass actually found and removed -- precise enough to regress-guard them
without re-litigating every generic placeholder the suite already uses on
purpose.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.helpers.runner import Ctx, new_registry, print_results, run_all
from halo_harness.redact import _BEARER_RE, _TOKEN_PATTERNS

REPO_DIR = Path(__file__).resolve().parent.parent
ALLOWLIST_PATH = REPO_DIR / "tests" / "privacy_scan_allowlist.txt"
test, TESTS = new_registry()

# RFC1918 private ranges -- same shape test_docs_hygiene.py uses for docs/
# alone; zero legitimate use anywhere in this tree (verified by hand), so
# safe to apply tree-wide with no allowlist.
_LAN_IP_RE = re.compile(
    r"\b(?:10\.\d{1,3}\.\d{1,3}\.\d{1,3}|"
    r"172\.(?:1[6-9]|2\d|3[0-1])\.\d{1,3}\.\d{1,3}|"
    r"192\.168\.\d{1,3}\.\d{1,3})\b"
)

# The two REAL home paths this pass found and fixed -- narrower than a
# blanket "C:\Users\<anything>"/"/home/<anything>" check on purpose: this
# tree's own tests legitimately build dozens of `C:\Users\example\...`-
# style FAKE fixture paths, and `/home/kali` alone is also used,
# legitimately, as a generic Kali-Linux-default-username example (that
# installer's own default account name, not by itself identifying) --
# neither of those is what leaked. What leaked was the owner's real
# account name in that position.
_REAL_HOME_PATH_RE = re.compile(r"(C:\\Users\\rolo\b|C:/Users/rolo\b|/home/kali/Documents\b)")

# The exact machine/project/hobby-gear/work-tool names this pass removed
# from tests/helpers/fake_home.py, tests/test_mcp_catalog.py,
# docs/harness/ACCEPTANCE-2026-09-25.md and docs/harness/INSTALL.md --
# plain substrings, case-sensitive (each is distinctive enough that a
# false positive would need the exact same wording back).
_REMOVED_NAMES = (
    "roundBoy",
    "project_plex_server",
    "Moog Subsequent 37",
    "Superior Drummer 3",
    "vibe-hacking",
    "mcp-kali-server",
    "caido-mcp-extended",
    "_REAL_TOOL_CATALOG",
    "ableton-console servers",
    "~/Documents/vibes/appDev/Halo-Harness",
)

# Release review finding 38 / W6b section C: a broader, case-INSENSITIVE
# safety net alongside `_REMOVED_NAMES` above -- generic substrings
# rather than exact multi-word strings, so a differently-worded FUTURE
# mention of the same hobby gear/vendor names, or of the owner's own
# first/last name, is still caught (the exact-string check above only
# ever catches the SPECIFIC wording this pass already found and fixed).
# Scoped to exactly the directories/files the brief names -- deliberately
# NOT the whole tree the other checks in this file scan: the repo's own
# LICENSE file legitimately carries the owner's real full name in its
# copyright line (expected, standard, not a leak) and must never be
# flagged by this.
_EXTENDED_SCAN_TERMS = ("ableton", "moog", "sd3", "superior drummer", "caido", "roundboy", "lowery", "robert")
_EXTENDED_SCAN_ROOTS = ("halo_harness/", "tests/", "docs/", "README.md", "CHANGELOG.md")


def _tracked_files() -> "list[Path]":
    """`git ls-files` PLUS `git ls-files --others --exclude-standard` (2.0.3
    round 4 fix-pass note: a worker's own NEW, still-untracked files went
    unscanned until staged -- two "Bearer <fake-token>" literals in a new
    test slipped through the worker's own full run this way and only
    surfaced at review) in a git checkout; otherwise (the tar copy the
    Kali suite runs from, a source archive) a walk of the tree minus the
    directories git never tracks, so the scan runs on every copy the
    suites run from instead of erroring out where there is no `.git`."""
    # This file names every banned string, so it is the one tracked file
    # the scan must never read (untracked while it was written, which hid
    # the self-match until the first git-less run on the Kali copy).
    self_path = Path(__file__).resolve()
    try:
        tracked = subprocess.run(["git", "ls-files"], capture_output=True, text=True, cwd=str(REPO_DIR), check=True)
        untracked = subprocess.run(["git", "ls-files", "--others", "--exclude-standard"],
                                    capture_output=True, text=True, cwd=str(REPO_DIR), check=True)
        lines = list(dict.fromkeys(tracked.stdout.splitlines() + untracked.stdout.splitlines()))
        return [REPO_DIR / line for line in lines if line.strip() and (REPO_DIR / line).resolve() != self_path]
    except (OSError, subprocess.CalledProcessError):
        pass
    skip_dirs = {".git", "__pycache__", ".venv", "venv", "build", "dist", "wheels", "node_modules",
                 ".pytest_cache", ".mypy_cache", ".ruff_cache", ".tox", ".eggs"}
    files: "list[Path]" = []
    for root, dirs, names in os.walk(REPO_DIR):
        dirs[:] = sorted(d for d in dirs if d not in skip_dirs and not d.endswith(".egg-info"))
        for name in names:
            if name.endswith((".pyc", ".pyo")):
                continue
            candidate = Path(root) / name
            if candidate.resolve() == self_path:
                continue
            files.append(candidate)
    return files


def _load_allowlist() -> "set[str]":
    if not ALLOWLIST_PATH.exists():
        return set()
    allowed = set()
    for line in ALLOWLIST_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        allowed.add(line)
    return allowed


# Binary content a text scan should never try to decode -- an SVG
# snapshot's own normalized content is still plain text and IS scanned (a
# real leak could hide there too); these genuinely aren't text.
_SKIP_SUFFIXES = (".png", ".jpg", ".jpeg", ".gif", ".ico", ".pdf", ".ttf", ".woff", ".woff2", ".whl", ".zip")


def _read_text(path: Path) -> Optional[str]:
    if path.suffix.lower() in _SKIP_SUFFIXES or not path.is_file():
        return None
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return None


@test
def test_no_lan_addresses_anywhere_in_the_tracked_tree(ctx: Ctx):
    problems = []
    for path in _tracked_files():
        text = _read_text(path)
        if not text:
            continue
        for m in _LAN_IP_RE.finditer(text):
            line_no = text.count("\n", 0, m.start()) + 1
            problems.append(f"{path.relative_to(REPO_DIR)}:{line_no}: LAN address {m.group(0)!r}")
    ctx.check("no LAN addresses found:\n  " + "\n  ".join(problems), not problems)


@test
def test_no_real_home_paths_anywhere_in_the_tracked_tree(ctx: Ctx):
    problems = []
    for path in _tracked_files():
        text = _read_text(path)
        if not text:
            continue
        for m in _REAL_HOME_PATH_RE.finditer(text):
            line_no = text.count("\n", 0, m.start()) + 1
            problems.append(f"{path.relative_to(REPO_DIR)}:{line_no}: real home path {m.group(0)!r}")
    ctx.check("no real home paths found:\n  " + "\n  ".join(problems), not problems)


@test
def test_no_removed_machine_or_project_names_regress(ctx: Ctx):
    problems = []
    for path in _tracked_files():
        text = _read_text(path)
        if not text:
            continue
        for name in _REMOVED_NAMES:
            idx = text.find(name)
            if idx == -1:
                continue
            line_no = text.count("\n", 0, idx) + 1
            problems.append(f"{path.relative_to(REPO_DIR)}:{line_no}: removed name {name!r} is back")
    ctx.check("no removed machine/project names regressed:\n  " + "\n  ".join(problems), not problems)


def _extended_scan_files() -> "list[Path]":
    out: "list[Path]" = []
    for path in _tracked_files():
        try:
            rel = path.resolve().relative_to(REPO_DIR).as_posix()
        except ValueError:
            continue
        if any(rel == root or rel.startswith(root) for root in _EXTENDED_SCAN_ROOTS):
            out.append(path)
    return out


@test
def test_no_hobby_gear_vendor_or_owner_name_terms_case_insensitive(ctx: Ctx):
    """Release review finding 38 / W6b section C: a broader net than
    `_REMOVED_NAMES` above -- case-insensitive, generic substrings for the
    owner's hobby gear/vendor names and the owner's own first/last name,
    across halo_harness/, tests/, docs/, README.md and CHANGELOG.md (never
    the whole tree -- see `_EXTENDED_SCAN_TERMS`'s own docstring for why
    LICENSE is deliberately excluded)."""
    problems = []
    for path in _extended_scan_files():
        text = _read_text(path)
        if not text:
            continue
        lower = text.lower()
        for term in _EXTENDED_SCAN_TERMS:
            idx = lower.find(term)
            while idx != -1:
                line_no = text.count("\n", 0, idx) + 1
                problems.append(f"{path.relative_to(REPO_DIR)}:{line_no}: {term!r}-shaped term found: "
                                 f"{text[idx:idx + len(term)]!r}")
                idx = lower.find(term, idx + 1)
    ctx.check("no hobby-gear/vendor/owner-name terms found:\n  " + "\n  ".join(problems), not problems)


@test
def test_no_key_shaped_fragments_outside_the_allowlist(ctx: Ctx):
    allowed = _load_allowlist()
    ctx.check("the allowlist file itself exists", ALLOWLIST_PATH.exists())
    problems = []
    for path in _tracked_files():
        if path == ALLOWLIST_PATH:
            continue  # its own exact fake values would obviously "match"
        text = _read_text(path)
        if not text:
            continue
        for pat in _TOKEN_PATTERNS:
            for m in pat.finditer(text):
                if m.group(0) in allowed:
                    continue
                line_no = text.count("\n", 0, m.start()) + 1
                problems.append(f"{path.relative_to(REPO_DIR)}:{line_no}: key-shaped fragment "
                                 f"{m.group(0)!r} (not in tests/privacy_scan_allowlist.txt)")
        for m in _BEARER_RE.finditer(text):
            if m.group(0) in allowed:
                continue
            line_no = text.count("\n", 0, m.start()) + 1
            problems.append(f"{path.relative_to(REPO_DIR)}:{line_no}: bearer-token-shaped fragment "
                             f"{m.group(0)!r} (not in tests/privacy_scan_allowlist.txt)")
    ctx.check("no un-allowlisted key-shaped fragments found:\n  " + "\n  ".join(problems), not problems)


if __name__ == "__main__":
    ctx = Ctx()
    results, passed, failed, skipped = run_all(TESTS, ctx)
    sys.exit(print_results(results, passed, failed, skipped))
