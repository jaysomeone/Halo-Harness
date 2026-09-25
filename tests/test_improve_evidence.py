"""tests.test_improve_evidence -- H10 Part B1: every failure-cluster class
from a controlled fixture log (tool_error repeats, repair-layer hits,
loop-breaker trips, user corrections/correction cues, Read ENOENT, a
3-tool sequence recurring across >= 3 sessions), plus excerpt bounds
(<= 6 excerpts, <= 600 chars each).
"""
import sys
import tempfile
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.helpers.runner import Ctx, new_registry, print_results, run_all
from rolo_claude.agent.log import SessionLog
from rolo_claude.improve import evidence

test, TESTS = new_registry()


def _bare_log(root: Path, session_id: str) -> SessionLog:
    log = SessionLog.__new__(SessionLog)
    log.cwd = "/home/user/proj"
    log.slug = "proj"
    log.session_id = session_id
    log.dir = root / "proj"
    log.dir.mkdir(parents=True, exist_ok=True)
    log.path = log.dir / f"{session_id}.jsonl"
    log._nodes = []
    log._lock = threading.Lock()
    return log


def _build_fixtures() -> Path:
    root = Path(tempfile.mkdtemp(prefix="improve-evidence-"))

    # Session 1: (a) 2x Grep schema_invalid errors, (b) 2x rename repairs,
    # (c) a loop-breaker trip, (d) a user correction after a tool error,
    # (e) a Read not_found error, plus a 3-tool trigram (Read, Grep, Edit).
    log1 = _bare_log(root, "sess1")
    log1.append_meta(model="or:deepseek/deepseek-v4-flash")
    log1.append_system("SYS")
    log1.append_user([{"type": "text", "text": "start"}])
    for i in range(2):
        tid = f"grep{i}"
        log1.append_assistant(content=[{"type": "tool_use", "id": tid, "name": "Grep", "input": {}}],
                               stop_reason="tool_use",
                               tool_meta={tid: {"repaired": False, "repair_kind": "none", "promoted_from_leak": False}})
        log1.append_tool_result(tool_use_id=tid, content="Invalid arguments for Grep: missing 'pattern'",
                                 is_error=True, tool="Grep", error_class="schema_invalid")
    for i in range(2):
        tid = f"bash{i}"
        log1.append_assistant(content=[{"type": "tool_use", "id": tid, "name": "Bash", "input": {"command": "x"}}],
                               stop_reason="tool_use",
                               tool_meta={tid: {"repaired": True, "repair_kind": "rename", "promoted_from_leak": False}})
        log1.append_tool_result(tool_use_id=tid, content="ok", is_error=False, tool="Bash")
    lb_id = "lb1"
    log1.append_assistant(content=[{"type": "tool_use", "id": lb_id, "name": "Bash", "input": {"command": "y"}}],
                           stop_reason="tool_use",
                           tool_meta={lb_id: {"repaired": False, "repair_kind": "none", "promoted_from_leak": False}})
    log1.append_tool_result(tool_use_id=lb_id, content="Loop breaker: Bash called 5 times -- denied.",
                             is_error=True, tool="Bash", error_class="loop_breaker")
    read_id = "read1"
    log1.append_assistant(content=[{"type": "tool_use", "id": read_id, "name": "Read", "input": {"file_path": "/x.py"}}],
                           stop_reason="tool_use",
                           tool_meta={read_id: {"repaired": False, "repair_kind": "none", "promoted_from_leak": False}})
    log1.append_tool_result(tool_use_id=read_id, content="File /x.py not found", is_error=True, tool="Read",
                             error_class="not_found")
    log1.append_user([{"type": "text", "text": "no, that's wrong -- use /y.py instead"}])
    for name in ("Read", "Grep", "Edit"):
        tid = f"tri-{name}"
        log1.append_assistant(content=[{"type": "tool_use", "id": tid, "name": name, "input": {}}],
                               stop_reason="tool_use",
                               tool_meta={tid: {"repaired": False, "repair_kind": "none", "promoted_from_leak": False}})
        log1.append_tool_result(tool_use_id=tid, content="ok", is_error=False, tool=name)

    # Sessions 2 and 3: just the SAME (Read, Grep, Edit) trigram, nothing else
    # -- makes it recur in 3 distinct sessions (f)'s own >= 3 threshold.
    for sid in ("sess2", "sess3"):
        log = _bare_log(root, sid)
        log.append_meta(model="or:deepseek/deepseek-v4-flash")
        log.append_system("SYS")
        log.append_user([{"type": "text", "text": "start"}])
        for name in ("Read", "Grep", "Edit"):
            tid = f"{sid}-{name}"
            log.append_assistant(content=[{"type": "tool_use", "id": tid, "name": name, "input": {}}],
                                  stop_reason="tool_use",
                                  tool_meta={tid: {"repaired": False, "repair_kind": "none", "promoted_from_leak": False}})
            log.append_tool_result(tool_use_id=tid, content="ok", is_error=False, tool=name)
    return root / "proj"


