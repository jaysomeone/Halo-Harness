"""tests.test_loop_h2_integration -- H2 scope D end to end through the mock
upstream: a Write -> Edit -> Bash tool loop via `ScriptedTurns`, permission
deny in `-p` (default mode denies a Write, json result carries
permission_denials + a suggested rule), --permission-mode/--tools/
--allowedTools/--disallowedTools wiring, a leaked/misnamed tool call
getting repaired mid-loop, and the loop breaker still working through the
new validate->repair->decide->run->truncate pipeline.
"""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.helpers.runner import Ctx, new_registry, print_results, run_all
from tests.helpers.fake_home import build_fake_home
from tests.helpers.mock_openai import MockUpstream, SCENARIOS, ScriptedTurns, _finish
from tests.helpers.provider_env_defaults import ensure_default_provider_credentials

ensure_default_provider_credentials()

REPO_DIR = Path(__file__).resolve().parent.parent

test, TESTS = new_registry()


def _run_cli(fh, mock, prompt, extra_args=None, timeout=30, model="or:mock/model"):
    env = _hermetic_child_env()
    env.update({
        "BRIDGE_TEST_HOME": str(fh["home"]), "BRIDGE_OPENROUTER_BASE_URL": mock.base_url,
        "OPENROUTER_API_KEY": "test-key", "PYTHONPATH": str(REPO_DIR),
    })
    args = [sys.executable, "-m", "halo_harness", "-p", prompt, "--model", model,
            "--cwd", str(fh["proj"])] + (extra_args or [])
    return subprocess.run(args, env=env, cwd=str(REPO_DIR), capture_output=True, text=True, timeout=timeout)


