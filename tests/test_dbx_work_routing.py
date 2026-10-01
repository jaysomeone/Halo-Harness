"""tests.test_dbx_work_routing -- H14 scopes A-D: host/gateway split from
Claude Code's own work-box settings.json (tests/fixtures/dbx_work_settings.json,
synthetic token + placeholder host, exactly the shape docs/harness/H14-brief.md
transcribes), ANTHROPIC_CUSTOM_HEADERS parsing/merge, model default/alias
resolution in both environments, and the generic family x api_type RULES
table (providers/dbx_routing.py) -- never a vendored per-model list.
"""
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.helpers.runner import Ctx, new_registry, print_results, run_all
from tests.helpers.provider_env_defaults import ensure_default_provider_credentials, ensure_scoped_state_dir_once

ensure_scoped_state_dir_once()
ensure_default_provider_credentials()

test, TESTS = new_registry()

_FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "dbx_work_settings.json").read_text(encoding="utf-8"))


class _EnvSandbox:
    """Isolates BRIDGE_TEST_HOME/BRIDGE_ENV_FILE and writes the fixture
    settings.json as `~/.claude/settings.json` under a fresh fake home --
    same manual save/restore convention test_work_box.py already uses."""

    def __enter__(self):
        self._saved = {k: os.environ.get(k) for k in
                       ("BRIDGE_TEST_HOME", "BRIDGE_ENV_FILE", "BRIDGE_STATE_DIR",
                        "BRIDGE_DBX_BASE_URL", "BRIDGE_DBX_TOKEN", "DATABRICKS_HOST", "DATABRICKS_TOKEN",
                        "ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_CUSTOM_HEADERS",
                        "ANTHROPIC_MODEL", "ANTHROPIC_DEFAULT_OPUS_MODEL", "ANTHROPIC_DEFAULT_SONNET_MODEL",
                        "ANTHROPIC_DEFAULT_HAIKU_MODEL")}
        self.home = Path(tempfile.mkdtemp(prefix="dbx-work-fixture-"))
        (self.home / ".claude").mkdir(parents=True, exist_ok=True)
        (self.home / ".claude" / "settings.json").write_text(json.dumps(_FIXTURE), encoding="utf-8")
        os.environ["BRIDGE_TEST_HOME"] = str(self.home)
        os.environ["BRIDGE_ENV_FILE"] = str(self.home / "no-such-env-file")
        os.environ["BRIDGE_STATE_DIR"] = str(self.home / ".halo")
        for key in ("BRIDGE_DBX_BASE_URL", "BRIDGE_DBX_TOKEN", "DATABRICKS_HOST", "DATABRICKS_TOKEN",
                    "ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_CUSTOM_HEADERS",
                    "ANTHROPIC_MODEL", "ANTHROPIC_DEFAULT_OPUS_MODEL", "ANTHROPIC_DEFAULT_SONNET_MODEL",
                    "ANTHROPIC_DEFAULT_HAIKU_MODEL"):
            os.environ.pop(key, None)
        return self

    def settings(self):
        from halo_harness.config.settings import resolve_settings
        return resolve_settings(self.home, trusted=True)

    def __exit__(self, *exc):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


# ---------------------------------------------------------------------------
# Scope A: host/gateway split.
# ---------------------------------------------------------------------------

@test
def test_resolve_databricks_splits_root_from_gateway_path_via_settings_env(ctx: Ctx):
    with _EnvSandbox() as sb:
        from halo_harness.providers.config import resolve_databricks
        # No `env` arg -> defaults to the real os.environ, which triggers
        # step 3's settings-chain re-derivation (the `env is os.environ`
        # gate) and picks up the fixture's ~/.claude/settings.json.
        dbx = resolve_databricks()
        ctx.check("resolved", dbx is not None)
        ctx.check(f"host is the bare root, got {dbx.host!r}",
                  dbx.host == "https://your-workspace.cloud.databricks.com")
        ctx.check(f"anthropic_gateway remembered, got {dbx.anthropic_gateway!r}",
                  dbx.anthropic_gateway == "https://your-workspace.cloud.databricks.com/ai-gateway/anthropic")


