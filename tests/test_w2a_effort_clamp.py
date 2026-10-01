"""tests.test_w2a_effort_clamp -- Halo 2.0.1 W2a (GLM-brief.md item 1):
the Databricks GLM effort clamp map (medium->high, minimal->low, xhigh->max,
none->low, default high -- NOT the gateway's own silent "always max"),
OpenRouter GLM's kept seven-value set, no `tool_choice: "required"` on a
GLM route, the `[0, 1]` temperature clamp, and `effort_sent` on the
stream-json init line. Pinning tests against the real `resolve_profile`/
`clamp_effort`/`map_effort` and the mock Databricks server.
"""
from __future__ import annotations

import io
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


def _scoped(fn):
    import functools

    @functools.wraps(fn)
    def wrapper(ctx):
        old = os.environ.get("BRIDGE_STATE_DIR")
        os.environ["BRIDGE_STATE_DIR"] = str(Path(tempfile.mkdtemp(prefix="w2a-effort-state-")))
        try:
            return fn(ctx)
        finally:
            if old is None:
                os.environ.pop("BRIDGE_STATE_DIR", None)
            else:
                os.environ["BRIDGE_STATE_DIR"] = old
    return test(wrapper)


def _session(fh, mock, *, model: str, effort=None):
    from halo_harness.agent.assemble import SessionContext
    from halo_harness.agent.loop import Session
    from halo_harness.model import ModelProfile, parse_model_ref
    from halo_harness.providers.stream import ProviderCreds
    ctx = SessionContext(cwd=fh["proj"], model_label=model)
    model_ref = parse_model_ref(model)
    return Session(
        cwd=fh["proj"], model_ref=model_ref, model_profile=ModelProfile(),
        creds=ProviderCreds(base_url=mock.root, api_key="t"),
        state_dir=Path(tempfile.mkdtemp(prefix="w2a-effort-sess-")), model_label=model,
        session_context=ctx, max_turns=6, effort=effort,
    )


@_scoped
def test_databricks_glm_effort_set_and_clamp_map(ctx: Ctx):
    from halo_harness.providers.effort import effort_set
    from halo_harness.providers.routing import Route
    for model in ("databricks-glm-5-2", "databricks-glm-5-3", "databricks-glm-5-3-flash",
                  "databricks-glm-5"):  # the last has NO model_table.json row at all
        route = Route(provider="databricks", upstream_model=model, dialect="openai-chat")
        es = effort_set(route)
        ctx.check(f"{model}: allowed is exactly low/high/max, got {es.allowed}",
                  es.allowed == ("low", "high", "max"))
        ctx.check(f"{model}: default is high (not max), got {es.default}", es.default == "high")
        ctx.check(f"{model}: clamp map matches the brief, got {es.clamp_map}", es.clamp_map == {
            "medium": "high", "minimal": "low", "xhigh": "max", "none": "low"})


@_scoped
def test_openrouter_glm_keeps_the_seven_zai_values(ctx: Ctx):
    from halo_harness.providers.effort import effort_set
    from halo_harness.providers.routing import Route
    for model in ("z-ai/glm-5", "z-ai/glm-5.2", "z-ai/glm-5.3", "z-ai/glm-5.3-flash"):
        route = Route(provider="openrouter", upstream_model=model, dialect="openai-chat")
        es = effort_set(route)
        ctx.check(f"{model}: all seven Z.ai values kept, got {es.allowed}",
                  set(es.allowed) == {"max", "high", "low", "medium", "minimal", "none", "xhigh"})
        ctx.check(f"{model}: no Databricks-style clamp map, got {es.clamp_map}", es.clamp_map == {})


