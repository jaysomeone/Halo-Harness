"""tests.test_hotfix_101_fix_init_preserve -- 1.0.1 fixpass finding 9 (init
never overwrites an existing `permission_mode`/`model` unless the user
actually chose a value THIS run -- `--yes` and an Esc'd picker both keep
whatever's already there, writing nothing at all on a genuinely fresh box)
and finding 17 (the "default" mode's own wording no longer implies a risk
check this project deliberately does not have).
"""
from __future__ import annotations

import contextlib
import io
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.helpers.runner import Ctx, new_registry, print_results, run_all

test, TESTS = new_registry()


class _Env:
    def __enter__(self):
        self._saved = {k: os.environ.get(k) for k in
                       ("BRIDGE_TEST_HOME", "BRIDGE_STATE_DIR", "BRIDGE_ENV_FILE",
                        "BRIDGE_DBX_BASE_URL", "BRIDGE_DBX_TOKEN", "DATABRICKS_HOST", "DATABRICKS_TOKEN",
                        "OPENROUTER_API_KEY", "ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN")}
        d = Path(tempfile.mkdtemp(prefix="hotfix101-init-preserve-"))
        os.environ["BRIDGE_TEST_HOME"] = str(d)
        os.environ["BRIDGE_STATE_DIR"] = str(d / ".rolo-claude")
        os.environ["BRIDGE_ENV_FILE"] = str(d / "no-env-file")
        for k in ("BRIDGE_DBX_BASE_URL", "BRIDGE_DBX_TOKEN", "DATABRICKS_HOST", "DATABRICKS_TOKEN",
                  "OPENROUTER_API_KEY", "ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN"):
            os.environ.pop(k, None)
        self.state_dir = d / ".rolo-claude"
        return self

    def __exit__(self, *exc):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


class _Isatty:
    def __init__(self, value: bool):
        self._value = value

    def isatty(self) -> bool:
        return self._value


@contextlib.contextmanager
def _fake_tty(stdin_tty: bool, stdout_tty: bool):
    real_stdin, real_stdout = sys.stdin, sys.stdout
    sys.stdin, sys.stdout = _Isatty(stdin_tty), _Isatty(stdout_tty)
    try:
        yield
    finally:
        sys.stdin, sys.stdout = real_stdin, real_stdout


def _console():
    from rich.console import Console
    return Console(file=io.StringIO())


def _args(yes: bool = False, model=None):
    return SimpleNamespace(yes=yes, model=model, team=None)


# ---------------------------------------------------------------------------
# finding 9a: permission_mode.
# ---------------------------------------------------------------------------

@test
def test_yes_on_a_fresh_box_writes_no_permission_mode(ctx: Ctx):
    from rolo_claude.init_cli import _step_pick_permission_mode
    from rolo_claude.theme import get_config_value
    with _Env():
        with _fake_tty(stdin_tty=False, stdout_tty=False):
            result = _step_pick_permission_mode(_args(yes=True), _console())
        ctx.check(f"nothing chosen -- empty string returned, got {result!r}", result == "")
        ctx.check("config.json has NO permission_mode key at all (settings.json defaultMode keeps working)",
                  get_config_value("permission_mode", default=None) is None)


@test
def test_yes_keeps_an_existing_permission_mode(ctx: Ctx):
    from rolo_claude.init_cli import _step_pick_permission_mode
    from rolo_claude.theme import get_config_value, set_config_value
    with _Env():
        set_config_value("permission_mode", "plan")
        with _fake_tty(stdin_tty=False, stdout_tty=False):
            result = _step_pick_permission_mode(_args(yes=True), _console())
        ctx.check(f"the existing value is returned unchanged, got {result!r}", result == "plan")
        ctx.check("config.json still says 'plan' -- --yes never downgraded it to 'auto'",
                  get_config_value("permission_mode", default=None) == "plan")


@test
def test_esc_at_the_interactive_picker_writes_nothing_on_a_fresh_box(ctx: Ctx):
    import rolo_claude.tui.dialogs.init_picker as picker_mod
    from rolo_claude.init_cli import _step_pick_permission_mode
    from rolo_claude.theme import get_config_value
    real_picker = picker_mod.run_simple_picker
    picker_mod.run_simple_picker = lambda *a, **kw: None  # Esc
    try:
        with _Env():
            with _fake_tty(stdin_tty=True, stdout_tty=True):
                result = _step_pick_permission_mode(_args(yes=False), _console())
            ctx.check(f"nothing chosen -- empty string returned, got {result!r}", result == "")
            ctx.check("config.json has NO permission_mode key at all",
                      get_config_value("permission_mode", default=None) is None)
    finally:
        picker_mod.run_simple_picker = real_picker