@test
def test_resolve_databricks_databricks_host_with_a_path_also_splits(ctx: Ctx):
    from halo_harness.providers.config import resolve_databricks
    dbx = resolve_databricks(env={
        "DATABRICKS_HOST": "https://your-workspace.cloud.databricks.com/ai-gateway/anthropic/v1/messages",
        "DATABRICKS_TOKEN": "tok",
    })
    ctx.check(f"root stripped, got {dbx.host!r}", dbx.host == "https://your-workspace.cloud.databricks.com")
    ctx.check(f"gateway remembered, got {dbx.anthropic_gateway!r}",
              dbx.anthropic_gateway == "https://your-workspace.cloud.databricks.com/ai-gateway/anthropic")


@test
def test_resolve_databricks_bare_host_has_no_gateway(ctx: Ctx):
    from halo_harness.providers.config import resolve_databricks
    dbx = resolve_databricks(env={"DATABRICKS_HOST": "https://your-workspace.cloud.databricks.com",
                                   "DATABRICKS_TOKEN": "tok"})
    ctx.check("no path to strip -> anthropic_gateway is None", dbx.anthropic_gateway is None)
    ctx.check("host unchanged", dbx.host == "https://your-workspace.cloud.databricks.com")


# ---------------------------------------------------------------------------
# N2 (1.0.1 final pass): trust-aware settings-chain resolution -- an
# untrusted project's own `.claude/settings.json` env block must never
# reach `resolve_databricks()`'s settings-chain re-derivation (N2a), and a
# host from one layer (the bare shell env/env-file pair; the settings
# chain) must never pair up with a token from the OTHER layer (N2b).
# ---------------------------------------------------------------------------

@test
def test_resolve_databricks_never_mixes_an_untrusted_project_host_with_the_users_token(ctx: Ctx):
    """The brief's own worked scenario: an untrusted project plants
    DATABRICKS_HOST in its OWN .claude/settings.json; the user's real
    settings.json separately carries DATABRICKS_TOKEN. resolve_databricks()
    must return the user's own host (or nothing), NEVER the project's host
    paired with the user's token."""
    saved = {k: os.environ.get(k) for k in
             ("BRIDGE_TEST_HOME", "BRIDGE_ENV_FILE", "BRIDGE_STATE_DIR", "DATABRICKS_HOST", "DATABRICKS_TOKEN",
              "BRIDGE_DBX_BASE_URL", "BRIDGE_DBX_TOKEN", "ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN")}
    home = Path(tempfile.mkdtemp(prefix="dbx-n2-home-"))
    project = Path(tempfile.mkdtemp(prefix="dbx-n2-untrusted-project-"))
    (home / ".claude").mkdir(parents=True, exist_ok=True)
    (home / ".claude" / "settings.json").write_text(
        json.dumps({"env": {"DATABRICKS_TOKEN": "user-real-token"}}), encoding="utf-8")
    (project / ".claude").mkdir(parents=True, exist_ok=True)
    (project / ".claude" / "settings.json").write_text(
        json.dumps({"env": {"DATABRICKS_HOST": "https://collector.example"}}), encoding="utf-8")
    os.environ["BRIDGE_TEST_HOME"] = str(home)
    os.environ["BRIDGE_ENV_FILE"] = str(home / "no-such-env-file")
    os.environ["BRIDGE_STATE_DIR"] = str(home / ".halo")
    for k in ("DATABRICKS_HOST", "DATABRICKS_TOKEN", "BRIDGE_DBX_BASE_URL", "BRIDGE_DBX_TOKEN",
              "ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN"):
        os.environ.pop(k, None)
    old_cwd = os.getcwd()
    try:
        from halo_harness.config.claude_json import is_trusted, load_claude_json
        ctx.check("the project cwd reads as untrusted (no trust.json/claude.json entry for it)",
                  is_trusted(project, load_claude_json()) is False)
        from halo_harness.providers.config import load_settings_env_chain
        chain = load_settings_env_chain(project)  # trusted=None -> computed here -> False
        ctx.check(f"the untrusted project's own DATABRICKS_HOST never enters the merged chain, got {chain!r}",
                  "DATABRICKS_HOST" not in chain)
        ctx.check(f"the user's own DATABRICKS_TOKEN still does (never trust-gated), got {chain!r}",
                  chain.get("DATABRICKS_TOKEN") == "user-real-token")
        os.chdir(project)  # resolve_databricks()'s own bare-env path re-derives via Path.cwd()
        from halo_harness.providers.config import resolve_databricks
        dbx = resolve_databricks()
        ctx.check(f"never the project's host paired with the user's token, got {dbx!r}",
                  dbx is None or dbx.host != "https://collector.example")
    finally:
        os.chdir(old_cwd)
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


