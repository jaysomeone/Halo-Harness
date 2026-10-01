"""tests.test_fuzz_h9 -- H9 Part C bug hunt: the fuzz harness.

Drives a REAL halo_harness.agent.loop.Session against deliberately
adversarial mock upstreams (malformed/truncated SSE, huge outputs, unicode
edge cases, control characters, 429/5xx storms, context overflow) with a
steer or an abort injected at a randomly chosen point in every run, then
checks the session-log invariants from H9-brief.md Part C and the two
"Must-dos carried over" sections' fuzz paragraphs (review-findings-
h4-h5-h3c.md and review-findings-h5b.md):

  1. every tool_use has exactly one tool_result before the next assistant
     turn (no unpaired calls, no duplicates) -- agent/invariants.
     find_unpaired_tool_use_ids, walked over the WHOLE log.
  2. no assistant message is empty after the real request-building path
     (providers/request.prepare_anthropic_messages) would send it.
  3. in the user message following a tool_use-bearing assistant message,
     any tool_result blocks come before any plain-text blocks.
  4. no hang -- every run has a hard wall-clock timeout enforced from
     OUTSIDE the run (a background-thread join(timeout=), never just
     trusting the mock to respond).
  5. no leaked threads/subprocesses -- threading.enumerate() (and,
     best-effort, child process count via psutil when installed) is
     snapshotted before/after every run.
  6. the run always ends with a valid, parseable session log.

The actual engines (mock upstreams, plan generation, invariant checks,
thread/process-leak bookkeeping) live in tests/helpers/fuzz_h9.py
(run_one -- OpenAI-dialect, malformed chunks/truncated tool JSON/huge
output/unicode/control chars/429-5xx storms/overflow, steer-or-abort at a
sync event boundary or via a background timer landing inside a hook/
retry-wait) and tests/helpers/fuzz_h9_extra.py (three more engines: a
steer/abort during auto-compaction, one while a permission decision is
pending, and one over the NATIVE Anthropic SSE dialect via
mock_anthropic). This file only wires them into the run_all.py-discovered
`test_*` pattern and asserts pass/fail.

Running the full >= 200-run sweep on demand (NOT the default -- see
below): either env var overrides the default small run --

    RC_FUZZ_N=200 python tests/test_fuzz_h9.py
    RC_FUZZ_N=200 RC_FUZZ_SEED=1000 python tests/test_fuzz_h9.py   # a second, disjoint range
    python -m tests.helpers.fuzz_h9_extra 200 0                    # the engine directly, verbose per-seed output

`RC_FUZZ_N` (default 20) is the run count; `RC_FUZZ_SEED` (default a fixed
constant, never time-based) is the base seed -- seeds base_seed..
base_seed+n-1 are used, so the DEFAULT run is deterministic (never flaky
in CI) while a bigger on-demand sweep is fully reproducible from the two
numbers alone. On ANY invariant failure the seed, engine and full error
are printed AND raised, so a failure is reproducible by feeding that
exact seed back into the named engine's own `run_one*` function.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.helpers.runner import Ctx, new_registry, print_results, run_all
from tests.helpers.fuzz_h9 import run_one
from tests.helpers.mock_openai import MockUpstream
from tests.helpers.fuzz_h9_extra import run_sweep_all
from tests.helpers.provider_env_defaults import ensure_default_provider_credentials

ensure_default_provider_credentials()

test, TESTS = new_registry()

_DEFAULT_SEED = 20260925  # fixed -- never time-based, so the default run is never flaky in CI
_DEFAULT_N = 20
# H9-brief.md Part C asks for "e.g. 10s"; measured directly (see the H9
# fuzz-bug-hunt report), a LEGITIMATE 3-in-a-row un-headered 5xx/
# connection-error streak (the plan generator's own worst case -- capped
# at 3 consecutive failure-ish acts) drives the real 1-2-4-8s exponential
# backoff ladder to ~17s before the plan's own guaranteed clean reply
# finally lands -- confirmed empirically: base_seed=20260925's own 200-run
# sweep hit exactly this (seed=20261072, plan starting
# ['http_5xx_503','http_5xx_500','malformed_chunk',...]) and falsely
# "timed out" at a 15.0s ceiling. 35s keeps a comfortable margin above
# that measured worst case while still being a hard, finite ceiling that
# catches a genuine hang.
_FUZZ_TIMEOUT_S = 35.0


def _fuzz_n() -> int:
    try:
        return max(1, int(os.environ.get("RC_FUZZ_N", str(_DEFAULT_N))))
    except (TypeError, ValueError):
        return _DEFAULT_N


def _fuzz_seed() -> int:
    try:
        return int(os.environ.get("RC_FUZZ_SEED", str(_DEFAULT_SEED)))
    except (TypeError, ValueError):
        return _DEFAULT_SEED


@test
def test_fuzz_sweep(ctx: Ctx):
    """The main sweep: RC_FUZZ_N runs (default 20, override for the full
    >= 200-run sweep) spread across all four engines -- general OpenAI-
    dialect (malformed/truncated/huge/unicode/control-chars/429-5xx/
    overflow with steer-or-abort at a sync boundary or a background
    timer), compaction, permission-pending, and native-Anthropic-dialect.
    Any failure names its seed, its engine and the exact error so it can
    be reproduced directly (see this module's own docstring)."""
    n, seed0 = _fuzz_n(), _fuzz_seed()
    summary = run_sweep_all(n, base_seed=seed0, timeout_s=_FUZZ_TIMEOUT_S, verbose=False)
    ctx.checks += summary["n"]  # each run is its own pass/fail unit, counted individually
    if summary["failed"]:
        lines = [f"seed={s} engine={e}: {err}" for s, e, err in summary["failures"]]
        raise AssertionError(
            f"{summary['failed']}/{summary['n']} fuzz runs failed (base_seed={seed0}, n={n}, "
            f"{summary['elapsed']:.1f}s total) -- reproduce with the named engine + seed:\n" + "\n".join(lines)
        )


@test
def test_pin_finish_ascii_safe_done_marker_has_correct_chunk_length(ctx: Ctx):
    """Pins the H9 fuzz-harness bug fixed in tests/helpers/fuzz_h9.py's
    _finish_ascii_safe: the `[DONE]` marker's HTTP chunk-size prefix used
    to be hardcoded as the literal byte "6" (the length of "data: " alone)
    instead of the real 16-byte frame's length (hex 10), corrupting the
    chunked-transfer framing for every "text_unicode" fuzz act. Before the
    fix this drove a REAL Session into upstream's own MAX_RETRIES=5 ladder
    (no Retry-After header on a raw connection error -> the 2/4/8/16/30s
    exponential backoff) -- reproduced directly: session.turn() took
    60-90s and ended in a terminal `error` event instead of ever seeing
    the clean unicode text. First checks the byte-level framing directly
    (no network needed), then drives a real Session end to end and
    confirms it now resolves in ONE request, quickly, with no error."""
    frame = b"data: [DONE]\r\n\r\n"
    ctx.check(f"the real [DONE] frame is 16 bytes (hex 10), got {len(frame)}", len(frame) == 16)
    ctx.check(f"hex-formatted chunk-size prefix is '10', got {'%x' % len(frame)!r}", ("%x" % len(frame)) == "10")

    import json
    import tempfile
    import time as _time

    from tests.helpers.fake_home import build_fake_home
    from tests.helpers.fuzz_h9 import _finish_ascii_safe, _role_chunk, _stop_chunk, _text_chunk
    from tests.helpers.mock_openai import SCENARIOS
    from halo_harness.agent.assemble import SessionContext
    from halo_harness.agent.loop import Session
    from halo_harness.model import ModelProfile, parse_model_ref
    from halo_harness.providers.stream import ProviderCreds

    lone_surrogate_snippet = "lone surrogate ahead: \ud800 (end)"
    scenario_name = "pin-finish-ascii-safe"

    def _scn(h, body):
        _finish_ascii_safe(h, [_role_chunk(), _text_chunk(lone_surrogate_snippet), _stop_chunk()])

    SCENARIOS[scenario_name] = _scn
    fh = build_fake_home()
    mock = MockUpstream().start()
    try:
        model = f"or:mock/{scenario_name}"
        os.environ["BRIDGE_TEST_HOME"] = str(fh["home"])
        session_ctx = SessionContext(cwd=fh["proj"], model_label=model)
        session = Session(
            cwd=fh["proj"], model_ref=parse_model_ref(model), model_profile=ModelProfile(),
            creds=ProviderCreds(base_url=mock.base_url, api_key="k"),
            state_dir=Path(tempfile.mkdtemp(prefix="pin-ascii-safe-")), model_label=model,
            session_context=session_ctx, openrouter_base_url=mock.base_url, max_turns=5,
        )
        t0 = _time.monotonic()
        kinds, texts = [], []
        for ev in session.turn("say the unicode snippet"):
            kinds.append(ev.kind)
            if ev.kind == "text_delta":
                texts.append(ev.data.get("text"))
        elapsed = _time.monotonic() - t0

        ctx.check(f"resolves in well under the old ~60-90s retry-storm time, got {elapsed:.2f}s", elapsed < 10.0)
        ctx.check(f"exactly one upstream request (no retry storm), got {len(mock.requests)}", len(mock.requests) == 1)
        ctx.check(f"no error event, got kinds={kinds}", "error" not in kinds)
        ctx.check(f"the lone-surrogate text streamed through, got {texts}",
                   any(lone_surrogate_snippet == t for t in texts))
        # The FINAL logged content must be repaired (agent/invariants.
        # repair_truncated_text) into something JSON/UTF-8-safe -- proven
        # by the fact the log file itself parses back as valid JSONL.
        if session.log.path.exists():
            for line in session.log.path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    json.loads(line)
    finally:
        mock.stop()
        SCENARIOS.pop(scenario_name, None)


@test
def test_seed_reproducibility_same_seed_same_plan(ctx: Ctx):
    """Meta/harness test: re-running the SAME seed through fuzz_h9.run_one
    picks the exact same plan/mode/action/hook_profile both times -- this
    is what "any failure is exactly reproducible" (H9-brief.md Part C)
    actually rests on. Uses a fixed seed far outside the default sweep's
    own range so it never collides with (or is coincidentally validated
    only by) whatever base_seed test_fuzz_sweep happens to use."""
    seed = 987654321
    mock = MockUpstream().start()
    try:
        r1 = run_one(seed, mock, timeout_s=_FUZZ_TIMEOUT_S)
        r2 = run_one(seed, mock, timeout_s=_FUZZ_TIMEOUT_S)
    finally:
        mock.stop()
    ctx.check(f"same plan both times, got {r1['plan']!r} vs {r2['plan']!r}", r1["plan"] == r2["plan"])
    ctx.check(f"same mode both times, got {r1['mode']!r} vs {r2['mode']!r}", r1["mode"] == r2["mode"])
    ctx.check(f"same action both times, got {r1['action']!r} vs {r2['action']!r}", r1["action"] == r2["action"])
    ctx.check(f"same hook_profile both times, got {r1['hook_profile']!r} vs {r2['hook_profile']!r}",
               r1["hook_profile"] == r2["hook_profile"])
    ctx.check(f"both runs pass, got r1={r1['ok']} ({r1['error']}) r2={r2['ok']} ({r2['error']})",
               r1["ok"] and r2["ok"])


if __name__ == "__main__":
    ctx = Ctx()
    results, passed, failed, skipped = run_all(TESTS, ctx)
    sys.exit(print_results(results, passed, failed, skipped))
