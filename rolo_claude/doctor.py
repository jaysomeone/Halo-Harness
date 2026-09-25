"""rolo_claude.doctor -- `rolo-claude doctor` subcommand + the `/doctor`
slash command's shared implementation (U0 scope A). Checks: Python version,
`~/.claude` layout, the env file, an OpenRouter key, Databricks discovery,
`claude.exe`/`claude` on PATH (needed for `--chrome`), node/npx on PATH
(needed for `--playwright`), and a WSL/Kali hint. Read-only: never writes
anything, never raises on a missing/misconfigured piece -- each check
degrades to a "not configured" line instead.
"""

from __future__ import annotations

import argparse
import os
import platform
import shutil
import sys
from pathlib import Path
from typing import Optional

OK, WARN, MISSING = "[OK]", "[WARN]", "[MISSING]"


def _check_python() -> str:
    info = sys.version_info
    version = f"{info.major}.{info.minor}.{info.micro}"
    status = OK if (info.major, info.minor) >= (3, 9) else WARN
    return f"{status} Python {version}"


def _check_claude_layout() -> list:
    from rolo_claude.config.paths import claude_config_dir, claude_json_path
    lines = []
    cfg_dir = claude_config_dir()
    lines.append(f"{OK if cfg_dir.is_dir() else MISSING} ~/.claude directory: {cfg_dir}")
    settings_path = cfg_dir / "settings.json"
    lines.append(f"{OK if settings_path.exists() else WARN} settings.json: {settings_path}")
    cj_path = claude_json_path()
    lines.append(f"{OK if cj_path.exists() else WARN} .claude.json: {cj_path}")
    return lines


def _check_env_file() -> str:
    from rolo_claude.config.paths import home
    env_path = Path(os.environ.get("BRIDGE_ENV_FILE", home() / ".config" / "vibes-hacker" / "env"))
    return f"{OK if env_path.exists() else WARN} env file: {env_path}"


def _load_env_file_best_effort() -> None:
    """Same env file `run_print_mode` loads (`BRIDGE_ENV_FILE` or
    `~/.config/vibes-hacker/env`) -- without this, doctor would report a
    key "not configured" even when the real harness would happily find it
    there (an env-var-only check would silently disagree with reality)."""
    try:
        import os
        from rolo_claude.config.paths import home
        from rolo_claude.providers.config import load_env_file
        load_env_file(Path(os.environ.get("BRIDGE_ENV_FILE", home() / ".config" / "vibes-hacker" / "env")))
    except Exception:
        pass


def _check_openrouter() -> str:
    _load_env_file_best_effort()
    try:
        from rolo_claude.providers.config import resolve_openrouter
        orc = resolve_openrouter()
    except Exception as e:  # never let a doctor check crash the whole command
        return f"{WARN} OpenRouter: could not check ({type(e).__name__}: {e})"
    if orc is None:
        return f"{WARN} OpenRouter: not configured (no OPENROUTER_API_KEY found)"
    return f"{OK} OpenRouter: key found ({orc.base_url})"


def _check_databricks() -> str:
    _load_env_file_best_effort()
    try:
        from rolo_claude.providers.config import resolve_databricks
        dbx = resolve_databricks()
    except Exception as e:
        return f"{WARN} Databricks: could not check ({type(e).__name__}: {e})"
    if dbx is None:
        return f"{WARN} Databricks: not configured (no host/token found)"
    return f"{OK} Databricks: configured ({dbx.host})"


_CHROME_NATIVE_HOST_ID = "com.anthropic.claude_code_browser_extension"
# POSIX native-messaging manifest search dirs (Chrome/Chromium/Edge/Brave,
# per-user) -- [verified doc]: Windows registers a registry key instead
# (checked separately below), so this list is consulted only off win32.
_POSIX_NATIVE_HOST_DIRS = (
    "~/.config/google-chrome/NativeMessagingHosts",
    "~/.config/chromium/NativeMessagingHosts",
    "~/.config/microsoft-edge/NativeMessagingHosts",
    "~/.config/BraveSoftware/Brave-Browser/NativeMessagingHosts",
    "~/.mozilla/native-messaging-hosts",  # Firefox uses a different manifest shape but shares the dir convention
)


