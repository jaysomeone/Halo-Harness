"""tests.test_ollama_tool_reliability_5b2 -- Halo 2.0.3 round 5b part 2
(brief item 1, as corrected by the 2026-10-04 fix pass) + the NEW
identical-call loop guard, end to end through the real CLI against
`tests/helpers/mock_ollama.MockUpstream` -- same runner style as
`tests/test_providers_ollama_session.py`. The local repair round (brief
item 2, unaffected by the fix pass) is its own sibling file,
`tests/test_ollama_tool_repair_5b2.py`.

Fix pass: an ordinary turn -- including right after a tool result -- is
NEVER constrained any more (a live run showed forcing the tool-call shape
onto every such turn leaves the model unable to answer in prose once it
runs out of anything useful to call, looping for 25 minutes). The old
"format present after a tool result"/"fallback on 400" tests are gone
with the mechanism they tested; replaced by a "never constrained" pin and
the new loop guard's own pin.
"""
from __future__ import annotations

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


# ---- item 1 (corrected): never constrained, not even post-tool-result --

@test
def test_no_format_on_first_turn_even_with_tools(ctx: Ctx):
    fh = build_fake_home()
    mock = MockUpstream().start()
    try:
        result = _run_cli(fh, mock, "say hi please", model="ol:plain-text")
        ctx.check(f"exit 0, got {result.returncode}", result.returncode == 0)
        chats = _chat_requests(mock)
        ctx.check(f"one call, got {len(chats)}", len(chats) == 1)
        ctx.check(f"no format on the first turn, got {chats[0]['body'].get('format')!r}",
                  "format" not in chats[0]["body"])
    finally:
        mock.stop()


@test
def test_no_format_right_after_a_tool_result_either(ctx: Ctx):
    """The exact live-run failure mode: a model answering freely in prose
    right after a tool result must not be forced into the tool-call
    shape -- `format` must be absent from BOTH requests."""
    fh = build_fake_home()
    note_path = fh["proj"] / "note.txt"
    note_path.write_text("MARKER\n", encoding="utf-8")
    mock = MockUpstream().start()
    try:
        mock.scenarios["fmt-turn2"] = ScriptedByCallCount([
            [{"message": {"role": "assistant", "content": "", "tool_calls": [
                {"function": {"name": "Read", "arguments": {"file_path": str(note_path)}}}]},
              "done": True, "done_reason": "stop", "prompt_eval_count": 20, "eval_count": 6}],
            [{"message": {"role": "assistant", "content": "Done!"}, "done": True, "done_reason": "stop",
              "prompt_eval_count": 10, "eval_count": 4}],
        ])
        result = _run_cli(fh, mock, "please read the note file", model="ol:fmt-turn2",
                           extra_args=["--permission-mode", "auto"])
        ctx.check(f"exit 0, got {result.returncode} stderr={result.stderr[-500:]!r}", result.returncode == 0)
        ctx.check(f"the free prose reply reached stdout, got {result.stdout!r}", "Done!" in result.stdout)
        chats = _chat_requests(mock)
        ctx.check(f"two calls, got {len(chats)}", len(chats) == 2)
        ctx.check("turn 1 (first user turn) has no format", "format" not in chats[0]["body"])
        ctx.check(f"turn 2 (right after the tool result) ALSO has no format, got "
                  f"{chats[1]['body'].get('format')!r}", "format" not in chats[1]["body"])
        ctx.check("the SAME tools list is still on the wire (native tool_calls untouched)",
                  bool(chats[1]["body"].get("tools")))
    finally:
        mock.stop()


# ---- the new identical-call loop guard ----------------------------------

@test
def test_identical_call_guard_stops_after_the_third_call_no_fourth_request(ctx: Ctx):
    """The exact live-run shape: a model stuck repeating ONE meaningless
    call turn after turn. Three identical `TaskStop` calls in a row must
    stop the turn with the plain notice; a fourth request must never
    happen."""
    fh = build_fake_home()

    def _same_call_every_time(h, body):
        _finish_chat(h, [{"message": {"role": "assistant", "content": "", "tool_calls": [
            {"function": {"name": "TaskStop", "arguments": {"task_id": "no-such-task"}}}]},
            "done": True, "done_reason": "stop", "prompt_eval_count": 20, "eval_count": 6}])

    mock = MockUpstream().start()
    try:
        mock.scenarios["loop-guard"] = _same_call_every_time
        # `--output-format json`, same as tests/test_loop_tools.py's own
        # generic-breaker test: an `end_turn` tool_result is NEVER
        # narrated in bare print mode (there is no final assistant reply
        # after it -- the turn stops right there), so the notice text
        # only shows up in the structured event stream.
        result = _run_cli(fh, mock, "finish up", model="ol:loop-guard",
                           extra_args=["--permission-mode", "auto", "--output-format", "stream-json"],
                           timeout=40)
        ctx.check(f"exit 0 or 1 (not hung/killed), got {result.returncode}", result.returncode in (0, 1))
        ctx.check(f"the plain stop notice appears in the event stream, got {result.stdout[-1200:]!r}",
                  "repeated the same tool call three times" in result.stdout
                  and "stopping this turn" in result.stdout)
        chats = _chat_requests(mock)
        ctx.check(f"exactly THREE /api/chat calls -- the loop guard stopped the turn after the third, "
                  f"never a fourth, got {len(chats)}", len(chats) == 3)
    finally:
        mock.stop()


if __name__ == "__main__":
    ctx = Ctx()
    results, passed, failed, skipped = run_all(TESTS, ctx)
    sys.exit(print_results(results, passed, failed, skipped))
