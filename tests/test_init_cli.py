"""tests.test_init_cli -- `halo init` (H12 brief Part A): the whole
first run in one command. Every test runs against an isolated BRIDGE_TEST_
HOME (never the real machine's ~/.config/halo/env, ~/.config/vibes-hacker/env, or ~/.halo)
and a default `BRIDGE_TEST_CC_AUTH_STATUS` of "not logged in" so a doctor
check inside `init` never spawns a real `claude auth status` subprocess
(fast, deterministic, independent of whatever's actually installed on the
box running this suite) -- a test that specifically wants a faked claude.ai
login overrides it explicitly, the same test seam `tests/test_cc_models.py`
documents. A live pong is exercised against `tests.helpers.mock_openai.
MockUpstream` (a loopback-only HTTP server), never a real network call.
"""
from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.helpers.mock_openai import MockUpstream
from tests.helpers.runner import Ctx, new_registry, print_results, run_all
from tests.helpers.provider_env_defaults import ensure_default_provider_credentials

# H15 part 2 addendum 3.1: a believable default credential (never a real
# one) keeps every or:/dbx:/ant: ref below resolving exactly as it did
# before parse_model_ref started refusing an auto-detected-disabled
# provider; no test in this file relies on credential ABSENCE itself.
ensure_default_provider_credentials()

REPO_DIR = Path(__file__).resolve().parent.parent
test, TESTS = new_registry()

_NOT_LOGGED_IN = json.dumps({"loggedIn": False})
_LOGGED_IN_CLAUDE_AI = json.dumps({"loggedIn": True, "authMethod": "claude.ai"})


def _fresh_home() -> Path:
    return Path(tempfile.mkdtemp(prefix="halo-init-"))


def _run(argv, home: Path, *, stdin: str = "", extra_env: "dict | None" = None, timeout: int = 40):
    # Hermetic child env: drop every harness/provider variable the parent
    # process (or an earlier test module) may carry, so an `init` under
    # test only ever sees what THIS test passes in.
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("BRIDGE_", "OPENROUTER_", "DATABRICKS_", "ANTHROPIC_",
                                "ROLO_CLAUDE_", "HALO_", "TYPESAFE_"))}
    env.update({"BRIDGE_TEST_HOME": str(home), "PYTHONPATH": str(REPO_DIR),
                "BRIDGE_TEST_CC_AUTH_STATUS": _NOT_LOGGED_IN})
    env.update(extra_env or {})
    return subprocess.run([sys.executable, "-m", "halo_harness"] + argv, env=env, cwd=str(REPO_DIR),
                           capture_output=True, text=True, encoding="utf-8", errors="replace",
                           input=stdin, timeout=timeout)


def _env_file(home: Path) -> Path:
    """2.0.0 rename: `halo init` now writes the NEW default path -- the
    legacy `~/.config/vibes-hacker/env` is still READ (see
    test_env_file_still_read_from_legacy_vibes_hacker_path_when_new_
    absent below) but never written to again."""
    return home / ".config" / "halo" / "env"


# ---------------------------------------------------------------------------
# Part A step 2/7: credentials, env-file mode bits, idempotency
# ---------------------------------------------------------------------------

@test
def test_home_preset_stdin_key_writes_env_file_and_config(ctx: Ctx):
    home = _fresh_home()
    result = _run(["init", "--preset", "home", "--yes", "--no-live"], home, stdin="sk-or-fake-key-123\n")
    ctx.check(f"exit 0, got {result.returncode}, stderr={result.stderr!r}", result.returncode == 0)
    ctx.check("the key is never printed to stdout", "sk-or-fake-key-123" not in result.stdout)
    ctx.check("the key is never printed to stderr", "sk-or-fake-key-123" not in result.stderr)

    env_path = _env_file(home)
    ctx.check(f"the env file was written at {env_path}", env_path.exists())
    content = env_path.read_text(encoding="utf-8")
    ctx.check(f"it carries the key, got {content!r}", "OPENROUTER_API_KEY=sk-or-fake-key-123" in content)

    cfg_path = home / ".halo" / "config.json"
    ctx.check(f"config.json was written at {cfg_path}", cfg_path.exists())
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    ctx.check(f"model set to the home default, got {cfg}", cfg.get("model") == "or:deepseek/deepseek-v4.1-flash")

    ctx.check("never writes ~/.claude.json", not (home / ".claude.json").exists())
    ctx.check("never writes ~/.claude/settings.json", not (home / ".claude" / "settings.json").exists())

    if os.name != "nt":
        ctx.check(f"env file mode 0600, got {oct(stat.S_IMODE(env_path.stat().st_mode))}",
                   stat.S_IMODE(env_path.stat().st_mode) == 0o600)
        dir_mode = stat.S_IMODE(env_path.parent.stat().st_mode)
        ctx.check(f"env dir mode 0700, got {oct(dir_mode)}", dir_mode == 0o700)


