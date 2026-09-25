"""tests.test_work_box -- H8 scope F: ucode-settings.json discovery,
catalog-age reporting, and `rolo-claude doctor --work`'s output shape with
no/an unreachable Databricks host (never a real VPN call in this suite)."""
import json
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.helpers.runner import Ctx, new_registry, print_results, run_all

test, TESTS = new_registry()


@test
def test_load_ucode_settings_top_level_keys(ctx: Ctx):
    from rolo_claude.providers.config import load_ucode_settings
    d = Path(tempfile.mkdtemp(prefix="ucode-"))
    p = d / "ucode-settings.json"
    p.write_text(json.dumps({"gateway_url": "https://x.cloud.databricks.com", "token": "abc123"}), encoding="utf-8")
    cfg = load_ucode_settings(p)
    ctx.check("parsed a config", cfg is not None)
    ctx.check("host correct", cfg.host == "https://x.cloud.databricks.com")
    ctx.check("token correct", cfg.token == "abc123")


@test
def test_load_ucode_settings_nested_gateway_key(ctx: Ctx):
    from rolo_claude.providers.config import load_ucode_settings
    d = Path(tempfile.mkdtemp(prefix="ucode-nested-"))
    p = d / "ucode-settings.json"
    p.write_text(json.dumps({"gateway": {"host": "y.cloud.databricks.com", "api_token": "tok"}}), encoding="utf-8")
    cfg = load_ucode_settings(p)
    ctx.check("parsed a nested config", cfg is not None)
    ctx.check("host from nested key", cfg.host == "y.cloud.databricks.com")
    ctx.check("token from nested key", cfg.token == "tok")


@test
def test_load_ucode_settings_missing_or_incomplete_is_none(ctx: Ctx):
    from rolo_claude.providers.config import load_ucode_settings
    d = Path(tempfile.mkdtemp(prefix="ucode-missing-"))
    ctx.check("missing file -> None", load_ucode_settings(d / "nope.json") is None)
    incomplete = d / "incomplete.json"
    incomplete.write_text(json.dumps({"host": "only-a-host.databricks.com"}), encoding="utf-8")
    ctx.check("host with no token -> None", load_ucode_settings(incomplete) is None)
    garbage = d / "garbage.json"
    garbage.write_text("{not json", encoding="utf-8")
    ctx.check("unparseable JSON -> None", load_ucode_settings(garbage) is None)


@test
def test_resolve_databricks_falls_back_to_ucode_settings(ctx: Ctx):
    """Step 5 of the discovery chain: when nothing earlier (explicit
    BRIDGE_DBX_*, ANTHROPIC_*, DATABRICKS_*) resolves, ucode-settings.json
    under the (test) claude config dir wins before ~/.databrickscfg."""
    from rolo_claude.providers.config import resolve_databricks
    d = Path(tempfile.mkdtemp(prefix="ucode-resolve-"))
    claude_dir = d / ".claude"
    claude_dir.mkdir(parents=True)
    (claude_dir / "ucode-settings.json").write_text(
        json.dumps({"gateway_url": "https://z.cloud.databricks.com", "token": "ztoken"}), encoding="utf-8")

    old_home = os.environ.get("BRIDGE_TEST_HOME")
    os.environ["BRIDGE_TEST_HOME"] = str(d)
    try:
        cfg = resolve_databricks(env={})  # an EMPTY env -- nothing wins before step 5
        ctx.check("resolved via ucode-settings.json", cfg is not None and cfg.host == "https://z.cloud.databricks.com")
        ctx.check("token from ucode-settings.json", cfg is not None and cfg.token == "ztoken")
    finally:
        if old_home is None:
            os.environ.pop("BRIDGE_TEST_HOME", None)
        else:
            os.environ["BRIDGE_TEST_HOME"] = old_home


@test
def test_resolve_databricks_explicit_override_beats_ucode_settings(ctx: Ctx):
    from rolo_claude.providers.config import resolve_databricks
    d = Path(tempfile.mkdtemp(prefix="ucode-precedence-"))
    claude_dir = d / ".claude"
    claude_dir.mkdir(parents=True)
    (claude_dir / "ucode-settings.json").write_text(
        json.dumps({"gateway_url": "https://loser.cloud.databricks.com", "token": "loser"}), encoding="utf-8")

    old_home = os.environ.get("BRIDGE_TEST_HOME")
    os.environ["BRIDGE_TEST_HOME"] = str(d)
    try:
        cfg = resolve_databricks(env={"BRIDGE_DBX_BASE_URL": "https://winner.cloud.databricks.com",
                                       "BRIDGE_DBX_TOKEN": "winner"})
        ctx.check("explicit override still wins", cfg.host == "https://winner.cloud.databricks.com")
    finally:
        if old_home is None:
            os.environ.pop("BRIDGE_TEST_HOME", None)
        else:
            os.environ["BRIDGE_TEST_HOME"] = old_home