@test
def test_esc_at_the_interactive_picker_keeps_an_existing_value(ctx: Ctx):
    import rolo_claude.tui.dialogs.init_picker as picker_mod
    from rolo_claude.init_cli import _step_pick_permission_mode
    from rolo_claude.theme import get_config_value, set_config_value
    real_picker = picker_mod.run_simple_picker
    picker_mod.run_simple_picker = lambda *a, **kw: None  # Esc
    try:
        with _Env():
            set_config_value("permission_mode", "acceptEdits")
            with _fake_tty(stdin_tty=True, stdout_tty=True):
                result = _step_pick_permission_mode(_args(yes=False), _console())
            ctx.check(f"the existing value is returned unchanged, got {result!r}", result == "acceptEdits")
            ctx.check("config.json still says 'acceptEdits'",
                      get_config_value("permission_mode", default=None) == "acceptEdits")
    finally:
        picker_mod.run_simple_picker = real_picker


@test
def test_a_real_pick_still_writes_it(ctx: Ctx):
    """The fix must not disable writing altogether -- an ACTUAL choice
    still persists exactly as before this fixpass."""
    import rolo_claude.tui.dialogs.init_picker as picker_mod
    from rolo_claude.init_cli import _step_pick_permission_mode
    from rolo_claude.theme import get_config_value
    real_picker = picker_mod.run_simple_picker
    picker_mod.run_simple_picker = lambda *a, **kw: "plan"
    try:
        with _Env():
            with _fake_tty(stdin_tty=True, stdout_tty=True):
                result = _step_pick_permission_mode(_args(yes=False), _console())
            ctx.check(f"the real pick is returned, got {result!r}", result == "plan")
            ctx.check("config.json now says 'plan'", get_config_value("permission_mode", default=None) == "plan")
    finally:
        picker_mod.run_simple_picker = real_picker


# ---------------------------------------------------------------------------
# finding 17: wording.
# ---------------------------------------------------------------------------

@test
def test_default_mode_wording_has_no_risk_language(ctx: Ctx):
    """No "risky"/safety-implying wording -- this project deliberately has
    no risk classifier (feedback_no_cyber_blocks)."""
    from rolo_claude.init_cli import _PERMISSION_MODE_ROWS
    label = dict(_PERMISSION_MODE_ROWS)["default"]
    ctx.check(f"no 'risky' wording, got {label!r}", "risky" not in label.lower())
    ctx.check(f"matches the required replacement wording, got {label!r}",
              label == "default -- ask before edits and non-read-only tools")


# ---------------------------------------------------------------------------
# finding 9b: default model, cross-provider finalize.
# ---------------------------------------------------------------------------

@test
def test_yes_keeps_existing_model_over_the_last_configured_providers_guess(ctx: Ctx):
    """The exact bug: a box with 2 providers configured must NOT get the
    LAST one's (fixed enumeration order, PROVIDERS tuple) guess written
    over an existing default just because nothing was chosen this run.
    anthropic's own model_entries_for_provider is a fixed alias table (no
    catalog fixture needed), so pairing it with openrouter is enough to
    force the real (len > 1) cross-provider path."""
    from rolo_claude.init_cli import _step_finalize_default_model
    from rolo_claude.theme import get_config_value, set_config_value
    with _Env():
        os.environ["OPENROUTER_API_KEY"] = "or-key"
        os.environ["ANTHROPIC_API_KEY"] = "ant-key"
        set_config_value("model", "ant:opus")
        result = _step_finalize_default_model(_args(yes=True), _console(),
                                               configured_this_run=[], picked_per_provider={})
        ctx.check(f"existing model kept, got {result!r}", result == "ant:opus")
        ctx.check("config.json still says the existing model",
                  get_config_value("model", default=None) == "ant:opus")


@test
def test_yes_with_no_existing_model_still_gets_a_working_fallback(ctx: Ctx):
    """A genuinely fresh box (never had a `model` at all) must still end up
    with SOMETHING usable -- the fix only stops CLOBBERING an existing
    value, it doesn't turn a real first-run into "nothing configured"."""
    from rolo_claude.init_cli import PROVIDER_DEFAULT_MODEL, _step_finalize_default_model
    from rolo_claude.theme import get_config_value
    with _Env():
        os.environ["OPENROUTER_API_KEY"] = "or-key"
        os.environ["ANTHROPIC_API_KEY"] = "ant-key"
        result = _step_finalize_default_model(_args(yes=True), _console(),
                                               configured_this_run=["anthropic"], picked_per_provider={})
        ctx.check(f"falls back to anthropic's own default (last configured this run), got {result!r}",
                  result == PROVIDER_DEFAULT_MODEL["anthropic"])
        ctx.check("config.json now has that fallback written (a fresh box must still work)",
                  get_config_value("model", default=None) == PROVIDER_DEFAULT_MODEL["anthropic"])


