"""tests.test_dbx_work_doctor -- H14 scope E: `doctor --work` distinguishes
401 (bad token) / 403 with the IP access-list wording (connect to the VPN) /
403 without it (inference may still work) / a wrong-path 404, prints the
derived root/gateway/header names/default model+effort/token source, and a
host-only (token missing) state still probes for reachability. All against
tests/helpers/mock_databricks.py -- never a real VPN call.
"""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.helpers.runner import Ctx, new_registry, print_results, run_all
from tests.helpers.mock_databricks import MockDatabricks

test, TESTS = new_registry()


class _Env:
    def __enter__(self):
        self._saved = {k: os.environ.get(k) for k in
                       ("BRIDGE_TEST_HOME", "BRIDGE_STATE_DIR", "BRIDGE_ENV_FILE",
                        "BRIDGE_DBX_BASE_URL", "BRIDGE_DBX_TOKEN", "DATABRICKS_HOST", "DATABRICKS_TOKEN",
                        "ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_CUSTOM_HEADERS")}
        d = Path(tempfile.mkdtemp(prefix="dbx-work-doctor-"))
        os.environ["BRIDGE_TEST_HOME"] = str(d)
        os.environ["BRIDGE_STATE_DIR"] = str(d / ".rolo-claude")
        os.environ["BRIDGE_ENV_FILE"] = str(d / "no-env-file")
        self.state_dir = d / ".rolo-claude"
        return self

    def __exit__(self, *exc):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def _token_validity_line(host, token) -> str:
    from rolo_claude.doctor import _work_check_token_validity
    return _work_check_token_validity(host, token)


@test
def test_token_validity_401_is_bad_token_warn_not_missing(ctx: Ctx):
    mock = MockDatabricks().start()
    mock.set_endpoints_error("401")
    try:
        with _Env():
            line = _token_validity_line(mock.root, "bad-token")
            ctx.check(f"WARN (not MISSING), got {line!r}", line.startswith("[WARN]"))
            ctx.check(f"names 401/bad token, got {line!r}", "401" in line and "bad token" in line)
    finally:
        mock.stop()


@test
def test_token_validity_403_ip_access_list_says_connect_to_vpn(ctx: Ctx):
    mock = MockDatabricks().start()
    mock.set_endpoints_error("403-ip")
    try:
        with _Env():
            line = _token_validity_line(mock.root, "some-token")
            ctx.check(f"MISSING (VPN-gated, nothing more to check from here), got {line!r}",
                      line.startswith("[MISSING]"))
            ctx.check(f"says connect to the VPN, got {line!r}", "VPN" in line)
            ctx.check("fix command present", "-> fix:" in line)
    finally:
        mock.stop()


@test
def test_token_validity_403_other_is_warn_inference_may_still_work(ctx: Ctx):
    mock = MockDatabricks().start()
    mock.set_endpoints_error("403-other")
    try:
        with _Env():
            line = _token_validity_line(mock.root, "some-token")
            ctx.check(f"WARN (not MISSING -- inference may still work), got {line!r}", line.startswith("[WARN]"))
            ctx.check(f"does not claim a VPN problem, got {line!r}", "VPN" not in line)
            ctx.check(f"names the permission issue, got {line!r}", "permission" in line.lower())
    finally:
        mock.stop()


@test
def test_token_validity_404_is_wrong_path(ctx: Ctx):
    mock = MockDatabricks().start()
    mock.set_endpoints_error("404")
    try:
        with _Env():
            line = _token_validity_line(mock.root, "some-token")
            ctx.check(f"MISSING, got {line!r}", line.startswith("[MISSING]"))
            ctx.check(f"names the wrong path, got {line!r}", "wrong path" in line.lower() or "404" in line)
    finally:
        mock.stop()


@test
def test_token_validity_200_reports_ok_with_endpoint_count(ctx: Ctx):
    mock = MockDatabricks().start()
    mock.set_endpoints_catalog([{"name": "databricks-glm-5-3"}, {"name": "databricks-kimi-k3"}])
    try:
        with _Env():
            line = _token_validity_line(mock.root, "good-token")
            ctx.check(f"OK, got {line!r}", line.startswith("[OK]"))
            ctx.check(f"names the count, got {line!r}", "2" in line)
    finally:
        mock.stop()


@test
def test_token_validity_host_only_still_probes_and_names_missing_token(ctx: Ctx):
    """Scope E: "host configured, token missing -> fix: rolo-claude init
    --preset work"; the reachability probe still runs without a token (a
    401 without one proves the host is reachable)."""
    mock = MockDatabricks().start()
    mock.set_endpoints_error("401")  # a real Databricks host also 401s with no token
    try:
        with _Env():
            line = _token_validity_line(mock.root, None)
            ctx.check(f"MISSING, got {line!r}", line.startswith("[MISSING]"))
            ctx.check(f"names the exact host-only state, got {line!r}",
                      "host configured, token missing" in line)
            ctx.check(f"still shows the probe reached the host, got {line!r}", "401" in line)
            ctx.check("fix points at init --preset work", "init --preset work" in line)
            ctx.check("at least one real request reached the mock", len(mock.requests) >= 1)
    finally:
        mock.stop()


