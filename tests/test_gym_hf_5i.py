"""tests.test_gym_hf_5i -- Halo 2.0.3 round 5i: `halo gym`'s `hf:local/*`
support (and `hf:mlx/*`'s portable refusal) -- the SAME battery
(`gym_tool_tasks.py`/`gym_reply_tasks.py`) through the round 5f shared
sender (`providers.huggingface_send.send_hf_turn`, dispatched by `gym_
send.send_turn_for`), pinned hermetically against `tests/helpers/
mock_openai.MockUpstream` (never a real server, never real mlx_lm). Mirrors
`tests.test_gym_5d`'s own Ollama scenario shape one to one for the
openai-chat delta/SSE wire instead of Ollama's native NDJSON.
"""
from __future__ import annotations

import contextlib
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import tests.helpers.mock_openai as mock_openai_mod
from tests.helpers.mock_openai import MockUpstream, _finish
from tests.helpers.runner import Ctx, new_registry, print_results, run_all

test, TESTS = new_registry()


def _text_chunks(text: str) -> list:
    return [
        {"choices": [{"index": 0, "delta": {"role": "assistant"}}]},
        {"choices": [{"index": 0, "delta": {"content": text}}]},
        {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
    ]


def _tool_chunks(name: str, arguments: dict) -> list:
    return [
        {"choices": [{"index": 0, "delta": {"role": "assistant"}}]},
        {"choices": [{"index": 0, "delta": {"tool_calls": [
            {"index": 0, "id": "call_1", "type": "function",
             "function": {"name": name, "arguments": json.dumps(arguments)}}]}}]},
        {"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]},
    ]


class GymHFScenario:
    """The openai-chat-delta twin of `tests.test_gym_5d.GymScenario` --
    same content-based dispatch (one responder covering every turn one
    `run_gym_for_model` call sends), same `mode` meaning ("pass"/"fail"/
    alternating "half")."""

    def __init__(self, mode: str):
        self.mode = mode
        self.calls = 0

    def _good(self) -> bool:
        self.calls += 1
        if self.mode == "pass":
            return True
        if self.mode == "fail":
            return False
        return self.calls % 2 == 1

    def __call__(self, h, body) -> None:
        messages = body.get("messages") or []
        text_all = " ".join(str(m.get("content") or "") for m in messages)
        tools = body.get("tools") or []
        good = self._good()
        if tools:
            fn_name = (tools[0].get("function") or {}).get("name")
            if fn_name == "Read":
                args = {"file_path": "fixture.txt"} if good else {"oops": "not a file_path at all"}
            elif fn_name == "Edit":
                args = ({"file_path": "fixture.txt", "old_string": "world", "new_string": "halo"} if good
                        else {"file_path": "fixture.txt"})
            else:
                args = {}
            _finish(h, _tool_chunks(fn_name or "Read", args))
            return
        if "repairing exactly one malformed tool call" in text_all:
            text = json.dumps({"file_path": "repaired.txt"} if self.mode != "fail" else {"still": "wrong"})
            _finish(h, _text_chunks(text))
        elif "secret checkpoint code" in text_all:
            _finish(h, _text_chunks("7k2p9" if good else "I don't know that one"))
        elif "what color is grass" in text_all:
            _finish(h, _text_chunks("green" if good else "The grass is green, typically"))
        elif "what is 7 + 5" in text_all:
            _finish(h, _text_chunks("12" if good else "The answer to 7 + 5 is 12"))
        else:
            _finish(h, _text_chunks("ok"))


@contextlib.contextmanager
def _installed_scenario(name: str, fn):
    """`mock_openai.SCENARIOS` is a plain module-level dict with no per-
    instance override (unlike `mock_ollama.MockUpstream`'s own
    constructor) -- this round does not edit that shared helper file, so
    a scenario is registered into the live dict for the duration of one
    test and always removed after, even on failure."""
    mock_openai_mod.SCENARIOS[name] = fn
    try:
        yield
    finally:
        mock_openai_mod.SCENARIOS.pop(name, None)


class _Env:
    _KEYS = ("BRIDGE_TEST_HOME", "BRIDGE_STATE_DIR", "BRIDGE_ENV_FILE", "OLLAMA_HOST")

    def __enter__(self):
        self._saved = {k: os.environ.get(k) for k in self._KEYS}
        d = Path(tempfile.mkdtemp(prefix="gym-hf-5i-"))
        os.environ["BRIDGE_TEST_HOME"] = str(d)
        os.environ["BRIDGE_STATE_DIR"] = str(d / ".halo")
        os.environ["BRIDGE_ENV_FILE"] = str(d / "no-env-file")
        self.state_dir = d / ".halo"
        return self

    def __exit__(self, *exc):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def _configure_local_server(name: str, base_url: str) -> None:
    from halo_harness.theme import set_config_value
    set_config_value("huggingface.local_servers", [{"name": name, "url": base_url, "default": True}])


@test
def test_hf_local_pass_scenario_scores_well(ctx: Ctx):
    from halo_harness.gym_run import run_gym_for_model
    with _Env() as env, _installed_scenario("gym-hf-pass", GymHFScenario("pass")):
        mock = MockUpstream(path_prefix="/v1").start()
        try:
            _configure_local_server("bench", mock.base_url)
            result = run_gym_for_model("hf:local/mock/gym-hf-pass@bench", quick=True, state_dir=env.state_dir)
            ctx.check(f"no run-level errors, got {result['errors']}", result["errors"] == [])
            ctx.check(f"tool-call accuracy is perfect, got {result['tool_call_accuracy']}",
                      result["tool_call_accuracy"]["score"] == 1.0)
            ctx.check(f"edit success is perfect, got {result['edit_success']}", result["edit_success"]["score"] == 1.0)
            ctx.check(f"context recall is perfect, got {result['context_recall']}",
                      result["context_recall"]["score"] == 1.0)
            ctx.check(f"instruction adherence is perfect, got {result['instruction_adherence']}",
                      result["instruction_adherence"]["score"] == 1.0)
            ctx.check(f"grouped under the shared huggingface host-slug, got {result['host_name']}",
                      result["host_name"] == "huggingface")
            ctx.check(f"keyed by the server's own name (stable id), got {result['digest']}",
                      result["digest"] == "bench")
            ctx.check(f"tokens_per_second measured, got {result['tokens_per_second']}",
                      isinstance(result["tokens_per_second"], (int, float)))
            ctx.check(f"prefill_seconds is honestly None (no TTFT available through this sender), "
                      f"got {result['prefill_seconds']}", result["prefill_seconds"] is None)
        finally:
            mock.stop()


@test
def test_hf_local_half_scenario_counts_repairs_separately(ctx: Ctx):
    from halo_harness.gym_run import run_gym_for_model
    with _Env() as env, _installed_scenario("gym-hf-half", GymHFScenario("half")):
        mock = MockUpstream(path_prefix="/v1").start()
        try:
            _configure_local_server("bench2", mock.base_url)
            result = run_gym_for_model("hf:local/mock/gym-hf-half@bench2", quick=False, state_dir=env.state_dir)
            tca = result["tool_call_accuracy"]
            ctx.check(f"roughly half pass first try, got {tca}", 0.0 < tca["score"] < 1.0)
            ctx.check(f"repairs recover some of the rest, got {tca}",
                      tca["repair_rounds"] > 0 and tca["valid_after_repair"] > 0)
        finally:
            mock.stop()


@test
def test_hf_mlx_on_non_apple_box_is_a_clean_refusal(ctx: Ctx):
    """`ensure_mlx_server` (round 5f) has no platform-injection seam
    `gym_run.py` threads through either (same design choice `doctor_
    local.py` already documents) -- on THIS box (never Apple Silicon)
    the refusal is the one deterministic, portable outcome to pin."""
    from halo_harness.gym_run import run_gym_for_model
    with _Env() as env:
        result = run_gym_for_model("hf:mlx/mlx-community/does-not-matter", quick=True, state_dir=env.state_dir)
        ctx.check(f"one plain error line, never a crash, got {result['errors']}",
                  len(result["errors"]) == 1 and "Apple Silicon" in result["errors"][0])
        ctx.check("every score stayed None (never attempted)",
                  all(result[k]["score"] is None for k in
                      ("tool_call_accuracy", "edit_success", "context_recall", "instruction_adherence")))


@test
def test_mixed_ollama_and_huggingface_run(ctx: Ctx):
    """The coordinator's own worked example: `--models ol:...,hf:local/...`
    in ONE `run_gym` call, against both fakes at once."""
    from tests.helpers.mock_ollama import MockUpstream as OllamaMockUpstream
    from tests.test_gym_5d import GymScenario as OllamaGymScenario
    from halo_harness.gym_run import run_gym
    with _Env() as env, _installed_scenario("gym-hf-pass", GymHFScenario("pass")):
        ollama_mock = OllamaMockUpstream(
            scenarios={"mix-ollama": OllamaGymScenario("pass")},
            tags_response={"models": [{"name": "mix-ollama", "model": "mix-ollama", "digest": "sha256:mix1",
                                        "details": {"family": "x"}}]},
            show_responses={"mix-ollama": {"capabilities": ["tools"], "details": {"family": "x"},
                                           "model_info": {"x.context_length": 8192}}}).start()
        hf_mock = MockUpstream(path_prefix="/v1").start()
        try:
            os.environ["OLLAMA_HOST"] = ollama_mock.base_url
            _configure_local_server("bench3", hf_mock.base_url)
            results = run_gym(["ol:mix-ollama", "hf:local/mock/gym-hf-pass@bench3"], quick=True,
                               state_dir=env.state_dir)
            ctx.check(f"both models scored, got {[r['model_ref'] for r in results]}", len(results) == 2)
            by_ref = {r["model_ref"]: r for r in results}
            ctx.check("the ollama result landed under its own host dir",
                      by_ref["ol:mix-ollama"]["host_name"] == "default")
            ctx.check("the hf:local result landed under the shared huggingface host dir",
                      by_ref["hf:local/mock/gym-hf-pass@bench3"]["host_name"] == "huggingface")
            ctx.check("both scored well", by_ref["ol:mix-ollama"]["tool_call_accuracy"]["score"] == 1.0
                      and by_ref["hf:local/mock/gym-hf-pass@bench3"]["tool_call_accuracy"]["score"] == 1.0)
            from halo_harness.gym import iter_results
            saved = iter_results(env.state_dir)
            ctx.check(f"both saved under separate host-slug dirs, got {[r['host_name'] for r in saved]}",
                      {"default", "huggingface"} <= {r["host_name"] for r in saved})
        finally:
            ollama_mock.stop()
            hf_mock.stop()


if __name__ == "__main__":
    ctx = Ctx()
    results, passed, failed, skipped = run_all(TESTS, ctx)
    sys.exit(print_results(results, passed, failed, skipped))
