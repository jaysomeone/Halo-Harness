"""tests.test_log_derive -- agent/log.py + agent/derive.py: derive_request
reconstructs (system_text, messages, tools) from the log; content_hash
backs the "model-visible means logged" runtime assertion; snapshot/user
node interleaving and tool_result pairing.

finding 16: every `@test` here is transparently wrapped in an isolated,
per-test `BRIDGE_STATE_DIR` (SessionLog's storage root is ALWAYS
bridge_home(), which without this fell back to the real
`~/.halo/sessions` -- 39 `derive-test-*` dirs accumulated there on
rolo's own box) with the prior value restored afterward, so a LATER test
module in the same `run_all.py` process (which runs every tests/test_*.py
file in one interpreter) never inherits it.
"""
import functools
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.helpers.runner import Ctx, new_registry, print_results, run_all
from halo_harness.agent.log import SessionLog
from halo_harness.agent.derive import LogAssemblyError, content_hash, content_hash_from_oai_body, derive_request

_register, TESTS = new_registry()


def test(fn):
    @functools.wraps(fn)
    def wrapper(ctx):
        old = os.environ.get("BRIDGE_STATE_DIR")
        os.environ["BRIDGE_STATE_DIR"] = str(Path(tempfile.mkdtemp(prefix="derive-test-state-")))
        try:
            return fn(ctx)
        finally:
            if old is None:
                os.environ.pop("BRIDGE_STATE_DIR", None)
            else:
                os.environ["BRIDGE_STATE_DIR"] = old
    return _register(wrapper)


def _fresh_log() -> SessionLog:
    return SessionLog(Path(tempfile.mkdtemp(prefix="derive-test-")), session_id="s")


@test
def test_exactly_one_system_node_required(ctx: Ctx):
    log = _fresh_log()
    log.append_user([{"type": "text", "text": "hi, no system yet"}])
    try:
        derive_request(log)
        ctx.check("missing system node must raise LogAssemblyError", False)
    except LogAssemblyError:
        ctx.check("raises loudly on a missing system node", True)

    log.append_system("SYS")
    try:
        derive_request(log)
        ctx.check("exactly one system node -> no raise", True)
    except LogAssemblyError:
        ctx.check("one system node must not raise", False)

    log.append_system("SYS AGAIN")
    try:
        derive_request(log)
        ctx.check("a SECOND system node must raise LogAssemblyError", False)
    except LogAssemblyError:
        ctx.check("raises loudly on more than one system node", True)


@test
def test_snapshot_and_user_interleave_into_one_pending_turn(ctx: Ctx):
    log = _fresh_log()
    log.append_system("SYS")
    log.append_snapshot([{"type": "text", "text": "CLAUDE.md content"}], kind="claude_md")
    log.append_snapshot([{"type": "text", "text": "memory index"}], kind="memory_index")
    log.append_user([{"type": "text", "text": "the actual question"}])
    system_text, messages, tools = derive_request(log)
    ctx.check(f"system text derived, got {system_text!r}", system_text == "SYS")
    ctx.check("exactly one user message (snapshots + user folded together)", len(messages) == 1)
    ctx.check("all three blocks present in log order", [b["text"] for b in messages[0]["content"]] ==
              ["CLAUDE.md content", "memory index", "the actual question"])


@test
def test_assistant_flushes_pending_user_turn(ctx: Ctx):
    log = _fresh_log()
    log.append_system("SYS")
    log.append_user([{"type": "text", "text": "q1"}])
    log.append_assistant(content=[{"type": "text", "text": "a1"}], stop_reason="end_turn")
    log.append_user([{"type": "text", "text": "q2"}])
    _, messages, _ = derive_request(log)
    ctx.check("4 messages: user, assistant, user", len(messages) == 3)
    ctx.check("roles in order", [m["role"] for m in messages] == ["user", "assistant", "user"])


@test
def test_tool_result_folds_into_pending_user_turn(ctx: Ctx):
    log = _fresh_log()
    log.append_system("SYS")
    log.append_user([{"type": "text", "text": "read a file"}])
    log.append_assistant(content=[{"type": "tool_use", "id": "call_1", "name": "Read", "input": {"file_path": "a.py"}}], stop_reason="tool_use")
    log.append_tool_result(tool_use_id="call_1", content="file contents")
    _, messages, _ = derive_request(log)
    ctx.check("3 messages: user, assistant, user(tool_result)", len(messages) == 3)
    tool_result_msg = messages[2]
    ctx.check("tool_result message has role user", tool_result_msg["role"] == "user")
    block = tool_result_msg["content"][0]
    ctx.check("tool_result block shape", block["type"] == "tool_result" and block["tool_use_id"] == "call_1" and block["content"] == "file contents")


