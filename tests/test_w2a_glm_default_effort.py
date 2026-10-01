"""tests.test_w2a_glm_default_effort -- Halo 2.0.1 (GLM-brief.md item 1,
"default high"): on Databricks GLM an OMITTED `reasoning_effort` means
`max` (the "it pauses" report itself), so the route default is SENT when
nothing is configured, a Claude-oriented settings `effortLevel` the route
does not accept lands on that default too, an explicit `--effort xhigh`
still goes through the clamp map to `max`, switching into a GLM route
mid-session applies the default, and every other chat-dialect family keeps
"omit -> provider default". Pinning tests against the real `Session` and the
mock Databricks server.
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


def _scoped(fn):
    import functools

    @functools.wraps(fn)
    def wrapper(ctx):
        old = os.environ.get("BRIDGE_STATE_DIR")
        os.environ["BRIDGE_STATE_DIR"] = str(Path(tempfile.mkdtemp(prefix="w2a-glm-default-state-")))
        try:
            return fn(ctx)
        finally:
            if old is None:
                os.environ.pop("BRIDGE_STATE_DIR", None)
            else:
                os.environ["BRIDGE_STATE_DIR"] = old
    return test(wrapper)


def _session(fh, mock, *, model: str, effort=None, effort_source=None):
    from halo_harness.agent.assemble import SessionContext
    from halo_harness.agent.loop import Session
    from halo_harness.model import ModelProfile, parse_model_ref
    from halo_harness.providers.stream import ProviderCreds
    ctx = SessionContext(cwd=fh["proj"], model_label=model)
    model_ref = parse_model_ref(model)
    return Session(
        cwd=fh["proj"], model_ref=model_ref, model_profile=ModelProfile(),
        creds=ProviderCreds(base_url=mock.root, api_key="t"),
        state_dir=Path(tempfile.mkdtemp(prefix="w2a-glm-default-sess-")), model_label=model,
        session_context=ctx, max_turns=6, effort=effort, effort_source=effort_source,
    )


def _wire_effort(mock) -> object:
    return mock.requests[0]["body"].get("reasoning_effort")


@_scoped
def test_glm_sends_the_route_default_when_nothing_is_configured(ctx: Ctx):
    fh = build_fake_home()
    mock = MockDatabricks().start()
    try:
        session = _session(fh, mock, model="dbx:databricks-glm-5-3-ok")
        ctx.check(f"session effort is the route default, got {session.effort!r}", session.effort == "high")
        ctx.check(f"effort_source says default, got {session.effort_source!r}", session.effort_source == "default")
        list(session.turn("howdy"))
        ctx.check(f"wire reasoning_effort is high (never omitted -> max), got {_wire_effort(mock)!r}",
                  _wire_effort(mock) == "high")
    finally:
        mock.stop()


@_scoped
def test_settings_xhigh_lands_on_the_route_default_not_on_max(ctx: Ctx):
    """Claude Code's `effortLevel: xhigh` is written for Claude models; on
    Databricks GLM it is not an accepted value, so it lands on the route
    default (`high`), not on the clamp map's `max` -- and the requested
    value stays visible for `/status`."""
    fh = build_fake_home()
    mock = MockDatabricks().start()
    try:
        session = _session(fh, mock, model="dbx:databricks-glm-5-3-ok", effort="xhigh", effort_source="settings")
        ctx.check(f"settings xhigh -> high, got {session.effort!r}", session.effort == "high")
        ctx.check(f"effort_source becomes default, got {session.effort_source!r}", session.effort_source == "default")
        ctx.check(f"requested value kept for display, got {session.effort_requested!r}",
                  session.effort_requested == "xhigh")
        list(session.turn("howdy"))
        ctx.check(f"wire reasoning_effort is high, got {_wire_effort(mock)!r}", _wire_effort(mock) == "high")
    finally:
        mock.stop()


@_scoped
def test_explicit_xhigh_is_still_a_deliberate_max(ctx: Ctx):
    fh = build_fake_home()
    mock = MockDatabricks().start()
    try:
        for source in ("flag", "session"):
            mock.requests.clear()
            session = _session(fh, mock, model="dbx:databricks-glm-5-3-ok", effort="xhigh", effort_source=source)
            ctx.check(f"{source}: explicit xhigh clamps to max, got {session.effort!r}", session.effort == "max")
            list(session.turn("howdy"))
            ctx.check(f"{source}: wire reasoning_effort is max, got {_wire_effort(mock)!r}", _wire_effort(mock) == "max")
    finally:
        mock.stop()


@_scoped
def test_switching_into_glm_with_no_effort_applies_the_default(ctx: Ctx):
    from halo_harness.model import ModelProfile, parse_model_ref
    fh = build_fake_home()
    mock = MockDatabricks().start()
    try:
        session = _session(fh, mock, model="dbx:databricks-kimi-k3-ok")
        ctx.check(f"kimi keeps omit -> provider default, got {session.effort!r}", session.effort is None)
        session.set_model(parse_model_ref("dbx:databricks-glm-5-3-ok"), ModelProfile())
        ctx.check(f"after the switch the GLM default is set, got {session.effort!r}", session.effort == "high")
        ctx.check(f"effort_source says default, got {session.effort_source!r}", session.effort_source == "default")
        list(session.turn("howdy"))
        ctx.check(f"wire reasoning_effort is high after the switch, got {_wire_effort(mock)!r}",
                  _wire_effort(mock) == "high")
    finally:
        mock.stop()


@_scoped
def test_other_chat_families_still_omit_the_field(ctx: Ctx):
    fh = build_fake_home()
    mock = MockDatabricks().start()
    try:
        session = _session(fh, mock, model="dbx:databricks-kimi-k3-ok")
        list(session.turn("howdy"))
        ctx.check(f"kimi with nothing configured omits reasoning_effort, got {_wire_effort(mock)!r}",
                  _wire_effort(mock) is None)
    finally:
        mock.stop()


if __name__ == "__main__":
    ctx = Ctx()
    results, passed, failed, skipped = run_all(TESTS, ctx)
    sys.exit(print_results(results, passed, failed, skipped))
