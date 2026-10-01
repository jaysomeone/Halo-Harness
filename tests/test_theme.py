"""tests.test_theme -- halo_harness/theme.py: precedence order, valid-name
set, persistence round-trip via ~/.halo/config.json (U0 scope D).
"""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.helpers.runner import Ctx, new_registry, print_results, run_all
from halo_harness import theme as t

test, TESTS = new_registry()


def _with_test_home(fn):
    """Run `fn(home_path)` with BRIDGE_TEST_HOME pointed at a fresh temp
    dir, restoring the previous value afterward."""
    old = os.environ.get("BRIDGE_TEST_HOME")
    tmp = Path(tempfile.mkdtemp(prefix="halo-theme-"))
    os.environ["BRIDGE_TEST_HOME"] = str(tmp)
    try:
        fn(tmp)
    finally:
        if old is None:
            os.environ.pop("BRIDGE_TEST_HOME", None)
        else:
            os.environ["BRIDGE_TEST_HOME"] = old


@test
def test_valid_theme_names(ctx: Ctx):
    ctx.check("claude-dark valid", t.is_valid_theme("claude-dark"))
    ctx.check("claude-light valid", t.is_valid_theme("claude-light"))
    ctx.check("claude-dark-daltonized valid", t.is_valid_theme("claude-dark-daltonized"))
    ctx.check("claude-light-daltonized valid", t.is_valid_theme("claude-light-daltonized"))
    ctx.check("claude-dark-ansi valid", t.is_valid_theme("claude-dark-ansi"))
    ctx.check("claude-light-ansi valid", t.is_valid_theme("claude-light-ansi"))
    ctx.check("garbage invalid", not t.is_valid_theme("solarized"))
    ctx.check("None invalid", not t.is_valid_theme(None))


@test
def test_precedence_cli_wins_over_everything(ctx: Ctx):
    resolved = t.resolve_theme(
        cli_theme="claude-light", env={"CLAUDE_BRIDGE_THEME": "claude-dark-ansi"},
        settings_theme="claude-dark", persisted_theme="claude-light-ansi",
    )
    ctx.check(f"cli wins, got {resolved!r}", resolved == "claude-light")


@test
def test_precedence_env_beats_settings_and_persisted(ctx: Ctx):
    resolved = t.resolve_theme(
        cli_theme=None, env={"CLAUDE_BRIDGE_THEME": "claude-dark-ansi"},
        settings_theme="claude-dark", persisted_theme="claude-light",
    )
    ctx.check(f"env wins over settings, got {resolved!r}", resolved == "claude-dark-ansi")


@test
def test_precedence_halo_harness_theme_env_checked_too(ctx: Ctx):
    resolved = t.resolve_theme(cli_theme=None, env={"ROLO_CLAUDE_THEME": "claude-light-daltonized"},
                                settings_theme=None, persisted_theme=None)
    ctx.check(f"ROLO_CLAUDE_THEME honored, got {resolved!r}", resolved == "claude-light-daltonized")


@test
def test_precedence_settings_beats_persisted(ctx: Ctx):
    resolved = t.resolve_theme(cli_theme=None, env={}, settings_theme="claude-light", persisted_theme="claude-dark-ansi")
    ctx.check(f"settings wins over persisted, got {resolved!r}", resolved == "claude-light")


@test
def test_precedence_persisted_beats_default(ctx: Ctx):
    resolved = t.resolve_theme(cli_theme=None, env={}, settings_theme=None, persisted_theme="claude-dark-ansi")
    ctx.check(f"persisted wins over hardcoded default, got {resolved!r}", resolved == "claude-dark-ansi")


