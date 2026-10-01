"""tests.test_docs_hygiene -- H14b brief: the repo goes public right after
this pass, so docs/, README.md, CHANGELOG.md and team.example.json must
carry no real hostname, LAN address, username, home path, or token/key
fragment. Pure text scan -- no subprocess, no network.

Deliberately does NOT ban the bare word "rolo" (the product's own name --
"halo"/"halo_harness"/"~/.halo" -- appears everywhere on
purpose); the real leak vector on this box is a pasted command-output path
like `C:\\Users\\<name>\\...`, which the home-path check below catches
directly regardless of what the account happens to be called.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.helpers.runner import Ctx, new_registry, print_results, run_all

REPO_DIR = Path(__file__).resolve().parent.parent
test, TESTS = new_registry()

# Every file this pass promised to keep clean.
_TARGET_FILES = ["README.md", "CHANGELOG.md", "team.example.json"]


def _doc_files() -> list:
    docs = sorted((REPO_DIR / "docs").rglob("*.md"))
    return docs + [REPO_DIR / f for f in _TARGET_FILES if (REPO_DIR / f).exists()]


# ---- home paths: the docs must say `~` (or `%USERPROFILE%`), never a real
# concrete account path -- this is also what makes a leaked username moot,
# since the leak vector is always "pasted straight from a command's output".
_HOME_PATH_RE = re.compile(r"(C:\\Users\\|C:/Users/|/home/[A-Za-z0-9_.-]|/Users/[A-Za-z0-9_.-])")

# ---- LAN addresses: RFC1918 private ranges.
_LAN_IP_RE = re.compile(
    r"\b(?:10\.\d{1,3}\.\d{1,3}\.\d{1,3}|"
    r"172\.(?:1[6-9]|2\d|3[0-1])\.\d{1,3}\.\d{1,3}|"
    r"192\.168\.\d{1,3}\.\d{1,3})\b"
)

# ---- real Databricks workspace hostnames: only the brief's own approved
# placeholder may appear; anything else matching a real Databricks domain
# suffix is a leaked workspace name.
_DATABRICKS_HOST_RE = re.compile(
    r"\b[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?\.(?:cloud\.databricks\.com|azuredatabricks\.net|gcp\.databricks\.com)\b"
)
_APPROVED_DATABRICKS_HOST = "your-workspace.cloud.databricks.com"

# ---- token/key fragments: the exact shapes halo_harness.export_cli already
# knows how to redact -- reused here rather than re-invented, so this test
# and the sanitizer it's checking against can never quietly drift apart.
from halo_harness.export_cli import _BEARER_RE, _TOKEN_PATTERNS  # noqa: E402


def _scan(path: Path) -> "list[str]":
    text = path.read_text(encoding="utf-8", errors="replace")
    problems: list = []

    for m in _HOME_PATH_RE.finditer(text):
        line_no = text.count("\n", 0, m.start()) + 1
        problems.append(f"{path.name}:{line_no}: home-path pattern {m.group(0)!r} -- use `~` instead")

    for m in _LAN_IP_RE.finditer(text):
        line_no = text.count("\n", 0, m.start()) + 1
        problems.append(f"{path.name}:{line_no}: LAN address {m.group(0)!r}")

    for m in _DATABRICKS_HOST_RE.finditer(text):
        if m.group(0) == _APPROVED_DATABRICKS_HOST:
            continue
        line_no = text.count("\n", 0, m.start()) + 1
        problems.append(f"{path.name}:{line_no}: real-looking Databricks host {m.group(0)!r} "
                         f"-- use {_APPROVED_DATABRICKS_HOST!r}")

    for pat in _TOKEN_PATTERNS:
        for m in pat.finditer(text):
            line_no = text.count("\n", 0, m.start()) + 1
            problems.append(f"{path.name}:{line_no}: token-shaped fragment {m.group(0)!r}")

    for m in _BEARER_RE.finditer(text):
        line_no = text.count("\n", 0, m.start()) + 1
        problems.append(f"{path.name}:{line_no}: bearer-token-shaped fragment {m.group(0)!r}")

    return problems


@test
def test_no_home_paths_lan_addresses_hostnames_or_tokens(ctx: Ctx):
    all_problems: list = []
    files = _doc_files()
    ctx.check("at least one doc file was found to scan", files)
    for path in files:
        all_problems.extend(_scan(path))
    ctx.check("no hygiene problems found:\n  " + "\n  ".join(all_problems), not all_problems)


@test
def test_docs_dir_has_the_expected_top_level_files(ctx: Ctx):
    """A cheap canary: if a required doc is simply missing, every OTHER
    docs test in this repo will fail in a confusing way -- fail here first,
    with a clear message, instead."""
    docs = REPO_DIR / "docs"
    expected = [
        "INSTALL.md", "COMMANDS.md", "SLASH-COMMANDS.md", "ARCHITECTURE.md", "CONFIG.md",
        "MODELS.md", "DATABRICKS.md", "TROUBLESHOOTING.md", "DEVELOPMENT.md",
    ]
    for name in expected:
        ctx.check(f"docs/{name} exists", (docs / name).is_file())
    ctx.check("docs/harness/README.md exists", (docs / "harness" / "README.md").is_file())


if __name__ == "__main__":
    ctx = Ctx()
    results, passed, failed, skipped = run_all(TESTS, ctx)
    sys.exit(print_results(results, passed, failed, skipped))