@test
def test_home_preset_key_already_in_env_var_not_rewritten(ctx: Ctx):
    """Already-discoverable (ambient env, not yet a stdin/getpass entry) --
    reported as configured, the env file is never created at all (nothing
    to write -- the credential already resolves without it)."""
    home = _fresh_home()
    result = _run(["init", "--preset", "home", "--yes", "--no-live"], home,
                   extra_env={"OPENROUTER_API_KEY": "sk-or-ambient-key"})
    ctx.check(f"exit 0, got {result.returncode}", result.returncode == 0)
    ctx.check("reports already configured", "already configured" in result.stdout)
    ctx.check("the key is never printed", "sk-or-ambient-key" not in result.stdout)
    ctx.check("no env file needed to be created", not _env_file(home).exists())


@test
def test_home_preset_rerun_is_idempotent(ctx: Ctx):
    home = _fresh_home()
    first = _run(["init", "--preset", "home", "--yes", "--no-live"], home, stdin="sk-or-fake-key-abc\n")
    ctx.check(f"first run exit 0, got {first.returncode}", first.returncode == 0)
    env_path = _env_file(home)
    before = env_path.read_text(encoding="utf-8")

    second = _run(["init", "--preset", "home", "--yes", "--no-live"], home, stdin="")
    ctx.check(f"second run exit 0, got {second.returncode}, stderr={second.stderr!r}", second.returncode == 0)
    ctx.check("second run reports the CURRENT state (already configured)", "already configured" in second.stdout)
    after = env_path.read_text(encoding="utf-8")
    ctx.check("the env file is byte-identical after a no-op re-run", before == after)
    ctx.check("only one OPENROUTER_API_KEY= line exists (never duplicated)",
              after.count("OPENROUTER_API_KEY=") == 1)


@test
def test_no_key_entered_warns_but_does_not_crash(ctx: Ctx):
    home = _fresh_home()
    result = _run(["init", "--preset", "home", "--yes", "--no-live"], home, stdin="")
    ctx.check(f"exit 0 (no live pong attempted, so nothing to fail on), got {result.returncode}",
              result.returncode == 0)
    ctx.check("warns that no key was entered", "no key entered" in result.stdout.lower()
              or "not found" in result.stdout.lower())
    ctx.check("no traceback", "Traceback" not in result.stderr)


# ---------------------------------------------------------------------------
# Part A step 1: preset detection / gating
# ---------------------------------------------------------------------------

@test
def test_preset_claude_rejected_without_a_login(ctx: Ctx):
    home = _fresh_home()
    result = _run(["init", "--preset", "claude", "--yes", "--no-live"], home)
    ctx.check(f"exit 2 (usage/config error), got {result.returncode}", result.returncode == 2)
    ctx.check("explains why", "claude.ai login" in result.stdout or "claude.ai login" in result.stderr)


@test
def test_preset_claude_accepted_with_a_faked_login(ctx: Ctx):
    """`fake_claude_cc`-style login fake: BRIDGE_TEST_CC_AUTH_STATUS short-
    circuits `claude auth status` (the same test seam tests/test_cc_models.py
    documents) to report a real claude.ai login with no subprocess at all."""
    home = _fresh_home()
    result = _run(["init", "--preset", "claude", "--yes", "--no-live"], home,
                   extra_env={"BRIDGE_TEST_CC_AUTH_STATUS": _LOGGED_IN_CLAUDE_AI})
    ctx.check(f"exit 0, got {result.returncode}, stderr={result.stderr!r}", result.returncode == 0)
    ctx.check("the claude provider path ran", "Claude subscription" in result.stdout)
    ctx.check("credentials step stores nothing", "nothing stored" in result.stdout)
    cfg = json.loads((home / ".halo" / "config.json").read_text(encoding="utf-8"))
    ctx.check(f"default model is cc:sonnet, got {cfg}", cfg.get("model") == "cc:sonnet")


@test
def test_preset_home_and_work_never_probe_claude_login(ctx: Ctx):
    """An explicit --preset home/work never needs to know whether a claude.ai
    login exists at all -- BRIDGE_TEST_CC_AUTH_STATUS is deliberately left
    UNPARSEABLE here; if `_step_preset` ever called claude_auth_status() for
    an explicit non-claude preset, doctor's own subscription check (step 4,
    which always runs) would still tolerate it, but this test's OWN point is
    the preset step itself never needs to for these two presets -- proven by
    it never crashing even with a broken override in place, same as always."""
    home = _fresh_home()
    result = _run(["init", "--preset", "home", "--yes", "--no-live"], home, stdin="sk-or-x\n",
                   extra_env={"BRIDGE_TEST_CC_AUTH_STATUS": "not json at all"})
    ctx.check(f"exit 0, got {result.returncode}", result.returncode == 0)
    ctx.check("no traceback", "Traceback" not in result.stderr)


# ---------------------------------------------------------------------------
# Part A step 2 (Databricks) / step 3 (model) / never touching claude files
# ---------------------------------------------------------------------------

@test
def test_work_preset_host_and_token_via_stdin(ctx: Ctx):
    home = _fresh_home()
    result = _run(["init", "--preset", "work", "--yes", "--no-live"], home,
                   stdin="https://fake-ws.cloud.databricks.com\nfake-token-999\n")
    ctx.check(f"exit 0, got {result.returncode}, stderr={result.stderr!r}", result.returncode == 0)
    ctx.check("token never printed", "fake-token-999" not in result.stdout)
    content = _env_file(home).read_text(encoding="utf-8")
    ctx.check(f"host written, got {content!r}", "DATABRICKS_HOST=https://fake-ws.cloud.databricks.com" in content)
    ctx.check(f"token written, got {content!r}", "DATABRICKS_TOKEN=fake-token-999" in content)
    cfg = json.loads((home / ".halo" / "config.json").read_text(encoding="utf-8"))
    ctx.check(f"model set to the work default, got {cfg}",
              cfg.get("model") == "dbx:databricks-deepseek-v4-1-flash")


