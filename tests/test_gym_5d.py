"""tests.test_gym_5d -- Halo 2.0.3 round 5d: the model gym's scoring
arithmetic, result-file shape, `gym show`, the picker's score suffix,
`--quick` sizing, and the battery itself run end to end against
`tests/helpers/mock_ollama.MockUpstream` with scripted PASS/FAIL/HALF-PASS
scenarios (never a real model). `gym_propose`'s own fixture-based tests
live in `tests/test_gym_propose_5d.py`; `doctor --local`'s own tests live
in `tests/test_doctor_local_5d.py`.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.helpers.mock_ollama import MockUpstream, end_ndjson, start_ndjson, write_ndjson_line
from tests.helpers.runner import Ctx, new_registry, print_results, run_all

test, TESTS = new_registry()


def _finish(handler, lines: list) -> None:
    start_ndjson(handler)
    for obj in lines:
        write_ndjson_line(handler, obj)
    end_ndjson(handler)


def _text_reply(text: str, **timing) -> dict:
    base = {"message": {"role": "assistant", "content": text}, "done": True, "done_reason": "stop",
            "prompt_eval_count": 50, "eval_count": 8, "prompt_eval_duration": 80_000_000,
            "eval_duration": 150_000_000}
    base.update(timing)
    return base


def _tool_reply(name: str, arguments: dict) -> dict:
    return {"message": {"role": "assistant", "content": "", "tool_calls": [
            {"function": {"name": name, "arguments": arguments}}]},
            "done": True, "done_reason": "stop", "prompt_eval_count": 30, "eval_count": 10,
            "prompt_eval_duration": 60_000_000, "eval_duration": 120_000_000}


class GymScenario:
    """A smart scripted `/api/chat` responder covering every turn one
    `run_gym_for_model` call sends (tool-call accuracy + its repair round,
    edit success, context recall, instruction adherence) -- `mode`
    decides whether each independent attempt comes back right ("pass"),
    wrong ("fail"), or alternates ("half", starting with a pass)."""

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

    def __call__(self, handler, body) -> None:
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
                        else {"file_path": "fixture.txt"})  # missing old_string/new_string
            else:
                args = {}
            _finish(handler, [_tool_reply(fn_name or "Read", args)])
            return
        if "repairing exactly one malformed tool call" in text_all:
            text = json.dumps({"file_path": "repaired.txt"} if self.mode != "fail" else {"still": "wrong"})
            _finish(handler, [_text_reply(text)])
            return
        if "secret checkpoint code" in text_all:
            _finish(handler, [_text_reply("7k2p9" if good else "I don't know that one")])
        elif "what color is grass" in text_all:
            _finish(handler, [_text_reply("green" if good else "The grass is green, typically")])
        elif "what is 7 + 5" in text_all:
            _finish(handler, [_text_reply("12" if good else "The answer to 7 + 5 is 12")])
        elif "Reply with the single word ready" in text_all:
            _finish(handler, [_text_reply("ready")])
        elif "Reply with exactly this JSON object" in text_all:
            _finish(handler, [_text_reply(json.dumps({"answer": "halo" if good else "nope"}))])
        elif "Summarize the following conversation" in text_all:
            _finish(handler, [_text_reply("The assistant added two debug log lines and all tests passed."
                                           if good else "")])
        else:
            _finish(handler, [_text_reply("ok")])


class _Env:
    _KEYS = ("BRIDGE_TEST_HOME", "BRIDGE_STATE_DIR", "BRIDGE_ENV_FILE", "OLLAMA_HOST", "OLLAMA_API_KEY")

    def __enter__(self):
        self._saved = {k: os.environ.get(k) for k in self._KEYS}
        d = Path(tempfile.mkdtemp(prefix="gym-5d-"))
        os.environ["BRIDGE_TEST_HOME"] = str(d)
        os.environ["BRIDGE_STATE_DIR"] = str(d / ".halo")
        os.environ["BRIDGE_ENV_FILE"] = str(d / "no-env-file")
        os.environ.pop("OLLAMA_API_KEY", None)
        self.state_dir = d / ".halo"
        return self

    def __exit__(self, *exc):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


@test
def test_ratio_score_and_overall_score_arithmetic(ctx: Ctx):
    from halo_harness.gym import RatioScore, ToolCallAccuracy, overall_score
    empty = RatioScore()
    ctx.check("an untried RatioScore scores None, not 0.0", empty.score is None)
    half = RatioScore(attempted=4, succeeded=1)
    ctx.check(f"3/4 -> 0.25, got {half.score}", half.score == 0.25)
    tca = ToolCallAccuracy(attempted=4, valid_first_try=2, valid_after_repair=1, failed=1, repair_rounds=2)
    ctx.check(f"headline score excludes repairs, got {tca.score}", tca.score == 0.5)
    ctx.check(f"repaired_score includes them, got {tca.repaired_score}", tca.repaired_score == 0.75)
    result = {"tool_call_accuracy": {"score": 1.0}, "edit_success": {"score": 0.5},
              "context_recall": {"score": None}, "instruction_adherence": {"score": 0.5}}
    ctx.check(f"overall_score means only the non-None ones, got {overall_score(result)}",
              abs(overall_score(result) - (1.0 + 0.5 + 0.5) / 3) < 1e-9)
    ctx.check("overall_score is None when nothing was measured",
              overall_score({"tool_call_accuracy": {"score": None}, "edit_success": {"score": None},
                             "context_recall": {"score": None}, "instruction_adherence": {"score": None}}) is None)


@test
def test_host_and_digest_slugs_are_filesystem_safe(ctx: Ctx):
    from halo_harness.gym import digest_slug, host_slug
    ctx.check("bare host name", host_slug("default") == "default")
    ctx.check("empty/None host falls back", host_slug(None) == "default" and host_slug("") == "default")
    ctx.check("sha256 prefix stripped", digest_slug("sha256:DeadBeef1") == "deadbeef1")
    ctx.check("missing digest falls back", digest_slug(None) == "nodigest")
    weird = host_slug("My LAN Box!!")
    ctx.check(f"unsafe chars become dashes, got {weird!r}", weird == "my-lan-box")


@test
def test_result_file_round_trip(ctx: Ctx):
    from halo_harness.gym import (GymResult, find_results_for_model, iter_results, load_result, result_path,
                                   save_result)
    d = Path(tempfile.mkdtemp(prefix="gym-store-"))
    result = GymResult(model="qwen3-coder:30b", model_ref="ol:qwen3-coder:30b", host_name="default",
                        host_url="http://127.0.0.1:11434", digest="sha256:abc123", quantization="Q4_0",
                        fitted_context=32768, ollama_version="0.34.2", finished_at=1000.0).to_dict()
    path = save_result(d, result)
    ctx.check(f"saved under gym/<host>/<digest>.json, got {path}",
              path == result_path(d, "default", "sha256:abc123") and path.exists())
    loaded = load_result(path)
    ctx.check("round-trips byte-identical (via JSON)", loaded == result)
    ctx.check("iter_results finds it", iter_results(d) == [result])
    ctx.check("find_results_for_model matches the bare model name", find_results_for_model(d, "qwen3-coder:30b"))
    ctx.check("find_results_for_model matches the full ref", find_results_for_model(d, "ol:qwen3-coder:30b"))
    ctx.check("a model with nothing saved finds nothing", find_results_for_model(d, "ol:nope") == [])
    ctx.check("iter_results on a fresh dir is empty, never raises",
              iter_results(Path(tempfile.mkdtemp(prefix="gym-empty-"))) == [])


@test
def test_picker_score_suffix(ctx: Ctx):
    from halo_harness.gym import picker_score_suffix, save_result
    d = Path(tempfile.mkdtemp(prefix="gym-picker-"))
    ctx.check("no suffix when nothing is on file", picker_score_suffix("ol:nope", state_dir=d) == "")
    save_result(d, {"model": "qwen3-coder:30b", "model_ref": "ol:qwen3-coder:30b", "host_name": "default",
                     "host_url": "http://x", "digest": "sha256:abc", "finished_at": 1.0,
                     "tool_call_accuracy": {"score": 1.0}, "edit_success": {"score": 1.0},
                     "context_recall": {"score": 1.0}, "instruction_adherence": {"score": 1.0},
                     "tokens_per_second": 42.0})
    suffix = picker_score_suffix("ol:qwen3-coder:30b", state_dir=d)
    ctx.check(f"shows a score and tok/s, got {suffix!r}", "gym 1.00" in suffix and "42 tok/s" in suffix)
    ctx.check("never raises on a garbage state_dir", picker_score_suffix("ol:x", state_dir="/does/not/exist") == "")


def _run_one(mock, model_name: str, *, quick: bool, state_dir) -> dict:
    from halo_harness.gym_run import run_gym_for_model
    return run_gym_for_model(f"ol:{model_name}", quick=quick, state_dir=state_dir)


@test
def test_battery_pass_scenario_scores_well(ctx: Ctx):
    with _Env() as env:
        mock = MockUpstream(scenarios={"gym-pass": GymScenario("pass")},
                             tags_response={"models": [{"name": "gym-pass", "model": "gym-pass",
                                            "digest": "sha256:p1", "details": {"family": "x",
                                            "quantization_level": "Q4_0"}}]},
                             show_responses={"gym-pass": {"capabilities": ["tools"], "details": {"family": "x"},
                                             "model_info": {"x.context_length": 8192}}}).start()
        try:
            os.environ["OLLAMA_HOST"] = mock.base_url
            result = _run_one(mock, "gym-pass", quick=True, state_dir=env.state_dir)
            ctx.check(f"no run-level errors, got {result['errors']}", result["errors"] == [])
            ctx.check(f"tool-call accuracy is perfect, got {result['tool_call_accuracy']}",
                      result["tool_call_accuracy"]["score"] == 1.0 and result["tool_call_accuracy"]["repair_rounds"] == 0)
            ctx.check(f"edit success is perfect, got {result['edit_success']}", result["edit_success"]["score"] == 1.0)
            ctx.check(f"context recall is perfect, got {result['context_recall']}",
                      result["context_recall"]["score"] == 1.0)
            ctx.check(f"instruction adherence is perfect, got {result['instruction_adherence']}",
                      result["instruction_adherence"]["score"] == 1.0)
            ctx.check(f"digest/quant/fitted_context/version captured, got {result}",
                      result["digest"] == "sha256:p1" and result["quantization"] == "Q4_0"
                      and isinstance(result["fitted_context"], int) and result["ollama_version"])
            ctx.check(f"throughput measured, got {result['tokens_per_second']}",
                      isinstance(result["tokens_per_second"], (int, float)))
            from halo_harness.gym import QUICK_N
            ctx.check(f"--quick halves N, got attempted={result['tool_call_accuracy']['attempted']}",
                      result["tool_call_accuracy"]["attempted"] == QUICK_N)
        finally:
            mock.stop()


@test
def test_battery_fail_scenario_scores_zero_and_counts_repairs(ctx: Ctx):
    with _Env() as env:
        mock = MockUpstream(scenarios={"gym-fail": GymScenario("fail")},
                             tags_response={"models": [{"name": "gym-fail", "model": "gym-fail",
                                            "digest": "sha256:f1", "details": {"family": "x"}}]},
                             show_responses={"gym-fail": {"capabilities": [], "details": {"family": "x"},
                                             "model_info": {"x.context_length": 8192}}}).start()
        try:
            os.environ["OLLAMA_HOST"] = mock.base_url
            result = _run_one(mock, "gym-fail", quick=True, state_dir=env.state_dir)
            tca = result["tool_call_accuracy"]
            ctx.check(f"every attempt needed (and failed) repair, got {tca}",
                      tca["score"] == 0.0 and tca["repair_rounds"] == tca["attempted"] and tca["valid_after_repair"] == 0)
            ctx.check(f"edit success is zero, got {result['edit_success']}", result["edit_success"]["score"] == 0.0)
            ctx.check(f"context recall is zero, got {result['context_recall']}",
                      result["context_recall"]["score"] == 0.0)
            ctx.check(f"instruction adherence is zero, got {result['instruction_adherence']}",
                      result["instruction_adherence"]["score"] == 0.0)
        finally:
            mock.stop()


@test
def test_battery_half_scenario_counts_repairs_separately(ctx: Ctx):
    with _Env() as env:
        mock = MockUpstream(scenarios={"gym-half": GymScenario("half")},
                             tags_response={"models": [{"name": "gym-half", "model": "gym-half",
                                            "digest": "sha256:h1", "details": {"family": "x"}}]},
                             show_responses={"gym-half": {"capabilities": ["tools"], "details": {"family": "x"},
                                             "model_info": {"x.context_length": 8192}}}).start()
        try:
            os.environ["OLLAMA_HOST"] = mock.base_url
            result = _run_one(mock, "gym-half", quick=False, state_dir=env.state_dir)
            tca = result["tool_call_accuracy"]
            ctx.check(f"roughly half pass first try, got {tca}", 0.0 < tca["score"] < 1.0)
            ctx.check(f"repairs recover some of the rest (repaired_score > score), got {tca}",
                      tca["repair_rounds"] > 0 and tca["valid_after_repair"] > 0)
            from halo_harness.gym import format_card
            card = format_card(result)
            ctx.check("the card mentions the repair rounds", "repair round" in card)
        finally:
            mock.stop()


if __name__ == "__main__":
    ctx = Ctx()
    results, passed, failed, skipped = run_all(TESTS, ctx)
    sys.exit(print_results(results, passed, failed, skipped))
