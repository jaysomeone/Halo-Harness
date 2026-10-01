"""tests.test_cli_file_attach -- H8 scope B: `--file SPEC [SPEC ...]` and
the TUI's `@path` image-attach path, both funnelled through
`headless.attach_cli_files`/`Controller.ingest_at_mentions`. Claude Code's
own `--file` downloads a claude.ai file_id:relative_path cloud resource;
rolo-claude has no such backing store, so that SPEC shape gets a clear
stderr notice instead, while an actual local path (the practically useful
case, and the one the TUI's `@path` mention shares) attaches for real.
"""
import functools
import io
import os
import sys
import tempfile
from contextlib import redirect_stderr
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.helpers.runner import Ctx, new_registry, print_results, run_all
from tests.helpers.provider_env_defaults import ensure_default_provider_credentials

# H15 part 2 addendum 3.1: a believable default credential (never a real
# one) keeps `or:mock/model` below resolving exactly as it did before
# parse_model_ref started refusing an auto-detected-disabled provider.
ensure_default_provider_credentials()

_register, TESTS = new_registry()


def test(fn):
    # H15 Part D2.1: `_real_session_and_controller` below builds a real
    # `Session`, whose `SessionLog` ALWAYS resolves its storage root via
    # `bridge_home()` (`BRIDGE_STATE_DIR`, else `BRIDGE_TEST_HOME`-derived,
    # else the REAL `~/.rolo-claude`) -- independent of the `state_dir=`
    # passed to `Session` itself. Every `@test` here is transparently
    # wrapped in an isolated, per-test `BRIDGE_STATE_DIR` so that path can
    # never resolve to the real machine, same pattern `tests/test_log_derive.py`
    # and `tests/test_invariants.py` already use.
    @functools.wraps(fn)
    def wrapper(ctx):
        old = os.environ.get("BRIDGE_STATE_DIR")
        os.environ["BRIDGE_STATE_DIR"] = str(Path(tempfile.mkdtemp(prefix="file-attach-test-state-")))
        try:
            return fn(ctx)
        finally:
            if old is None:
                os.environ.pop("BRIDGE_STATE_DIR", None)
            else:
                os.environ["BRIDGE_STATE_DIR"] = old
    return _register(wrapper)

_PNG_1X1 = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
    "890000000a49444154789c6360000002000100ffff03000006000557bfabd400"
    "0000004945" "4e44ae426082"
)  # a real, tiny, valid 1x1 RGBA PNG


class _FakeLog:
    def __init__(self):
        self.snapshots = []

    def append_snapshot(self, blocks, *, kind):
        self.snapshots.append((kind, blocks))


class _FakeProfile:
    def __init__(self, vision: bool):
        self.vision = vision


class _FakeSession:
    def __init__(self, vision: bool):
        self.model_profile = _FakeProfile(vision)
        self.log = _FakeLog()


@test
def test_attach_cli_files_none_or_empty_is_a_noop(ctx: Ctx):
    from rolo_claude.headless import attach_cli_files
    session = _FakeSession(vision=True)
    attach_cli_files(session, None, cwd=Path.cwd())
    attach_cli_files(session, [], cwd=Path.cwd())
    ctx.check("nothing appended", session.log.snapshots == [])


@test
def test_attach_cli_files_local_text_file_becomes_a_text_snapshot(ctx: Ctx):
    from rolo_claude.headless import attach_cli_files
    with tempfile.TemporaryDirectory() as d:
        cwd = Path(d)
        (cwd / "notes.txt").write_text("hello from a --file attachment", encoding="utf-8")
        session = _FakeSession(vision=False)
        attach_cli_files(session, ["notes.txt"], cwd=cwd)
        ctx.check(f"one snapshot, got {session.log.snapshots}", len(session.log.snapshots) == 1)
        kind, blocks = session.log.snapshots[0]
        ctx.check("kind is at_mention (same convention @path mentions use)", kind == "at_mention")
        ctx.check(f"text content present, got {blocks}",
                  len(blocks) == 1 and "hello from a --file attachment" in blocks[0]["text"])