@test
def test_work_preset_already_configured_not_rewritten(ctx: Ctx):
    home = _fresh_home()
    result = _run(["init", "--preset", "work", "--yes", "--no-live"], home,
                   extra_env={"DATABRICKS_HOST": "https://ambient.cloud.databricks.com",
                              "DATABRICKS_TOKEN": "ambient-token"})
    ctx.check(f"exit 0, got {result.returncode}", result.returncode == 0)
    ctx.check("reports already configured", "already configured" in result.stdout)
    ctx.check("token never printed", "ambient-token" not in result.stdout)
    ctx.check("no env file needed to be created", not _env_file(home).exists())


@test
def test_model_flag_overrides_preset_default(ctx: Ctx):
    home = _fresh_home()
    result = _run(["init", "--preset", "home", "--yes", "--no-live", "--model", "or:deepseek/deepseek-v3.2"],
                   home, stdin="sk-or-x\n")
    ctx.check(f"exit 0, got {result.returncode}", result.returncode == 0)
    cfg = json.loads((home / ".halo" / "config.json").read_text(encoding="utf-8"))
    ctx.check(f"--model wins over the preset default, got {cfg}", cfg.get("model") == "or:deepseek/deepseek-v3.2")


# ---------------------------------------------------------------------------
# Part A step 3: model precedence in the real resolver both -p and the TUI use
# ---------------------------------------------------------------------------

@test
def test_model_precedence_flag_then_config_then_builtin_default(ctx: Ctx):
    from halo_harness.model import DEFAULT_MODEL_REF, resolve_default_model_raw
    from halo_harness.theme import set_config_value
    home = _fresh_home()
    old_home = os.environ.get("BRIDGE_TEST_HOME")
    os.environ["BRIDGE_TEST_HOME"] = str(home)
    old_bridge_model = os.environ.pop("BRIDGE_MODEL", None)
    try:
        ctx.check(f"nothing configured -> the built-in default, got {resolve_default_model_raw({})!r}",
                  resolve_default_model_raw({}) == DEFAULT_MODEL_REF)

        set_config_value("model", "or:deepseek/deepseek-v3.2")
        ctx.check("config.json's model wins over the built-in default",
                  resolve_default_model_raw({}) == "or:deepseek/deepseek-v3.2")

        ctx.check("routes.json's own 'default' still wins over config.json (pre-existing precedence)",
                  resolve_default_model_raw({"default": "or:from-routes/model"}) == "or:from-routes/model")

        os.environ["BRIDGE_MODEL"] = "or:from-env/model"
        ctx.check("BRIDGE_MODEL still wins over everything below it (pre-existing precedence)",
                  resolve_default_model_raw({"default": "or:from-routes/model"}) == "or:from-env/model")
    finally:
        os.environ.pop("BRIDGE_MODEL", None)
        if old_bridge_model is not None:
            os.environ["BRIDGE_MODEL"] = old_bridge_model
        if old_home is not None:
            os.environ["BRIDGE_TEST_HOME"] = old_home
        else:
            os.environ.pop("BRIDGE_TEST_HOME", None)


@test
def test_headless_build_session_honours_config_json_model(ctx: Ctx):
    """The one shared session builder both `-p` and the TUI call -- proof
    that a `--model` flag beats config.json's own model, which in turn beats
    the hardcoded default, at the ACTUAL session-construction call site."""
    from halo_harness import headless
    from halo_harness.theme import set_config_value
    home = _fresh_home()
    old_home = os.environ.get("BRIDGE_TEST_HOME")
    os.environ["BRIDGE_TEST_HOME"] = str(home)
    try:
        set_config_value("model", "or:deepseek/deepseek-v3.2")
        build = headless.build_session(cwd=home, bare=True, print_mode=True)
        ctx.check(f"no --model given -> config.json's model wins, got {build.model_ref.raw!r}",
                  build.model_ref.raw == "or:deepseek/deepseek-v3.2")

        build2 = headless.build_session(cwd=home, model_ref_raw="or:deepseek/deepseek-chat-v3.1",
                                         bare=True, print_mode=True)
        ctx.check(f"--model still wins over config.json, got {build2.model_ref.raw!r}",
                  build2.model_ref.raw == "or:deepseek/deepseek-chat-v3.1")
    finally:
        if old_home is not None:
            os.environ["BRIDGE_TEST_HOME"] = old_home
        else:
            os.environ.pop("BRIDGE_TEST_HOME", None)


# ---------------------------------------------------------------------------
# Part A step 6: Linux fixes (rc-file marker, rg fake download, --no-fixes)
# ---------------------------------------------------------------------------

