"""tests.test_providers_ollama -- Halo 2.0.3 round 2 (`ol:` provider on the
native API): the dialect/profile shape, `ol:`/`ol:<model>@<host>` model-ref
parsing, the native `/api/chat` request builder's wire-field pinning
(`options.num_ctx` every request, `keep_alive`, `think` per effort level,
tools in the OpenAI function shape), the NDJSON decoder (synthesized
tool-call ids, `done_reason` handling incl. the one-time "load" retry),
and the context-ownership rule. Catalog/capability-probe/host-config/
enablement-probe pinning tests are in
tests/test_providers_ollama_catalog.py (split per the brief's own
"<= 250 lines per Write" house habit -- see tests/test_roles.py's own
precedent for the same split).
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

test, TESTS = new_registry()


def _fresh_state_dir(prefix: str) -> Path:
    d = Path(tempfile.mkdtemp(prefix=prefix))
    os.environ["BRIDGE_STATE_DIR"] = str(d)
    return d


def _clear_state_dir_env() -> None:
    os.environ.pop("BRIDGE_STATE_DIR", None)


# ---- dialect / profile -----------------------------------------------------

@test
def test_resolve_profile_ollama_dialect_shape(ctx: Ctx):
    from halo_harness.providers.profiles import resolve_profile
    from halo_harness.providers.routing import Route
    profile = resolve_profile(Route(provider="ollama", upstream_model="qwen3:30b", dialect="ollama"))
    ctx.check("tools_supported True", profile.tools_supported is True)
    ctx.check("tool_choice_required_supported False (undocumented on the native API)",
              profile.tool_choice_required_supported is False)
    ctx.check("reasoning_effort_supported False (think has its own mapping, not map_effort)",
              profile.reasoning_effort_supported is False)
    ctx.check("family classified from the bare id", profile.family == "qwen")
    ctx.check("model_id carried through", profile.model_id == "qwen3:30b")
    # Halo 2.0.3 round 3: tools_max is no longer the permanently-unbounded
    # `None` round 2 shipped (the hand-off bug: every request carried the
    # full tool catalog) -- resolve_profile's own ollama branch now seeds
    # a real, context-class-derived default (providers.ollama_fit).
    ctx.check(f"tools_max is a real int now, got {profile.tools_max!r}", isinstance(profile.tools_max, int))


# ---- model.py: ol: / ol:<model>@<host> ref parsing -------------------------

@test
def test_parse_model_ref_ol_default_host(ctx: Ctx):
    from halo_harness.model import parse_model_ref
    ref = parse_model_ref("ol:qwen3:30b")
    ctx.check("provider ollama", ref.provider == "ollama")
    ctx.check("dialect ollama", ref.dialect == "ollama")
    ctx.check("model keeps its own embedded ':' tag", ref.model == "qwen3:30b")
    ctx.check("no @host -> host is None (default host)", ref.host is None)


@test
def test_parse_model_ref_ol_named_host(ctx: Ctx):
    """`ol:<model>@<hostname>` -- the exact syntax a LAN host or an Ollama
    Cloud entry is addressed by (research doc section 7/Q7); `partition`
    on the first "@" so a model tag's own embedded ":" never confuses the
    split."""
    from halo_harness.model import parse_model_ref
    ref = parse_model_ref("ol:qwen3:30b@lan")
    ctx.check("model", ref.model == "qwen3:30b")
    ctx.check("host", ref.host == "lan")
    ref2 = parse_model_ref("ol:gpt-oss:20b@cloud")
    ctx.check("cloud-named host", ref2.host == "cloud")
    ctx.check("model with cloud host", ref2.model == "gpt-oss:20b")


@test
def test_parse_model_ref_ol_disabled_override_refuses(ctx: Ctx):
    from halo_harness.model import parse_model_ref
    from halo_harness.providers.routing import InvalidModelError
    from halo_harness.theme import set_config_value
    _fresh_state_dir("ol-disabled-")
    try:
        set_config_value("providers", {"ollama": {"enabled": False}})
        raised = False
        try:
            parse_model_ref("ol:qwen3:30b")
        except InvalidModelError:
            raised = True
        ctx.check("an explicit providers.ollama.enabled=false refuses the ref", raised)
    finally:
        _clear_state_dir_env()


@test
def test_parse_model_ref_unknown_error_mentions_ol(ctx: Ctx):
    from halo_harness.model import parse_model_ref
    from halo_harness.providers.routing import InvalidModelError
    try:
        parse_model_ref("   not a model   ")
        ctx.check("should have raised", False)
    except InvalidModelError as e:
        ctx.check(f"error names ol: as an accepted form, got {e}", "ol:" in str(e))


# ---- request builder: wire-field pinning -----------------------------------

def _route_profile(model="qwen3:30b"):
    from halo_harness.providers.profiles import resolve_profile
    from halo_harness.providers.routing import Route
    route = Route(provider="ollama", upstream_model=model, dialect="ollama")
    return route, resolve_profile(route)


@test
def test_build_request_body_sends_num_ctx_every_request_even_unknown_trained_context(ctx: Ctx):
    from halo_harness.providers.ollama import FALLBACK_NUM_CTX, OllamaHost
    from halo_harness.providers.ollama_request import build_ollama_request_body
    route, profile = _route_profile()
    host = OllamaHost(name="default", url="http://127.0.0.1:11434")
    body = build_ollama_request_body(system_text="s", messages=[{"role": "user", "content": "hi"}],
                                      tools=None, tool_choice=None, route=route, profile=profile,
                                      effort=None, host=host)
    ctx.check("options.num_ctx present even with nothing known yet",
              body["options"]["num_ctx"] == FALLBACK_NUM_CTX)
    body2 = build_ollama_request_body(system_text="s", messages=[{"role": "user", "content": "hi"}],
                                       tools=None, tool_choice=None, route=route, profile=profile,
                                       effort=None, host=host, trained_context=40960)
    ctx.check("num_ctx reflects a known trained context", body2["options"]["num_ctx"] == 40960)


@test
def test_build_request_body_keep_alive_from_host_config(ctx: Ctx):
    from halo_harness.providers.ollama import OllamaHost
    from halo_harness.providers.ollama_request import build_ollama_request_body
    route, profile = _route_profile()
    host_default = OllamaHost(name="default", url="http://127.0.0.1:11434")
    body = build_ollama_request_body(system_text="", messages=[{"role": "user", "content": "hi"}],
                                      tools=None, tool_choice=None, route=route, profile=profile,
                                      effort=None, host=host_default)
    ctx.check("keep_alive is left out when the host config has none (the server's own setting stands)",
              "keep_alive" not in body)
    host_cfg = OllamaHost(name="lan", url="http://127.0.0.1:11434", keep_alive="30m")
    body2 = build_ollama_request_body(system_text="", messages=[{"role": "user", "content": "hi"}],
                                       tools=None, tool_choice=None, route=route, profile=profile,
                                       effort=None, host=host_cfg)
    ctx.check("host.keep_alive forwarded verbatim", body2["keep_alive"] == "30m")


@test
def test_build_request_body_think_mapping_per_effort_level(ctx: Ctx):
    from halo_harness.providers.ollama import OllamaHost
    from halo_harness.providers.ollama_request import build_ollama_request_body
    host = OllamaHost(name="default", url="http://127.0.0.1:11434")

    def think_for(model, effort):
        route, profile = _route_profile(model)
        body = build_ollama_request_body(system_text="", messages=[{"role": "user", "content": "hi"}],
                                          tools=None, tool_choice=None, route=route, profile=profile,
                                          effort=effort, host=host)
        return body.get("think", "OMITTED")

    ctx.check("no effort configured -> field omitted entirely (model default)",
              think_for("qwen3:30b", None) == "OMITTED")
    ctx.check("low effort -> think off on a bool-only model", think_for("qwen3:30b", "low") is False)
    for lvl in ("medium", "high", "xhigh", "max"):
        ctx.check(f"{lvl} effort -> think on (bool-only model)", think_for("qwen3:30b", lvl) is True)
    ctx.check("deepseek-r1 is bool-only too", think_for("deepseek-r1:7b", "high") is True)
    ctx.check("gpt-oss low -> graded 'low'", think_for("gpt-oss:20b", "low") == "low")
    ctx.check("gpt-oss medium -> graded 'medium'", think_for("gpt-oss:20b", "medium") == "medium")
    for lvl in ("high", "xhigh", "max"):
        ctx.check(f"gpt-oss {lvl} -> graded 'high'", think_for("gpt-oss:20b", lvl) == "high")


@test
def test_build_request_body_tools_in_openai_function_shape(ctx: Ctx):
    from halo_harness.providers.ollama import OllamaHost
    from halo_harness.providers.ollama_request import build_ollama_request_body
    route, profile = _route_profile()
    host = OllamaHost(name="default", url="http://127.0.0.1:11434")
    tool = {"name": "Read", "description": "read a file",
            "input_schema": {"type": "object", "properties": {"file_path": {"type": "string"}},
                              "required": ["file_path"]}}
    body = build_ollama_request_body(system_text="", messages=[{"role": "user", "content": "hi"}],
                                      tools=[tool], tool_choice=None, route=route, profile=profile,
                                      effort=None, host=host)
    wire_tool = body["tools"][0]
    ctx.check("type: function (the native API's own documented tool shape, research doc Q1)",
              wire_tool["type"] == "function")
    ctx.check("function.name", wire_tool["function"]["name"] == "Read")
    ctx.check("function.parameters is the JSON-schema object (unchanged)",
              wire_tool["function"]["parameters"]["properties"]["file_path"]["type"] == "string")
    ctx.check("no tool_choice field on the wire (undocumented on the native API)",
              "tool_choice" not in body)


# ---- end-to-end through the mock server (stream_ollama_completion) --------

def _ollama_completion_request(mock, scenario: str, *, host=None, state_dir=None, effort="medium"):
    from halo_harness.providers.ollama import OllamaHost
    from halo_harness.providers.ollama_request import build_ollama_request_body
    from halo_harness.providers.routing import Route
    from halo_harness.providers.profiles import resolve_profile
    from halo_harness.providers.stream import CompletionRequest, ProviderCreds
    route = Route(provider="ollama", upstream_model=scenario, dialect="ollama")
    profile = resolve_profile(route)
    host = host or OllamaHost(name="default", url=mock.base_url)
    body = build_ollama_request_body(system_text="sys", messages=[{"role": "user", "content": [
        {"type": "text", "text": "hi"}]}], tools=None, tool_choice=None, route=route, profile=profile,
        effort=effort, host=host, trained_context=40960)
    creds = ProviderCreds(base_url=host.url, api_key=host.api_key or "")
    return CompletionRequest(body={}, route=route, profile=profile, creds=creds,
                              state_dir=state_dir or Path(tempfile.mkdtemp(prefix="ol-stream-")),
                              extra_headers={}, model_label=f"ol:{scenario}",
                              prebuilt_ollama_body=body, ping_interval=5.0)


@test
def test_wire_num_ctx_keep_alive_think_actually_sent(ctx: Ctx):
    from tests.helpers.mock_ollama import MockUpstream
    from halo_harness.providers.stream import stream_ollama_completion
    with MockUpstream() as mock:
        req = _ollama_completion_request(mock, "echo-wire", effort="high")
        events = list(stream_ollama_completion(req))
        ctx.check("got a normal message_start..message_stop stream",
                  events[0]["type"] == "message_start" and events[-1]["type"] == "message_stop")
        req_body = mock.requests[-1]["body"]
        ctx.check(f"num_ctx on the wire, got {req_body.get('options')}", req_body["options"]["num_ctx"] == 40960)
        ctx.check(f"no keep_alive on the wire for an unconfigured host, got {req_body.get('keep_alive')!r}",
                  "keep_alive" not in req_body)
        ctx.check(f"think on the wire for high effort, got {req_body.get('think')!r}", req_body["think"] is True)


@test
def test_tool_call_ids_synthesized_stable_single_and_parallel(ctx: Ctx):
    from tests.helpers.mock_ollama import MockUpstream
    from halo_harness.providers.stream import stream_ollama_completion
    with MockUpstream() as mock:
        req = _ollama_completion_request(mock, "tool-call-single")
        events = list(stream_ollama_completion(req))
        starts = [e for e in events if e["type"] == "content_block_start" and e["content_block"]["type"] == "tool_use"]
        ctx.check("one synthesized id, toolu_-shaped", len(starts) == 1 and starts[0]["content_block"]["id"].startswith("toolu_"))

        mock.clear()
        req2 = _ollama_completion_request(mock, "tool-calls-parallel")
        events2 = list(stream_ollama_completion(req2))
        starts2 = [e for e in events2 if e["type"] == "content_block_start" and e["content_block"]["type"] == "tool_use"]
        ids2 = [b["content_block"]["id"] for b in starts2]
        ctx.check("two parallel calls, two DISTINCT synthesized ids", len(ids2) == 2 and len(set(ids2)) == 2)
        ctx.check("both stable toolu_ ids", all(i.startswith("toolu_") for i in ids2))
        names2 = [b["content_block"]["name"] for b in starts2]
        ctx.check("names preserved in index order", names2 == ["Read", "Read"])


@test
def test_tool_call_id_stable_across_multiturn_replay(ctx: Ctx):
    """"Stable across a multi-turn replay" (brief, round 2): a tool-call id
    Halo synthesized in turn 1, once stored in the derived transcript, must
    survive being replayed into turn 2's OUTGOING request without the wire
    ever needing that id back -- Ollama's own replay shape keys off
    `tool_name` alone (research doc Q1), so round-tripping it must never
    require a second code path (or silently drop/rename the tool) just
    because this provider never produced that id itself."""
    from tests.helpers.mock_ollama import MockUpstream
    from halo_harness.providers.ollama import OllamaHost
    from halo_harness.providers.ollama_request import build_ollama_request_body
    from halo_harness.providers.stream import stream_ollama_completion
    with MockUpstream() as mock:
        req = _ollama_completion_request(mock, "tool-call-single")
        events = list(stream_ollama_completion(req))
        tool_block = [e["content_block"] for e in events
                      if e["type"] == "content_block_start" and e["content_block"]["type"] == "tool_use"][0]
        synthesized_id = tool_block["id"]

        route, profile = _route_profile("tool-call-single")
        host = OllamaHost(name="default", url=mock.base_url)
        history = [
            {"role": "user", "content": [{"type": "text", "text": "read a.txt"}]},
            {"role": "assistant", "content": [
                {"type": "tool_use", "id": synthesized_id, "name": "Read", "input": {"file_path": "a.txt"}}]},
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": synthesized_id,
                 "content": [{"type": "text", "text": "file contents"}]}]},
        ]
        body2 = build_ollama_request_body(system_text="sys", messages=history, tools=None, tool_choice=None,
                                           route=route, profile=profile, effort=None, host=host)
        tool_msg = [m for m in body2["messages"] if m["role"] == "tool"][0]
        ctx.check(f"replay keys off tool_name, got {tool_msg}", tool_msg["tool_name"] == "Read")
        ctx.check("the synthesized id never leaks onto the wire", "tool_call_id" not in tool_msg and
                  synthesized_id not in str(body2))
        asst_msg = [m for m in body2["messages"] if m.get("tool_calls")][0]
        ctx.check("replayed assistant tool_calls carry a parsed arguments object (not Halo's synthesized id)",
                  asst_msg["tool_calls"][0]["function"]["arguments"] == {"file_path": "a.txt"})


# ---- done_reason handling ---------------------------------------------------

@test
def test_done_reason_length_maps_to_max_tokens(ctx: Ctx):
    from tests.helpers.mock_ollama import MockUpstream
    from halo_harness.providers.stream import stream_ollama_completion
    with MockUpstream() as mock:
        req = _ollama_completion_request(mock, "done-reason-length")
        events = list(stream_ollama_completion(req))
        md = [e for e in events if e["type"] == "message_delta"][0]
        ctx.check(f"done_reason: length -> stop_reason max_tokens, got {md['delta']}",
                  md["delta"]["stop_reason"] == "max_tokens")
        ctx.check("done_reason carried in harness_meta for telemetry",
                  md["harness_meta"]["done_reason"] == "length")


@test
def test_done_reason_stop_maps_to_end_turn(ctx: Ctx):
    from tests.helpers.mock_ollama import MockUpstream
    from halo_harness.providers.stream import stream_ollama_completion
    with MockUpstream() as mock:
        req = _ollama_completion_request(mock, "done-reason-stop")
        events = list(stream_ollama_completion(req))
        md = [e for e in events if e["type"] == "message_delta"][0]
        ctx.check("done_reason: stop -> end_turn", md["delta"]["stop_reason"] == "end_turn")


@test
def test_done_reason_load_retries_once_then_succeeds(ctx: Ctx):
    """Round 2 brief: "load" is treated as "retry once" (2.0.5 brief's own
    assumption, UNCONFIRMED against a live server -- round 6's live-check
    has to settle it for real). This test pins the exact shape Halo
    retries against TODAY -- it fails loudly the moment a live server's
    behavior turns out to disagree with this scripted shape, which is
    exactly the point: round 6 gets something concrete to diff against."""
    from tests.helpers.mock_ollama import MockUpstream, ScriptedByCallCount
    from halo_harness.providers.stream import stream_ollama_completion
    with MockUpstream() as mock:
        mock.scenarios["load-retry"] = ScriptedByCallCount([
            [{"message": {"role": "assistant", "content": ""}, "done": True, "done_reason": "load",
              "prompt_eval_count": 0, "eval_count": 0}],
            [{"message": {"role": "assistant", "content": "ready now"}, "done": True, "done_reason": "stop",
              "prompt_eval_count": 10, "eval_count": 3}],
        ])
        req = _ollama_completion_request(mock, "load-retry")
        events = list(stream_ollama_completion(req))
        ctx.check(f"exactly ONE retry (two upstream calls total), got {len(mock.requests)}",
                  len(mock.requests) == 2)
        text = "".join(e["delta"].get("text", "") for e in events if e["type"] == "content_block_delta")
        ctx.check(f"the retry's real content reaches the caller, got {text!r}", text == "ready now")
        md = [e for e in events if e["type"] == "message_delta"][0]
        ctx.check("final stop_reason reflects the SUCCESSFUL retry, not the discarded 'load' attempt",
                  md["delta"]["stop_reason"] == "end_turn")


@test
def test_done_reason_load_with_real_output_is_not_retried(ctx: Ctx):
    """An empty-looking "load" is only ever retried while NOTHING has been
    shown yet -- real output alongside done_reason "load" (not how Ollama
    is believed to behave today, but defensively handled) must never be
    silently discarded."""
    from tests.helpers.mock_ollama import MockUpstream, _finish_chat
    from halo_harness.providers.stream import stream_ollama_completion

    def _scn_load_with_content(h, body):
        _finish_chat(h, [{"message": {"role": "assistant", "content": "real answer"}, "done": True,
                           "done_reason": "load", "prompt_eval_count": 5, "eval_count": 2}])

    with MockUpstream() as mock:
        mock.scenarios["load-with-content"] = _scn_load_with_content
        req = _ollama_completion_request(mock, "load-with-content")
        events = list(stream_ollama_completion(req))
        ctx.check(f"never retried (exactly one upstream call), got {len(mock.requests)}",
                  len(mock.requests) == 1)
        text = "".join(e["delta"].get("text", "") for e in events if e["type"] == "content_block_delta")
        ctx.check(f"real content preserved, got {text!r}", text == "real answer")


@test
def test_thinking_accumulated_for_display_never_replayed(ctx: Ctx):
    from tests.helpers.mock_ollama import MockUpstream
    from halo_harness.providers.ollama import OllamaHost
    from halo_harness.providers.ollama_request import build_ollama_request_body
    from halo_harness.providers.stream import stream_ollama_completion
    with MockUpstream() as mock:
        req = _ollama_completion_request(mock, "thinking")
        events = list(stream_ollama_completion(req))
        md = [e for e in events if e["type"] == "message_delta"][0]
        ctx.check(f"reasoning captured for display, got {md['harness_meta']}",
                  md["harness_meta"]["reasoning_text"] == "Let me think it through...")
        ctx.check("no native Anthropic thinking content_block emitted",
                  not any(e["type"] == "content_block_start" and e["content_block"].get("type") == "thinking"
                          for e in events))

        # A later turn's OUTGOING request must never replay that reasoning
        # text back onto the wire (reasoning_replay="empty" for this dialect).
        route, profile = _route_profile("thinking")
        host = OllamaHost(name="default", url=mock.base_url)
        history = [{"role": "assistant", "reasoning": {"text": "Let me think it through..."},
                    "content": [{"type": "text", "text": "the answer"}]}]
        body2 = build_ollama_request_body(system_text="", messages=history, tools=None, tool_choice=None,
                                           route=route, profile=profile, effort=None, host=host)
        ctx.check("prior reasoning text never reaches the wire",
                  "Let me think it through" not in str(body2))


# ---- context ownership ------------------------------------------------------

@test
def test_compute_num_ctx_clamps_to_minimum_of_every_candidate(ctx: Ctx):
    from halo_harness.providers.ollama import FALLBACK_NUM_CTX, HARD_CONTEXT_CAP, compute_num_ctx
    ctx.check("nothing known -> the documented fallback", compute_num_ctx(None, None, None) == FALLBACK_NUM_CTX)
    ctx.check("trained context wins when smallest", compute_num_ctx(16384, 32768, None) == 16384)
    ctx.check("host.max_ctx override wins when smallest", compute_num_ctx(131072, 32768, None) == 32768)
    ctx.check("a fit estimate wins when smallest", compute_num_ctx(131072, 65536, 8000) == 8000)
    ctx.check("never exceeds the hard cap regardless of a huge trained context",
              compute_num_ctx(1_000_000, None, None) == HARD_CONTEXT_CAP)


@test
def test_compaction_trigger_is_75_percent_of_num_ctx(ctx: Ctx):
    from halo_harness.providers.ollama import compaction_trigger_tokens
    ctx.check("75% of 32768", compaction_trigger_tokens(32768) == 24576)
    ctx.check("always at least 1", compaction_trigger_tokens(1) == 1)


# ---- FIX PASS: <name>/<name>:latest normalization ---------------------------

@test
def test_ollama_names_match_untagged_and_latest(ctx: Ctx):
    from halo_harness.providers.ollama import normalize_ollama_model_name, ollama_names_match
    ctx.check('"foo" normalizes to "foo:latest"', normalize_ollama_model_name("foo") == "foo:latest")
    ctx.check('an explicit tag is left alone', normalize_ollama_model_name("foo:v1") == "foo:v1")
    ctx.check("foo matches foo:latest", ollama_names_match("foo", "foo:latest"))
    ctx.check("foo:latest matches foo", ollama_names_match("foo:latest", "foo"))
    ctx.check("foo:latest matches foo:latest", ollama_names_match("foo:latest", "foo:latest"))
    ctx.check("foo:v1 does NOT match foo (a real, different tag)", not ollama_names_match("foo:v1", "foo"))
    ctx.check("empty/None never vacuously match", not ollama_names_match("", "foo") and not ollama_names_match(None, "foo"))


@test
def test_trained_context_for_resolves_an_untagged_ref_against_its_latest_row(ctx: Ctx):
    """Pin: a tags entry `foo:latest` resolves for `ol:foo` AND
    `ol:foo:latest` -- the live-run root cause (`halo-live-test` imported
    with no tag, stored/reported as `halo-live-test:latest`, previously
    went unmatched against the bare `ol:halo-live-test` ref)."""
    from halo_harness.providers.ollama import trained_context_for
    catalog = {"models": [{"model": "foo:latest", "name": "foo:latest",
                            "model_info": {"llama.context_length": 8192}}]}
    ctx.check("bare ref resolves", trained_context_for(catalog, "foo") == 8192)
    ctx.check("tagged ref resolves too", trained_context_for(catalog, "foo:latest") == 8192)
    ctx.check("a genuinely different model does not", trained_context_for(catalog, "bar") is None)


# ---- FIX PASS: Ollama's real exceed_context_size_error 400 ------------------

def _ollama_completion_request_overflow(mock, scenario: str, *, ceiling=None):
    """Same shape as `_ollama_completion_request` above, but exposed here
    so the overflow tests can set `ollama_ctx_retry_ceiling` afterwards
    (that field postdates this file's own shared helper)."""
    req = _ollama_completion_request(mock, scenario)
    req.ollama_ctx_retry_ceiling = ceiling
    return req


@test
def test_context_overflow_400_retries_once_with_a_bigger_num_ctx_then_succeeds(ctx: Ctx):
    """Live run, build 0.34.2: Ollama answers 400 `exceed_context_size_
    error` on overflow -- round 2's docstring wrongly assumed silent
    truncation. When a bigger window is actually allowed (the ceiling
    here mirrors `min(learned_cap, host.max_ctx, fit_estimate, hard_cap)`
    -- 131072, nothing else configured), Halo retries ONCE with the
    smallest power of two that holds `n_prompt_tokens` plus the output
    budget, never reaching compaction at all."""
    from tests.helpers.mock_ollama import MockUpstream, ScriptedByCallCount, send_json
    from halo_harness.providers.stream import stream_ollama_completion

    def _overflow_400(h, body):
        send_json(h, 400, {"error": {"code": 400, "message": "request (100000 tokens) exceeds the available "
                                       "context size (40960 tokens), try increasing it",
                            "type": "exceed_context_size_error", "n_prompt_tokens": 100000, "n_ctx": 40960}})

    with MockUpstream() as mock:
        mock.scenarios["overflow-then-ok"] = ScriptedByCallCount([
            _overflow_400,
            [{"message": {"role": "assistant", "content": "recovered"}, "done": True, "done_reason": "stop",
              "prompt_eval_count": 100000, "eval_count": 2}],
        ])
        req = _ollama_completion_request_overflow(mock, "overflow-then-ok", ceiling=131072)
        events = list(stream_ollama_completion(req))
        ctx.check(f"a normal message_start..message_stop stream, got {events[0]} .. {events[-1]}",
                  events[0]["type"] == "message_start" and events[-1]["type"] == "message_stop")
        chats = [r for r in mock.requests if r["path"].rstrip("/") == "/api/chat"]
        ctx.check(f"exactly two attempts (overflow, then the bigger retry), got {len(chats)}", len(chats) == 2)
        first_ctx = (chats[0]["body"].get("options") or {}).get("num_ctx")
        second_ctx = (chats[1]["body"].get("options") or {}).get("num_ctx")
        ctx.check(f"the retry used a strictly BIGGER num_ctx, got first={first_ctx} second={second_ctx}",
                  isinstance(second_ctx, int) and isinstance(first_ctx, int) and second_ctx > first_ctx)
        ctx.check(f"the smallest power of two that holds 100000 prompt tokens + the output budget, got {second_ctx}",
                  second_ctx == 131072)


@test
def test_context_overflow_400_with_no_room_to_grow_raises_context_overflow(ctx: Ctx):
    """The OTHER half of the same pin: when even the full ceiling can't
    hold the prompt, Halo never retries -- it raises the SAME
    `ContextOverflow` it always did, for the existing compaction-and-
    retry path (`agent/loop.py`) to pick up unchanged."""
    from tests.helpers.mock_ollama import MockUpstream, send_json
    from halo_harness.providers.stream import ContextOverflow, stream_ollama_completion

    def _overflow_400_huge(h, body):
        send_json(h, 400, {"error": {"code": 400, "message": "nope", "type": "exceed_context_size_error",
                                       "n_prompt_tokens": 9_999_999, "n_ctx": 40960}})

    with MockUpstream() as mock:
        mock.scenarios["overflow-no-room"] = _overflow_400_huge
        req = _ollama_completion_request_overflow(mock, "overflow-no-room", ceiling=131072)
        try:
            list(stream_ollama_completion(req))
            ctx.check("must raise ContextOverflow when no bigger context fits", False)
        except ContextOverflow as e:
            ctx.check(f"limit/prompt_tokens carried through from the 400, got limit={e.limit} "
                      f"prompt_tokens={e.prompt_tokens}", e.limit == 40960 and e.prompt_tokens == 9_999_999)
        chats = [r for r in mock.requests if r["path"].rstrip("/") == "/api/chat"]
        ctx.check(f"exactly ONE attempt -- no retry was even tried, got {len(chats)}", len(chats) == 1)


@test
def test_context_overflow_400_with_no_ceiling_known_raises_immediately(ctx: Ctx):
    """`ollama_ctx_retry_ceiling` unset (its dataclass default) -- the
    SAME terminal behaviour as "no room to grow", never a crash on a
    missing ceiling."""
    from tests.helpers.mock_ollama import MockUpstream, send_json
    from halo_harness.providers.stream import ContextOverflow, stream_ollama_completion

    def _overflow_400(h, body):
        send_json(h, 400, {"error": {"code": 400, "message": "nope", "type": "exceed_context_size_error",
                                       "n_prompt_tokens": 50000, "n_ctx": 40960}})

    with MockUpstream() as mock:
        mock.scenarios["overflow-no-ceiling"] = _overflow_400
        req = _ollama_completion_request(mock, "overflow-no-ceiling")  # ollama_ctx_retry_ceiling left at its default
        try:
            list(stream_ollama_completion(req))
            ctx.check("must raise ContextOverflow with no ceiling known", False)
        except ContextOverflow:
            pass
        chats = [r for r in mock.requests if r["path"].rstrip("/") == "/api/chat"]
        ctx.check(f"exactly ONE attempt, got {len(chats)}", len(chats) == 1)


if __name__ == "__main__":
    ctx = Ctx()
    results, passed, failed, skipped = run_all(TESTS, ctx)
    sys.exit(print_results(results, passed, failed, skipped))
