"""tests.test_ollama_tool_repair_5b2 -- Halo 2.0.3 round 5b part 2 (brief
item 2): the ONE local repair round for the `ollama` dialect, end to end
through the real CLI against `tests/helpers/mock_ollama.MockUpstream`.
Split out of `tests/test_ollama_tool_reliability_5b2.py` (item 1's own
constrained-decoding/fallback tests) to keep each file within the house
250-line-per-write habit.
"""
from __future__ import annotations

import json as _json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.helpers.runner import Ctx, new_registry, print_results, run_all
from tests.helpers.fake_home import build_fake_home
from tests.helpers.mock_ollama import MockUpstream, ScriptedByCallCount, _finish_chat

REPO_DIR = Path(__file__).resolve().parent.parent
test, TESTS = new_registry()


def _hermetic_child_env() -> dict:
    env = dict(os.environ)
    env.pop("BRIDGE_STATE_DIR", None)
    for k in [k for k in env if k.startswith("HALO_")]:
        env.pop(k, None)
    env.pop("OLLAMA_API_KEY", None)
    return env


def _run_cli(fh, mock, prompt, *, model: str, extra_args=None, timeout=30):
    env = _hermetic_child_env()
    env.update({"BRIDGE_TEST_HOME": str(fh["home"]), "BRIDGE_TEST_NO_BACKGROUND_NET": "1",
                "OLLAMA_HOST": mock.base_url, "PYTHONPATH": str(REPO_DIR)})
    args = [sys.executable, "-m", "halo_harness", "-p", prompt, "--model", model,
            "--cwd", str(fh["proj"])] + (extra_args or [])
    return subprocess.run(args, env=env, cwd=str(REPO_DIR), capture_output=True, text=True, timeout=timeout)


def _chat_requests(mock) -> list:
    return [r for r in mock.requests if r["method"] == "POST" and r["path"].rstrip("/") == "/api/chat"]


# Never `ctx.check` from inside a scenario callback anywhere in this file
# -- it runs on the mock server's own handler thread, and a failed check
# there raises INSIDE that thread (swallowed by mock_ollama's own `except
# Exception: traceback.print_exc()`), leaving the HTTP response never
# sent and the CLIENT subprocess hanging until its own timeout. Every
# callback below only ever RECORDS into a plain dict; every `ctx.check`
# runs on the test's own thread, after the subprocess has exited.

@test
def test_repair_round_fixes_malformed_json_args(ctx: Ctx):
    fh = build_fake_home()
    mock = MockUpstream().start()
    seen: dict = {}

    def _malformed(h, body):
        _finish_chat(h, [{"message": {"role": "assistant", "content": "", "tool_calls": [
            {"function": {"name": "Bash", "arguments": "{not valid json"}}]},
            "done": True, "done_reason": "stop", "prompt_eval_count": 20, "eval_count": 6}])

    def _repair_reply(h, body):
        seen["repair_format"] = body.get("format")
        seen["repair_tools"] = body.get("tools")
        _finish_chat(h, [{"message": {"role": "assistant", "content": '{"command": "echo hi"}'},
                           "done": True, "done_reason": "stop", "prompt_eval_count": 5, "eval_count": 5}])

    def _followup(h, body):
        tool_msgs = [m for m in (body.get("messages") or []) if m.get("role") == "tool"]
        seen["followup_tool_msgs"] = tool_msgs
        _finish_chat(h, [{"message": {"role": "assistant", "content": "ran it"}, "done": True,
                           "done_reason": "stop", "prompt_eval_count": 5, "eval_count": 2}])

    try:
        mock.scenarios["repair-malformed"] = ScriptedByCallCount([_malformed, _repair_reply, _followup])
        result = _run_cli(fh, mock, "run echo hi", model="ol:repair-malformed",
                           extra_args=["--permission-mode", "auto"], timeout=40)
        ctx.check(f"exit 0, got {result.returncode} stderr={result.stderr[-800:]!r}", result.returncode == 0)
        ctx.check(f"the follow-up reply reached stdout, got {result.stdout!r}", "ran it" in result.stdout)
        ctx.check("exactly 3 /api/chat calls (original, repair, follow-up)", len(_chat_requests(mock)) == 3)
        ctx.check("the repair call carried format (the tool's own schema)", seen.get("repair_format") is not None)
        ctx.check("the repair call offered no tools", not seen.get("repair_tools"))
        ctx.check(f"the follow-up carried a tool result (dispatch succeeded), got {seen.get('followup_tool_msgs')!r}",
                  len(seen.get("followup_tool_msgs") or []) == 1)
    finally:
        mock.stop()