@test
def test_reasoning_attached_to_assistant_message(ctx: Ctx):
    log = _fresh_log()
    log.append_system("SYS")
    log.append_user([{"type": "text", "text": "q"}])
    log.append_assistant(content=[{"type": "text", "text": "a"}], reasoning={"format": "text", "value": "my reasoning"}, stop_reason="end_turn")
    _, messages, _ = derive_request(log)
    ctx.check("reasoning carried on the assistant message", messages[-1].get("reasoning") == {"format": "text", "value": "my reasoning"})


@test
def test_tools_from_explicit_arg_wins_over_meta(ctx: Ctx):
    log = _fresh_log()
    log.append_meta(tools=[{"name": "FromMeta"}])
    log.append_system("SYS")
    _, _, tools = derive_request(log, tools=[{"name": "Explicit"}])
    ctx.check("explicit tools param wins", tools == [{"name": "Explicit"}])
    _, _, tools2 = derive_request(log, tools=None)
    ctx.check("falls back to meta's tools when none given explicitly", tools2 == [{"name": "FromMeta"}])


@test
def test_content_hash_stable_and_order_sensitive(ctx: Ctx):
    h1 = content_hash("SYS", [{"role": "user", "content": [{"type": "text", "text": "a"}]}], [{"name": "Read"}])
    h2 = content_hash("SYS", [{"role": "user", "content": [{"type": "text", "text": "a"}]}], [{"name": "Read"}])
    ctx.check("same content -> same hash", h1 == h2)
    h3 = content_hash("SYS", [{"role": "user", "content": [{"type": "text", "text": "b"}]}], [{"name": "Read"}])
    ctx.check("different content -> different hash", h1 != h3)


@test
def test_model_visible_means_logged_round_trip(ctx: Ctx):
    """The runtime assertion (scope E): re-deriving the log UP TO (excluding)
    a given assistant node and re-hashing must match the hash stored ON
    that node when it was first appended -- proving the request that
    produced it is reconstructable from the log alone.

    finding 4: `tools=None` (the meta node's own FROZEN catalog) both
    times, not the SAME hardcoded literal list passed to both calls -- the
    pre-H2 version of this test could never fail no matter what
    derive_request actually did with tools, since it was really only
    proving content_hash is a deterministic pure function of whatever you
    hand it."""
    log = _fresh_log()
    log.append_meta(tools=[{"name": "Read"}])
    log.append_system("SYS")
    log.append_user([{"type": "text", "text": "hello"}])

    # Simulate what agent/loop.py does: derive BEFORE appending the reply, hash it, store it.
    pre_system, pre_messages, pre_tools = derive_request(log, tools=None)
    ctx.check("tools came from the meta node's frozen catalog", pre_tools == [{"name": "Read"}])
    stored_hash = content_hash(pre_system, pre_messages, pre_tools)
    assistant_node = log.append_assistant(content=[{"type": "text", "text": "hi there"}], stop_reason="end_turn", request_hash=stored_hash)

    # Now re-derive using ONLY the nodes before that assistant node (upto its seq index).
    replay_system, replay_messages, replay_tools = derive_request(log, tools=None, upto=assistant_node["seq"])
    replay_hash = content_hash(replay_system, replay_messages, replay_tools)
    ctx.check(f"re-derived request hash matches the one stored on the assistant node, "
              f"stored={stored_hash!r} replay={replay_hash!r}", replay_hash == assistant_node["request_hash"])
    ctx.check("stored hash matches what we computed just before appending", stored_hash == assistant_node["request_hash"])


