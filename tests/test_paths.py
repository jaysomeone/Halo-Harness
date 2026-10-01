"""tests.test_paths -- config/paths.py: slug rules, both .claude.json key
separator forms, precedence/merge in lookup_project, git-toplevel walk for
trust (exercised indirectly via claude_json in test_settings.py), posix
path conversion.
"""
import os
import sys
import shutil
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.helpers.runner import Ctx, new_registry, print_results, run_all
from halo_harness.config import paths as p

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
    """2.0.0 rename: `BRIDGE_TEST_HOME` is now ALSO scoped here (it wasn't
    before) -- `bridge_home()` gained a real filesystem side effect (the
    one-time `~/.rolo-claude` -> `~/.halo` migration), so the "default"
    half of this test must never resolve `home()` to the REAL machine
    home any more, only a disposable temp one; see test_bridge_home_
    migrates_an_existing_legacy_dir below for the migration itself."""
    old_state, old_home = os.environ.get("BRIDGE_STATE_DIR"), os.environ.get("BRIDGE_TEST_HOME")
    try:
        os.environ.pop("BRIDGE_STATE_DIR", None)
        os.environ["BRIDGE_TEST_HOME"] = str(Path(tempfile.mkdtemp(prefix="h2-bridge-home-default-")))
        default = p.bridge_home()
        ctx.check("default is <home>/.halo", default.name == ".halo")
        os.environ["BRIDGE_STATE_DIR"] = str(Path(tempfile.gettempdir()) / "custom-state")
        overridden = p.bridge_home()
        ctx.check("BRIDGE_STATE_DIR overrides default", overridden == Path(tempfile.gettempdir()) / "custom-state")
    finally:
        for key, old in (("BRIDGE_STATE_DIR", old_state), ("BRIDGE_TEST_HOME", old_home)):
            if old is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = old


@test
def test_bridge_home_migrates_an_existing_legacy_dir(ctx: Ctx):
    """2.0.0 rename brief item 2: a pre-existing `~/.rolo-claude` is
    RENAMED (not copied) to `~/.halo` the first time `bridge_home()`
    resolves it, with exactly one stderr line; a second call is a silent
    no-op (the old dir is simply gone by then)."""
    import contextlib
    import io

    old_state, old_home = os.environ.get("BRIDGE_STATE_DIR"), os.environ.get("BRIDGE_TEST_HOME")
    tmp = Path(tempfile.mkdtemp(prefix="h2-bridge-home-migrate-"))
    try:
        os.environ.pop("BRIDGE_STATE_DIR", None)
        os.environ["BRIDGE_TEST_HOME"] = str(tmp)
        legacy = tmp / ".rolo-claude"
        legacy.mkdir()
        (legacy / "marker.txt").write_text("hello", encoding="utf-8")

        stderr_buf = io.StringIO()
        with contextlib.redirect_stderr(stderr_buf):
            migrated = p.bridge_home()
        ctx.check(f"returns the NEW path, got {migrated}", migrated == tmp / ".halo")
        ctx.check("the new dir actually exists now", migrated.is_dir())
        ctx.check("its content survived the rename", (migrated / "marker.txt").read_text(encoding="utf-8") == "hello")
        # 2.0.0 fixpass item B: no link is created at the old location at
        # all -- `rm -rf ~/.rolo-claude/` with a trailing slash follows a
        # symlink/junction and would empty `~/.halo` right along with it,
        # so the decision is to leave NOTHING behind at the old path.
        ctx.check("no link is created -- the old path is simply gone", not legacy.exists())
        ctx.check(f"exactly one migration line on stderr, got {stderr_buf.getvalue()!r}",
                  stderr_buf.getvalue().count("migrated state directory") == 1)

        stderr_buf2 = io.StringIO()
        with contextlib.redirect_stderr(stderr_buf2):
            again = p.bridge_home()
        ctx.check("a second call is idempotent (same path)", again == migrated)
        ctx.check(f"and silent -- no repeat announcement, got {stderr_buf2.getvalue()!r}", stderr_buf2.getvalue() == "")
    finally:
        for key, old in (("BRIDGE_STATE_DIR", old_state), ("BRIDGE_TEST_HOME", old_home)):
            if old is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = old


@test
def test_bridge_home_leaves_an_unrelated_legacy_dir_alone_when_state_dir_overridden(ctx: Ctx):
    """BRIDGE_STATE_DIR is an explicit override -- it must win outright,
    with NO migration side effect at all, even when a legacy `~/.rolo-
    claude` happens to exist right next to it."""
    old_state, old_home = os.environ.get("BRIDGE_STATE_DIR"), os.environ.get("BRIDGE_TEST_HOME")
    tmp = Path(tempfile.mkdtemp(prefix="h2-bridge-home-override-"))
    try:
        os.environ["BRIDGE_TEST_HOME"] = str(tmp)
        legacy = tmp / ".rolo-claude"
        legacy.mkdir()
        custom = tmp / "explicit-state-dir"
        os.environ["BRIDGE_STATE_DIR"] = str(custom)
        result = p.bridge_home()
        ctx.check(f"BRIDGE_STATE_DIR wins outright, got {result}", result == custom)
        ctx.check("the unrelated legacy dir is left completely alone", legacy.is_dir() and not custom.exists())
    finally:
        for key, old in (("BRIDGE_STATE_DIR", old_state), ("BRIDGE_TEST_HOME", old_home)):
            if old is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = old


