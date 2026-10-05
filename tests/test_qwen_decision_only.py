"""tests.test_qwen_decision_only -- Halo 2.0.2 round 5 (Qwen-at-work
brief, item 1): a decision-only/judge endpoint (the owner's work-VM
"openjev qwen" report, Databricks' own `databricks-openjev-qwen35-4b`) is
classified from `model_table.json` data (a tabled row's own
`capabilities.decision_only`, or the top-level `decision_only_name_
patterns` net for an untabled name), never used as the session model
(`Controller.set_model` / `headless.build_session` redirect it to the
`judge` role instead), and shown in the model picker under its own
"judge / decision" group.
"""
from __future__ import annotations

import contextlib
import io
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.helpers.runner import Ctx, new_registry, print_results, run_all

test, TESTS = new_registry()

_OPENJEV = "databricks-openjev-qwen35-4b"
_PROVIDER_ENV_VARS = (
    "OPENROUTER_API_KEY", "DATABRICKS_HOST", "DATABRICKS_TOKEN", "BRIDGE_DBX_BASE_URL",
    "BRIDGE_DBX_TOKEN", "ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN",
    "TYPESAFE_API_KEY", "HALO_MODEL", "BRIDGE_MODEL",
)


class _Env:
    """Every test scopes its own home -- BRIDGE_TEST_HOME/BRIDGE_STATE_DIR
    point at a fresh temp dir, never the real ~/.halo."""

    def __enter__(self):
        self._saved = {k: os.environ.get(k) for k in
                       ("BRIDGE_TEST_HOME", "BRIDGE_STATE_DIR", "BRIDGE_ENV_FILE") + _PROVIDER_ENV_VARS}
        d = Path(tempfile.mkdtemp(prefix="qwen-decision-only-"))
        os.environ["BRIDGE_TEST_HOME"] = str(d)
        os.environ["BRIDGE_STATE_DIR"] = str(d / ".halo")
        os.environ["BRIDGE_ENV_FILE"] = str(d / "no-env-file")
        for k in _PROVIDER_ENV_VARS:
            os.environ.pop(k, None)
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
# Classification: model_table.json data, read through providers.profiles.
# ---------------------------------------------------------------------------

@test
def test_openjev_row_is_decision_only_and_tools_unsupported(ctx: Ctx):
    from halo_harness.providers.profiles import reset_model_table_cache, resolve_profile
    from halo_harness.providers.routing import Route
    reset_model_table_cache()
    profile = resolve_profile(Route(provider="databricks", upstream_model=_OPENJEV, dialect="openai-chat"))
    ctx.check("decision_only is True", profile.decision_only is True)
    ctx.check(f"a human-readable reason is carried, got {profile.decision_only_reason!r}",
              bool(profile.decision_only_reason))
    ctx.check("tools_supported is False", profile.tools_supported is False)
    ctx.check("model_id carries the bare endpoint name", profile.model_id == _OPENJEV)


@test
def test_ordinary_qwen_row_is_unaffected(ctx: Ctx):
    from halo_harness.providers.profiles import resolve_profile
    from halo_harness.providers.routing import Route
    profile = resolve_profile(Route(provider="databricks", upstream_model="databricks-qwen35-122b-a10b",
                                     dialect="openai-chat"))
    ctx.check("not decision-only", profile.decision_only is False)
    ctx.check("tools ARE supported", profile.tools_supported is True)
    ctx.check("no reason text", profile.decision_only_reason is None)


@test
def test_an_untabled_name_matching_the_pattern_net_is_still_classified(ctx: Ctx):
    """The "a wrong guess is a one-line fix" net: a DIFFERENTLY-named
    judge-style endpoint with no row of its own still gets classified
    purely from the `decision_only_name_patterns.patterns` substring list
    -- edit the data, never providers/profiles.py's code."""
    from halo_harness.providers.profiles import decision_only_info
    ctx.check("matches on the bare 'openjev' substring",
              decision_only_info("databricks-some-future-openjev-endpoint-v2") is not None)
    ctx.check("matches on the 'jev-judge' substring",
              decision_only_info("my-workspace-jev-judge-model") is not None)
    ctx.check("an ordinary name never matches", decision_only_info("databricks-glm-5-3") is None)


