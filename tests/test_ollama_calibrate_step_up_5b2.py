"""tests.test_ollama_calibrate_step_up_5b2 -- Halo 2.0.3 round 5b part 2
(brief item 7, part 1 follow-ups): `run_calibration`'s own step-UP phase
(bounded by trained context and the hard cap, `--no-up` skips it) against
scripted `/api/ps` sequences, and the auto-calibrate notice actually
draining from the two secondary call sites (`call_small_model`,
`_run_compaction`) that part 1 left unflushed.
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.helpers.runner import Ctx, new_registry, print_results, run_all
from tests.helpers.mock_ollama import MockUpstream, SCENARIOS

test, TESTS = new_registry()
_GB = 1024 ** 3


class _StepScenario:
    """Same shape as tests/test_ollama_calibrate.py's own helper -- each
    `/api/chat` call advances `mock.ps_response` to the next scripted
    `/api/ps` snapshot before answering trivially."""

    def __init__(self, mock: MockUpstream, ps_steps: list):
        self.mock = mock
        self.ps_steps = ps_steps
        self.calls = 0

    def __call__(self, handler, body) -> None:
        idx = min(self.calls, len(self.ps_steps) - 1)
        entry = self.ps_steps[idx]
        self.mock.ps_response = {"models": [entry]} if entry else {"models": []}
        self.calls += 1
        SCENARIOS["done-reason-stop"](handler, body)


# ---- step-up phase -------------------------------------------------------

@test
def test_steps_up_from_a_fitting_first_guess(ctx: Ctx):
    from halo_harness.providers.ollama import OllamaHost
    from halo_harness.providers.ollama_calibrate import run_calibration
    mock = MockUpstream().start()
    model = "qwen3-coder:30b"
    try:
        # Fits at 8192 (the starting candidate) AND at 16384 AND at 32768;
        # NOT fully resident at 65536 -- the true ceiling is 32768.
        mock.scenarios[model] = _StepScenario(mock, [
            {"model": model, "size": 10 * _GB, "size_vram": 10 * _GB},   # 8192: fits
            {"model": model, "size": 10 * _GB, "size_vram": 10 * _GB},   # 16384: fits
            {"model": model, "size": 10 * _GB, "size_vram": 10 * _GB},   # 32768: fits
            {"model": model, "size": 20 * _GB, "size_vram": 5 * _GB},    # 65536: does NOT fit
        ])
        host = OllamaHost(name="mock", url=mock.base_url)
        result = run_calibration(host, model, start_ctx=8192, step_up=True, hard_cap=131072)
        ctx.check(f"fits, got {result.outcome!r}", result.outcome == "fits")
        ctx.check(f"stepped UP to the true ceiling 32768, got {result.max_full_gpu_ctx}",
                  result.max_full_gpu_ctx == 32768)
        ctx.check(f"4 steps (8192, 16384, 32768, 65536-fails), got {result.steps}", result.steps == 4)
    finally:
        mock.stop()


@test
def test_no_up_skips_the_step_up_phase(ctx: Ctx):
    from halo_harness.providers.ollama import OllamaHost
    from halo_harness.providers.ollama_calibrate import run_calibration
    mock = MockUpstream().start()
    model = "qwen3-coder:30b"
    try:
        mock.scenarios[model] = _StepScenario(mock, [{"model": model, "size": 10 * _GB, "size_vram": 10 * _GB}])
        host = OllamaHost(name="mock", url=mock.base_url)
        result = run_calibration(host, model, start_ctx=8192, step_up=False, hard_cap=131072)
        ctx.check(f"stays at the first fitting guess, got {result.max_full_gpu_ctx}",
                  result.max_full_gpu_ctx == 8192)
        ctx.check(f"exactly one step (never tries 16384), got {result.steps}", result.steps == 1)
    finally:
        mock.stop()


@test
def test_step_up_bounded_by_trained_context(ctx: Ctx):
    from halo_harness.providers.ollama import OllamaHost
    from halo_harness.providers.ollama_calibrate import run_calibration
    mock = MockUpstream().start()
    model = "qwen3-coder:30b"
    try:
        # Would ALSO fit at 16384 if tried, but trained_context=8192 must
        # stop the up-phase from ever trying past the model's own window.
        mock.scenarios[model] = _StepScenario(mock, [{"model": model, "size": 10 * _GB, "size_vram": 10 * _GB}])
        host = OllamaHost(name="mock", url=mock.base_url)
        result = run_calibration(host, model, start_ctx=8192, step_up=True, trained_context=8192, hard_cap=131072)
        ctx.check(f"never probes past the trained context, got {result.max_full_gpu_ctx}",
                  result.max_full_gpu_ctx == 8192)
        ctx.check(f"exactly one step, got {result.steps}", result.steps == 1)
    finally:
        mock.stop()


@test
def test_step_up_bounded_by_hard_cap(ctx: Ctx):
    from halo_harness.providers.ollama import OllamaHost
    from halo_harness.providers.ollama_calibrate import run_calibration
    mock = MockUpstream().start()
    model = "qwen3-coder:30b"
    try:
        # Every candidate "fits" (tiny weights, huge VRAM) -- without a
        # hard cap this would double forever; hard_cap=16384 must stop it.
        mock.scenarios[model] = _StepScenario(mock, [{"model": model, "size": 1 * _GB, "size_vram": 100 * _GB}])
        host = OllamaHost(name="mock", url=mock.base_url)
        result = run_calibration(host, model, start_ctx=8192, step_up=True, hard_cap=16384)
        ctx.check(f"stops exactly at the hard cap, got {result.max_full_gpu_ctx}", result.max_full_gpu_ctx == 16384)
        ctx.check(f"two steps (8192, 16384 -- 32768 would exceed the cap, never tried), got {result.steps}",
                  result.steps == 2)
    finally:
        mock.stop()


@test
def test_auto_calibration_passes_trained_context_through(ctx: Ctx):
    """`run_auto_calibration` (the automatic trigger) now also steps up,
    bounded by this model's OWN trained context from the catalog --
    `qwen3:30b` is mock_ollama's own default fixture, trained context
    40960."""
    from halo_harness.providers.ollama import OllamaHost
    from halo_harness.providers.ollama_calibrate import lookup_learned_cap, run_auto_calibration
    d = Path(tempfile.mkdtemp(prefix="ol-autocal-stepup-"))
    mock = MockUpstream().start()
    model = "qwen3:30b"
    try:
        mock.scenarios[model] = _StepScenario(mock, [{"model": model, "size": 1 * _GB, "size_vram": 1 * _GB}])
        host = OllamaHost(name="mock", url=mock.base_url)
        os.environ["BRIDGE_STATE_DIR"] = str(d)
        notice = run_auto_calibration(host, model, state_dir=d)
        ctx.check(f"a plain notice, got {notice!r}", isinstance(notice, str))
        got_cap = lookup_learned_cap(d, host_url=host.url, model=model)
        # Tiny weights (1 GiB) + huge headroom means it steps up past the
        # 32768 starting guess but must still stop AT or before the
        # catalog's trained context (40960), never past it.
        ctx.check(f"stepped up, but never past the trained context (40960), got {got_cap}",
                  isinstance(got_cap, int) and 32768 <= got_cap <= 40960)
    finally:
        mock.stop()
        os.environ.pop("BRIDGE_STATE_DIR", None)


# ---- notice flush from the two secondary call sites ----------------------

def _minimal_session(state_dir, mock, *, model="plain-text"):
    from halo_harness.agent.assemble import SessionContext
    from halo_harness.agent.loop import Session
    from halo_harness.model import ModelProfile, parse_model_ref
    from halo_harness.permissions import PermissionEngine
    from halo_harness.providers.stream import ProviderCreds
    cwd = Path(tempfile.mkdtemp(prefix="ol-notice-flush-cwd-"))
    session_ctx = SessionContext(cwd=cwd, model_label=f"ol:{model}")
    return Session(
        cwd=cwd, model_ref=parse_model_ref(f"ol:{model}"),
        model_profile=ModelProfile(context_tokens=10_000, max_output_tokens=1_000),
        creds=ProviderCreds(base_url=mock.base_url, api_key=""),
        state_dir=Path(state_dir), model_label=f"ol:{model}",
        session_context=session_ctx, max_turns=10,
        permission_engine=PermissionEngine(mode="auto", cwd=cwd),
    )


@test
def test_drain_pending_ollama_notices_pops_and_clears(ctx: Ctx):
    mock = MockUpstream().start()
    d = Path(tempfile.mkdtemp(prefix="ol-drain-"))
    try:
        session = _minimal_session(d, mock)
        session._pending_ollama_notices.append("notice A")
        session._pending_ollama_notices.append("notice B")
        drained = session._drain_pending_ollama_notices()
        ctx.check(f"both notices drained in order, got {drained!r}", drained == ["notice A", "notice B"])
        ctx.check("the queue is cleared", session._pending_ollama_notices == [])
        ctx.check("a second drain is empty (never shown twice)", session._drain_pending_ollama_notices() == [])
    finally:
        mock.stop()


@test
def test_call_small_model_drains_notices_via_log_not_return_value(ctx: Ctx):
    """`call_small_model` has no event stream to yield a `notification`
    through, and several of its callers (title text, /improve drafts, a
    hook's own decision text) must never have their return value polluted
    with an extra line -- the notice is drained (so it never sits queued
    forever) but surfaces via the logger, not the returned string."""
    mock = MockUpstream().start()
    d = Path(tempfile.mkdtemp(prefix="ol-smallmodel-notice-"))
    try:
        session = _minimal_session(d, mock)
        session._pending_ollama_notices.append("a calibration happened")
        reply = session.call_small_model(system_text="sys", user_text="say hi", model_ref=session.model_ref)
        ctx.check(f"the reply text is clean, got {reply!r}", "calibration" not in reply)
        ctx.check("the notice queue was drained by this call", session._pending_ollama_notices == [])
    finally:
        mock.stop()


@test
def test_run_compaction_yields_notification_for_a_pending_notice(ctx: Ctx):
    """`_run_compaction` IS a real event-yielding generator (unlike
    `call_small_model`) -- a notice queued before/during its own summary
    call must come out as an ordinary `notification` event, the same way
    `_step`'s own main-turn flush already does."""
    from tests.helpers.fake_home import build_fake_home
    mock = MockUpstream().start()
    fh = build_fake_home()
    try:
        session = _minimal_session(Path(tempfile.mkdtemp(prefix="ol-compact-notice-")), mock)
        filler = "x" * 400
        for i in range(60):
            session.log.append_user([{"type": "text", "text": f"question number {i} -- {filler}"}])
            session.log.append_assistant(content=[{"type": "text", "text": f"answer number {i}. {filler}"}],
                                          stop_reason="end_turn")
        session._pending_ollama_notices.append("calibrated during compaction")
        events_seen = list(session._run_compaction(1, trigger="manual"))
        notif = [e for e in events_seen if e.kind == "notification"]
        ctx.check(f"a notification event was yielded, got {[e.data for e in notif]!r}",
                  any(e.data.get("text") == "calibrated during compaction" for e in notif))
        ctx.check("the queue was drained", session._pending_ollama_notices == [])
    finally:
        mock.stop()


if __name__ == "__main__":
    ctx = Ctx()
    results, passed, failed, skipped = run_all(TESTS, ctx)
    sys.exit(print_results(results, passed, failed, skipped))