@test
def test_precedence_falls_back_to_default(ctx: Ctx):
    # U5: with nothing else set, the LAST fallback is terminal-capability-
    # aware (`auto_theme_for_env`) -- a truecolor-capable terminal (signalled
    # here explicitly, matching this test's own original intent: "falls back
    # to the hardcoded default when every named tier is absent") still
    # resolves to the plain default; the no-truecolor-signal case is its own
    # dedicated test below.
    resolved = t.resolve_theme(cli_theme=None, env={"COLORTERM": "truecolor"}, settings_theme=None,
                                persisted_theme=None)
    ctx.check(f"falls back to claude-dark, got {resolved!r}", resolved == t.DEFAULT_THEME)


@test
def test_invalid_values_at_every_tier_fall_through(ctx: Ctx):
    resolved = t.resolve_theme(
        cli_theme="not-a-theme", env={"CLAUDE_BRIDGE_THEME": "also-bogus", "COLORTERM": "truecolor"},
        settings_theme="still-bogus", persisted_theme=None,
    )
    ctx.check(f"every invalid tier skipped, falls to default, got {resolved!r}", resolved == t.DEFAULT_THEME)


@test
def test_auto_theme_for_env_downgrades_to_ansi_without_truecolor_signal(ctx: Ctx):
    # U5 scope D: "system/ansi theme auto-select ... COLORTERM, TERM
    # checks" -- when NOTHING (cli/env/settings/persisted) names a theme
    # AND the terminal never signalled truecolor support, the fallback is
    # the -ansi sibling, not the plain (assumed-truecolor) default.
    resolved = t.resolve_theme(cli_theme=None, env={"TERM": "xterm"}, settings_theme=None, persisted_theme=None)
    ctx.check(f"no COLORTERM/known-truecolor TERM -> falls back to the -ansi variant, got {resolved!r}",
              resolved == f"{t.DEFAULT_THEME}-ansi")
    ctx.check("supports_truecolor(...) itself says False for a bare xterm",
              t.supports_truecolor({"TERM": "xterm"}) is False)
    ctx.check("supports_truecolor(...) says True for COLORTERM=truecolor",
              t.supports_truecolor({"COLORTERM": "truecolor"}) is True)
    ctx.check("supports_truecolor(...) says True for a kitty TERM even without COLORTERM",
              t.supports_truecolor({"TERM": "xterm-kitty"}) is True)
    ctx.check("an EXPLICIT theme choice at any tier is never auto-downgraded to -ansi",
              t.resolve_theme(cli_theme="claude-light", env={"TERM": "xterm"}) == "claude-light")


@test
def test_persist_and_reload_round_trip(ctx: Ctx):
    def _run(home_path):
        path = t.persist_theme("claude-light-ansi")
        ctx.check("persist_theme wrote under BRIDGE_TEST_HOME", str(path).startswith(str(home_path)))
        ctx.check("load_persisted_theme reads it back", t.load_persisted_theme() == "claude-light-ansi")
        # a SECOND write must preserve any other key already in the file.
        t.set_config_value("other_key", 42)
        t.persist_theme("claude-dark")
        data = t.load_config()
        ctx.check("other_key survived a later persist_theme call", data.get("other_key") == 42)
        ctx.check("theme updated to the newest value", data.get("theme") == "claude-dark")
    _with_test_home(_run)


@test
def test_persist_theme_rejects_invalid_name(ctx: Ctx):
    def _run(_home_path):
        raised = False
        try:
            t.persist_theme("not-a-real-theme")
        except ValueError:
            raised = True
        ctx.check("persist_theme raises ValueError for an invalid name", raised)
    _with_test_home(_run)


@test
def test_load_config_missing_file_returns_empty_dict(ctx: Ctx):
    def _run(_home_path):
        ctx.check("load_config() == {} when nothing was ever persisted", t.load_config() == {})
        ctx.check("load_persisted_theme() is None when nothing was ever persisted", t.load_persisted_theme() is None)
    _with_test_home(_run)


if __name__ == "__main__":
    ctx = Ctx()
    results, passed, failed, skipped = run_all(TESTS, ctx)
    sys.exit(print_results(results, passed, failed, skipped))