@test
def test_catalog_ages_reports_missing_and_fresh(ctx: Ctx):
    from rolo_claude.doctor import _check_catalog_ages
    d = Path(tempfile.mkdtemp(prefix="catalog-ages-"))
    old_state = os.environ.get("BRIDGE_STATE_DIR")
    os.environ["BRIDGE_STATE_DIR"] = str(d)
    try:
        lines = _check_catalog_ages()
        ctx.check(f"one line per catalog file, got {len(lines)}", len(lines) == 3)
        ctx.check("all missing/never-cached -- WARN, not MISSING (vendored fallback still applies)",
                  all("[WARN]" in l for l in lines))
        (d / "models.json").write_text("{}", encoding="utf-8")
        lines2 = _check_catalog_ages()
        ok_lines = [l for l in lines2 if "models.json" in l]
        ctx.check("a freshly-written models.json is now OK", ok_lines and "[OK]" in ok_lines[0])
    finally:
        if old_state is None:
            os.environ.pop("BRIDGE_STATE_DIR", None)
        else:
            os.environ["BRIDGE_STATE_DIR"] = old_state


@test
def test_doctor_work_no_databricks_configured(ctx: Ctx):
    from rolo_claude.doctor import run_work_checks
    d = Path(tempfile.mkdtemp(prefix="work-none-"))
    old_home = os.environ.get("BRIDGE_TEST_HOME")
    old_env = os.environ.get("BRIDGE_ENV_FILE")
    os.environ["BRIDGE_TEST_HOME"] = str(d)
    os.environ["BRIDGE_ENV_FILE"] = str(d / "no-such-env-file")
    for key in ("BRIDGE_DBX_BASE_URL", "BRIDGE_DBX_TOKEN", "DATABRICKS_HOST", "DATABRICKS_TOKEN",
                "ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN"):
        os.environ.pop(key, None)
    try:
        lines, ok = run_work_checks()
        ctx.check("not ok (nothing configured)", ok is False)
        ctx.check("Databricks config line present", any("Databricks config" in l for l in lines))
        ctx.check("no live network call attempted (clean MISSING, no traceback)",
                  all("Traceback" not in l for l in lines))
    finally:
        for var, old in (("BRIDGE_TEST_HOME", old_home), ("BRIDGE_ENV_FILE", old_env)):
            if old is None:
                os.environ.pop(var, None)
            else:
                os.environ[var] = old


@test
def test_doctor_work_unreachable_host_reports_vpn_hint(ctx: Ctx):
    """A syntactically valid but non-routable Databricks-shaped host: the
    VPN hint text must appear, and nothing downstream (token check, open
    questions) should raise just because the connect failed."""
    from rolo_claude.doctor import run_work_checks
    d = Path(tempfile.mkdtemp(prefix="work-unreachable-"))
    os.environ["BRIDGE_TEST_HOME"] = str(d)
    os.environ["BRIDGE_DBX_BASE_URL"] = "https://this-host-does-not-exist-rolo-claude-test.databricks.com"
    os.environ["BRIDGE_DBX_TOKEN"] = "fake-token-for-a-test"
    try:
        lines, ok = run_work_checks()
        ctx.check("not ok (unreachable)", ok is False)
        joined = "\n".join(lines)
        ctx.check("VPN hint present", "VPN" in joined)
        ctx.check("no traceback leaked into the output", "Traceback" not in joined)
    finally:
        os.environ.pop("BRIDGE_TEST_HOME", None)
        os.environ.pop("BRIDGE_DBX_BASE_URL", None)
        os.environ.pop("BRIDGE_DBX_TOKEN", None)


if __name__ == "__main__":
    ctx = Ctx()
    results, passed, failed, skipped = run_all(TESTS, ctx)
    sys.exit(print_results(results, passed, failed, skipped))