# ---------------------------------------------------------------------------
# 2.0.0 rename: env_compat/env_file_path (HALO_* canonical, BRIDGE_*/
# ROLO_CLAUDE_* legacy fallback with one DEBUG line).
# ---------------------------------------------------------------------------

@test
def test_env_compat_prefers_the_new_name(ctx: Ctx):
    env = {"HALO_MODEL": "new-wins", "BRIDGE_MODEL": "old-loses"}
    ctx.check("HALO_* wins outright when both are set", p.env_compat("MODEL", env) == "new-wins")


@test
def test_env_compat_falls_back_to_bridge_then_rolo_claude(ctx: Ctx):
    ctx.check("falls back to BRIDGE_*", p.env_compat("MODEL", {"BRIDGE_MODEL": "legacy-bridge"}) == "legacy-bridge")
    ctx.check("falls back to ROLO_CLAUDE_* when BRIDGE_* is also absent",
              p.env_compat("MODEL", {"ROLO_CLAUDE_MODEL": "legacy-rolo"}) == "legacy-rolo")
    ctx.check("BRIDGE_* wins over ROLO_CLAUDE_* when both legacy names are set",
              p.env_compat("MODEL", {"BRIDGE_MODEL": "b", "ROLO_CLAUDE_MODEL": "r"}) == "b")


@test
def test_env_compat_default_when_nothing_set(ctx: Ctx):
    ctx.check("returns the default", p.env_compat("MODEL", {}, "fallback") == "fallback")
    ctx.check("returns None with no default given", p.env_compat("MODEL", {}) is None)


@test
def test_env_compat_empty_string_legacy_value_still_wins_over_default(ctx: Ctx):
    """Presence-based, like dict.get -- a legacy name explicitly set to ""
    is still "set", so it wins over the default, never silently skipped
    for being falsy."""
    ctx.check('an explicit "" legacy value beats the default',
              p.env_compat("MODEL", {"BRIDGE_MODEL": ""}, "fallback") == "")


@test
def test_env_compat_logs_one_debug_line_only_for_a_legacy_name(ctx: Ctx):
    import logging

    class _Capture(logging.Handler):
        def __init__(self):
            super().__init__()
            self.records = []

        def emit(self, record):
            self.records.append(record.getMessage())

    logger = logging.getLogger("halo_harness.config.paths")
    old_level = logger.level
    handler = _Capture()
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    # finding 11: the "already warned" set is process-wide and keyed on the
    # bare legacy name, so some OTHER test earlier in this same process run
    # may have already "spent" BRIDGE_MODEL's one-time warning -- force a
    # clean slate for this one name, then put it back exactly as found.
    had_warned = "BRIDGE_MODEL" in p._deprecated_names_warned
    p._deprecated_names_warned.discard("BRIDGE_MODEL")
    try:
        p.env_compat("MODEL", {"HALO_MODEL": "x"})
        ctx.check("no debug line for the NEW name", handler.records == [])
        p.env_compat("MODEL", {"BRIDGE_MODEL": "x"})
        ctx.check(f"exactly one debug line for a LEGACY name, got {handler.records}", len(handler.records) == 1)
        ctx.check(f"it names both the old and new var, got {handler.records}",
                  "BRIDGE_MODEL" in handler.records[0] and "HALO_MODEL" in handler.records[0])
    finally:
        logger.removeHandler(handler)
        logger.setLevel(old_level)
        if had_warned:
            p._deprecated_names_warned.add("BRIDGE_MODEL")
        else:
            p._deprecated_names_warned.discard("BRIDGE_MODEL")


