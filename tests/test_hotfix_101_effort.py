"""tests.test_hotfix_101_effort -- 1.0.1 hotfixes 19/20: a Databricks Claude
foundation endpoint rejects `output_config.effort: "xhigh"` outright (`Input
should be 'low', 'medium', 'high' or 'max'`) -- verified live on
`dbx:databricks-claude-opus-4-6` with a plain "howdy" prompt, no `--effort`
given at all (the value came from the user's own `~/.claude/settings.json`
`effortLevel: "xhigh"`, a value real Claude Code's own routes accept but
this harness's Anthropic-passthrough routes must clamp). Covers: the new
Anthropic-family "high" default, `clamp_effort`'s xhigh->max narrowing (and
that a chat-dialect route's own xhigh support is untouched), the one-shot
400 retry with the effort field stripped, and `/effort`'s own set/show
behavior (hotfix 20 -- it used to be pure decoration: `/effort <anything>`
ignored its argument completely and only ever echoed the session's
snapshotted starting value).
"""
from __future__ import annotations

import functools
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.helpers.runner import Ctx, new_registry, print_results, run_all
from tests.helpers.fake_home import build_fake_home
from tests.helpers.mock_databricks import MockDatabricks
from tests.helpers.provider_env_defaults import ensure_default_provider_credentials

# H15 part 2 addendum 3.1: a believable default credential (never a real
# one) keeps every dbx:/or: ref below resolving exactly as it did before
# parse_model_ref started refusing an auto-detected-disabled provider;
# each test here already scopes its OWN BRIDGE_STATE_DIR (see `test`
# wrapper below).
ensure_default_provider_credentials()

_register, TESTS = new_registry()


def test(fn):
    """H15 Part D2.1: `_new_dbx_session` below builds a REAL `Session`,
    whose `SessionLog` ALWAYS resolves its storage root via `bridge_home()`
    -- independent of the `state_dir=` this module's own `_new_dbx_session`
    passes, which only ever sets `Session.state_dir` (config/catalog
    caching), never where the log itself is written. `build_fake_home()`
    alone sets no env var at all, so every `@test` here is transparently
    wrapped in an isolated, per-test `BRIDGE_STATE_DIR` (found leaking real
    `halo-fakehome-*-proj` slug directories into
    `~/.halo/sessions` during the H15 fix pass), same pattern
    `tests/test_log_derive.py` already uses."""
    @functools.wraps(fn)
    def wrapper(ctx):
        old = os.environ.get("BRIDGE_STATE_DIR")
        os.environ["BRIDGE_STATE_DIR"] = str(Path(tempfile.mkdtemp(prefix="hotfix101-effort-state-")))
        try:
            return fn(ctx)
        finally:
            if old is None:
                os.environ.pop("BRIDGE_STATE_DIR", None)
            else:
                os.environ["BRIDGE_STATE_DIR"] = old
    return _register(wrapper)


def _new_dbx_session(fh, mock, *, model: str, effort=None):
    from halo_harness.agent.assemble import SessionContext
    from halo_harness.agent.loop import Session
    from halo_harness.model import ModelProfile, parse_model_ref
    from halo_harness.providers.stream import ProviderCreds
    session_ctx = SessionContext(cwd=fh["proj"], model_label=model)
    model_ref = parse_model_ref(model)
    return Session(
        cwd=fh["proj"], model_ref=model_ref, model_profile=ModelProfile(),
        creds=ProviderCreds(base_url=mock.root, api_key="test-token"),
        state_dir=Path(tempfile.mkdtemp(prefix="hotfix101-effort-")), model_label=model,
        session_context=session_ctx, max_turns=6, effort=effort,
    )


# ---------------------------------------------------------------------------
# clamp_effort -- pure function, no network
# ---------------------------------------------------------------------------

@test
def test_clamp_effort_passes_through_a_value_the_route_accepts(ctx: Ctx):
    from halo_harness.providers.profiles import ANTHROPIC_EFFORT_LEVELS, ProviderProfile, clamp_effort
    profile = ProviderProfile(thinking_format="anthropic_thinking", effort_values_supported=ANTHROPIC_EFFORT_LEVELS)
    ctx.check("'high' passes through unchanged", clamp_effort("high", profile) == "high")