def _chrome_native_host_registered() -> "tuple[bool, str]":
    """`(registered, detail)` -- registry key on win32
    [claude-in-chrome-integration.md: `HKCU\\...\\NativeMessagingHosts\\
    com.anthropic.claude_code_browser_extension`], manifest file presence
    under the usual per-browser dirs on POSIX. Best-effort: any lookup
    failure (no `winreg`, permission error, ...) reports "not found"
    rather than raising."""
    if sys.platform == "win32":
        try:
            import winreg
            key_path = f"Software\\Google\\Chrome\\NativeMessagingHosts\\{_CHROME_NATIVE_HOST_ID}"
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path) as key:
                manifest_path, _ = winreg.QueryValueEx(key, None)
            return True, f"HKCU\\{key_path} -> {manifest_path}"
        except OSError:
            return False, f"no HKCU\\Software\\Google\\Chrome\\NativeMessagingHosts\\{_CHROME_NATIVE_HOST_ID} key"
    for d in _POSIX_NATIVE_HOST_DIRS:
        manifest = Path(d).expanduser() / f"{_CHROME_NATIVE_HOST_ID}.json"
        if manifest.exists():
            return True, str(manifest)
    return False, f"no {_CHROME_NATIVE_HOST_ID}.json manifest found under the usual browser config dirs"


def _chrome_bridge_pipe_present() -> bool:
    """Best-effort: the native-host pipe/socket exists only while the
    extension currently holds it open, so a False here is completely
    normal (Chrome just isn't running with the extension enabled right
    now) -- never treated as a failure, only extra detail."""
    try:
        user = os.environ.get("USERNAME") or os.environ.get("USER") or "?"
        if sys.platform == "win32":
            return Path(rf"\\.\pipe\claude-mcp-browser-bridge-{user}").exists()
        return Path(f"/tmp/claude-mcp-browser-bridge-{user}").exists()
    except OSError:
        return False


def _check_chrome() -> str:
    from rolo_claude.mcp_setup import find_claude_exe
    claude_exe = find_claude_exe()
    if not claude_exe:
        return f"{WARN} claude executable not found on PATH -- --chrome cannot spawn the claude-in-chrome MCP server"
    registered, detail = _chrome_native_host_registered()
    pipe = " (bridge pipe currently open)" if _chrome_bridge_pipe_present() else ""
    if registered:
        return f"{OK} claude={claude_exe}; native host registered ({detail}){pipe}"
    return f"{WARN} claude={claude_exe}; Chrome extension native host NOT registered ({detail}) -- install/enable Claude in Chrome first"


def _check_plugins() -> str:
    """finding 11: report what plugin-provided MCP servers were actually
    discovered under ~/.claude/plugins/ -- the fastest way to confirm a
    `claude plugin install ...`'d plugin's servers are reachable at all,
    without needing a full `-p`/TUI launch."""
    try:
        from rolo_claude.config.plugins import discover_plugin_mcp_servers
        servers, notices = discover_plugin_mcp_servers(env=dict(os.environ))
    except Exception as e:  # never let a doctor check crash the whole command
        return f"{WARN} Plugins: could not check ({type(e).__name__}: {e})"
    if not servers:
        return f"{OK} Plugins: no plugin-provided MCP servers discovered"
    names = ", ".join(sorted(servers))
    suffix = f" -- {len(notices)} notice(s)" if notices else ""
    return f"{OK} Plugins: {len(servers)} MCP server(s) discovered ({names}){suffix}"


def _check_playwright() -> str:
    node = shutil.which("node")
    npx = shutil.which("npx") or shutil.which("npx.cmd")
    if node and npx:
        return f"{OK} node/npx on PATH (needed for --playwright): {node}"
    missing = ", ".join(n for n, p in (("node", node), ("npx", npx)) if not p)
    return f"{WARN} missing on PATH for --playwright: {missing}"


def _check_platform() -> str:
    system = platform.system()
    if system == "Linux":
        try:
            release = Path("/proc/version").read_text(encoding="utf-8", errors="replace").lower()
        except OSError:
            release = ""
        if "microsoft" in release:
            return f"{OK} Linux (WSL) -- {platform.release()}"
        try:
            os_release = Path("/etc/os-release").read_text(encoding="utf-8", errors="replace").lower()
        except OSError:
            os_release = ""
        if "kali" in os_release:
            return f"{OK} Linux (Kali) -- rolo-claude's primary target platform"
        return f"{OK} Linux -- {platform.release()}"
    if system == "Windows":
        wsl = shutil.which("wsl")
        hint = " (wsl.exe found -- verify parity there too)" if wsl else " (no wsl.exe found on PATH)"
        return f"{OK} Windows -- build/test host, not the primary target{hint}"
    return f"{OK} {system} -- {platform.release()}"