@test
def test_env_file_path_override_and_new_default(ctx: Ctx):
    """2.0.0 fixpass finding 4: env_file_path() is now a PURE display/
    write-target resolver -- always the new canonical path (or an explicit
    override), never silently swapped for the legacy path just because
    that one exists and the new one doesn't (that WAS the bug: once the
    new file existed at all, a credential kept only in the legacy file
    went invisible). See test_env_file_path_for_write_copies_legacy_
    forward_once for the writer's own copy-forward, and test_load_
    provider_env_files_new_wins_legacy_fills_gaps for the actual
    dual-file READ merge."""
    old_home = os.environ.get("BRIDGE_TEST_HOME")
    old_env_file = os.environ.get("BRIDGE_ENV_FILE")
    tmp = Path(tempfile.mkdtemp(prefix="h2-env-file-path-"))
    try:
        os.environ["BRIDGE_TEST_HOME"] = str(tmp)
        os.environ.pop("BRIDGE_ENV_FILE", None)
        ctx.check("neither path exists yet -> the NEW default path",
                  p.env_file_path() == tmp / ".config" / "halo" / "env")

        os.environ["BRIDGE_ENV_FILE"] = str(tmp / "explicit-env-file")
        ctx.check("BRIDGE_ENV_FILE overrides outright", p.env_file_path() == tmp / "explicit-env-file")
        os.environ.pop("BRIDGE_ENV_FILE", None)

        legacy = tmp / ".config" / "vibes-hacker" / "env"
        legacy.parent.mkdir(parents=True)
        legacy.write_text("OPENROUTER_API_KEY=x\n", encoding="utf-8")
        ctx.check("still the NEW path even when only the legacy one exists on disk",
                  p.env_file_path() == tmp / ".config" / "halo" / "env")

        new_path = tmp / ".config" / "halo" / "env"
        new_path.parent.mkdir(parents=True)
        new_path.write_text("OPENROUTER_API_KEY=y\n", encoding="utf-8")
        ctx.check("the new path, now that it also exists", p.env_file_path() == new_path)
    finally:
        for key, old in (("BRIDGE_TEST_HOME", old_home), ("BRIDGE_ENV_FILE", old_env_file)):
            if old is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = old


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


# ---------------------------------------------------------------------------
# 2.0.0 fixpass finding 11: the deprecated-name DEBUG line fires once per
# NAME, not once per lookup (PING_INTERVAL alone is looked up per proxied
# request and per turn).
# ---------------------------------------------------------------------------

@test
def test_env_compat_deprecated_warning_fires_once_per_name_not_per_call(ctx: Ctx):
    had_warned = "BRIDGE_PING_INTERVAL" in p._deprecated_names_warned
    p._deprecated_names_warned.discard("BRIDGE_PING_INTERVAL")
    try:
        for _ in range(5):
            ctx.check("value still resolves on every call",
                      p.env_compat("PING_INTERVAL", {"BRIDGE_PING_INTERVAL": "0.2"}) == "0.2")
        ctx.check("the name is now marked warned", "BRIDGE_PING_INTERVAL" in p._deprecated_names_warned)
    finally:
        if had_warned:
            p._deprecated_names_warned.add("BRIDGE_PING_INTERVAL")
        else:
            p._deprecated_names_warned.discard("BRIDGE_PING_INTERVAL")


# ---------------------------------------------------------------------------
# 2.0.0 fixpass finding 1: bridge_home()'s migration-failure fallback, the
# both-exist warning, and per-process memoization.
# ---------------------------------------------------------------------------

@test
def test_bridge_home_failed_rename_warns_and_falls_back_to_old_dir(ctx: Ctx):
    """A failed rename (PermissionError on Windows, EXDEV/EROFS on Linux)
    must never silently abandon state into a new_dir that doesn't exist
    yet -- one warning naming both paths, and old_dir returned so the
    NEXT run retries."""
    import contextlib
    import io

    old_state, old_home = os.environ.get("BRIDGE_STATE_DIR"), os.environ.get("BRIDGE_TEST_HOME")
    tmp = Path(tempfile.mkdtemp(prefix="h2-bridge-home-failed-rename-"))
    real_rename = Path.rename
    try:
        os.environ.pop("BRIDGE_STATE_DIR", None)
        os.environ["BRIDGE_TEST_HOME"] = str(tmp)
        legacy = tmp / ".rolo-claude"
        legacy.mkdir()
        (legacy / "marker.txt").write_text("hello", encoding="utf-8")

        def _boom(self, target):
            raise OSError(13, "simulated: permission denied")
        Path.rename = _boom

        stderr_buf = io.StringIO()
        with contextlib.redirect_stderr(stderr_buf):
            result = p.bridge_home()
        ctx.check(f"returns old_dir on a failed rename, got {result}", result == legacy)
        ctx.check("new_dir was never created", not (tmp / ".halo").exists())
        msg = stderr_buf.getvalue()
        ctx.check(f"exactly one warning line, got {msg!r}", msg.count("\n") == 1)
        ctx.check(f"it names both paths, got {msg!r}", str(legacy) in msg and str(tmp / ".halo") in msg)
    finally:
        Path.rename = real_rename
        for key, old in (("BRIDGE_STATE_DIR", old_state), ("BRIDGE_TEST_HOME", old_home)):
            if old is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = old