@test
def test_no_fixes_flag_skips_the_whole_step(ctx: Ctx):
    home = _fresh_home()
    result = _run(["init", "--preset", "home", "--yes", "--no-live", "--no-fixes"], home, stdin="sk-or-x\n")
    ctx.check(f"exit 0, got {result.returncode}", result.returncode == 0)
    ctx.check("step 6 reports skipped", "skipped (--no-fixes)" in result.stdout)


@test
def test_rc_file_line_written_once_with_marker(ctx: Ctx):
    """`ensure_local_bin_on_rc`/`rc_file_for_shell` (shared by doctor.py's
    own check and this init step): zsh -> ~/.zshenv, else ~/.profile;
    idempotent -- a second call is a pure no-op, never a duplicate line."""
    from halo_harness.linux_fixes import PATH_LINE, PATH_MARKER, ensure_local_bin_on_rc, rc_file_for_shell
    home = _fresh_home()
    ctx.check("zsh -> ~/.zshenv", rc_file_for_shell("/usr/bin/zsh", home=home) == home / ".zshenv")
    ctx.check("bash -> ~/.profile", rc_file_for_shell("/bin/bash", home=home) == home / ".profile")
    ctx.check("unknown/empty $SHELL -> ~/.profile", rc_file_for_shell("", home=home) == home / ".profile")

    written1, path1 = ensure_local_bin_on_rc(shell="/bin/bash", home=home)
    ctx.check("first call writes it", written1 and path1 == home / ".profile")
    written2, path2 = ensure_local_bin_on_rc(shell="/bin/bash", home=home)
    ctx.check("second call is a no-op", not written2 and path2 == path1)

    content = path1.read_text(encoding="utf-8")
    ctx.check("marker present exactly once", content.count(PATH_MARKER) == 1)
    ctx.check("PATH line present exactly once", content.count(PATH_LINE) == 1)


@test
def test_ripgrep_install_exercised_with_a_fake_download(ctx: Ctx):
    """No real network: `fetch_json`/`fetch_bytes` are swapped for fakes
    returning a genuine in-memory tar.gz built with the stdlib `tarfile`
    module, so real extraction/chmod/verify logic still runs end to end."""
    import io
    import tarfile

    from halo_harness.linux_fixes import install_static_ripgrep, pick_ripgrep_asset, ripgrep_asset_suffix

    ctx.check("linux x86_64 -> the musl asset suffix",
              ripgrep_asset_suffix(system="Linux", machine="x86_64") == "x86_64-unknown-linux-musl.tar.gz")
    ctx.check("windows -> no asset (this fix is Linux-only)",
              ripgrep_asset_suffix(system="Windows", machine="AMD64") is None)

    release = {"assets": [
        {"name": "ripgrep-14.1.0-x86_64-unknown-linux-musl.tar.gz",
         "browser_download_url": "https://example.invalid/rg.tar.gz"},
        {"name": "ripgrep-14.1.0-x86_64-pc-windows-msvc.zip",
         "browser_download_url": "https://example.invalid/rg.zip"},
    ]}
    asset = pick_ripgrep_asset(release, system="Linux", machine="x86_64")
    ctx.check(f"picks the musl tarball, got {asset}", asset is not None and asset["name"].endswith("musl.tar.gz"))

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        payload = b"#!/bin/sh\necho fake-rg\n"
        info = tarfile.TarInfo(name="ripgrep-14.1.0-x86_64-unknown-linux-musl/rg")
        info.size = len(payload)
        info.mode = 0o755
        tf.addfile(info, io.BytesIO(payload))
    archive_bytes = buf.getvalue()

    dest = _fresh_home() / "rg-dest"
    ok, msg = install_static_ripgrep(
        dest, system="Linux", machine="x86_64",
        fetch_json=lambda url: release, fetch_bytes=lambda url: archive_bytes,
        verify=lambda p: p.exists(),
    )
    ctx.check(f"reports success, got ({ok}, {msg!r})", ok and msg == str(dest / "rg"))
    ctx.check("the fake rg binary actually landed on disk", (dest / "rg").exists())

    ok2, msg2 = install_static_ripgrep(dest, system="Darwin", machine="x86_64",
                                        fetch_json=lambda url: release, fetch_bytes=lambda url: b"")
    ctx.check(f"an unsupported OS falls back to a package-manager hint, got ({ok2}, {msg2!r})",
              ok2 is False and isinstance(msg2, str) and msg2)


# ---------------------------------------------------------------------------
# Part A step 5: live pong (against a local mock, never a real network call)
# ---------------------------------------------------------------------------

@test
def test_live_pong_success_against_mock_upstream(ctx: Ctx):
    home = _fresh_home()
    mock = MockUpstream().start()
    try:
        result = _run(["init", "--preset", "home", "--yes", "--model", "or:mock/model"], home,
                       stdin="sk-or-x\n",
                       extra_env={"BRIDGE_OPENROUTER_BASE_URL": mock.base_url})
        ctx.check(f"exit 0, got {result.returncode}, stdout={result.stdout!r} stderr={result.stderr!r}",
                  result.returncode == 0)
        ctx.check("live pong header printed", "Live pong (" in result.stdout)
        ctx.check("reports the reply/model/provider/cost line",
                  "reply='pong'" in result.stdout and "provider=openrouter" in result.stdout)
        ctx.check("no key ever printed", "sk-or-x" not in result.stdout)
        ctx.check("summary points at starting the real thing", "Run `halo`" in result.stdout)
    finally:
        mock.stop()