@test
def test_repair_round_gives_up_after_one_failure_surfaces_plain_error(ctx: Ctx):
    fh = build_fake_home()
    mock = MockUpstream().start()
    seen: dict = {}

    def _malformed(h, body):
        _finish_chat(h, [{"message": {"role": "assistant", "content": "", "tool_calls": [
            {"function": {"name": "Bash", "arguments": "{still not valid"}}]},
            "done": True, "done_reason": "stop", "prompt_eval_count": 20, "eval_count": 6}])

    def _repair_still_bad(h, body):
        # The repair attempt ITSELF replies with non-JSON text -- a second
        # failure, which must surface the ordinary plain error rather than
        # retrying again.
        _finish_chat(h, [{"message": {"role": "assistant", "content": "I cannot help with that."},
                           "done": True, "done_reason": "stop", "prompt_eval_count": 5, "eval_count": 5}])

    def _followup_after_error(h, body):
        tool_msgs = [m for m in (body.get("messages") or []) if m.get("role") == "tool"]
        seen["followup_tool_msgs"] = tool_msgs
        _finish_chat(h, [{"message": {"role": "assistant", "content": "got the error"}, "done": True,
                           "done_reason": "stop", "prompt_eval_count": 5, "eval_count": 2}])

    try:
        mock.scenarios["repair-fails"] = ScriptedByCallCount([_malformed, _repair_still_bad, _followup_after_error])
        result = _run_cli(fh, mock, "run echo hi", model="ol:repair-fails",
                           extra_args=["--permission-mode", "auto"], timeout=40)
        ctx.check(f"exit 0, got {result.returncode} stderr={result.stderr[-800:]!r}", result.returncode == 0)
        ctx.check(f"the plain-error follow-up reached stdout, got {result.stdout!r}",
                  "got the error" in result.stdout)
        ctx.check("exactly ONE repair attempt (3 calls: original, repair, follow-up -- never a second repair)",
                  len(_chat_requests(mock)) == 3)
        tool_msgs = seen.get("followup_tool_msgs") or []
        ctx.check("the follow-up turn's tool result is an error, naming the invalid JSON",
                  len(tool_msgs) == 1 and "not valid JSON" in str(tool_msgs[0].get("content", "")))
    finally:
        mock.stop()


@test
def test_repair_round_fixes_missing_required_argument(ctx: Ctx):
    """`invalid_args` (`agent.repair.validate_and_coerce`'s own "missing
    required parameter" case), not a JSON parse failure -- Read's schema
    requires `file_path`; an empty-object call is syntactically valid
    JSON but fails schema validation, the OTHER repairable case brief
    item 2 names."""
    fh = build_fake_home()
    note_path = fh["proj"] / "note.txt"
    note_path.write_text("MARKER_MISSING_ARG\n", encoding="utf-8")
    mock = MockUpstream().start()
    seen: dict = {}

    def _missing_arg(h, body):
        _finish_chat(h, [{"message": {"role": "assistant", "content": "", "tool_calls": [
            {"function": {"name": "Read", "arguments": {}}}]},
            "done": True, "done_reason": "stop", "prompt_eval_count": 20, "eval_count": 6}])

    def _repair_reply(h, body):
        seen["repair_tools"] = body.get("tools")
        # json.dumps, not an f-string: note_path is a real Windows path
        # with backslashes that must be JSON-escaped, exactly the kind of
        # path a real model's own repaired reply would also have to
        # escape correctly.
        content = _json.dumps({"file_path": str(note_path)})
        _finish_chat(h, [{"message": {"role": "assistant", "content": content}, "done": True,
                           "done_reason": "stop", "prompt_eval_count": 5, "eval_count": 5}])

    def _followup(h, body):
        tool_msgs = [m for m in (body.get("messages") or []) if m.get("role") == "tool"]
        seen["followup_tool_msgs"] = tool_msgs
        _finish_chat(h, [{"message": {"role": "assistant", "content": "read it"}, "done": True,
                           "done_reason": "stop", "prompt_eval_count": 5, "eval_count": 2}])

    try:
        mock.scenarios["repair-missing-arg"] = ScriptedByCallCount([_missing_arg, _repair_reply, _followup])
        result = _run_cli(fh, mock, "read the note file", model="ol:repair-missing-arg",
                           extra_args=["--permission-mode", "auto"], timeout=40)
        ctx.check(f"exit 0, got {result.returncode} stderr={result.stderr[-800:]!r}", result.returncode == 0)
        ctx.check(f"stdout shows the follow-up, got {result.stdout!r}", "read it" in result.stdout)
        ctx.check("the repair call offered no tools", not seen.get("repair_tools"))
        tool_msgs = seen.get("followup_tool_msgs") or []
        ctx.check(f"the repaired Read actually ran against the real file, got {tool_msgs!r}",
                  len(tool_msgs) == 1 and "MARKER_MISSING_ARG" in str(tool_msgs[0].get("content", "")))
    finally:
        mock.stop()


if __name__ == "__main__":
    ctx = Ctx()
    results, passed, failed, skipped = run_all(TESTS, ctx)
    sys.exit(print_results(results, passed, failed, skipped))