@test
def test_finding_4_request_hash_from_actual_body_survives_policy_changes(ctx: Ctx):
    """finding 4's OWN specific fix: agent/loop.py stores `request_hash` as
    `content_hash_from_oai_body(the EXACT body actually sent)`, not a
    second `derive_request` re-run after the fact. Rebuilding the body from
    `derive_request(upto=seq)` PLUS the meta node's own tools must
    reproduce the SAME hash even when a POLICY value (max_tokens -- a
    budget choice, not conversation content) differs between the original
    call and the replay; a genuinely different TOOL CATALOG must still
    change it."""
    from halo_harness.providers.profiles import resolve_profile
    from halo_harness.providers.request import build_request_body
    from halo_harness.providers.routing import Route

    log = _fresh_log()
    tools = [{"name": "Read", "description": "reads a file", "input_schema": {"type": "object", "properties": {}}}]
    log.append_meta(model="or:mock/model", cwd="/x", system_prompt_bytes=3, tools=tools)
    log.append_system("SYS")
    log.append_user([{"type": "text", "text": "hi"}])

    route = Route(provider="openrouter", upstream_model="mock/model", dialect="openai-chat")
    profile = resolve_profile(route)

    system_text, messages, derived_tools = derive_request(log, tools=None)
    body = build_request_body(system_text=system_text, messages=messages, tools=derived_tools, route=route, profile=profile)
    stored_hash = content_hash_from_oai_body(body)
    assistant_node = log.append_assistant(content=[{"type": "text", "text": "hi there"}], stop_reason="end_turn", request_hash=stored_hash)

    replay_system, replay_messages, replay_tools = derive_request(log, tools=None, upto=assistant_node["seq"])
    ctx.check(f"tools reproduced from the meta node's frozen catalog, got {replay_tools}", replay_tools == tools)
    # A deliberately DIFFERENT max_tokens (requested_max_tokens) -- a policy
    # choice, never logged conversation content -- must NOT change the hash.
    replay_body = build_request_body(system_text=replay_system, messages=replay_messages, tools=replay_tools,
                                      route=route, profile=profile, requested_max_tokens=999)
    replay_hash = content_hash_from_oai_body(replay_body)
    ctx.check(f"re-derived hash matches despite a different max_tokens (a policy key, excluded), "
              f"stored={stored_hash!r} replay={replay_hash!r}", replay_hash == stored_hash)
    ctx.check("stored hash matches what was computed just before appending", stored_hash == assistant_node["request_hash"])

    changed_tools = [{"name": "Read", "description": "CHANGED description", "input_schema": {"type": "object", "properties": {}}}]
    changed_body = build_request_body(system_text=replay_system, messages=replay_messages, tools=changed_tools,
                                       route=route, profile=profile)
    ctx.check("a genuinely different tool catalog DOES change the hash",
              content_hash_from_oai_body(changed_body) != stored_hash)


@test
def test_byte_identical_request_across_two_turns_with_identical_history(ctx: Ctx):
    """Two independently-built logs with the SAME conversation content must
    derive byte-identical requests -- the actual "session log derive ->
    byte-identical request across two turns" test the brief asks for,
    proven via two fresh sessions rather than resuming one (equivalent:
    derivation is a pure function of log content)."""
    def build():
        log = _fresh_log()
        log.append_system("SYS PROMPT")
        log.append_snapshot([{"type": "text", "text": "CLAUDE.md"}], kind="claude_md")
        log.append_user([{"type": "text", "text": "read a.py"}])
        return log

    log_a, log_b = build(), build()
    sa, ma, ta = derive_request(log_a, tools=[{"name": "Read"}])
    sb, mb, tb = derive_request(log_b, tools=[{"name": "Read"}])
    ctx.check("identical system text", sa == sb)
    ctx.check("identical messages", ma == mb)
    ctx.check("identical content_hash", content_hash(sa, ma, ta) == content_hash(sb, mb, tb))


@test
def test_compacted_node_shadows_everything_before_it(ctx: Ctx):
    """H5 scope B: appending a `compacted` marker then a replacement `user`
    node makes derive_request SKIP every original message before the
    marker -- the shadowed range -- while keeping the (never-shadowed)
    system node and everything appended after the marker."""
    log = _fresh_log()
    log.append_system("SYS")
    log.append_user([{"type": "text", "text": "q1 -- should be shadowed"}])
    log.append_assistant(content=[{"type": "text", "text": "a1 -- should be shadowed"}], stop_reason="end_turn")
    log.append_user([{"type": "text", "text": "q2 -- should be shadowed"}])

    marker = log.append_compacted(trigger="auto")
    ctx.check("append_compacted returns the marker node with surface_op=replace", marker["surface_op"] == "replace")
    log.append_user([{"type": "text", "text": "<compacted-summary>...</compacted-summary>"}])
    log.append_user([{"type": "text", "text": "new turn after compaction"}])

    system_text, messages, _ = derive_request(log)
    ctx.check("system text is untouched by compaction", system_text == "SYS")
    all_text = [b["text"] for m in messages for b in m["content"]]
    ctx.check(f"shadowed content is gone, got {all_text}",
              not any("should be shadowed" in t for t in all_text))
    ctx.check("the compacted-summary text made it through", any("compacted-summary" in t for t in all_text))
    ctx.check("the new post-compaction turn made it through", any("new turn after compaction" in t for t in all_text))