@test
def test_live_pong_failure_is_a_nonzero_exit_with_a_hint(ctx: Ctx):
    home = _fresh_home()
    mock = MockUpstream().start()
    try:
        result = _run(["init", "--preset", "home", "--yes", "--model", "or:mock/error-401"], home,
                       stdin="sk-or-x\n",
                       extra_env={"BRIDGE_OPENROUTER_BASE_URL": mock.base_url})
        ctx.check(f"nonzero exit when the pong fails, got {result.returncode}", result.returncode != 0)
        ctx.check("step 5 reports a WARN", "[WARN]" in result.stdout)
        ctx.check("summary names a next step", "Next:" in result.stdout)
    finally:
        mock.stop()


@test
def test_no_live_skips_pong_and_catalog_refresh(ctx: Ctx):
    home = _fresh_home()
    result = _run(["init", "--preset", "home", "--yes", "--no-live"], home, stdin="sk-or-x\n")
    ctx.check(f"exit 0, got {result.returncode}", result.returncode == 0)
    ctx.check("live pong explicitly skipped", "Live pong: skipped (--no-live)" in result.stdout)
    ctx.check("catalog refresh explicitly skipped", "catalog refresh skipped" in result.stdout)


# ---------------------------------------------------------------------------
# Part A: wired into cli.py's help
# ---------------------------------------------------------------------------

@test
def test_top_level_help_epilog_mentions_init(ctx: Ctx):
    from halo_harness.cli import _build_parser
    help_text = _build_parser().format_help()
    ctx.check("epilog lists the init command", "init" in help_text.split("Commands:")[-1])


@test
def test_init_help_runs_standalone(ctx: Ctx):
    home = _fresh_home()
    result = _run(["init", "--help"], home)
    ctx.check(f"exit 0, got {result.returncode}", result.returncode == 0)
    ctx.check("mentions every flag", all(f in result.stdout for f in
              ("--preset", "--model", "--yes", "--no-live", "--no-fixes")))


# ---------------------------------------------------------------------------
# 1.0.1 hotfix 5: the interactive model picker -- --yes/--model skip it;
# a non-tty run (every subprocess test here -- stdin/stdout both piped)
# falls back to the numbered list, one stdin line for the choice.
# ---------------------------------------------------------------------------

_ONE_ENDPOINT_PER_FAMILY_PARSED = [
    {"name": "databricks-glm-5-3", "task": "llm/v1/chat", "ready": True, "permission_level": None,
     "endpoint_type": None, "ai_gateway_v2_supported": None,
     "api_types": ["mlflow/v1/chat/completions"], "foundation_model_name": "glm-5-3", "model_class": None},
    {"name": "databricks-kimi-k3", "task": "llm/v1/chat", "ready": True, "permission_level": None,
     "endpoint_type": None, "ai_gateway_v2_supported": None,
     "api_types": ["mlflow/v1/chat/completions"], "foundation_model_name": "kimi-k3", "model_class": None},
    {"name": "databricks-deepseek-v4-1-flash", "task": "llm/v1/chat", "ready": True, "permission_level": None,
     "endpoint_type": None, "ai_gateway_v2_supported": None,
     "api_types": ["mlflow/v1/chat/completions"], "foundation_model_name": "deepseek-v4-1-flash",
     "model_class": None},
]


def _seed_dbx_cache(home: Path) -> None:
    from halo_harness.providers.databricks import write_dbx_endpoints_json
    write_dbx_endpoints_json(home / ".halo", _ONE_ENDPOINT_PER_FAMILY_PARSED)


@test
def test_picker_skipped_with_yes_even_with_a_cached_catalog(ctx: Ctx):
    home = _fresh_home()
    _seed_dbx_cache(home)
    result = _run(["init", "--preset", "work", "--yes", "--no-live"], home,
                   extra_env={"DATABRICKS_HOST": "https://fake-ws.cloud.databricks.com",
                              "DATABRICKS_TOKEN": "fake-token"})
    ctx.check(f"exit 0, got {result.returncode}, stderr={result.stderr!r}", result.returncode == 0)
    ctx.check("no picker prompt printed", "Pick a default model" not in result.stdout)
    cfg = json.loads((home / ".halo" / "config.json").read_text(encoding="utf-8"))
    ctx.check(f"still the plain preset default, got {cfg}",
              cfg.get("model") == "dbx:databricks-deepseek-v4-1-flash")


@test
def test_picker_skipped_when_model_flag_given(ctx: Ctx):
    home = _fresh_home()
    _seed_dbx_cache(home)
    result = _run(["init", "--preset", "work", "--model", "dbx:databricks-kimi-k3", "--no-live"], home,
                   extra_env={"DATABRICKS_HOST": "https://fake-ws.cloud.databricks.com",
                              "DATABRICKS_TOKEN": "fake-token"})
    ctx.check(f"exit 0, got {result.returncode}, stderr={result.stderr!r}", result.returncode == 0)
    ctx.check("no picker prompt printed", "Pick a default model" not in result.stdout)
    cfg = json.loads((home / ".halo" / "config.json").read_text(encoding="utf-8"))
    ctx.check(f"the explicit --model wins, got {cfg}", cfg.get("model") == "dbx:databricks-kimi-k3")