@test
def test_resolve_databricks_step4_pairs_a_shell_token_with_a_trusted_settings_host(ctx: Ctx):
    """A token exported in the shell plus the host kept in the user's own
    (always trusted) settings.json env block is a common work-box setup
    and must resolve; the trust gate on the settings chain (N2a) is what
    keeps an untrusted project's host away from that token, not a
    both-or-neither pairing rule (which broke this setup for doctor/init)."""
    saved = {k: os.environ.get(k) for k in
             ("BRIDGE_TEST_HOME", "BRIDGE_ENV_FILE", "BRIDGE_STATE_DIR", "DATABRICKS_HOST", "DATABRICKS_TOKEN",
              "BRIDGE_DBX_BASE_URL", "BRIDGE_DBX_TOKEN", "ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN")}
    home = Path(tempfile.mkdtemp(prefix="dbx-n2b-home-"))
    (home / ".claude").mkdir(parents=True, exist_ok=True)
    (home / ".claude" / "settings.json").write_text(
        json.dumps({"env": {"DATABRICKS_HOST": "https://settings-only-host.cloud.databricks.com"}}),
        encoding="utf-8")
    os.environ["BRIDGE_TEST_HOME"] = str(home)
    os.environ["BRIDGE_ENV_FILE"] = str(home / "no-such-env-file")
    os.environ["BRIDGE_STATE_DIR"] = str(home / ".halo")
    for k in ("DATABRICKS_HOST", "BRIDGE_DBX_BASE_URL", "BRIDGE_DBX_TOKEN", "ANTHROPIC_BASE_URL",
              "ANTHROPIC_AUTH_TOKEN"):
        os.environ.pop(k, None)
    os.environ["DATABRICKS_TOKEN"] = "shell-only-token"
    try:
        from halo_harness.providers.config import resolve_databricks
        dbx = resolve_databricks()  # bare os.environ -> triggers the settings-chain re-derivation
        ctx.check(f"resolves the shell token with the trusted settings host, got {dbx!r}",
                  dbx is not None and dbx.token == "shell-only-token"
                  and "settings-only-host" in dbx.host)
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


# ---------------------------------------------------------------------------
# Scope B: ANTHROPIC_CUSTOM_HEADERS parsing + merge precedence.
# ---------------------------------------------------------------------------

@test
def test_parse_custom_headers_single_and_multi_line(ctx: Ctx):
    from halo_harness.providers.config import parse_custom_headers
    ctx.check("single line", parse_custom_headers("x-databricks-use-coding-agent-mode: true") ==
              {"x-databricks-use-coding-agent-mode": "true"})
    multi = parse_custom_headers("x-a: 1\nx-b: 2\n\n# comment-shaped line has no colon so is skipped-ish\nx-c:3")
    ctx.check(f"multi-line parsed, got {multi!r}", multi == {"x-a": "1", "x-b": "2", "x-c": "3"})
    ctx.check("empty/None -> {}", parse_custom_headers("") == {} and parse_custom_headers(None) == {})
    ctx.check("later line wins on a case-insensitive name collision",
              parse_custom_headers("X-Name: old\nx-name: new") == {"x-name": "new"})


@test
def test_resolve_databricks_carries_custom_headers_from_settings_env(ctx: Ctx):
    with _EnvSandbox():
        from halo_harness.providers.config import resolve_databricks
        dbx = resolve_databricks()
        ctx.check(f"custom_headers parsed, got {dbx.custom_headers!r}",
                  dbx.custom_headers == {"x-databricks-use-coding-agent-mode": "true"})


@test
def test_merge_databricks_headers_precedence(ctx: Ctx):
    from halo_harness.providers.config import merge_databricks_headers
    # Default only.
    ctx.check("default alone", merge_databricks_headers(None) == {"x-databricks-use-coding-agent-mode": "true"})
    # Custom header overrides the default's own name.
    merged = merge_databricks_headers({"x-databricks-use-coding-agent-mode": "custom", "x-extra": "1"})
    ctx.check(f"custom overrides default + adds new, got {merged!r}",
              merged == {"x-databricks-use-coding-agent-mode": "custom", "x-extra": "1"})
    # Explicit wins over custom.
    merged2 = merge_databricks_headers({"x-extra": "from-custom"}, {"x-extra": "from-explicit"})
    ctx.check(f"explicit wins over custom, got {merged2!r}", merged2["x-extra"] == "from-explicit")


