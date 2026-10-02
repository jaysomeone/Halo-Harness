"""tests.test_bugreport -- 2.0.1 W3a: `halo bugreport`/`/bugreport` ("one
paste instead of screenshots") and the per-turn timeline (`agent.loop.
Session.turn`'s own recording wrapper, `/timeline`, `halo timeline --last
N`). The critical test here is `test_bugreport_redacts_a_planted_fake_key_
in_every_source`: a fake secret-shaped key is planted in the environment, a
settings env block, config.json, bridge.log, and a fake session log's own
tool_result content -- the assembled report must never contain it, in any
of its several forms (raw, JSON-escaped, as a Bearer header, as a bare
token-shaped run).
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.helpers.runner import Ctx, new_registry, print_results, run_all

test, TESTS = new_registry()

_PROVIDER_ENV_VARS = (
    "OPENROUTER_API_KEY", "OPENROUTER_MANAGEMENT_KEY", "BRIDGE_OPENROUTER_BASE_URL",
    "DATABRICKS_HOST", "DATABRICKS_TOKEN", "BRIDGE_DBX_BASE_URL", "BRIDGE_DBX_TOKEN",
    "ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN", "BRIDGE_ANTHROPIC_BASE_URL",
    "TYPESAFE_API_KEY", "HALO_MODEL", "BRIDGE_MODEL", "ROLO_CLAUDE_MODEL", "BRIDGE_TEST_CC_AUTH_STATUS",
)

_FAKE_KEY = "sk-or-v1-FAKEFAKEFAKEFAKEFAKEFAKEFAKEFAKE1234567890abcdef"


class _Env:
    def __enter__(self):
        self._saved = {k: os.environ.get(k) for k in
                       (("BRIDGE_TEST_HOME", "BRIDGE_STATE_DIR", "BRIDGE_ENV_FILE") + _PROVIDER_ENV_VARS)}
        d = Path(tempfile.mkdtemp(prefix="bugreport-"))
        os.environ["BRIDGE_TEST_HOME"] = str(d)
        os.environ["BRIDGE_STATE_DIR"] = str(d / ".halo")
        os.environ["BRIDGE_ENV_FILE"] = str(d / "no-env-file")
        for k in _PROVIDER_ENV_VARS:
            os.environ.pop(k, None)
        os.environ["BRIDGE_TEST_CC_AUTH_STATUS"] = json.dumps({"loggedIn": False})
        self.home = d
        self.state_dir = d / ".halo"
        return self

    def __exit__(self, *exc):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


# ---------------------------------------------------------------------------
# The critical test: a fake key planted in EVERY source must never appear.
# ---------------------------------------------------------------------------

@test
def test_bugreport_redacts_a_planted_fake_key_in_every_source(ctx: Ctx):
    from halo_harness.agent.log import SessionLog
    from halo_harness.bugreport import _LogOnlySession, build_bugreport_text
    from halo_harness.config.settings import Settings, SettingsLayer

    with _Env() as env:
        cwd = env.home / "proj"
        cwd.mkdir(parents=True, exist_ok=True)

        # 1. Shell environment (a REAL secret env var name).
        os.environ["OPENROUTER_API_KEY"] = _FAKE_KEY

        # 2. config.json containing it (simulating an accidental write).
        env.state_dir.mkdir(parents=True, exist_ok=True)
        (env.state_dir / "config.json").write_text(
            json.dumps({"model": "or:test/model", "notes": f"OPENROUTER_API_KEY={_FAKE_KEY}"}), encoding="utf-8")

        # 3. bridge.log containing it, plus a Bearer-header form and a
        # JSON-escaped-quoted form (the shape sanitize_node's own
        # json.dumps round trip produces).
        (env.state_dir / "bridge.log").write_text(
            f"2026-10-01 12:00:00 DEBUG upstream request Authorization: Bearer {_FAKE_KEY}\n"
            f"2026-10-01 12:00:01 DEBUG env snapshot \"OPENROUTER_API_KEY\": \"{_FAKE_KEY}\"\n"
            f"2026-10-01 12:00:02 DEBUG raw bare token {_FAKE_KEY}\n",
            encoding="utf-8")

        # 4. A settings env block carrying it (the "settings env" reason
        # path, and bugreport's own settings-source listing must still
        # only ever print the PATH, never this value).
        settings_dir = cwd / ".claude"
        settings_dir.mkdir(parents=True, exist_ok=True)
        settings_path = settings_dir / "settings.json"
        settings_path.write_text(json.dumps({"env": {"OPENROUTER_API_KEY": _FAKE_KEY}}), encoding="utf-8")
        layer = SettingsLayer(name="project", path=settings_path, base_dir=cwd,
                               data={"env": {"OPENROUTER_API_KEY": _FAKE_KEY}})
        settings = Settings(raw={"env": {"OPENROUTER_API_KEY": _FAKE_KEY}}, layers=[layer], errors=[])

        # 5. A fake session log whose own tool_result content embeds it
        # (what a Bash `env`/`printenv` the model actually ran would log).
        log = SessionLog(cwd)
        log.append_user([{"type": "text", "text": "what's my openrouter key"}])
        log.append_assistant(content=[{"type": "tool_use", "id": "tu1", "name": "Bash",
                                        "input": {"command": "env | grep OPENROUTER"}}])
        log.append_user([{"type": "tool_result", "tool_use_id": "tu1",
                           "content": [{"type": "text", "text": f"OPENROUTER_API_KEY={_FAKE_KEY}"}]}])
        session = _LogOnlySession(log)

        text = build_bugreport_text(session=session, settings=settings, state_dir=env.state_dir, cwd=cwd,
                                     include_content=True)

        ctx.check("the report is non-trivial (sanity: sources were actually read)", len(text) > 200)
        ctx.check(f"the fake key never appears anywhere in the report", _FAKE_KEY not in text)
        ctx.check("the settings path IS shown (paths only, not values)", str(settings_path) in text)
        ctx.check("a <redacted> marker appears where the key would have been", "<redacted>" in text)


# ---------------------------------------------------------------------------
# redact.redact_for_bugreport's own extra capabilities.
# ---------------------------------------------------------------------------

@test
def test_redact_for_bugreport_catches_a_long_hex_run(ctx: Ctx):
    from halo_harness.redact import redact_for_bugreport
    text = "token=" + "a" * 40
    out = redact_for_bugreport(text)
    ctx.check(f"a 40-char hex-shaped run is redacted, got {out!r}", "aaaa" not in out and "<redacted>" in out)


@test
def test_redact_for_bugreport_catches_a_long_base64_run(ctx: Ctx):
    from halo_harness.redact import redact_for_bugreport
    fake_b64 = "QWxhZGRpbjpvcGVuIHNlc2FtZQQWxhZGRpbjpvcGVuIHNlc2FtZQ=="
    out = redact_for_bugreport(f"value: {fake_b64}")
    ctx.check(f"a 40+ char base64-shaped run is redacted, got {out!r}", fake_b64 not in out)


@test
def test_redact_for_bugreport_catches_a_known_value_with_no_recognizable_shape(ctx: Ctx):
    from halo_harness.redact import redact_for_bugreport
    weird_value = "correct-horse-battery-staple"  # no sk-/dapi/hex/base64 shape at all
    out = redact_for_bugreport(f"the configured secret is {weird_value}", known_secret_values=(weird_value,))
    ctx.check(f"caught purely by value, got {out!r}", weird_value not in out and "<redacted>" in out)


@test
def test_redact_for_bugreport_never_blanket_replaces_a_trivially_short_value(ctx: Ctx):
    from halo_harness.redact import redact_for_bugreport
    out = redact_for_bugreport("the count is 12 today", known_secret_values=("12",))
    ctx.check(f"a 2-char 'secret' never nukes an unrelated number, got {out!r}", "12" in out)


@test
def test_export_cli_still_exposes_the_same_names_after_the_move(ctx: Ctx):
    """2.0.1 W3a: redact.py is the new canonical home -- every existing
    `from halo_harness.export_cli import ...` must keep working unchanged."""
    from halo_harness.export_cli import _BEARER_RE, _SECRET_ENV_NAMES, _TOKEN_PATTERNS, sanitize_node, sanitize_text
    ctx.check("sanitize_text still redacts a known shape",
              "<redacted>" in sanitize_text("sk-ant-" + "x" * 20))
    ctx.check("_SECRET_ENV_NAMES still has the expected names", "OPENROUTER_API_KEY" in _SECRET_ENV_NAMES)
    ctx.check("sanitize_node is callable", callable(sanitize_node))
    ctx.check("_TOKEN_PATTERNS/_BEARER_RE still present", len(_TOKEN_PATTERNS) > 0 and _BEARER_RE is not None)


# ---------------------------------------------------------------------------
# halo bugreport (CLI) / state.json-style placement under ~/.halo only.
# ---------------------------------------------------------------------------

@test
def test_cmd_bugreport_writes_under_halo_home_only(ctx: Ctx):
    from halo_harness.bugreport import cmd_bugreport
    with _Env() as env, tempfile.TemporaryDirectory() as cwd:
        rc = cmd_bugreport(["--cwd", cwd])
        ctx.check(f"exit 0, got {rc}", rc == 0)
        reports_dir = env.state_dir / "bugreports"
        ctx.check(f"a report landed under ~/.halo/bugreports, got {list(reports_dir.glob('*.md'))}",
                  reports_dir.is_dir() and len(list(reports_dir.glob("*.md"))) == 1)


@test
def test_cmd_bugreport_out_flag_writes_to_the_named_file(ctx: Ctx):
    from halo_harness.bugreport import cmd_bugreport
    with _Env() as env, tempfile.TemporaryDirectory() as cwd:
        out_path = Path(cwd) / "my-report.md"
        rc = cmd_bugreport(["--cwd", cwd, "--out", str(out_path)])
        ctx.check(f"exit 0, got {rc}", rc == 0)
        ctx.check("the named file was written", out_path.exists())


@test
def test_bugreport_and_timeline_are_registered_builtin_slash_commands(ctx: Ctx):
    from halo_harness.commands.builtins import _BUILTIN_SPECS
    ctx.check("/bugreport is registered", "bugreport" in _BUILTIN_SPECS)
    ctx.check("/timeline is registered", "timeline" in _BUILTIN_SPECS)


@test
def test_cmd_timeline_builtin_reports_nothing_recorded_with_a_bare_facade(ctx: Ctx):
    from halo_harness.commands.builtins import HeadlessFacade, _cmd_timeline
    from halo_harness import debug_timeline
    debug_timeline.reset_turns()
    facade = HeadlessFacade(cwd=Path("."))
    result = _cmd_timeline("", facade)
    ctx.check(f"says nothing recorded, got {result!r}", "No turns recorded" in result)


# ---------------------------------------------------------------------------
# Per-turn timeline recording itself.
# ---------------------------------------------------------------------------

@test
def test_debug_timeline_records_phases_tools_and_message_end(ctx: Ctx):
    from halo_harness import debug_timeline, events as ev
    debug_timeline.reset_turns()
    debug_timeline.start_turn(1)
    debug_timeline.record_turn_event(ev.phase(state="request_sent", turn=1))
    debug_timeline.record_turn_event(ev.phase(state="headers", turn=1, ttfb_ms=5.0))
    debug_timeline.record_turn_event(ev.phase(state="first_token", turn=1, kind="text"))
    debug_timeline.record_turn_event(ev.Event("tool_use_ready", {"id": "tu1", "name": "Bash"}, turn=1))
    debug_timeline.record_turn_event(ev.Event("tool_result", {"id": "tu1", "ok": True}, turn=1))
    debug_timeline.record_turn_event(ev.turn_done(turn=1))
    record = debug_timeline.end_turn()

    ctx.check(f"request_sent_ms recorded, got {record}", record["request_sent_ms"] is not None)
    ctx.check("headers_ms recorded", record["headers_ms"] is not None)
    ctx.check("first_text_ms recorded (not first_reasoning/tool)",
              record["first_text_ms"] is not None and record["first_reasoning_ms"] is None
              and record["first_tool_call_ms"] is None)
    ctx.check(f"exactly one tool recorded ok, got {record['tools']}",
              len(record["tools"]) == 1 and record["tools"][0]["name"] == "Bash"
              and record["tools"][0]["status"] == "ok")
    ctx.check("message_end_ms recorded", record["message_end_ms"] is not None)
    ctx.check("last_turn() returns the same record", debug_timeline.last_turn() == record)


@test
def test_debug_timeline_keeps_only_the_last_20_turns(ctx: Ctx):
    from halo_harness import debug_timeline, events as ev
    debug_timeline.reset_turns()
    for i in range(25):
        debug_timeline.start_turn(i)
        debug_timeline.record_turn_event(ev.turn_done(turn=i))
        debug_timeline.end_turn()
    kept = debug_timeline.last_n_turns(100)
    ctx.check(f"capped at 20, got {len(kept)}", len(kept) == 20)
    ctx.check(f"the OLDEST 5 were dropped (FIFO), got turns={[r['turn'] for r in kept][:3]}",
              kept[0]["turn"] == 5)


@test
def test_session_turn_writes_one_meta_timeline_node_per_turn_never_model_visible(ctx: Ctx):
    """2.0.1 W3a: `Session.turn`'s own wrapper -- the recorded timeline
    lands as a `meta` node (never a `snapshot`, which derive_request WOULD
    replay to the model as a user-role message)."""
    from halo_harness import debug_timeline
    from tests.helpers.mock_openai import MockUpstream, SCENARIOS, ScriptedTurns

    def _final_text_chunk(text: str) -> list:
        return [
            {"choices": [{"index": 0, "delta": {"role": "assistant"}}]},
            {"choices": [{"index": 0, "delta": {"content": text}}]},
            {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
        ]

    SCENARIOS["bugreport-timeline-turn"] = ScriptedTurns([_final_text_chunk("hi there")])
    mock = MockUpstream().start()
    debug_timeline.reset_turns()
    try:
        with _Env(), tempfile.TemporaryDirectory() as cwd:
            os.environ["OPENROUTER_API_KEY"] = "test-key-not-real"
            os.environ["BRIDGE_OPENROUTER_BASE_URL"] = mock.base_url
            from halo_harness import headless
            build = headless.build_session(cwd=Path(cwd), model_ref_raw="or:mock/bugreport-timeline-turn",
                                            bare=True, print_mode=True, max_turns=3)
            session = build.session
            list(session.turn("hello"))

            record = debug_timeline.last_turn()
            ctx.check(f"a timeline record was captured, got {record}", record is not None)

            nodes = session.log.read_all()
            meta_timeline_nodes = [n for n in nodes if n.get("type") == "meta" and "timeline" in n]
            ctx.check(f"exactly one meta/timeline node written, got {len(meta_timeline_nodes)}",
                      len(meta_timeline_nodes) == 1)

            from halo_harness.agent.derive import derive_request
            _system_text, messages, _tools_out = derive_request(session.log)
            serialized = json.dumps(messages)
            ctx.check(f"the timeline record is NEVER reconstructed into a model-visible message, got a match? "
                      f"{'request_sent_ms' in serialized}", "request_sent_ms" not in serialized)

            # `halo timeline --last N` (a SEPARATE process in real life, but
            # exercised in-process here): reads the SAME meta/timeline node
            # back from the log file on disk, independent of debug_
            # timeline's own in-memory deque.
            from halo_harness.bugreport_timeline_cli import cmd_timeline, read_timeline_records
            on_disk = read_timeline_records(Path(cwd))
            ctx.check(f"exactly one record read back from disk, got {on_disk}", len(on_disk) == 1)
            rc = cmd_timeline(["--cwd", cwd])
            ctx.check(f"halo timeline --cwd exits 0 with a real session present, got {rc}", rc == 0)
    finally:
        mock.stop()


@test
def test_cli_bugreport_loads_the_env_file_and_reports_launch_defaults(ctx: Ctx):
    """`halo bugreport` from a shell (no live session) must see a key that
    lives only in the env file, the way `halo providers` and doctor do
    (seen live on the Kali VM: every provider read "disabled"), name the
    env file as the reason, report the permission mode and the model the
    next launch would use instead of "?", and still never print the key."""
    import contextlib
    import io
    from halo_harness.bugreport import cmd_bugreport

    with _Env() as env:
        cwd = env.home / "proj"
        cwd.mkdir(parents=True, exist_ok=True)
        env_file = env.home / "env-file"
        env_file.write_text(f"OPENROUTER_API_KEY={_FAKE_KEY}\n", encoding="utf-8")
        os.environ["BRIDGE_ENV_FILE"] = str(env_file)
        env.state_dir.mkdir(parents=True, exist_ok=True)
        out = env.home / "report.md"
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            rc = cmd_bugreport(["--out", str(out), "--cwd", str(cwd)])
        text = out.read_text(encoding="utf-8")
        ctx.check(f"exit 0, got {rc}", rc == 0)
        ctx.check("openrouter reads as enabled from the env file",
                  "openrouter: enabled (env file)" in text)
        ctx.check("permission mode is resolved, never '?'",
                  "Permission mode:" in text and "Permission mode: ?" not in text)
        ctx.check("the route block names what the next launch would use",
                  "next launch" in text and "default model:" in text and "last used here" in text)
        ctx.check("the env-file key never appears in the report", _FAKE_KEY not in text)


if __name__ == "__main__":
    ctx = Ctx()
    results, passed, failed, skipped = run_all(TESTS, ctx)
    sys.exit(print_results(results, passed, failed, skipped))
