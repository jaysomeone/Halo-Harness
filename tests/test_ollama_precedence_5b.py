"""tests.test_ollama_precedence_5b -- Halo 2.0.3 round 5b: `providers.
ollama.resolve_num_ctx_and_source`'s learned-cap/host-max_ctx/live-fit/
remote-default precedence chain (brief item 3, the MUST-FIX from the
LAN-host live run), plus the stable-request-prefix pinning test (brief's
own "Stable request prefix" bullet). Pure functions and `providers.
ollama_request.build_ollama_request_body` only -- no network, no mock
server needed here (see tests/test_ollama_calibrate.py for the parts that
DO need mock_ollama).
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.helpers.provider_env_defaults import ensure_default_provider_credentials
ensure_default_provider_credentials()

from tests.helpers.runner import Ctx, new_registry, print_results, run_all

test, TESTS = new_registry()


# ---- resolve_num_ctx_and_source / compute_num_ctx precedence -------------

@test
def test_every_pre_5b_pinned_case_is_unchanged(ctx: Ctx):
    """The exact assertions tests/test_providers_ollama.py and
    tests/test_providers_ollama_hw.py already pin for `compute_num_ctx`'s
    three-positional-arg (pre-5b) call shape -- re-pinned here as a single
    regression sentinel for the round-5b rewrite, which MUST produce
    identical numbers for every one of these when `learned_cap`/`remote`
    are left at their defaults."""
    from halo_harness.providers.ollama import FALLBACK_NUM_CTX, HARD_CONTEXT_CAP, compute_num_ctx
    from halo_harness.providers.ollama_fit import WEIGHTS_DO_NOT_FIT
    ctx.check("nothing known -> fallback", compute_num_ctx(None, None, None) == FALLBACK_NUM_CTX)
    ctx.check("trained context wins when smallest", compute_num_ctx(16384, 32768, None) == 16384)
    ctx.check("host.max_ctx wins when smallest", compute_num_ctx(131072, 32768, None) == 32768)
    ctx.check("a fit estimate wins when smallest", compute_num_ctx(131072, 65536, 8000) == 8000)
    ctx.check("never exceeds the hard cap", compute_num_ctx(1_000_000, None, None) == HARD_CONTEXT_CAP)
    ctx.check("weights-do-not-fit -> fallback, not the hard cap",
              compute_num_ctx(262144, None, WEIGHTS_DO_NOT_FIT) == 8192)
    ctx.check("weights-do-not-fit + a smaller host.max_ctx -> that override",
              compute_num_ctx(262144, 4096, WEIGHTS_DO_NOT_FIT) == 4096)
    ctx.check("plain unknown fit -> the hard cap still stands",
              compute_num_ctx(262144, None, None) == 131072)


@test
def test_learned_cap_overrides_a_stale_live_fit_estimate(ctx: Ctx):
    """Ground truth (a real `halo ollama calibrate` measurement) beats a
    computed guess -- even a guess that itself claims WEIGHTS_DO_NOT_FIT,
    since a learned cap is proof the model DOES fit at SOME size."""
    from halo_harness.providers.ollama import resolve_num_ctx_and_source
    from halo_harness.providers.ollama_fit import WEIGHTS_DO_NOT_FIT
    num_ctx, source = resolve_num_ctx_and_source(262144, None, 8192, learned_cap=65536)
    ctx.check(f"learned cap (65536) wins over the smaller live estimate (8192), got {num_ctx}", num_ctx == 65536)
    ctx.check(f"source names the learned cap, got {source!r}", source == "learned cap")
    num_ctx2, source2 = resolve_num_ctx_and_source(262144, None, WEIGHTS_DO_NOT_FIT, learned_cap=65536)
    ctx.check(f"learned cap overrides WEIGHTS_DO_NOT_FIT too, got {num_ctx2}", num_ctx2 == 65536)
    ctx.check(f"source2 names the learned cap, got {source2!r}", source2 == "learned cap")
    ctx.check("the 8192 fallback is NOT invoked once a learned cap is known",
              num_ctx2 != 8192)


@test
def test_learned_cap_still_bounded_by_trained_context_and_hard_cap(ctx: Ctx):
    """A learned cap never lets `num_ctx` exceed the model's own trained
    context or the 131072 hard cap -- it competes in the SAME min(), it
    never bypasses them."""
    from halo_harness.providers.ollama import resolve_num_ctx_and_source
    num_ctx, source = resolve_num_ctx_and_source(16384, None, None, learned_cap=65536)
    ctx.check(f"trained context (16384) still wins over a bigger learned cap, got {num_ctx}", num_ctx == 16384)
    ctx.check(f"source names trained context, got {source!r}", source == "trained context")
    # trained_context must be KNOWN (not None) here -- an unknown trained
    # context is a SEPARATE, pre-5b-documented reason the 8192 fallback
    # joins the candidate pool (see resolve_num_ctx_and_source's own
    # docstring: "still clamped... like any other candidate" -- it is
    # just one more min() entry, not a special override), so pairing an
    # absurd learned_cap with trained_context=None would correctly yield
    # 8192 via that unrelated rule, not what THIS test means to isolate.
    num_ctx2, source2 = resolve_num_ctx_and_source(999_999_999, None, None, learned_cap=999_999_999)
    ctx.check(f"the 131072 hard cap still wins over an absurd learned cap, got {num_ctx2}", num_ctx2 == 131072)
    ctx.check(f"source2 names the hard cap, got {source2!r}", source2 == "hard cap")


@test
def test_remote_host_nothing_known_gets_32768_never_131072(ctx: Ctx):
    """The LAN-host live-run MUST FIX: a remote host with no host.max_ctx,
    no learned cap, and no live fit estimate gets the conservative 32768
    default -- never the 131072 hard cap."""
    from halo_harness.providers.ollama import REMOTE_UNKNOWN_DEFAULT_NUM_CTX, resolve_num_ctx_and_source
    num_ctx, source = resolve_num_ctx_and_source(262144, None, None, remote=True)
    ctx.check(f"32768, not 131072, got {num_ctx}", num_ctx == REMOTE_UNKNOWN_DEFAULT_NUM_CTX == 32768)
    ctx.check(f"source names the remote default, got {source!r}", source == "remote default")
    # A LOCAL host with nothing known still gets the ordinary hard cap --
    # the conservative default is specifically for the "no GPU read at
    # all" remote case, never for a local host (which always has an OS
    # probe to fall back on well before "nothing known" would apply).
    num_ctx_local, source_local = resolve_num_ctx_and_source(262144, None, None, remote=False)
    ctx.check(f"local host, nothing known -> the ordinary hard cap, got {num_ctx_local}", num_ctx_local == 131072)
    ctx.check(f"source names the hard cap, got {source_local!r}", source_local == "hard cap")


@test
def test_remote_host_max_ctx_is_the_documented_explicit_override(ctx: Ctx):
    """`ollama.hosts[].max_ctx` is the documented override for the remote-
    unknown-default case (release-notes-for-fix-pass.md) -- once it is
    SET, "nothing known" is no longer true, so the operator's own number
    wins (still bounded by the hard cap -- see the next test)."""
    from halo_harness.providers.ollama import resolve_num_ctx_and_source
    num_ctx, source = resolve_num_ctx_and_source(262144, 16384, None, remote=True)
    ctx.check(f"host.max_ctx (16384) wins, not 32768, got {num_ctx}", num_ctx == 16384)
    ctx.check(f"source names host max_ctx, got {source!r}", source == "host max_ctx")


@test
def test_remote_host_max_ctx_still_never_exceeds_hard_cap(ctx: Ctx):
    from halo_harness.providers.ollama import HARD_CONTEXT_CAP, resolve_num_ctx_and_source
    num_ctx, source = resolve_num_ctx_and_source(262144, 500_000, None, remote=True)
    ctx.check(f"the hard cap still wins over an oversized host.max_ctx, got {num_ctx}", num_ctx == HARD_CONTEXT_CAP)
    ctx.check(f"source names the hard cap, got {source!r}", source == "hard cap")


@test
def test_remote_with_a_live_fit_estimate_does_not_need_the_default(ctx: Ctx):
    """"nothing known" specifically means no host.max_ctx AND no usable
    fit signal -- a remote host that DOES have a live fit estimate (e.g.
    its own already-loaded /api/ps context, or an ssh GPU read) never
    falls back to the 32768 default at all."""
    from halo_harness.providers.ollama import resolve_num_ctx_and_source
    num_ctx, source = resolve_num_ctx_and_source(262144, None, 16384, remote=True)
    ctx.check(f"the live fit estimate (16384) wins, got {num_ctx}", num_ctx == 16384)
    ctx.check(f"source names the fit estimate, got {source!r}", source == "fit estimate")


# ---- stable request prefix (brief's own "Stable request prefix" bullet) --

def _route_profile(model="qwen3:30b"):
    from halo_harness.providers.profiles import resolve_profile
    from halo_harness.providers.routing import Route
    route = Route(provider="ollama", upstream_model=model, dialect="ollama")
    return route, resolve_profile(route)


@test
def test_prefix_byte_stable_across_two_consecutive_turns(ctx: Ctx):
    """Builds two bodies simulating turn 1 and turn 2 of the SAME `ol:`
    session (identical system_text/tools/host/trained_context/fit_
    estimate -- the ordinary case once a model is loaded, per round 3's
    own fix pass: a loaded model's fit estimate is read from `/api/ps`'s
    own context_length and does not fluctuate turn to turn) and asserts
    the PREFIX -- the leading system message, the tools array, and
    `options` -- is byte-identical. Tool list passed in a DIFFERENT
    order between the two calls on purpose: `convert_tools`'s own
    alphabetical sort (not this test) is what makes tool order stable
    regardless of the caller's own list order."""
    from halo_harness.providers.ollama import OllamaHost
    from halo_harness.providers.ollama_request import build_ollama_request_body
    route, profile = _route_profile()
    host = OllamaHost(name="default", url="http://127.0.0.1:11434")
    tools_turn1 = [
        {"name": "Read", "description": "Read a file", "input_schema": {"type": "object", "properties": {}}},
        {"name": "Bash", "description": "Run a command", "input_schema": {"type": "object", "properties": {}}},
    ]
    tools_turn2 = list(reversed(tools_turn1))  # same SET, deliberately different order

    turn1_messages = [{"role": "user", "content": [{"type": "text", "text": "hello"}]}]
    body1 = build_ollama_request_body(system_text="You are Halo.", messages=turn1_messages, tools=tools_turn1,
                                       tool_choice=None, route=route, profile=profile, effort="medium", host=host,
                                       trained_context=40960, fit_estimate=16384)
    turn2_messages = turn1_messages + [
        {"role": "assistant", "content": [{"type": "text", "text": "hi there"}]},
        {"role": "user", "content": [{"type": "text", "text": "again"}]},
    ]
    body2 = build_ollama_request_body(system_text="You are Halo.", messages=turn2_messages, tools=tools_turn2,
                                       tool_choice=None, route=route, profile=profile, effort="medium", host=host,
                                       trained_context=40960, fit_estimate=16384)
    ctx.check(f"leading system message identical, got {body1['messages'][0]!r} vs {body2['messages'][0]!r}",
              body1["messages"][0] == body2["messages"][0])
    ctx.check(f"system message is actually the leading one (role=system)",
              body1["messages"][0]["role"] == "system")
    ctx.check(f"tools array identical (order-independent input), got {body1.get('tools')} vs {body2.get('tools')}",
              body1.get("tools") == body2.get("tools"))
    ctx.check(f"options identical, got {body1['options']} vs {body2['options']}",
              body1["options"] == body2["options"])
    # The part that's VOLATILE BY DESIGN: the full messages list grows
    # (turn 2 carries turn 1's own reply plus the new user turn) -- never
    # claimed stable, only the prefix (system/tools/options) is.
    ctx.check("the full messages list legitimately GROWS turn to turn (not part of the stable prefix)",
              len(body2["messages"]) > len(body1["messages"]))