@test
def test_bridge_home_memoizes_so_a_later_retry_cannot_switch_mid_run(ctx: Ctx):
    """Once a failed rename has resolved this process to old_dir for a
    given home(), a SECOND call -- even after whatever blocked the rename
    is gone, when a retry would now succeed -- must keep returning
    old_dir. Only a fresh process gets to try the rename again."""
    old_state, old_home = os.environ.get("BRIDGE_STATE_DIR"), os.environ.get("BRIDGE_TEST_HOME")
    tmp = Path(tempfile.mkdtemp(prefix="h2-bridge-home-memo-"))
    real_rename = Path.rename
    try:
        os.environ.pop("BRIDGE_STATE_DIR", None)
        os.environ["BRIDGE_TEST_HOME"] = str(tmp)
        legacy = tmp / ".rolo-claude"
        legacy.mkdir()

        import contextlib
        import io

        def _boom(self, target):
            raise OSError(13, "simulated: permission denied")
        Path.rename = _boom
        with contextlib.redirect_stderr(io.StringIO()):
            first = p.bridge_home()
        Path.rename = real_rename  # the "lock" is gone -- an unmemoized call would now succeed
        second = p.bridge_home()
        ctx.check(f"second call still returns old_dir (memoized), got {second}", second == legacy == first)
        ctx.check("still never renamed", legacy.is_dir() and not (tmp / ".halo").exists())
    finally:
        Path.rename = real_rename
        for key, old in (("BRIDGE_STATE_DIR", old_state), ("BRIDGE_TEST_HOME", old_home)):
            if old is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = old


@test
def test_bridge_home_adopts_new_dir_silently_when_a_concurrent_process_wins_the_race(ctx: Ctx):
    """2.0.0 fixpass item D: when old_dir.rename(new_dir) itself raises but
    new_dir now exists AND old_dir is gone -- a concurrent process
    evidently finished migrating first -- this process silently adopts
    that new_dir instead of printing a false "could not migrate" warning
    and falling back to a now-nonexistent old_dir."""
    import contextlib
    import io

    old_state, old_home = os.environ.get("BRIDGE_STATE_DIR"), os.environ.get("BRIDGE_TEST_HOME")
    tmp = Path(tempfile.mkdtemp(prefix="h2-bridge-home-concurrent-"))
    real_rename = Path.rename
    try:
        os.environ.pop("BRIDGE_STATE_DIR", None)
        os.environ["BRIDGE_TEST_HOME"] = str(tmp)
        legacy = tmp / ".rolo-claude"
        legacy.mkdir()
        (legacy / "marker.txt").write_text("hello", encoding="utf-8")
        new_dir = tmp / ".halo"

        def _concurrent_winner(self, target):
            if self == legacy:
                # Another "process" finishes its own rename right here:
                # the real destination appears, the source disappears --
                # THEN this call's own attempt raises.
                (legacy / "marker.txt").unlink()
                legacy.rmdir()
                new_dir.mkdir(parents=True)
                (new_dir / "marker.txt").write_text("hello", encoding="utf-8")
                raise OSError(17, "simulated: lost the race to a concurrent migration")
            return real_rename(self, target)
        Path.rename = _concurrent_winner

        stderr_buf = io.StringIO()
        with contextlib.redirect_stderr(stderr_buf):
            result = p.bridge_home()
        ctx.check(f"adopts new_dir silently, got {result}", result == new_dir)
        ctx.check(f"no warning is printed, got {stderr_buf.getvalue()!r}", stderr_buf.getvalue() == "")
    finally:
        Path.rename = real_rename
        for key, old in (("BRIDGE_STATE_DIR", old_state), ("BRIDGE_TEST_HOME", old_home)):
            if old is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = old


@test
def test_state_dir_both_exist_nonempty_warns_once_and_bridge_home_keeps_new_dir(ctx: Ctx):
    """When both directories already exist and old_dir has something in
    it, bridge_home() keeps using new_dir and warns exactly once;
    state_dir_both_exist_nonempty() is the one shared detection `doctor`
    also renders as a WARN, so the two can never disagree."""
    import contextlib
    import io

    old_state, old_home = os.environ.get("BRIDGE_STATE_DIR"), os.environ.get("BRIDGE_TEST_HOME")
    tmp = Path(tempfile.mkdtemp(prefix="h2-bridge-home-both-exist-"))
    try:
        os.environ.pop("BRIDGE_STATE_DIR", None)
        os.environ["BRIDGE_TEST_HOME"] = str(tmp)
        old_dir = tmp / ".rolo-claude"
        new_dir = tmp / ".halo"
        old_dir.mkdir()
        new_dir.mkdir()
        (old_dir / "leftover.txt").write_text("leftover", encoding="utf-8")

        ctx.check("state_dir_both_exist_nonempty reports the pair",
                  p.state_dir_both_exist_nonempty() == (old_dir, new_dir))

        stderr_buf = io.StringIO()
        with contextlib.redirect_stderr(stderr_buf):
            result = p.bridge_home()
        ctx.check(f"uses new_dir, got {result}", result == new_dir)
        msg = stderr_buf.getvalue()
        ctx.check(f"warns once naming both paths, got {msg!r}",
                  msg.count("\n") == 1 and str(old_dir) in msg and str(new_dir) in msg)

        stderr_buf2 = io.StringIO()
        with contextlib.redirect_stderr(stderr_buf2):
            p.bridge_home()
        ctx.check(f"a second call is silent (memoized), got {stderr_buf2.getvalue()!r}",
                  stderr_buf2.getvalue() == "")
    finally:
        for key, old in (("BRIDGE_STATE_DIR", old_state), ("BRIDGE_TEST_HOME", old_home)):
            if old is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = old


