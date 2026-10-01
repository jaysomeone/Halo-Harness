"""tests.test_h15_effort_remainder -- H15 item 22 remainder: `/effort`/the
status-bar tag show "none (tools)" on a route where the learned-or-table
`reasoning_effort_with_tools` rule applies; that rule now persists in a
per-endpoint cache across sessions, not only in memory for the rest of one
session. Also pins the reviewer minor `clamp_effort` fix (`max` -> `xhigh`
on a chat route with no `max` of its own).
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.helpers.runner import Ctx, new_registry, print_results, run_all
from tests.helpers.fake_home import build_fake_home
from tests.helpers.mock_databricks import MockDatabricks
from tests.helpers.provider_env_defaults import ensure_default_provider_credentials

ensure_default_provider_credentials()

test, TESTS = new_registry()


class _Env:
    """Session.__init__ builds a real SessionLog under bridge_home() the
    moment it exists -- BRIDGE_TEST_HOME must be set before that, never
    left to fall back to the real ~/.rolo-claude/sessions."""

    def __init__(self, fh=None):
        self._fh = fh

    def __enter__(self):
        self._saved = {k: os.environ.get(k) for k in
                       ("BRIDGE_TEST_HOME", "BRIDGE_STATE_DIR", "BRIDGE_ENV_FILE",
                        "OPENROUTER_API_KEY", "DATABRICKS_HOST", "DATABRICKS_TOKEN",
                        "ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN", "TYPESAFE_API_KEY")}
        if self._fh is not None:
            os.environ["BRIDGE_TEST_HOME"] = str(self._fh["home"])
            os.environ.pop("BRIDGE_STATE_DIR", None)
        else:
            d = Path(tempfile.mkdtemp(prefix="h15-effort-remainder-"))
            os.environ["BRIDGE_TEST_HOME"] = str(d)
            os.environ["BRIDGE_STATE_DIR"] = str(d / ".rolo-claude")
            os.environ["BRIDGE_ENV_FILE"] = str(d / "no-env-file")
        for k in ("OPENROUTER_API_KEY", "DATABRICKS_HOST", "DATABRICKS_TOKEN",
                  "ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN", "TYPESAFE_API_KEY"):
            os.environ.pop(k, None)
        return self

    def __exit__(self, *exc):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


# ---------------------------------------------------------------------------
# clamp_effort: reviewer minor -- max -> xhigh on a route with no max.
# ---------------------------------------------------------------------------

@test
def test_clamp_effort_max_becomes_xhigh_on_a_route_that_has_xhigh_but_no_max(ctx: Ctx):
    from rolo_claude.providers.profiles import OPENAI_EFFORT_LEVELS, ProviderProfile, clamp_effort
    profile = ProviderProfile(effort_values_supported=OPENAI_EFFORT_LEVELS)  # low/medium/high/xhigh, no "max"
    got = clamp_effort("max", profile)
    ctx.check(f"max clamps to xhigh (the route's own strongest level), got {got!r}", got == "xhigh")


@test
def test_clamp_effort_max_still_falls_back_to_default_with_neither_max_nor_xhigh(ctx: Ctx):
    from rolo_claude.providers.profiles import ProviderProfile, clamp_effort
    profile = ProviderProfile(effort_values_supported=("low", "medium", "high"))
    got = clamp_effort("max", profile)
    ctx.check(f"neither max nor xhigh supported -- falls back to medium, got {got!r}", got == "medium")


@test
def test_clamp_effort_xhigh_still_becomes_max_unaffected_by_this_fix(ctx: Ctx):
    """Regression guard: the EXISTING xhigh->max case must be untouched."""
    from rolo_claude.providers.profiles import ANTHROPIC_EFFORT_LEVELS, ProviderProfile, clamp_effort
    profile = ProviderProfile(effort_values_supported=ANTHROPIC_EFFORT_LEVELS)  # low/medium/high/max, no xhigh
    got = clamp_effort("xhigh", profile)
    ctx.check(f"xhigh still clamps to max, got {got!r}", got == "max")


@test
def test_clamp_effort_an_actually_supported_value_passes_through(ctx: Ctx):
    from rolo_claude.providers.profiles import OPENAI_EFFORT_LEVELS, ProviderProfile, clamp_effort
    profile = ProviderProfile(effort_values_supported=OPENAI_EFFORT_LEVELS)
    ctx.check("medium passes through unchanged", clamp_effort("medium", profile) == "medium")


# ---------------------------------------------------------------------------
# effort_display_override
# ---------------------------------------------------------------------------

@test
def test_effort_display_override_none_without_a_route_rule(ctx: Ctx):
    from rolo_claude.providers.profiles import ProviderProfile, effort_display_override
    ctx.check("no override, no display string", effort_display_override(ProviderProfile()) is None)
    ctx.check("a None profile is also safe", effort_display_override(None) is None)


@test
def test_effort_display_override_shows_none_tools_for_the_gpt6_rule(ctx: Ctx):
    from rolo_claude.providers.profiles import ProviderProfile, effort_display_override
    profile = ProviderProfile(reasoning_effort_with_tools="none")
    got = effort_display_override(profile)
    ctx.check(f"shows 'none (tools)', got {got!r}", got == "none (tools)")


@test
def test_effort_display_override_never_applies_to_the_anthropic_thinking_shape(ctx: Ctx):
    from rolo_claude.providers.profiles import ProviderProfile, effort_display_override
    profile = ProviderProfile(reasoning_effort_with_tools="none", thinking_format="anthropic_thinking")
    ctx.check("anthropic_thinking never uses this field", effort_display_override(profile) is None)


# ---------------------------------------------------------------------------
# /effort (headless) shows "none (tools)" + why.
# ---------------------------------------------------------------------------

@test
def test_cmd_effort_bare_shows_none_tools_and_explains_why(ctx: Ctx):
    from rolo_claude.commands.builtins import HeadlessFacade, _cmd_effort
    from rolo_claude.providers.profiles import ProviderProfile

    class _FakeSession:
        provider_profile = ProviderProfile(reasoning_effort_with_tools="none",
                                            effort_values_supported=("low", "medium", "high"))
        effort = "high"
        effort_source = "session"
        model_ref = type("R", (), {"raw": "dbx:databricks-gpt-6-sol"})()

    facade = HeadlessFacade(cwd=Path.cwd(), session=_FakeSession())
    result = _cmd_effort("", facade)
    ctx.check(f"shows the EFFECTIVE value, got {result!r}", "none (tools)" in result)
    ctx.check(f"explains why, got {result!r}", "reasoning_effort" in result and "tools" in result)


@test
def test_cmd_effort_bare_unaffected_without_an_override(ctx: Ctx):
    from rolo_claude.commands.builtins import HeadlessFacade, _cmd_effort
    from rolo_claude.providers.profiles import ProviderProfile

    class _FakeSession:
        provider_profile = ProviderProfile(effort_values_supported=("low", "medium", "high"))
        effort = "high"
        effort_source = "session"
        model_ref = type("R", (), {"raw": "or:deepseek/deepseek-v3.2"})()

    facade = HeadlessFacade(cwd=Path.cwd(), session=_FakeSession())
    result = _cmd_effort("", facade)
    ctx.check(f"shows the plain configured value, got {result!r}", "Effort level: high" in result)
    ctx.check(f"no tools-override wording, got {result!r}", "(tools)" not in result)


# ---------------------------------------------------------------------------
# EffortCard's own description line says why.
# ---------------------------------------------------------------------------

@test
def test_effort_card_shows_the_override_note(ctx: Ctx):
    from rolo_claude.tui.widgets.cards import EffortCard
    decisions = []
    card = EffortCard(levels=["low", "medium", "high"], current="high", model_id="dbx:databricks-gpt-6-sol",
                       on_select=decisions.append, override_note="Note: this route sends reasoning_effort='none' "
                                                                   "whenever a turn carries tools.")
    rendered = card.renderable if hasattr(card, "renderable") else str(card.render())
    ctx.check(f"the override note is shown, got {rendered!r}", "reasoning_effort='none'" in str(rendered))


@test
def test_effort_card_without_a_note_is_unaffected(ctx: Ctx):
    """Regression guard: omitting override_note must render EXACTLY as
    before this item (no extra line, no AttributeError)."""
    from rolo_claude.tui.widgets.cards import EffortCard
    card = EffortCard(levels=["low", "medium"], current="low", model_id="or:x", on_select=lambda l: None)
    rendered = card.renderable if hasattr(card, "renderable") else str(card.render())
    ctx.check(f"no stray 'Note:' line, got {rendered!r}", "Note:" not in str(rendered))


# ---------------------------------------------------------------------------
# status_event() carries the effective display value.
# ---------------------------------------------------------------------------

@test
def test_status_event_effort_field_shows_none_tools(ctx: Ctx):
    fh = build_fake_home()
    with _Env(fh):
        mock = MockDatabricks().start()
        try:
            from rolo_claude.agent.assemble import SessionContext
            from rolo_claude.agent.loop import Session
            from rolo_claude.model import ModelProfile, parse_model_ref
            from rolo_claude.providers.stream import ProviderCreds
            # H15 part 2 addendum 3.1: see _new_dbx_session's own comment.
            os.environ["DATABRICKS_HOST"] = "https://test.cloud.databricks.com"
            os.environ["DATABRICKS_TOKEN"] = "test-token"
            model = "dbx:databricks-gpt-6-sol"
            session_ctx = SessionContext(cwd=fh["proj"], model_label=model)
            session = Session(
                cwd=fh["proj"], model_ref=parse_model_ref(model), model_profile=ModelProfile(),
                creds=ProviderCreds(base_url=mock.root, api_key="test-token"),
                state_dir=Path(tempfile.mkdtemp(prefix="h15-effort-status-")), model_label=model,
                session_context=session_ctx, max_turns=3, effort="high",
            )
            status = session.status_event()
            ctx.check(f"status effort field shows the override, got {status.data.get('effort')!r}",
                      status.data.get("effort") == "none (tools)")
        finally:
            mock.stop()


# ---------------------------------------------------------------------------
# learned_rules module: persistence round trip.
# ---------------------------------------------------------------------------

@test
def test_learned_rules_round_trip(ctx: Ctx):
    from rolo_claude.providers.learned_rules import learn_reasoning_effort_with_tools, learned_reasoning_effort_with_tools
    state_dir = Path(tempfile.mkdtemp(prefix="h15-learned-rules-"))
    ctx.check("nothing learned yet", learned_reasoning_effort_with_tools(state_dir, "databricks", "my-model") is None)
    learn_reasoning_effort_with_tools(state_dir, "databricks", "my-model", "none")
    ctx.check("learned value reads back",
              learned_reasoning_effort_with_tools(state_dir, "databricks", "my-model") == "none")
    path = state_dir / "learned-rules.json"
    ctx.check(f"a real file was written, got {list(state_dir.iterdir())}", path.exists())


@test
def test_learned_rules_is_scoped_per_endpoint(ctx: Ctx):
    from rolo_claude.providers.learned_rules import learn_reasoning_effort_with_tools, learned_reasoning_effort_with_tools
    state_dir = Path(tempfile.mkdtemp(prefix="h15-learned-rules-scope-"))
    learn_reasoning_effort_with_tools(state_dir, "databricks", "model-a", "none")
    ctx.check("a DIFFERENT endpoint never learned anything",
              learned_reasoning_effort_with_tools(state_dir, "databricks", "model-b") is None)


@test
def test_resolve_profile_falls_back_to_a_learned_rule_for_an_untabled_endpoint(ctx: Ctx):
    from rolo_claude.providers.learned_rules import learn_reasoning_effort_with_tools
    from rolo_claude.providers.profiles import reset_model_table_cache, resolve_profile
    from rolo_claude.providers.routing import Route
    reset_model_table_cache()
    state_dir = Path(tempfile.mkdtemp(prefix="h15-learned-rules-resolve-"))
    model_id = "databricks-some-brand-new-gpt-family-endpoint"
    route = Route(provider="databricks", upstream_model=model_id, dialect="openai-chat")
    before = resolve_profile(route, state_dir=state_dir)
    ctx.check(f"nothing learned yet -- no override, got {before.reasoning_effort_with_tools!r}",
              before.reasoning_effort_with_tools is None)
    learn_reasoning_effort_with_tools(state_dir, "databricks", model_id, "none")
    after = resolve_profile(route, state_dir=state_dir)
    ctx.check(f"the learned rule now applies, got {after.reasoning_effort_with_tools!r}",
              after.reasoning_effort_with_tools == "none")


@test
def test_resolve_profile_a_tabled_row_wins_over_a_conflicting_learned_entry(ctx: Ctx):
    """A model_table.json row is a VERIFIED fact -- the learned cache must
    never override it, even if (hypothetically) it disagreed."""
    from rolo_claude.providers.learned_rules import learn_reasoning_effort_with_tools
    from rolo_claude.providers.profiles import reset_model_table_cache, resolve_profile
    from rolo_claude.providers.routing import Route
    reset_model_table_cache()
    state_dir = Path(tempfile.mkdtemp(prefix="h15-learned-rules-precedence-"))
    model_id = "databricks-gpt-6-sol"  # a real tabled row: reasoning_effort_with_tools == "none"
    learn_reasoning_effort_with_tools(state_dir, "databricks", model_id, "high")  # a deliberately WRONG learned value
    route = Route(provider="databricks", upstream_model=model_id, dialect="openai-chat")
    profile = resolve_profile(route, state_dir=state_dir)
    ctx.check(f"the tabled value wins, got {profile.reasoning_effort_with_tools!r}",
              profile.reasoning_effort_with_tools == "none")


@test
def test_resolve_profile_without_state_dir_is_unaffected(ctx: Ctx):
    """Every EXISTING caller that omits state_dir keeps today's behaviour
    byte for byte -- no learned-rule lookup at all."""
    from rolo_claude.providers.profiles import reset_model_table_cache, resolve_profile
    from rolo_claude.providers.routing import Route
    reset_model_table_cache()
    route = Route(provider="databricks", upstream_model="databricks-some-brand-new-gpt-family-endpoint",
                  dialect="openai-chat")
    profile = resolve_profile(route)  # no state_dir at all
    ctx.check(f"no override without state_dir, got {profile.reasoning_effort_with_tools!r}",
              profile.reasoning_effort_with_tools is None)


# ---------------------------------------------------------------------------
# End to end: a live retry teaches the session AND the disk cache; a fresh
# session against the SAME endpoint picks it up immediately.
# ---------------------------------------------------------------------------

_UNTABLED_MODEL = "databricks-gpt-5-reasoning-effort-tools-reject-unless-none"


def _new_dbx_session(fh, mock, state_dir, *, effort=None):
    from rolo_claude.agent.assemble import SessionContext
    from rolo_claude.agent.loop import Session
    from rolo_claude.model import ModelProfile, parse_model_ref
    from rolo_claude.providers.stream import ProviderCreds
    # H15 part 2 addendum 3.1: parse_model_ref now refuses a dbx: ref whose
    # provider isn't auto-detected as enabled -- `_Env` above deliberately
    # clears real credential vars, so this needs its own (believable, never
    # the mock's own creds= below, which carry the WIRE address instead).
    os.environ["DATABRICKS_HOST"] = "https://test.cloud.databricks.com"
    os.environ["DATABRICKS_TOKEN"] = "test-token"
    model = f"dbx:{_UNTABLED_MODEL}"
    session_ctx = SessionContext(cwd=fh["proj"], model_label=model)
    return Session(
        cwd=fh["proj"], model_ref=parse_model_ref(model), model_profile=ModelProfile(),
        creds=ProviderCreds(base_url=mock.root, api_key="test-token"),
        state_dir=state_dir, model_label=model, session_context=session_ctx, max_turns=6, effort=effort,
    )


@test
def test_a_successful_none_retry_learns_the_rule_for_the_rest_of_the_session(ctx: Ctx):
    from rolo_claude.providers.profiles import reset_model_table_cache
    reset_model_table_cache()
    fh = build_fake_home()
    with _Env(fh):
        mock = MockDatabricks().start()
        try:
            state_dir = Path(tempfile.mkdtemp(prefix="h15-learn-live-"))
            session = _new_dbx_session(fh, mock, state_dir, effort="high")
            events_seen = list(session.turn("hello"))
            errors = [e for e in events_seen if e.kind == "error"]
            ctx.check(f"no error surfaced, got kinds={[e.kind for e in events_seen]}", errors == [])
            ctx.check(f"two attempts (original + the none-retry), got {len(mock.requests)}",
                      len(mock.requests) == 2)
            ctx.check(f"session.provider_profile now carries the learned override, got "
                      f"{session.provider_profile.reasoning_effort_with_tools!r}",
                      session.provider_profile.reasoning_effort_with_tools == "none")
            from rolo_claude.providers.learned_rules import learned_reasoning_effort_with_tools
            ctx.check("persisted to the per-endpoint cache on disk",
                      learned_reasoning_effort_with_tools(state_dir, "databricks", _UNTABLED_MODEL) == "none")
        finally:
            mock.stop()


@test
def test_a_fresh_session_against_the_same_endpoint_never_pays_for_the_failing_request_again(ctx: Ctx):
    from rolo_claude.providers.profiles import reset_model_table_cache
    reset_model_table_cache()
    fh = build_fake_home()
    with _Env(fh):
        mock = MockDatabricks().start()
        try:
            state_dir = Path(tempfile.mkdtemp(prefix="h15-learn-persist-"))
            from rolo_claude.providers.learned_rules import learn_reasoning_effort_with_tools
            learn_reasoning_effort_with_tools(state_dir, "databricks", _UNTABLED_MODEL, "none")
            session = _new_dbx_session(fh, mock, state_dir, effort="high")
            ctx.check(f"the FRESH session's own profile already carries it at construction time, got "
                      f"{session.provider_profile.reasoning_effort_with_tools!r}",
                      session.provider_profile.reasoning_effort_with_tools == "none")
            events_seen = list(session.turn("hello"))
            errors = [e for e in events_seen if e.kind == "error"]
            ctx.check(f"no error surfaced, got kinds={[e.kind for e in events_seen]}", errors == [])
            ctx.check(f"exactly ONE attempt -- no wasted retry this time, got {len(mock.requests)}",
                      len(mock.requests) == 1)
            ctx.check(f"that one attempt already sent 'none', got "
                      f"{(mock.requests[0]['body'] or {}).get('reasoning_effort')!r}",
                      (mock.requests[0]["body"] or {}).get("reasoning_effort") == "none")
        finally:
            mock.stop()


if __name__ == "__main__":
    ctx = Ctx()
    results, passed, failed, skipped = run_all(TESTS, ctx)
    sys.exit(print_results(results, passed, failed, skipped))