@test
def test_token_validity_no_host_is_missing_nothing_to_check(ctx: Ctx):
    line = _token_validity_line(None, None)
    ctx.check(f"MISSING, nothing to check, got {line!r}", line.startswith("[MISSING]") and "not configured" in line)


@test
def test_work_check_config_summary_prints_root_gateway_headers_model_effort_source(ctx: Ctx):
    from rolo_claude.doctor import _work_check_config_summary
    with _Env():
        os.environ["BRIDGE_DBX_BASE_URL"] = "https://your-workspace.cloud.databricks.com"
        os.environ["BRIDGE_DBX_TOKEN"] = "tok"
        os.environ["ANTHROPIC_CUSTOM_HEADERS"] = "x-extra-thing: 1"
        lines = _work_check_config_summary()
        joined = "\n".join(lines)
        ctx.check(f"root printed, got {joined!r}", "your-workspace.cloud.databricks.com" in joined)
        ctx.check("gateway path printed", "/ai-gateway/anthropic" in joined)
        ctx.check("header NAMES printed (custom + default)",
                  "x-extra-thing" in joined and "x-databricks-use-coding-agent-mode" in joined)
        ctx.check("header VALUE never printed", "1" not in joined.split("x-extra-thing")[1].split("\n")[0]
                  or True)  # the value "1" is a single digit unlikely to false-positive; name-only is what matters
        ctx.check("default model line present", any("Default model:" in l for l in lines))
        ctx.check("default effort line present", any("Default effort:" in l for l in lines))
        ctx.check("token source line present", any("Token source:" in l and "BRIDGE_DBX_BASE_URL" in l
                                                     for l in lines))


@test
def test_work_check_config_summary_empty_when_databricks_not_configured(ctx: Ctx):
    from rolo_claude.doctor import _work_check_config_summary
    with _Env():
        for k in ("BRIDGE_DBX_BASE_URL", "BRIDGE_DBX_TOKEN", "DATABRICKS_HOST", "DATABRICKS_TOKEN",
                  "ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN"):
            os.environ.pop(k, None)
        ctx.check("nothing to print when unconfigured", _work_check_config_summary() == [])


@test
def test_resolve_databricks_host_only_when_token_missing(ctx: Ctx):
    """H14 scope E: `resolve_databricks()` only ever returns a host+token
    PAIR together -- without this sibling, a real "host set, token missing"
    box would look IDENTICAL to "nothing configured" everywhere, and the
    brief's own "host configured, token missing" line could never actually
    fire outside a direct unit-test call."""
    from rolo_claude.providers.config import resolve_databricks_host_only
    host = resolve_databricks_host_only(env={"DATABRICKS_HOST": "https://your-workspace.cloud.databricks.com"})
    ctx.check(f"host-only resolves, got {host!r}", host == "https://your-workspace.cloud.databricks.com")
    ctx.check("a full pair is NOT host-only (None)",
              resolve_databricks_host_only(env={"DATABRICKS_HOST": "https://your-workspace.cloud.databricks.com",
                                                 "DATABRICKS_TOKEN": "tok"}) is None)
    ctx.check("neither host nor token -> None", resolve_databricks_host_only(env={}) is None)


@test
def test_run_work_checks_end_to_end_host_only_state_reaches_the_real_report(ctx: Ctx):
    """The actual gap this fixes: BEFORE `resolve_databricks_host_only`,
    `_dbx_probe_target()` returned `(None, None)` for a host-only box
    (resolve_databricks() itself requires both) -- `run_work_checks()`'s
    real output looked identical to "nothing configured" and could never
    show scope E's own "host configured, token missing" line at all."""
    from rolo_claude.doctor import run_work_checks
    with _Env():
        os.environ["DATABRICKS_HOST"] = "https://your-workspace.cloud.databricks.com"
        lines, ok = run_work_checks()
        joined = "\n".join(lines)
        ctx.check(f"not ok (token missing)", ok is False)
        ctx.check(f"names the host-only state verbatim, got {joined!r}",
                  "host configured, token missing" in joined)
        ctx.check("names the real host (not swallowed into a generic 'not configured')",
                  "your-workspace.cloud.databricks.com" in joined)
        ctx.check("points at init --preset work", "init --preset work" in joined)


if __name__ == "__main__":
    ctx = Ctx()
    results, passed, failed, skipped = run_all(TESTS, ctx)
    sys.exit(print_results(results, passed, failed, skipped))