@test
def test_numbered_fallback_picks_the_chosen_entry_non_tty(ctx: Ctx):
    """Every `_run` invocation here has stdin/stdout both piped (never a
    real tty), so this exercises the numbered-list fallback exactly as a
    piped/non-interactive box would see it."""
    home = _fresh_home()
    _seed_dbx_cache(home)
    # No --yes: the picker step runs; the numbered list is alphabetical by
    # endpoint name (deepseek, glm, kimi) -- "2" is databricks-glm-5-3.
    result = _run(["init", "--preset", "work", "--no-live"], home,
                   stdin="2\n",
                   extra_env={"DATABRICKS_HOST": "https://fake-ws.cloud.databricks.com",
                              "DATABRICKS_TOKEN": "fake-token"})
    ctx.check(f"exit 0, got {result.returncode}, stderr={result.stderr!r}", result.returncode == 0)
    ctx.check("the numbered picker was actually offered", "Pick a default model" in result.stdout)
    cfg = json.loads((home / ".halo" / "config.json").read_text(encoding="utf-8"))
    ctx.check(f"the chosen (2nd) endpoint was written, got {cfg}",
              cfg.get("model") == "dbx:databricks-glm-5-3")


@test
def test_numbered_fallback_empty_input_keeps_the_preset_default(ctx: Ctx):
    home = _fresh_home()
    _seed_dbx_cache(home)
    result = _run(["init", "--preset", "work", "--no-live"], home, stdin="\n",
                   extra_env={"DATABRICKS_HOST": "https://fake-ws.cloud.databricks.com",
                              "DATABRICKS_TOKEN": "fake-token"})
    ctx.check(f"exit 0, got {result.returncode}, stderr={result.stderr!r}", result.returncode == 0)
    cfg = json.loads((home / ".halo" / "config.json").read_text(encoding="utf-8"))
    ctx.check(f"unchanged preset default, got {cfg}",
              cfg.get("model") == "dbx:databricks-deepseek-v4-1-flash")


@test
def test_picker_offers_nothing_without_a_cached_catalog(ctx: Ctx):
    home = _fresh_home()
    result = _run(["init", "--preset", "work", "--no-live"], home, stdin="\n",
                   extra_env={"DATABRICKS_HOST": "https://fake-ws.cloud.databricks.com",
                              "DATABRICKS_TOKEN": "fake-token"})
    ctx.check(f"exit 0, got {result.returncode}, stderr={result.stderr!r}", result.returncode == 0)
    ctx.check("no picker prompt with nothing cached", "Pick a default model" not in result.stdout)


# ---------------------------------------------------------------------------
# 1.0.1 hotfix 13: provider-first init -- "select a provider to set up"
# instead of a home/work/claude preset. `--preset` stays as a deprecated
# `--provider` alias; `--provider` (repeatable) drives the non-interactive
# path directly; the fully interactive picker loop + cross-provider default
# pick are exercised in-process with the picker/confirm steps monkeypatched
# (a real subprocess has no tty to drive a Textual app through anyway).
# ---------------------------------------------------------------------------

@test
def test_provider_databricks_yes_non_interactive(ctx: Ctx):
    home = _fresh_home()
    result = _run(["init", "--provider", "databricks", "--yes", "--no-live"], home,
                   extra_env={"DATABRICKS_HOST": "https://fake-ws.cloud.databricks.com",
                              "DATABRICKS_TOKEN": "fake-token"})
    ctx.check(f"exit 0, got {result.returncode}, stderr={result.stderr!r}", result.returncode == 0)
    ctx.check("the databricks provider path ran", "Databricks" in result.stdout)
    ctx.check("no preset/home/work wording anywhere", not any(
        w in result.stdout for w in ("Preset:", "home preset", "work preset")))
    cfg = json.loads((home / ".halo" / "config.json").read_text(encoding="utf-8"))
    ctx.check(f"model set to the databricks default, got {cfg}",
              cfg.get("model") == "dbx:databricks-deepseek-v4-1-flash")


@test
def test_provider_openrouter_yes_non_interactive(ctx: Ctx):
    home = _fresh_home()
    result = _run(["init", "--provider", "openrouter", "--yes", "--no-live"], home, stdin="sk-or-x\n")
    ctx.check(f"exit 0, got {result.returncode}, stderr={result.stderr!r}", result.returncode == 0)
    cfg = json.loads((home / ".halo" / "config.json").read_text(encoding="utf-8"))
    ctx.check(f"model set to the openrouter default, got {cfg}",
              cfg.get("model") == "or:deepseek/deepseek-v4.1-flash")


@test
def test_provider_anthropic_yes_non_interactive_writes_api_key(ctx: Ctx):
    home = _fresh_home()
    result = _run(["init", "--provider", "anthropic", "--yes", "--no-live"], home, stdin="sk-ant-fake-1\n")
    ctx.check(f"exit 0, got {result.returncode}, stderr={result.stderr!r}", result.returncode == 0)
    ctx.check("the key is never printed", "sk-ant-fake-1" not in result.stdout)
    content = _env_file(home).read_text(encoding="utf-8")
    ctx.check(f"ANTHROPIC_API_KEY written, got {content!r}", "ANTHROPIC_API_KEY=sk-ant-fake-1" in content)
    cfg = json.loads((home / ".halo" / "config.json").read_text(encoding="utf-8"))
    ctx.check(f"model set to the anthropic default, got {cfg}", cfg.get("model") == "ant:sonnet")