@test
def test_every_cluster_class_fires(ctx: Ctx):
    d = _build_fixtures()
    clusters = evidence.build_clusters(since="all", slug="proj", sessions_dir=d.parent)
    by_kind = {}
    for c in clusters:
        by_kind.setdefault(c.kind, []).append(c)

    ctx.check(f"tool_error cluster present, got kinds={list(by_kind)}", "tool_error" in by_kind)
    ge = next(c for c in by_kind["tool_error"] if "Grep" in c.cluster_id)
    ctx.check(f"Grep schema_invalid count==2, got {ge.count}", ge.count == 2)

    ctx.check("repair cluster present", "repair" in by_kind)
    rc = by_kind["repair"][0]
    ctx.check(f"rename repair count==2, got {rc.count}", rc.count == 2)

    ctx.check("loop_breaker cluster present", "loop_breaker" in by_kind)
    ctx.check("read_enoent cluster present", "read_enoent" in by_kind)
    ctx.check("user_correction cluster present", "user_correction" in by_kind)
    uc = by_kind["user_correction"][0]
    ctx.check("correction excerpt captured the cue text", any("no," in ex.text for ex in uc.excerpts))

    ctx.check("tool_sequence cluster present (recurs in 3 sessions)", "tool_sequence" in by_kind)
    seq = by_kind["tool_sequence"][0]
    ctx.check(f"tool_sequence spans 3 sessions, got {seq.sessions}", len(seq.sessions) == 3)
    ctx.check("trigram names Read -> Grep -> Edit", "Read -> Grep -> Edit" in seq.label)


@test
def test_correction_cues_constant_list(ctx: Ctx):
    for cue in evidence.CORRECTION_CUES:
        ctx.check(f"{cue!r} matches its own text", evidence._is_correction_text(cue + " do it differently"))
    ctx.check("unrelated text does not match", not evidence._is_correction_text("please continue reading the file"))


@test
def test_excerpt_bounds(ctx: Ctx):
    root = Path(tempfile.mkdtemp(prefix="improve-evidence-bounds-"))
    log = _bare_log(root, "sess1")
    log.append_meta(model="or:x")
    log.append_system("SYS")
    log.append_user([{"type": "text", "text": "start"}])
    long_text = "E" * 5000
    for i in range(10):
        tid = f"e{i}"
        log.append_assistant(content=[{"type": "tool_use", "id": tid, "name": "Bash", "input": {}}],
                              stop_reason="tool_use",
                              tool_meta={tid: {"repaired": False, "repair_kind": "none", "promoted_from_leak": False}})
        log.append_tool_result(tool_use_id=tid, content=long_text, is_error=True, tool="Bash", error_class="other")

    clusters = evidence.build_clusters(since="all", slug="proj", sessions_dir=root)
    c = next(c for c in clusters if c.kind == "tool_error")
    ctx.check(f"count reflects all 10 occurrences, got {c.count}", c.count == 10)
    ctx.check(f"excerpts capped at {evidence.MAX_EXCERPTS}, got {len(c.excerpts)}",
              len(c.excerpts) == evidence.MAX_EXCERPTS)
    for ex in c.excerpts:
        ctx.check(f"excerpt text capped at {evidence.MAX_EXCERPT_CHARS}+1 (ellipsis), got {len(ex.text)}",
                   len(ex.text) <= evidence.MAX_EXCERPT_CHARS + 1)
        ctx.check("excerpt tagged from_tool_output", ex.from_tool_output is True)


if __name__ == "__main__":
    ctx = Ctx()
    results, passed, failed, skipped = run_all(TESTS, ctx)
    sys.exit(print_results(results, passed, failed, skipped))
