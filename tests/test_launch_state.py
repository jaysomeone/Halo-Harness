"""tests.test_launch_state -- 2.0.1 W3a ("launch with the last session's
model and effort"): `halo_harness.launch_state`'s own persist/resolve pair
(cwd-then-global, tmp+os.replace, state.json lives under ~/.halo only), and
`headless.build_session`'s precedence chain (flags > last_model/last_effort
> config.json > settings.json > provider default), the disabled-provider
fallthrough notice, and -c/--resume leaving it alone.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.helpers.runner import Ctx, new_registry, print_results, run_all

test, TESTS = new_registry()

_PROVIDER_ENV_VARS = (
    "OPENROUTER_API_KEY", "OPENROUTER_MANAGEMENT_KEY", "BRIDGE_OPENROUTER_BASE_URL",
    "DATABRICKS_HOST", "DATABRICKS_TOKEN", "BRIDGE_DBX_BASE_URL", "BRIDGE_DBX_TOKEN",
    "ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN", "BRIDGE_ANTHROPIC_BASE_URL",
    "TYPESAFE_API_KEY", "HALO_MODEL", "BRIDGE_MODEL", "ROLO_CLAUDE_MODEL",
)


class _Env:
    """Every test scopes its home: a fresh BRIDGE_TEST_HOME/BRIDGE_STATE_DIR
    per test -- state.json lives only under this scratch `~/.halo`, never
    the real one. Also clears every provider env var so model resolution
    is driven purely by what each test sets up itself."""

    def __enter__(self):
        self._saved = {k: os.environ.get(k) for k in
                       (("BRIDGE_TEST_HOME", "BRIDGE_STATE_DIR", "BRIDGE_ENV_FILE") + _PROVIDER_ENV_VARS)}
        d = Path(tempfile.mkdtemp(prefix="launch-state-"))
        os.environ["BRIDGE_TEST_HOME"] = str(d)
        os.environ["BRIDGE_STATE_DIR"] = str(d / ".halo")
        os.environ["BRIDGE_ENV_FILE"] = str(d / "no-env-file")
        for k in _PROVIDER_ENV_VARS:
            os.environ.pop(k, None)
        os.environ["BRIDGE_TEST_CC_AUTH_STATUS"] = json.dumps({"loggedIn": False})
        self.home = d
        self.state_dir = d / ".halo"
        return self

    def __exit__(self, *exc):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        os.environ.pop("BRIDGE_TEST_CC_AUTH_STATUS", None)


# ---------------------------------------------------------------------------
# launch_state itself: persist/resolve, cwd-then-global, state.json location
# ---------------------------------------------------------------------------

@test
def test_record_and_resolve_last_model_round_trips(ctx: Ctx):
    from halo_harness import launch_state
    with _Env() as env:
        cwd = env.home / "proj"
        launch_state.record_last_model("or:vendor/model-a", cwd=cwd)
        ctx.check("resolves back the same ref", launch_state.resolve_last_model(cwd) == "or:vendor/model-a")
        ctx.check("state.json lives under ~/.halo only, never ~/.claude",
                  launch_state.state_path() == env.state_dir / "state.json")
        ctx.check("the file is real JSON on disk", json.loads(launch_state.state_path().read_text(encoding="utf-8")))


@test
def test_record_and_resolve_last_effort_round_trips(ctx: Ctx):
    from halo_harness import launch_state
    with _Env() as env:
        cwd = env.home / "proj"
        launch_state.record_last_effort("high", cwd=cwd)
        ctx.check("resolves back the same level", launch_state.resolve_last_effort(cwd) == "high")


@test
def test_state_json_write_is_atomic_tmp_plus_replace(ctx: Ctx):
    from halo_harness import launch_state
    with _Env() as env:
        launch_state.record_last_model("or:a/b", cwd=env.home)
        leftover = list(env.state_dir.glob("*.tmp"))
        ctx.check(f"no .tmp file left behind, got {leftover}", leftover == [])


@test
def test_cwd_memory_wins_over_global_then_falls_back_to_global(ctx: Ctx):
    from halo_harness import launch_state
    with _Env() as env:
        cwd_a = env.home / "proj-a"
        cwd_b = env.home / "proj-b"
        cwd_c = env.home / "proj-never-visited"
        launch_state.record_last_model("or:global/one", cwd=cwd_a)  # also becomes "global"
        launch_state.record_last_model("or:cwd-b/two", cwd=cwd_b)   # cwd_b's own entry, NEW global
        ctx.check("cwd_a keeps its OWN per-cwd choice, not the newer global one",
                  launch_state.resolve_last_model(cwd_a) == "or:global/one")
        ctx.check("cwd_b sees its own (and latest-global) choice",
                  launch_state.resolve_last_model(cwd_b) == "or:cwd-b/two")
        ctx.check("an unvisited cwd falls back to the global (most recent) choice",
                  launch_state.resolve_last_model(cwd_c) == "or:cwd-b/two")
        ctx.check("memory='global' ignores cwd_a's own entry, uses the global one",
                  launch_state.resolve_last_model(cwd_a, memory="global") == "or:cwd-b/two")


@test
def test_resolve_last_model_none_when_never_recorded(ctx: Ctx):
    from halo_harness import launch_state
    with _Env() as env:
        ctx.check("None with an empty state.json", launch_state.resolve_last_model(env.home) is None)


# ---------------------------------------------------------------------------
# headless.build_session precedence: flags > last_model/last_effort >
# config.json > settings.json > provider default; disabled-provider
# fallthrough with a notice; resume/continue leave it alone.
# ---------------------------------------------------------------------------

def _build(cwd: Path, **kw):
    from halo_harness import headless
    kw.setdefault("bare", True)
    kw.setdefault("print_mode", True)
    kw.setdefault("max_turns", 3)
    return headless.build_session(cwd=cwd, **kw)


@test
def test_build_session_uses_last_model_when_no_flag_given(ctx: Ctx):
    from halo_harness import launch_state
    with _Env() as env, tempfile.TemporaryDirectory() as cwd:
        os.environ["OPENROUTER_API_KEY"] = "test-key-not-real"
        launch_state.record_last_model("or:mock/last-used-model", cwd=Path(cwd))
        build = _build(Path(cwd))
        ctx.check(f"the persisted last_model wins over the hardcoded default, got {build.model_ref.raw}",
                  build.model_ref.model == "mock/last-used-model")


@test
def test_build_session_explicit_flag_beats_last_model(ctx: Ctx):
    from halo_harness import launch_state
    with _Env() as env, tempfile.TemporaryDirectory() as cwd:
        os.environ["OPENROUTER_API_KEY"] = "test-key-not-real"
        launch_state.record_last_model("or:mock/last-used-model", cwd=Path(cwd))
        build = _build(Path(cwd), model_ref_raw="or:mock/explicit-flag-model")
        ctx.check(f"--model always wins, got {build.model_ref.model}",
                  build.model_ref.model == "mock/explicit-flag-model")


@test
def test_build_session_disabled_provider_fallthrough_prints_one_notice(ctx: Ctx):
    """The persisted ref's OWN provider (databricks) is disabled -- the
    fallthrough lands on resolve_default_model_raw's own hardcoded default
    (or:deepseek/..., DEFAULT_MODEL_REF), which needs OpenRouter to stay
    enabled+keyed (unrelated to the provider this test disables)."""
    import io
    import contextlib
    from halo_harness import launch_state
    from halo_harness.model import DEFAULT_MODEL_REF
    from halo_harness.theme import set_config_value
    with _Env() as env, tempfile.TemporaryDirectory() as cwd:
        os.environ["OPENROUTER_API_KEY"] = "test-key-not-real"
        launch_state.record_last_model("dbx:now-disabled-endpoint", cwd=Path(cwd))
        set_config_value("providers", {"databricks": {"enabled": False}})
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            build = _build(Path(cwd))
        notice = stderr.getvalue()
        ctx.check(f"falls through to the hardcoded default instead of raising, got {build.model_ref.raw}",
                  build.model_ref.raw == DEFAULT_MODEL_REF)
        ctx.check(f"exactly one notice line naming the stale model, got {notice!r}",
                  "dbx:now-disabled-endpoint" in notice and "no longer available" in notice)


@test
def test_build_session_last_model_with_no_credentials_falls_through_with_a_notice(ctx: Ctx):
    """Release review finding 35: the remembered ref was validated only
    with `parse_model_ref`, which rejects only an explicitly DISABLED
    provider -- a model whose provider is merely "not set up" (no
    credentials at all, e.g. a global last model chosen on another box)
    used to be accepted silently and built with creds=None, failing every
    turn. Deliberately no DATABRICKS_HOST/DATABRICKS_TOKEN at all here,
    and the provider itself is NOT disabled -- parse_model_ref alone
    would have accepted this before the fix."""
    import io
    import contextlib
    from halo_harness import launch_state
    from halo_harness.model import DEFAULT_MODEL_REF
    with _Env() as env, tempfile.TemporaryDirectory() as cwd:
        os.environ["OPENROUTER_API_KEY"] = "test-key-not-real"
        launch_state.record_last_model("dbx:databricks-glm-5-3", cwd=Path(cwd))
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            build = _build(Path(cwd))
        notice = stderr.getvalue()
        ctx.check(f"falls through to the hardcoded default instead of using creds=None, got {build.model_ref.raw}",
                  build.model_ref.raw == DEFAULT_MODEL_REF)
        ctx.check(f"a real, usable creds object backs the fallback, got {build.creds}", build.creds is not None)
        ctx.check(f"exactly one notice line naming the stale model, got {notice!r}",
                  "dbx:databricks-glm-5-3" in notice and "no credentials" in notice)


@test
def test_build_session_last_model_cc_route_is_never_rejected_for_missing_creds(ctx: Ctx):
    """A remembered cc: (Claude Code subscription) model needs no
    ProviderCreds at all -- `_resolve_creds` doesn't model that route and
    returns None for it unconditionally, so the new credentials check
    must never apply to it (creds=None is the CORRECT, expected value for
    a cc: session, not a sign it's unusable)."""
    from halo_harness import launch_state
    with _Env() as env, tempfile.TemporaryDirectory() as cwd:
        os.environ["OPENROUTER_API_KEY"] = "test-key-not-real"
        launch_state.record_last_model("cc:fable", cwd=Path(cwd))
        build = _build(Path(cwd))
        ctx.check(f"the remembered cc: ref is used as-is, got {build.model_ref.raw}",
                  build.model_ref.raw == "cc:fable")


@test
def test_build_session_last_effort_beats_config_json_beats_settings(ctx: Ctx):
    from halo_harness import launch_state
    from halo_harness.theme import set_config_value
    with _Env() as env, tempfile.TemporaryDirectory() as cwd:
        os.environ["OPENROUTER_API_KEY"] = "test-key-not-real"
        set_config_value("effort", "low")
        build1 = _build(Path(cwd), model_ref_raw="or:mock/model")
        ctx.check(f"config.json's effort key applies with nothing more specific, got {build1.session.effort}",
                  build1.session.effort == "low" and build1.session.effort_source == "config")

        launch_state.record_last_effort("high", cwd=Path(cwd))
        build2 = _build(Path(cwd), model_ref_raw="or:mock/model")
        ctx.check(f"last_effort beats config.json's effort key, got {build2.session.effort}",
                  build2.session.effort == "high" and build2.session.effort_source == "last")

        build3 = _build(Path(cwd), model_ref_raw="or:mock/model", effort="medium")
        ctx.check(f"an explicit --effort flag beats last_effort too, got {build3.session.effort}",
                  build3.session.effort == "medium" and build3.session.effort_source == "flag")


@test
def test_build_session_resume_and_continue_never_consult_last_model_or_effort(ctx: Ctx):
    """-c/--resume keep the resumed session's own model -- the persisted-
    choice layer must never even be CONSULTED for either, proven by
    poisoning both resolvers."""
    import halo_harness.launch_state as launch_state_mod
    with _Env() as env, tempfile.TemporaryDirectory() as cwd:
        os.environ["OPENROUTER_API_KEY"] = "test-key-not-real"

        def _poison_model(*a, **kw):
            raise AssertionError("resolve_last_model must never be consulted for -c/--resume")

        def _poison_effort(*a, **kw):
            raise AssertionError("resolve_last_effort must never be consulted for -c/--resume")

        real_model, real_effort = launch_state_mod.resolve_last_model, launch_state_mod.resolve_last_effort
        launch_state_mod.resolve_last_model = _poison_model
        launch_state_mod.resolve_last_effort = _poison_effort
        try:
            _build(Path(cwd), model_ref_raw="or:mock/model", continue_=True)
            _build(Path(cwd), model_ref_raw="or:mock/model", resume="some-session-id")
        finally:
            launch_state_mod.resolve_last_model = real_model
            launch_state_mod.resolve_last_effort = real_effort


@test
def test_print_mode_also_reads_the_persisted_choice(ctx: Ctx):
    from halo_harness import launch_state
    with _Env() as env, tempfile.TemporaryDirectory() as cwd:
        os.environ["OPENROUTER_API_KEY"] = "test-key-not-real"
        launch_state.record_last_model("or:mock/persisted-for-print-mode", cwd=Path(cwd))
        launch_state.record_last_effort("high", cwd=Path(cwd))
        build = _build(Path(cwd), print_mode=True)
        ctx.check(f"print mode (-p) reads the persisted model too, got {build.model_ref.model}",
                  build.model_ref.model == "mock/persisted-for-print-mode")
        ctx.check(f"and the persisted effort, got {build.session.effort}", build.session.effort == "high")


if __name__ == "__main__":
    ctx = Ctx()
    results, passed, failed, skipped = run_all(TESTS, ctx)
    sys.exit(print_results(results, passed, failed, skipped))