def _tool_call_chunk(call_id, name, arguments):
    return [
        {"choices": [{"index": 0, "delta": {"role": "assistant"}}]},
        {"choices": [{"index": 0, "delta": {"tool_calls": [
            {"index": 0, "id": call_id, "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}},
        ]}}]},
        {"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]},
    ]


def _final_text_chunk(text):
    return [
        {"choices": [{"index": 0, "delta": {"role": "assistant"}}]},
        {"choices": [{"index": 0, "delta": {"content": text}}]},
        {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
    ]


@test
def test_write_edit_bash_loop_end_to_end(ctx: Ctx):
    """The brief's own acceptance shape, scripted: Write -> Edit -> Bash,
    three real tool dispatches through the full H2 pipeline, final answer
    reflects the actual file content after all three ran for real."""
    fh = build_fake_home()
    target = fh["proj"] / "h2_loop_target.txt"
    steps = [
        _tool_call_chunk("call_w", "Write", {"file_path": str(target), "content": "alpha beta gamma\n"}),
        _tool_call_chunk("call_e", "Edit", {"file_path": str(target), "old_string": "beta", "new_string": "delta"}),
        _tool_call_chunk("call_b", "Bash", {"command": f"cat {target}"}),
        _final_text_chunk("alpha delta gamma"),
    ]
    SCENARIOS["h2-write-edit-bash"] = ScriptedTurns(steps)
    mock = MockUpstream().start()
    try:
        result = _run_cli(fh, mock, "do the three steps", extra_args=["--permission-mode", "auto"],
                           model="or:mock/h2-write-edit-bash")
        ctx.check(f"exit 0, got {result.returncode} (stderr: {result.stderr[-800:]!r})", result.returncode == 0)
        ctx.check(f"final answer reflects all three real tool results, got {result.stdout!r}",
                   result.stdout.strip() == "alpha delta gamma")
        ctx.check("file really has the edited content on disk", target.read_text(encoding="utf-8") == "alpha delta gamma\n")
        ctx.check("exactly 4 upstream calls (3 tool round trips + final)", len(mock.requests) == 4)
    finally:
        mock.stop()


@test
def test_permission_deny_in_print_mode_default(ctx: Ctx):
    """default mode (no explicit rule) must ask for a Write inside the
    working dir -- in print mode that resolves to deny + a suggested rule
    + permission_denials in the json result, and the tool is NEVER run."""
    fh = build_fake_home()
    target = fh["proj"] / "should_not_be_written.txt"
    SCENARIOS["h2-write-denied"] = ScriptedTurns([
        _tool_call_chunk("call_w", "Write", {"file_path": str(target), "content": "nope\n"}),
        _final_text_chunk("done"),
    ])
    mock = MockUpstream().start()
    try:
        result = _run_cli(fh, mock, "write the file", extra_args=["--permission-mode", "default", "--output-format", "json"],
                           model="or:mock/h2-write-denied")
        ctx.check(f"exit 0 (a denial is not a process error), got {result.returncode}", result.returncode == 0)
        obj = json.loads(result.stdout)
        ctx.check(f"permission_denials populated, got {obj.get('permission_denials')}", len(obj.get("permission_denials") or []) >= 1)
        denial = obj["permission_denials"][0]
        ctx.check("denial names the Write tool", denial.get("tool_name") == "Write")
        ctx.check("file was never actually written", not target.exists())
    finally:
        mock.stop()


@test
def test_tools_flag_empty_disables_every_tool(ctx: Ctx):
    fh = build_fake_home()
    SCENARIOS["h2-notools-probe"] = ScriptedTurns([_final_text_chunk("no tools needed")])
    mock = MockUpstream().start()
    try:
        result = _run_cli(fh, mock, "hi", extra_args=["--tools", "", "--verbose"], model="or:mock/h2-notools-probe")
        ctx.check(f"exit 0, got {result.returncode}", result.returncode == 0)
        ctx.check("no tools were offered on the wire", not mock.requests[0]["body"].get("tools"))
    finally:
        mock.stop()


@test
def test_tools_flag_restricts_to_named_subset(ctx: Ctx):
    fh = build_fake_home()
    SCENARIOS["h2-subset-probe"] = ScriptedTurns([_final_text_chunk("ok")])
    mock = MockUpstream().start()
    try:
        result = _run_cli(fh, mock, "hi", extra_args=["--tools", "Bash,Read"], model="or:mock/h2-subset-probe")
        ctx.check(f"exit 0, got {result.returncode}", result.returncode == 0)
        tool_names = {t["function"]["name"] for t in (mock.requests[0]["body"].get("tools") or [])}
        ctx.check(f"only Bash+Read offered, got {tool_names}", tool_names == {"Bash", "Read"})
    finally:
        mock.stop()


@test
def test_disallowed_tools_bare_name_removes_from_catalog(ctx: Ctx):
    fh = build_fake_home()
    SCENARIOS["h2-disallow-probe"] = ScriptedTurns([_final_text_chunk("ok")])
    mock = MockUpstream().start()
    try:
        result = _run_cli(fh, mock, "hi", extra_args=["--disallowedTools", "Bash"], model="or:mock/h2-disallow-probe")
        ctx.check(f"exit 0, got {result.returncode}", result.returncode == 0)
        tool_names = {t["function"]["name"] for t in (mock.requests[0]["body"].get("tools") or [])}
        ctx.check("Bash removed from the wire catalog entirely", "Bash" not in tool_names)
        ctx.check("other tools remain", "Read" in tool_names)
    finally:
        mock.stop()


@test
def test_allowed_tools_flag_grants_an_otherwise_asked_write(ctx: Ctx):
    """--allowedTools carries a real Tool(...) rule (not just a bare tool
    name) into the engine's allow list -- default mode would normally ASK
    (-> deny in print mode) for a Write inside the working dir; an
    explicit --allowedTools rule for that exact path must allow it."""
    fh = build_fake_home()
    target = fh["proj"] / "allowed_via_flag.txt"
    SCENARIOS["h2-allowedtools-probe"] = ScriptedTurns([
        _tool_call_chunk("call_w", "Write", {"file_path": str(target), "content": "granted\n"}),
        _final_text_chunk("done"),
    ])
    mock = MockUpstream().start()
    try:
        # Write(...) rules are never consulted (dead grammar, per D-CFG) --
        # an Edit(...) rule covers Write, so THAT's the real way to grant
        # this from the CLI, exactly as a real settings.local.json rule
        # would have to. A BARE filename is cwd-relative on both platforms
        # identically (D-CFG's own "cwd-relative" form); a rule holding a
        # platform-native ABSOLUTE path would need the //-prefixed form
        # instead (single leading "/" means base-relative, not
        # filesystem-root-absolute -- see test_permissions.py's own
        # //, ~/, /-relative coverage) -- avoided here since target's
        # parent IS the engine's cwd anyway, so the bare form is both
        # simplest and platform-neutral.
        rule = f"Edit({target.name})"
        result = _run_cli(fh, mock, "write it", extra_args=["--permission-mode", "default", "--allowedTools", rule],
                           model="or:mock/h2-allowedtools-probe")
        ctx.check(f"exit 0, got {result.returncode} (stderr {result.stderr[-500:]!r})", result.returncode == 0)
        ctx.check("file actually written thanks to the CLI allow rule", target.exists()
                  and target.read_text(encoding="utf-8") == "granted\n")
    finally:
        mock.stop()


@test
def test_dangerously_skip_permissions_allows_write(ctx: Ctx):
    fh = build_fake_home()
    target = fh["proj"] / "bypass_target.txt"
    SCENARIOS["h2-bypass-probe"] = ScriptedTurns([
        _tool_call_chunk("call_w", "Write", {"file_path": str(target), "content": "bypassed\n"}),
        _final_text_chunk("done"),
    ])
    mock = MockUpstream().start()
    try:
        result = _run_cli(fh, mock, "write it", extra_args=["--dangerously-skip-permissions"],
                           model="or:mock/h2-bypass-probe")
        ctx.check(f"exit 0, got {result.returncode} (stderr {result.stderr[-500:]!r})", result.returncode == 0)
        ctx.check("file actually written under bypassPermissions", target.exists() and target.read_text(encoding="utf-8") == "bypassed\n")
    finally:
        mock.stop()


@test
def test_repair_layer_promotes_a_misnamed_tool_call(ctx: Ctx):
    """A model that names a real Claude-Code-shaped tool call with a
    common alternate spelling (read_file instead of Read) must still get
    dispatched for real, via the repair layer's rename -- not bounce as an
    unknown-tool error."""
    fh = build_fake_home()
    target = fh["proj"] / "repair_target.txt"
    target.write_text("hello from disk\n", encoding="utf-8")
    SCENARIOS["h2-repair-probe"] = ScriptedTurns([
        _tool_call_chunk("call_r", "read_file", {"file_path": str(target)}),
        _final_text_chunk("hello from disk"),
    ])
    mock = MockUpstream().start()
    try:
        result = _run_cli(fh, mock, "read it", extra_args=["--permission-mode", "auto", "--verbose"],
                           model="or:mock/h2-repair-probe")
        ctx.check(f"exit 0, got {result.returncode} (stderr {result.stderr[-800:]!r})", result.returncode == 0)
        ctx.check(f"read_file was silently repaired to Read and actually ran, got {result.stdout!r}",
                   "hello from disk" in result.stdout)
    finally:
        mock.stop()


@test
def test_read_only_pool_runs_concurrently_and_preserves_call_order(ctx: Ctx):
    """tools/registry.py's run_read_only_batch (wired into agent/loop.py's
    _dispatch_tools): consecutive read-only calls run on the 4-thread pool
    (real wall-clock concurrency, not just "doesn't crash"), and results
    come back in ORIGINAL call order even though the FASTEST call actually
    finishes first."""
    import threading
    import time as time_mod
    from halo_harness.tools.base import Tool, ToolContext
    from halo_harness.tools.base import ToolResult as TR
    from halo_harness.tools.registry import ToolRegistry, run_read_only_batch

    completion_order = []
    lock = threading.Lock()

    class _SlowReadOnlyTool(Tool):
        name = "SlowRO"
        is_read_only = True

        def run(self, input, ctx):
            time_mod.sleep(input.get("delay", 0))
            with lock:
                completion_order.append(input.get("id"))
            return TR(f"done-{input.get('id')}")

    reg = ToolRegistry(tools=[_SlowReadOnlyTool()])
    calls = [("SlowRO", {"id": "a", "delay": 0.3}), ("SlowRO", {"id": "b", "delay": 0.05}),
             ("SlowRO", {"id": "c", "delay": 0.15})]
    t0 = time_mod.monotonic()
    results = run_read_only_batch(reg, calls, ToolContext(cwd=Path(".")))
    elapsed = time_mod.monotonic() - t0

    ctx.check(f"ran concurrently (well under the 0.5s sequential sum), got {elapsed:.2f}s", elapsed < 0.45)
    ctx.check(f"results in ORIGINAL call order regardless of completion order, got {[r.content for r in results]}",
               [r.content for r in results] == ["done-a", "done-b", "done-c"])
    ctx.check(f"actual completion order proves real concurrency (b,c,a not a,b,c), got {completion_order}",
               completion_order == ["b", "c", "a"])


@test
def test_read_only_batch_single_call_routes_through_the_same_wait(ctx: Ctx):
    """finding 4 must-do: "route single read-only calls through the same
    wait" -- the old fast path (`len(calls) == 1`) dispatched a solo call
    DIRECTLY, bypassing the abort-aware poll loop entirely. A solo call
    must still be abortable via ctx.abort, same as a batch of 2+."""
    import threading
    import time as time_mod
    from halo_harness.tools.base import Tool, ToolContext
    from halo_harness.tools.base import ToolResult as TR
    from halo_harness.tools.registry import ToolRegistry, run_read_only_batch

    class _HangingReadOnlyTool(Tool):
        name = "HangRO"
        is_read_only = True

        def run(self, input, ctx):
            time_mod.sleep(30.0)
            return TR("should never get here")

    reg = ToolRegistry(tools=[_HangingReadOnlyTool()])
    abort = threading.Event()
    threading.Timer(0.3, abort.set).start()
    t0 = time_mod.monotonic()
    results = run_read_only_batch(reg, [("HangRO", {})], ToolContext(cwd=Path("."), abort=abort))
    elapsed = time_mod.monotonic() - t0
    ctx.check(f"a SOLO call aborts promptly too, took {elapsed:.2f}s", elapsed < 2.0)
    ctx.check(f"one result, marked aborted, got {results}", len(results) == 1 and results[0].is_error is True
              and "aborted" in results[0].content.lower())


@test
def test_read_only_batch_per_call_tool_use_id(ctx: Ctx):
    """finding 5 must-do: each pooled call gets its OWN tool_use_id via a
    3-tuple `(name, input, tool_use_id)`, not one shared ctx with
    tool_use_id=None for the whole batch."""
    from halo_harness.tools.base import Tool, ToolContext
    from halo_harness.tools.base import ToolResult as TR
    from halo_harness.tools.registry import ToolRegistry, run_read_only_batch

    seen_ids = []

    class _IdEchoTool(Tool):
        name = "IdEcho"
        is_read_only = True

        def run(self, input, ctx):
            seen_ids.append(ctx.tool_use_id)
            return TR(f"id={ctx.tool_use_id}")

    reg = ToolRegistry(tools=[_IdEchoTool()])
    calls = [("IdEcho", {}, "toolu_a"), ("IdEcho", {}, "toolu_b")]
    results = run_read_only_batch(reg, calls, ToolContext(cwd=Path(".")))
    ctx.check(f"each call saw its own tool_use_id, got {sorted(seen_ids)}", sorted(seen_ids) == ["toolu_a", "toolu_b"])
    ctx.check(f"results carry the matching ids in call order, got {[r.content for r in results]}",
              [r.content for r in results] == ["id=toolu_a", "id=toolu_b"])


@test
def test_loop_dispatches_parallel_read_calls_correctly(ctx: Ctx):
    """A single assistant message with THREE parallel Read tool_use blocks
    -- the loop's own batching wiring must still pair each tool_result
    with the RIGHT tool_use_id, in order, through the real mock+CLI path."""
    fh = build_fake_home()
    t1 = fh["proj"] / "parallel_a.txt"
    t2 = fh["proj"] / "parallel_b.txt"
    t3 = fh["proj"] / "parallel_c.txt"
    t1.write_text("AAA\n", encoding="utf-8")
    t2.write_text("BBB\n", encoding="utf-8")
    t3.write_text("CCC\n", encoding="utf-8")

    def _scn(h, body):
        messages = (body or {}).get("messages") or []
        if any(m.get("role") == "tool" for m in messages):
            _finish(h, _final_text_chunk("AAA BBB CCC"))
        else:
            _finish(h, [
                {"choices": [{"index": 0, "delta": {"role": "assistant"}}]},
                {"choices": [{"index": 0, "delta": {"tool_calls": [
                    {"index": 0, "id": "call_a", "type": "function", "function": {"name": "Read", "arguments": json.dumps({"file_path": str(t1)})}},
                    {"index": 1, "id": "call_b", "type": "function", "function": {"name": "Read", "arguments": json.dumps({"file_path": str(t2)})}},
                    {"index": 2, "id": "call_c", "type": "function", "function": {"name": "Read", "arguments": json.dumps({"file_path": str(t3)})}},
                ]}}]},
                {"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]},
            ])

    SCENARIOS["h2-parallel-reads"] = _scn
    mock = MockUpstream().start()
    try:
        result = _run_cli(fh, mock, "read all three", extra_args=["--permission-mode", "auto"],
                           model="or:mock/h2-parallel-reads")
        ctx.check(f"exit 0, got {result.returncode} (stderr {result.stderr[-500:]!r})", result.returncode == 0)
        ctx.check(f"final answer reflects all three files' real content, got {result.stdout!r}",
                   result.stdout.strip() == "AAA BBB CCC")
    finally:
        mock.stop()


def _make_session(fh, mock, *, scenario: str, extra_profile_fields=None):
    """A real in-process Session against the mock upstream, WITHOUT going
    through a CLI subprocess -- needed for tests below that must override
    `provider_profile` fields (tool_leak_patterns, tool_choice_required_
    supported) no fake `mock/...` model id has a real model_table.json row
    for. `scenario` becomes the model id's tail (`mock/<scenario>`), which
    is exactly what MockUpstream's own dispatcher keys SCENARIOS lookups
    on -- mismatching this against the registered SCENARIOS key silently
    404s every request. Mirrors tests/test_loop_tools.py's own
    test_interrupt_leaves_no_unanswered_tool_use pattern."""
    import dataclasses
    from halo_harness.agent.assemble import SessionContext
    from halo_harness.agent.loop import Session as _Session
    from halo_harness.model import ModelProfile, parse_model_ref
    from halo_harness.providers.stream import ProviderCreds

    model_label = f"mock/{scenario}"
    os.environ["BRIDGE_TEST_HOME"] = str(fh["home"])
    os.environ["BRIDGE_OPENROUTER_BASE_URL"] = mock.base_url
    session_ctx = SessionContext(cwd=fh["proj"], model_label=model_label)
    model_ref = parse_model_ref(f"or:{model_label}")
    session = _Session(
        cwd=fh["proj"], model_ref=model_ref, model_profile=ModelProfile(),
        creds=ProviderCreds(base_url=mock.base_url, api_key="k"), state_dir=Path(tempfile.mkdtemp(prefix="h2-session-")),
        model_label=model_label, session_context=session_ctx, openrouter_base_url=mock.base_url,
    )
    if extra_profile_fields:
        session.provider_profile = dataclasses.replace(session.provider_profile, **extra_profile_fields)
    return session


@test
def test_leaked_text_embedded_call_is_promoted_and_dispatched(ctx: Ctx):
    """A model that emits a Hermes-style <tool_call>{json}</tool_call> as
    TEXT instead of a native call (no tool_calls on the wire at all) must
    still have it promoted to a real tool_use and actually dispatched --
    agent/loop.py's own leak_parser wiring, exercised through a real
    Session.turn(), not just repair.py's unit-level extractor tests."""
    fh = build_fake_home()
    target = fh["proj"] / "leaked_call_target.txt"
    target.write_text("leaked call worked\n", encoding="utf-8")

    def _scn(h, body):
        messages = (body or {}).get("messages") or []
        if any(m.get("role") == "tool" for m in messages):
            _finish(h, _final_text_chunk("leaked call worked"))
        else:
            leaked_text = '<tool_call>' + json.dumps({"name": "Read", "arguments": {"file_path": str(target)}}) + '</tool_call>'
            _finish(h, [
                {"choices": [{"index": 0, "delta": {"role": "assistant"}}]},
                {"choices": [{"index": 0, "delta": {"content": leaked_text}}]},
                {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
            ])

    SCENARIOS["h2-leaked-call"] = _scn
    mock = MockUpstream().start()
    try:
        session = _make_session(fh, mock, scenario="h2-leaked-call",
                                 extra_profile_fields={"tool_leak_patterns": ("hermes_tool_call",)})
        seen = list(session.turn("read it"))
        ready = [e for e in seen if e.kind == "tool_use_ready"]
        results = [e for e in seen if e.kind == "tool_result"]
        ctx.check(f"exactly one promoted tool_use_ready, got {ready}", len(ready) == 1)
        ctx.check("the promoted call resolved to Read", ready[0].data.get("name") == "Read")
        ctx.check("flagged repaired (promoted from text, not a native call)", ready[0].data.get("repaired") is True)
        ctx.check(f"the tool actually ran and succeeded, got {results}", results and results[0].data.get("ok") is True)
        final_text = "".join(e.data.get("text", "") for e in seen if e.kind == "text_delta")
        ctx.check(f"the final answer reflects the real file content, got {final_text!r}", "leaked call worked" in final_text)
    finally:
        mock.stop()
        os.environ.pop("BRIDGE_OPENROUTER_BASE_URL", None)


@test
def test_qwen_missing_opener_leak_is_promoted_and_dispatched(ctx: Ctx):
    """Halo 2.0.2 round 5 (Qwen-at-work brief, item 3): the Qwen3-Coder
    #475 shape -- a bare JSON tool call with NO opening `<tool_call>` tag
    (only the closing one survives) -- end to end through a real
    `Session.turn()`, the same way `test_leaked_text_embedded_call_is_
    promoted_and_dispatched` already proves for the paired-tag shape.
    `missing_tool_call_opener` was declared by every Qwen row but never
    implemented before this round."""
    fh = build_fake_home()
    target = fh["proj"] / "missing_opener_target.txt"
    target.write_text("missing opener worked\n", encoding="utf-8")

    def _scn(h, body):
        messages = (body or {}).get("messages") or []
        if any(m.get("role") == "tool" for m in messages):
            _finish(h, _final_text_chunk("missing opener worked"))
        else:
            # No opening <tool_call> tag at all -- only the closer.
            leaked_text = ("Sure, reading it now:\n"
                           + json.dumps({"name": "Read", "arguments": {"file_path": str(target)}})
                           + "</tool_call>")
            _finish(h, [
                {"choices": [{"index": 0, "delta": {"role": "assistant"}}]},
                {"choices": [{"index": 0, "delta": {"content": leaked_text}}]},
                {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
            ])

    SCENARIOS["h2-missing-opener"] = _scn
    mock = MockUpstream().start()
    try:
        session = _make_session(fh, mock, scenario="h2-missing-opener",
                                 extra_profile_fields={"tool_leak_patterns": ("missing_tool_call_opener",)})
        seen = list(session.turn("read it"))
        ready = [e for e in seen if e.kind == "tool_use_ready"]
        results = [e for e in seen if e.kind == "tool_result"]
        ctx.check(f"exactly one promoted tool_use_ready despite the missing opener, got {ready}", len(ready) == 1)
        ctx.check("the promoted call resolved to Read", ready[0].data.get("name") == "Read")
        ctx.check("a real (minted) id, never empty -- the existing promotion path's own id normalisation",
                  bool(ready[0].data.get("id")))
        ctx.check(f"the tool actually ran and succeeded, got {results}", results and results[0].data.get("ok") is True)
        final_text = "".join(e.data.get("text", "") for e in seen if e.kind == "text_delta")
        ctx.check(f"the final answer reflects the real file content, got {final_text!r}",
                  "missing opener worked" in final_text)
    finally:
        mock.stop()
        os.environ.pop("BRIDGE_OPENROUTER_BASE_URL", None)


@test
def test_tool_choice_required_retry_recovers_a_malformed_leak(ctx: Ctx):
    """When the text looks like an ATTEMPTED (but malformed/truncated) leak
    that leak_parser's own regex can't cleanly parse, and the profile row
    supports it, the loop retries ONCE with tool_choice=required -- proving
    both that the retry actually happens (a 3rd upstream call, with
    tool_choice=="required" on the 2nd) and that it recovers a usable
    answer instead of silently ending the turn on garbled prose."""
    fh = build_fake_home()
    target = fh["proj"] / "retry_recovered_target.txt"
    target.write_text("recovered\n", encoding="utf-8")
    call_log = []

    def _scn(h, body):
        call_log.append(body)
        messages = (body or {}).get("messages") or []
        if any(m.get("role") == "tool" for m in messages):
            _finish(h, _final_text_chunk("recovered"))
        elif (body or {}).get("tool_choice") == "required":
            _finish(h, _tool_call_chunk("call_recovered", "Read", {"file_path": str(target)}))
        else:
            # A truncated/malformed <tool_call> leak_parser's regex cannot
            # match (no closing tag, no valid JSON) -- looks ATTEMPTED, not
            # an ordinary final answer.
            _finish(h, [
                {"choices": [{"index": 0, "delta": {"role": "assistant"}}]},
                {"choices": [{"index": 0, "delta": {"content": '<tool_call>{"name": "Read", "argum'}}]},
                {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
            ])

    SCENARIOS["h2-retry-required"] = _scn
    mock = MockUpstream().start()
    try:
        session = _make_session(fh, mock, scenario="h2-retry-required", extra_profile_fields={
            "tool_leak_patterns": ("hermes_tool_call",), "tool_choice_required_supported": True,
        })
        seen = list(session.turn("read it"))
        ctx.check(f"3 upstream calls (malformed attempt, forced retry, final answer), got {len(call_log)}", len(call_log) == 3)
        ctx.check(f"the retry forced tool_choice=required, got {call_log[1].get('tool_choice')!r}",
                   call_log[1].get("tool_choice") == "required")
        final_text = "".join(e.data.get("text", "") for e in seen if e.kind == "text_delta")
        ctx.check(f"the retry recovered a usable answer, got {final_text!r}", "recovered" in final_text)
    finally:
        mock.stop()
        os.environ.pop("BRIDGE_OPENROUTER_BASE_URL", None)


@test
def test_ordinary_final_answer_never_triggers_a_spurious_retry(ctx: Ctx):
    """The critical regression this mechanism must never cause: an
    ORDINARY final answer (no tool call needed, no leak markers at all)
    must NOT trigger an extra upstream call -- exactly one call for a
    single-shot text reply, same as before this feature existed."""
    fh = build_fake_home()
    SCENARIOS["h2-ordinary-answer"] = ScriptedTurns([_final_text_chunk("just a plain answer")])
    mock = MockUpstream().start()
    try:
        result = _run_cli(fh, mock, "say something", model="or:mock/h2-ordinary-answer")
        ctx.check(f"exit 0, got {result.returncode}", result.returncode == 0)
        ctx.check(f"exactly ONE upstream call for an ordinary answer, got {len(mock.requests)}", len(mock.requests) == 1)
        ctx.check(f"the plain answer passed through unchanged, got {result.stdout!r}",
                   result.stdout.strip() == "just a plain answer")
    finally:
        mock.stop()


@test
def test_new_4_explanatory_fenced_json_example_never_dispatches(ctx: Ctx):
    """finding 2's own verified end-to-end failure: a model asked to
    EXPLAIN how tool calls work answers with prose that happens to embed
    a fenced ```json {"name": "Bash", "arguments": {...}} ``` example --
    this must be promoted to a real tool_use, executed, and made a second
    upstream call. It must instead stay a single, unmodified prose
    answer -- markup that is NOT the message's trailing content (more
    explanation follows the fence) is never promoted."""
    fh = build_fake_home()

    def _scn(h, body):
        explanation = (
            'A harness would send a JSON tool call like ```json\n'
            '{"name": "Bash", "arguments": {"command": "echo hi > f"}}\n'
            '```\nwhich the harness then parses, validates, and executes, returning the '
            'result back to the model in the next turn so the conversation can continue.'
        )
        _finish(h, _final_text_chunk(explanation))

    SCENARIOS["h2-explain-json"] = _scn
    mock = MockUpstream().start()
    try:
        session = _make_session(fh, mock, scenario="h2-explain-json",
                                 extra_profile_fields={"tool_leak_patterns": ("json_text_call",)})
        seen = list(session.turn("explain in one paragraph how a harness sends a tool call"))
        ready = [e for e in seen if e.kind == "tool_use_ready"]
        ctx.check(f"no tool call is ever dispatched, got {ready}", ready == [])
        ctx.check(f"exactly one upstream call (no forced retry either), got {len(mock.requests)}",
                   len(mock.requests) == 1)
        final_text = "".join(e.data.get("text", "") for e in seen if e.kind == "text_delta")
        ctx.check(f"the explanatory prose passed through unchanged, got {final_text!r}",
                   "A harness would send" in final_text and '"name": "Bash"' in final_text)
    finally:
        mock.stop()
        os.environ.pop("BRIDGE_OPENROUTER_BASE_URL", None)


@test
def test_loop_breaker_still_works_through_the_new_pipeline(ctx: Ctx):
    """The loop breaker (remind/deny/end at 3/5/8) still fires correctly
    now that dispatch goes through repair + permission decide first."""
    fh = build_fake_home()
    target = Path(tempfile.gettempdir()) / "h2-breaker-target.txt"
    target.write_text("x\n", encoding="utf-8")

    def _scn(h, body):
        _finish(h, _tool_call_chunk("call_repeat", "Read", {"file_path": str(target)}))

    SCENARIOS["h2-breaker-probe"] = _scn
    mock = MockUpstream().start()
    try:
        result = _run_cli(fh, mock, "read it repeatedly", extra_args=["--permission-mode", "auto", "--max-turns", "20"],
                           model="or:mock/h2-breaker-probe")
        ctx.check(f"process exits cleanly, got {result.returncode}", result.returncode in (0, 1))
        ctx.check(f"bounded upstream calls (loop breaker fired), got {len(mock.requests)}", len(mock.requests) <= 10)
    finally:
        mock.stop()
        try:
            target.unlink()
        except OSError:
            pass


def _hermetic_child_env() -> dict:
    """2.0.0 fixpass item G: never forward a stray BRIDGE_STATE_DIR
    (would let bridge_home() escape this test's own BRIDGE_TEST_HOME
    scoping) or HALO_* (would out-rank the legacy BRIDGE_* name a
    fixture deliberately sets, per env_compat's own precedence) from
    the parent process into a spawned child -- same hermeticity
    tests/test_init_cli.py::_run already has, applied at each of this
    file's own `env = dict(os.environ)` call sites."""
    env = dict(os.environ)
    env.pop("BRIDGE_STATE_DIR", None)
    for k in [k for k in env if k.startswith("HALO_")]:
        env.pop(k, None)
    return env


if __name__ == "__main__":
    ctx = Ctx()
    results, passed, failed, skipped = run_all(TESTS, ctx)
    sys.exit(print_results(results, passed, failed, skipped))
