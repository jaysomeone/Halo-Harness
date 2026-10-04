"""tests.test_huggingface_tool_reliability_5b2 -- Halo 2.0.3 round 5b part
2 (brief items 1/2, as corrected by the 2026-10-04 fix pass) + the NEW
identical-call loop guard, for `hf:local/*` (openai-chat dialect) against
`tests/helpers/mock_openai.MockUpstream` -- reuses that module's own
`SCENARIOS`/`_finish`/`ScriptedTurns` machinery per the round-4 brief's own
instruction, never a second fake server.

Fix pass: an ordinary turn -- including right after a tool result -- is
NEVER constrained any more (see tests/test_ollama_tool_reliability_5b2.py's
own module docstring for the live-run story). The old "response_format
present after a tool result"/"fallback on 400" tests are gone with the
mechanism they tested.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.helpers.runner import Ctx, new_registry, print_results, run_all
from tests.helpers.fake_home import build_fake_home
from tests.helpers.mock_openai import MockUpstream, SCENARIOS, _finish

REPO_DIR = Path(__file__).resolve().parent.parent
test, TESTS = new_registry()


def _hermetic_child_env() -> dict:
    env = dict(os.environ)
    env.pop("BRIDGE_STATE_DIR", None)
    for k in [k for k in env if k.startswith("HALO_")]:
        env.pop(k, None)
    env.pop("HF_TOKEN", None)
    env.pop("OLLAMA_HOST", None)
    return env


def _run_cli(fh, prompt, *, model: str, extra_env=None, timeout=30):
    env = _hermetic_child_env()
    env.update({"BRIDGE_TEST_HOME": str(fh["home"]), "BRIDGE_TEST_NO_BACKGROUND_NET": "1",
                "PYTHONPATH": str(REPO_DIR)})
    env.update(extra_env or {})
    args = [sys.executable, "-m", "halo_harness", "-p", prompt, "--model", model, "--cwd", str(fh["proj"])]
    return subprocess.run(args, env=env, cwd=str(REPO_DIR), capture_output=True, text=True, timeout=timeout)


def _seed_config(fh, key: str, value) -> None:
    old_home = os.environ.get("BRIDGE_TEST_HOME")
    old_state_dir = os.environ.get("BRIDGE_STATE_DIR")
    os.environ["BRIDGE_TEST_HOME"] = str(fh["home"])
    os.environ.pop("BRIDGE_STATE_DIR", None)
    try:
        from halo_harness.theme import set_config_value
        set_config_value(key, value)
    finally:
        if old_home is None:
            os.environ.pop("BRIDGE_TEST_HOME", None)
        else:
            os.environ["BRIDGE_TEST_HOME"] = old_home
        if old_state_dir is not None:
            os.environ["BRIDGE_STATE_DIR"] = old_state_dir


def _chat_requests(mock) -> list:
    return [r for r in mock.requests if r["method"] == "POST"]


# ---- item 1: constrained decoding on the wire --------------------------

@test
def test_hf_local_no_response_format_on_first_turn(ctx: Ctx):
    scn = "hf-fmt-turn1"
    SCENARIOS[scn] = lambda h, b: _finish(h, [
        {"choices": [{"index": 0, "delta": {"role": "assistant"}}]},
        {"choices": [{"index": 0, "delta": {"content": "pong"}}]},
        {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
    ])
    fh = build_fake_home()
    mock = MockUpstream(path_prefix="/v1").start()
    try:
        _seed_config(fh, "huggingface.local_servers", [{"name": "bench", "url": mock.base_url, "default": True}])
        result = _run_cli(fh, "say hi please", model=f"hf:local/mock/{scn}@bench")
        ctx.check(f"exit 0, got {result.returncode} stderr={result.stderr[-500:]!r}", result.returncode == 0)
        chats = _chat_requests(mock)
        ctx.check(f"one call, got {len(chats)}", len(chats) == 1)
        ctx.check("no response_format on the first turn", "response_format" not in chats[0]["body"])
    finally:
        mock.stop()


@test
def test_hf_local_no_response_format_after_a_tool_result_either(ctx: Ctx):
    """The exact live-run failure mode (see tests/
    test_ollama_tool_reliability_5b2.py's own module docstring): a model
    answering freely in prose right after a tool result must not be
    forced into the tool-call shape -- `response_format` must be absent
    from BOTH requests."""
    scn = "hf-fmt-turn2"
    note_path = None

    def _turn1(h, body):
        _finish(h, [
            {"choices": [{"index": 0, "delta": {"role": "assistant"}}]},
            {"choices": [{"index": 0, "delta": {"tool_calls": [
                {"index": 0, "id": "call_1", "type": "function",
                 "function": {"name": "Read", "arguments": json.dumps({"file_path": str(note_path)})}}]}}]},
            {"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]},
        ])

    def _turn2(h, body):
        seen["format"] = body.get("response_format")
        seen["tools"] = body.get("tools")
        _finish(h, [
            {"choices": [{"index": 0, "delta": {"role": "assistant"}}]},
            {"choices": [{"index": 0, "delta": {"content": "Done!"}}]},
            {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
        ])

    from tests.helpers.mock_openai import ScriptedTurns
    seen: dict = {}
    fh = build_fake_home()
    note_path = fh["proj"] / "note.txt"
    note_path.write_text("MARKER\n", encoding="utf-8")
    SCENARIOS[scn] = ScriptedTurns([_turn1, _turn2])
    mock = MockUpstream(path_prefix="/v1").start()
    try:
        _seed_config(fh, "huggingface.local_servers", [{"name": "bench", "url": mock.base_url, "default": True}])
        result = _run_cli(fh, "please read the note file", model=f"hf:local/mock/{scn}@bench",
                           extra_env={}, timeout=30)
        ctx.check(f"exit 0, got {result.returncode} stderr={result.stderr[-800:]!r}", result.returncode == 0)
        ctx.check(f"two calls, got {len(_chat_requests(mock))}", len(_chat_requests(mock)) == 2)
        ctx.check(f"NO response_format after the tool result either, got {seen.get('format')!r}",
                  seen.get("format") is None)
        ctx.check("tools still offered (native tool_calls untouched)", bool(seen.get("tools")))
        ctx.check(f"the free prose reply reached stdout, got {result.stdout!r}", "Done!" in result.stdout)
    finally:
        mock.stop()


@test
def test_hf_local_identical_call_guard_stops_after_the_third_call(ctx: Ctx):
    """The hf:local twin of the ollama loop-guard pin: the SAME tool call
    repeated three times in a row stops the turn, never a fourth
    request."""
    scn = "hf-loop-guard"

    def _same_call_every_time(h, body):
        _finish(h, [
            {"choices": [{"index": 0, "delta": {"role": "assistant"}}]},
            {"choices": [{"index": 0, "delta": {"tool_calls": [
                {"index": 0, "id": "call_x", "type": "function",
                 "function": {"name": "TaskStop", "arguments": json.dumps({"task_id": "no-such-task"})}}]}}]},
            {"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]},
        ])

    SCENARIOS[scn] = _same_call_every_time
    fh = build_fake_home()
    mock = MockUpstream(path_prefix="/v1").start()
    try:
        _seed_config(fh, "huggingface.local_servers", [{"name": "bench", "url": mock.base_url, "default": True}])
        result = _run_cli(fh, "finish up", model=f"hf:local/mock/{scn}@bench", timeout=40)
        ctx.check(f"exit 0 or 1 (not hung/killed), got {result.returncode}", result.returncode in (0, 1))
        chats = _chat_requests(mock)
        ctx.check(f"exactly THREE calls -- the loop guard stopped the turn after the third, never a "
                  f"fourth, got {len(chats)}", len(chats) == 3)
    finally:
        mock.stop()


# ---- item 2: the local repair round -------------------------------------

@test
def test_hf_local_repair_round_fixes_malformed_json_args(ctx: Ctx):
    """A plain call-counter scenario, NOT `ScriptedTurns` -- the repair
    round's own request is ISOLATED (no prior messages at all), so it has
    the SAME "zero role:tool messages" shape as the very first turn;
    `ScriptedTurns`'s own tool-message-count keying cannot tell the two
    apart (confirmed the hard way: using it here made the repair call
    silently replay the first turn's own malformed-call step instead)."""
    scn = "hf-repair-malformed"
    seen: dict = {}
    calls: list = []

    def _scn(h, body):
        calls.append(body)
        n = len(calls)
        if n == 1:
            _finish(h, [
                {"choices": [{"index": 0, "delta": {"role": "assistant"}}]},
                {"choices": [{"index": 0, "delta": {"tool_calls": [
                    {"index": 0, "id": "call_1", "type": "function",
                     "function": {"name": "Bash", "arguments": '{"command": '}}]}}]},
                {"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]},
            ])
        elif n == 2:
            seen["repair_format"] = body.get("response_format")
            seen["repair_tools"] = body.get("tools")
            _finish(h, [
                {"choices": [{"index": 0, "delta": {"role": "assistant"}}]},
                {"choices": [{"index": 0, "delta": {"content": '{"command": "echo hi"}'}}]},
                {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
            ])
        else:
            tool_msgs = [m for m in (body.get("messages") or []) if m.get("role") == "tool"]
            seen["followup_tool_msgs"] = tool_msgs
            _finish(h, [
                {"choices": [{"index": 0, "delta": {"role": "assistant"}}]},
                {"choices": [{"index": 0, "delta": {"content": "ran it"}}]},
                {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
            ])

    SCENARIOS[scn] = _scn
    fh = build_fake_home()
    mock = MockUpstream(path_prefix="/v1").start()
    try:
        _seed_config(fh, "huggingface.local_servers", [{"name": "bench", "url": mock.base_url, "default": True}])
        result = _run_cli(fh, "run echo hi", model=f"hf:local/mock/{scn}@bench", timeout=40)
        ctx.check(f"exit 0, got {result.returncode} stderr={result.stderr[-800:]!r}", result.returncode == 0)
        ctx.check(f"the follow-up reply reached stdout, got {result.stdout!r}", "ran it" in result.stdout)
        ctx.check("the repair call offered no tools", not seen.get("repair_tools"))
        ctx.check(f"the repair call carried response_format, got {seen.get('repair_format')!r}",
                  seen.get("repair_format") is not None)
        tool_msgs = seen.get("followup_tool_msgs") or []
        ctx.check(f"the follow-up carries a successful tool result, got {tool_msgs!r}", len(tool_msgs) == 1)
    finally:
        mock.stop()


if __name__ == "__main__":
    ctx = Ctx()
    results, passed, failed, skipped = run_all(TESTS, ctx)
    sys.exit(print_results(results, passed, failed, skipped))