@test
def test_clamp_effort_xhigh_becomes_max_on_anthropic_route(ctx: Ctx):
    from halo_harness.providers.profiles import ANTHROPIC_EFFORT_LEVELS, ProviderProfile, clamp_effort
    profile = ProviderProfile(thinking_format="anthropic_thinking", effort_values_supported=ANTHROPIC_EFFORT_LEVELS)
    got = clamp_effort("xhigh", profile)
    ctx.check(f"xhigh -> max on a route with no xhigh, got {got!r}", got == "max")


@test
def test_clamp_effort_xhigh_stays_xhigh_when_the_route_supports_it(ctx: Ctx):
    """Other families keep their per-family defaults/sets -- a chat-dialect
    route that DOES declare xhigh support (the harness-wide default
    `EFFORT_LEVELS`, unchanged) must never have it narrowed."""
    from halo_harness.providers.profiles import ProviderProfile, clamp_effort
    profile = ProviderProfile(reasoning_effort_supported=True)  # default effort_values_supported = EFFORT_LEVELS
    got = clamp_effort("xhigh", profile)
    ctx.check(f"xhigh preserved on a route that supports it, got {got!r}", got == "xhigh")


@test
def test_clamp_effort_max_becomes_xhigh_on_a_chat_route_that_supports_it(ctx: Ctx):
    """1.0.1 part 2 reviewer minor 2: `max` (the harness's strongest
    ANTHROPIC-dialect-style level) on a chat-dialect route that has no
    `max` but DOES list `xhigh` (e.g. Databricks' gpt-oss-120b row) must
    become `xhigh`, that route's own equivalent strongest level -- not
    fall all the way through to the bland "medium"/`reasoning_default_
    effort` default, two full levels below what the route can actually
    do (the mirror of xhigh -> max on an Anthropic route, already pinned
    above)."""
    from halo_harness.providers.profiles import ProviderProfile, clamp_effort
    profile = ProviderProfile(reasoning_effort_supported=True,
                               effort_values_supported=("low", "medium", "high", "xhigh"),
                               reasoning_default_effort="medium")
    got = clamp_effort("max", profile)
    ctx.check(f"max -> xhigh on a route with no max but xhigh, got {got!r}", got == "xhigh")


@test
def test_clamp_effort_max_falls_back_to_default_on_a_route_with_neither_max_nor_xhigh(ctx: Ctx):
    """The genuinely plain case the mirror rule above must NOT swallow:
    a route offering neither `max` nor `xhigh` still downgrades `max` to
    its own bland default, exactly as before."""
    from halo_harness.providers.profiles import ProviderProfile, clamp_effort
    profile = ProviderProfile(reasoning_effort_supported=True,
                               effort_values_supported=("low", "medium", "high"),
                               reasoning_default_effort="medium")
    got = clamp_effort("max", profile)
    ctx.check(f"max -> the route's own plain default, got {got!r}", got == "medium")


@test
def test_clamp_effort_unknown_value_falls_back_to_route_default(ctx: Ctx):
    from halo_harness.providers.profiles import ANTHROPIC_EFFORT_LEVELS, ProviderProfile, clamp_effort
    profile = ProviderProfile(thinking_format="anthropic_thinking", effort_values_supported=ANTHROPIC_EFFORT_LEVELS,
                               reasoning_default_effort="medium")
    got = clamp_effort("not-a-real-level", profile)
    ctx.check(f"unknown value -> the route's own default, got {got!r}", got == "medium")


@test
def test_clamp_effort_none_passes_through_untouched(ctx: Ctx):
    from halo_harness.providers.profiles import ProviderProfile, clamp_effort
    ctx.check("None (nothing configured) is never invented into a value",
              clamp_effort(None, ProviderProfile()) is None)


@test
def test_map_effort_chat_dialect_clamps_through_the_same_helper(ctx: Ctx):
    """map_effort (chat-dialect: OpenRouter/Databricks openai-chat) routes
    through clamp_effort too -- today a no-op for every row (full
    EFFORT_LEVELS is the default `effort_values_supported`), but wired so a
    future per-family restriction (model_table.json) is honoured
    automatically."""
    from halo_harness.providers.profiles import ProviderProfile, map_effort
    profile = ProviderProfile(reasoning_effort_supported=True, host_specific_fields=True)
    body = map_effort("xhigh", profile)
    ctx.check(f"xhigh reaches the wire verbatim on a route that supports it, got {body}",
              body == {"reasoning": {"effort": "xhigh"}})


# ---------------------------------------------------------------------------
# Session-level: the "high" default and re-clamp on /model switch
# ---------------------------------------------------------------------------

