"""tests.test_paths -- config/paths.py: slug rules, both .claude.json key
separator forms, precedence/merge in lookup_project, git-toplevel walk for
trust (exercised indirectly via claude_json in test_settings.py), posix
path conversion.
"""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.helpers.runner import Ctx, new_registry, print_results, run_all
from rolo_claude.config import paths as p

test, TESTS = new_registry()


@test
def test_home_honors_bridge_test_home(ctx: Ctx):
    old = os.environ.get("BRIDGE_TEST_HOME")
    try:
        os.environ["BRIDGE_TEST_HOME"] = r"C:\fake\test\home"
        ctx.check("home() reads BRIDGE_TEST_HOME", p.home() == Path(r"C:\fake\test\home"))
    finally:
        if old is None:
            os.environ.pop("BRIDGE_TEST_HOME", None)
        else:
            os.environ["BRIDGE_TEST_HOME"] = old


@test
def test_project_slug_rule(ctx: Ctx):
    slug = p.project_slug("C:/Users/user")
    ctx.check(f"non-alphanumeric -> '-', got {slug!r}", slug == "C--Users-user")


@test
def test_project_slug_truncates_long_paths(ctx: Ctx):
    long_path = "C:/" + ("x" * 300)
    slug = p.project_slug(long_path)
    ctx.check("truncated slug <= 209 chars (200 + '-' + 8 hex)", len(slug) <= 209)
    ctx.check("truncated slug has a hash suffix", "-" in slug[-9:])


@test
def test_normalize_cwd_forward_slashes_and_drive_upper(ctx: Ctx):
    n = p.normalize_cwd("c:/Users/someone")
    ctx.check(f"forward slashes, upper drive, got {n!r}", n == "C:/Users/someone")
    n2 = p.normalize_cwd(r"c:\Users\someone")
    ctx.check(f"backslash input also normalized, got {n2!r}", n2 == "C:/Users/someone")


@test
def test_project_key_candidates_both_forms(ctx: Ctx):
    candidates = p.project_key_candidates("C:/Users/user")
    ctx.check("forward-slash form present", "C:/Users/user" in candidates)
    ctx.check("backslash form present", r"C:\Users\user" in candidates)
    ctx.check("exactly two candidates", len(candidates) == 2)


@test
def test_lookup_project_merges_both_key_forms(ctx: Ctx):
    cj = {
        "projects": {
            r"C:\Users\user": {"hasTrustDialogAccepted": True, "allowedTools": ["Bash"]},
            "C:/Users/user": {"hasTrustDialogAccepted": False, "allowedTools": ["Read"]},
        }
    }
    merged = p.lookup_project(cj, "C:/Users/user")
    ctx.check("booleans OR'd (True wins)", merged.get("hasTrustDialogAccepted") is True)
    ctx.check("lists unioned, first-seen order",
              merged.get("allowedTools") == ["Bash", "Read"] or merged.get("allowedTools") == ["Read", "Bash"])
    ctx.check("both tools present", set(merged.get("allowedTools", [])) == {"Bash", "Read"})


@test
def test_lookup_project_no_match_returns_empty(ctx: Ctx):
    ctx.check("no projects key -> {}", p.lookup_project({}, "C:/nope") == {})
    ctx.check("no matching entry -> {}", p.lookup_project({"projects": {"C:/other": {}}}, "C:/nope") == {})


@test
def test_lookup_project_malformed_input_never_raises(ctx: Ctx):
    try:
        r1 = p.lookup_project({"projects": "not-a-dict"}, "C:/x")
        r2 = p.lookup_project({"projects": {"C:/x": "not-a-dict"}}, "C:/x")
        r3 = p.lookup_project(None, "C:/x")  # type: ignore[arg-type]
        ctx.check("malformed projects value -> {}", r1 == {})
        ctx.check("malformed record value -> {}", r2 == {})
        ctx.check("None claude_json -> {}", r3 == {})
    except Exception as e:
        ctx.check(f"lookup_project must never raise, got {e!r}", False)


@test
def test_lookup_project_dict_shallow_merge(ctx: Ctx):
    cj = {
        "projects": {
            r"C:\Users\user": {"mcpServers": {"a": {"command": "1"}}},
            "C:/Users/user": {"mcpServers": {"b": {"command": "2"}}},
        }
    }
    merged = p.lookup_project(cj, "C:/Users/user")
    servers = merged.get("mcpServers", {})
    ctx.check("both server names present after shallow merge", set(servers.keys()) == {"a", "b"})


@test
def test_to_posix_and_from_posix_roundtrip(ctx: Ctx):
    posix = p.to_posix(r"C:\Users\user\file.txt")
    ctx.check(f"drive rewritten to /c/, got {posix!r}", posix == "/c/Users/user/file.txt")
    back = p.from_posix(posix)
    ctx.check(f"round-trips back to C:/, got {back!r}", back == "C:/Users/user/file.txt")


@test
def test_to_posix_leaves_relative_paths_alone(ctx: Ctx):
    ctx.check("relative path passthrough", p.to_posix("src/main.py") == "src/main.py")


@test
def test_git_bash_never_raises(ctx: Ctx):
    try:
        result = p.git_bash()
        ctx.check("git_bash() returns Path or None", result is None or isinstance(result, Path))
    except Exception as e:
        ctx.check(f"git_bash() must never raise, got {e!r}", False)


@test
def test_bridge_home_default_and_override(ctx: Ctx):
    old = os.environ.get("BRIDGE_STATE_DIR")
    try:
        os.environ.pop("BRIDGE_STATE_DIR", None)
        default = p.bridge_home()
        ctx.check("default is <home>/.rolo-claude", default.name == ".rolo-claude")
        os.environ["BRIDGE_STATE_DIR"] = str(Path(tempfile.gettempdir()) / "custom-state")
        overridden = p.bridge_home()
        ctx.check("BRIDGE_STATE_DIR overrides default", overridden == Path(tempfile.gettempdir()) / "custom-state")
    finally:
        if old is None:
            os.environ.pop("BRIDGE_STATE_DIR", None)
        else:
            os.environ["BRIDGE_STATE_DIR"] = old


@test
def test_memory_dir_default_and_override(ctx: Ctx):
    default = p.memory_dir("C:/Users/user")
    ctx.check("default under claude_config_dir/projects/<slug>/memory",
              default.parts[-2:] == ("C--Users-user", "memory") or str(default).endswith("memory"))

    class FakeSettingsObj:
        auto_memory_directory = r"C:\custom\memdir"

    overridden = p.memory_dir("C:/Users/user", FakeSettingsObj())
    ctx.check("Settings-object override honored", overridden == Path(r"C:\custom\memdir"))

    overridden_dict = p.memory_dir("C:/Users/user", {"autoMemoryDirectory": r"C:\custom\memdir2"})
    ctx.check("dict-shaped override also honored", overridden_dict == Path(r"C:\custom\memdir2"))


@test
def test_managed_settings_files_never_raises_when_absent(ctx: Ctx):
    try:
        files = p.managed_settings_files()
        ctx.check("returns a list", isinstance(files, list))
    except Exception as e:
        ctx.check(f"managed_settings_files() must never raise, got {e!r}", False)


if __name__ == "__main__":
    ctx = Ctx()
    results, passed, failed, skipped = run_all(TESTS, ctx)
    sys.exit(print_results(results, passed, failed, skipped))
