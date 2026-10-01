"""tests.test_w2a_finish_reasons_and_phase -- Halo 2.0.1 W2a (GLM-brief.md
items 3/6, HALO-2.0.1-liveness-tips-brief.md Part A6): chat-dialect
finish-reason mapping (model_context_window_exceeded -> overflow path,
sensitive -> terminal error, length-with-no-output stays a provider
failure), the steer-restart pilot (a steer during a silent pre-first-token
wait aborts and resends), the `phase` event contract in order, and the new
per-call telemetry fields landing in the session log and `stats --models`.
"""
from __future__ import annotations

import functools
import json
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.helpers.runner import Ctx, new_registry, print_results, run_all
from tests.helpers.fake_home import build_fake_home
from tests.helpers.mock_databricks import MockDatabricks, STEER_RESTART_MARKER
from tests.helpers.provider_env_defaults import ensure_default_provider_credentials

ensure_default_provider_credentials()
test, TESTS = new_registry()


def _scoped(fn):
    @functools.wraps(fn)
    def wrapper(ctx):
        old = os.environ.get("BRIDGE_STATE_DIR")
        os.environ["BRIDGE_STATE_DIR"] = str(Path(tempfile.mkdtemp(prefix="w2a-phase-state-")))
        try:
            return fn(ctx)
        finally:
            if old is None:
                os.environ.pop("BRIDGE_STATE_DIR", None)
            else:
                os.environ["BRIDGE_STATE_DIR"] = old
    return test(wrapper)


def _session(fh, mock, *, model: str):
    from halo_harness.agent.assemble import SessionContext
    from halo_harness.agent.loop import Session
    from halo_harness.model import ModelProfile, parse_model_ref
    from halo_harness.providers.stream import ProviderCreds
    ctx = SessionContext(cwd=fh["proj"], model_label=model)
    model_ref = parse_model_ref(model)
    return Session(
        cwd=fh["proj"], model_ref=model_ref, model_profile=ModelProfile(),
        creds=ProviderCreds(base_url=mock.root, api_key="t"),
        state_dir=Path(tempfile.mkdtemp(prefix="w2a-phase-sess-")), model_label=model,
        session_context=ctx, max_turns=6,
    )


@_scoped
def test_model_context_window_exceeded_feeds_overflow_then_terminal_error(ctx: Ctx):
    fh = build_fake_home()
    mock = MockDatabricks().start()
    try:
        session = _session(fh, mock, model="dbx:databricks-glm-5-3-finish-model-context-window-exceeded")
        events_seen = list(session.turn("howdy"))
        errors = [e for e in events_seen if e.kind == "error"]
        ctx.check(f"exactly one terminal error, got {[e.data for e in errors]}", len(errors) == 1)
        ctx.check(f"category is CONTEXT_WINDOW_EXCEEDED, got {errors[0].data.get('category')}",
                  errors[0].data.get("category") == "CONTEXT_WINDOW_EXCEEDED")
        ctx.check(f"compaction was attempted (more than one upstream call), got {len(mock.requests)}",
                  len(mock.requests) > 1)
    finally:
        mock.stop()


@_scoped
def test_sensitive_finish_becomes_a_clear_never_retried_error(ctx: Ctx):
    fh = build_fake_home()
    mock = MockDatabricks().start()
    try:
        session = _session(fh, mock, model="dbx:databricks-glm-5-3-finish-sensitive")
        events_seen = list(session.turn("howdy"))
        errors = [e for e in events_seen if e.kind == "error"]
        ctx.check(f"exactly one error, got {[e.data for e in errors]}", len(errors) == 1)
        ctx.check(f"err_type is 'sensitive', got {errors[0].data.get('err_type')}",
                  errors[0].data.get("err_type") == "sensitive")
        ctx.check(f"message carries the provider's own text, got {errors[0].data.get('message')!r}",
                  "can't help" in errors[0].data.get("message", ""))
        ctx.check(f"never retried, got {len(mock.requests)} request(s)", len(mock.requests) == 1)
    finally:
        mock.stop()


@_scoped
def test_length_with_minimal_output_still_a_provider_failure(ctx: Ctx):
    """Pre-existing behavior (oai_stream.py's `length_with_minimal_output`)
    -- pinned here alongside the two NEW finish reasons so all three are
    verified together, per the brief's own "finish reason mapping for all
    three"."""
    fh = build_fake_home()
    mock = MockDatabricks().start()
    try:
        session = _session(fh, mock, model="dbx:databricks-glm-5-3-finish-length-minimal")
        events_seen = list(session.turn("howdy"))
        errors = [e for e in events_seen if e.kind == "error"]
        ctx.check(f"a provider_failure error, got {[e.data for e in errors]}",
                  errors and errors[0].data.get("err_type") == "provider_failure")
    finally:
        mock.stop()


@_scoped
def test_steer_restart_aborts_silent_call_and_resends_with_steer(ctx: Ctx):
    """The brief's own verification: "steer at 1s into a silent call ->
    second request body carries the steer, first was aborted"."""
    fh = build_fake_home()
    mock = MockDatabricks().start()
    try:
        session = _session(fh, mock, model="dbx:databricks-glm-5-3-delay-then-ok")
        events_seen = []

        def _drive():
            events_seen.extend(session.turn("howdy"))

        t = threading.Thread(target=_drive, daemon=True)
        t.start()
        time.sleep(0.35)  # well into STEER_RESTART_DELAY_S's 1.0s silent window
        queued = session.steer(STEER_RESTART_MARKER)
        ctx.check("steer was accepted (a turn is running)", queued)
        t.join(timeout=10)
        ctx.check("the turn thread finished", not t.is_alive())
        ctx.check(f"exactly 2 upstream requests (first aborted, second succeeded), got {len(mock.requests)}",
                  len(mock.requests) == 2)
        second_messages = json.dumps(mock.requests[1]["body"].get("messages"))
        ctx.check("the second request's body carries the steer text", STEER_RESTART_MARKER in second_messages)
        restarts = [e for e in events_seen if e.kind == "steer_restart"]
        ctx.check(f"a steer_restart event fired, got kinds={[e.kind for e in events_seen]}", restarts)
        ctx.check("no error surfaced", not [e for e in events_seen if e.kind == "error"])
    finally:
        mock.stop()