def _check_catalog_ages() -> list:
    """H8 scope C: show the age of every cached catalog file (models.json,
    dbx-endpoints.json, models-dev.json) -- a missing file is a plain WARN
    ("never refreshed yet", not a failure: `resolve_model_profile` still
    has the vendored package fallback); a very stale one (>30 days) is
    flagged so a work-box user knows `rolo-claude models --refresh` is
    overdue, without ever being INTERNET-only (the vendored tier means
    a stale/missing cache is never actually broken, just less current)."""
    import time
    from rolo_claude.config.paths import bridge_home
    from rolo_claude.providers.databricks import dbx_endpoints_path, models_json_path
    from rolo_claude.providers.models_dev import models_dev_json_path
    state_dir = bridge_home()
    lines = []
    for label, path_fn in (("models.json (OpenRouter)", models_json_path),
                            ("dbx-endpoints.json (Databricks)", dbx_endpoints_path),
                            ("models-dev.json (models.dev)", models_dev_json_path)):
        path = path_fn(state_dir)
        if not path.exists():
            lines.append(f"{WARN} {label}: never cached (vendored package fallback still applies) -- "
                         f"run `rolo-claude models --refresh`")
            continue
        age_days = (time.time() - path.stat().st_mtime) / 86400
        status = WARN if age_days > 30 else OK
        lines.append(f"{status} {label}: cached {age_days:.1f} day(s) ago ({path})")
    return lines


def _dbx_probe_target():
    """`(host, token)` from the real discovery chain, or `(None, None)` --
    shared by every `--work` check below so they never disagree about
    which config they're evaluating (the same chain `_check_databricks`
    uses for the plain `doctor` line, just returning the raw values
    instead of a formatted string)."""
    _load_env_file_best_effort()
    try:
        from rolo_claude.providers.config import resolve_databricks
        dbx = resolve_databricks()
    except Exception:
        return None, None
    if dbx is None:
        return None, None
    return dbx.host, dbx.token


def _check_ucode_settings() -> str:
    """H8 scope F must-do (`ug`-compatibility note): report whether
    `~/.claude/ucode-settings.json` exists/parses, independent of whether it
    actually WON the discovery chain (an earlier source, e.g. DATABRICKS_
    HOST, may have already resolved first) -- purely informational."""
    from rolo_claude.config.paths import claude_config_dir
    from rolo_claude.providers.config import load_ucode_settings
    path = claude_config_dir() / "ucode-settings.json"
    if not path.exists():
        return f"{WARN} ucode-settings.json: not found at {path} (ug/unity-gateway CLI not run on this box, or not installed)"
    parsed = load_ucode_settings(path)
    if parsed is None:
        return f"{WARN} ucode-settings.json: found at {path} but no recognizable gateway URL/token in it"
    return f"{OK} ucode-settings.json: found and parsed ({path}) -> host {parsed.host}"


def _work_check_vpn_reachability(host: Optional[str]) -> str:
    """A real TCP+TLS connect attempt (no data sent) to the resolved
    Databricks host -- the same connect path `providers.http.open_upstream`
    uses, so a WARN here means a live request would fail the identical way."""
    if not host:
        return f"{MISSING} Databricks host: not configured (see the Databricks line above) -- nothing to reach"
    import socket
    import ssl
    import urllib.parse
    parsed = urllib.parse.urlparse(host if "://" in host else f"https://{host}")
    hostname = parsed.hostname or host
    port = parsed.port or 443
    try:
        with socket.create_connection((hostname, port), timeout=6) as sock:
            with ssl.create_default_context().wrap_socket(sock, server_hostname=hostname):
                pass
        return f"{OK} VPN/reachability: connected to {hostname}:{port}"
    except Exception as e:
        return (f"{MISSING} VPN/reachability: could not reach {hostname}:{port} ({type(e).__name__}: {e}) "
                f"-- are you on the VPN? Databricks is whitelisted")


