"""tests.test_w5_fallback_model -- W5 (carried from W4a): `--fallback-model`
wiring into `_step`'s own retry ladder (agent/loop.py::Session._step /
_try_fallback_after_exhaustion). Pinning test against the mock upstream
(tests/helpers/mock_openai.py): the primary model 429s forever (a zero-
second Retry-After so the ladder's own backoff costs no real wall time),
its retry ladder exhausts (MAX_RETRIES), and the session swaps onto the
`--fallback-model` entry for the rest of the turn, with one `notification`
event marking the switch and a real streamed reply coming back from the
SECOND model.
"""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.helpers.runner import Ctx, new_registry, print_results, run_all
from tests.helpers.fake_home import build_fake_home
from tests.helpers.mock_openai import MockUpstream, SCENARIOS, _finish, send_json_response
from tests.helpers.provider_env_defaults import ensure_default_provider_credentials

ensure_default_provider_credentials()

test, TESTS = new_registry()


def _new_session(fh, mock, *, model, fallback_models=None):
    from halo_harness.agent.assemble import SessionContext
    from halo_harness.agent.loop import Session
    from halo_harness.model import ModelProfile, parse_model_ref
    from halo_harness.providers.stream import ProviderCreds
    os.environ["BRIDGE_TEST_HOME"] = str(fh["home"])
    os.environ["BRIDGE_OPENROUTER_BASE_URL"] = mock.base_url
    session_ctx = SessionContext(cwd=fh["proj"], model_label=model)
    model_ref = parse_model_ref(model)
    return Session(
        cwd=fh["proj"], model_ref=model_ref, model_profile=ModelProfile(),
        creds=ProviderCreds(base_url=mock.base_url, api_key="k"),
        state_dir=Path(tempfile.mkdtemp(prefix="fallback-model-")), model_label=model,
        session_context=session_ctx, openrouter_base_url=mock.base_url, max_turns=6,
        cli_flags={"fallback_models": fallback_models or []},
    )