@test
def test_session_defaults_effort_to_high_for_anthropic_family_when_unset(ctx: Ctx):
    fh = build_fake_home()
    mock = MockDatabricks().start()
    try:
        session = _new_dbx_session(fh, mock, model="dbx:databricks-claude-opus-4-6@anthropic", effort=None)
        ctx.check(f"effort defaults to high, got {session.effort!r}", session.effort == "high")
        ctx.check(f"source labeled default, got {session.effort_source!r}", session.effort_source == "default")
    finally:
        mock.stop()


@test
def test_session_leaves_effort_unset_for_non_anthropic_family(ctx: Ctx):
    fh = build_fake_home()
    mock = MockDatabricks().start()
    try:
        # 2.0.1: Databricks GLM is the one chat-dialect family that now gets
        # an explicit default (tests/test_w2a_glm_default_effort.py), so the
        # "every other chat-dialect route stays omit -> provider default"
        # rule is pinned on a Kimi route here.
        session = _new_dbx_session(fh, mock, model="dbx:databricks-kimi-k3", effort=None)
        ctx.check(f"no new default for a chat-dialect route, got {session.effort!r}", session.effort is None)
    finally:
        mock.stop()


@test
def test_session_construction_clamps_an_explicit_xhigh_on_anthropic_route(ctx: Ctx):
    fh = build_fake_home()
    mock = MockDatabricks().start()
    try:
        session = _new_dbx_session(fh, mock, model="dbx:databricks-claude-opus-4-6@anthropic", effort="xhigh")
        ctx.check(f"xhigh clamped to max at construction, got {session.effort!r}", session.effort == "max")
    finally:
        mock.stop()


@test
def test_model_switch_reclamps_and_sets_change_note(ctx: Ctx):
    """Starting model is a generic chat-dialect route (Kimi), not GLM --
    Halo 2.0.1 gives Databricks GLM its OWN narrower low/high/max set
    (GLM-brief.md item 1), so `xhigh` would no longer pass through
    untouched there, which isn't what this test is about (model-switch
    reclamping in general)."""
    from halo_harness.model import ModelProfile, parse_model_ref
    fh = build_fake_home()
    mock = MockDatabricks().start()
    try:
        session = _new_dbx_session(fh, mock, model="dbx:databricks-kimi-k3", effort="xhigh")
        ctx.check("xhigh untouched on the starting chat route", session.effort == "xhigh")
        new_ref = parse_model_ref("dbx:databricks-claude-opus-4-6@anthropic")
        session.set_model(new_ref, ModelProfile())
        ctx.check(f"re-clamped for the new Anthropic route, got {session.effort!r}", session.effort == "max")
        ctx.check(f"a change note was set, got {session.effort_change_note!r}",
                  session.effort_change_note is not None and "max" in session.effort_change_note)
    finally:
        mock.stop()


# ---------------------------------------------------------------------------
# End to end: the wire body, and the 400 retry
# ---------------------------------------------------------------------------

@test
def test_anthropic_route_wire_body_carries_high_by_default(ctx: Ctx):
    fh = build_fake_home()
    mock = MockDatabricks().start()
    try:
        session = _new_dbx_session(fh, mock, model="dbx:databricks-claude-opus-4-6-ok@anthropic", effort=None)
        list(session.turn("howdy"))
        ctx.check(f"at least one request reached the mock, got {len(mock.requests)}", len(mock.requests) >= 1)
        body = mock.requests[0]["body"] or {}
        ctx.check(f"output_config.effort is high by default, got {body.get('output_config')}",
                  (body.get("output_config") or {}).get("effort") == "high")
    finally:
        mock.stop()


@test
def test_anthropic_route_wire_body_sends_max_for_xhigh(ctx: Ctx):
    fh = build_fake_home()
    mock = MockDatabricks().start()
    try:
        session = _new_dbx_session(fh, mock, model="dbx:databricks-claude-opus-4-6-ok@anthropic", effort="xhigh")
        list(session.turn("howdy"))
        body = mock.requests[0]["body"] or {}
        ctx.check(f"xhigh clamped to max on the wire, got {body.get('output_config')}",
                  (body.get("output_config") or {}).get("effort") == "max")
    finally:
        mock.stop()