# ---------------------------------------------------------------------------
# 1.0.1 part 2 (reviewer minor): the PER-PROVIDER default-model step
# (_step_default_model, called early in _run_provider_setup) must not
# overwrite a custom model on a re-run -- only _step_finalize_default_
# model's own cross-provider pick (tested above) previously preserved it.
# ---------------------------------------------------------------------------

@test
def test_per_provider_step_keeps_a_custom_model_on_a_rerun_with_yes(ctx: Ctx):
    """The exact bug: re-running `init --provider databricks --yes` on a
    box that already has a DIFFERENT (deliberately chosen) model configured
    used to silently reset it back to that provider's own hardcoded
    default the moment they differed."""
    from rolo_claude.init_cli import _step_default_model
    from rolo_claude.theme import get_config_value, set_config_value
    with _Env():
        set_config_value("model", "dbx:databricks-kimi-k3")  # a deliberate, non-default pick
        chosen, path = _step_default_model("databricks", _args(yes=True), _console(), write=True)
        ctx.check(f"the custom model is returned, not the provider default, got {chosen!r}",
                  chosen == "dbx:databricks-kimi-k3")
        ctx.check("nothing was (re)written -- the value didn't change", path is None)
        ctx.check("config.json still says the custom model",
                  get_config_value("model", default=None) == "dbx:databricks-kimi-k3")


@test
def test_per_provider_step_still_writes_a_fresh_box_default(ctx: Ctx):
    """The fix must not disable writing altogether -- a genuinely fresh box
    (no `model` key at all yet) still gets the provider's own default."""
    from rolo_claude.init_cli import PROVIDER_DEFAULT_MODEL, _step_default_model
    from rolo_claude.theme import get_config_value
    with _Env():
        chosen, path = _step_default_model("databricks", _args(yes=True), _console(), write=True)
        ctx.check(f"the provider's own default is chosen, got {chosen!r}",
                  chosen == PROVIDER_DEFAULT_MODEL["databricks"])
        ctx.check("a real path was written", path is not None)
        ctx.check("config.json now has it", get_config_value("model", default=None) == chosen)


@test
def test_per_provider_step_explicit_model_flag_still_overrides_a_custom_value(ctx: Ctx):
    """An explicit --model THIS run is real user intent -- it must still
    win over whatever was configured before, unlike the --yes/re-run case
    above."""
    from rolo_claude.init_cli import _step_default_model
    from rolo_claude.theme import get_config_value, set_config_value
    with _Env():
        set_config_value("model", "dbx:databricks-kimi-k3")
        chosen, path = _step_default_model("databricks", _args(yes=True, model="dbx:databricks-glm-5-3"),
                                            _console(), write=True)
        ctx.check(f"the explicit --model wins, got {chosen!r}", chosen == "dbx:databricks-glm-5-3")
        ctx.check("a real path was written (the value DID change)", path is not None)
        ctx.check("config.json reflects the explicit override",
                  get_config_value("model", default=None) == "dbx:databricks-glm-5-3")


@test
def test_per_provider_step_write_false_never_touches_config_either_way(ctx: Ctx):
    """Regression guard: the write=False path (used when more than one
    provider is in play this run) must still never write, custom value or
    not -- unchanged by this fix."""
    from rolo_claude.init_cli import _step_default_model
    from rolo_claude.theme import get_config_value, set_config_value
    with _Env():
        set_config_value("model", "dbx:databricks-kimi-k3")
        chosen, path = _step_default_model("databricks", _args(yes=True), _console(), write=False)
        ctx.check(f"computes the provider default (write=False ignores the existing value by design), "
                  f"got {chosen!r}", chosen == "dbx:databricks-deepseek-v4-1-flash")
        ctx.check("nothing written", path is None)
        ctx.check("config.json untouched", get_config_value("model", default=None) == "dbx:databricks-kimi-k3")


if __name__ == "__main__":
    ctx = Ctx()
    results, passed, failed, skipped = run_all(TESTS, ctx)
    sys.exit(print_results(results, passed, failed, skipped))
