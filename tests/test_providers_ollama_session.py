"""tests.test_providers_ollama_session -- Halo 2.0.3 round 2b: the `ol:`
dialect wired into the real agent loop (`agent/loop.py`'s `_derive_and_build`/
`_build_request`/`_stream`, `headless.py`'s `_resolve_creds`), exercised end
to end through the real `-p` CLI entry point (print mode) -- same runner
style as `tests/test_headless_stream_json.py`: a subprocess per test, a
`tests.helpers.mock_ollama.MockUpstream` standing in for the real Ollama
daemon via `OLLAMA_HOST`. Round 2's own unit/wire-shape tests (the request
builder, the NDJSON decoder, the catalog) live in
`tests/test_providers_ollama.py`/`tests/test_providers_ollama_catalog.py`;
this module only covers what changed in round 2b -- that an `ol:` model
actually runs a turn (including a tool round trip) through the real
Session, not just through a hand-built CompletionRequest.
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.helpers.runner import Ctx, new_registry, print_results, run_all
from tests.helpers.fake_home import build_fake_home
from tests.helpers.mock_ollama import MockUpstream, ScriptedByCallCount

REPO_DIR = Path(__file__).resolve().parent.parent
test, TESTS = new_registry()


def _hermetic_child_env() -> dict:
    """Same hermeticity rule test_headless_stream_json.py's own helper
    applies (never forward a stray BRIDGE_STATE_DIR/HALO_* from the parent
    into the child); OLLAMA_API_KEY is ALSO scrubbed unconditionally here
    (never just left as whatever the developer's own shell happens to
    have) -- every test below sets it explicitly when it wants it present
    at all, so this suite never depends on the box running it."""
    env = dict(os.environ)
    env.pop("BRIDGE_STATE_DIR", None)
    for k in [k for k in env if k.startswith("HALO_")]:
        env.pop(k, None)
    env.pop("OLLAMA_API_KEY", None)
    return env


def _run_cli(fh, mock, prompt, *, model: str, extra_args=None, ollama_api_key=None, timeout=30):
    env = _hermetic_child_env()
    env.update({"BRIDGE_TEST_HOME": str(fh["home"]), "BRIDGE_TEST_NO_BACKGROUND_NET": "1",
                "OLLAMA_HOST": mock.base_url, "PYTHONPATH": str(REPO_DIR)})
    if ollama_api_key is not None:
        env["OLLAMA_API_KEY"] = ollama_api_key
    args = [sys.executable, "-m", "halo_harness", "-p", prompt, "--model", model,
            "--cwd", str(fh["proj"])] + (extra_args or [])
    return subprocess.run(args, env=env, cwd=str(REPO_DIR), capture_output=True, text=True, timeout=timeout)


def _chat_requests(mock) -> list:
    """Every recorded `/api/tags`/`/api/show` catalog read (the context-
    ownership rule's own foreground probe, round 2) is noise for these
    tests -- only the actual `/api/chat` turns matter here."""
    return [r for r in mock.requests if r["method"] == "POST" and r["path"].rstrip("/") == "/api/chat"]


@test
def test_ol_plain_text_prints_reply_and_wire_shape(ctx: Ctx):
    fh = build_fake_home()
    mock = MockUpstream().start()
    try:
        result = _run_cli(fh, mock, "say hi please", model="ol:plain-text")
        ctx.check(f"exit 0, got {result.returncode} stderr={result.stderr[-500:]!r}", result.returncode == 0)
        ctx.check(f"the scenario's text reached stdout, got {result.stdout!r}", "Hello!" in result.stdout)

        chats = _chat_requests(mock)
        ctx.check(f"exactly one POST /api/chat recorded, got {len(chats)}", len(chats) == 1)
        req = chats[0]
        body = req["body"]
        ctx.check(f"model on the wire, got {body.get('model')!r}", body.get("model") == "plain-text")
        ctx.check(f"stream true, got {body.get('stream')!r}", body.get("stream") is True)
        num_ctx = (body.get("options") or {}).get("num_ctx")
        ctx.check(f"options.num_ctx is an int, got {num_ctx!r}", isinstance(num_ctx, int))
        ctx.check(f"no keep_alive for an unconfigured host, got {body.get('keep_alive')!r}", "keep_alive" not in body)
        messages = body.get("messages") or []
        first_role = messages[0].get("role") if messages else None
        ctx.check(f"first message is role system, got {first_role!r}", first_role == "system")
        ctx.check("the user prompt text reached a user message",
                  any(m.get("role") == "user" and "say hi please" in str(m.get("content", "")) for m in messages))
        ctx.check(f"no authorization header (no api key configured), got {req['headers']!r}",
                  "authorization" not in req["headers"])
    finally:
        mock.stop()


@test
def test_ol_tool_round_trip_reaches_second_call(ctx: Ctx):
    fh = build_fake_home()
    note_path = fh["proj"] / "note.txt"
    note_path.write_text("OLLAMA_ROUNDTRIP_MARKER_TEXT\n", encoding="utf-8")
    mock = MockUpstream().start()
    try:
        mock.scenarios["tool-round-trip"] = ScriptedByCallCount([
            [{"message": {"role": "assistant", "content": "", "tool_calls": [
                {"function": {"name": "Read", "arguments": {"file_path": str(note_path)}}}]},
              "done": True, "done_reason": "stop", "prompt_eval_count": 20, "eval_count": 6}],
            [
                {"message": {"role": "assistant", "content": "Hel"}, "done": False},
                {"message": {"role": "assistant", "content": "lo!"}, "done": False},
                {"message": {"role": "assistant", "content": ""}, "done": True, "done_reason": "stop",
                 "prompt_eval_count": 10, "eval_count": 4},
            ],
        ])
        result = _run_cli(fh, mock, "please read the note file", model="ol:tool-round-trip",
                           extra_args=["--permission-mode", "auto"])
        ctx.check(f"exit 0, got {result.returncode} stderr={result.stderr[-500:]!r}", result.returncode == 0)
        ctx.check(f"the follow-up reply reached stdout, got {result.stdout!r}", "Hello!" in result.stdout)

        chats = _chat_requests(mock)
        ctx.check(f"two /api/chat calls (the tool call + the follow-up), got {len(chats)}", len(chats) == 2)
        second_messages = chats[1]["body"].get("messages") or []
        tool_msgs = [m for m in second_messages if m.get("role") == "tool"]
        ctx.check(f"a tool-role message is present on the SECOND call, got {second_messages!r}",
                  len(tool_msgs) == 1)
        ctx.check(f"tool_name is Read, got {tool_msgs[0].get('tool_name')!r}",
                  tool_msgs[0].get("tool_name") == "Read")
        ctx.check(f"the file's own text reached the tool result content, got {tool_msgs[0].get('content')!r}",
                  "OLLAMA_ROUNDTRIP_MARKER_TEXT" in str(tool_msgs[0].get("content", "")))
    finally:
        mock.stop()


@test
def test_ol_cloud_api_key_sends_bearer_header(ctx: Ctx):
    fh = build_fake_home()
    mock = MockUpstream().start()
    try:
        result = _run_cli(fh, mock, "say hi please", model="ol:plain-text", ollama_api_key="some-test-value")
        ctx.check(f"exit 0, got {result.returncode} stderr={result.stderr[-500:]!r}", result.returncode == 0)
        chats = _chat_requests(mock)
        ctx.check(f"exactly one POST /api/chat recorded, got {len(chats)}", len(chats) == 1)
        got_auth = chats[0]["headers"].get("authorization")
        ctx.check(f"authorization header carries the configured cloud key, got {got_auth!r}",
                  got_auth == "Bearer some-test-value")
    finally:
        mock.stop()


@test
def test_ol_compaction_summary_uses_native_body(ctx: Ctx):
    """Halo 2.0.3 round 2b: `_build_body_for_messages` (the compaction/
    summary call path, `_run_summary_call`) must build a native `/api/chat`
    body for an `ol:` session -- never an openai-chat body stashed as
    `prebuilt_ollama_body` (which `stream_ollama_completion` would then
    send wire-wrong: no `options.num_ctx`/`keep_alive`, a stray
    `max_tokens` field instead). Builds a real Session directly against a
    mock_ollama upstream -- same synthetic-history-then-`_run_compaction`
    approach tests/test_compaction_integration.py's own tests use -- then
    repeats it with a `compactionModel` override naming a DIFFERENT ol:
    model, proving `_compaction_model_override` passes THAT model's own
    ref/route/profile through to the same builder, not the main model's."""
    from halo_harness.agent.assemble import SessionContext
    from halo_harness.agent.compact import CompactionKnobs
    from halo_harness.agent.loop import Session
    from halo_harness.model import ModelProfile, parse_model_ref
    from halo_harness.permissions import PermissionEngine
    from halo_harness.providers.stream import ProviderCreds

    fh = build_fake_home()
    mock = MockUpstream().start()
    mock.scenarios["plain-text-v2"] = mock.scenarios["plain-text"]
    try:
        os.environ["BRIDGE_TEST_HOME"] = str(fh["home"])
        os.environ["OLLAMA_HOST"] = mock.base_url
        session_ctx = SessionContext(cwd=fh["proj"], model_label="ol:plain-text")
        session = Session(
            cwd=fh["proj"], model_ref=parse_model_ref("ol:plain-text"),
            model_profile=ModelProfile(context_tokens=10_000, max_output_tokens=1_000),
            creds=ProviderCreds(base_url=mock.base_url, api_key=""),
            state_dir=Path(tempfile.mkdtemp(prefix="ol-compact-")), model_label="ol:plain-text",
            session_context=session_ctx, max_turns=10,
            permission_engine=PermissionEngine(mode="auto", cwd=fh["proj"]),
        )
        filler = "x" * 400
        for i in range(60):
            session.log.append_user([{"type": "text", "text": f"question number {i} -- {filler}"}])
            session.log.append_assistant(content=[{"type": "text", "text": f"answer number {i}. {filler}"}],
                                          stop_reason="end_turn")

        events_seen = list(session._run_compaction(1, trigger="manual"))
        ctx.check(f"compaction completed, got phases {[e.data.get('phase') for e in events_seen if e.kind == 'compaction']}",
                  any(e.kind == "compaction" and e.data["phase"] == "done" for e in events_seen))

        chats = _chat_requests(mock)
        ctx.check(f"at least one /api/chat call recorded for the summary, got {len(chats)}", len(chats) >= 1)
        body = chats[-1]["body"]
        ctx.check(f"model on the wire is the ol: model, got {body.get('model')!r}", body.get("model") == "plain-text")
        num_ctx = (body.get("options") or {}).get("num_ctx")
        ctx.check(f"native body: options.num_ctx present, got {num_ctx!r}", isinstance(num_ctx, int))
        ctx.check(f"native body: no keep_alive for an unconfigured host, got {body.get('keep_alive')!r}",
                  "keep_alive" not in body)
        ctx.check("never an openai-chat max_tokens field on an ollama wire body", "max_tokens" not in body)

        mock.clear()
        session._compaction_knobs = CompactionKnobs(compaction_model="ol:plain-text-v2")
        events_seen2 = list(session._run_compaction(2, trigger="manual"))
        ctx.check("second (compactionModel-overridden) compaction completed",
                  any(e.kind == "compaction" and e.data["phase"] == "done" for e in events_seen2))
        chats2 = _chat_requests(mock)
        models_called = [c["body"].get("model") for c in chats2]
        ctx.check(f"the compactionModel's OWN model name reached the wire, got {models_called!r}",
                  "plain-text-v2" in models_called)
        ctx.check(f"the session's main model_ref is restored after the summary call, got {session.model_ref.raw!r}",
                  session.model_ref.raw == "ol:plain-text")
    finally:
        mock.stop()
        os.environ.pop("BRIDGE_TEST_HOME", None)
        os.environ.pop("OLLAMA_HOST", None)


if __name__ == "__main__":
    ctx = Ctx()
    results, passed, failed, skipped = run_all(TESTS, ctx)
    sys.exit(print_results(results, passed, failed, skipped))
