"""tests.test_sessions_h6 -- H6 scope D: --continue/--resume/--session-id/
--fork-session resolution and index.json bookkeeping (agent/sessions.py).
"""
import json
import os
import sys
import tempfile
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.helpers.runner import Ctx, new_registry, print_results, run_all
from tests.helpers.provider_env_defaults import ensure_default_provider_credentials, ensure_scoped_state_dir_once

ensure_scoped_state_dir_once()
ensure_default_provider_credentials()
from halo_harness.agent import sessions as agent_sessions

test, TESTS = new_registry()

# Test hygiene: BRIDGE_STATE_DIR is a process-wide env var (tests/run_all.py
# imports every test_*.py module into ONE interpreter) -- captured once
# here and restored by test_zzz_restore_env, registered LAST (tests run in
# file/registration order -- see tests/helpers/runner.py's run_all), so a
# LATER test module in the same run_all.py process never inherits our
# temp value.
_ORIGINAL_BRIDGE_STATE_DIR = os.environ.get("BRIDGE_STATE_DIR")


def _isolated_state_dir() -> Path:
    d = Path(tempfile.mkdtemp(prefix="rc-state-"))
    os.environ["BRIDGE_STATE_DIR"] = str(d)
    return d


def _write_session_log(cwd, session_id: str, *, user_text: str = "hello", title: str = None,
                        mtime: float = None) -> Path:
    directory = agent_sessions.sessions_dir(cwd)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{session_id}.jsonl"
    nodes = [{"type": "meta", "model": "or:mock/x"}]
    if title:
        nodes[0]["title"] = title
    nodes.append({"type": "system", "text": "sys"})
    nodes.append({"type": "user", "content": [{"type": "text", "text": user_text}]})
    with open(path, "w", encoding="utf-8") as f:
        for n in nodes:
            f.write(json.dumps(n) + "\n")
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


# ---- is_valid_session_id -----------------------------------------------------

@test
def test_is_valid_session_id_dashed_uuid(ctx: Ctx):
    ctx.check("dashed uuid valid", agent_sessions.is_valid_session_id(str(uuid.uuid4())))


@test
def test_is_valid_session_id_bare_hex(ctx: Ctx):
    ctx.check("bare 32-hex uuid valid", agent_sessions.is_valid_session_id(uuid.uuid4().hex))


@test
def test_is_valid_session_id_rejects_garbage(ctx: Ctx):
    ctx.check("not-a-uuid rejected", not agent_sessions.is_valid_session_id("not-a-uuid"))
    ctx.check("empty rejected", not agent_sessions.is_valid_session_id(""))
    ctx.check("None rejected", not agent_sessions.is_valid_session_id(None))


# ---- resolve_continue ---------------------------------------------------------

@test
def test_resolve_continue_no_sessions_returns_none(ctx: Ctx):
    _isolated_state_dir()
    cwd = Path(tempfile.mkdtemp(prefix="rc-cwd-"))
    ctx.check("no sessions -> None", agent_sessions.resolve_continue(cwd) is None)


@test
def test_resolve_continue_picks_most_recent(ctx: Ctx):
    _isolated_state_dir()
    cwd = Path(tempfile.mkdtemp(prefix="rc-cwd2-"))
    now = time.time()
    older_id, newer_id = uuid.uuid4().hex, uuid.uuid4().hex
    _write_session_log(cwd, older_id, mtime=now - 100)
    _write_session_log(cwd, newer_id, mtime=now)
    ctx.check("picks the newer mtime", agent_sessions.resolve_continue(cwd) == newer_id)


# ---- resolve_resume -----------------------------------------------------------

@test
def test_resolve_resume_empty_value_falls_back_to_continue(ctx: Ctx):
    _isolated_state_dir()
    cwd = Path(tempfile.mkdtemp(prefix="rc-cwd3-"))
    sid = uuid.uuid4().hex
    _write_session_log(cwd, sid)
    result, err = agent_sessions.resolve_resume(cwd, "")
    ctx.check("empty value resolves like --continue", result == sid)
    ctx.check("no error", err is None)


