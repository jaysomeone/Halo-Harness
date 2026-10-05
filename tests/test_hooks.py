"""tests.test_hooks -- H2 must-do 3: one test per named code-branch hook
(providers/hooks.py) -- reasoning_echo, tool_id_normalize, system_normalize,
leak_parser, think_tag_strip, args_repair, stream_aggregate, max_tokens_budget,
overflow_classifier, loop_guards, host_allowlist -- proving each is a REAL,
independently-testable function, not the empty `run_code_branch` registry
that predated H2.
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.helpers.runner import Ctx, new_registry, print_results, run_all
from halo_harness.providers import hooks
from halo_harness.providers.profiles import resolve_profile
from halo_harness.providers.routing import Route

test, TESTS = new_registry()


def _profile(host, model, dialect="openai-chat"):
    return resolve_profile(Route(provider=host, upstream_model=model, dialect=dialect))


# ---- 1. reasoning_echo -----------------------------------------------------

@test
def test_reasoning_echo_text_mode_injects_empty_string_when_absent(ctx: Ctx):
    profile = _profile("databricks", "databricks-deepseek-v4-1-flash")
    oai_messages = [{"role": "assistant", "content": "a"}, {"role": "assistant", "content": "b"}]
    messages = [
        {"role": "assistant", "content": [], "reasoning": {"text": "real reasoning", "details": None}},
        {"role": "assistant", "content": []},  # no reasoning captured this turn
    ]
    hooks.reasoning_echo(oai_messages, messages, profile, tools_present=True)
    ctx.check("first proto gets the real text", oai_messages[0]["reasoning_content"] == "real reasoning")
    ctx.check('second proto gets "" (not omitted, not the first turn\'s leftover)', oai_messages[1]["reasoning_content"] == "")


@test
def test_reasoning_echo_dual_field_sends_both(ctx: Ctx):
    profile = _profile("openrouter", "deepseek/deepseek-v4.1-flash")
    ctx.check("row is dual-field", profile.reasoning_dual_field is True)
    details = [{"type": "reasoning.text", "text": "x"}]
    oai_messages = [{"role": "assistant", "content": "a"}]
    messages = [{"role": "assistant", "content": [], "reasoning": {"text": "plain text", "details": details}}]
    hooks.reasoning_echo(oai_messages, messages, profile, tools_present=True)
    ctx.check("reasoning_details verbatim", oai_messages[0]["reasoning_details"] is details)
    ctx.check("reasoning_content ALSO present", oai_messages[0]["reasoning_content"] == "plain text")


@test
def test_reasoning_echo_empty_mode_never_replays(ctx: Ctx):
    profile = _profile("openrouter", "qwen/qwen3-235b-a22b-thinking-2507")
    ctx.check("row replay is empty", profile.reasoning_replay == "empty")
    oai_messages = [{"role": "assistant", "content": "a"}]
    messages = [{"role": "assistant", "content": [], "reasoning": {"text": "should never appear", "details": None}}]
    hooks.reasoning_echo(oai_messages, messages, profile, tools_present=True)
    ctx.check("no reasoning field of any kind injected", "reasoning_content" not in oai_messages[0] and "reasoning_details" not in oai_messages[0])


# ---- 2. tool_id_normalize (normalize_tool_id) ------------------------------

@test
def test_normalize_tool_id_preserve_and_mint(ctx: Ctx):
    ctx.check("preserve: verbatim", hooks.normalize_tool_id("call_abc", name="Read", tool_id_format="preserve", counter=[0]) == "call_abc")
    ctx.check("preserve: empty in -> empty out (caller mints)", hooks.normalize_tool_id(None, name="Read", tool_id_format="preserve", counter=[0]) == "")
    ctx.check("mint: ALWAYS empty regardless of raw_id (the proxy's own default)",
              hooks.normalize_tool_id("call_abc", name="Read", tool_id_format="mint", counter=[0]) == "")


@test
def test_normalize_tool_id_alnum9_mistral(ctx: Ctx):
    got = hooks.normalize_tool_id("2968-LWy3uasib", name="f", tool_id_format="alnum9", counter=[0])
    ctx.check(f"exactly 9 chars, alnum only, got {got!r}", len(got) == 9 and got.isalnum())
    ctx.check("matches OpenCode's own scrub for this exact input", got == "2968LWy3u")


@test
def test_normalize_tool_id_kimi_preserves_native_renames_else(ctx: Ctx):
    native = hooks.normalize_tool_id("functions.Read:0", name="Read", tool_id_format="kimi_functions_idx", counter=[5])
    ctx.check("already-native id left untouched (verbatim)", native == "functions.Read:0")
    counter = [0]
    renamed = hooks.normalize_tool_id("toolu_deadbeef", name="Read", tool_id_format="kimi_functions_idx", counter=counter)
    ctx.check(f"non-native id renamed to the kimi shape, got {renamed!r}", renamed == "functions.Read:0")
    ctx.check("counter advanced", counter[0] == 1)


# ---- 3. system_normalize ---------------------------------------------------

@test
def test_system_normalize_folds_into_first_user_for_gemma(ctx: Ctx):
    profile = _profile("openrouter", "google/gemma-3-27b-it")
    ctx.check("row folds system into the first user turn", profile.system_placement == "fold_into_first_user")
    messages = [{"role": "user", "content": [{"type": "text", "text": "hello"}]}]
    new_system, new_messages = hooks.system_normalize("THE SYSTEM TEXT", messages, profile)
    ctx.check("system_text emptied", new_system == "")
    ctx.check("folded into the first user message", any(
        b.get("text") == "THE SYSTEM TEXT" for b in new_messages[0]["content"] if isinstance(b, dict)))


@test
def test_system_normalize_no_op_for_ordinary_rows(ctx: Ctx):
    profile = _profile("openrouter", "deepseek/deepseek-v4.1-flash")
    ctx.check("ordinary row keeps system_placement=first", profile.system_placement == "first")
    messages = [{"role": "user", "content": [{"type": "text", "text": "hi"}]}]
    new_system, new_messages = hooks.system_normalize("SYS", messages, profile)
    ctx.check("system text unchanged", new_system == "SYS")
    ctx.check("messages unchanged (same object)", new_messages is messages)


# ---- 4/5/6. leak_parser / think_tag_strip / args_repair --------------------

@test
def test_leak_parser_hermes_tool_call(ctx: Ctx):
    profile = _profile("openrouter", "qwen/qwen3-coder")  # tool_leak_patterns includes hermes_tool_call
    ctx.check("row has hermes_tool_call in its patterns", "hermes_tool_call" in profile.tool_leak_patterns)
    text = 'sure, calling it now\n<tool_call>{"name": "Read", "arguments": {"file_path": "a.py"}}</tool_call>'
    got = hooks.leak_parser(text, profile)
    ctx.check(f"parsed a name+arguments dict, got {got}", got is not None and got["name"] == "Read")
    ctx.check("arguments parsed", got["arguments"] == {"file_path": "a.py"})
    ctx.check("no match on ordinary prose", hooks.leak_parser("just a normal reply", profile) is None)


@test
def test_leak_parser_glm_arg_key(ctx: Ctx):
    profile = _profile("openrouter", "z-ai/glm-4.6")
    ctx.check("row has glm_arg_key in its patterns", "glm_arg_key" in profile.tool_leak_patterns)
    text = "<tool_call>Read<arg_key>file_path</arg_key><arg_value>a.py</arg_value></tool_call>"
    got = hooks.leak_parser(text, profile)
    ctx.check(f"parsed name Read, got {got}", got is not None and got["name"] == "Read")
    ctx.check("arguments parsed from arg_key/arg_value pairs", got["arguments"] == {"file_path": "a.py"})


@test
def test_leak_parser_missing_tool_call_opener(ctx: Ctx):
    """Halo 2.0.2 round 5 (Qwen-at-work brief, item 3): declared by every
    Qwen row (model_table.json) but never implemented until now --
    Qwen3-Coder #475's own shape, a bare JSON call with no OPENING
    `<tool_call>` tag after a prose lead-in, closed by `</tool_call>`."""
    profile = _profile("openrouter", "qwen/qwen3-coder")
    ctx.check("row declares missing_tool_call_opener", "missing_tool_call_opener" in profile.tool_leak_patterns)
    text = 'Sure, reading it now:\n{"name": "Read", "arguments": {"file_path": "a.py"}}</tool_call>'
    got = hooks.leak_parser(text, profile)
    ctx.check(f"parsed despite the missing opener, got {got}", got is not None and got["name"] == "Read")
    ctx.check("arguments parsed", got["arguments"] == {"file_path": "a.py"})


@test
def test_missing_tool_call_opener_is_fast_on_a_long_brace_heavy_answer(ctx: Ctx):
    """2.0.2 review finding 35 pin: `(\\{.*?\\})\\s*</tool_call>` tried
    every single `{` in the text as a candidate match start when there
    was no `</tool_call>` anywhere at all -- O(n x braces), measured at
    0.77s for 68KB and 4.8s for 170KB of code-like text, and this runs
    at the end of EVERY tool-less Qwen turn. A 200KB brace-heavy answer
    with no `</tool_call>` must now finish in well under a second (the
    fix anchors the opening brace to the real shape, `{"name"...`, which
    a plain code snippet essentially never starts with)."""
    profile = _profile("openrouter", "qwen/qwen3-coder")
    chunk = 'def f(x):\n    return {"a": x, "b": {"c": 1, "d": [1, 2, 3]}}\n'
    text = chunk * (200_000 // len(chunk))  # ~200KB, brace-heavy, no </tool_call> anywhere
    ctx.check("no </tool_call> in the benchmark text (the pathological case)", "</tool_call>" not in text)
    t0 = time.monotonic()
    got = hooks.leak_parser(text, profile)
    elapsed = time.monotonic() - t0
    ctx.check(f"finishes in well under a second, got {elapsed:.3f}s", elapsed < 1.0)
    ctx.check("no leak found (there genuinely isn't one)", got is None)


@test
def test_leak_parser_python_repr_args(ctx: Ctx):
    """Declared by every Qwen row but never implemented until now --
    agno#10231's own shape: a BARE Python-dict-literal call, single quotes
    and Python True/False/None, no `<tool_call>`/fence wrapper at all."""
    profile = _profile("openrouter", "qwen/qwen3-coder")
    ctx.check("row declares python_repr_args", "python_repr_args" in profile.tool_leak_patterns)
    text = "{'name': 'Read', 'arguments': {'file_path': 'x', 'ok': True}}"
    got = hooks.leak_parser(text, profile)
    ctx.check(f"parsed the bare repr dict, got {got}", got is not None and got["name"] == "Read")
    ctx.check("single-quoted + Python True decoded correctly",
              got["arguments"] == {"file_path": "x", "ok": True})
    ctx.check("a bare dict with no real argument-carrying key is NOT promoted",
              hooks.leak_parser("{'name': 'my-cli', 'version': '1.0'}", profile) is None)


@test
def test_think_tag_strip_matches_the_old_scope_c_function(ctx: Ctx):
    from halo_harness.providers.oai_stream import strip_display_artifacts
    ctx.check("re-exported under the old name, same behavior",
              hooks.think_tag_strip is strip_display_artifacts)
    ctx.check("strips a leading think block", hooks.think_tag_strip("<think>x</think>answer") == "answer")


@test
def test_think_tag_strip_bare_unpaired_closer(ctx: Ctx):
    """Halo 2.0.2 round 5 (Qwen-at-work brief): Qwen3-235B-Thinking-2507
    and QwQ's own model cards document replayed history may contain only
    a closing `</think>` with no opening tag -- the old regex only matched
    a PAIRED block and leaked this straight into displayed text."""
    ctx.check("bare leading closer stripped", hooks.think_tag_strip("</think>the final answer") == "the final answer")
    ctx.check("a PAIRED block is still handled exactly as before (no double-strip)",
              hooks.think_tag_strip("<think>reasoning</think>the answer") == "the answer")
    ctx.check("ordinary text with no think markup at all is untouched",
              hooks.think_tag_strip("just a plain reply") == "just a plain reply")


@test
def test_args_repair_trailing_comma_and_python_literals(ctx: Ctx):
    got = hooks.args_repair('{"a": 1, "b": True, "c": None,}')
    ctx.check(f"trailing comma + Python literals repaired, got {got}", got == {"a": 1, "b": True, "c": None})
    ctx.check("unrepairable garbage returns None, never raises", hooks.args_repair("not json at all {{{") is None)
    ctx.check("empty string returns None", hooks.args_repair("") is None)


# ---- 7. stream_aggregate ----------------------------------------------------

@test
def test_stream_aggregate_key_indexless_delta_continues_current_call(ctx: Ctx):
    """finding 11 rule 3: a GLM-style id change on an index-less delta
    must continue the CURRENT call, never open a new one."""
    id_to_key, next_auto, last_key = {}, [0], [None]
    k1 = hooks.stream_aggregate_key(0, "chatcmpl-tool-abc", id_to_key=id_to_key, next_auto=next_auto, last_key=last_key)
    k2 = hooks.stream_aggregate_key(None, "toolcall0", id_to_key=id_to_key, next_auto=next_auto, last_key=last_key)
    ctx.check(f"index-less delta with a DIFFERENT id still keys to the SAME call, got {k1!r} vs {k2!r}", k1 == k2)
    k3 = hooks.stream_aggregate_key(1, "call_b", id_to_key=id_to_key, next_auto=next_auto, last_key=last_key)
    ctx.check("a genuinely NEW index opens a new key", k3 != k1)


@test
def test_stream_aggregate_apply_null_safe(ctx: Ctx):
    """finding 11 rules 1-2: null name never overwrites a captured one;
    null arguments never raises (treated as no-op, not TypeError)."""
    buf = {"name": None, "args": "", "raw_id": None}
    hooks.stream_aggregate_apply(buf, {"name": "Read", "arguments": '{"a":'})
    ctx.check("name captured", buf["name"] == "Read")
    ctx.check("args accumulated", buf["args"] == '{"a":')
    n = hooks.stream_aggregate_apply(buf, {"name": None, "arguments": None})
    ctx.check("null name did not clobber the real one", buf["name"] == "Read")
    ctx.check("null arguments treated as a no-op, never raised", buf["args"] == '{"a":' and n == 0)


# ---- 8. max_tokens_budget + overflow_classifier ----------------------------

@test
def test_max_tokens_budget_no_op_without_rate_limits(ctx: Ctx):
    profile = _profile("openrouter", "deepseek/deepseek-v4.1-flash")
    ctx.check("openrouter rows carry no databricks_rate_limits", profile.databricks_rate_limits is None)
    got = hooks.max_tokens_budget(profile, model_key="test/no-rl", requested=None)
    ctx.check(f"falls straight through to min(default, cap), got {got}",
              got == max(1, min(profile.max_tokens_default or profile.max_tokens_cap, profile.max_tokens_cap)))


@test
def test_max_tokens_budget_rolling_otpm_window(ctx: Ctx):
    """finding 6: the SECOND call within the same 60s window must get a
    budget reduced by what the FIRST call already spent, not the whole
    OTPM handed out twice in a row (which pre-admits a 429). Below the
    floor, the function WAITS for the window to free up rather than
    returning a near-zero budget immediately -- proven here with a short
    `abort` so the test doesn't have to sit through the real 60s window
    (which would let the just-recorded usage age back out and legitimately
    recover the budget, a DIFFERENT true behavior this test isn't about)."""
    import threading
    hooks.reset_databricks_otpm_history()
    profile = _profile("databricks", "databricks-deepseek-v4-pro-0813")
    ctx.check("OTPM present for this row", profile.databricks_rate_limits.get("otpm") == 4000)
    key = "test/otpm-window"
    first = hooks.max_tokens_budget(profile, model_key=key, requested=None)
    ctx.check(f"first call gets close to the full OTPM budget, got {first}", first > 3000)
    hooks.record_databricks_output_tokens(key, 3500)

    abort = threading.Event()
    threading.Timer(1.0, abort.set).start()
    t0 = time.monotonic()
    second = hooks.max_tokens_budget(profile, model_key=key, requested=None, abort=abort)
    dt = time.monotonic() - t0
    ctx.check(f"waited (didn't return the stale/full budget immediately), got dt={dt:.1f}s", dt >= 0.9)
    ctx.check(f"second call's budget reflects what was just spent, got {second} (first was {first})",
              second < first - 3000)
    hooks.reset_databricks_otpm_history()


@test
def test_overflow_classifier_matches_the_errors_module(ctx: Ctx):
    from halo_harness.providers.errors import CONTEXT_WINDOW_EXCEEDED, classify_error_category
    msg = "This model's maximum context length is 128000 tokens. However, you requested 130000 tokens."
    ctx.check("delegates to classify_error_category, same answer",
              hooks.overflow_classifier(400, msg) == classify_error_category(400, msg) == CONTEXT_WINDOW_EXCEEDED)


# ---- 9. loop_guards ---------------------------------------------------------

@test
def test_loop_guards_empty_completion_detection(ctx: Ctx):
    ctx.check("empty text, no tool_use, tools WERE present -> retryable",
              hooks.is_retryable_empty_completion(stop_reason="end_turn", text="", tool_use_count=0, tools_present=True) is True)
    ctx.check("empty text but no tools offered -> NOT flagged (nothing to call)",
              hooks.is_retryable_empty_completion(stop_reason="end_turn", text="", tool_use_count=0, tools_present=False) is False)
    ctx.check("real text present -> not empty",
              hooks.is_retryable_empty_completion(stop_reason="end_turn", text="an answer", tool_use_count=0, tools_present=True) is False)
    ctx.check("a genuine tool_use turn is never flagged as empty",
              hooks.is_retryable_empty_completion(stop_reason="tool_use", text="", tool_use_count=1, tools_present=True) is False)


@test
def test_loop_guards_length_vs_malformed_classification(ctx: Ctx):
    ctx.check("truncated_by_length -> 'length'", hooks.classify_length_tool_call({"truncated_by_length": True}) == "length")
    ctx.check("malformed_json -> 'malformed'", hooks.classify_length_tool_call({"malformed_json": True}) == "malformed")
    ctx.check("neither flag -> 'ok'", hooks.classify_length_tool_call({}) == "ok")


# ---- 10. host_allowlist -----------------------------------------------------

@test
def test_host_allowlist_merges_pin_and_forces_require_parameters(ctx: Ctx):
    profile = _profile("openrouter", "deepseek/deepseek-v4.1-flash")
    # the ingestion script merges the row's informal fallback_order into
    # order (with allow_fallbacks forced true) -- see
    # tools/ingest_model_table.py's compile_openrouter_pin, verified live
    # against the owner's own OpenRouter account (Guardrails/ZDR settings
    # otherwise hard-fail the primary-only, no-fallback pin outright).
    pin_order = profile.openrouter_pin.get("order")
    ctx.check(f"row has a pin, primary first, got {pin_order}", pin_order[0] == "deepseek" and len(pin_order) > 1)
    body = {}
    hooks.host_allowlist(body, profile, tools_present=True)
    ctx.check(f"pin merged into provider, got {body.get('provider')}", body["provider"].get("order") == pin_order)
    ctx.check("require_parameters forced true when tools are present", body["provider"].get("require_parameters") is True)

    body_no_tools = {}
    hooks.host_allowlist(body_no_tools, profile, tools_present=False)
    ctx.check("pin STILL merged even with no tools (every OpenRouter request, per finding 5)",
              body_no_tools["provider"].get("order") == pin_order)
    ctx.check("require_parameters NOT forced when no tools are sent", "require_parameters" not in body_no_tools["provider"])


@test
def test_host_allowlist_no_op_for_databricks(ctx: Ctx):
    profile = _profile("databricks", "databricks-deepseek-v4-1-flash")
    body = {}
    hooks.host_allowlist(body, profile, tools_present=True)
    ctx.check("no provider field added for a non-OpenRouter profile", "provider" not in body)


if __name__ == "__main__":
    ctx = Ctx()
    results, passed, failed, skipped = run_all(TESTS, ctx)
    sys.exit(print_results(results, passed, failed, skipped))