@test
def test_decision_only_notice_names_the_model_and_the_judge_role(ctx: Ctx):
    from halo_harness.providers.profiles import decision_only_notice
    notice = decision_only_notice(_OPENJEV)
    ctx.check(f"names the model, got {notice!r}", _OPENJEV in notice)
    ctx.check("names the judge role", "judge" in notice)
    ctx.check("no doubled punctuation from the table's own trailing period",
              ".." not in notice)
    ctx.check("an ordinary model gets no notice at all",
              decision_only_notice("databricks-qwen35-122b-a10b") is None)


# ---------------------------------------------------------------------------
# The model picker: Controller.list_models() groups it separately.
# ---------------------------------------------------------------------------

class _FakeModelRef:
    raw = "or:mock/current"
    provider = "openrouter"


class _FakeModelProfile:
    context_tokens = 128000
    max_output_tokens = 8192


class _FakeSession:
    model_ref = _FakeModelRef()
    model_profile = _FakeModelProfile()


def _controller(state_dir, **kw):
    from halo_harness.controller import Controller
    return Controller(session=_FakeSession(), cwd=Path.cwd(), state_dir=state_dir, routes={}, **kw)


def _dbx_endpoint_entry(name):
    return {"name": name, "task": "llm/v1/chat", "ready": None, "permission_level": None,
            "endpoint_type": None, "ai_gateway_v2_supported": None,
            "api_types": ["mlflow/v1/chat/completions"], "foundation_model_name": name, "model_class": None}


@test
def test_picker_groups_the_openjev_endpoint_under_judge_decision(ctx: Ctx):
    from halo_harness.providers.databricks import write_dbx_endpoints_json
    with _Env() as env:
        os.environ["DATABRICKS_HOST"] = "https://test.cloud.databricks.com"
        os.environ["DATABRICKS_TOKEN"] = "test-token"
        write_dbx_endpoints_json(env.state_dir, [
            _dbx_endpoint_entry(_OPENJEV), _dbx_endpoint_entry("databricks-qwen35-122b-a10b"),
        ])
        models = _controller(env.state_dir).list_models()
        rows = {m["ref"]: m for m in models if m.get("provider") == "databricks"}
        ctx.check(f"both endpoints listed, got {sorted(rows)}",
                  f"dbx:{_OPENJEV}" in rows and "dbx:databricks-qwen35-122b-a10b" in rows)
        ctx.check(f"openjev gets its own group, got {rows[f'dbx:{_OPENJEV}']['group']!r}",
                  rows[f"dbx:{_OPENJEV}"]["group"] == "Databricks (judge / decision)")
        ctx.check("openjev's detail carries the capability note, not a generic family tag",
                  "yes/no" in rows[f"dbx:{_OPENJEV}"]["detail"])
        ctx.check(f"the ordinary qwen row keeps its plain family group, got "
                  f"{rows['dbx:databricks-qwen35-122b-a10b']['group']!r}",
                  rows["dbx:databricks-qwen35-122b-a10b"]["group"] == "Databricks (qwen)")


# ---------------------------------------------------------------------------
# Controller.set_model(): never installs it as the session model.
# ---------------------------------------------------------------------------

def _resolver_for(model_id):
    from halo_harness.model import ModelProfile, ModelRef

    def resolve(raw):
        return ModelRef(raw=raw, provider="databricks", model=model_id, dialect="openai-chat"), ModelProfile(), None
    return resolve


@test
def test_set_model_redirects_decision_only_to_the_judge_role_not_the_session(ctx: Ctx):
    from halo_harness.theme import get_config_value
    with _Env() as env:
        ctrl = _controller(env.state_dir, model_resolver=_resolver_for(_OPENJEV))
        err = ctrl.set_model(f"dbx:{_OPENJEV}")
        ctx.check(f"a notice is returned (not silently accepted), got {err!r}", bool(err))
        ctx.check("notice names the judge role", "judge" in err)
        ctx.check("the session model switch was NEVER queued", ctrl.commands.empty())
        ctx.check(f"roles.judge was set to the ref instead, got {get_config_value('roles.judge', default=None)!r}",
                  get_config_value("roles.judge", default=None) == f"dbx:{_OPENJEV}")