@test
def test_resolve_resume_exact_id(ctx: Ctx):
    _isolated_state_dir()
    cwd = Path(tempfile.mkdtemp(prefix="rc-cwd4-"))
    sid = uuid.uuid4().hex
    _write_session_log(cwd, sid)
    result, err = agent_sessions.resolve_resume(cwd, sid)
    ctx.check("exact id resolves", result == sid)
    ctx.check("no error", err is None)


@test
def test_resolve_resume_transcript_path(ctx: Ctx):
    _isolated_state_dir()
    cwd = Path(tempfile.mkdtemp(prefix="rc-cwd5-"))
    sid = uuid.uuid4().hex
    path = _write_session_log(cwd, sid)
    result, err = agent_sessions.resolve_resume(cwd, str(path))
    ctx.check("path resolves to its own stem", result == sid)
    ctx.check("no error", err is None)


@test
def test_resolve_resume_by_title(ctx: Ctx):
    _isolated_state_dir()
    cwd = Path(tempfile.mkdtemp(prefix="rc-cwd6-"))
    sid = uuid.uuid4().hex
    _write_session_log(cwd, sid, title="My Special Session")
    result, err = agent_sessions.resolve_resume(cwd, "special")
    ctx.check("matched by title substring", result == sid)


@test
def test_resolve_resume_by_first_user_text(ctx: Ctx):
    _isolated_state_dir()
    cwd = Path(tempfile.mkdtemp(prefix="rc-cwd7-"))
    sid = uuid.uuid4().hex
    _write_session_log(cwd, sid, user_text="please fix the flaky retry test")
    result, err = agent_sessions.resolve_resume(cwd, "flaky retry")
    ctx.check("matched by first-user-message substring", result == sid)


@test
def test_resolve_resume_no_match_returns_error(ctx: Ctx):
    _isolated_state_dir()
    cwd = Path(tempfile.mkdtemp(prefix="rc-cwd8-"))
    result, err = agent_sessions.resolve_resume(cwd, "nothing-will-match-this")
    ctx.check("no match -> None", result is None)
    ctx.check("error message present", bool(err))


# ---- fork_session --------------------------------------------------------------

@test
def test_fork_session_copies_bytes_exactly(ctx: Ctx):
    _isolated_state_dir()
    cwd = Path(tempfile.mkdtemp(prefix="rc-cwd9-"))
    sid = uuid.uuid4().hex
    src_path = _write_session_log(cwd, sid, user_text="original content")
    new_id = agent_sessions.fork_session(cwd, sid)
    ctx.check("new id is a valid uuid", agent_sessions.is_valid_session_id(new_id))
    ctx.check("new id differs from source", new_id != sid)
    dst_path = agent_sessions.sessions_dir(cwd) / f"{new_id}.jsonl"
    ctx.check("forked file exists", dst_path.exists())
    ctx.check("byte-identical copy", dst_path.read_bytes() == src_path.read_bytes())
    ctx.check("original untouched", src_path.exists())


@test
def test_fork_session_nonexistent_source_yields_id_with_no_file(ctx: Ctx):
    _isolated_state_dir()
    cwd = Path(tempfile.mkdtemp(prefix="rc-cwd10-"))
    new_id = agent_sessions.fork_session(cwd, uuid.uuid4().hex)
    ctx.check("still returns a valid id", agent_sessions.is_valid_session_id(new_id))
    ctx.check("no file was created", not (agent_sessions.sessions_dir(cwd) / f"{new_id}.jsonl").exists())


# ---- list_sessions ---------------------------------------------------------------

@test
def test_list_sessions_empty(ctx: Ctx):
    _isolated_state_dir()
    cwd = Path(tempfile.mkdtemp(prefix="rc-cwd11-"))
    ctx.check("no directory -> []", agent_sessions.list_sessions(cwd) == [])


@test
def test_list_sessions_newest_first_with_titles(ctx: Ctx):
    _isolated_state_dir()
    cwd = Path(tempfile.mkdtemp(prefix="rc-cwd12-"))
    now = time.time()
    old_id, new_id = uuid.uuid4().hex, uuid.uuid4().hex
    _write_session_log(cwd, old_id, title="Old One", mtime=now - 50)
    _write_session_log(cwd, new_id, title="New One", mtime=now)
    rows = agent_sessions.list_sessions(cwd)
    ctx.check("two rows", len(rows) == 2)
    ctx.check("newest first", rows[0]["id"] == new_id)
    ctx.check("title surfaced", rows[0]["title"] == "New One")