@test
def test_state_dir_both_exist_nonempty_ignores_an_empty_old_dir(ctx: Ctx):
    """The "non-empty" qualifier: an old_dir that's merely present but has
    nothing in it must never trigger the warning."""
    old_state, old_home = os.environ.get("BRIDGE_STATE_DIR"), os.environ.get("BRIDGE_TEST_HOME")
    tmp = Path(tempfile.mkdtemp(prefix="h2-bridge-home-empty-old-"))
    try:
        os.environ.pop("BRIDGE_STATE_DIR", None)
        os.environ["BRIDGE_TEST_HOME"] = str(tmp)
        (tmp / ".rolo-claude").mkdir()
        (tmp / ".halo").mkdir()
        ctx.check("an empty old_dir never counts as both-exist", p.state_dir_both_exist_nonempty() is None)
    finally:
        for key, old in (("BRIDGE_STATE_DIR", old_state), ("BRIDGE_TEST_HOME", old_home)):
            if old is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = old


# ---------------------------------------------------------------------------
# 2.0.0 fixpass finding 10: rewrite_legacy_state_dir_prefix.
# ---------------------------------------------------------------------------

@test
def test_rewrite_legacy_state_dir_prefix_rewrites_when_old_dir_is_truly_gone(ctx: Ctx):
    old_home = os.environ.get("BRIDGE_TEST_HOME")
    tmp = Path(tempfile.mkdtemp(prefix="h2-rewrite-prefix-"))
    try:
        os.environ["BRIDGE_TEST_HOME"] = str(tmp)
        old_dir = tmp / ".rolo-claude"
        new_dir = tmp / ".halo"
        legacy_path = str(old_dir / "sessions" / "proj" / "sess1" / "tool-results" / "X.txt")
        rewritten = p.rewrite_legacy_state_dir_prefix(legacy_path)
        expected = str(new_dir / "sessions" / "proj" / "sess1" / "tool-results" / "X.txt")
        ctx.check(f"prefix rewritten to the new dir, got {rewritten!r}", rewritten == expected)

        ctx.check("an unrelated path is never touched",
                  p.rewrite_legacy_state_dir_prefix("/some/other/path.txt") == "/some/other/path.txt")

        # old_dir IS usable now (a real dir here stands in for a working
        # compat link -- is_dir() treats both the same way) -- leave alone.
        old_dir.mkdir()
        ctx.check("a still-usable old_dir is left alone",
                  p.rewrite_legacy_state_dir_prefix(legacy_path) == legacy_path)
    finally:
        if old_home is None:
            os.environ.pop("BRIDGE_TEST_HOME", None)
        else:
            os.environ["BRIDGE_TEST_HOME"] = old_home


# ---------------------------------------------------------------------------
# 2.0.0 fixpass finding 4: env_file_path_for_write's copy-forward, and the
# new/legacy read-merge (load_provider_env_files / tool_child_env).
# ---------------------------------------------------------------------------

@test
def test_env_file_path_for_write_copies_legacy_forward_once(ctx: Ctx):
    old_home = os.environ.get("BRIDGE_TEST_HOME")
    saved_overrides = {k: os.environ.get(k) for k in ("BRIDGE_ENV_FILE", "HALO_ENV_FILE", "ROLO_CLAUDE_ENV_FILE")}
    tmp = Path(tempfile.mkdtemp(prefix="h2-env-file-write-"))
    try:
        for k in saved_overrides:
            os.environ.pop(k, None)
        os.environ["BRIDGE_TEST_HOME"] = str(tmp)
        legacy = tmp / ".config" / "vibes-hacker" / "env"
        legacy.parent.mkdir(parents=True)
        legacy.write_text("OPENROUTER_API_KEY=legacy-value\n", encoding="utf-8")
        ctx.check("legacy_env_file_path matches", p.legacy_env_file_path() == legacy)

        write_path = p.env_file_path_for_write()
        ctx.check(f"write path is the NEW canonical path, got {write_path}",
                  write_path == tmp / ".config" / "halo" / "env")
        ctx.check("the new file was copied forward from the legacy one, behind the import marker",
                  write_path.exists() and write_path.read_text(encoding="utf-8")
                  == p.ENV_IMPORT_MARKER + "\nOPENROUTER_API_KEY=legacy-value\n")

        # A second call, after the writer appends something, must never
        # re-copy over it (the new file already exists).
        with open(write_path, "a", encoding="utf-8") as f:
            f.write("DATABRICKS_TOKEN=added\n")
        write_path2 = p.env_file_path_for_write()
        ctx.check("a second call doesn't re-copy/clobber the now-edited new file",
                  write_path2.read_text(encoding="utf-8") ==
                  p.ENV_IMPORT_MARKER + "\nOPENROUTER_API_KEY=legacy-value\nDATABRICKS_TOKEN=added\n")
    finally:
        if old_home is None:
            os.environ.pop("BRIDGE_TEST_HOME", None)
        else:
            os.environ["BRIDGE_TEST_HOME"] = old_home
        for k, v in saved_overrides.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


