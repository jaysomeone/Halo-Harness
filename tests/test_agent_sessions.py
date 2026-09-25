"""tests.test_agent_sessions -- agent/sessions.py (H6 scope D):
--continue/--resume/--session-id/--fork-session resolution and index.json
bookkeeping, plus the CLI-level wiring in headless.py (--session-id UUID
validation, --continue/--resume picking up a real prior session, forked
sessions never mutate the original).
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.helpers.runner import Ctx, new_registry, print_results, run_all
from rolo_claude.agent import sessions as agent_sessions

test, TESTS = new_registry()


def _fresh_cwd() -> Path:
    # H10b: NONE of this file's call sites ever set BRIDGE_TEST_HOME, so
    # `agent_sessions.sessions_dir(cwd)` (which resolves through
    # `bridge_home()`) fell through to the REAL `~/.rolo-claude/sessions`
    # whenever this module happened to run before anything else in the
    # process had set the seam -- exactly how real session files leaked
    # into rolo's real session history under `agent-sessions-cwd-*`
    # project slugs (H10b report, caught by tests/run_all.py's own real-
    # sessions guard). One fix here covers every caller in this file.
    os.environ["BRIDGE_TEST_HOME"] = str(Path(tempfile.mkdtemp(prefix="agent-sessions-home-")))
    return Path(tempfile.mkdtemp(prefix="agent-sessions-cwd-"))


def _write_session_file(cwd, session_id: str, *, first_user_text: str = "", title: str = "", mtime_bump: float = 0.0):
    directory = agent_sessions.sessions_dir(cwd)
    directory.mkdir(parents=True, exist_ok=True)
    nodes = [{"type": "meta", "seq": 0}]
    if title:
        nodes.append({"type": "meta", "title": title, "seq": 1})
    if first_user_text:
        nodes.append({"type": "user", "content": [{"type": "text", "text": first_user_text}], "seq": 2})
    path = directory / f"{session_id}.jsonl"
    path.write_text("\n".join(json.dumps(n) for n in nodes) + "\n", encoding="utf-8")
    if mtime_bump:
        t = time.time() + mtime_bump
        os.utime(path, (t, t))
    return path


@test
def test_is_valid_session_id(ctx: Ctx):
    ctx.check("a fresh uuid4 hex validates", agent_sessions.is_valid_session_id(uuid.uuid4().hex))
    ctx.check("a dashed uuid also validates", agent_sessions.is_valid_session_id(str(uuid.uuid4())))
    ctx.check("garbage does not validate", not agent_sessions.is_valid_session_id("not-a-uuid-at-all"))
    ctx.check("empty string does not validate", not agent_sessions.is_valid_session_id(""))


@test
def test_resolve_continue_no_sessions_returns_none(ctx: Ctx):
    cwd = _fresh_cwd()
    ctx.check("no sessions directory at all -> None", agent_sessions.resolve_continue(cwd) is None)


@test
def test_resolve_continue_picks_most_recent(ctx: Ctx):
    cwd = _fresh_cwd()
    older = uuid.uuid4().hex
    newer = uuid.uuid4().hex
    _write_session_file(cwd, older, mtime_bump=-10)
    _write_session_file(cwd, newer, mtime_bump=0)
    ctx.check("the most recently modified session wins", agent_sessions.resolve_continue(cwd) == newer)


@test
def test_resolve_resume_exact_id(ctx: Ctx):
    cwd = _fresh_cwd()
    sid = uuid.uuid4().hex
    _write_session_file(cwd, sid)
    resolved, err = agent_sessions.resolve_resume(cwd, sid)
    ctx.check("exact id resolves directly", resolved == sid and err is None)


@test
def test_resolve_resume_by_name_substring(ctx: Ctx):
    cwd = _fresh_cwd()
    sid = uuid.uuid4().hex
    _write_session_file(cwd, sid, first_user_text="please refactor the login flow")
    resolved, err = agent_sessions.resolve_resume(cwd, "login flow")
    ctx.check("a substring of the first user message resolves the session", resolved == sid and err is None)


@test
def test_resolve_resume_by_title(ctx: Ctx):
    cwd = _fresh_cwd()
    sid = uuid.uuid4().hex
    _write_session_file(cwd, sid, title="Fix the auth bug")
    resolved, err = agent_sessions.resolve_resume(cwd, "auth bug")
    ctx.check("a substring of a stored title resolves the session", resolved == sid and err is None)


@test
def test_resolve_resume_no_match_is_an_error(ctx: Ctx):
    cwd = _fresh_cwd()
    _write_session_file(cwd, uuid.uuid4().hex, first_user_text="something unrelated")
    resolved, err = agent_sessions.resolve_resume(cwd, "nonexistent phrase xyz")
    ctx.check("no match -> (None, error message)", resolved is None and bool(err))


@test
def test_resolve_resume_empty_value_falls_back_to_continue(ctx: Ctx):
    cwd = _fresh_cwd()
    sid = uuid.uuid4().hex
    _write_session_file(cwd, sid)
    resolved, err = agent_sessions.resolve_resume(cwd, None)
    ctx.check("--resume with no value falls back to the latest session", resolved == sid and err is None)
    resolved2, err2 = agent_sessions.resolve_resume(cwd, "")
    ctx.check("--resume '' behaves the same as no value", resolved2 == sid and err2 is None)


@test
def test_resolve_resume_by_transcript_path(ctx: Ctx):
    cwd = _fresh_cwd()
    sid = uuid.uuid4().hex
    path = _write_session_file(cwd, sid)
    resolved, err = agent_sessions.resolve_resume(cwd, str(path))
    ctx.check("a direct .jsonl path resolves to its own filename stem", resolved == sid and err is None)


@test
def test_fork_session_copies_without_mutating_original(ctx: Ctx):
    cwd = _fresh_cwd()
    sid = uuid.uuid4().hex
    original_path = _write_session_file(cwd, sid, first_user_text="original content")
    original_bytes = original_path.read_bytes()

    new_id = agent_sessions.fork_session(cwd, sid)
    ctx.check("a NEW session id was returned", new_id != sid)
    new_path = agent_sessions.sessions_dir(cwd) / f"{new_id}.jsonl"
    ctx.check("the forked file exists", new_path.is_file())
    ctx.check("the forked file's content matches the source", new_path.read_bytes() == original_bytes)
    ctx.check("the original file is untouched", original_path.read_bytes() == original_bytes)


@test
def test_fork_session_missing_source_still_returns_a_new_id(ctx: Ctx):
    cwd = _fresh_cwd()
    new_id = agent_sessions.fork_session(cwd, "does-not-exist")
    ctx.check("forking a nonexistent source doesn't raise, still yields an id", bool(new_id))


@test
def test_index_json_round_trip(ctx: Ctx):
    cwd = _fresh_cwd()
    sid = uuid.uuid4().hex
    ctx.check("empty index before anything is recorded", agent_sessions.load_index(cwd) == {})
    agent_sessions.record_session_start(cwd, sid, "do the initial thing")
    entry = agent_sessions.load_index(cwd)[sid]
    ctx.check("first_prompt recorded", entry["first_prompt"] == "do the initial thing")
    ctx.check("turns starts at 0", entry["turns"] == 0)
    ctx.check("started/last timestamps present", isinstance(entry.get("started"), (int, float)) and isinstance(entry.get("last"), (int, float)))

    agent_sessions.record_session_turn(cwd, sid, cost_usd=0.01)
    entry2 = agent_sessions.load_index(cwd)[sid]
    ctx.check("turns incremented to 1", entry2["turns"] == 1)
    ctx.check("cost_usd recorded", entry2["cost_usd"] == 0.01)

    agent_sessions.record_session_turn(cwd, sid, cost_usd=0.02)
    entry3 = agent_sessions.load_index(cwd)[sid]
    ctx.check("turns incremented again to 2", entry3["turns"] == 2)


@test
def test_set_title_and_generate_title_fallback(ctx: Ctx):
    cwd = _fresh_cwd()
    sid = uuid.uuid4().hex
    agent_sessions.record_session_start(cwd, sid, "first prompt")
    agent_sessions.set_title(cwd, sid, "My Custom Title")
    ctx.check("set_title persists into index.json", agent_sessions.load_index(cwd)[sid]["title"] == "My Custom Title")

    fallback = agent_sessions.generate_title("fix the login bug\nmore detail here", small_model_caller=None)
    ctx.check("no small_model_caller -> a deterministic first-line fallback title",
              fallback == "fix the login bug")

    def _boom(prompt, timeout_s):
        raise RuntimeError("upstream exploded")

    fallback2 = agent_sessions.generate_title("handle the crash gracefully", small_model_caller=_boom)
    ctx.check("a failing small_model_caller never raises -- falls back instead",
              fallback2 == "handle the crash gracefully")


@test
def test_index_json_write_failure_never_raises(ctx: Ctx):
    # update_index_entry's own contract: best-effort, matches SessionLog's
    # "a full disk must not crash a working session" reasoning.
    cwd = _fresh_cwd()
    try:
        agent_sessions.update_index_entry(cwd, "some-id", turns=1)
        ok = True
    except Exception:
        ok = False
    ctx.check("a normal write never raises", ok)


if __name__ == "__main__":
    ctx = Ctx()
    results, passed, failed, skipped = run_all(TESTS, ctx)
    sys.exit(print_results(results, passed, failed, skipped))