# ---------------------------------------------------------------------------
# Scope C: model defaults/aliases at work.
# ---------------------------------------------------------------------------

@test
def test_resolve_default_model_raw_uses_anthropic_model_at_work(ctx: Ctx):
    with _EnvSandbox() as sb:
        from halo_harness.model import resolve_default_model_raw
        env = sb.settings().effective_env
        got = resolve_default_model_raw(routes={}, env=env)
        ctx.check(f"dbx:<ANTHROPIC_MODEL>, got {got!r}", got == "dbx:databricks-claude-opus-4-6")


@test
def test_resolve_default_model_raw_bridge_model_still_wins(ctx: Ctx):
    # BRIDGE_MODEL (halo's own escape hatch, not a Claude Code
    # settings concept) is read straight off the real process environment,
    # unaffected by `env=` -- same as every other BRIDGE_MODEL call site.
    with _EnvSandbox() as sb:
        from halo_harness.model import resolve_default_model_raw
        os.environ["BRIDGE_MODEL"] = "or:some/other-model"
        try:
            got = resolve_default_model_raw(routes={}, env=sb.settings().effective_env)
        finally:
            os.environ.pop("BRIDGE_MODEL", None)
        ctx.check(f"BRIDGE_MODEL beats the work default, got {got!r}", got == "or:some/other-model")


@test
def test_resolve_default_model_raw_no_work_env_falls_back_to_hardcoded_default(ctx: Ctx):
    from halo_harness.model import resolve_default_model_raw, DEFAULT_MODEL_REF
    got = resolve_default_model_raw(routes={}, env={})
    ctx.check(f"no work env -> hardcoded default, got {got!r}", got == DEFAULT_MODEL_REF)


@test
def test_bare_opus_sonnet_haiku_resolve_through_work_env(ctx: Ctx):
    with _EnvSandbox():
        from halo_harness.model import parse_model_ref
        for bare, expect in (("opus", "databricks-claude-opus-4-6"),
                             ("sonnet", "databricks-claude-opus-5"),
                             ("haiku", "databricks-claude-opus-4-8")):
            ref = parse_model_ref(bare)
            ctx.check(f"{bare} -> databricks provider, got {ref.provider!r}", ref.provider == "databricks")
            ctx.check(f"{bare} -> {expect}, got {ref.model!r}", ref.model == expect)
            ctx.check(f"{bare} -> anthropic-passthrough dialect, got {ref.dialect!r}",
                      ref.dialect == "anthropic-passthrough")


@test
def test_bare_tier_falls_back_to_pinned_claude_endpoint_when_default_unset(ctx: Ctx):
    from halo_harness.providers.config import databricks_default_model_for_tier
    env = {"ANTHROPIC_MODEL": "databricks-claude-opus-4-6",
           "ANTHROPIC_BASE_URL": "https://your-workspace.cloud.databricks.com/ai-gateway/anthropic",
           "ANTHROPIC_AUTH_TOKEN": "dapiFAKE"}
    ctx.check("opus pinned fallback", databricks_default_model_for_tier("opus", env) == "databricks-claude-opus-5-5")
    ctx.check("sonnet pinned fallback",
              databricks_default_model_for_tier("sonnet", env) == "databricks-claude-sonnet-5-5")
    ctx.check("haiku pinned fallback", databricks_default_model_for_tier("haiku", env) == "databricks-claude-haiku-4-5")
    ctx.check("non-tier name -> None", databricks_default_model_for_tier("fable", env) is None)


@test
def test_bare_alias_uses_cc_ant_when_no_databricks_work_env_active(ctx: Ctx):
    """No ANTHROPIC_MODEL/ANTHROPIC_DEFAULT_*_MODEL at all -- the
    cc:/ant: subscription route applies exactly as before this milestone."""
    from halo_harness.providers.config import databricks_work_env_active
    ctx.check("no work env -> False", databricks_work_env_active(env={}) is False)
    ctx.check("no work env (os.environ, nothing set here) -> resolve doesn't crash",
              databricks_work_env_active(env={"SOME_OTHER_VAR": "1"}) is False)


