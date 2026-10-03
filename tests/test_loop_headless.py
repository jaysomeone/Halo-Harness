"""tests.test_loop_headless -- e2e: `python -m halo_harness -p "..."` against
the mock upstream via BRIDGE_TEST_HOME + BRIDGE_OPENROUTER_BASE_URL. Asserts
the system prompt is byte-stable across two constructions, MEMORY.md content
reaches the actual upstream request, and the `--output-format json` shape.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.helpers.runner import Ctx, new_registry, print_results, run_all
from tests.helpers.fake_home import build_fake_home
from tests.helpers.mock_openai import MockUpstream

REPO_DIR = Path(__file__).resolve().parent.parent

test, TESTS = new_registry()


def _run_cli(fh, mock, prompt, extra_args=None, extra_env=None, timeout=30):
    env = _hermetic_child_env()
    env.update({
        "BRIDGE_TEST_HOME": str(fh["home"]),
        "BRIDGE_OPENROUTER_BASE_URL": mock.base_url,
        "OPENROUTER_API_KEY": "test-key",
        "PYTHONPATH": str(REPO_DIR),
    })
    if extra_env:
        env.update(extra_env)
    args = [sys.executable, "-m", "halo_harness", "-p", prompt, "--model", "or:mock/model",
            "--cwd", str(fh["proj"])] + (extra_args or [])
    return subprocess.run(args, env=env, cwd=str(REPO_DIR), capture_output=True, text=True, timeout=timeout)


@test
def test_print_mode_text_output_says_pong(ctx: Ctx):
    fh = build_fake_home()
    mock = MockUpstream().start()
    try:
        result = _run_cli(fh, mock, "reply with the single word pong")
        ctx.check(f"exit 0, got {result.returncode} (stderr: {result.stderr[-500:]!r})", result.returncode == 0)
        ctx.check(f"stdout mentions pong, got {result.stdout!r}", "pong" in result.stdout)
    finally:
        mock.stop()


@test
def test_memory_md_content_reaches_the_upstream_request(ctx: Ctx):
    fh = build_fake_home()
    mock = MockUpstream().start()
    try:
        result = _run_cli(fh, mock, "what does MEMORY.md say about the media server?")
        ctx.check(f"exit 0, got {result.returncode} (stderr: {result.stderr[-500:]!r})", result.returncode == 0)
        ctx.check("at least one request recorded", len(mock.requests) >= 1)
        last_body = mock.requests[-1]["body"] or {}
        # The system prompt travels as an OpenAI "system" role message (or
        # folded into the first message) once translated -- check the whole
        # serialized body for the memory text rather than assuming exactly
        # which message index/role carries it.
        whole_body_text = json.dumps(last_body)
        ctx.check("MEMORY.md's media-server content reached the actual upstream request",
                  "mediabox" in whole_body_text)
        ctx.check("the LAN host reached the actual upstream request",
                  "lan-host.lan" in whole_body_text)
    finally:
        mock.stop()


@test
def test_h5b_f07_websearch_reaches_the_first_wire_tools_catalog(ctx: Ctx):
    """finding 7 (major, h4-h5-h3c review): `SessionCatalog(names=
    frozen_registry.names())` used to be snapshotted BEFORE WebSearch was
    added to the registry -- WebSearch ended up in the tool REGISTRY (so
    the system prompt, built straight off it, still described it) but
    missing from the actual wire `tools` field of the FIRST request, since
    agent/loop.py's Session.__init__ logs `tool_registry.definitions_for(
    session_catalog.names)` for its initial `meta` node -- so the model
    was never actually offered WebSearch at all. Verified against a real
    `-p` subprocess (a real Session, finding 17) with the mock upstream,
    zero MCP servers configured (fake_home's default) -- OpenRouter creds
    present is the only gate `build_session` checks."""
    fh = build_fake_home()
    mock = MockUpstream().start()
    try:
        result = _run_cli(fh, mock, "reply with the single word pong")
        ctx.check(f"exit 0, got {result.returncode} (stderr: {result.stderr[-500:]!r})", result.returncode == 0)
        ctx.check("at least one request recorded", len(mock.requests) >= 1)
        first_body = mock.requests[0]["body"] or {}
        tool_names = [((t or {}).get("function") or {}).get("name") for t in (first_body.get("tools") or [])]
        ctx.check(f"WebSearch is in the FIRST request's own wire tools catalog, got {tool_names}",
                  "WebSearch" in tool_names)
    finally:
        mock.stop()


@test
def test_finding_14_memory_snapshot_includes_directory_path(ctx: Ctx):
    """finding 14: the memory directory's real path is included in the
    memory snapshot itself (rather than the system prompt CLAIMING a
    writing capability this build doesn't have)."""
    from halo_harness.agent.assemble import SessionContext
    fh = build_fake_home()
    os.environ["BRIDGE_TEST_HOME"] = str(fh["home"])
    try:
        ctx_obj = SessionContext(cwd=fh["proj"], model_label="or:mock/model")
        snapshot = ctx_obj.memory_snapshot_text()
        ctx.check("memory snapshot non-empty for this fixture", bool(snapshot))
        ctx.check(f"memory directory path present in the snapshot, got head={snapshot[:120]!r}",
                  "Memory directory:" in snapshot and str(ctx_obj.memory_store.memory_dir_path) in snapshot)
    finally:
        os.environ.pop("BRIDGE_TEST_HOME", None)


@test
def test_finding_14_no_false_capability_promises_in_prompt(ctx: Ctx):
    """finding 14: with only the Read tool registered (a registry that
    genuinely has no Write/Bash -- H0's own shape), the system prompt must
    not claim it can write memory or run a test; the MCP sentence must
    always be honest about what this BUILD can't do, not about what the
    user has configured, regardless of registry shape."""
    from halo_harness.agent.prompt import build_system_prompt
    from halo_harness.tools.read import ReadTool
    from halo_harness.tools.registry import ToolRegistry
    prompt = build_system_prompt(model_label="or:mock/model", cwd="/tmp/x",
                                  tool_definitions=ToolRegistry(tools=[ReadTool()]).definitions(), family="generic")
    ctx.check('no "write it ONLY inside that memory directory" promise (no Write tool)',
              "write it ONLY inside" not in prompt)
    ctx.check('no "running a test" promise (no Bash tool)', "running a test" not in prompt)
    ctx.check('no false "No MCP servers are configured" claim', "No MCP servers are configured" not in prompt)
    # H3: MCP support now genuinely exists in this build -- the honest
    # statement is per-SESSION ("none configured/connected right now"),
    # never a blanket "not available in this build" (that claim would now
    # be FALSE whenever the user actually has servers configured).
    ctx.check('honest "none configured/connected" MCP framing present (no mcp_servers passed)',
              "none are configured/connected in this session" in prompt)
    ctx.check('never claims MCP support doesn\'t exist as a build limitation',
              "MCP tools are not available in this build" not in prompt)


@test
def test_finding_14_capability_promises_appear_once_the_tools_exist(ctx: Ctx):
    """The flip side of finding 14, now that H2's default registry actually
    HAS Write and Bash: the SAME registry-driven sentences must now
    promise memory-writing and mention running a test, proving prompt.py's
    logic is genuinely registry-driven (not just permanently "off")."""
    from halo_harness.agent.prompt import build_system_prompt
    from halo_harness.tools.registry import ToolRegistry
    prompt = build_system_prompt(model_label="or:mock/model", cwd="/tmp/x",
                                  tool_definitions=ToolRegistry().definitions(), family="generic")
    ctx.check('memory-write promise present now that Write exists',
              "write it ONLY inside" in prompt)
    ctx.check('"running a test" verification option present now that Bash exists',
              "running a test" in prompt)


@test
def test_json_output_shape(ctx: Ctx):
    fh = build_fake_home()
    mock = MockUpstream().start()
    try:
        result = _run_cli(fh, mock, "hi", extra_args=["--output-format", "json"])
        ctx.check(f"exit 0, got {result.returncode} (stderr: {result.stderr[-500:]!r})", result.returncode == 0)
        try:
            obj = json.loads(result.stdout.strip().splitlines()[-1])
        except (json.JSONDecodeError, IndexError) as e:
            ctx.check(f"stdout is valid JSON, got {result.stdout!r} ({e})", False)
            return
        for key in ("type", "subtype", "is_error", "result", "session_id", "num_turns",
                    "stop_reason", "usage", "total_cost_usd", "model"):
            ctx.check(f"json result has key {key!r}", key in obj)
        ctx.check("type == result", obj.get("type") == "result")
        ctx.check("is_error is False on success", obj.get("is_error") is False)
        ctx.check("result text is 'pong'", obj.get("result") == "pong")
    finally:
        mock.stop()


@test
def test_system_prompt_byte_stable_across_two_constructions(ctx: Ctx):
    """Builds SessionContext (which computes the system prompt) twice with
    identical inputs and confirms byte-for-byte equality -- the actual
    "computed once per session, byte-identical every turn" guarantee is that
    agent.loop.Session never recomputes it after __init__, so this proves
    the INPUT side (build_system_prompt/collect_git_info/memory read) has
    no hidden non-determinism that would make even a fresh computation
    differ."""
    fh = build_fake_home()
    os.environ["BRIDGE_TEST_HOME"] = str(fh["home"])
    from halo_harness.agent.assemble import SessionContext

    ctx1 = SessionContext(cwd=fh["proj"], model_label="or:mock/model")
    ctx2 = SessionContext(cwd=fh["proj"], model_label="or:mock/model")
    ctx.check("system prompts are byte-identical across two constructions",
              ctx1.system_prompt == ctx2.system_prompt)
    ctx.check("system prompt is non-trivial", len(ctx1.system_prompt) > 100)


@test
def test_append_system_prompt_is_appended(ctx: Ctx):
    fh = build_fake_home()
    mock = MockUpstream().start()
    try:
        result = _run_cli(fh, mock, "hi", extra_args=["--append-system-prompt", "MARKER-TEXT-XYZ"])
        ctx.check(f"exit 0, got {result.returncode}", result.returncode == 0)
        last_body = mock.requests[-1]["body"] or {}
        ctx.check("appended text reached the upstream request",
                  "MARKER-TEXT-XYZ" in json.dumps(last_body))
    finally:
        mock.stop()


@test
def test_verbose_flag_does_not_break_output(ctx: Ctx):
    fh = build_fake_home()
    mock = MockUpstream().start()
    try:
        result = _run_cli(fh, mock, "hi", extra_args=["--verbose"])
        ctx.check(f"exit 0 with --verbose, got {result.returncode}", result.returncode == 0)
        ctx.check("stdout still has the reply", "pong" in result.stdout)
    finally:
        mock.stop()


@test
def test_without_p_flag_prints_tui_notice_exit_2(ctx: Ctx):
    # U2: bare launch now opens the real full-screen TUI, which needs an
    # actual terminal -- stdin=PIPE (closed immediately, no `input=`) makes
    # `sys.stdin.isatty()` reliably False regardless of the tty state of
    # whatever process runs this test suite, so this can never hang
    # waiting on Textual to set up a terminal that isn't there (must stay
    # fast/deterministic in a headless CI runner). NOTE: `stdin=DEVNULL`
    # does NOT work for this on Windows -- CPython's `os.isatty()` there
    # only inspects `GetFileType`, and NUL reports FILE_TYPE_CHAR just
    # like a real console, so it still reads as a tty.
    env = _hermetic_child_env()
    env["PYTHONPATH"] = str(REPO_DIR)
    result = subprocess.run([sys.executable, "-m", "halo_harness"], env=env, cwd=str(REPO_DIR),
                             capture_output=True, text=True, timeout=15, stdin=subprocess.PIPE)
    ctx.check(f"exit code 2, got {result.returncode}", result.returncode == 2)
    ctx.check("not-a-tty notice printed", "stdin is not a tty" in result.stderr)


@test
def test_version_flag(ctx: Ctx):
    env = _hermetic_child_env()
    env["PYTHONPATH"] = str(REPO_DIR)
    result = subprocess.run([sys.executable, "-m", "halo_harness", "--version"], env=env, cwd=str(REPO_DIR),
                             capture_output=True, text=True, timeout=15)
    ctx.check(f"exit 0, got {result.returncode}", result.returncode == 0)
    from halo_harness import __version__
    ctx.check(f"prints halo {__version__}, got {result.stdout!r}",
              result.stdout.strip() == f"halo {__version__}")


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
