"""tests.test_providers_huggingface_session -- Halo 2.0.3 round 4: a
wire-shape pinning test for the router's `:suffix` passthrough, plus the
two print-mode end-to-end runs the brief asks for (router and dedicated
endpoint) through the real `-p` CLI entry point -- same subprocess-per-
test runner style as tests/test_providers_ollama_session.py, a
`tests.helpers.mock_openai.MockUpstream` standing in for the real router
(`BRIDGE_HF_ROUTER_BASE_URL`) or a configured `huggingface.endpoints`
entry's own url.
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.helpers.fake_home import build_fake_home
from tests.helpers.mock_openai import MockUpstream
from tests.helpers.runner import Ctx, new_registry, print_results, run_all

REPO_DIR = Path(__file__).resolve().parent.parent
test, TESTS = new_registry()


@test
def test_wire_model_field_carries_router_suffix_verbatim(ctx: Ctx):
    """Unit-level: `route.upstream_model` (== `ModelRef.model`, which
    `parse_model_ref` keeps the `:fastest`/`:cheapest`/`:preferred`/
    `:<provider>` suffix on verbatim) reaches `body["model"]` through the
    SAME shared `build_request_body` every openai-chat-dialect provider
    uses -- no huggingface-specific code in that path at all."""
    from halo_harness.providers.profiles import resolve_profile
    from halo_harness.providers.request import build_request_body
    from halo_harness.providers.routing import Route
    route = Route(provider="huggingface", upstream_model="openai/gpt-oss-120b:groq", dialect="openai-chat")
    profile = resolve_profile(route)
    body = build_request_body(system_text="", messages=[], route=route, profile=profile)
    ctx.check(f"model on the wire carries the provider suffix verbatim, got {body.get('model')!r}",
              body.get("model") == "openai/gpt-oss-120b:groq")


def _hermetic_child_env() -> dict:
    env = dict(os.environ)
    env.pop("BRIDGE_STATE_DIR", None)
    for k in [k for k in env if k.startswith("HALO_")]:
        env.pop(k, None)
    env.pop("HF_TOKEN", None)
    return env


def _run_cli(fh, prompt, *, model: str, extra_env=None, timeout=30):
    env = _hermetic_child_env()
    env.update({"BRIDGE_TEST_HOME": str(fh["home"]), "BRIDGE_TEST_NO_BACKGROUND_NET": "1",
                "PYTHONPATH": str(REPO_DIR)})
    env.update(extra_env or {})
    args = [sys.executable, "-m", "halo_harness", "-p", prompt, "--model", model,
            "--cwd", str(fh["proj"])]
    return subprocess.run(args, env=env, cwd=str(REPO_DIR), capture_output=True, text=True, timeout=timeout)


def _seed_config(fh, key: str, value) -> None:
    """Writes into THIS fake home's config.json (the subprocess reads the
    SAME file via its own BRIDGE_TEST_HOME) -- scopes BRIDGE_TEST_HOME to
    `fh["home"]` AND clears any ambient BRIDGE_STATE_DIR (which would
    otherwise outrank it in `bridge_home()`'s own precedence -- the exact
    override the child subprocess's own env does NOT carry, see
    `_hermetic_child_env`) only for the duration of this one write,
    restoring whatever the parent process had before either way."""
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


def _chat_requests(mock) -> list:
    return [r for r in mock.requests if r["method"] == "POST"]


@test
def test_router_end_to_end_bearer_and_bill_to(ctx: Ctx):
    fh = build_fake_home()
    mock = MockUpstream(path_prefix="/v1", expected_bearer="router-test-token").start()
    try:
        _seed_config(fh, "huggingface.bill_to", "test-org")
        result = _run_cli(fh, "say hi please", model="hf:mock/model",
                           extra_env={"HF_TOKEN": "router-test-token", "HALO_HF_ROUTER_BASE_URL": mock.base_url})
        ctx.check(f"exit 0, got {result.returncode} stderr={result.stderr[-500:]!r}", result.returncode == 0)
        ctx.check(f"the scenario's reply reached stdout, got {result.stdout!r}", "pong" in result.stdout)

        chats = _chat_requests(mock)
        ctx.check(f"exactly one chat call recorded, got {len(chats)}", len(chats) == 1)
        req = chats[0]
        ctx.check(f"model on the wire, got {req['body'].get('model')!r}", req["body"].get("model") == "mock/model")
        ctx.check(f"bearer is HF_TOKEN, got {req['headers'].get('authorization')!r}",
                  req["headers"].get("authorization") == "Bearer " + "router-test-token")
        ctx.check(f"X-HF-Bill-To carries the configured org, got {req['headers'].get('x-hf-bill-to')!r}",
                  req["headers"].get("x-hf-bill-to") == "test-org")
    finally:
        mock.stop()


@test
def test_router_end_to_end_no_bill_to_header_when_unconfigured(ctx: Ctx):
    fh = build_fake_home()
    mock = MockUpstream(path_prefix="/v1", expected_bearer="router-test-token-2").start()
    try:
        result = _run_cli(fh, "say hi please", model="hf:mock/model",
                           extra_env={"HF_TOKEN": "router-test-token-2", "HALO_HF_ROUTER_BASE_URL": mock.base_url})
        ctx.check(f"exit 0, got {result.returncode} stderr={result.stderr[-500:]!r}", result.returncode == 0)
        chats = _chat_requests(mock)
        ctx.check(f"exactly one chat call recorded, got {len(chats)}", len(chats) == 1)
        ctx.check(f"no X-HF-Bill-To header when huggingface.bill_to is unset, got {chats[0]['headers']!r}",
                  "x-hf-bill-to" not in chats[0]["headers"])
    finally:
        mock.stop()


@test
def test_endpoint_end_to_end_uses_its_own_bearer_never_hf_token(ctx: Ctx):
    fh = build_fake_home()
    mock = MockUpstream(path_prefix="/v1", expected_bearer="endpoint-only-token").start()
    try:
        _seed_config(fh, "huggingface.endpoints",
                     [{"name": "my-prod", "url": mock.base_url, "token": "endpoint-only-token"}])
        # Also configured so a wrong (router) bearer being sent would 401
        # against this mock instead of succeeding -- the strongest proof
        # the two credential sources never cross-wire.
        _seed_config(fh, "huggingface.bill_to", "test-org")
        result = _run_cli(fh, "say hi please", model="hf:endpoint/my-prod",
                           extra_env={"HF_TOKEN": "a-router-token-that-must-never-be-sent-here"})
        ctx.check(f"exit 0, got {result.returncode} stderr={result.stderr[-500:]!r}", result.returncode == 0)
        ctx.check(f"the scenario's reply reached stdout, got {result.stdout!r}", "pong" in result.stdout)

        chats = _chat_requests(mock)
        ctx.check(f"exactly one chat call recorded, got {len(chats)}", len(chats) == 1)
        req = chats[0]
        ctx.check(f"bearer is the ENDPOINT's own token, got {req['headers'].get('authorization')!r}",
                  req["headers"].get("authorization") == "Bearer " + "endpoint-only-token")
        ctx.check("never X-HF-Bill-To on a dedicated endpoint, even with huggingface.bill_to configured",
                  "x-hf-bill-to" not in req["headers"])
    finally:
        mock.stop()


if __name__ == "__main__":
    ctx = Ctx()
    results, passed, failed, skipped = run_all(TESTS, ctx)
    sys.exit(print_results(results, passed, failed, skipped))