@test
def test_compacted_node_upto_before_marker_sees_original_history(ctx: Ctx):
    """A replay cut BEFORE the compaction happened (`upto` < marker seq)
    must reconstruct the ORIGINAL, unshadowed history -- proves the shadow
    is a property of what's IN the sliced node list, not a global flag."""
    log = _fresh_log()
    log.append_system("SYS")
    log.append_user([{"type": "text", "text": "q1"}])
    cut_point = len(log.nodes())
    log.append_compacted(trigger="manual")
    log.append_user([{"type": "text", "text": "<compacted-summary>x</compacted-summary>"}])

    _, messages, _ = derive_request(log, upto=cut_point)
    ctx.check("pre-compaction replay still sees the original user turn",
              messages[0]["content"][0]["text"] == "q1")


@test
def test_compacted_node_carries_trigger_and_instructions(ctx: Ctx):
    log = _fresh_log()
    log.append_system("SYS")
    marker = log.append_compacted(trigger="manual", custom_instructions="focus on file names")
    ctx.check("trigger recorded", marker["trigger"] == "manual")
    ctx.check("custom_instructions recorded", marker["custom_instructions"] == "focus on file names")
    marker2 = log.append_compacted(trigger="overflow")
    ctx.check("custom_instructions defaults to None", marker2["custom_instructions"] is None)


@test
def test_only_the_latest_compacted_node_matters(ctx: Ctx):
    """A SECOND compaction shadows everything before IT (including the
    first compaction's own marker and replacement summary) -- only the
    latest marker's seq is consulted."""
    log = _fresh_log()
    log.append_system("SYS")
    log.append_user([{"type": "text", "text": "first era"}])
    log.append_compacted(trigger="auto")
    log.append_user([{"type": "text", "text": "<compacted-summary>first summary</compacted-summary>"}])
    log.append_user([{"type": "text", "text": "second era"}])
    log.append_compacted(trigger="auto")
    log.append_user([{"type": "text", "text": "<compacted-summary>second summary (merged)</compacted-summary>"}])

    _, messages, _ = derive_request(log)
    all_text = [b["text"] for m in messages for b in m["content"]]
    ctx.check(f"only content from the SECOND compaction onward survives, got {all_text}",
              all_text == ["<compacted-summary>second summary (merged)</compacted-summary>"])


# ============================================================================
# H10 Part A: non-wire telemetry metadata (usage/assistant tool_meta/
# tool_result) must never change a derived request -- proven at THREE
# levels: derive_request's own (system_text, messages, tools) tuple, the
# OpenAI-dialect wire body (providers.request.build_request_body), and the
# native-Anthropic wire body (build_anthropic_request_body), since that
# second dialect's own `prepare_anthropic_messages` passes an unrecognized
# block key through VERBATIM (it only special-cases "thinking"/"text") --
# the real reason metadata is stored as a SIBLING key on the node rather
# than inside a tool_use/tool_result content block (see agent/log.py's own
# append_assistant/append_tool_result docstrings).
# ============================================================================

def _build_plain_log() -> SessionLog:
    """One realistic tool-call turn, logged with NONE of H10's new
    telemetry kwargs -- the "before" log."""
    log = _fresh_log()
    log.append_system("SYS")
    log.append_user([{"type": "text", "text": "list files"}])
    log.append_assistant(content=[
        {"type": "text", "text": "sure"},
        {"type": "tool_use", "id": "call_1", "name": "Bash", "input": {"command": "ls"}},
    ], stop_reason="tool_use")
    log.append_tool_result(tool_use_id="call_1", content="a.py\nb.py\n", is_error=False)
    log.append_usage({"input_tokens": 100, "output_tokens": 20}, 0.001)
    log.append_assistant(content=[{"type": "text", "text": "done: a.py, b.py"}], stop_reason="end_turn")
    return log