@test
def test_effort_rejected_400_retries_once_with_effort_stripped(ctx: Ctx):
    """The backstop for a route whose accepted set isn't modeled correctly
    yet: a live 400 naming the effort field retries ONCE with it removed,
    and the turn still succeeds -- never surfaced as an error unless the
    retry ALSO fails."""
    fh = build_fake_home()
    mock = MockDatabricks().start()
    try:
        session = _new_dbx_session(
            fh, mock, model="dbx:databricks-claude-opus-4-6-effort-reject-unless-stripped@anthropic", effort="high")
        events_seen = list(session.turn("howdy"))
        errors = [e for e in events_seen if e.kind == "error"]
        ctx.check(f"no error surfaced, got kinds={[e.kind for e in events_seen]}", errors == [])
        ctx.check(f"exactly two upstream attempts, got {len(mock.requests)}", len(mock.requests) == 2)
        first_body, second_body = mock.requests[0]["body"] or {}, mock.requests[1]["body"] or {}
        ctx.check(f"first attempt carried the effort field, got {first_body.get('output_config')}",
                  "output_config" in first_body or "thinking" in first_body)
        ctx.check(f"retry stripped it, got {second_body.get('output_config')}",
                  "output_config" not in second_body and "thinking" not in second_body)
    finally:
        mock.stop()


@test
def test_is_effort_rejected_message_matches_the_live_wording(ctx: Ctx):
    from halo_harness.providers.errors import is_effort_rejected_message
    ctx.check("matches the verified live wording",
              is_effort_rejected_message("output_config.effort: Input should be 'low', 'medium', 'high' or 'max'"))
    ctx.check("matches a chat-dialect reasoning_effort 400 too",
              is_effort_rejected_message("Invalid value for reasoning_effort"))
    ctx.check("an unrelated 400 does not match", not is_effort_rejected_message("prompt is too long"))


# ---------------------------------------------------------------------------
# /effort command (hotfix 20.1) -- set + show + source
# ---------------------------------------------------------------------------

@test
def test_cmd_effort_bare_shows_effective_value_source_and_accepted_levels(ctx: Ctx):
    from halo_harness.commands.builtins import HeadlessFacade, _cmd_effort
    from halo_harness.providers.profiles import ANTHROPIC_EFFORT_LEVELS, ProviderProfile

    class _FakeSession:
        provider_profile = ProviderProfile(thinking_format="anthropic_thinking",
                                            effort_values_supported=ANTHROPIC_EFFORT_LEVELS,
                                            reasoning_effort_supported=True)
        effort = "high"
        effort_source = "default"

    facade = HeadlessFacade(cwd=Path("."), session=_FakeSession())
    out = _cmd_effort("", facade)
    ctx.check(f"shows the effective value, got {out!r}", "high" in out)
    ctx.check(f"shows the source, got {out!r}", "default" in out)
    ctx.check(f"lists the accepted levels, got {out!r}", "max" in out and "xhigh" not in out)


@test
def test_cmd_effort_with_argument_sets_and_confirms(ctx: Ctx):
    """The reported bug: '/effort medium' used to print whatever the
    facade's SNAPSHOT effort was (xhigh, from settings) three times in a
    row, ignoring the argument outright -- this must actually change the
    live session's own effort."""
    from halo_harness.commands.builtins import HeadlessFacade, _cmd_effort
    from halo_harness.providers.profiles import ANTHROPIC_EFFORT_LEVELS, ProviderProfile

    class _FakeSession:
        provider_profile = ProviderProfile(thinking_format="anthropic_thinking",
                                            effort_values_supported=ANTHROPIC_EFFORT_LEVELS,
                                            reasoning_effort_supported=True)
        effort = "xhigh"  # the pre-fix permanent snapshotted value
        effort_source = "settings"

    session = _FakeSession()
    facade = HeadlessFacade(cwd=Path("."), session=session, effort="xhigh")
    out = _cmd_effort("medium", facade)
    ctx.check(f"session effort actually changed, got {session.effort!r}", session.effort == "medium")
    ctx.check(f"source becomes session, got {session.effort_source!r}", session.effort_source == "session")
    ctx.check(f"confirmation names the value, got {out!r}", "medium" in out)

    out2 = _cmd_effort("xhigh", facade)
    ctx.check(f"xhigh clamped to max on this route, got {session.effort!r}", session.effort == "max")
    ctx.check(f"confirmation names max, got {out2!r}", "max" in out2)


