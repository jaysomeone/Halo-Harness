"""Codex app-server catalog discovery; conversation tests live in test_codex_session."""
import json
import os
import sys
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.helpers.runner import Ctx, new_registry, print_results, run_all
from tests.helpers.provider_env_defaults import ensure_default_provider_credentials

os.environ["BRIDGE_TEST_HOME"] = tempfile.mkdtemp(prefix="cx-session-scratchhome-")
ensure_default_provider_credentials()

test, TESTS = new_registry()

REPO_DIR = Path(__file__).resolve().parent.parent
FAKE_CODEX = REPO_DIR / "tests" / "helpers" / "fake_codex_cx.py"
_ENV_KEYS = ("HALO_CODEX_EXE", "FAKE_CODEX_LOGIN", "FAKE_CODEX_LOG", "FAKE_CODEX_USER_MCP",
             "FAKE_CODEX_RESUME_FAILS", "BRIDGE_TEST_CX_LOGIN_STATUS", "OPENAI_API_KEY", "CODEX_HOME")


@contextmanager
def _fake_codex_env(**extra):
    saved = {k: os.environ.get(k) for k in _ENV_KEYS}
    log_path = Path(tempfile.mkdtemp(prefix="cx-log-")) / "codex.jsonl"
    os.environ.pop("CODEX_HOME", None)
    os.environ["HALO_CODEX_EXE"] = '"' + sys.executable + '" "' + str(FAKE_CODEX) + '"'
    os.environ["FAKE_CODEX_LOG"] = str(log_path)
    os.environ.pop("BRIDGE_TEST_CX_LOGIN_STATUS", None)
    for k, v in extra.items():
        os.environ[k] = v
    from halo_harness.providers.cx_models import reset_cached_codex_login_status
    reset_cached_codex_login_status()
    try:
        yield log_path
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        reset_cached_codex_login_status()


def _logged(log_path: Path, method: str) -> list:
    if not log_path.exists():
        return []
    rows = [json.loads(ln) for ln in log_path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    return [r["params"] for r in rows if r["method"] == method]


@test
def test_cx_login_status_parsing(ctx: Ctx):
    from halo_harness.providers.cx_models import parse_login_status_text
    ok = parse_login_status_text("WARNING: something\nLogged in using ChatGPT\n")
    ctx.check("ChatGPT", ok.logged_in and ok.method == "chatgpt")
    key = parse_login_status_text("Logged in using an API key - sk-proj-***ABCD")
    ctx.check("API key", key.logged_in and key.method == "api_key")
    ctx.check("not logged in", not parse_login_status_text("Not logged in").logged_in)
    ctx.check("garbage", not parse_login_status_text("").logged_in)


@test
def test_cx_catalog_refresh_and_helpers(ctx: Ctx):
    from halo_harness.providers import cx_models as m
    state = Path(tempfile.mkdtemp(prefix="cx-cat-"))
    with _fake_codex_env():
        ctx.check("seed before refresh", m.cx_catalog_is_seed(state))
        data = m.refresh_cx_catalog(state_dir=state)
    ids = [x["id"] for x in data.get("models", [])]
    ctx.check(f"models from model/list, got {ids}", ids == ["gpt-test", "gpt-hidden"])
    ctx.check("hidden marked", data["models"][1].get("hidden") is True)
    ctx.check("plan kept, email not stored", data["account"] == {"type": "chatgpt", "plan": "pro"}
              and "example.com" not in json.dumps(data))
    ctx.check("default alias", m.resolve_cx_alias("default", state) == "gpt-test")
    fields = m.profile_fields_for_cx_model("gpt-test", state)
    ctx.check("efforts from model/list", fields["efforts"] == ("low", "medium", "high"))
    m.record_context_window("gpt-test", 123456, state)
    ctx.check("observed context window wins", m.profile_fields_for_cx_model("gpt-test", state)["context_tokens"] == 123456)
    line = m.format_rate_limits({"plan": "pro", "primary": {"used_percent": 12, "window_mins": 300},
                                 "secondary": {"used_percent": 40, "window_mins": 10080}})
    ctx.check(f"usage line, got {line!r}", line == "pro plan · 5 h: 12% used · weekly: 40% used")
    m.record_rate_limits({"planType": "pro", "primary": {"usedPercent": 1, "windowDurationMins": 300}}, state)
    ctx.check("fresh reading has no age", "as of" not in m.cached_rate_limits_line(state))
    ctx.check("old reading says when", "as of" in m.cached_rate_limits_line(state, now=time.time() + 3600))


@test
def test_cx_model_ref_and_child_env(ctx: Ctx):
    from halo_harness.model import parse_model_ref
    ref = parse_model_ref("cx:gpt-5.5")
    ctx.check("provider codex", ref.provider == "codex" and ref.model == "gpt-5.5" and ref.dialect == "codex-subprocess")
    from halo_harness.providers.cx_models import cx_child_env
    env = cx_child_env({"OPENAI_API_KEY": "sk-x", "CODEX_API_KEY": "k", "OPENAI_BASE_URL": "u",
                        "CODEX_HOME": "/h", "PATH": "/bin"})
    ctx.check("API key and base URL stripped, CODEX_HOME kept",
              "OPENAI_API_KEY" not in env and "CODEX_API_KEY" not in env and "OPENAI_BASE_URL" not in env
              and env.get("CODEX_HOME") == "/h")
    from halo_harness.providers.enablement import canonical, label_for
    ctx.check("cx and codex name the provider", canonical("cx") == canonical("codex") == "codex_subscription")
    ctx.check("label", label_for("cx") == "Codex subscription (ChatGPT)")


if __name__ == "__main__":
    ctx = Ctx()
    results, passed, failed, skipped = run_all(TESTS, ctx)
    sys.exit(print_results(results, passed, failed, skipped))