@test
def test_provider_anthropic_already_configured_not_rewritten(ctx: Ctx):
    home = _fresh_home()
    result = _run(["init", "--provider", "anthropic", "--yes", "--no-live"], home,
                   extra_env={"ANTHROPIC_API_KEY": "sk-ant-ambient"})
    ctx.check(f"exit 0, got {result.returncode}", result.returncode == 0)
    ctx.check("reports already configured", "already configured" in result.stdout)
    ctx.check("the key is never printed", "sk-ant-ambient" not in result.stdout)
    ctx.check("no env file needed to be created", not _env_file(home).exists())


@test
def test_provider_requires_at_least_one_value(ctx: Ctx):
    """`--provider` with no value at all is an argparse error (exit 2);
    covered here mainly so a FUTURE argparse config change that silently
    allows an empty list is caught (the guard in cmd_init is otherwise
    unreachable via argparse's own `action="append"`)."""
    home = _fresh_home()
    result = _run(["init", "--provider"], home)
    ctx.check(f"argparse usage error, got {result.returncode}", result.returncode == 2)


@test
def test_preset_alias_prints_a_deprecation_notice_and_behaves_like_provider(ctx: Ctx):
    home = _fresh_home()
    result = _run(["init", "--preset", "work", "--yes", "--no-live"], home,
                   extra_env={"DATABRICKS_HOST": "https://fake-ws.cloud.databricks.com",
                              "DATABRICKS_TOKEN": "fake-token"})
    ctx.check(f"exit 0, got {result.returncode}", result.returncode == 0)
    ctx.check("prints a one-line deprecation notice naming the real provider",
              "deprecated alias for --provider databricks" in result.stdout)


@test
def test_unconfigured_provider_status_tag(ctx: Ctx):
    from halo_harness.init_providers import provider_status
    home = _fresh_home()
    old_home = os.environ.get("BRIDGE_TEST_HOME")
    old_auth = os.environ.get("BRIDGE_TEST_CC_AUTH_STATUS")
    # 1.0.1 part 2 fixpass finding 10: snapshot every provider var cleared
    # below so the `finally` can RESTORE them (not just pop them) --
    # popping without restoring leaked into test_scripted_provider_loop_...
    # (this module's own `ensure_default_provider_credentials()` module-
    # level default), making ITS cross-provider pick see 2 or 3 configured
    # providers depending on which order the test runner happened to pick.
    old_provider_env = {k: os.environ.get(k) for k in
                         ("OPENROUTER_API_KEY", "DATABRICKS_HOST", "DATABRICKS_TOKEN", "ANTHROPIC_API_KEY",
                          "ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN")}
    os.environ["BRIDGE_TEST_HOME"] = str(home)
    os.environ["BRIDGE_TEST_CC_AUTH_STATUS"] = _NOT_LOGGED_IN
    for k in ("OPENROUTER_API_KEY", "DATABRICKS_HOST", "DATABRICKS_TOKEN", "ANTHROPIC_API_KEY",
              "ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN"):
        os.environ.pop(k, None)
    try:
        for p in ("databricks", "openrouter", "anthropic", "claude"):
            ctx.check(f"{p} starts out 'not set up', got {provider_status(p)!r}",
                      provider_status(p) == "not set up")
        os.environ["OPENROUTER_API_KEY"] = "sk-or-x"
        ctx.check(f"openrouter flips to configured, got {provider_status('openrouter')!r}",
                  provider_status("openrouter") == "configured")
        os.environ["BRIDGE_TEST_CC_AUTH_STATUS"] = _LOGGED_IN_CLAUDE_AI
        ctx.check(f"claude flips to 'logged in', got {provider_status('claude')!r}",
                  provider_status("claude") == "logged in")
    finally:
        for k, v in old_provider_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        if old_home is None:
            os.environ.pop("BRIDGE_TEST_HOME", None)
        else:
            os.environ["BRIDGE_TEST_HOME"] = old_home
        if old_auth is None:
            os.environ.pop("BRIDGE_TEST_CC_AUTH_STATUS", None)
        else:
            os.environ["BRIDGE_TEST_CC_AUTH_STATUS"] = old_auth