@test
def test_cmd_effort_no_session_is_a_clean_message_not_a_crash(ctx: Ctx):
    from halo_harness.commands.builtins import HeadlessFacade, _cmd_effort
    facade = HeadlessFacade(cwd=Path("."), session=None, effort=None)
    out = _cmd_effort("high", facade)
    ctx.check(f"a clean message, got {out!r}", "running" in out.lower())


# ---------------------------------------------------------------------------
# 1.0.1 hotfix 22: gpt-6's own reasoning_effort+tools 400 -- verified live on
# `dbx:databricks-gpt-6-sol`, every turn failed with "Function tools with
# reasoning_effort are not supported for gpt-6-sol in /v1/chat/completions
# ... set reasoning_effort to 'none'." Refines hotfix 19.3's drop-the-field
# retry: this family needs the field set to "none" EXPLICITLY (the
# endpoint's own default is not none either).
# ---------------------------------------------------------------------------

@test
def test_gpt6_forces_reasoning_effort_none_when_tools_present(ctx: Ctx):
    from halo_harness.providers.profiles import map_effort, reset_model_table_cache, resolve_profile
    from halo_harness.providers.routing import Route
    reset_model_table_cache()
    route = Route(provider="databricks", upstream_model="databricks-gpt-6-sol", dialect="openai-chat")
    profile = resolve_profile(route)
    ctx.check(f"profile carries the override, got {profile.reasoning_effort_with_tools!r}",
              profile.reasoning_effort_with_tools == "none")
    body = map_effort("high", profile, has_tools=True)
    ctx.check(f"forced to none despite --effort high, got {body}", body == {"reasoning_effort": "none"})


@test
def test_gpt6_keeps_requested_effort_without_tools(ctx: Ctx):
    from halo_harness.providers.profiles import map_effort, reset_model_table_cache, resolve_profile
    from halo_harness.providers.routing import Route
    reset_model_table_cache()
    route = Route(provider="databricks", upstream_model="databricks-gpt-6-sol", dialect="openai-chat")
    profile = resolve_profile(route)
    body = map_effort("high", profile, has_tools=False)
    ctx.check(f"unaffected without tools, got {body}", body == {"reasoning_effort": "high"})


@test
def test_gpt_family_reasoning_effort_tools_400_retries_with_none(ctx: Ctx):
    """A DIFFERENT (non-gpt-6) OpenAI-family endpoint hitting the same
    wording -- the reactive backstop, independent of the gpt-6 model-table
    rule above (this model name deliberately does NOT contain "gpt-6")."""
    fh = build_fake_home()
    mock = MockDatabricks().start()
    try:
        session = _new_dbx_session(
            fh, mock, model="dbx:databricks-gpt-5-reasoning-effort-tools-reject-unless-none", effort="high")
        events_seen = list(session.turn("read a file"))
        errors = [e for e in events_seen if e.kind == "error"]
        ctx.check(f"no error surfaced, got kinds={[e.kind for e in events_seen]}", errors == [])
        ctx.check(f"exactly two upstream attempts, got {len(mock.requests)}", len(mock.requests) == 2)
        first_body, second_body = mock.requests[0]["body"] or {}, mock.requests[1]["body"] or {}
        ctx.check(f"first attempt sent the requested effort, got {first_body.get('reasoning_effort')!r}",
                  first_body.get("reasoning_effort") == "high")
        ctx.check(f"retry set it explicitly to none, got {second_body.get('reasoning_effort')!r}",
                  second_body.get("reasoning_effort") == "none")
    finally:
        mock.stop()


@test
def test_is_effort_with_tools_rejected_message_matches_the_live_wording(ctx: Ctx):
    from halo_harness.providers.errors import is_effort_with_tools_rejected_message
    ctx.check("matches the verified live gpt-6 wording", is_effort_with_tools_rejected_message(
        "Function tools with reasoning_effort are not supported for gpt-6-sol in /v1/chat/completions. "
        "To use function tools, use /v1/responses or set reasoning_effort to 'none'."))
    ctx.check("an unrelated 400 does not match", not is_effort_with_tools_rejected_message("prompt is too long"))
    ctx.check("the plain output_config.effort wording does not match this MORE SPECIFIC check",
              not is_effort_with_tools_rejected_message(
                  "output_config.effort: Input should be 'low', 'medium', 'high' or 'max'"))


if __name__ == "__main__":
    ctx = Ctx()
    results, passed, failed, skipped = run_all(TESTS, ctx)
    sys.exit(print_results(results, passed, failed, skipped))
