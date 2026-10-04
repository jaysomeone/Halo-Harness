"""tests.test_ollama_roles_local -- Halo 2.0.3 round 3: the model picker's
`u`-action plumbing (`roles.assign_role`/`default_role_for_ref`/`main_role_
consequence_note`) and `/local <question>`'s "never touches the main
transcript" contract (`commands.builtins._cmd_local`), against a real
`agent.loop.Session` built on a `mock_ollama.MockUpstream` for the one test
that actually needs a live small-model call.
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.helpers.provider_env_defaults import ensure_default_provider_credentials
ensure_default_provider_credentials()

from tests.helpers.runner import Ctx, new_registry, print_results, run_all
from tests.helpers.fake_home import build_fake_home
from tests.helpers.mock_ollama import MockUpstream

test, TESTS = new_registry()


def _fresh_state_dir(prefix: str) -> Path:
    d = Path(tempfile.mkdtemp(prefix=prefix))
    os.environ["BRIDGE_STATE_DIR"] = str(d)
    return d


def _clear_state_dir_env() -> None:
    os.environ.pop("BRIDGE_STATE_DIR", None)


class _FakeSession:
    """The two attributes `_cmd_local` actually reads -- avoids building a
    real, heavy `agent.loop.Session` for the tests that never reach
    `call_small_model` at all (the usage/no-session/wrong-provider early
    returns)."""

    def __init__(self, model_ref, small_model_ref=None):
        self.model_ref = model_ref
        self.small_model_ref = small_model_ref


# ---- roles.py: assign_role / default_role_for_ref / main_role_consequence_note

@test
def test_assign_role_writes_and_reads_back(ctx: Ctx):
    from halo_harness.roles import assign_role, configured_role_table
    _fresh_state_dir("ol-assign-role-")
    try:
        ok, problems = assign_role("small", "ol:qwen3:30b")
        ctx.check(f"assign_role succeeded, got {problems}", ok and not problems)
        ctx.check(f"readable back via configured_role_table, got {configured_role_table()}",
                  configured_role_table().get("small") == "ol:qwen3:30b")
    finally:
        _clear_state_dir_env()


@test
def test_assign_role_rejects_bad_role_name_and_blank_ref(ctx: Ctx):
    from halo_harness.roles import assign_role
    ok1, problems1 = assign_role("Not Valid", "ol:x")
    ctx.check(f"bad role name syntax rejected, got {(ok1, problems1)}", ok1 is False and problems1)
    ok2, problems2 = assign_role("small", "   ")
    ctx.check(f"blank model ref rejected, got {(ok2, problems2)}", ok2 is False and problems2)


@test
def test_default_role_for_ref_ol_is_small_others_main(ctx: Ctx):
    from halo_harness.roles import default_role_for_ref
    ctx.check("ol: ref defaults to small (a supporting role)", default_role_for_ref("ol:qwen3:30b") == "small")
    ctx.check("ol: with a host suffix still defaults to small",
              default_role_for_ref("ol:qwen3:30b@lan") == "small")
    ctx.check("a non-ollama ref defaults to orchestrator (main)",
              default_role_for_ref("or:vendor/model") == "orchestrator")


@test
def test_main_role_consequence_note_variants(ctx: Ctx):
    from halo_harness.roles import main_role_consequence_note
    ctx.check("declares tools -> no note",
              main_role_consequence_note("ol:x", catalog_capabilities=["tools"]) is None)
    ctx.check("unknown capability (not probed yet) -> no note (benefit of the doubt)",
              main_role_consequence_note("ol:x", catalog_capabilities=None) is None)
    note = main_role_consequence_note("ol:x", catalog_capabilities=["thinking"])
    ctx.check(f"no tools declared -> the plain consequence sentence, got {note!r}",
              note is not None and "cannot edit files" in note)
    ctx.check("never fires for a non-ollama ref",
              main_role_consequence_note("or:vendor/model", catalog_capabilities=[]) is None)


# ---- /local: never touches the main transcript --------------------------

@test
def test_local_cmd_usage_when_blank(ctx: Ctx):
    from halo_harness.commands.builtins import HeadlessFacade, _cmd_local
    out = _cmd_local("   ", HeadlessFacade(cwd=Path(".")))
    ctx.check(f"usage line, got {out!r}", out.startswith("Usage: /local"))


@test
def test_local_cmd_no_live_session(ctx: Ctx):
    from halo_harness.commands.builtins import HeadlessFacade, _cmd_local
    out = _cmd_local("anything", HeadlessFacade(cwd=Path(".")))
    ctx.check(f"plain no-session message, got {out!r}", "no live session" in out)


@test
def test_local_cmd_errors_plainly_when_small_role_not_ollama(ctx: Ctx):
    from halo_harness.commands.builtins import HeadlessFacade, _cmd_local
    from halo_harness.model import parse_model_ref
    fake = _FakeSession(model_ref=parse_model_ref("or:vendor/model"), small_model_ref=None)
    out = _cmd_local("anything", HeadlessFacade(cwd=Path("."), session=fake))
    ctx.check(f"names roles.small, got {out!r}", "roles.small" in out)


@test
def test_local_cmd_answers_without_touching_main_transcript(ctx: Ctx):
    from halo_harness.agent.assemble import SessionContext
    from halo_harness.agent.loop import Session
    from halo_harness.commands.builtins import HeadlessFacade, _cmd_local
    from halo_harness.model import ModelProfile, parse_model_ref
    from halo_harness.permissions import PermissionEngine
    from halo_harness.providers.stream import ProviderCreds

    fh = build_fake_home()
    mock = MockUpstream().start()
    mock.scenarios["small-reply"] = mock.scenarios["plain-text"]
    try:
        os.environ["BRIDGE_TEST_HOME"] = str(fh["home"])
        os.environ["OLLAMA_HOST"] = mock.base_url
        session_ctx = SessionContext(cwd=fh["proj"], model_label="ol:plain-text")
        session = Session(
            cwd=fh["proj"], model_ref=parse_model_ref("ol:plain-text"),
            model_profile=ModelProfile(context_tokens=10_000, max_output_tokens=1_000),
            creds=ProviderCreds(base_url=mock.base_url, api_key=""),
            state_dir=Path(tempfile.mkdtemp(prefix="ol-local-")), model_label="ol:plain-text",
            session_context=session_ctx, max_turns=10,
            permission_engine=PermissionEngine(mode="auto", cwd=fh["proj"]),
            small_model_ref=parse_model_ref("ol:small-reply"),
        )
        session.log.append_user([{"type": "text", "text": "the real conversation"}])
        before = len(session.log.nodes())

        answer = _cmd_local("say hi please", HeadlessFacade(cwd=fh["proj"], session=session))

        ctx.check(f"answer contains the scripted reply, got {answer!r}", "Hello!" in answer)
        after = len(session.log.nodes())
        ctx.check(f"the main transcript is UNCHANGED, before={before} after={after}", before == after)
        chats = [r for r in mock.requests if r["method"] == "POST" and r["path"].rstrip("/") == "/api/chat"]
        ctx.check(f"exactly one /api/chat call, got {len(chats)}", len(chats) == 1)
        ctx.check(f"the SMALL model's own name reached the wire, got {chats[0]['body'].get('model')!r}",
                  chats[0]["body"].get("model") == "small-reply")
    finally:
        mock.stop()
        os.environ.pop("BRIDGE_TEST_HOME", None)
        os.environ.pop("OLLAMA_HOST", None)


if __name__ == "__main__":
    ctx = Ctx()
    results, passed, failed, skipped = run_all(TESTS, ctx)
    sys.exit(print_results(results, passed, failed, skipped))