@test
def test_prefix_stable_even_when_learned_cap_and_remote_are_set(ctx: Ctx):
    """The round-5b additions (`learned_cap`/`remote`) must not themselves
    introduce volatility -- same two-turn shape, now also passing a
    learned cap and remote=True, still byte-stable."""
    from halo_harness.providers.ollama import OllamaHost
    from halo_harness.providers.ollama_request import build_ollama_request_body
    route, profile = _route_profile()
    host = OllamaHost(name="remote", url="http://gpu-box.example:11434")
    tools = [{"name": "Read", "description": "Read a file", "input_schema": {"type": "object", "properties": {}}}]
    turn1_messages = [{"role": "user", "content": [{"type": "text", "text": "hello"}]}]
    kwargs = dict(system_text="You are Halo.", tools=tools, tool_choice=None, route=route, profile=profile,
                  effort="medium", host=host, trained_context=131072, fit_estimate=None,
                  learned_cap=32768, remote=True)
    body1 = build_ollama_request_body(messages=turn1_messages, **kwargs)
    turn2_messages = turn1_messages + [
        {"role": "assistant", "content": [{"type": "text", "text": "ok"}]},
        {"role": "user", "content": [{"type": "text", "text": "again"}]},
    ]
    body2 = build_ollama_request_body(messages=turn2_messages, **kwargs)
    ctx.check("system message stable with learned_cap/remote set", body1["messages"][0] == body2["messages"][0])
    ctx.check(f"options stable (num_ctx from the learned cap both times), got {body1['options']} "
              f"vs {body2['options']}", body1["options"] == body2["options"])
    ctx.check(f"num_ctx is actually the learned cap (32768), got {body1['options'].get('num_ctx')}",
              body1["options"].get("num_ctx") == 32768)


if __name__ == "__main__":
    ctx = Ctx()
    results, passed, failed, skipped = run_all(TESTS, ctx)
    sys.exit(print_results(results, passed, failed, skipped))
