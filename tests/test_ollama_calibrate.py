"""tests.test_ollama_calibrate -- Halo 2.0.3 round 5b item 1/2: the
`~/.halo/ollama-fit.json` store (learned caps + last-turn throughput) and
`run_calibration`'s stepping loop, against `tests/helpers/mock_ollama.
MockUpstream` -- never a real daemon, never a real model. Every test
scopes its own `BRIDGE_STATE_DIR` (never the real `~/.halo`).
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.helpers.provider_env_defaults import ensure_default_provider_credentials
ensure_default_provider_credentials()

from tests.helpers.runner import Ctx, new_registry, print_results, run_all
from tests.helpers.mock_ollama import SCENARIOS, MockUpstream

test, TESTS = new_registry()


def _fresh_state_dir(prefix: str) -> Path:
    d = Path(tempfile.mkdtemp(prefix=prefix))
    os.environ["BRIDGE_STATE_DIR"] = str(d)
    return d


def _clear_state_dir_env() -> None:
    os.environ.pop("BRIDGE_STATE_DIR", None)


# ---- the ~/.halo/ollama-fit.json store --------------------------------

@test
def test_store_round_trip_record_and_lookup(ctx: Ctx):
    from halo_harness.providers.ollama_calibrate import lookup_learned_cap, record_calibration
    d = _fresh_state_dir("ol-fit-store-")
    try:
        record_calibration(d, host_url="http://h", model="m", digest="sha256:a", max_full_gpu_ctx=16384,
                            ollama_version="0.5.0")
        got = lookup_learned_cap(d, host_url="http://h", model="m")
        ctx.check(f"learned cap round-trips, got {got}", got == 16384)
    finally:
        _clear_state_dir_env()


@test
def test_store_has_calibration_entry_true_even_for_does_not_fit(ctx: Ctx):
    from halo_harness.providers.ollama_calibrate import has_calibration_entry, lookup_learned_cap, record_calibration
    d = _fresh_state_dir("ol-fit-store-nofit-")
    try:
        record_calibration(d, host_url="http://h", model="m", digest="sha256:a", max_full_gpu_ctx=None,
                            ollama_version="0.5.0")
        ctx.check("has_calibration_entry is True (a recorded fact, not a missing one)",
                  has_calibration_entry(d, host_url="http://h", model="m") is True)
        ctx.check("lookup_learned_cap is None (does-not-fit carries no usable cap)",
                  lookup_learned_cap(d, host_url="http://h", model="m") is None)
    finally:
        _clear_state_dir_env()


@test
def test_store_digest_mismatch_invalidates_the_cap(ctx: Ctx):
    from halo_harness.providers.ollama_calibrate import lookup_learned_cap, record_calibration
    d = _fresh_state_dir("ol-fit-store-digest-")
    try:
        record_calibration(d, host_url="http://h", model="m", digest="sha256:old", max_full_gpu_ctx=16384,
                            ollama_version="0.5.0")
        ctx.check("matching digest -> cap returned",
                  lookup_learned_cap(d, host_url="http://h", model="m", digest="sha256:old") == 16384)
        ctx.check("a DIFFERENT digest (re-pulled model) -> stale, None",
                  lookup_learned_cap(d, host_url="http://h", model="m", digest="sha256:new") is None)
        ctx.check("digest=None (the hot per-turn path) -> trusts it anyway",
                  lookup_learned_cap(d, host_url="http://h", model="m", digest=None) == 16384)
    finally:
        _clear_state_dir_env()


@test
def test_store_version_mismatch_invalidates_the_cap(ctx: Ctx):
    from halo_harness.providers.ollama_calibrate import lookup_learned_cap, record_calibration
    d = _fresh_state_dir("ol-fit-store-version-")
    try:
        record_calibration(d, host_url="http://h", model="m", digest="sha256:a", max_full_gpu_ctx=16384,
                            ollama_version="0.5.0")
        ctx.check("matching version -> cap returned",
                  lookup_learned_cap(d, host_url="http://h", model="m", ollama_version="0.5.0") == 16384)
        ctx.check("a DIFFERENT version (server upgraded) -> stale, None",
                  lookup_learned_cap(d, host_url="http://h", model="m", ollama_version="0.6.0") is None)
    finally:
        _clear_state_dir_env()


@test
def test_store_upsert_overwrites_not_duplicates(ctx: Ctx):
    from halo_harness.providers.ollama_calibrate import load_fit_store, lookup_learned_cap, record_calibration
    d = _fresh_state_dir("ol-fit-store-upsert-")
    try:
        record_calibration(d, host_url="http://h", model="m", digest="sha256:a", max_full_gpu_ctx=8192,
                            ollama_version="0.5.0")
        record_calibration(d, host_url="http://h", model="m", digest="sha256:b", max_full_gpu_ctx=16384,
                            ollama_version="0.6.0")
        entries = load_fit_store(d)["entries"]
        ctx.check(f"exactly one entry for (host, model), got {len(entries)}", len(entries) == 1)
        ctx.check(f"the LATEST measurement wins, got {lookup_learned_cap(d, host_url='http://h', model='m')}",
                  lookup_learned_cap(d, host_url="http://h", model="m") == 16384)
    finally:
        _clear_state_dir_env()


@test
def test_store_missing_file_degrades_to_empty(ctx: Ctx):
    from halo_harness.providers.ollama_calibrate import has_calibration_entry, load_fit_store
    d = _fresh_state_dir("ol-fit-store-missing-")
    try:
        ctx.check("no file yet -> empty shape, never raises", load_fit_store(d)["entries"] == [])
        ctx.check("no entry -> False", has_calibration_entry(d, host_url="http://h", model="m") is False)
    finally:
        _clear_state_dir_env()


@test
def test_auto_calibrate_enabled_default_true_and_opt_out(ctx: Ctx):
    from halo_harness.providers.ollama_calibrate import auto_calibrate_enabled
    from halo_harness.theme import set_config_value
    d = _fresh_state_dir("ol-fit-autocal-config-")
    try:
        ctx.check("default True (nothing configured)", auto_calibrate_enabled() is True)
        set_config_value("ollama.auto_calibrate", False)
        ctx.check("explicit false opts out", auto_calibrate_enabled() is False)
        set_config_value("ollama.auto_calibrate", True)
        ctx.check("explicit true", auto_calibrate_enabled() is True)
    finally:
        _clear_state_dir_env()


_GB = 1024 ** 3


class _StepScenario:
    """A `/api/chat` scenario (keyed by model name in `mock.scenarios`)
    that, on each call, sets `mock.ps_response` to the NEXT scripted
    `/api/ps` entry BEFORE answering with a trivial one-line reply --
    simulates "loading at this candidate num_ctx produced this /api/ps
    snapshot" for `run_calibration`'s own stepping loop, entirely through
    `MockUpstream`'s public surface (never touching its private
    internals). `ps_steps[i]` is an entry dict (loaded) or `None` (the
    load failed / model not found); the LAST step repeats once exhausted."""

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


# ---- run_calibration's stepping loop (mock_ollama, never a real model) --

@test
def test_run_calibration_fits_on_the_first_try(ctx: Ctx):
    from halo_harness.providers.ollama import OllamaHost
    from halo_harness.providers.ollama_calibrate import run_calibration
    mock = MockUpstream().start()
    model = "qwen3-coder:30b"
    try:
        mock.scenarios[model] = _StepScenario(mock, [{"model": model, "size": 20 * _GB, "size_vram": 20 * _GB}])
        host = OllamaHost(name="mock", url=mock.base_url)
        result = run_calibration(host, model, start_ctx=16384)
        ctx.check(f"fits, got {result.outcome!r}", result.outcome == "fits")
        ctx.check(f"max_full_gpu_ctx == the starting candidate, got {result.max_full_gpu_ctx}",
                  result.max_full_gpu_ctx == 16384)
        ctx.check(f"exactly one step, got {result.steps}", result.steps == 1)
    finally:
        mock.stop()


@test
def test_run_calibration_fits_after_two_steps(ctx: Ctx):
    from halo_harness.providers.ollama import OllamaHost
    from halo_harness.providers.ollama_calibrate import run_calibration
    mock = MockUpstream().start()
    model = "qwen3-coder:30b"
    try:
        mock.scenarios[model] = _StepScenario(mock, [
            {"model": model, "size": 20 * _GB, "size_vram": 12 * _GB},   # partial at 16384
            {"model": model, "size": 20 * _GB, "size_vram": 20 * _GB},  # fully resident at 8192
        ])
        host = OllamaHost(name="mock", url=mock.base_url)
        result = run_calibration(host, model, start_ctx=16384)
        ctx.check(f"fits, got {result.outcome!r}", result.outcome == "fits")
        ctx.check(f"max_full_gpu_ctx halved once, got {result.max_full_gpu_ctx}", result.max_full_gpu_ctx == 8192)
        ctx.check(f"exactly two steps, got {result.steps}", result.steps == 2)
    finally:
        mock.stop()


@test
def test_run_calibration_never_fits_stops_at_floor(ctx: Ctx):
    from halo_harness.providers.ollama import OllamaHost
    from halo_harness.providers.ollama_calibrate import MIN_CALIBRATE_CTX, run_calibration
    mock = MockUpstream().start()
    model = "qwen3-coder:30b"
    try:
        # Every step reports partial offload -- 16384 -> 8192 -> 4096, then
        # stops (4096 is MIN_CALIBRATE_CTX; 2048 is never tried).
        mock.scenarios[model] = _StepScenario(mock, [{"model": model, "size": 40 * _GB, "size_vram": 2 * _GB}])
        host = OllamaHost(name="mock", url=mock.base_url)
        result = run_calibration(host, model, start_ctx=16384)
        ctx.check(f"does not fit, got {result.outcome!r}", result.outcome == "does_not_fit")
        ctx.check("max_full_gpu_ctx is None (no size fits)", result.max_full_gpu_ctx is None)
        ctx.check(f"stepped 16384 -> 8192 -> {MIN_CALIBRATE_CTX} == 3 steps, got {result.steps}", result.steps == 3)
    finally:
        mock.stop()


@test
def test_run_calibration_load_failure_counts_as_not_resident(ctx: Ctx):
    """A step whose /api/ps entry disappears entirely (model not found --
    `None` in `ps_steps`) steps down exactly like a partial-offload
    reading, never raises."""
    from halo_harness.providers.ollama import OllamaHost
    from halo_harness.providers.ollama_calibrate import run_calibration
    mock = MockUpstream().start()
    model = "qwen3-coder:30b"
    try:
        mock.scenarios[model] = _StepScenario(mock, [None, {"model": model, "size": 1 * _GB, "size_vram": 1 * _GB}])
        host = OllamaHost(name="mock", url=mock.base_url)
        result = run_calibration(host, model, start_ctx=8192)
        ctx.check(f"fits on the second step once the model actually shows up, got {result.outcome!r}",
                  result.outcome == "fits")
        ctx.check(f"max_full_gpu_ctx halved once, got {result.max_full_gpu_ctx}", result.max_full_gpu_ctx == 4096)
    finally:
        mock.stop()


@test
def test_run_calibration_start_ctx_floored_to_power_of_two(ctx: Ctx):
    from halo_harness.providers.ollama import OllamaHost
    from halo_harness.providers.ollama_calibrate import run_calibration
    mock = MockUpstream().start()
    model = "qwen3-coder:30b"
    try:
        mock.scenarios[model] = _StepScenario(mock, [{"model": model, "size": 1 * _GB, "size_vram": 1 * _GB}])
        host = OllamaHost(name="mock", url=mock.base_url)
        result = run_calibration(host, model, start_ctx=20000)  # not a power of two
        ctx.check(f"floored to 16384 before the first load, got {result.max_full_gpu_ctx}",
                  result.max_full_gpu_ctx == 16384)
    finally:
        mock.stop()


# ---- run_auto_calibration (the end-to-end orchestration) ------------------

@test
def test_run_auto_calibration_records_and_returns_a_fits_notice(ctx: Ctx):
    from halo_harness.providers.ollama import OllamaHost
    from halo_harness.providers.ollama_calibrate import lookup_learned_cap, run_auto_calibration
    d = _fresh_state_dir("ol-autocal-fits-")
    mock = MockUpstream().start()
    model = "qwen3:30b"  # already in mock_ollama's own DEFAULT_TAGS/DEFAULT_SHOW
    try:
        mock.scenarios[model] = _StepScenario(mock, [{"model": model, "size": 1 * _GB, "size_vram": 1 * _GB}])
        host = OllamaHost(name="mock", url=mock.base_url)
        notice = run_auto_calibration(host, model, state_dir=d)
        ctx.check(f"a plain notice string, got {notice!r}", isinstance(notice, str) and len(notice) > 0)
        ctx.check(f"notice names the model, got {notice!r}", model in notice)
        got_cap = lookup_learned_cap(d, host_url=host.url, model=model)
        ctx.check(f"recorded to the store, got {got_cap}", isinstance(got_cap, int) and got_cap > 0)
    finally:
        mock.stop()
        _clear_state_dir_env()


@test
def test_run_auto_calibration_unknown_model_returns_none(ctx: Ctx):
    from halo_harness.providers.ollama import OllamaHost
    from halo_harness.providers.ollama_calibrate import run_auto_calibration
    d = _fresh_state_dir("ol-autocal-unknown-")
    mock = MockUpstream().start()
    try:
        host = OllamaHost(name="mock", url=mock.base_url)
        notice = run_auto_calibration(host, "not-in-the-catalog:1b", state_dir=d)
        ctx.check("None (never raises) for a model not in this host's catalog", notice is None)
    finally:
        mock.stop()
        _clear_state_dir_env()


# ---- last-turn throughput persistence --------------------------------

@test
def test_last_turn_throughput_round_trip(ctx: Ctx):
    from halo_harness.providers.ollama_calibrate import get_last_turn_throughput, record_last_turn_throughput
    d = _fresh_state_dir("ol-throughput-")
    try:
        record_last_turn_throughput(d, host_url="http://h", model="m", tokens_per_second=41.0,
                                     prefill_seconds=1.2, offloaded=False, output_tokens=120)
        got = get_last_turn_throughput(d, host_url="http://h", model="m")
        ctx.check(f"tokens_per_second round-trips, got {got}", got["tokens_per_second"] == 41.0)
        ctx.check(f"prefill_seconds round-trips, got {got}", got["prefill_seconds"] == 1.2)
        ctx.check(f"offloaded round-trips, got {got}", got["offloaded"] is False)
    finally:
        _clear_state_dir_env()


@test
def test_last_turn_throughput_unknown_pair_is_none(ctx: Ctx):
    from halo_harness.providers.ollama_calibrate import get_last_turn_throughput
    d = _fresh_state_dir("ol-throughput-missing-")
    try:
        ctx.check("never recorded -> None", get_last_turn_throughput(d, host_url="http://h", model="m") is None)
    finally:
        _clear_state_dir_env()


# ---- Session._maybe_auto_calibrate_ollama's own gating logic -------------

def _minimal_session(state_dir, mock):
    """The SAME minimal-Session construction tests/test_providers_ollama_
    session.py::test_ol_compaction_summary_uses_native_body already uses,
    just enough to call a Session method directly without a real turn.
    UNLIKE that test (which deliberately uses a state_dir separate from
    its own BRIDGE_TEST_HOME, since it never touches calibration), `self.
    state_dir` is passed in explicitly here and MUST equal whatever this
    test itself calls `record_calibration(...)`/`has_calibration_entry(
    ...)` against -- `_maybe_auto_calibrate_ollama` reads/writes through
    `self.state_dir`, matching the real CLI's own `state_dir = bridge_
    home()` (headless.py) exactly, including for a sub-agent Session
    (`agent/subagent.py`'s `_build_child_session` passes `parent.state_
    dir` straight through, never a different one)."""
    from halo_harness.agent.assemble import SessionContext
    from halo_harness.agent.loop import Session
    from halo_harness.model import ModelProfile, parse_model_ref
    from halo_harness.permissions import PermissionEngine
    from halo_harness.providers.stream import ProviderCreds
    cwd = Path(tempfile.mkdtemp(prefix="ol-autocal-cwd-"))
    session_ctx = SessionContext(cwd=cwd, model_label="ol:plain-text")
    return Session(
        cwd=cwd, model_ref=parse_model_ref("ol:plain-text"),
        model_profile=ModelProfile(context_tokens=10_000, max_output_tokens=1_000),
        creds=ProviderCreds(base_url=mock.base_url, api_key=""),
        state_dir=Path(state_dir), model_label="ol:plain-text",
        session_context=session_ctx, max_turns=10,
        permission_engine=PermissionEngine(mode="auto", cwd=cwd),
    )


@test
def test_maybe_auto_calibrate_skips_under_no_background_net(ctx: Ctx):
    """The safe/hermetic case every OTHER test in this suite runs under:
    `BRIDGE_TEST_NO_BACKGROUND_NET=1` means the trigger never touches the
    network at all -- no notice queued, and the (host, model) pair is
    still marked "attempted" so a SECOND call this same process is a fast
    no-op rather than re-checking every time."""
    from halo_harness.providers.ollama import OllamaHost
    d = _fresh_state_dir("ol-autocal-session-nonet-")
    mock = MockUpstream().start()
    try:
        session = _minimal_session(d, mock)
        host = OllamaHost(name="mock", url=mock.base_url)
        ctx.check("BRIDGE_TEST_NO_BACKGROUND_NET is set for this whole suite",
                  os.environ.get("BRIDGE_TEST_NO_BACKGROUND_NET") == "1")
        session._maybe_auto_calibrate_ollama(host, "qwen3:30b")
        ctx.check("no notice queued", session._pending_ollama_notices == [])
        ctx.check("the pair is recorded as attempted",
                  (host.url, "qwen3:30b") in session._ollama_calibrate_attempted)
        before = len(mock.requests)
        session._maybe_auto_calibrate_ollama(host, "qwen3:30b")  # second call, same pair
        ctx.check("a repeat call for the SAME pair never even reaches the dedupe-bypassing checks "
                  "(no new HTTP request at all)", len(mock.requests) == before)
    finally:
        mock.stop()
        _clear_state_dir_env()


@test
def test_maybe_auto_calibrate_skips_when_config_disabled(ctx: Ctx):
    from halo_harness.providers.ollama import OllamaHost
    from halo_harness.theme import set_config_value
    d = _fresh_state_dir("ol-autocal-session-optout-")
    mock = MockUpstream().start()
    try:
        set_config_value("ollama.auto_calibrate", False)
        old_flag = os.environ.pop("BRIDGE_TEST_NO_BACKGROUND_NET", None)
        try:
            session = _minimal_session(d, mock)
            host = OllamaHost(name="mock", url=mock.base_url)
            session._maybe_auto_calibrate_ollama(host, "qwen3:30b")
            ctx.check("opted out -> no notice, no HTTP request at all", session._pending_ollama_notices == []
                      and len(mock.requests) == 0)
        finally:
            if old_flag is not None:
                os.environ["BRIDGE_TEST_NO_BACKGROUND_NET"] = old_flag
    finally:
        mock.stop()
        _clear_state_dir_env()


@test
def test_maybe_auto_calibrate_skips_when_already_calibrated(ctx: Ctx):
    from halo_harness.providers.ollama import OllamaHost
    from halo_harness.providers.ollama_calibrate import record_calibration
    d = _fresh_state_dir("ol-autocal-session-existing-")
    mock = MockUpstream().start()
    try:
        record_calibration(d, host_url=mock.base_url, model="qwen3:30b", digest="sha256:a",
                            max_full_gpu_ctx=16384, ollama_version="0.5.0")
        old_flag = os.environ.pop("BRIDGE_TEST_NO_BACKGROUND_NET", None)
        try:
            session = _minimal_session(d, mock)
            host = OllamaHost(name="mock", url=mock.base_url)
            session._maybe_auto_calibrate_ollama(host, "qwen3:30b")
            ctx.check("an existing entry -> no notice, never re-measured automatically",
                      session._pending_ollama_notices == [])
            chats = [r for r in mock.requests if r["method"] == "POST" and r["path"].rstrip("/") == "/api/chat"]
            ctx.check("never loads the model just to check this", len(chats) == 0)
        finally:
            if old_flag is not None:
                os.environ["BRIDGE_TEST_NO_BACKGROUND_NET"] = old_flag
    finally:
        mock.stop()
        _clear_state_dir_env()


if __name__ == "__main__":
    ctx = Ctx()
    results, passed, failed, skipped = run_all(TESTS, ctx)
    sys.exit(print_results(results, passed, failed, skipped))
