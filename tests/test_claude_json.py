"""tests.test_claude_json -- config/claude_json.py: finding 5's trust walk
(git-toplevel-bounded when inside a repo, filesystem-root-bounded
otherwise -- not just cwd itself), CLAUDE_CODE_SANDBOXED accepting ANY
non-empty value, and our own trust.json fallback.
"""
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.helpers.runner import Ctx, new_registry, print_results, run_all
from halo_harness.config.claude_json import is_trusted, load_bridge_trust, load_claude_json

test, TESTS = new_registry()


@test
def test_load_claude_json_reads_a_bom_prefixed_file(ctx: Ctx):
    """Linux/H4 must-do: `mcp add`/`mcp remove` PRESERVE a BOM the file
    already had (mcp_cli.py's own byte-exact contract), but the old plain
    `utf-8` read chokes on the BOM bytes and silently returns {} for an
    otherwise perfectly valid file -- every configured server/project
    vanishes. `utf-8-sig` must strip the BOM and parse normally."""
    with tempfile.TemporaryDirectory() as td:
        saved = _clean_env("BRIDGE_TEST_HOME", "CLAUDE_CONFIG_DIR")
        os.environ["BRIDGE_TEST_HOME"] = td
        try:
            path = Path(td) / ".claude.json"
            path.write_bytes(b"\xef\xbb\xbf" + json.dumps({"mcpServers": {"x": {"command": "y"}}}).encode("utf-8"))
            data = load_claude_json()
            ctx.check(f"BOM'd file still parses, got {data}", data.get("mcpServers", {}).get("x", {}).get("command") == "y")
        finally:
            _restore_env(saved)


@test
def test_load_claude_json_still_reads_a_plain_utf8_file(ctx: Ctx):
    """No regression: a file with NO BOM (the common case) still reads
    identically under `utf-8-sig` as it did under plain `utf-8`."""
    with tempfile.TemporaryDirectory() as td:
        saved = _clean_env("BRIDGE_TEST_HOME", "CLAUDE_CONFIG_DIR")
        os.environ["BRIDGE_TEST_HOME"] = td
        try:
            path = Path(td) / ".claude.json"
            path.write_text(json.dumps({"mcpServers": {"a": {"command": "b"}}}), encoding="utf-8")
            data = load_claude_json()
            ctx.check(f"plain utf-8 file still parses, got {data}", data.get("mcpServers", {}).get("a", {}).get("command") == "b")
        finally:
            _restore_env(saved)


def _clean_env(*names):
    saved = {n: os.environ.pop(n, None) for n in names}
    return saved


def _restore_env(saved: dict):
    for k, v in saved.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


@test
def test_trust_from_a_non_git_child_of_a_trusted_dir(ctx: Ctx):
    """finding 5: outside a git repo, the walk goes all the way to the
    filesystem root, not just cwd -- a folder trusted at its own level must
    still read as trusted from a child directory with no .git anywhere."""
    root = Path(tempfile.mkdtemp(prefix="trust-nongit-"))
    child = root / "sub" / "deeper"
    child.mkdir(parents=True, exist_ok=True)
    claude_json = {"projects": {str(root).replace("\\", "/"): {"hasTrustDialogAccepted": True}}}
    ctx.check("child of a trusted non-git dir is trusted", is_trusted(child, claude_json, bridge_trust={}) is True)
    ctx.check("an unrelated dir is not trusted", is_trusted(Path(tempfile.mkdtemp(prefix="trust-unrelated-")), claude_json, bridge_trust={}) is False)


@test
def test_trust_walk_bounded_by_git_toplevel(ctx: Ctx):
    """Inside a repo, the walk stops at the git toplevel -- a directory
    trusted ABOVE the repo root must NOT leak trust into the repo (finding
    5 ports claude.exe's own git-root-bounded walk)."""
    root = Path(tempfile.mkdtemp(prefix="trust-git-"))
    repo = root / "myrepo"
    (repo / ".git").mkdir(parents=True, exist_ok=True)
    sub = repo / "src" / "pkg"
    sub.mkdir(parents=True, exist_ok=True)

    # Trust recorded on `root` (ABOVE the git toplevel) must not apply inside the repo.
    cj_above = {"projects": {str(root).replace("\\", "/"): {"hasTrustDialogAccepted": True}}}
    ctx.check("trust above the git toplevel does not leak into the repo", is_trusted(sub, cj_above, bridge_trust={}) is False)

    # Trust recorded on the repo toplevel itself DOES apply to a subdirectory inside it.
    cj_at_root = {"projects": {str(repo).replace("\\", "/"): {"hasTrustDialogAccepted": True}}}
    ctx.check("trust at the git toplevel applies to a subdirectory inside it", is_trusted(sub, cj_at_root, bridge_trust={}) is True)


@test
def test_sandboxed_env_any_nonempty_value_counts(ctx: Ctx):
    saved = _clean_env("CLAUDE_CODE_SANDBOXED")
    try:
        untrusted_dir = Path(tempfile.mkdtemp(prefix="trust-sandboxed-"))
        os.environ["CLAUDE_CODE_SANDBOXED"] = "true"  # NOT the literal "1" -- finding 5
        ctx.check('CLAUDE_CODE_SANDBOXED="true" counts as trusted', is_trusted(untrusted_dir, {}, bridge_trust={}) is True)
        os.environ["CLAUDE_CODE_SANDBOXED"] = ""
        ctx.check("an EMPTY CLAUDE_CODE_SANDBOXED does not count", is_trusted(untrusted_dir, {}, bridge_trust={}) is False)
    finally:
        _restore_env(saved)


@test
def test_bridge_trust_fallback_loaded_when_none_passed(ctx: Ctx):
    """assemble.py calls is_trusted(cwd, claude_json) with NO bridge_trust
    arg -- must self-load from ~/.halo/trust.json rather than
    silently treating that as an empty/untrusted dict (finding 5)."""
    home_dir = Path(tempfile.mkdtemp(prefix="trust-bridgehome-"))
    saved = _clean_env("BRIDGE_TEST_HOME", "BRIDGE_STATE_DIR", "CLAUDE_CODE_SANDBOXED")
    try:
        os.environ["BRIDGE_TEST_HOME"] = str(home_dir)
        target = Path(tempfile.mkdtemp(prefix="trust-bridgehome-target-"))
        from halo_harness.config.paths import normalize_cwd
        trust_path = home_dir / ".halo" / "trust.json"
        trust_path.parent.mkdir(parents=True, exist_ok=True)
        trust_path.write_text(json.dumps({normalize_cwd(target): True}), encoding="utf-8")

        ctx.check("bridge_trust=None self-loads trust.json", is_trusted(target, {}) is True)
        ctx.check("load_bridge_trust() reads the same file directly", load_bridge_trust().get(normalize_cwd(target)) is True)
    finally:
        _restore_env(saved)


if __name__ == "__main__":
    ctx = Ctx()
    results, passed, failed, skipped = run_all(TESTS, ctx)
    sys.exit(print_results(results, passed, failed, skipped))