@test
def test_attach_cli_files_local_image_with_vision_becomes_a_real_image_block(ctx: Ctx):
    from rolo_claude.headless import attach_cli_files
    with tempfile.TemporaryDirectory() as d:
        cwd = Path(d)
        (cwd / "shot.png").write_bytes(_PNG_1X1)
        session = _FakeSession(vision=True)
        attach_cli_files(session, ["shot.png"], cwd=cwd)
        ctx.check(f"one snapshot, got {session.log.snapshots}", len(session.log.snapshots) == 1)
        kind, blocks = session.log.snapshots[0]
        ctx.check(f"label text block first, got {blocks}", blocks[0] == {"type": "text", "text": "@shot.png"})
        ctx.check(f"then a real image block, got {blocks}", len(blocks) == 2 and blocks[1].get("type") == "image")


@test
def test_attach_cli_files_local_image_without_vision_is_a_text_note_not_an_image_block(ctx: Ctx):
    from rolo_claude.headless import attach_cli_files
    with tempfile.TemporaryDirectory() as d:
        cwd = Path(d)
        (cwd / "shot.png").write_bytes(_PNG_1X1)
        session = _FakeSession(vision=False)
        attach_cli_files(session, ["shot.png"], cwd=cwd)
        kind, blocks = session.log.snapshots[0]
        ctx.check(f"one text block, no image block, got {blocks}",
                  len(blocks) == 1 and blocks[0]["type"] == "text" and "no vision support" in blocks[0]["text"])


@test
def test_attach_cli_files_missing_local_path_warns_and_does_not_append(ctx: Ctx):
    from rolo_claude.headless import attach_cli_files
    session = _FakeSession(vision=True)
    buf = io.StringIO()
    with redirect_stderr(buf):
        attach_cli_files(session, ["does/not/exist.png"], cwd=Path(tempfile.mkdtemp()))
    ctx.check("nothing appended for a missing file", session.log.snapshots == [])
    ctx.check(f"a warning was printed, got {buf.getvalue()!r}", "not a file" in buf.getvalue())


@test
def test_attach_cli_files_cloud_resource_spec_gets_a_clear_notice_not_a_crash(ctx: Ctx):
    """Claude Code's own `--file file_abc:doc.txt` cloud-resource form:
    rolo-claude has no claude.ai file store to resolve `file_abc` against,
    so this must be a clear stderr notice, never a crash or a silently
    mis-attached local file named literally `file_abc:doc.txt`."""
    from rolo_claude.headless import attach_cli_files
    session = _FakeSession(vision=True)
    buf = io.StringIO()
    with redirect_stderr(buf):
        attach_cli_files(session, ["file_abc:doc.txt"], cwd=Path(tempfile.mkdtemp()))
    ctx.check("nothing appended for a cloud-resource spec", session.log.snapshots == [])
    msg = buf.getvalue()
    ctx.check(f"explains the cloud-resource mismatch, got {msg!r}",
              "file_id" in msg and "no claude.ai-hosted file store" in msg)


@test
def test_attach_cli_files_windows_style_drive_letter_path_is_not_mistaken_for_a_cloud_spec(ctx: Ctx):
    """A `:` at position 1 of an ABSOLUTE local path (`C:\\...`) must resolve
    as a real local file first, before the cloud-resource-spec heuristic
    (which also keys off `:`) ever gets a chance to misfire on it."""
    from rolo_claude.headless import attach_cli_files
    with tempfile.TemporaryDirectory() as d:
        cwd = Path(d)
        real_file = cwd / "drive-letter-style.txt"
        real_file.write_text("still a local file even though its absolute form has a colon", encoding="utf-8")
        session = _FakeSession(vision=False)
        # The absolute path itself is what matters (contains ":" on Windows,
        # e.g. "C:\\...\\drive-letter-style.txt"); on POSIX this is simply a
        # slash path with no colon at all, so the assertion is trivially
        # about the same code path either way -- the real regression this
        # guards is Windows-specific but the test is OS-neutral.
        attach_cli_files(session, [str(real_file)], cwd=cwd)
        ctx.check(f"resolved as a real local file, got {session.log.snapshots}", len(session.log.snapshots) == 1)
        ctx.check("no cloud-resource notice", session.log.snapshots[0][0] == "at_mention")