@_scoped
def test_databricks_glm_wire_reasoning_effort_per_requested_level(ctx: Ctx):
    """The pinning test the brief itself asks for: "assert the wire
    reasoning_effort for each requested level"."""
    fh = build_fake_home()
    mock = MockDatabricks().start()
    try:
        cases = {
            "low": "low", "high": "high", "max": "max",
            "medium": "high", "minimal": "low", "xhigh": "max", "none": "low",
            # No --effort at all: Databricks GLM's own default for an omitted
            # field is `max` (the "it pauses" report), so the route default
            # `high` is SENT explicitly (ProviderProfile.default_effort_when_
            # unset); every other chat-dialect family keeps "omit -> provider
            # default".
            None: "high",
        }
        for requested, expected_sent in cases.items():
            mock.requests.clear()
            session = _session(fh, mock, model="dbx:databricks-glm-5-3-ok", effort=requested)
            list(session.turn("howdy"))
            body = mock.requests[0]["body"]
            ctx.check(f"--effort {requested!r} -> wire reasoning_effort {expected_sent!r}, got {body.get('reasoning_effort')!r}",
                      body.get("reasoning_effort") == expected_sent)
    finally:
        mock.stop()


@_scoped
def test_no_tool_choice_required_and_temperature_in_range_on_glm(ctx: Ctx):
    fh = build_fake_home()
    mock = MockDatabricks().start()
    try:
        session = _session(fh, mock, model="dbx:databricks-glm-5-3-tool-call-ok")
        list(session.turn("read a file"))
        body = mock.requests[0]["body"]
        ctx.check(f"tool_choice is never 'required' on GLM, got {body.get('tool_choice')!r}",
                  body.get("tool_choice") != "required")
        temp = body.get("temperature")
        ctx.check(f"temperature omitted or within [0, 1], got {temp!r}",
                  temp is None or 0.0 <= temp <= 1.0)
    finally:
        mock.stop()


@_scoped
def test_clamp_temperature_is_a_no_op_outside_glm_and_clamps_within_it(ctx: Ctx):
    from halo_harness.providers.profiles import ProviderProfile, clamp_temperature
    glm_profile = ProviderProfile(family="glm")
    other_profile = ProviderProfile(family="deepseek")
    ctx.check("GLM: 1.0 stays 1.0", clamp_temperature(1.0, glm_profile) == 1.0)
    ctx.check("GLM: an out-of-range 1.5 clamps to 1.0", clamp_temperature(1.5, glm_profile) == 1.0)
    ctx.check("GLM: an out-of-range -0.3 clamps to 0.0", clamp_temperature(-0.3, glm_profile) == 0.0)
    ctx.check("non-GLM: 1.5 passes through unchanged", clamp_temperature(1.5, other_profile) == 1.5)
    ctx.check("None passes through for any family", clamp_temperature(None, glm_profile) is None)


@_scoped
def test_effort_sent_differs_from_requested_in_stream_json_init_line(ctx: Ctx):
    """W2-plan item 8: "effort_sent in the init line"."""
    from halo_harness.output import StreamJsonSink
    import json as json_module
    buf = io.StringIO()
    sink = StreamJsonSink(session_id="s1", cwd="/tmp", model="dbx:databricks-glm-5-3",
                           permission_mode="default", stream=buf, effort="medium", effort_sent="high")
    sink.emit_init()
    obj = json_module.loads(buf.getvalue())
    ctx.check(f"init line has effort='medium', got {obj.get('effort')!r}", obj.get("effort") == "medium")
    ctx.check(f"init line has effort_sent='high', got {obj.get('effort_sent')!r}", obj.get("effort_sent") == "high")


@_scoped
def test_effort_requested_tracked_on_session_and_cmd_effort(ctx: Ctx):
    fh = build_fake_home()
    mock = MockDatabricks().start()
    try:
        session = _session(fh, mock, model="dbx:databricks-glm-5-3-ok", effort="medium")
        ctx.check(f"sent value is 'high', got {session.effort!r}", session.effort == "high")
        ctx.check(f"requested value preserved as 'medium', got {session.effort_requested!r}",
                  session.effort_requested == "medium")
        from halo_harness.commands.builtins import HeadlessFacade, _cmd_effort
        out = _cmd_effort("", HeadlessFacade(cwd=fh["proj"], session=session))
        ctx.check(f"bare /effort shows 'sent as high', got {out!r}", "sent as high" in out)
    finally:
        mock.stop()


if __name__ == "__main__":
    ctx = Ctx()
    results, passed, failed, skipped = run_all(TESTS, ctx)
    sys.exit(print_results(results, passed, failed, skipped))