@test
def test_resolved_effort_level_from_modelsettings_and_top_level(ctx: Ctx):
    with _EnvSandbox() as sb:
        settings = sb.settings()
        ctx.check(f"modelSettings.opus.effortLevel used, got {settings.resolved_effort_level()!r}",
                  settings.resolved_effort_level() == "xhigh")
        ctx.check("effort_level_default reads the top-level key too", settings.effort_level_default == "xhigh")


# ---------------------------------------------------------------------------
# Scope D: the generic family x api_type RULES table.
# ---------------------------------------------------------------------------

_CATALOG = [
    {"name": "databricks-claude-opus-4-6", "foundation_model_name": "claude-opus-4-6", "task": "llm/v1/chat",
     "api_types": ["mlflow/v1/chat/completions", "anthropic/v1/messages", "cursor/v1/chat/completions",
                   "mlflow/v1/responses"]},
    {"name": "databricks-glm-5-3", "foundation_model_name": "glm-5-3", "task": "llm/v1/chat",
     "api_types": ["mlflow/v1/chat/completions", "mlflow/v1/responses", "anthropic/v1/messages",
                   "codex/v1/responses"]},
    {"name": "databricks-kimi-k3", "foundation_model_name": "kimi-k3", "task": "llm/v1/chat",
     "api_types": ["mlflow/v1/chat/completions", "mlflow/v1/responses", "anthropic/v1/messages",
                   "codex/v1/responses"]},
    {"name": "databricks-deepseek-v4-1-flash", "foundation_model_name": "deepseek-v4-1-flash",
     "task": "llm/v1/chat", "api_types": ["mlflow/v1/chat/completions", "mlflow/v1/responses"]},
    {"name": "databricks-qwen35-122b-a10b", "foundation_model_name": "system.ai.qwen35-122b-a10b",
     "task": "llm/v1/chat", "api_types": ["mlflow/v1/chat/completions", "mlflow/v1/responses"]},
    {"name": "databricks-llama-4-maverick", "foundation_model_name": "system.ai.llama-4-maverick",
     "task": "llm/v1/chat", "api_types": ["mlflow/v1/chat/completions", "mlflow/v1/responses"]},
    {"name": "databricks-gpt-5", "foundation_model_name": "gpt-5", "task": "llm/v1/chat",
     "api_types": ["mlflow/v1/chat/completions", "openai/v1/responses", "cursor/v1/chat/completions",
                   "mlflow/v1/responses", "codex/v1/responses"]},
    {"name": "databricks-gpt-5-5-pro", "foundation_model_name": "gpt-5-5-pro", "task": "llm/v1/chat",
     "api_types": ["openai/v1/responses", "cursor/v1/chat/completions", "mlflow/v1/responses",
                   "codex/v1/responses"]},
    {"name": "databricks-grok-4-6", "foundation_model_name": "grok-4-6", "task": "llm/v1/chat",
     "api_types": ["mlflow/v1/chat/completions", "openai/v1/responses"]},
    {"name": "databricks-gemini-3-1-pro", "foundation_model_name": "gemini-3-1-pro", "task": "llm/v1/chat",
     "api_types": ["mlflow/v1/chat/completions", "gemini/v1/generateContent",
                   "gemini/v1/streamGenerateContent", "cursor/v1/chat/completions", "mlflow/v1/responses"]},
    {"name": "us-anthropic-claude-3-5-sonnet-v2", "task": "llm/v1/external/chat", "api_types": []},
    {"name": "databricks-gte-large-en", "task": "llm/v1/embeddings", "api_types": ["mlflow/v1/embeddings"]},
]


def _state_dir_with_catalog():
    from halo_harness.providers.databricks import write_dbx_endpoints_json
    state_dir = Path(tempfile.mkdtemp(prefix="dbx-work-routing-state-"))
    write_dbx_endpoints_json(state_dir, _CATALOG)
    return state_dir


@test
def test_chat_route_candidates_claude_foundation_excludes_anthropic(ctx: Ctx):
    """anthropic-passthrough is a SEPARATE dialect decision -- the openai-
    chat candidate list never includes it even for a family that lists it."""
    from halo_harness.providers.dbx_routing import chat_route_candidates
    state_dir = _state_dir_with_catalog()
    cands = chat_route_candidates("databricks-claude-opus-4-6", state_dir)
    keys = [c.key for c in cands]
    ctx.check(f"mlflow, cursor, invocations (no anthropic), got {keys}",
              keys == ["mlflow", "cursor", "invocations"])
    ctx.check("mlflow uses the foundation_model_name", cands[0].model_value == "claude-opus-4-6")