def _work_check_token_validity(host: Optional[str], token: Optional[str]) -> str:
    """GET /api/2.0/serving-endpoints -- 200 means the token can list
    endpoints; 401/403 means it can likely still run inference but not
    list them (same wording the proxy's own --probe has always used);
    anything else (incl. a connect failure) is reported honestly."""
    if not host or not token:
        return f"{MISSING} Token validity: not configured -- nothing to check"
    try:
        from rolo_claude.providers.databricks import probe_databricks_endpoints
        from rolo_claude.providers.config import derive_workspace_root
        status, names = probe_databricks_endpoints(derive_workspace_root(host), token)
    except Exception as e:
        return f"{MISSING} Token validity: connection failed ({type(e).__name__}: {e}) -- are you on the VPN?"
    if status == 200:
        return f"{OK} Token validity: valid, can list endpoints ({len(names)} found)"
    if status in (401, 403):
        return f"{WARN} Token validity: got HTTP {status} listing endpoints -- token may only be able to run inference, not list it"
    return f"{WARN} Token validity: got HTTP {status} from /api/2.0/serving-endpoints"


_THINKING_FAMILY_HINTS = ("deepseek", "kimi", "moonshot", "glm", "zhipu", "z-ai")


def _work_check_reasoning_replay_after_tool_call(host: Optional[str], token: Optional[str], state_dir) -> list:
    """Plan open question 1: "whether Databricks forwards replayed
    `reasoning_content` into DeepSeek/Kimi/GLM after a tool call" -- and
    plan open question 2: "whether the invocations-without-model vs
    mlflow-with-system.ai split holds for every catalogue model", answered
    TOGETHER by ONE real, cheap (max_tokens=64) request against the first
    DeepSeek/Kimi/GLM-family endpoint the live catalog has: a synthetic
    prior assistant turn carries `reasoning_content` AND a tool call, a
    synthetic tool result follows it, and the model is asked one more
    question -- the HTTP status this gets back (200 vs a 400 naming
    reasoning_content) directly answers question 1, and whichever
    candidate route actually served it (recorded in routes-cache.json by
    the SAME call, via the ordinary `call_databricks_chat` path) directly
    answers question 2. Never raises -- any failure becomes a WARN/MISSING
    line naming what happened, since this is exploratory (the plan's own
    "unknowns"), not a pass/fail gate on the rest of `doctor --work`."""
    lines = ["Open questions: (1) does Databricks forward replayed reasoning_content into "
             "DeepSeek/Kimi/GLM after a tool call? (2) does the invocations-vs-mlflow route "
             "split hold for this model?"]
    if not host or not token:
        lines.append(f"  {MISSING} cannot probe -- Databricks not configured")
        return lines
    try:
        from rolo_claude.providers.databricks import probe_databricks_endpoints_full
        from rolo_claude.providers.config import derive_workspace_root
        status, entries = probe_databricks_endpoints_full(derive_workspace_root(host), token)
    except Exception as e:
        lines.append(f"  {MISSING} cannot probe -- connection failed ({type(e).__name__}: {e}); are you on the VPN?")
        return lines
    if status != 200:
        lines.append(f"  {MISSING} cannot probe -- HTTP {status} listing endpoints")
        return lines
    candidate = next((e.get("name") for e in entries
                       if isinstance(e, dict) and any(h in (e.get("name") or "").lower()
                                                        for h in _THINKING_FAMILY_HINTS)), None)
    if not candidate:
        lines.append(f"  {WARN} no DeepSeek/Kimi/GLM-family endpoint in the catalog to probe")
        return lines

    try:
        from rolo_claude.providers.config import derive_workspace_root
        from rolo_claude.providers.databricks import dbx_cache_get_route, databricks_route_candidates
        from rolo_claude.providers.http import call_databricks_chat
        root = derive_workspace_root(host)
        body = {
            "model": candidate,
            "messages": [
                {"role": "user", "content": "What is 2+2? Use the calculator tool to check your answer."},
                {"role": "assistant", "content": None,
                 "reasoning_content": "The user wants 2+2; I will call the calculator tool to verify.",
                 "tool_calls": [{"id": "call_probe_1", "type": "function",
                                  "function": {"name": "calculator", "arguments": "{\"expr\": \"2+2\"}"}}]},
                {"role": "tool", "tool_call_id": "call_probe_1", "content": "4"},
            ],
            "tools": [{"type": "function", "function": {
                "name": "calculator", "description": "Evaluate a simple arithmetic expression",
                "parameters": {"type": "object", "properties": {"expr": {"type": "string"}}, "required": ["expr"]},
            }}],
            "max_tokens": 64, "stream": False,
        }
        result = call_databricks_chat(root, token, body, {}, state_dir, candidate)
        raw = b""
        try:
            if result.resp is not None:
                raw = result.resp.read()
        except Exception:
            pass
        snippet = raw.decode("utf-8", "replace")[:200].replace("\n", " ")
        lines.append(f"  {OK if result.status == 200 else WARN} question 1 (reasoning_content replay) against "
                     f"{candidate!r}: HTTP {result.status} -- {snippet or '(empty body)'}")

        predicted_first = databricks_route_candidates(candidate)[0][0]
        used_index = dbx_cache_get_route(candidate, state_dir)
        used_path = databricks_route_candidates(candidate)[used_index][0] if isinstance(used_index, int) else None
        if used_path is None:
            lines.append(f"  {WARN} question 2 (route split) for {candidate!r}: no route recorded from this call")
        else:
            match = "MATCHES prediction" if used_path == predicted_first else "DIFFERS from prediction"
            lines.append(f"  {OK} question 2 (route split) for {candidate!r}: predicted-first={predicted_first!r}, "
                         f"actually served by={used_path!r} ({match})")
    except Exception as e:
        lines.append(f"  {WARN} open-questions probe errored: {type(e).__name__}: {e}")
    return lines