@test
def test_scripted_provider_loop_sets_up_two_then_done_then_cross_provider_pick(ctx: Ctx):
    """In-process (never a subprocess -- a real tty-driven Textual picker
    has no piped-stdin equivalent to script): monkeypatches `_step_select_
    provider`/`_confirm`/`_run_entry_picker` to walk databricks then
    openrouter, say "yes" to "set up another?" once and "no" the second
    time, then answers the final cross-provider pick explicitly."""
    import halo_harness.init_cli as init_cli

    home = _fresh_home()
    old_home = os.environ.get("BRIDGE_TEST_HOME")
    old_env_file = os.environ.get("BRIDGE_ENV_FILE")
    old_auth = os.environ.get("BRIDGE_TEST_CC_AUTH_STATUS")
    os.environ["BRIDGE_TEST_HOME"] = str(home)
    os.environ["BRIDGE_ENV_FILE"] = str(home / ".config" / "vibes-hacker" / "env")
    os.environ["BRIDGE_TEST_CC_AUTH_STATUS"] = _NOT_LOGGED_IN
    os.environ["DATABRICKS_HOST"] = "https://fake-ws.cloud.databricks.com"
    os.environ["DATABRICKS_TOKEN"] = "fake-token"
    os.environ["OPENROUTER_API_KEY"] = "sk-or-fake"
    # Both providers' catalogs are pre-seeded (rather than a live --refresh,
    # which --no-live skips) so the final cross-provider pick has real
    # entries from EACH provider to choose between.
    from halo_harness.providers.databricks import write_dbx_endpoints_json, write_models_json
    write_models_json(home / ".halo", [{"id": "vendor/model-x", "context_length": 128000,
                                                "max_output_tokens": 8192}])
    write_dbx_endpoints_json(home / ".halo", _ONE_ENDPOINT_PER_FAMILY_PARSED)

    real_select = init_cli._step_select_provider
    real_confirm = init_cli._confirm
    real_run_entry_picker = init_cli._run_entry_picker
    select_calls = iter(["databricks", "openrouter"])
    confirm_calls = iter([True, False])

    def _fake_select(args, console, *, header):
        try:
            return next(select_calls)
        except StopIteration:
            return "done"

    def _fake_confirm(args, console, question, *, default):
        try:
            return next(confirm_calls)
        except StopIteration:
            return False

    def _fake_run_entry_picker(args, console, entries):
        # The final cross-provider pick: explicitly choose the OpenRouter
        # entry if present, proving the picker really did see BOTH
        # providers' catalogs merged together.
        for e in entries:
            if e["ref"].startswith("or:"):
                return e["ref"]
        return None

    init_cli._step_select_provider = _fake_select
    init_cli._confirm = _fake_confirm
    init_cli._run_entry_picker = _fake_run_entry_picker
    try:
        rc = init_cli.cmd_init(["--no-live"])
        ctx.check(f"exit 0, got {rc}", rc == 0)
        cfg = json.loads((home / ".halo" / "config.json").read_text(encoding="utf-8"))
        ctx.check(f"both providers' credentials resolve now, got databricks="
                  f"{init_cli.provider_status('databricks')!r} openrouter={init_cli.provider_status('openrouter')!r}",
                  init_cli.provider_status("databricks") == "configured"
                  and init_cli.provider_status("openrouter") == "configured")
        ctx.check(f"the cross-provider pick (an OpenRouter entry) won, got {cfg}",
                  isinstance(cfg.get("model"), str) and cfg["model"].startswith("or:"))
    finally:
        init_cli._step_select_provider = real_select
        init_cli._confirm = real_confirm
        init_cli._run_entry_picker = real_run_entry_picker
        for k in ("DATABRICKS_HOST", "DATABRICKS_TOKEN", "OPENROUTER_API_KEY"):
            os.environ.pop(k, None)
        for k, old in (("BRIDGE_TEST_HOME", old_home), ("BRIDGE_ENV_FILE", old_env_file),
                       ("BRIDGE_TEST_CC_AUTH_STATUS", old_auth)):
            if old is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = old


# ---------------------------------------------------------------------------
# 1.0.1 part 2 fixpass finding 15: a successful provider setup must not
# write a PERMANENT `enabled: true` override unless it's flipping an
# existing EXPLICIT `enabled: false` -- auto-detection already covers the
# ordinary case live, every time.
# ---------------------------------------------------------------------------

@test
def test_provider_setup_does_not_write_a_permanent_enabled_override_on_a_fresh_box(ctx: Ctx):
    home = _fresh_home()
    result = _run(["init", "--provider", "openrouter", "--yes", "--no-live"], home,
                   extra_env={"OPENROUTER_API_KEY": "sk-or-fake"})
    ctx.check(f"exit 0, got {result.returncode}", result.returncode == 0)
    config_path = home / ".halo" / "config.json"
    providers_block = None
    if config_path.exists():
        providers_block = json.loads(config_path.read_text(encoding="utf-8")).get("providers")
    ctx.check(f"no 'providers' block written just from a fresh successful setup, got {providers_block!r}",
              not providers_block)


@test
def test_provider_setup_flips_an_existing_explicit_disable_back_on(ctx: Ctx):
    home = _fresh_home()
    config_dir = home / ".halo"
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "config.json").write_text(
        json.dumps({"providers": {"openrouter": {"enabled": False}}}), encoding="utf-8")
    result = _run(["init", "--provider", "openrouter", "--yes", "--no-live"], home,
                   extra_env={"OPENROUTER_API_KEY": "sk-or-fake"})
    ctx.check(f"exit 0, got {result.returncode}", result.returncode == 0)
    config = json.loads((config_dir / "config.json").read_text(encoding="utf-8"))
    ctx.check(f"the explicit disable was flipped back to enabled, got {config.get('providers')}",
              config["providers"]["openrouter"]["enabled"] is True)


if __name__ == "__main__":
    ctx = Ctx()
    results, passed, failed, skipped = run_all(TESTS, ctx)
    sys.exit(print_results(results, passed, failed, skipped))