@_scoped
def test_steer_restart_disabled_via_config_never_fires_the_event(ctx: Ctx):
    """With `steer.restart_when_silent` off, a steer queued during the
    silent window must never produce a `steer_restart` event -- it falls
    back entirely to the pre-existing "cut at next chunk" mechanism
    instead (which still eventually applies the steer once the upstream's
    first real content arrives, via `_apply_pending_steers_events`'s own
    established path -- unchanged, not re-pinned here)."""
    from halo_harness import theme as theme_mod
    fh = build_fake_home()
    theme_mod.set_config_value("steer.restart_when_silent", False)
    mock = MockDatabricks().start()
    try:
        session = _session(fh, mock, model="dbx:databricks-glm-5-3-delay-then-ok")
        events_seen = []

        def _drive():
            events_seen.extend(session.turn("howdy"))

        t = threading.Thread(target=_drive, daemon=True)
        t.start()
        time.sleep(0.35)
        session.steer(STEER_RESTART_MARKER)
        t.join(timeout=10)
        ctx.check("the turn thread finished", not t.is_alive())
        ctx.check(f"no steer_restart event with the feature off, got kinds={[e.kind for e in events_seen]}",
                  not [e for e in events_seen if e.kind == "steer_restart"])
        ctx.check(f"the steer still reaches the model eventually (2 requests), got {len(mock.requests)}",
                  len(mock.requests) == 2)
    finally:
        mock.stop()
        theme_mod.set_config_value("steer.restart_when_silent", True)


@_scoped
def test_phase_events_fire_in_order_request_sent_headers_first_token(ctx: Ctx):
    fh = build_fake_home()
    mock = MockDatabricks().start()
    try:
        session = _session(fh, mock, model="dbx:databricks-glm-5-3-phase-sequence")
        events_seen = list(session.turn("howdy"))
        phases = [e for e in events_seen if e.kind == "phase"]
        states = [p.data.get("state") for p in phases]
        ctx.check(f"request_sent, headers, first_token in order, got {states}",
                  states == ["request_sent", "headers", "first_token"])
        ctx.check(f"first_token names kind=reasoning (it streamed first on the wire), got {phases[2].data}",
                  phases[2].data.get("kind") == "reasoning")
        ctx.check(f"headers carries ttfb_ms, got {phases[1].data}", isinstance(phases[1].data.get("ttfb_ms"), float))
    finally:
        mock.stop()


@_scoped
def test_waiting_for_model_phase_between_tool_dispatch_and_next_call(ctx: Ctx):
    fh = build_fake_home()
    mock = MockDatabricks().start()
    try:
        session = _session(fh, mock, model="dbx:databricks-glm-5-3-tool-call-ok")
        events_seen = list(session.turn("read a file"))
        states = [e.data.get("state") for e in events_seen if e.kind == "phase"]
        ctx.check(f"waiting_for_model appears after the tool call, got {states}", "waiting_for_model" in states)
    finally:
        mock.stop()


@_scoped
def test_telemetry_fields_in_session_log_and_stats_models(ctx: Ctx):
    fh = build_fake_home()
    mock = MockDatabricks().start()
    try:
        session = _session(fh, mock, model="dbx:databricks-glm-5-3-reasoning-content-shape")
        list(session.turn("howdy"))
        usage_nodes = [n for n in session.log.nodes() if n.get("type") == "usage"]
        u = usage_nodes[-1]
        for key in ("ttfb_ms", "first_reasoning_ms", "first_text_ms", "reasoning_streamed"):
            ctx.check(f"usage node has {key!r}, got {u}", key in u)
        ctx.check(f"reasoning_streamed True (the mock sends 2 reasoning_content chunks), got {u['reasoning_streamed']}",
                  u["reasoning_streamed"] is True)

        from halo_harness import telemetry
        summary = telemetry._summarize_nodes(
            session_id="s1", slug="proj", path="p", mtime=0.0, size=0,
            nodes=session.log.nodes(), corrupt_lines=0)
        rows = telemetry.aggregate_by_model([summary])
        ctx.check(f"exactly one model row, got {len(rows)}", len(rows) == 1)
        row = rows[0]
        for key in ("ttft_p50_ms", "ttft_p95_ms", "waits_over_20s", "reasoning_calls", "reasoning_streamed_pct"):
            ctx.check(f"aggregated row has {key!r}, got {row}", key in row)
        ctx.check(f"reasoning_streamed_pct is 100 (the only call streamed), got {row['reasoning_streamed_pct']}",
                  row["reasoning_streamed_pct"] == 100.0)

        from halo_harness import stats_cli
        ctx.check("the four wide-only columns are present in _MODEL_COLUMNS",
                  {"ttft p50", "ttft p95", "waits>20s", "reasoning%"}.issubset(
                      {h for h, _ in stats_cli._MODEL_COLUMNS}))
        for _, fmt in stats_cli._MODEL_COLUMNS:
            fmt(row)  # must not KeyError on a REAL aggregated row
    finally:
        mock.stop()


if __name__ == "__main__":
    ctx = Ctx()
    results, passed, failed, skipped = run_all(TESTS, ctx)
    sys.exit(print_results(results, passed, failed, skipped))