def run_work_checks() -> "tuple[list, bool]":
    """H8 scope F: `rolo-claude doctor --work` -- VPN reachability, token
    validity, ucode-settings.json, and the plan's own two open questions as
    runnable probes. Never raises; every check degrades to a clear WARN/
    MISSING line with the VPN hint instead of crashing when the work box
    genuinely isn't reachable from here (expected when run outside the VPN,
    e.g. this exact build/test box)."""
    from rolo_claude.config.paths import bridge_home
    host, token = _dbx_probe_target()
    lines = [
        f"{OK if host else MISSING} Databricks config: " + (f"host {host}" if host else "not configured"),
        _check_ucode_settings(),
        _work_check_vpn_reachability(host),
        _work_check_token_validity(host, token),
    ]
    lines.extend(_work_check_reasoning_replay_after_tool_call(host, token, bridge_home()))
    ok = not any(line.strip().startswith(MISSING) for line in lines)
    return lines, ok


def run_checks(cwd: Optional[Path] = None) -> "tuple[list, bool]":
    """Returns (lines, ok) -- `ok` is True iff nothing came back MISSING
    (a WARN is informational, e.g. "no Databricks configured", and never
    fails doctor as a whole)."""
    lines = [_check_python()]
    lines.extend(_check_claude_layout())
    lines.append(_check_env_file())
    lines.append(_check_openrouter())
    lines.append(_check_databricks())
    lines.append(_check_chrome())
    lines.append(_check_playwright())
    lines.append(_check_plugins())
    lines.append(_check_platform())
    lines.extend(_check_catalog_ages())
    # U5 leftover / H8 cheap must-do: tui/clipboard.py's own
    # clipboard_doctor_line() was written ready-to-call but never actually
    # wired into a real doctor run.
    from rolo_claude.tui.clipboard import clipboard_doctor_line
    lines.append(clipboard_doctor_line())
    ok = not any(line.startswith(MISSING) for line in lines)
    return lines, ok


def cmd_doctor(argv: list) -> int:
    parser = argparse.ArgumentParser(prog="rolo-claude doctor", add_help=True,
                                      description="Check the health of your rolo-claude installation.")
    parser.add_argument("--work", action="store_true",
                         help="Run the Databricks work-box preset (VPN reachability, token validity, "
                              "route-split/reasoning-replay probes) instead of the general checks")
    args = parser.parse_args(argv)
    if args.work:
        lines, ok = run_work_checks()
        print("rolo-claude doctor --work")
        for line in lines:
            print(f"  {line}")
        return 0 if ok else 1
    lines, ok = run_checks()
    print("rolo-claude doctor")
    for line in lines:
        print(f"  {line}")
    return 0 if ok else 1