# ---- index.json ---------------------------------------------------------------

@test
def test_load_index_missing_file_returns_empty(ctx: Ctx):
    _isolated_state_dir()
    cwd = Path(tempfile.mkdtemp(prefix="rc-cwd13-"))
    ctx.check("missing index -> {}", agent_sessions.load_index(cwd) == {})


@test
def test_update_index_entry_round_trip(ctx: Ctx):
    _isolated_state_dir()
    cwd = Path(tempfile.mkdtemp(prefix="rc-cwd14-"))
    sid = uuid.uuid4().hex
    agent_sessions.update_index_entry(cwd, sid, turns=1, cost_usd=0.01, last=time.time())
    agent_sessions.update_index_entry(cwd, sid, turns=2)
    entry = agent_sessions.load_index(cwd)[sid]
    ctx.check("turns updated", entry["turns"] == 2)
    ctx.check("cost_usd preserved across updates", entry["cost_usd"] == 0.01)
    ctx.check("last is set (passed explicitly by the caller)", "last" in entry)


@test
def test_record_session_start_and_turn(ctx: Ctx):
    _isolated_state_dir()
    cwd = Path(tempfile.mkdtemp(prefix="rc-cwd15-"))
    sid = uuid.uuid4().hex
    agent_sessions.record_session_start(cwd, sid, "first prompt text here")
    entry = agent_sessions.load_index(cwd)[sid]
    ctx.check("first_prompt captured", entry["first_prompt"] == "first prompt text here")
    ctx.check("turns starts at 0", entry["turns"] == 0)
    agent_sessions.record_session_turn(cwd, sid, cost_usd=0.5)
    entry = agent_sessions.load_index(cwd)[sid]
    ctx.check("turns incremented to 1", entry["turns"] == 1)
    agent_sessions.record_session_turn(cwd, sid, cost_usd=1.5)
    entry = agent_sessions.load_index(cwd)[sid]
    ctx.check("turns incremented to 2", entry["turns"] == 2)
    ctx.check("cost_usd updated", entry["cost_usd"] == 1.5)


@test
def test_set_title(ctx: Ctx):
    _isolated_state_dir()
    cwd = Path(tempfile.mkdtemp(prefix="rc-cwd16-"))
    sid = uuid.uuid4().hex
    agent_sessions.set_title(cwd, sid, "  My Title  ")
    ctx.check("title trimmed", agent_sessions.load_index(cwd)[sid]["title"] == "My Title")


@test
def test_set_title_truncates_long_titles(ctx: Ctx):
    _isolated_state_dir()
    cwd = Path(tempfile.mkdtemp(prefix="rc-cwd17-"))
    sid = uuid.uuid4().hex
    agent_sessions.set_title(cwd, sid, "x" * 200)
    ctx.check("title capped at 80 chars", len(agent_sessions.load_index(cwd)[sid]["title"]) == 80)


# ---- generate_title -------------------------------------------------------------

@test
def test_generate_title_fallback_first_line(ctx: Ctx):
    title = agent_sessions.generate_title("fix the flaky test\nmore detail here")
    ctx.check("first line used as fallback", title == "fix the flaky test")


@test
def test_generate_title_empty_text(ctx: Ctx):
    ctx.check("empty text -> 'New session'", agent_sessions.generate_title("") == "New session")


@test
def test_generate_title_uses_small_model_when_given(ctx: Ctx):
    def _caller(prompt, timeout_s):
        return '"Fix flaky retry test"'
    title = agent_sessions.generate_title("please fix the flaky retry test", small_model_caller=_caller)
    ctx.check("small model result used, quotes stripped", title == "Fix flaky retry test")


@test
def test_generate_title_small_model_failure_falls_back(ctx: Ctx):
    def _caller(prompt, timeout_s):
        raise RuntimeError("upstream down")
    title = agent_sessions.generate_title("fix the flaky test please", small_model_caller=_caller)
    ctx.check("falls back on exception", title == "fix the flaky test please")


