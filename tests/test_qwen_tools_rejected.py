"""tests.test_qwen_tools_rejected -- Halo 2.0.2 round 5 (Qwen-at-work
brief, items 1/4): `providers.request.ToolsNotSupported` (the pre-flight
guard -- never put `tools` on the wire for a profile that can't take
them), `providers.errors.is_tools_rejected_message` (the live-400 twin --
Databricks' own `unknown field "tools"`/OpenRouter's `no endpoints...
support tool use`), and `providers.learned_rules.learn_tools_rejected`/
`learned_tools_rejected` (the per-endpoint cache a live 400 writes to, so
the NEXT request against the same untabled endpoint never pays for the
round trip again -- "the mechanism 2.0.3 extends").
"""
from __future__ import annotations

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
    left to fall back to the real ~/.halo/sessions."""

    def __init__(self, fh=None):
        self._fh = fh

    def __enter__(self):
        import os
        self._saved = {k: os.environ.get(k) for k in
                       ("BRIDGE_TEST_HOME", "BRIDGE_STATE_DIR", "BRIDGE_ENV_FILE",
                        "OPENROUTER_API_KEY", "DATABRICKS_HOST", "DATABRICKS_TOKEN",
                        "ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN", "TYPESAFE_API_KEY")}
        if self._fh is not None:
            os.environ["BRIDGE_TEST_HOME"] = str(self._fh["home"])
            os.environ["BRIDGE_STATE_DIR"] = str(self._fh["home"] / ".halo")
        return self

    def __exit__(self, *exc):
        import os
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


# ---------------------------------------------------------------------------
# 1. The pre-flight guard: providers.request.ToolsNotSupported.
# ---------------------------------------------------------------------------

@test
def test_convert_tools_raises_for_a_decision_only_profile(ctx: Ctx):
    from halo_harness.providers.request import ToolsNotSupported, convert_tools
    from halo_harness.providers.profiles import resolve_profile
    from halo_harness.providers.routing import Route
    profile = resolve_profile(Route(provider="databricks", upstream_model="databricks-openjev-qwen35-4b",
                                     dialect="openai-chat"))
    tools = [{"name": "Read", "description": "d", "input_schema": {"type": "object", "properties": {}}}]
    try:
        convert_tools(tools, profile)
        ctx.check("ToolsNotSupported was raised", False)
    except ToolsNotSupported as e:
        ctx.check(f"names the model, got {e}", "databricks-openjev-qwen35-4b" in str(e))
        ctx.check("names the judge role", "judge" in str(e))


@test
def test_convert_tools_unaffected_for_an_ordinary_profile(ctx: Ctx):
    from halo_harness.providers.request import convert_tools
    from halo_harness.providers.profiles import resolve_profile
    from halo_harness.providers.routing import Route
    profile = resolve_profile(Route(provider="databricks", upstream_model="databricks-qwen35-122b-a10b",
                                     dialect="openai-chat"))
    tools = [{"name": "Read", "description": "d", "input_schema": {"type": "object", "properties": {}}}]
    out = convert_tools(tools, profile)
    ctx.check(f"tools pass through unchanged, got {out}", out is not None and len(out) == 1)


# ---------------------------------------------------------------------------
# 2. The live-400 message classifier.
# ---------------------------------------------------------------------------

@test
def test_is_tools_rejected_message_matches_known_wordings(ctx: Ctx):
    from halo_harness.providers.errors import is_tools_rejected_message
    ctx.check("databricks strict-allowlist wording",
              is_tools_rejected_message('json: unknown field "tools"'))
    ctx.check("openrouter routing 404 wording",
              is_tools_rejected_message("404 No endpoints found that support tool use"))
    ctx.check("an unrelated 400 never matches",
              not is_tools_rejected_message("json: unknown field \"reasoning_effort\""))
    ctx.check("ordinary prose never matches", not is_tools_rejected_message("rate limit exceeded"))


# ---------------------------------------------------------------------------
# 3. The learned-rule pair (mirrors learn_reasoning_effort_with_tools' own
#    pinning tests in test_h15_effort_remainder.py).
# ---------------------------------------------------------------------------

@test
def test_learned_tools_rejected_round_trip(ctx: Ctx):
    from halo_harness.providers.learned_rules import learn_tools_rejected, learned_tools_rejected
    state_dir = Path(tempfile.mkdtemp(prefix="qwen-tools-rejected-learned-"))
    ctx.check("nothing learned yet", learned_tools_rejected(state_dir, "databricks", "my-model") is False)
    learn_tools_rejected(state_dir, "databricks", "my-model")
    ctx.check("learned value reads back", learned_tools_rejected(state_dir, "databricks", "my-model") is True)
    ctx.check("a DIFFERENT endpoint never learned anything",
              learned_tools_rejected(state_dir, "databricks", "other-model") is False)


@test
def test_learned_tools_rejected_expires_after_its_ttl(ctx: Ctx):
    """2.0.2 review finding 32 pin: a learned "tools_rejected" row never
    expired before this -- one transient 400 made an endpoint
    permanently unusable for tools until learned-rules.json was edited
    by hand. A fresh learn + a hand-aged timestamp past the TTL must
    read back False (and so does a pre-existing row with NO timestamp
    at all, as if written before this fix shipped)."""
    import json
    from halo_harness.providers.learned_rules import (
        TOOLS_REJECTED_TTL_S, _key, _path, learn_tools_rejected, learned_tools_rejected, load_learned_rules,
    )
    state_dir = Path(tempfile.mkdtemp(prefix="qwen-tools-rejected-ttl-"))
    learn_tools_rejected(state_dir, "databricks", "my-model")
    ctx.check("freshly learned reads back True", learned_tools_rejected(state_dir, "databricks", "my-model") is True)

    rules = load_learned_rules(state_dir)
    rules[_key("databricks", "my-model")]["tools_rejected_at"] -= (TOOLS_REJECTED_TTL_S + 1)
    _path(state_dir).write_text(json.dumps(rules), encoding="utf-8")
    ctx.check("expired after the TTL -- re-learned fresh on the next live call",
              learned_tools_rejected(state_dir, "databricks", "my-model") is False)

    rules2 = load_learned_rules(state_dir)
    rules2[_key("databricks", "old-model")] = {"tools_rejected": True}  # no timestamp -- pre-fix row shape
    _path(state_dir).write_text(json.dumps(rules2), encoding="utf-8")
    ctx.check("a pre-existing row with no timestamp at all is treated as expired, not stuck forever",
              learned_tools_rejected(state_dir, "databricks", "old-model") is False)


@test
def test_resolve_profile_consults_the_learned_rule_for_an_untabled_endpoint(ctx: Ctx):
    from halo_harness.providers.learned_rules import learn_tools_rejected
    from halo_harness.providers.profiles import reset_model_table_cache, resolve_profile
    from halo_harness.providers.routing import Route
    reset_model_table_cache()
    state_dir = Path(tempfile.mkdtemp(prefix="qwen-tools-rejected-resolve-"))
    model_id = "databricks-some-brand-new-untabled-endpoint"
    route = Route(provider="databricks", upstream_model=model_id, dialect="openai-chat")
    before = resolve_profile(route, state_dir=state_dir)
    ctx.check("tools_supported True before anything is learned", before.tools_supported is True)
    learn_tools_rejected(state_dir, "databricks", model_id)
    after = resolve_profile(route, state_dir=state_dir)
    ctx.check("tools_supported False once the live rule is learned", after.tools_supported is False)


# ---------------------------------------------------------------------------
# 4. End to end against the mock server: a live 400 learns the rule AND
#    ends the turn with a clear error; a fresh session against the SAME
#    endpoint never even tries to send tools again.
# ---------------------------------------------------------------------------

_LIVE_MODEL = "databricks-mystery-endpoint-tools-rejected-400"


def _new_dbx_session(fh, mock, state_dir, *, model_id=_LIVE_MODEL):
    import os
    from halo_harness.agent.assemble import SessionContext
    from halo_harness.agent.loop import Session
    from halo_harness.model import ModelProfile, parse_model_ref
    from halo_harness.providers.stream import ProviderCreds
    os.environ["DATABRICKS_HOST"] = "https://test.cloud.databricks.com"
    os.environ["DATABRICKS_TOKEN"] = "test-token"
    model = f"dbx:{model_id}"
    session_ctx = SessionContext(cwd=fh["proj"], model_label=model)
    return Session(
        cwd=fh["proj"], model_ref=parse_model_ref(model), model_profile=ModelProfile(),
        creds=ProviderCreds(base_url=mock.root, api_key="test-token"),
        state_dir=state_dir, model_label=model, session_context=session_ctx, max_turns=6,
    )


@test
def test_live_400_learns_tools_rejected_and_ends_the_turn_clearly(ctx: Ctx):
    from halo_harness.providers.profiles import reset_model_table_cache
    reset_model_table_cache()
    fh = build_fake_home()
    with _Env(fh):
        mock = MockDatabricks().start()
        try:
            state_dir = Path(tempfile.mkdtemp(prefix="qwen-tools-rejected-live-"))
            session = _new_dbx_session(fh, mock, state_dir)
            events_seen = list(session.turn("hello"))
            errors = [e for e in events_seen if e.kind == "error"]
            ctx.check(f"exactly one terminal error, got kinds={[e.kind for e in events_seen]}", len(errors) == 1)
            ctx.check(f"err_type names the condition, got {errors[0].data.get('err_type')!r}",
                      errors[0].data.get("err_type") == "tools_not_supported")
            ctx.check(f"exactly one (wasted, teaching) attempt, got {len(mock.requests)}", len(mock.requests) == 1)
            ctx.check("that one attempt DID carry tools -- Halo didn't know yet",
                      bool((mock.requests[0]["body"] or {}).get("tools")))
            from halo_harness.providers.learned_rules import learned_tools_rejected
            ctx.check("persisted to the per-endpoint cache on disk",
                      learned_tools_rejected(state_dir, "databricks", _LIVE_MODEL) is True)
        finally:
            mock.stop()


@test
def test_a_fresh_session_against_the_same_endpoint_never_sends_tools_again(ctx: Ctx):
    from halo_harness.providers.profiles import reset_model_table_cache
    from halo_harness.providers.learned_rules import learn_tools_rejected
    reset_model_table_cache()
    fh = build_fake_home()
    with _Env(fh):
        mock = MockDatabricks().start()
        try:
            state_dir = Path(tempfile.mkdtemp(prefix="qwen-tools-rejected-persist-"))
            learn_tools_rejected(state_dir, "databricks", _LIVE_MODEL)
            session = _new_dbx_session(fh, mock, state_dir)
            ctx.check("the FRESH session's own profile already reflects it at construction time",
                      session.provider_profile.tools_supported is False)
            events_seen = list(session.turn("hello"))
            errors = [e for e in events_seen if e.kind == "error"]
            ctx.check(f"still a clear terminal error (never silently tool-less), got "
                      f"{[e.kind for e in events_seen]}", len(errors) == 1)
            ctx.check(f"err_type names the condition, got {errors[0].data.get('err_type')!r}",
                      errors[0].data.get("err_type") == "tools_not_supported")
            ctx.check(f"ZERO wire requests this time -- caught before ever sending, got {len(mock.requests)}",
                      len(mock.requests) == 0)
        finally:
            mock.stop()


@test
def test_the_real_tabled_openjev_endpoint_never_reaches_the_wire_at_all(ctx: Ctx):
    """End to end through the FULL Session/turn() path (not just the unit-
    level convert_tools check) for the actual tabled row -- proactive
    classification means this never even costs the one "teaching" round
    trip the untabled test above needs."""
    from halo_harness.providers.profiles import reset_model_table_cache
    reset_model_table_cache()
    fh = build_fake_home()
    with _Env(fh):
        mock = MockDatabricks().start()
        try:
            state_dir = Path(tempfile.mkdtemp(prefix="qwen-tools-rejected-tabled-"))
            session = _new_dbx_session(fh, mock, state_dir, model_id="databricks-openjev-qwen35-4b")
            ctx.check("decision_only already known at construction time",
                      session.provider_profile.decision_only is True)
            events_seen = list(session.turn("hello"))
            errors = [e for e in events_seen if e.kind == "error"]
            ctx.check(f"a clear terminal error, got {[e.kind for e in events_seen]}", len(errors) == 1)
            ctx.check(f"err_type names the condition, got {errors[0].data.get('err_type')!r}",
                      errors[0].data.get("err_type") == "tools_not_supported")
            ctx.check(f"ZERO wire requests -- never even tried, got {len(mock.requests)}", len(mock.requests) == 0)
        finally:
            mock.stop()


if __name__ == "__main__":
    ctx = Ctx()
    results, passed, failed, skipped = run_all(TESTS, ctx)
    sys.exit(print_results(results, passed, failed, skipped))
