"""tests.test_w5_prompt_suggestions -- W5 (carried from W4a): `--prompt-
suggestions` now also works in the `--input-format stream-json` multi-turn
loop (previously only the single-turn `-p` text path, left as a follow-up:
that loop's own per-turn `sink.consume()` had no `finish=False` gap to slot
the extra small-model call into). Pins `headless.run_print_mode`'s own
stream-json loop end to end against the mock upstream.
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

from tests.helpers.runner import Ctx, new_registry, print_results, run_all
from tests.helpers.fake_home import build_fake_home
from tests.helpers.mock_openai import MockUpstream, SCENARIOS, _finish
from tests.helpers.provider_env_defaults import ensure_default_provider_credentials

ensure_default_provider_credentials()
test, TESTS = new_registry()

_SUGGESTION_TEXT = "try asking about the next step"


def _scn_reply_or_suggest(h, body):
    """Answers the REAL turn with a plain reply; answers the small-model
    prompt-suggestion call (its own distinct system prompt, see
    `headless._maybe_add_prompt_suggestion`) with a fixed, recognisable
    suggestion string instead."""
    as_text = json.dumps(body)
    if "predict the user" in as_text:
        _finish(h, [
            {"choices": [{"index": 0, "delta": {"role": "assistant"}}]},
            {"choices": [{"index": 0, "delta": {"content": _SUGGESTION_TEXT}}]},
            {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
        ])
    else:
        _finish(h, [
            {"choices": [{"index": 0, "delta": {"role": "assistant"}}]},
            {"choices": [{"index": 0, "delta": {"content": "the real answer"}}]},
            {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
        ])


def _run_stream_json_turn(*, prompt_suggestions: bool):
    from halo_harness.headless import run_print_mode

    fh = build_fake_home()
    os.environ["BRIDGE_TEST_HOME"] = str(fh["home"])
    mock = MockUpstream().start()
    SCENARIOS["w5-suggest"] = _scn_reply_or_suggest
    out = io.StringIO()
    try:
        os.environ["BRIDGE_OPENROUTER_BASE_URL"] = mock.base_url
        with contextlib.redirect_stdout(out):
            code = run_print_mode(
                cwd=fh["proj"], model_ref_raw="or:mock/w5-suggest",
                input_format="stream-json", output_format="stream-json", max_turns=4,
                stdin_lines=[{"type": "user", "message": {"role": "user", "content": "hi"}}],
                cli_flags={"prompt_suggestions": prompt_suggestions},
            )
    finally:
        mock.stop()
        os.environ.pop("BRIDGE_OPENROUTER_BASE_URL", None)
        os.environ.pop("BRIDGE_TEST_HOME", None)
    lines = []
    for raw_line in out.getvalue().splitlines():
        raw_line = raw_line.strip()
        if not raw_line:
            continue
        try:
            lines.append(json.loads(raw_line))
        except ValueError:
            continue
    return code, lines


@test
def test_prompt_suggestions_flag_adds_a_suggestion_line_in_the_stream_json_loop(ctx: Ctx):
    code, lines = _run_stream_json_turn(prompt_suggestions=True)
    ctx.check(f"exit 0, got {code}", code == 0)
    suggestion_lines = [L for L in lines if L.get("type") == "prompt_suggestion"]
    ctx.check(f"exactly one prompt_suggestion line, got {[L.get('type') for L in lines]}",
              len(suggestion_lines) == 1)
    ctx.check(f"carries the predicted text, got {suggestion_lines[0]}",
              suggestion_lines[0].get("suggestion") == _SUGGESTION_TEXT)
    result_lines = [L for L in lines if L.get("type") == "result"]
    ctx.check(f"the result line ALSO carries it (plain json callers never see stream-json's own "
              f"extra message types), got {result_lines}",
              result_lines and result_lines[-1].get("prompt_suggestion") == _SUGGESTION_TEXT)
    ctx.check(f"the real turn's own answer still came through untouched, got {result_lines}",
              result_lines and result_lines[-1].get("result") == "the real answer")


@test
def test_no_prompt_suggestions_flag_means_no_suggestion_line_token_thrift(ctx: Ctx):
    code, lines = _run_stream_json_turn(prompt_suggestions=False)
    ctx.check(f"exit 0, got {code}", code == 0)
    ctx.check(f"never a prompt_suggestion line when the flag is off, got {[L.get('type') for L in lines]}",
              not any(L.get("type") == "prompt_suggestion" for L in lines))
    result_lines = [L for L in lines if L.get("type") == "result"]
    ctx.check(f"still gets the real turn's own answer, got {result_lines}",
              result_lines and result_lines[-1].get("result") == "the real answer")


if __name__ == "__main__":
    ctx = Ctx()
    results, passed, failed, skipped = run_all(TESTS, ctx)
    sys.exit(print_results(results, passed, failed, skipped))
