"""tests.test_w3b_listing_cwd_settings_flag -- W3b findings 22/23
(docs/harness/RECOMMENDATIONS.md): `listing_effective_env()` now honours an
explicit `cwd` (finding 22) and a `settings_flag` (finding 23), and the
three listing surfaces that call it (`/providers`+`halo providers`,
`halo doctor`, the init tabs' `tab_credential_state`) all thread them
through instead of silently resolving against bare `Path.cwd()`/no
settings-flag -- so a real session (or CLI invocation) launched with an
explicit `--cwd`/`--settings` never disagrees with what these listing
surfaces report.
"""
from __future__ import annotations

import io
import json
import os
import sys
import tempfile
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.helpers.runner import Ctx, new_registry, print_results, run_all

test, TESTS = new_registry()

_PROVIDER_ENV_VARS = (
    "OPENROUTER_API_KEY", "BRIDGE_OPENROUTER_BASE_URL", "DATABRICKS_HOST", "DATABRICKS_TOKEN",
    "BRIDGE_DBX_BASE_URL", "BRIDGE_DBX_TOKEN", "ANTHROPIC_API_KEY", "BRIDGE_ANTHROPIC_BASE_URL",
    "ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN", "TYPESAFE_API_KEY", "BRIDGE_TEST_CC_AUTH_STATUS",
)