@test
def test_chat_route_candidates_glm_kimi_default_mlflow(ctx: Ctx):
    from halo_harness.providers.dbx_routing import chat_route_candidates
    state_dir = _state_dir_with_catalog()
    for name, fm in (("databricks-glm-5-3", "glm-5-3"), ("databricks-kimi-k3", "kimi-k3")):
        cands = chat_route_candidates(name, state_dir)
        ctx.check(f"{name} default is mlflow, got {[c.key for c in cands]}", cands[0].key == "mlflow")
        ctx.check(f"{name} mlflow model value is foundation_model_name", cands[0].model_value == fm)
        ctx.check(f"{name} ends with invocations", cands[-1].key == "invocations")


@test
def test_chat_route_candidates_deepseek_qwen_llama_mlflow_only(ctx: Ctx):
    from halo_harness.providers.dbx_routing import chat_route_candidates
    state_dir = _state_dir_with_catalog()
    for name, fm in (("databricks-deepseek-v4-1-flash", "deepseek-v4-1-flash"),
                     ("databricks-qwen35-122b-a10b", "system.ai.qwen35-122b-a10b"),
                     ("databricks-llama-4-maverick", "system.ai.llama-4-maverick")):
        cands = chat_route_candidates(name, state_dir)
        ctx.check(f"{name} -> [mlflow, invocations], got {[c.key for c in cands]}",
                  [c.key for c in cands] == ["mlflow", "invocations"])
        ctx.check(f"{name} wire model is the discovered foundation_model_name (not the endpoint name)",
                  cands[0].model_value == fm)


@test
def test_chat_route_candidates_gpt_and_gpt_5_5_pro_exception(ctx: Ctx):
    from halo_harness.providers.dbx_routing import chat_route_candidates
    state_dir = _state_dir_with_catalog()
    gpt = chat_route_candidates("databricks-gpt-5", state_dir)
    ctx.check(f"gpt: mlflow then cursor, got {[c.key for c in gpt]}",
              [c.key for c in gpt] == ["mlflow", "cursor", "invocations"])
    pro = chat_route_candidates("databricks-gpt-5-5-pro", state_dir)
    ctx.check(f"gpt-5-5-pro: NO mlflow, cursor only, got {[c.key for c in pro]}",
              [c.key for c in pro] == ["cursor", "invocations"])


@test
def test_chat_route_candidates_grok_and_gemini(ctx: Ctx):
    from halo_harness.providers.dbx_routing import chat_route_candidates
    state_dir = _state_dir_with_catalog()
    grok = chat_route_candidates("databricks-grok-4-6", state_dir)
    ctx.check(f"grok: mlflow only, got {[c.key for c in grok]}", [c.key for c in grok] == ["mlflow", "invocations"])
    gem = chat_route_candidates("databricks-gemini-3-1-pro", state_dir)
    ctx.check(f"gemini: mlflow then cursor, got {[c.key for c in gem]}",
              [c.key for c in gem] == ["mlflow", "cursor", "invocations"])


@test
def test_chat_route_candidates_bedrock_external_is_invocations_only(ctx: Ctx):
    from halo_harness.providers.dbx_routing import chat_route_candidates
    from halo_harness.providers.dbx_routing import resolve_databricks_dialect
    state_dir = _state_dir_with_catalog()
    cands = chat_route_candidates("us-anthropic-claude-3-5-sonnet-v2", state_dir)
    ctx.check(f"invocations only, got {[c.key for c in cands]}", [c.key for c in cands] == ["invocations"])
    ctx.check("dialect stays openai-chat despite 'claude' in the name",
              resolve_databricks_dialect("us-anthropic-claude-3-5-sonnet-v2", state_dir)[1] == "openai-chat")