@test
def test_repeated_429_exhaustion_swaps_to_the_fallback_model(ctx: Ctx):
    calls = {"primary": 0, "fallback": 0}

    def _scn_primary_busy(h, body):
        calls["primary"] += 1
        send_json_response(h, 429, {"error": {"message": "rate limited", "type": "rate_limit_error"}},
                            {"Retry-After": "0"})

    def _scn_fallback_ok(h, body):
        calls["fallback"] += 1
        _finish(h, [
            {"choices": [{"index": 0, "delta": {"role": "assistant"}}]},
            {"choices": [{"index": 0, "delta": {"content": "answered by the fallback"}}]},
            {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
        ])

    SCENARIOS["fallback-primary-busy"] = _scn_primary_busy
    SCENARIOS["fallback-secondary-ok"] = _scn_fallback_ok
    fh = build_fake_home()
    mock = MockUpstream().start()
    try:
        session = _new_session(
            fh, mock, model="or:mock/fallback-primary-busy",
            fallback_models=["or:mock/fallback-secondary-ok"],
        )
        events_seen = list(session.turn("hi"))
        ctx.check(f"the primary was called MAX_RETRIES+1 times then abandoned, got {calls}",
                   calls["primary"] == 6)
        ctx.check(f"the fallback then answered, got {calls}", calls["fallback"] == 1)
        notes = [e.data.get("text", "") for e in events_seen if e.kind == "notification"]
        ctx.check(f"one notice line names both models, got {notes}",
                   any("fallback-primary-busy" in n and "fallback-secondary-ok" in n for n in notes))
        texts = [e.data.get("text", "") for e in events_seen if e.kind == "text_delta"]
        ctx.check(f"the fallback's real reply streamed through, got {texts}",
                   "".join(texts) == "answered by the fallback")
        errors = [e for e in events_seen if e.kind == "error"]
        ctx.check(f"no terminal error -- the turn recovered via fallback, got {errors}", errors == [])
        # finding 4 (W6a): the notice's own wording says the swap lasts
        # "for the rest of this turn" -- it used to stay on the fallback
        # forever (every later turn, too). The turn is over by the time
        # `session.turn()` returns, so the primary must already be back.
        ctx.check(f"the session is back on the PRIMARY once the turn ends, got {session.model_ref.raw}",
                   session.model_ref.raw == "or:mock/fallback-primary-busy")
    finally:
        mock.stop()
        os.environ.pop("BRIDGE_OPENROUTER_BASE_URL", None)
        os.environ.pop("BRIDGE_TEST_HOME", None)


@test
def test_no_fallback_configured_still_fails_exactly_as_before(ctx: Ctx):
    """No `--fallback-model` -- `_fallback_remaining` is empty, so
    `apply_next_fallback_model` is a no-op and the ladder's existing
    terminal-failure path fires exactly as it always did (a regression
    guard for the un-flagged path)."""
    def _scn_busy(h, body):
        send_json_response(h, 429, {"error": {"message": "rate limited", "type": "rate_limit_error"}},
                            {"Retry-After": "0"})

    SCENARIOS["fallback-no-config-busy"] = _scn_busy
    fh = build_fake_home()
    mock = MockUpstream().start()
    try:
        session = _new_session(fh, mock, model="or:mock/fallback-no-config-busy")
        events_seen = list(session.turn("hi"))
        errors = [e for e in events_seen if e.kind == "error"]
        ctx.check(f"terminal error, unchanged from before this feature existed, got kinds="
                   f"{[e.kind for e in events_seen]}", len(errors) == 1)
        notes = [e.data.get("text", "") for e in events_seen if e.kind == "notification"]
        ctx.check(f"no fallback notice -- none was configured, got {notes}", notes == [])
    finally:
        mock.stop()
        os.environ.pop("BRIDGE_OPENROUTER_BASE_URL", None)
        os.environ.pop("BRIDGE_TEST_HOME", None)


@test
def test_f4_primary_is_retried_again_at_the_start_of_the_next_turn(ctx: Ctx):
    """finding 4 (W6a): part 10's own commit message and the in-turn
    notice both say the primary is retried "at the start of each user
    turn" -- it never actually was; a fallback used once stayed the
    session's model for every LATER turn too. Two turns, same session,
    the primary 429ing forever: turn 2 must try the primary a full ladder
    again, not skip straight to the fallback it's still parked on."""
    calls = {"primary": 0, "fallback": 0}

    def _scn_primary_busy2(h, body):
        calls["primary"] += 1
        send_json_response(h, 429, {"error": {"message": "rate limited", "type": "rate_limit_error"}},
                            {"Retry-After": "0"})

    def _scn_fallback_ok2(h, body):
        calls["fallback"] += 1
        _finish(h, [
            {"choices": [{"index": 0, "delta": {"role": "assistant"}}]},
            {"choices": [{"index": 0, "delta": {"content": "answered by the fallback"}}]},
            {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
        ])

    SCENARIOS["fallback-primary-busy-2turn"] = _scn_primary_busy2
    SCENARIOS["fallback-secondary-ok-2turn"] = _scn_fallback_ok2
    fh = build_fake_home()
    mock = MockUpstream().start()
    try:
        session = _new_session(
            fh, mock, model="or:mock/fallback-primary-busy-2turn",
            fallback_models=["or:mock/fallback-secondary-ok-2turn"],
        )
        list(session.turn("turn one"))
        ctx.check(f"turn 1: primary tried MAX_RETRIES+1 times, got {calls}", calls["primary"] == 6)
        ctx.check(f"turn 1: fallback answered once, got {calls}", calls["fallback"] == 1)
        ctx.check(f"turn 1 ends back on the primary, got {session.model_ref.raw}",
                   session.model_ref.raw == "or:mock/fallback-primary-busy-2turn")

        list(session.turn("turn two"))
        ctx.check(f"turn 2: the primary is tried again from scratch, got {calls}", calls["primary"] == 12)
        ctx.check(f"turn 2: falls back again, got {calls}", calls["fallback"] == 2)
    finally:
        mock.stop()
        os.environ.pop("BRIDGE_OPENROUTER_BASE_URL", None)
        os.environ.pop("BRIDGE_TEST_HOME", None)


@test
def test_f4_current_model_excluded_from_its_own_fallback_list(ctx: Ctx):
    """finding 4 (W6a): if `--fallback-model` names the session's OWN
    current model (directly, or because a PRIOR turn's primary ended up
    listed), trying to "switch" to itself must never waste a whole retry
    ladder before reaching a REAL fallback entry."""
    calls = {"primary": 0, "fallback": 0}

    def _scn_primary_busy3(h, body):
        calls["primary"] += 1
        send_json_response(h, 429, {"error": {"message": "rate limited", "type": "rate_limit_error"}},
                            {"Retry-After": "0"})

    def _scn_fallback_ok3(h, body):
        calls["fallback"] += 1
        _finish(h, [
            {"choices": [{"index": 0, "delta": {"role": "assistant"}}]},
            {"choices": [{"index": 0, "delta": {"content": "answered by the real fallback"}}]},
            {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
        ])

    SCENARIOS["fallback-primary-busy-3"] = _scn_primary_busy3
    SCENARIOS["fallback-secondary-ok-3"] = _scn_fallback_ok3
    fh = build_fake_home()
    mock = MockUpstream().start()
    try:
        session = _new_session(
            fh, mock, model="or:mock/fallback-primary-busy-3",
            fallback_models=["or:mock/fallback-primary-busy-3", "or:mock/fallback-secondary-ok-3"],
        )
        events_seen = list(session.turn("hi"))
        ctx.check(f"the primary is tried exactly one ladder's worth (6), never 12 from a wasted "
                  f"self-swap, got {calls}", calls["primary"] == 6)
        ctx.check(f"the real fallback answered once, got {calls}", calls["fallback"] == 1)
        texts = [e.data.get("text", "") for e in events_seen if e.kind == "text_delta"]
        ctx.check(f"the real fallback's reply streamed through, got {texts}",
                  "".join(texts) == "answered by the real fallback")
    finally:
        mock.stop()
        os.environ.pop("BRIDGE_OPENROUTER_BASE_URL", None)
        os.environ.pop("BRIDGE_TEST_HOME", None)


@test
def test_f3_cross_provider_fallback_resolves_its_own_credentials(ctx: Ctx):
    """finding 3 (W6a): `apply_next_fallback_model` used to pass
    `self.creds` (the CURRENT model's own credentials) unconditionally --
    a cross-provider fallback sent its request to the NEW provider's
    upstream with the OLD provider's base_url/key."""
    fh = build_fake_home()
    mock = MockUpstream().start()
    try:
        session = _new_session(fh, mock, model="or:mock/model-f3", fallback_models=["dbx:databricks-glm-5-3"])
        session._fallback_remaining = list(session.fallback_models)
        applied = session.apply_next_fallback_model()
        ctx.check("the fallback was applied", applied)
        ctx.check(f"the session switched provider, got {session.model_ref.raw}",
                  session.model_ref.provider == "databricks")
        ctx.check(f"creds are the fallback's OWN Databricks creds, not the primary's OpenRouter ones, "
                  f"got base_url={session.creds.base_url!r}",
                  session.creds.base_url != mock.base_url and "databricks" in (session.creds.base_url or "").lower())
    finally:
        mock.stop()
        os.environ.pop("BRIDGE_OPENROUTER_BASE_URL", None)
        os.environ.pop("BRIDGE_TEST_HOME", None)


@test
def test_f3_fallback_entry_with_no_credentials_is_skipped_not_installed(ctx: Ctx):
    """An entry whose provider has no usable credentials at all must be
    skipped exactly like an unresolvable model string -- never installed
    with `creds=None` (every later request on it would then fail outright
    with no credentials at all)."""
    fh = build_fake_home()
    mock = MockUpstream().start()
    snapshot = {k: os.environ.get(k) for k in ("DATABRICKS_HOST", "DATABRICKS_TOKEN")}
    try:
        os.environ.pop("DATABRICKS_HOST", None)
        os.environ.pop("DATABRICKS_TOKEN", None)
        session = _new_session(fh, mock, model="or:mock/model-f3b", fallback_models=["dbx:databricks-glm-5-3"])
        before_ref = session.model_ref
        session._fallback_remaining = list(session.fallback_models)
        applied = session.apply_next_fallback_model()
        ctx.check(f"no usable fallback left (the only entry had no creds) -> False, got {applied}",
                  applied is False)
        ctx.check(f"the session never switched off the primary, got {session.model_ref.raw}",
                  session.model_ref.raw == before_ref.raw)
    finally:
        mock.stop()
        os.environ.pop("BRIDGE_OPENROUTER_BASE_URL", None)
        os.environ.pop("BRIDGE_TEST_HOME", None)
        for k, v in snapshot.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


if __name__ == "__main__":
    ctx = Ctx()
    results, passed, failed, skipped = run_all(TESTS, ctx)
    sys.exit(print_results(results, passed, failed, skipped))