@test
def test_h5b_b_resume_twice_loads_existing_nodes_both_times(ctx: Ctx):
    """B must-do: "--resume/--continue/--session-id must load the existing
    nodes into SessionLog._nodes" (H6) -- verified here with a session
    resumed TWICE in a row (build -> quit -> resume -> quit -> resume
    again), proving the second resume doesn't re-append a duplicate
    system/meta node (which would make derive_request raise
    LogAssemblyError on every later call) and both resumes actually see
    the FULL prior history, not just what the most recent process wrote."""
    import tempfile
    from pathlib import Path
    from halo_harness.agent.assemble import SessionContext
    from halo_harness.agent.derive import derive_request
    from halo_harness.agent.log import SessionLog
    from halo_harness.agent.loop import Session
    from halo_harness.model import ModelProfile, parse_model_ref
    from halo_harness.providers.stream import ProviderCreds

    cwd = Path(tempfile.mkdtemp(prefix="resume-twice-"))
    os.environ["BRIDGE_TEST_HOME"] = str(cwd / "home")
    session_id = "11111111-1111-1111-1111-111111111111"
    try:
        def _build_or_resume(existing_session_id):
            log = SessionLog(cwd, session_id=existing_session_id)
            log._nodes = log.read_all()  # exactly how a real --resume loads it
            ctx_obj = SessionContext(cwd=cwd, model_label="or:mock/model")
            model_ref = parse_model_ref("or:mock/model")
            return Session(
                cwd=cwd, model_ref=model_ref, model_profile=ModelProfile(),
                creds=ProviderCreds(base_url="http://x", api_key="k"),
                state_dir=cwd / "state", model_label=model_ref.raw, session_context=ctx_obj,
                session_log=log,
            )

        # 1) brand-new session (this process "starts" it).
        s1 = _build_or_resume(session_id)
        s1.log.append_user([{"type": "text", "text": "first turn content"}])
        s1.log.append_assistant(content=[{"type": "text", "text": "first reply"}], stop_reason="end_turn")
        del s1  # simulate the process exiting

        # 2) first --resume: must see turn 1's content, no LogAssemblyError.
        s2 = _build_or_resume(session_id)
        system_text, messages, _tools = derive_request(s2.log, tools=None)
        ctx.check("first resume sees turn 1's content",
                  "first turn content" in json.dumps(messages) and "first reply" in json.dumps(messages))
        s2.log.append_user([{"type": "text", "text": "second turn content"}])
        s2.log.append_assistant(content=[{"type": "text", "text": "second reply"}], stop_reason="end_turn")
        del s2  # simulate the process exiting again

        # 3) SECOND --resume in a row: must see BOTH turns, still no
        # duplicate system/meta node, still no LogAssemblyError.
        s3 = _build_or_resume(session_id)
        system_text3, messages3, _tools3 = derive_request(s3.log, tools=None)
        whole = json.dumps(messages3)
        ctx.check(f"second resume sees turn 1's content too, got {whole!r}", "first turn content" in whole)
        ctx.check(f"second resume sees turn 2's content, got {whole!r}", "second turn content" in whole)
        system_nodes = [n for n in s3.log.nodes() if n.get("type") == "system"]
        ctx.check(f"exactly one system node after TWO resumes (never re-appended), got {len(system_nodes)}",
                  len(system_nodes) == 1)
        s3.log.append_user([{"type": "text", "text": "third turn"}])  # would raise LogAssemblyError if malformed
        derive_request(s3.log, tools=None)  # must not raise
    finally:
        os.environ.pop("BRIDGE_TEST_HOME", None)


@test
def test_zzz_restore_env(ctx: Ctx):
    """Not a real test -- see the module-level comment by
    `_ORIGINAL_BRIDGE_STATE_DIR`. Must stay the LAST `@test` in this file."""
    if _ORIGINAL_BRIDGE_STATE_DIR is None:
        os.environ.pop("BRIDGE_STATE_DIR", None)
    else:
        os.environ["BRIDGE_STATE_DIR"] = _ORIGINAL_BRIDGE_STATE_DIR
    ctx.check("restored", True)


if __name__ == "__main__":
    ctx = Ctx()
    results, passed, failed, skipped = run_all(TESTS, ctx)
    sys.exit(print_results(results, passed, failed, skipped))