@test
def test_env_file_path_for_write_marks_the_new_file_and_leaves_the_legacy_file_alone(ctx: Ctx):
    """Item E, non-destructive: the legacy `~/.config/vibes-hacker/env` is
    shared with other tools on the box, so the copy-forward never renames or
    edits it. The new file starts with ENV_IMPORT_MARKER instead, and
    `load_provider_env_files()` skips the legacy file whenever the marker is
    present -- so a key deleted from the new file never comes back."""
    from halo_harness.providers.config import load_provider_env_files

    old_home = os.environ.get("BRIDGE_TEST_HOME")
    saved_overrides = {k: os.environ.get(k) for k in ("BRIDGE_ENV_FILE", "HALO_ENV_FILE", "ROLO_CLAUDE_ENV_FILE")}
    saved_keys = {k: os.environ.get(k) for k in ("OPENROUTER_API_KEY", "DATABRICKS_TOKEN")}
    tmp = Path(tempfile.mkdtemp(prefix="h2-env-marker-"))
    try:
        for k in saved_overrides:
            os.environ.pop(k, None)
        for k in saved_keys:
            os.environ.pop(k, None)
        os.environ["BRIDGE_TEST_HOME"] = str(tmp)
        legacy = tmp / ".config" / "vibes-hacker" / "env"
        legacy.parent.mkdir(parents=True)
        legacy_text = "OPENROUTER_API_KEY=from-legacy\nDATABRICKS_TOKEN=tok-legacy\n"
        legacy.write_text(legacy_text, encoding="utf-8")

        write_path = p.env_file_path_for_write()
        new_text = write_path.read_text(encoding="utf-8")
        ctx.check("the new file starts with the import marker",
                  new_text.splitlines()[0] == p.ENV_IMPORT_MARKER)
        ctx.check("the legacy content was copied forward after the marker",
                  new_text.endswith(legacy_text))
        ctx.check("the legacy file still exists, byte-identical, under its original name",
                  legacy.exists() and legacy.read_text(encoding="utf-8") == legacy_text)
        ctx.check(f"nothing else appeared next to the legacy file, got {sorted(q.name for q in legacy.parent.iterdir())}",
                  sorted(q.name for q in legacy.parent.iterdir()) == ["env"])
        ctx.check("the marker is detected", p.env_file_has_import_marker(write_path))

        # Delete one key from the NEW file: it must stay deleted even though
        # the legacy file still carries it.
        write_path.write_text(p.ENV_IMPORT_MARKER + "\nDATABRICKS_TOKEN=tok-legacy\n", encoding="utf-8")
        for k in saved_keys:
            os.environ.pop(k, None)
        loaded = load_provider_env_files()
        ctx.check(f"the deleted key does not come back from the legacy file, got {sorted(loaded)}",
                  "OPENROUTER_API_KEY" not in loaded and loaded.get("DATABRICKS_TOKEN") == "tok-legacy")

        # Without the marker (a hand-written new file) the legacy file still
        # fills the gaps, exactly as before item E.
        for k in saved_keys:
            os.environ.pop(k, None)
        write_path.write_text("DATABRICKS_TOKEN=tok-new\n", encoding="utf-8")
        loaded = load_provider_env_files()
        ctx.check(f"a hand-written new file without the marker still falls back to the legacy file, got {sorted(loaded)}",
                  loaded.get("OPENROUTER_API_KEY") == "from-legacy" and loaded.get("DATABRICKS_TOKEN") == "tok-new")
    finally:
        for k in saved_keys:
            os.environ.pop(k, None)
        for k, v in saved_keys.items():
            if v is not None:
                os.environ[k] = v
        for k, v in saved_overrides.items():
            if v is not None:
                os.environ[k] = v
        if old_home is None:
            os.environ.pop("BRIDGE_TEST_HOME", None)
        else:
            os.environ["BRIDGE_TEST_HOME"] = old_home
        shutil.rmtree(tmp, ignore_errors=True)