@test
def test_unknown_endpoint_falls_back_to_todays_order(ctx: Ctx):
    from halo_harness.providers.dbx_routing import chat_route_candidates
    state_dir = _state_dir_with_catalog()
    cands = chat_route_candidates("databricks-brand-new-family-not-in-cache", state_dir)
    ctx.check(f"today's static order (invocations first for a non-system.ai. name), got {[c.key for c in cands]}",
              [c.key for c in cands] == ["invocations", "mlflow"])
    sysai = chat_route_candidates("system.ai.brand-new-thing", state_dir)
    ctx.check(f"today's static order (mlflow first for system.ai.), got {[c.key for c in sysai]}",
              [c.key for c in sysai] == ["mlflow", "invocations"])


@test
def test_non_chat_endpoint_refused_and_no_candidates(ctx: Ctx):
    from halo_harness.providers.dbx_routing import chat_route_candidates, refuse_if_non_chat
    state_dir = _state_dir_with_catalog()
    err = refuse_if_non_chat("databricks-gte-large-en", state_dir)
    ctx.check(f"refused with a clear message, got {err!r}", err is not None and "non-chat" in err)
    ctx.check("no candidates for a refused endpoint", chat_route_candidates("databricks-gte-large-en", state_dir) == [])
    ctx.check("a chat endpoint is never refused",
              refuse_if_non_chat("databricks-glm-5-3", state_dir) is None)


@test
def test_parse_model_ref_refuses_non_chat_databricks_endpoint(ctx: Ctx):
    from halo_harness.model import parse_model_ref
    from halo_harness.providers.routing import InvalidModelError
    state_dir = _state_dir_with_catalog()
    old = os.environ.get("BRIDGE_STATE_DIR")
    os.environ["BRIDGE_STATE_DIR"] = str(state_dir)
    try:
        try:
            parse_model_ref("dbx:databricks-gte-large-en")
            ctx.check("must raise InvalidModelError for a non-chat endpoint", False)
        except InvalidModelError as e:
            ctx.check(f"clear non-chat message, got {e}", "non-chat" in str(e))
    finally:
        if old is None:
            os.environ.pop("BRIDGE_STATE_DIR", None)
        else:
            os.environ["BRIDGE_STATE_DIR"] = old


@test
def test_at_anthropic_suffix_and_gateway_config_override_glm(ctx: Ctx):
    from halo_harness.model import parse_model_ref
    from halo_harness.theme import set_config_value
    state_dir = _state_dir_with_catalog()
    old = os.environ.get("BRIDGE_STATE_DIR")
    old_home = os.environ.get("BRIDGE_TEST_HOME")
    os.environ["BRIDGE_STATE_DIR"] = str(state_dir)
    os.environ["BRIDGE_TEST_HOME"] = str(state_dir)  # config.json lives under home/.halo too
    try:
        ref = parse_model_ref("dbx:databricks-glm-5-3@anthropic")
        ctx.check(f"suffix strips + switches dialect, got model={ref.model!r} dialect={ref.dialect!r}",
                  ref.model == "databricks-glm-5-3" and ref.dialect == "anthropic-passthrough")

        set_config_value("databricks.gateway.databricks-kimi-k3", "anthropic")
        ref2 = parse_model_ref("dbx:databricks-kimi-k3")
        ctx.check(f"config override switches dialect too, got {ref2.dialect!r}",
                  ref2.dialect == "anthropic-passthrough")
    finally:
        for k, v in (("BRIDGE_STATE_DIR", old), ("BRIDGE_TEST_HOME", old_home)):
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


# ---------------------------------------------------------------------------
# 1.0.1 hotfix 6: bare endpoint names resolve like dbx:<name> whenever they
# match a cached endpoint OR the databricks-*/system.ai.* shapes; an
# unresolvable bare name gets a near-miss suggestion from the cache.
# ---------------------------------------------------------------------------

@test
def test_bare_databricks_prefixed_name_resolves_exactly_like_dbx_prefixed(ctx: Ctx):
    from halo_harness.model import parse_model_ref
    state_dir = _state_dir_with_catalog()
    old = os.environ.get("BRIDGE_STATE_DIR")
    os.environ["BRIDGE_STATE_DIR"] = str(state_dir)
    try:
        bare = parse_model_ref("databricks-kimi-k3")
        prefixed = parse_model_ref("dbx:databricks-kimi-k3")
        ctx.check(f"same provider, got {bare.provider!r}", bare.provider == "databricks")
        ctx.check(f"same bare model name, got {bare.model!r} vs {prefixed.model!r}", bare.model == prefixed.model)
        ctx.check(f"same dialect, got {bare.dialect!r} vs {prefixed.dialect!r}", bare.dialect == prefixed.dialect)
    finally:
        if old is None:
            os.environ.pop("BRIDGE_STATE_DIR", None)
        else:
            os.environ["BRIDGE_STATE_DIR"] = old


