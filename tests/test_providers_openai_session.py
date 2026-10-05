"""tests.test_providers_openai_session -- Halo 2.0.3 round 5i part 1: the
`oai:` route wired into the real agent loop end to end through the real
`-p` CLI entry point, both dialects, against the parameterized
tests.helpers.mock_openai fake (same subprocess-per-test runner style as
tests/test_providers_huggingface_session.py/tests/
test_providers_ollama_session.py) -- plus `/providers`' spend-text note
and the init wizard's OpenAI tab Save path. No OpenAI key exists on this
machine and none is requested here: this fake IS the verification; see
docs/harness/OPENAI-RESEARCH.md and docs/MODELS.md's own "unverified
against the real API" sentence.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import tests.helpers.mock_openai as mock_openai_mod
from tests.helpers.fake_home import build_fake_home
from tests.helpers.mock_openai import MockUpstream, ScriptedByCallCount
from tests.helpers.runner import Ctx, new_registry, print_results, run_all

REPO_DIR = Path(__file__).resolve().parent.parent
test, TESTS = new_registry()


def _hermetic_child_env() -> dict:
    env = dict(os.environ)
    env.pop("BRIDGE_STATE_DIR", None)
    for k in [k for k in env if k.startswith("HALO_")]:
        env.pop(k, None)
    env.pop("OPENAI_API_KEY", None)
    return env


def _run_cli(fh, prompt, *, model: str, extra_env=None, extra_args=None, timeout=30):
    env = _hermetic_child_env()
    env.update({"BRIDGE_TEST_HOME": str(fh["home"]), "BRIDGE_TEST_NO_BACKGROUND_NET": "1",
                "PYTHONPATH": str(REPO_DIR)})
    env.update(extra_env or {})
    args = [sys.executable, "-m", "halo_harness", "-p", prompt, "--model", model,
            "--cwd", str(fh["proj"])] + (extra_args or [])
    return subprocess.run(args, env=env, cwd=str(REPO_DIR), capture_output=True, text=True, timeout=timeout)


def _seed_config(fh, key: str, value) -> None:
    old_home = os.environ.get("BRIDGE_TEST_HOME")
    old_state_dir = os.environ.get("BRIDGE_STATE_DIR")
    os.environ["BRIDGE_TEST_HOME"] = str(fh["home"])
    os.environ.pop("BRIDGE_STATE_DIR", None)
    try:
        from halo_harness.theme import set_config_value
        set_config_value(key, value)
    finally:
        if old_home is None:
            os.environ.pop("BRIDGE_TEST_HOME", None)
        else:
            os.environ["BRIDGE_TEST_HOME"] = old_home
        if old_state_dir is not None:
            os.environ["BRIDGE_STATE_DIR"] = old_state_dir


def _posts(mock, path_suffix: str) -> list:
    return [r for r in mock.requests if r["method"] == "POST" and r["path"].rstrip("/").endswith(path_suffix)]


# ---- chat dialect, end to end -----------------------------------------------

@test
def test_chat_dialect_end_to_end_bearer_and_wire_shape(ctx: Ctx):
    fh = build_fake_home()
    mock = MockUpstream(path_prefix="/v1", expected_bearer="chat-test-token").start()
    try:
        result = _run_cli(fh, "say hi please", model="oai:mock/model",
                           extra_env={"OPENAI_API_KEY": "chat-test-token", "HALO_OPENAI_BASE_URL": mock.base_url})
        ctx.check(f"exit 0, got {result.returncode} stderr={result.stderr[-500:]!r}", result.returncode == 0)
        ctx.check(f"the scenario's reply reached stdout, got {result.stdout!r}", "pong" in result.stdout)

        chats = _posts(mock, "/chat/completions")
        ctx.check(f"exactly one chat-completions call recorded, got {len(chats)}", len(chats) == 1)
        req = chats[0]
        ctx.check(f"model on the wire, got {req['body'].get('model')!r}", req["body"].get("model") == "mock/model")
        ctx.check(f"bearer is OPENAI_API_KEY, got {req['headers'].get('authorization')!r}",
                  req["headers"].get("authorization") == "Bearer chat-test-token")
    finally:
        mock.stop()


# ---- responses dialect, end to end, with a Read tool round trip ------------

@test
def test_responses_dialect_end_to_end_tool_round_trip(ctx: Ctx):
    fh = build_fake_home()
    note_path = fh["proj"] / "note.txt"
    note_path.write_text("OPENAI_RESPONSES_ROUNDTRIP_MARKER\n", encoding="utf-8")
    mock = MockUpstream(path_prefix="/v1", expected_bearer="resp-test-token").start()
    try:
        scenario_name = "oai-resp-round-trip"
        mock_openai_mod.RESPONSES_SCENARIOS[scenario_name] = ScriptedByCallCount([
            [
                {"type": "response.output_item.added", "output_index": 0,
                 "item": {"type": "function_call", "id": "fc1", "call_id": "call1", "name": "Read", "arguments": ""}},
                {"type": "response.function_call_arguments.done", "item_id": "fc1", "output_index": 0,
                 "arguments": json.dumps({"file_path": str(note_path)})},
                {"type": "response.output_item.done", "output_index": 0,
                 "item": {"type": "function_call", "id": "fc1", "call_id": "call1", "name": "Read",
                          "arguments": json.dumps({"file_path": str(note_path)})}},
                {"type": "response.completed",
                 "response": {"id": "r1", "status": "completed", "usage": {"input_tokens": 20, "output_tokens": 6}}},
            ],
            [
                {"type": "response.output_item.added", "output_index": 0,
                 "item": {"type": "message", "id": "m1", "role": "assistant", "content": []}},
                {"type": "response.output_text.delta", "item_id": "m1", "output_index": 0, "delta": "Hello!"},
                {"type": "response.output_item.done", "output_index": 0,
                 "item": {"type": "message", "id": "m1", "role": "assistant",
                          "content": [{"type": "output_text", "text": "Hello!"}]}},
                {"type": "response.completed",
                 "response": {"id": "r2", "status": "completed", "usage": {"input_tokens": 30, "output_tokens": 4}}},
            ],
        ])
        _seed_config(fh, "openai.dialect_overrides", {f"mock/{scenario_name}": "responses"})

        result = _run_cli(fh, "please read the note file", model=f"oai:mock/{scenario_name}",
                           extra_env={"OPENAI_API_KEY": "resp-test-token", "HALO_OPENAI_BASE_URL": mock.base_url},
                           extra_args=["--permission-mode", "auto"])
        ctx.check(f"exit 0, got {result.returncode} stderr={result.stderr[-500:]!r}", result.returncode == 0)
        ctx.check(f"the follow-up reply reached stdout, got {result.stdout!r}", "Hello!" in result.stdout)

        calls = _posts(mock, "/responses")
        ctx.check(f"two /v1/responses calls (the tool call + the follow-up), got {len(calls)}", len(calls) == 2)
        ctx.check(f"bearer is OPENAI_API_KEY, got {calls[0]['headers'].get('authorization')!r}",
                  calls[0]["headers"].get("authorization") == "Bearer resp-test-token")
        ctx.check("store:false on every request", all(c["body"].get("store") is False for c in calls))
        ctx.check("previous_response_id never sent", all("previous_response_id" not in c["body"] for c in calls))

        second_input = calls[1]["body"].get("input") or []
        outputs = [i for i in second_input if i.get("type") == "function_call_output"]
        ctx.check(f"a function_call_output item is present on the SECOND call, got {second_input!r}",
                  len(outputs) == 1)
        ctx.check(f"call_id matches the first call's own id, got {outputs[0].get('call_id')!r}",
                  outputs[0].get("call_id") == "call1")
        ctx.check(f"the file's own text reached the tool result content, got {outputs[0].get('output')!r}",
                  "OPENAI_RESPONSES_ROUNDTRIP_MARKER" in str(outputs[0].get("output", "")))
    finally:
        mock.stop()


# ---- error scenarios (through the real mock server) -------------------------

@test
def test_responses_error_scenarios_surface_plain_messages(ctx: Ctx):
    from pathlib import Path as _Path
    from halo_harness.providers.responses_stream import ResponsesStreamToAnthropic
    from halo_harness.providers.routing import Route
    from halo_harness.providers.stream import CompletionRequest, ProviderCreds, UpstreamError, _run_phase1_responses
    mock = MockUpstream(path_prefix="/v1").start()
    try:
        state_dir = _Path(tempfile.mkdtemp(prefix="oai-err-"))
        route = Route(provider="openai", upstream_model="mock-err", dialect="openai-responses")
        for scenario, expect_status, expect_substr in (
            ("oai-responses-401", 401, "Incorrect API key"),
            ("oai-responses-429-quota", 429, "exceeded your current quota"),
            ("oai-responses-404-model", 404, "does not exist"),
        ):
            creds = ProviderCreds(base_url=mock.base_url, api_key="irrelevant")
            req = CompletionRequest(body={}, route=route, profile={}, creds=creds, state_dir=state_dir,
                                     extra_headers={}, model_label="oai:mock-err",
                                     prebuilt_responses_body={"model": f"mock/{scenario}"})
            try:
                _run_phase1_responses(req)
                ctx.check(f"{scenario} must raise UpstreamError", False)
            except UpstreamError as e:
                ctx.check(f"{scenario}: status {expect_status}, got {e.status}", e.status == expect_status)
                ctx.check(f"{scenario}: plain message contains {expect_substr!r}, got {e.message!r}",
                          expect_substr in e.message)
    finally:
        mock.stop()


# ---- /providers spend text ---------------------------------------------------

@test
def test_providers_table_notes_no_balance_endpoint_when_enabled(ctx: Ctx):
    from halo_harness.providers_cli import format_providers_table
    enabled_row = [{"name": "openai", "label": "OpenAI API (key)", "enabled": True, "status": "auto (detected from env)",
                    "credentials": True, "credentials_source": "env", "reachable": "reachable", "model_count": 0}]
    text = format_providers_table(enabled_row)
    ctx.check("says plainly there is no public balance endpoint",
              "no public balance endpoint" in text and "OpenAI API" in text)
    ctx.check("points at computed spend when no session number is given",
              "computed" in text or "/cost" in text)
    with_number = format_providers_table(enabled_row, openai_spend_line="this session: $0.1234 across 3 turn(s)")
    ctx.check("shows the live computed number when a session has one", "$0.1234" in with_number)

    disabled_row = [{**enabled_row[0], "enabled": False}]
    text_disabled = format_providers_table(disabled_row)
    ctx.check("no note at all when the provider isn't enabled",
              "no public balance endpoint" not in text_disabled)


@test
def test_providers_cli_shows_the_note_end_to_end(ctx: Ctx):
    """`halo providers list` (the standalone CLI, no live session) with a
    real OPENAI_API_KEY in its env -- the common case (checking status,
    not mid-turn) -- shows the plain sentence."""
    fh = build_fake_home()
    env = _hermetic_child_env()
    env.update({"BRIDGE_TEST_HOME": str(fh["home"]), "BRIDGE_TEST_NO_BACKGROUND_NET": "1",
                "PYTHONPATH": str(REPO_DIR), "OPENAI_API_KEY": "sk-cli-test",
                "BRIDGE_TEST_CC_AUTH_STATUS": json.dumps({"loggedIn": False})})
    result = subprocess.run([sys.executable, "-m", "halo_harness", "providers", "list"],
                             env=env, cwd=str(REPO_DIR), capture_output=True, text=True, timeout=30)
    ctx.check(f"exit 0, got {result.returncode} stderr={result.stderr[-500:]!r}", result.returncode == 0)
    ctx.check(f"OpenAI row present and enabled, got {result.stdout!r}", "OpenAI API (key)" in result.stdout)
    ctx.check("the no-balance-endpoint note is printed", "no public balance endpoint" in result.stdout)


# ---- init wizard tab Save ----------------------------------------------------

@test
def test_openai_tab_save_and_state_round_trip(ctx: Ctx):
    import tempfile as _tempfile
    d = Path(_tempfile.mkdtemp(prefix="oai-tab-"))
    saved = {k: os.environ.get(k) for k in ("BRIDGE_TEST_HOME", "BRIDGE_STATE_DIR", "BRIDGE_ENV_FILE", "OPENAI_API_KEY")}
    os.environ["BRIDGE_TEST_HOME"] = str(d)
    os.environ["BRIDGE_STATE_DIR"] = str(d / ".halo")
    os.environ["BRIDGE_ENV_FILE"] = str(d / "env-file")
    os.environ.pop("OPENAI_API_KEY", None)
    try:
        from halo_harness.init_providers import TAB_LABEL, TAB_PROVIDERS, save_tab_credentials, tab_credential_state
        ctx.check("openai is a registered tab", "openai" in TAB_PROVIDERS)
        ctx.check(f"tab label, got {TAB_LABEL['openai']!r}", TAB_LABEL["openai"] == "OpenAI API (key)")

        state_before = tab_credential_state("openai")
        ctx.check("not configured before Save", state_before["configured"] is False)
        ctx.check(f"one key field offered, got {state_before['fields']!r}",
                  len(state_before["fields"]) == 1 and state_before["fields"][0]["name"] == "key"
                  and state_before["fields"][0]["secret"] is True)

        ok, msg = save_tab_credentials("openai", {"key": "sk-wizard-test"})
        ctx.check(f"Save succeeds, got ok={ok!r} msg={msg!r}", ok is True and "OPENAI_API_KEY" in msg)

        state_after = tab_credential_state("openai")
        ctx.check("configured after Save", state_after["configured"] is True)
        ctx.check(f"masked value doesn't leak the real key, got {state_after['masked']!r}",
                  state_after["masked"] is not None and "sk-wizard-test" not in state_after["masked"])

        ok_blank, msg_blank = save_tab_credentials("openai", {"key": ""})
        ctx.check(f"a blank Save refuses plainly, got ok={ok_blank!r} msg={msg_blank!r}",
                  ok_blank is False and "OPENAI_API_KEY" in msg_blank)
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


if __name__ == "__main__":
    ctx = Ctx()
    results, passed, failed, skipped = run_all(TESTS, ctx)
    sys.exit(print_results(results, passed, failed, skipped))