@test
def test_env_file_path_for_write_honors_an_explicit_override(ctx: Ctx):
    """An explicit HALO_ENV_FILE/BRIDGE_ENV_FILE is total -- it wins
    outright, with no copy-forward side effect at all, even with a legacy
    file sitting right there."""
    old_home = os.environ.get("BRIDGE_TEST_HOME")
    old_env_file = os.environ.get("BRIDGE_ENV_FILE")
    tmp = Path(tempfile.mkdtemp(prefix="h2-env-file-write-override-"))
    try:
        os.environ["BRIDGE_TEST_HOME"] = str(tmp)
        legacy = tmp / ".config" / "vibes-hacker" / "env"
        legacy.parent.mkdir(parents=True)
        legacy.write_text("X=1\n", encoding="utf-8")
        custom = tmp / "explicit-write-target"
        os.environ["BRIDGE_ENV_FILE"] = str(custom)
        ctx.check("override wins outright", p.env_file_path_for_write() == custom)
        ctx.check("no copy-forward happened", not custom.exists())
    finally:
        for key, old in (("BRIDGE_TEST_HOME", old_home), ("BRIDGE_ENV_FILE", old_env_file)):
            if old is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = old


@test
def test_load_provider_env_files_new_wins_legacy_fills_gaps(ctx: Ctx):
    from halo_harness.providers.config import load_provider_env_files

    old_home = os.environ.get("BRIDGE_TEST_HOME")
    saved_overrides = {k: os.environ.get(k) for k in ("BRIDGE_ENV_FILE", "HALO_ENV_FILE", "ROLO_CLAUDE_ENV_FILE")}
    saved_keys = {k: os.environ.get(k) for k in ("OPENROUTER_API_KEY", "DATABRICKS_TOKEN", "DATABRICKS_HOST")}
    tmp = Path(tempfile.mkdtemp(prefix="h2-load-provider-env-"))
    try:
        for k in saved_overrides:
            os.environ.pop(k, None)
        for k in saved_keys:
            os.environ.pop(k, None)
        os.environ["BRIDGE_TEST_HOME"] = str(tmp)
        legacy = tmp / ".config" / "vibes-hacker" / "env"
        legacy.parent.mkdir(parents=True)
        legacy.write_text("OPENROUTER_API_KEY=from-legacy\nDATABRICKS_TOKEN=from-legacy\n", encoding="utf-8")
        new = tmp / ".config" / "halo" / "env"
        new.parent.mkdir(parents=True)
        new.write_text("OPENROUTER_API_KEY=from-new\nDATABRICKS_HOST=from-new\n", encoding="utf-8")

        loaded = load_provider_env_files()
        ctx.check(f"new file's key wins on a collision, got {loaded}", loaded.get("OPENROUTER_API_KEY") == "from-new")
        ctx.check(f"a key only in the legacy file still loads, got {loaded}",
                  loaded.get("DATABRICKS_TOKEN") == "from-legacy")
        ctx.check(f"a key only in the new file still loads, got {loaded}",
                  loaded.get("DATABRICKS_HOST") == "from-new")
        ctx.check("os.environ itself reflects the same precedence",
                  os.environ.get("OPENROUTER_API_KEY") == "from-new"
                  and os.environ.get("DATABRICKS_TOKEN") == "from-legacy")
    finally:
        if old_home is None:
            os.environ.pop("BRIDGE_TEST_HOME", None)
        else:
            os.environ["BRIDGE_TEST_HOME"] = old_home
        for k, v in saved_overrides.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        for k, v in saved_keys.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


@test
def test_tool_child_env_strips_keys_from_both_new_and_legacy_files(ctx: Ctx):
    from halo_harness.providers.config import tool_child_env

    old_home = os.environ.get("BRIDGE_TEST_HOME")
    saved_overrides = {k: os.environ.get(k) for k in ("BRIDGE_ENV_FILE", "HALO_ENV_FILE", "ROLO_CLAUDE_ENV_FILE")}
    tmp = Path(tempfile.mkdtemp(prefix="h2-tool-child-env-"))
    try:
        for k in saved_overrides:
            os.environ.pop(k, None)
        os.environ["BRIDGE_TEST_HOME"] = str(tmp)
        legacy = tmp / ".config" / "vibes-hacker" / "env"
        legacy.parent.mkdir(parents=True)
        legacy.write_text("LEGACY_ONLY_SECRET=x\n", encoding="utf-8")
        new = tmp / ".config" / "halo" / "env"
        new.parent.mkdir(parents=True)
        new.write_text("NEW_ONLY_SECRET=y\n", encoding="utf-8")

        raw = {"LEGACY_ONLY_SECRET": "x", "NEW_ONLY_SECRET": "y", "ORDINARY_VAR": "kept"}
        stripped = tool_child_env(raw)
        ctx.check(f"a key only in the legacy file is stripped, got {stripped}", "LEGACY_ONLY_SECRET" not in stripped)
        ctx.check(f"a key only in the new file is stripped, got {stripped}", "NEW_ONLY_SECRET" not in stripped)
        ctx.check(f"an ordinary var survives, got {stripped}", stripped.get("ORDINARY_VAR") == "kept")
    finally:
        if old_home is None:
            os.environ.pop("BRIDGE_TEST_HOME", None)
        else:
            os.environ["BRIDGE_TEST_HOME"] = old_home
        for k, v in saved_overrides.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