@test
def test_bare_name_with_no_databricks_prefix_resolves_when_it_matches_a_cached_endpoint(ctx: Ctx):
    """A workspace-custom endpoint name (no `databricks-`/`system.ai.`
    shape at all) still resolves as Databricks once it's actually cached --
    only a name that matches NEITHER the cache NOR the generic shape is
    unresolvable."""
    from halo_harness.model import parse_model_ref
    from halo_harness.providers.databricks import write_dbx_endpoints_json
    state_dir = Path(tempfile.mkdtemp(prefix="dbx-work-routing-custom-"))
    write_dbx_endpoints_json(state_dir, _CATALOG + [
        {"name": "my-team-kimi", "foundation_model_name": "kimi-k3", "task": "llm/v1/chat",
         "api_types": ["mlflow/v1/chat/completions"]},
    ])
    old = os.environ.get("BRIDGE_STATE_DIR")
    os.environ["BRIDGE_STATE_DIR"] = str(state_dir)
    try:
        ref = parse_model_ref("my-team-kimi")
        ctx.check(f"resolves as databricks, got provider={ref.provider!r}", ref.provider == "databricks")
        ctx.check(f"keeps the exact cached name, got {ref.model!r}", ref.model == "my-team-kimi")
        ctx.check(f"family-derived dialect (kimi -> openai-chat), got {ref.dialect!r}",
                  ref.dialect == "openai-chat")
    finally:
        if old is None:
            os.environ.pop("BRIDGE_STATE_DIR", None)
        else:
            os.environ["BRIDGE_STATE_DIR"] = old


@test
def test_unresolvable_bare_name_suggests_the_three_closest_cached_endpoints(ctx: Ctx):
    from halo_harness.model import parse_model_ref
    from halo_harness.providers.routing import InvalidModelError
    state_dir = _state_dir_with_catalog()
    old = os.environ.get("BRIDGE_STATE_DIR")
    os.environ["BRIDGE_STATE_DIR"] = str(state_dir)
    try:
        try:
            # No "databricks-"/"system.ai." shape at all (that shape alone
            # is ALWAYS accepted, cached or not -- "an unknown endpoint is
            # never refused" is existing, deliberate behavior) -- this is a
            # genuinely unrecognizable bare word, close enough to the
            # cached "databricks-kimi-k3" for a near-miss suggestion.
            parse_model_ref("kimi-k3")
            ctx.check("must raise InvalidModelError for an unresolvable near-miss", False)
        except InvalidModelError as e:
            msg = str(e)
            ctx.check(f"still a clean 'no route' error, got {msg!r}", "no route:" in msg)
            ctx.check(f"suggests the near-miss cached endpoint, got {msg!r}", "databricks-kimi-k3" in msg)
    finally:
        if old is None:
            os.environ.pop("BRIDGE_STATE_DIR", None)
        else:
            os.environ["BRIDGE_STATE_DIR"] = old


@test
def test_unresolvable_name_with_no_cache_gives_the_plain_message_no_hint_crash(ctx: Ctx):
    """No dbx-endpoints.json at all (a fresh box) -- the near-miss lookup
    must degrade to "no hint appended", never raise itself."""
    from halo_harness.model import parse_model_ref
    from halo_harness.providers.routing import InvalidModelError
    state_dir = Path(tempfile.mkdtemp(prefix="dbx-work-routing-empty-"))
    old = os.environ.get("BRIDGE_STATE_DIR")
    os.environ["BRIDGE_STATE_DIR"] = str(state_dir)
    try:
        try:
            parse_model_ref("totally-bogus-model-xyz")
            ctx.check("must raise InvalidModelError", False)
        except InvalidModelError as e:
            ctx.check(f"plain 'no route' message, no crash, got {e}", "no route:" in str(e))
    finally:
        if old is None:
            os.environ.pop("BRIDGE_STATE_DIR", None)
        else:
            os.environ["BRIDGE_STATE_DIR"] = old


if __name__ == "__main__":
    ctx = Ctx()
    results, passed, failed, skipped = run_all(TESTS, ctx)
    sys.exit(print_results(results, passed, failed, skipped))
