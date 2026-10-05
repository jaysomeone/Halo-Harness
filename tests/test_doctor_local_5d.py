"""tests.test_doctor_local_5d -- Halo 2.0.3 round 5d: `halo doctor --local
[--model ol:x]`, the 60-second acceptance check -- step sequencing (always
all four, in order, even on a resolve failure) and its FAIL text, hermetic
via `tests/helpers/mock_ollama.MockUpstream` (never a real model).
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.helpers.mock_ollama import MockUpstream, end_ndjson, start_ndjson, write_ndjson_line
from tests.helpers.runner import Ctx, new_registry, print_results, run_all

test, TESTS = new_registry()

_STEP_NAMES = ("load", "tool call", "structured output", "compaction summary")


def _finish(handler, lines: list) -> None:
    start_ndjson(handler)
    for obj in lines:
        write_ndjson_line(handler, obj)
    end_ndjson(handler)


def _text_reply(text: str) -> dict:
    return {"message": {"role": "assistant", "content": text}, "done": True, "done_reason": "stop",
            "prompt_eval_count": 20, "eval_count": 5, "prompt_eval_duration": 40_000_000,
            "eval_duration": 80_000_000}


def _tool_reply(name: str, arguments: dict) -> dict:
    return {"message": {"role": "assistant", "content": "", "tool_calls": [
            {"function": {"name": name, "arguments": arguments}}]},
            "done": True, "done_reason": "stop", "prompt_eval_count": 20, "eval_count": 6}


def _doctor_scenario(good: bool):
    def responder(handler, body):
        messages = body.get("messages") or []
        text_all = " ".join(str(m.get("content") or "") for m in messages)
        tools = body.get("tools") or []
        if tools:
            args = {"file_path": "fixture.txt"} if good else {"not_file_path": 1}
            _finish(handler, [_tool_reply("Read", args)])
        elif "Reply with the single word ready" in text_all:
            _finish(handler, [_text_reply("ready")])
        elif "Reply with exactly this JSON object" in text_all:
            _finish(handler, [_text_reply(json.dumps({"answer": "halo"}) if good else "sure, the answer is halo")])
        elif "Summarize the following conversation" in text_all:
            _finish(handler, [_text_reply("Two debug lines were added and all tests passed." if good else "")])
        else:
            _finish(handler, [_text_reply("ok")])
    return responder


class _Env:
    _KEYS = ("BRIDGE_TEST_HOME", "BRIDGE_STATE_DIR", "BRIDGE_ENV_FILE", "OLLAMA_HOST", "OLLAMA_API_KEY")

    def __enter__(self):
        self._saved = {k: os.environ.get(k) for k in self._KEYS}
        d = Path(tempfile.mkdtemp(prefix="doctor-local-5d-"))
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


def _mock_for(model: str, good: bool) -> MockUpstream:
    return MockUpstream(
        scenarios={model: _doctor_scenario(good)},
        tags_response={"models": [{"name": model, "model": model, "digest": "sha256:dl1",
                                    "details": {"family": "x"}}]},
        show_responses={model: {"capabilities": ["tools"], "details": {"family": "x"},
                                 "model_info": {"x.context_length": 8192}}},
    ).start()


@test
def test_acceptance_check_all_pass_in_order(ctx: Ctx):
    from halo_harness.doctor_local import run_local_acceptance_check
    with _Env() as env:
        mock = _mock_for("doctor-pass", good=True)
        try:
            os.environ["OLLAMA_HOST"] = mock.base_url
            steps, ok = run_local_acceptance_check("ol:doctor-pass", state_dir=env.state_dir)
            ctx.check(f"exactly four steps, got {[s['step'] for s in steps]}",
                      tuple(s["step"] for s in steps) == _STEP_NAMES)
            ctx.check(f"every step passed, got {steps}", ok and all(s["ok"] for s in steps))
            ctx.check("every step carries a plain reason and a numeric elapsed time",
                      all(isinstance(s["reason"], str) and s["reason"] and isinstance(s["elapsed_s"], float)
                          for s in steps))
        finally:
            mock.stop()


@test
def test_acceptance_check_fail_text_per_step(ctx: Ctx):
    from halo_harness.doctor_local import format_acceptance_lines, run_local_acceptance_check
    with _Env() as env:
        mock = _mock_for("doctor-fail", good=False)
        try:
            os.environ["OLLAMA_HOST"] = mock.base_url
            steps, ok = run_local_acceptance_check("ol:doctor-fail", state_dir=env.state_dir)
            ctx.check(f"sequencing still runs all four even though every one fails, got "
                      f"{[s['step'] for s in steps]}", tuple(s["step"] for s in steps) == _STEP_NAMES)
            ctx.check(f"overall is FAIL, got ok={ok}", ok is False)
            by_step = {s["step"]: s for s in steps}
            ctx.check(f"load still passes (content doesn't matter there), got {by_step['load']}",
                      by_step["load"]["ok"] is True)
            ctx.check(f"tool call fails with a schema reason, got {by_step['tool call']['reason']}",
                      by_step["tool call"]["ok"] is False and "schema" in by_step["tool call"]["reason"])
            ctx.check(f"structured output fails with a JSON reason, got {by_step['structured output']['reason']}",
                      by_step["structured output"]["ok"] is False and "JSON" in by_step["structured output"]["reason"])
            ctx.check(f"compaction summary fails on an empty reply, got {by_step['compaction summary']['reason']}",
                      by_step["compaction summary"]["ok"] is False and "empty" in by_step["compaction summary"]["reason"])
            lines = format_acceptance_lines(steps)
            ctx.check(f"every rendered line is tagged PASS or FAIL, got {lines}",
                      len(lines) == 4 and sum("[FAIL]" in ln for ln in lines) == 3 and sum("[PASS]" in ln for ln in lines) == 1)
        finally:
            mock.stop()


@test
def test_resolve_failure_reports_all_four_steps_with_one_reason(ctx: Ctx):
    from halo_harness.doctor_local import run_local_acceptance_check
    with _Env() as env:
        steps, ok = run_local_acceptance_check("ol:nope@no-such-host", state_dir=env.state_dir)
        ctx.check(f"still exactly four steps, got {[s['step'] for s in steps]}",
                  tuple(s["step"] for s in steps) == _STEP_NAMES)
        ctx.check(f"all FAIL with the resolve reason, never a crash, got {steps}",
                  ok is False and all(not s["ok"] for s in steps)
                  and len({s["reason"] for s in steps}) == 1)


@test
def test_cmd_doctor_local_cli_exit_codes_and_output(ctx: Ctx):
    from halo_harness.doctor import cmd_doctor
    with _Env() as env:
        mock = _mock_for("doctor-cli-pass", good=True)
        try:
            os.environ["OLLAMA_HOST"] = mock.base_url
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = cmd_doctor(["--local", "--model", "ol:doctor-cli-pass"])
            ctx.check(f"exit 0 on a clean pass, got {rc}", rc == 0)
            ctx.check(f"PASS lines printed, got {buf.getvalue()!r}", buf.getvalue().count("[PASS]") == 4)
        finally:
            mock.stop()
        mock2 = _mock_for("doctor-cli-fail", good=False)
        try:
            os.environ["OLLAMA_HOST"] = mock2.base_url
            buf2 = io.StringIO()
            with contextlib.redirect_stdout(buf2):
                rc2 = cmd_doctor(["--local", "--model", "ol:doctor-cli-fail"])
            ctx.check(f"exit 1 when any step fails, got {rc2}", rc2 == 1)
            ctx.check(f"at least one FAIL line printed, got {buf2.getvalue()!r}", "[FAIL]" in buf2.getvalue())
        finally:
            mock2.stop()


if __name__ == "__main__":
    ctx = Ctx()
    results, passed, failed, skipped = run_all(TESTS, ctx)
    sys.exit(print_results(results, passed, failed, skipped))