class _Env:
    def __enter__(self):
        self._saved = {k: os.environ.get(k) for k in (("BRIDGE_TEST_HOME", "BRIDGE_STATE_DIR",
                                                         "BRIDGE_ENV_FILE") + _PROVIDER_ENV_VARS)}
        d = Path(tempfile.mkdtemp(prefix="w3b-listing-cwd-"))
        os.environ["BRIDGE_TEST_HOME"] = str(d)
        os.environ["BRIDGE_STATE_DIR"] = str(d / ".halo")
        os.environ["BRIDGE_ENV_FILE"] = str(d / "no-such-env-file")
        for k in _PROVIDER_ENV_VARS:
            os.environ.pop(k, None)
        os.environ["BRIDGE_TEST_CC_AUTH_STATUS"] = json.dumps({"loggedIn": False})
        self.home = d
        self.state_dir = d / ".halo"
        return self

    def mark_trusted(self, project: Path) -> None:
        """MERGES into ~/.claude.json's own `projects` map (never
        overwrites a previous `mark_trusted` call for a different project)
        so `is_trusted()` (and therefore `load_settings_env_chain`/
        `resolve_settings`'s own trust filter) treats `project` as trusted
        -- same shape tests/test_mcp_compat_matrix.py already uses."""
        claude_json = self.home / ".claude.json"
        data = {"projects": {}}
        if claude_json.exists():
            try:
                data = json.loads(claude_json.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                data = {"projects": {}}
        data.setdefault("projects", {})
        data["projects"][str(project).replace("\\", "/")] = {"hasTrustDialogAccepted": True}
        data["projects"][str(project).replace("/", "\\")] = {"hasTrustDialogAccepted": True}
        claude_json.write_text(json.dumps(data), encoding="utf-8")

    def write_project_settings_env(self, project: Path, env_block: dict) -> None:
        proj_claude = project / ".claude"
        proj_claude.mkdir(parents=True, exist_ok=True)
        (proj_claude / "settings.json").write_text(json.dumps({"env": env_block}), encoding="utf-8")

    def __exit__(self, *exc):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


# ---------------------------------------------------------------------------
# Finding 22: listing_effective_env(cwd=...) resolves against THAT cwd, not
# bare Path.cwd() -- proven by two trusted project dirs with distinct keys.
# ---------------------------------------------------------------------------

@test
def test_listing_effective_env_honours_explicit_cwd(ctx: Ctx):
    from halo_harness.providers.config import listing_effective_env
    with _Env() as env:
        proj_a = Path(tempfile.mkdtemp(prefix="w3b-proj-a-"))
        proj_b = Path(tempfile.mkdtemp(prefix="w3b-proj-b-"))
        env.mark_trusted(proj_a)
        env.mark_trusted(proj_b)
        env.write_project_settings_env(proj_a, {"OPENROUTER_API_KEY": "sk-or-project-a"})
        env.write_project_settings_env(proj_b, {"OPENROUTER_API_KEY": "sk-or-project-b"})
        env_a = listing_effective_env(cwd=proj_a)
        env_b = listing_effective_env(cwd=proj_b)
        ctx.check(f"cwd=proj_a sees project A's own key, got {env_a.get('OPENROUTER_API_KEY')!r}",
                  env_a.get("OPENROUTER_API_KEY") == "sk-or-project-a")
        ctx.check(f"cwd=proj_b sees project B's own key, got {env_b.get('OPENROUTER_API_KEY')!r}",
                  env_b.get("OPENROUTER_API_KEY") == "sk-or-project-b")


# ---------------------------------------------------------------------------
# Finding 23: listing_effective_env(settings_flag=...) reaches resolve_
# settings()'s own flagSettings layer (never trust-gated, inline JSON or a
# file path, exactly like a real session's --settings).
# ---------------------------------------------------------------------------

@test
def test_listing_effective_env_honours_settings_flag(ctx: Ctx):
    from halo_harness.providers.config import listing_effective_env
    with _Env():
        flag = json.dumps({"env": {"ANTHROPIC_API_KEY": "sk-ant-from-flag"}})
        with_flag = listing_effective_env(settings_flag=flag)
        without_flag = listing_effective_env()
        ctx.check(f"--settings env block reaches effective_env, got {with_flag.get('ANTHROPIC_API_KEY')!r}",
                  with_flag.get("ANTHROPIC_API_KEY") == "sk-ant-from-flag")
        ctx.check("absent without the flag", "ANTHROPIC_API_KEY" not in without_flag)


# ---------------------------------------------------------------------------
# The three listing surfaces thread cwd/settings_flag through to
# listing_effective_env instead of dropping them on the floor.
# ---------------------------------------------------------------------------

@test
def test_provider_rows_threads_settings_flag(ctx: Ctx):
    from halo_harness.providers_cli import provider_rows
    with _Env():
        flag = json.dumps({"env": {"OPENROUTER_API_KEY": "sk-or-from-flag"}})
        rows = {r["name"]: r for r in provider_rows(settings_flag=flag)}
        ctx.check(f"OpenRouter detected from --settings alone, got {rows['openrouter']!r}",
                  rows["openrouter"]["credentials"] is True)


@test
def test_provider_rows_prefers_an_explicit_env_over_rederiving(ctx: Ctx):
    """A live session's own `facade.settings.effective_env` (commands.
    builtins._cmd_providers) must be used VERBATIM, never silently
    re-resolved a second way."""
    from halo_harness.providers_cli import provider_rows
    with _Env():
        rows = {r["name"]: r for r in provider_rows(env={"OPENROUTER_API_KEY": "sk-or-live-session"})}
        ctx.check("OpenRouter detected from the explicit env dict", rows["openrouter"]["credentials"] is True)


@test
def test_doctor_check_providers_enabled_threads_cwd_and_settings_flag(ctx: Ctx):
    from halo_harness.doctor import _check_providers_enabled
    with _Env():
        flag = json.dumps({"env": {"OPENROUTER_API_KEY": "sk-or-from-flag"}})
        line = _check_providers_enabled(None, flag)
        ctx.check(f"doctor's providers-enabled line sees it too, got {line!r}", "openrouter" in line)


@test
def test_tab_credential_state_threads_cwd_and_settings_flag(ctx: Ctx):
    from halo_harness.init_providers import tab_credential_state
    with _Env():
        flag = json.dumps({"env": {"OPENROUTER_API_KEY": "sk-or-from-flag"}})
        state = tab_credential_state("openrouter", settings_flag=flag)
        ctx.check(f"init tabs see a --settings-only key too, got {state!r}", state["configured"] is True)


# ---------------------------------------------------------------------------
# CLI-level: `halo providers`/`halo doctor` accept --cwd/--settings.
# ---------------------------------------------------------------------------

@test
def test_cmd_providers_cli_accepts_settings_flag(ctx: Ctx):
    from halo_harness.providers_cli import cmd_providers
    with _Env():
        flag = json.dumps({"env": {"OPENROUTER_API_KEY": "sk-or-from-flag"}})
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = cmd_providers(["list", "--settings", flag])
        ctx.check(f"exit 0, got {rc}", rc == 0)
        out = buf.getvalue()
        ctx.check(f"OpenRouter shown as auto-detected, got {out!r}",
                  "OpenRouter" in out and "auto (detected" in out)


@test
def test_cmd_doctor_cli_accepts_cwd_and_settings_flag(ctx: Ctx):
    from halo_harness.doctor import cmd_doctor
    with _Env():
        flag = json.dumps({"env": {"OPENROUTER_API_KEY": "sk-or-from-flag"}})
        buf = io.StringIO()
        with redirect_stdout(buf):
            cmd_doctor(["--json", "--cwd", str(Path.cwd()), "--settings", flag])
        checks = json.loads(buf.getvalue())
        row = next(c for c in checks if c["id"] == "providers_enabled")
        ctx.check(f"doctor --json sees the --settings-only key, got {row!r}", "openrouter" in row["message"])


if __name__ == "__main__":
    ctx = Ctx()
    results, passed, failed, skipped = run_all(TESTS, ctx)
    sys.exit(print_results(results, passed, failed, skipped))