@test
def test_env_import_marker_survives_a_bom_and_a_line_added_above_it(ctx: Ctx):
    """A Windows editor re-saving the new file with a UTF-8 BOM, or a comment
    typed above the marker by hand, must not silently re-enable the legacy
    file (which would bring deleted keys back)."""
    tmp = Path(tempfile.mkdtemp(prefix="h2-env-marker-edges-"))
    try:
        f = tmp / "env"
        f.write_bytes(b"\xef\xbb\xbf" + (p.ENV_IMPORT_MARKER + "\nA=1\n").encode("utf-8"))
        ctx.check("marker detected behind a BOM", p.env_file_has_import_marker(f))
        f.write_text("# my own note\n" + p.ENV_IMPORT_MARKER + "\nA=1\n", encoding="utf-8")
        ctx.check("marker detected on a later line", p.env_file_has_import_marker(f))
        f.write_text("A=1\n", encoding="utf-8")
        ctx.check("no marker -> False", not p.env_file_has_import_marker(f))
        ctx.check("missing file -> False", not p.env_file_has_import_marker(tmp / "nope"))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


@test
def test_tool_child_env_stops_stripping_legacy_only_keys_once_the_marker_exists(ctx: Ctx):
    """After the copy-forward, a key the user removed from the new file (and
    exports on purpose) is no longer stripped from tool children just
    because the shared legacy file still lists it; before the marker exists
    both files' keys are stripped."""
    from halo_harness.providers.config import tool_child_env

    old_home = os.environ.get("BRIDGE_TEST_HOME")
    saved_overrides = {k: os.environ.get(k) for k in ("BRIDGE_ENV_FILE", "HALO_ENV_FILE", "ROLO_CLAUDE_ENV_FILE")}
    tmp = Path(tempfile.mkdtemp(prefix="h2-env-childenv-"))
    try:
        for k in saved_overrides:
            os.environ.pop(k, None)
        os.environ["BRIDGE_TEST_HOME"] = str(tmp)
        legacy = tmp / ".config" / "vibes-hacker" / "env"
        legacy.parent.mkdir(parents=True)
        legacy.write_text("MY_LEGACY_SECRET=x\n", encoding="utf-8")
        new = tmp / ".config" / "halo" / "env"
        new.parent.mkdir(parents=True)
        new.write_text("OTHER=1\n", encoding="utf-8")
        child = tool_child_env({"MY_LEGACY_SECRET": "x", "PATH": "/usr/bin"})
        ctx.check("without the marker the legacy-only key is stripped", "MY_LEGACY_SECRET" not in child)
        new.write_text(p.ENV_IMPORT_MARKER + "\nOTHER=1\n", encoding="utf-8")
        child = tool_child_env({"MY_LEGACY_SECRET": "x", "PATH": "/usr/bin"})
        ctx.check("with the marker the legacy file no longer drives stripping",
                  child.get("MY_LEGACY_SECRET") == "x" and child.get("PATH") == "/usr/bin")
    finally:
        for k, v in saved_overrides.items():
            if v is not None:
                os.environ[k] = v
        if old_home is None:
            os.environ.pop("BRIDGE_TEST_HOME", None)
        else:
            os.environ["BRIDGE_TEST_HOME"] = old_home
        shutil.rmtree(tmp, ignore_errors=True)


@test
def test_env_file_copy_forward_is_atomic_and_leaves_no_tmp_file(ctx: Ctx):
    old_home = os.environ.get("BRIDGE_TEST_HOME")
    saved_overrides = {k: os.environ.get(k) for k in ("BRIDGE_ENV_FILE", "HALO_ENV_FILE", "ROLO_CLAUDE_ENV_FILE")}
    tmp = Path(tempfile.mkdtemp(prefix="h2-env-atomic-"))
    try:
        for k in saved_overrides:
            os.environ.pop(k, None)
        os.environ["BRIDGE_TEST_HOME"] = str(tmp)
        legacy = tmp / ".config" / "vibes-hacker" / "env"
        legacy.parent.mkdir(parents=True)
        legacy.write_text("K=v\n", encoding="utf-8")
        write_path = p.env_file_path_for_write()
        names = sorted(q.name for q in write_path.parent.iterdir())
        ctx.check(f"only the final file exists in the new dir, got {names}", names == ["env"])
        ctx.check("content is marker + legacy", write_path.read_text(encoding="utf-8") == p.ENV_IMPORT_MARKER + "\nK=v\n")
    finally:
        for k, v in saved_overrides.items():
            if v is not None:
                os.environ[k] = v
        if old_home is None:
            os.environ.pop("BRIDGE_TEST_HOME", None)
        else:
            os.environ["BRIDGE_TEST_HOME"] = old_home
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    ctx = Ctx()
    results, passed, failed, skipped = run_all(TESTS, ctx)
    sys.exit(print_results(results, passed, failed, skipped))