def _build_telemetry_log() -> SessionLog:
    """The exact SAME conversation, but every H10 kwarg populated -- the
    "after" log."""
    log = _fresh_log()
    log.append_system("SYS")
    log.append_user([{"type": "text", "text": "list files"}])
    log.append_assistant(
        content=[
            {"type": "text", "text": "sure"},
            {"type": "tool_use", "id": "call_1", "name": "Bash", "input": {"command": "ls"}},
        ], stop_reason="tool_use",
        tool_meta={"call_1": {"repaired": True, "repair_kind": "rename", "promoted_from_leak": False}},
    )
    log.append_tool_result(tool_use_id="call_1", content="a.py\nb.py\n", is_error=False,
                            tool="Bash", error_class=None, ms=12.5, num_bytes=10, spilled=False)
    log.append_usage({"input_tokens": 100, "output_tokens": 20}, 0.001, model="or:deepseek/deepseek-v4-flash",
                      route="or", provider="DeepInfra", finish_reason="tool_use", latency_ms=900.0,
                      ttft_ms=200.0, retries=2, status="ok")
    log.append_assistant(content=[{"type": "text", "text": "done: a.py, b.py"}], stop_reason="end_turn")
    return log


@test
def test_telemetry_metadata_never_changes_derive_request_output(ctx: Ctx):
    plain = _build_plain_log()
    rich = _build_telemetry_log()
    sys_p, msgs_p, tools_p = derive_request(plain)
    sys_r, msgs_r, tools_r = derive_request(rich)
    ctx.check("system_text identical", sys_p == sys_r)
    ctx.check("messages identical (deep equality)", msgs_p == msgs_r)
    ctx.check("tools identical", tools_p == tools_r)
    ctx.check("content_hash identical", content_hash(sys_p, msgs_p, tools_p) == content_hash(sys_r, msgs_r, tools_r))


@test
def test_telemetry_metadata_never_changes_openai_dialect_wire_body(ctx: Ctx):
    import json as _json
    from halo_harness.providers.profiles import reset_model_table_cache, resolve_profile
    from halo_harness.providers.request import build_request_body
    from halo_harness.providers.routing import Route

    reset_model_table_cache()
    route = Route(provider="openrouter", upstream_model="deepseek/deepseek-v4-flash", dialect="openai-chat")
    profile = resolve_profile(route)
    plain, rich = _build_plain_log(), _build_telemetry_log()
    sys_p, msgs_p, tools_p = derive_request(plain)
    sys_r, msgs_r, tools_r = derive_request(rich)
    body_p = build_request_body(system_text=sys_p, messages=msgs_p, tools=tools_p, route=route, profile=profile)
    body_r = build_request_body(system_text=sys_r, messages=msgs_r, tools=tools_r, route=route, profile=profile)
    dump_p = _json.dumps(body_p, sort_keys=True, default=str)
    dump_r = _json.dumps(body_r, sort_keys=True, default=str)
    ctx.check("OpenAI-dialect wire body is byte-identical whether or not telemetry metadata was logged",
              dump_p == dump_r)


@test
def test_telemetry_metadata_never_changes_anthropic_dialect_wire_body(ctx: Ctx):
    """The at-risk path: `prepare_anthropic_messages` passes an unrecognized
    tool_use/tool_result block key straight through -- this test is the
    proof that H10's metadata never lands INSIDE a content block (it lives
    as a sibling key on the node instead, which derive_request never reads
    into a message at all)."""
    import json as _json
    from halo_harness.providers.profiles import reset_model_table_cache, resolve_profile
    from halo_harness.providers.request import build_anthropic_request_body
    from halo_harness.providers.routing import Route

    reset_model_table_cache()
    route = Route(provider="anthropic", upstream_model="claude-sonnet-5", dialect="anthropic-passthrough")
    profile = resolve_profile(route)
    plain, rich = _build_plain_log(), _build_telemetry_log()
    sys_p, msgs_p, tools_p = derive_request(plain)
    sys_r, msgs_r, tools_r = derive_request(rich)
    body_p = build_anthropic_request_body(system_text=sys_p, messages=msgs_p, tools=tools_p, route=route, profile=profile)
    body_r = build_anthropic_request_body(system_text=sys_r, messages=msgs_r, tools=tools_r, route=route, profile=profile)
    dump_p = _json.dumps(body_p, sort_keys=True, default=str)
    dump_r = _json.dumps(body_r, sort_keys=True, default=str)
    ctx.check("native-Anthropic wire body is byte-identical whether or not telemetry metadata was logged",
              dump_p == dump_r)


if __name__ == "__main__":
    ctx = Ctx()
    results, passed, failed, skipped = run_all(TESTS, ctx)
    sys.exit(print_results(results, passed, failed, skipped))