@test
def test_file_flag_is_a_real_flag_not_a_not_yet_flag(ctx: Ctx):
    from rolo_claude import cli
    real_names = {f for flags, _ in cli._REAL_FLAGS for f in flags}
    not_yet_names = {f for flags, _, _, _ in cli._NOT_YET_FLAGS for f in flags}
    ctx.check("--file is in _REAL_FLAGS", "--file" in real_names)
    ctx.check("--file is NOT in _NOT_YET_FLAGS", "--file" not in not_yet_names)


def _real_session_and_controller(*, vision: bool, cwd: Path):
    from rolo_claude.agent.assemble import SessionContext
    from rolo_claude.agent.loop import Session
    from rolo_claude.controller import Controller
    from rolo_claude.model import ModelProfile, parse_model_ref
    session_ctx = SessionContext(cwd=cwd, model_label="or:mock/model")
    model_ref = parse_model_ref("or:mock/model")
    session = Session(cwd=cwd, model_ref=model_ref, model_profile=ModelProfile(vision=vision), creds=None,
                       state_dir=Path(tempfile.mkdtemp(prefix="file-attach-state-")), model_label="or:mock/model",
                       session_context=session_ctx, mcp_manager=None)
    return session, Controller(session=session, cwd=cwd)


@test
def test_ingest_at_mentions_image_with_vision_appends_a_real_image_block(ctx: Ctx):
    """The TUI's live-typed `@path` mention (Controller.ingest_at_mentions,
    NOT the --file flag) for an image path: same vision gate/image-block
    preservation as attach_cli_files above -- this is the "@path for
    images" half of scope B's "--file-style attach via @path for images"."""
    with tempfile.TemporaryDirectory() as d:
        cwd = Path(d)
        (cwd / "shot.png").write_bytes(_PNG_1X1)
        session, controller = _real_session_and_controller(vision=True, cwd=cwd)
        controller.ingest_at_mentions("look at @shot.png please")
        nodes = [n for n in session.log.nodes() if n.get("kind") == "at_mention"]
        ctx.check(f"one snapshot, got {nodes}", len(nodes) == 1)
        blocks = nodes[0]["content"]
        ctx.check(f"a real image block is present, got {blocks}",
                  any(b.get("type") == "image" for b in blocks))


@test
def test_ingest_at_mentions_image_without_vision_appends_a_text_note_not_an_image_block(ctx: Ctx):
    with tempfile.TemporaryDirectory() as d:
        cwd = Path(d)
        (cwd / "shot.png").write_bytes(_PNG_1X1)
        session, controller = _real_session_and_controller(vision=False, cwd=cwd)
        controller.ingest_at_mentions("look at @shot.png please")
        nodes = [n for n in session.log.nodes() if n.get("kind") == "at_mention"]
        blocks = nodes[0]["content"]
        ctx.check(f"no image block without vision, got {blocks}",
                  not any(b.get("type") == "image" for b in blocks))
        ctx.check(f"a text note explains why, got {blocks}",
                  any("no vision support" in b.get("text", "") for b in blocks))


@test
def test_cli_parses_file_flag_with_multiple_specs(ctx: Ctx):
    # `--file` is `nargs="+"` (like `--add-dir`/`--mcp-config`/`--betas`
    # already were): it greedily swallows every following token, including
    # a trailing positional PROMPT, exactly the same way those other flags
    # already do -- so PROMPT must come BEFORE it on the command line, same
    # rule a user already has to follow for those.
    from rolo_claude.cli import _build_parser
    parser = _build_parser()
    args = parser.parse_args(["-p", "hello", "--file", "a.png", "b.txt"])
    ctx.check(f"both specs captured, got {args.file}", args.file == ["a.png", "b.txt"])
    ctx.check(f"prompt still parsed, got {args.prompt!r}", args.prompt == "hello")


if __name__ == "__main__":
    ctx = Ctx()
    results, passed, failed, skipped = run_all(TESTS, ctx)
    sys.exit(print_results(results, passed, failed, skipped))
