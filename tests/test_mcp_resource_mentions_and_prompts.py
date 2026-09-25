"""tests.test_mcp_resource_mentions_and_prompts -- H8 scope E (deferred by
H3): `@server:resource` mention expansion (rolo_claude/mcp/mentions.py) and
`/mcp__server__prompt` slash commands (commands/registry.register_mcp_
prompts), against the real fake MCP server (`fake://note` resource,
`greet` prompt).
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.helpers.runner import Ctx, new_registry, print_results, run_all
from tests.helpers.fake_mcp_server import running_manager
from rolo_claude.mcp.manager import McpServerConfig

test, TESTS = new_registry()

REPO_DIR = Path(__file__).resolve().parent.parent


def _fake_config() -> "dict[str, McpServerConfig]":
    # A fresh McpServerConfig per call -- McpManager/McpServerHandle mutate
    # bits of state onto what they're given across a server's lifecycle, so
    # (unlike a module-level constant) this must not be shared between the
    # many `running_manager(_fake_config())` call sites below.
    return {"fake": McpServerConfig(name="fake", type="stdio", command=sys.executable,
                                     args=["-m", "tests.helpers.fake_mcp_server"], env={},
                                     cwd=str(REPO_DIR))}


# ---- @server:resource mentions ---------------------------------------------

@test
def test_extract_server_resource_mentions_matches_a_real_resource(ctx: Ctx):
    from rolo_claude.mcp.mentions import extract_server_resource_mentions
    with running_manager(_fake_config()) as mgr:
        found = extract_server_resource_mentions("please read @fake:fake://note for context", mcp_manager=mgr)
        ctx.check(f"matched the real resource, got {found}", found == [("fake", "fake://note")])


@test
def test_extract_server_resource_mentions_ignores_unknown_server_and_uri(ctx: Ctx):
    from rolo_claude.mcp.mentions import extract_server_resource_mentions
    with running_manager(_fake_config()) as mgr:
        found = extract_server_resource_mentions("@nosuchserver:fake://note and @fake:not-a-real-uri", mcp_manager=mgr)
        ctx.check(f"neither bogus mention matches, got {found}", found == [])


@test
def test_extract_server_resource_mentions_none_manager_is_empty(ctx: Ctx):
    from rolo_claude.mcp.mentions import extract_server_resource_mentions
    ctx.check("no manager -> no mentions, never raises",
              extract_server_resource_mentions("@fake:fake://note", mcp_manager=None) == [])


@test
def test_read_server_resource_snapshots_reads_the_real_content(ctx: Ctx):
    from rolo_claude.mcp.mentions import read_server_resource_snapshots
    with running_manager(_fake_config()) as mgr:
        snapshots = read_server_resource_snapshots("see @fake:fake://note", mcp_manager=mgr)
        ctx.check(f"one snapshot returned, got {snapshots}", len(snapshots) == 1)
        label, content = snapshots[0]
        ctx.check("label names server:uri", label == "fake:fake://note")
        ctx.check(f"real resource text, got {content!r}", "fake MCP resource" in content)


@test
def test_read_server_resource_snapshots_bad_uri_after_disconnect_is_a_note_not_a_crash(ctx: Ctx):
    """A resource that WAS listed but fails to read (server died between
    listing and reading) becomes a short note, never an exception."""
    from rolo_claude.mcp.mentions import read_server_resource_snapshots

    class _FakeManagerRaisesOnRead:
        def resources(self):
            return [("fake", type("R", (), {"uri": "fake://note"})())]

        def read_resource(self, server, uri):
            raise RuntimeError("server went away")

    snapshots = read_server_resource_snapshots("@fake:fake://note", mcp_manager=_FakeManagerRaisesOnRead())
    ctx.check(f"one snapshot, an error note, got {snapshots}",
              len(snapshots) == 1 and "could not read" in snapshots[0][1])


@test
def test_ingest_at_mentions_and_headless_append_wire_resource_snapshots(ctx: Ctx):
    """The two real call sites (Controller.ingest_at_mentions, headless.py's
    _append_at_mention_snapshots) actually append a log snapshot for a
    resource-only mention -- not just the standalone mentions.py functions."""
    import tempfile
    from rolo_claude.agent.assemble import SessionContext
    from rolo_claude.agent.loop import Session
    from rolo_claude.model import ModelProfile, parse_model_ref

    with running_manager(_fake_config()) as mgr:
        cwd = Path(tempfile.mkdtemp(prefix="mcp-mentions-"))
        session_ctx = SessionContext(cwd=cwd, model_label="or:mock/model")
        model_ref = parse_model_ref("or:mock/model")
        session = Session(cwd=cwd, model_ref=model_ref, model_profile=ModelProfile(), creds=None,
                           state_dir=Path(tempfile.mkdtemp(prefix="mcp-mentions-state-")), model_label="or:mock/model",
                           session_context=session_ctx, mcp_manager=mgr)

        from rolo_claude.headless import _append_at_mention_snapshots
        _append_at_mention_snapshots(session, "look at @fake:fake://note please", cwd)
        snapshot_nodes = [n for n in session.log.nodes() if n.get("kind") == "at_mention"]
        ctx.check(f"headless path appended a snapshot, got {len(snapshot_nodes)}", len(snapshot_nodes) == 1)
        text = snapshot_nodes[0]["content"][0]["text"]
        ctx.check(f"contains the real resource text, got {text!r}", "fake MCP resource" in text)

        from rolo_claude.controller import Controller
        controller = Controller(session=session, cwd=cwd)
        controller.ingest_at_mentions("also see @fake:fake://note")
        snapshot_nodes2 = [n for n in session.log.nodes() if n.get("kind") == "at_mention"]
        ctx.check(f"Controller.ingest_at_mentions ALSO appended one (now 2 total), got {len(snapshot_nodes2)}",
                  len(snapshot_nodes2) == 2)


# ---- /mcp__server__prompt slash commands -----------------------------------

@test
def test_register_mcp_prompts_adds_a_command_named_after_the_server_and_prompt(ctx: Ctx):
    from rolo_claude.commands.registry import Registry, register_mcp_prompts
    with running_manager(_fake_config()) as mgr:
        reg = Registry()
        register_mcp_prompts(reg, mgr)
        cmd = reg.resolve("mcp__fake__greet")
        ctx.check(f"the command was registered, got {reg.all()}", cmd is not None)
        ctx.check("kind is 'prompt' (expands into the turn, never printed directly)", cmd.kind == "prompt")
        ctx.check("source is 'mcp'", cmd.source == "mcp")
        ctx.check(f"argument hint names the prompt's own argument, got {cmd.argument_hint!r}",
                  cmd.argument_hint == "<name>")


@test
def test_register_mcp_prompts_none_manager_is_a_noop(ctx: Ctx):
    from rolo_claude.commands.registry import Registry, register_mcp_prompts
    reg = Registry()
    register_mcp_prompts(reg, None)
    ctx.check("nothing registered", reg.all() == [])


@test
def test_mcp_prompt_command_run_calls_get_prompt_and_returns_its_text(ctx: Ctx):
    from rolo_claude.commands.registry import Registry, register_mcp_prompts
    with running_manager(_fake_config()) as mgr:
        reg = Registry()
        register_mcp_prompts(reg, mgr)
        cmd = reg.resolve("mcp__fake__greet")
        output = cmd.run("Alice", facade=None)
        ctx.check(f"the real prompt text came back, got {output!r}", "Say hello to Alice" in output)


@test
def test_mcp_prompt_command_run_with_no_args_uses_the_prompt_default(ctx: Ctx):
    from rolo_claude.commands.registry import Registry, register_mcp_prompts
    with running_manager(_fake_config()) as mgr:
        reg = Registry()
        register_mcp_prompts(reg, mgr)
        cmd = reg.resolve("mcp__fake__greet")
        output = cmd.run("", facade=None)
        ctx.check(f"the server's own default argument value was used, got {output!r}", "Say hello to world" in output)


@test
def test_mcp_prompt_command_does_not_override_an_existing_command(ctx: Ctx):
    from rolo_claude.commands.registry import Registry, SlashCommand, register_mcp_prompts
    with running_manager(_fake_config()) as mgr:
        reg = Registry()
        reg.add(SlashCommand(name="mcp__fake__greet", description="a pre-existing builtin", kind="core",
                              source="builtin", run=lambda a, f: "builtin wins"))
        register_mcp_prompts(reg, mgr)
        cmd = reg.resolve("mcp__fake__greet")
        ctx.check("the pre-existing command was never overwritten", cmd.source == "builtin")


if __name__ == "__main__":
    ctx = Ctx()
    results, passed, failed, skipped = run_all(TESTS, ctx)
    sys.exit(print_results(results, passed, failed, skipped))