@test
def test_set_model_an_ordinary_model_switch_is_unaffected(ctx: Ctx):
    with _Env() as env:
        ctrl = _controller(env.state_dir, model_resolver=_resolver_for("databricks-qwen35-122b-a10b"))
        err = ctrl.set_model("dbx:databricks-qwen35-122b-a10b")
        ctx.check(f"no error/notice, got {err!r}", err is None)
        ctx.check("the session model switch WAS queued", not ctrl.commands.empty())


# ---------------------------------------------------------------------------
# headless.build_session(): the SAME guard at initial session construction.
# ---------------------------------------------------------------------------

def _build(cwd: Path, **kw):
    from halo_harness import headless
    kw.setdefault("bare", True)
    kw.setdefault("print_mode", True)
    kw.setdefault("max_turns", 3)
    return headless.build_session(cwd=cwd, **kw)


@test
def test_build_session_never_starts_on_a_decision_only_model(ctx: Ctx):
    from halo_harness.model import DEFAULT_MODEL_REF
    from halo_harness.theme import get_config_value
    with _Env() as env, tempfile.TemporaryDirectory() as cwd:
        os.environ["OPENROUTER_API_KEY"] = "test-key-not-real"
        os.environ["DATABRICKS_HOST"] = "https://test.cloud.databricks.com"
        os.environ["DATABRICKS_TOKEN"] = "test-token"
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            build = _build(Path(cwd), model_ref_raw=f"dbx:{_OPENJEV}")
        notice = stderr.getvalue()
        ctx.check(f"falls back to the hardcoded default instead of starting on it, got {build.model_ref.raw}",
                  build.model_ref.raw == DEFAULT_MODEL_REF)
        ctx.check(f"the console notice names the model and the judge role, got {notice!r}",
                  _OPENJEV in notice and "judge" in notice)
        ctx.check(f"roles.judge was configured as a side effect, got {get_config_value('roles.judge', default=None)!r}",
                  get_config_value("roles.judge", default=None) == f"dbx:{_OPENJEV}")


@test
def test_build_session_falls_through_when_the_configured_default_itself_is_decision_only(ctx: Ctx):
    """2.0.2 review finding 8 (major) pin: "a session can never start on
    it" failed specifically when the CONFIGURED DEFAULT (config.model,
    HALO_MODEL, routes.default, or the work-env dbx:<ANTHROPIC_MODEL>
    shortcut -- never an explicit --model) is itself the decision-only
    endpoint. `resolve_default_model_raw()`'s own fallback call returns
    the EXACT SAME ref a second time (nothing about its inputs changed
    in between), so the old code's single re-check never caught it and
    the session was built on the decision-only endpoint anyway."""
    from halo_harness.model import DEFAULT_MODEL_REF
    from halo_harness.theme import get_config_value, set_config_value
    with _Env() as env, tempfile.TemporaryDirectory() as cwd:
        os.environ["OPENROUTER_API_KEY"] = "test-key-not-real"
        os.environ["DATABRICKS_HOST"] = "https://test.cloud.databricks.com"
        os.environ["DATABRICKS_TOKEN"] = "test-token"
        set_config_value("model", f"dbx:{_OPENJEV}")  # the CONFIGURED DEFAULT itself, not an explicit --model
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            build = _build(Path(cwd))  # model_ref_raw=None -- goes through resolve_default_model_raw
        ctx.check(f"falls all the way through to the hardcoded default, got {build.model_ref.raw}",
                  build.model_ref.raw == DEFAULT_MODEL_REF)
        ctx.check(f"roles.judge was still configured as a side effect, got "
                  f"{get_config_value('roles.judge', default=None)!r}",
                  get_config_value("roles.judge", default=None) == f"dbx:{_OPENJEV}")


if __name__ == "__main__":
    ctx = Ctx()
    results, passed, failed, skipped = run_all(TESTS, ctx)
    sys.exit(print_results(results, passed, failed, skipped))
