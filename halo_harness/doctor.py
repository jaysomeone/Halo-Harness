"""halo_harness.doctor -- `halo doctor` subcommand + the `/doctor`
slash command's shared implementation (U0 scope A). Checks: Python version,
`~/.claude` layout, the env file, an OpenRouter key, Databricks discovery,
`claude.exe`/`claude` on PATH (needed for `--chrome`), node/npx on PATH
(needed for `--playwright`), and a WSL/Kali hint. Read-only: never writes
anything, never raises on a missing/misconfigured piece -- each check
degrades to a "not configured" line instead.

H12 Part B (RECOMMENDATIONS.md P0 #2, "prescriptive doctor"): every `[WARN]`/
`[MISSING]` line ends with `-> fix: <exact command>` (via `_fix` below), or
`-> see: <URL/section>` when there's no single command -- an `[OK]`/info line
never gets one (nothing to fix). `run_checks_structured`/`doctor --json`
exposes the SAME lines as `{id, status, message, fix}` records for
`halo init` (and any other machine caller) to consume without
re-parsing rendered text.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Optional

OK, WARN, MISSING = "[OK]", "[WARN]", "[MISSING]"


def _fix(line: str, *, cmd: Optional[str] = None, see: Optional[str] = None) -> str:
    """Appends ` -> fix: <cmd>` or ` -> see: <ref>` to a WARN/MISSING line
    -- a no-op on an OK/info line (nothing to fix) or when neither is given.
    `cmd` wins when both are passed (a real command is always more useful
    than a reference)."""
    if not (line.startswith(WARN) or line.startswith(MISSING)):
        return line
    if cmd:
        return f"{line} -> fix: {cmd}"
    if see:
        return f"{line} -> see: {see}"
    return line


_FIX_SUFFIX_RE = re.compile(r"\s*-> (fix|see): (.+)$")


def _parse_check_line(line: str) -> dict:
    """One rendered `[OK]`/`[WARN]`/`[MISSING]` line -> `{status, message,
    fix, see}` (status lowercased; `fix`/`see` are `None` when the line
    carries neither, which is always true for a non-WARN/MISSING line).
    Leading whitespace is stripped first -- `--work`'s own reasoning-replay
    probe indents its sub-lines two spaces, unlike every other line in this
    module."""
    stripped = line.strip()
    if stripped.startswith(OK):
        status, rest = "ok", stripped[len(OK):].strip()
    elif stripped.startswith(WARN):
        status, rest = "warn", stripped[len(WARN):].strip()
    elif stripped.startswith(MISSING):
        status, rest = "missing", stripped[len(MISSING):].strip()
    else:
        status, rest = "info", stripped
    fix = see = None
    m = _FIX_SUFFIX_RE.search(rest)
    if m:
        rest = rest[:m.start()].rstrip()
        if m.group(1) == "fix":
            fix = m.group(2).strip()
        else:
            see = m.group(2).strip()
    return {"status": status, "message": rest, "fix": fix, "see": see}


def _check_python() -> str:
    # H9 whole-tree review finding 33: this hardcoded (3, 9), one full
    # minor version below pyproject.toml's real `requires-python = ">=
    # 3.10"` -- a Python 3.9 interpreter reported [OK] here despite not
    # actually meeting the package's own declared minimum.
    info = sys.version_info
    version = f"{info.major}.{info.minor}.{info.micro}"
    if (info.major, info.minor) >= (3, 10):
        return f"{OK} Python {version}"
    return _fix(f"{WARN} Python {version}", see="docs/harness/INSTALL.md (Prerequisites: Python 3.10+)")


def _check_claude_layout() -> list:
    from halo_harness.config.paths import claude_config_dir, claude_json_path
    lines = []
    cfg_dir = claude_config_dir()
    if cfg_dir.is_dir():
        lines.append(f"{OK} ~/.claude directory: {cfg_dir}")
    else:
        lines.append(_fix(f"{MISSING} ~/.claude directory: {cfg_dir}", cmd="claude"))
    settings_path = cfg_dir / "settings.json"
    if settings_path.exists():
        lines.append(f"{OK} settings.json: {settings_path}")
    else:
        lines.append(_fix(f"{WARN} settings.json: {settings_path}",
                           see="README.md (Config reuse from Claude Code) -- optional, defaults apply"))
    cj_path = claude_json_path()
    if cj_path.exists():
        lines.append(f"{OK} .claude.json: {cj_path}")
    else:
        lines.append(_fix(f"{WARN} .claude.json: {cj_path}",
                           see="README.md (Config reuse from Claude Code) -- optional, defaults apply"))
    return lines


def _check_env_file() -> str:
    from halo_harness.config.paths import env_file_path, legacy_env_file_path
    env_path = env_file_path()
    # 2.0.0 fixpass finding 4: the legacy file is still read (see
    # _load_env_file_best_effort below), so its mere existence must still
    # count as "configured" here too -- otherwise this ONE line would WARN
    # even though every credential in it is actually in effect.
    if env_path.exists() or legacy_env_file_path().exists():
        return f"{OK} env file: {env_path}"
    return _fix(f"{WARN} env file: {env_path}", cmd="halo init")


def _load_env_file_best_effort() -> None:
    """Same env files `run_print_mode` loads (2.0.0 fixpass finding 4: the
    new `~/.config/halo/env`/HALO_ENV_FILE path, THEN the legacy
    `~/.config/vibes-hacker/env` one) -- without this, doctor would report
    a key "not configured" even when the real harness would happily find
    it there (an env-var-only check would silently disagree with
    reality)."""
    try:
        from halo_harness.providers.config import load_provider_env_files
        load_provider_env_files()
    except Exception:
        pass


def _check_state_dir_migration() -> Optional[str]:
    """2.0.0 fixpass finding 1: "both-exist case is silent too" -- a
    standing WARN (never OK/MISSING; this is informational, same spirit as
    `_check_catalog_ages`) whenever the OLD `~/.rolo-claude` and the NEW
    `~/.halo` both exist as genuinely distinct, non-empty directories
    (never when the old path happens to resolve to the exact same real
    directory as the new one -- a plain migration never creates such a
    link, item B, but this still can't be fooled if one exists anyway).
    `None` (no line at all) in the overwhelmingly common case -- a clean
    migration, a fresh box, or BRIDGE_STATE_DIR overriding the question
    entirely -- shares the ONE detection `bridge_home()` itself warns
    about once per process, so doctor can never disagree with it."""
    from halo_harness.config.paths import state_dir_both_exist_nonempty
    both = state_dir_both_exist_nonempty()
    if both is None:
        return None
    old_dir, new_dir = both
    return _fix(f"{WARN} state dir: both {old_dir} and {new_dir} exist",
                see="docs/harness/INSTALL.md (Upgrading from rolo-claude 1.0.1)")


def _check_openrouter() -> str:
    _load_env_file_best_effort()
    try:
        from halo_harness.providers.config import resolve_openrouter
        orc = resolve_openrouter()
    except Exception as e:  # never let a doctor check crash the whole command
        return _fix(f"{WARN} OpenRouter: could not check ({type(e).__name__}: {e})", cmd="halo init")
    if orc is None:
        return _fix(f"{WARN} OpenRouter: not configured (no OPENROUTER_API_KEY found)",
                     cmd="halo init --preset home")
    return f"{OK} OpenRouter: key found ({orc.base_url})"


def _check_databricks() -> str:
    _load_env_file_best_effort()
    try:
        from halo_harness.providers.config import resolve_databricks
        dbx = resolve_databricks()
    except Exception as e:
        return _fix(f"{WARN} Databricks: could not check ({type(e).__name__}: {e})", cmd="halo init")
    if dbx is None:
        return _fix(f"{WARN} Databricks: not configured (no host/token found)",
                     cmd="halo init --preset work")
    return f"{OK} Databricks: configured ({dbx.host})"


def _check_claude_subscription() -> str:
    """H11 Part B: `cc:` model availability -- reads ONLY `claude auth
    status`'s own JSON (providers.cc_models.claude_auth_status), NEVER
    `~/.claude/.credentials.json` (binding constraint, brief). Optional
    (the harness's primary models are open-weight via Databricks/
    OpenRouter): neither "not installed" nor "installed but not logged
    in" ever fails doctor's overall `ok`. On the Kali VM `claude` lives
    at `~/.local/bin` -- covered by `mcp_setup.find_claude_exe`'s own
    PATH-then-~/.local/bin lookup, same as every other `claude` use here.

    H11b finding 23: the version comes from `claude --version` (real
    `claude auth status` JSON has no version key at all -- verified live:
    analyticsDisabled, apiProvider, authMethod, configDirectory, email,
    loggedIn, orgId, orgName, projectsDirectory, subscriptionType -- so
    the old `status.version` bit never actually printed anything). A
    timeout gets its own message instead of reading as "not installed".
    Finding 2: WARNs (never OK) unless `authMethod == "claude.ai"` -- an
    `api_key` authMethod (an ANTHROPIC_API_KEY visible to `claude auth
    status`'s own -- now stripped -- environment) means cc: would NOT use
    the subscription even though `loggedIn` is true.

    2.0.1 launch-hang fix: calls `refresh_cached_claude_auth_status()`
    (never bare `claude_auth_status()`) -- same live, synchronous spawn
    `halo doctor` always did here (this check's own timing is unchanged),
    but it also warms the SAME cache `_check_providers_enabled()` reads
    later in this same run, so the two checks can never disagree, and it
    picks up the gateway short-circuit (`is_claude_gateway_driven()`) for
    free -- the owner's own work-VM repro hung on this exact doctor line
    too, every time `halo doctor` ran, not just on TUI launch."""
    from halo_harness.providers.cc_models import (
        GATEWAY_AUTH_METHOD, SUBSCRIPTION_AUTH_METHODS, refresh_cached_claude_auth_status,
    )
    try:
        status = refresh_cached_claude_auth_status()
    except Exception as e:  # never let a doctor check crash the whole command
        return _fix(f"{WARN} Claude subscription: could not check ({type(e).__name__}: {e})",
                     cmd="halo doctor")
    if status is None:
        return _fix(f"{WARN} Claude subscription: claude not found (cc: models unavailable -- install Claude Code)",
                     see="https://claude.com/claude-code")
    if getattr(status, "timed_out", False):
        return _fix(f"{WARN} Claude subscription: `claude auth status` timed out (try again -- cc: models "
                     f"unavailable for now)", cmd="halo doctor")
    if status.auth_method == GATEWAY_AUTH_METHOD:
        return _fix(f"{WARN} Claude subscription: claude is configured for a gateway "
                     f"(ANTHROPIC_BASE_URL/ANTHROPIC_AUTH_TOKEN/apiKeyHelper set) -- cc: will not use this "
                     f"(that's the dbx:/ant: route); expected on a work box, nothing to fix", cmd="halo providers")
    if not status.logged_in:
        return _fix(f"{WARN} Claude subscription: claude found but not logged in (run `claude` once to log in "
                     f"for cc: models)", cmd="claude")
    version = _claude_version()
    version_bit = f" via claude {version}" if version else ""
    if status.auth_method not in SUBSCRIPTION_AUTH_METHODS:
        via = status.auth_method or "an unrecognized method"
        return _fix(f"{WARN} Claude subscription: logged in via {via}, not claude.ai{version_bit} -- cc: will "
                     f"not use this (that's the ant: route); log in with `claude` and no ANTHROPIC_API_KEY set "
                     f"for cc:", cmd="unset ANTHROPIC_API_KEY && claude")
    # 2.0.1 W3a: doctor pins the tested Claude Code version range
    # (providers/cc_tested.json) and WARNs (never fails `ok`) when the
    # installed claude is NEWER than it -- an older install is never
    # flagged here (a different, pre-existing kind of compatibility
    # concern, out of this round's scope).
    from halo_harness.providers.cc_models import load_cc_tested_range, version_outside_tested_range
    tested = load_cc_tested_range()
    if version_outside_tested_range(version, tested=tested):
        return _fix(f"{WARN} Claude subscription: logged in (claude.ai){version_bit} -- cc: models available, "
                     f"but this is newer than the tested range ({tested.get('min')}-{tested.get('max')}, "
                     f"verified {tested.get('date')}) -- watch for behavior changes", cmd="halo doctor")
    return f"{OK} Claude subscription: logged in (claude.ai){version_bit} -- cc: models available"


def _claude_version() -> Optional[str]:
    # 2.0.1 W3a: the real implementation moved to providers.cc_models
    # (`installed_claude_version`) so `agent/cc_runtime.py`'s own one-shot
    # "newer than the tested range" notice can call it too, without an
    # import cycle through this module -- this name/location stays for
    # every existing caller (and test double) of `doctor._claude_version`.
    from halo_harness.providers.cc_models import installed_claude_version
    return installed_claude_version()


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
    from halo_harness.mcp_setup import find_claude_exe
    claude_exe = find_claude_exe()
    if not claude_exe:
        return _fix(f"{WARN} claude executable not found on PATH -- --chrome cannot spawn the claude-in-chrome "
                     f"MCP server", see="https://claude.com/claude-code")
    registered, detail = _chrome_native_host_registered()
    pipe = " (bridge pipe currently open)" if _chrome_bridge_pipe_present() else ""
    if registered:
        return f"{OK} claude={claude_exe}; native host registered ({detail}){pipe}"
    return _fix(f"{WARN} claude={claude_exe}; Chrome extension native host NOT registered ({detail}) -- "
                 f"install/enable Claude in Chrome first", see="README.md (Browser)")


def _check_plugins() -> str:
    """finding 11: report what plugin-provided MCP servers were actually
    discovered under ~/.claude/plugins/ -- the fastest way to confirm a
    `claude plugin install ...`'d plugin's servers are reachable at all,
    without needing a full `-p`/TUI launch."""
    try:
        from halo_harness.config.plugins import discover_plugin_mcp_servers
        servers, notices = discover_plugin_mcp_servers(env=dict(os.environ))
    except Exception as e:  # never let a doctor check crash the whole command
        return _fix(f"{WARN} Plugins: could not check ({type(e).__name__}: {e})",
                     see="~/.claude/plugins/installed_plugins.json (check for a syntax error)")
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
    return _fix(f"{WARN} missing on PATH for --playwright: {missing}",
                 see="https://nodejs.org/ (install Node.js, then re-run doctor)")


def _check_ripgrep() -> str:
    """H9 OpenCode item 23: `rg` is optional (the Grep tool -- halo_harness/
    tools/grep_tool.py -- has a pure-Python fallback engine that's used
    transparently whenever `rg` isn't found), so its absence is a WARN,
    never a MISSING -- Grep still works either way, just slower on big
    trees without it."""
    rg = shutil.which("rg")
    if rg:
        return f"{OK} rg (ripgrep) on PATH: {rg}"
    return _fix(f"{WARN} rg (ripgrep) not on PATH -- the Grep tool falls back to a slower pure-Python "
                f"search engine; install ripgrep for full speed (Claude Code itself ships rg embedded)",
                cmd="halo init")


def _check_editor() -> str:
    """H9 OpenCode item 23: `$VISUAL`/`$EDITOR` back Ctrl+E (edit the
    current prompt draft in an external editor -- tui/app.py). Neither
    being set is a WARN, not MISSING: the TUI still works fully, only that
    one shortcut is unavailable."""
    editor = os.environ.get("VISUAL") or os.environ.get("EDITOR")
    if editor:
        return f"{OK} $VISUAL/$EDITOR set: {editor}"
    return _fix(f"{WARN} $VISUAL/$EDITOR not set -- Ctrl+E (edit the prompt draft in an external editor) "
                f"won't work", cmd="export EDITOR=nano")


def _check_terminal_program() -> str:
    """Halo 2.0.2 round C (the owner's own macOS report): "ctrl+e or
    command+e does not work on the mac using halo" traced to VS Code's
    integrated terminal intercepting the chord before halo ever sees it
    (Cmd+E can never work at all, on ANY terminal app -- the terminal
    app itself owns Cmd shortcuts; only Ctrl+E can ever reach halo).
    Always prints TERM_PROGRAM/TERM (useful on its own for any "does a
    shortcut even reach halo" question, not just this one -- `/keys` is
    the live version of the same question); when it's exactly "vscode",
    names BOTH real remedies verbatim (docs/TROUBLESHOOTING.md carries
    the same two) so there's something to paste straight into
    settings.json without hunting for the exact wording."""
    term_program_raw = os.environ.get("TERM_PROGRAM") or ""
    term_program = term_program_raw or "(not set)"
    term = os.environ.get("TERM") or "(not set)"
    if term_program_raw.strip().lower() == "vscode":
        return (f"{OK} TERM_PROGRAM=vscode, TERM={term} -- if a shortcut (e.g. Ctrl+E) does nothing, add "
                f'ONE of these to settings.json (Cmd+Shift+P, "Preferences: Open User Settings (JSON)"): '
                f'"terminal.integrated.sendKeybindingsToShell": true  -- or, to release just that one key: '
                f'"terminal.integrated.commandsToSkipShell": ["-<command owning ctrl+e>"] (find the exact '
                f'command id in Keyboard Shortcuts, Cmd+K Cmd+S, search ctrl+e). Check with /keys afterward.')
    return f"{OK} TERM_PROGRAM={term_program}, TERM={term}"


def _check_shell() -> str:
    """H9 OpenCode item 23 (carried-over must-do: "Git Bash (win32)"): the
    Bash tool (halo_harness/tools/bash.py) needs Git Bash on win32
    (config.paths.git_bash()) or `/bin/bash` on POSIX -- without it, the
    Bash tool, `!`-pre-execution and every `command`/`shell:"bash"` hook
    handler are all unusable, so a genuinely missing shell IS a MISSING,
    not a WARN (unlike rg/$EDITOR above, which degrade gracefully)."""
    from halo_harness.config.paths import git_bash
    bash = git_bash()
    if bash is not None and Path(bash).exists():
        label = "Git Bash" if sys.platform == "win32" else "bash"
        return f"{OK} {label} found: {bash}"
    if sys.platform == "win32":
        return _fix(f"{MISSING} Git Bash not found -- the Bash tool requires it on Windows "
                    f"(install Git for Windows, or set CLAUDE_CODE_GIT_BASH_PATH)",
                    see="https://git-scm.com/download/win")
    return _fix(f"{MISSING} bash not found on PATH -- the Bash tool (and every shell hook) requires it",
                cmd="sudo apt install bash")


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
            return f"{OK} Linux (Kali) -- halo's primary target platform"
        return f"{OK} Linux -- {platform.release()}"
    if system == "Windows":
        wsl = shutil.which("wsl")
        hint = " (wsl.exe found -- verify parity there too)" if wsl else " (no wsl.exe found on PATH)"
        return f"{OK} Windows -- build/test host, not the primary target{hint}"
    return f"{OK} {system} -- {platform.release()}"


def _check_drain_tick_rate() -> str:
    """Halo 2.0.2 round C (the owner's own background-streaming report):
    "halo doctor gains a measured drain-tick rate over 2 s" -- the TUI's
    real `_drain` timer (tui/app.py) fires at `DRAIN_HZ` (30 Hz) and is
    the ONLY thing that keeps the status bar's live signal (agents/bg
    jobs/elapsed) and a sub-agent's own card moving while a turn runs
    off-thread; if the host's asyncio event loop can't actually sustain
    that rate (a busy terminal, a starved VM, the owner's own macOS/VS
    Code report), the screen looks hung even though real work is still
    happening. Runs a bare asyncio loop at the SAME cadence for 2 REAL
    seconds and reports how many ticks landed -- never builds a real
    BridgeApp/Controller/Session (this measures the HOST's own asyncio
    scheduling under THIS terminal/platform, which is the actual
    variable in play; a real session would need model/credential setup
    doctor has no business requiring just to answer this)."""
    import asyncio

    from halo_harness.tui.keys import DRAIN_HZ

    # Test hygiene: every doctor-touching test in the suite calls
    # `run_checks()`/`run_checks_structured()` (several call it more than
    # once), which would otherwise all pay a real 2 s for this one check --
    # `tests/helpers/provider_env_defaults.ensure_default_provider_
    # credentials`/`tests/helpers/runner.run_all` both `setdefault` this to
    # a near-instant window before any test runs (same "scope it down for
    # tests" rule BRIDGE_TEST_NO_BACKGROUND_NET already follows); a real
    # `halo doctor` invocation never sets it, so it still measures the full
    # 2 s the brief asks for.
    window_s = 2.0
    override = os.environ.get("BRIDGE_TEST_DRAIN_TICK_WINDOW_S")
    if override:
        try:
            window_s = max(0.05, float(override))
        except ValueError:
            pass

    async def _measure() -> int:
        count = 0
        interval = 1.0 / DRAIN_HZ
        loop = asyncio.get_event_loop()
        deadline = loop.time() + window_s
        while loop.time() < deadline:
            await asyncio.sleep(interval)
            count += 1
        return count

    try:
        ticks = asyncio.run(_measure())
    except Exception as e:
        return f"{WARN} could not measure the drain-tick rate: {type(e).__name__}: {e}"
    hz = ticks / window_s
    # Below half the target rate is a real, visible stall (a turn's own
    # liveness segments would update roughly half as often as designed,
    # or worse) -- above that, occasional scheduling jitter is normal
    # and not worth a WARN.
    if hz < DRAIN_HZ * 0.5:
        return (f"{WARN} drain-tick rate ~{hz:.1f} Hz measured over {window_s:g} s (target {DRAIN_HZ} Hz) -- "
                f"this terminal/platform may show stale liveness info (status bar, sub-agent cards) "
                f"during a long-running turn")
    return f"{OK} drain-tick rate ~{hz:.1f} Hz measured over {window_s:g} s (target {DRAIN_HZ} Hz)"


def _check_catalog_ages() -> list:
    """H8 scope C: show the age of every cached catalog file (models.json,
    dbx-endpoints.json, models-dev.json) -- a missing file is a plain WARN
    ("never refreshed yet", not a failure: `resolve_model_profile` still
    has the vendored package fallback); a very stale one (>30 days) is
    flagged so a work-box user knows `halo models --refresh` is
    overdue, without ever being INTERNET-only (the vendored tier means
    a stale/missing cache is never actually broken, just less current)."""
    import time
    from halo_harness.config.paths import bridge_home
    from halo_harness.providers.databricks import dbx_endpoints_path, models_json_path
    from halo_harness.providers.models_dev import models_dev_json_path
    state_dir = bridge_home()
    lines = []
    for label, path_fn in (("models.json (OpenRouter)", models_json_path),
                            ("dbx-endpoints.json (Databricks)", dbx_endpoints_path),
                            ("models-dev.json (models.dev)", models_dev_json_path)):
        path = path_fn(state_dir)
        if not path.exists():
            lines.append(_fix(f"{WARN} {label}: never cached (vendored package fallback still applies)",
                               cmd="halo models --refresh"))
            continue
        age_days = (time.time() - path.stat().st_mtime) / 86400
        if age_days > 30:
            lines.append(_fix(f"{WARN} {label}: cached {age_days:.1f} day(s) ago ({path})",
                               cmd="halo models --refresh"))
        else:
            lines.append(f"{OK} {label}: cached {age_days:.1f} day(s) ago ({path})")
    return lines


def _check_telemetry_and_improve() -> "list[str]":
    """H10 Part A/B: `stats`'s own sessions count + stats-cache age, and
    `/improve`'s active config (a plain INFO line, never OK/WARN/MISSING --
    there is nothing here that can be "missing"; every key has a built-in
    default). Read-only, same contract as every other check in this
    module."""
    from halo_harness import telemetry
    from halo_harness.improve.config import load_improve_config

    n = telemetry.total_sessions_count()
    age = telemetry.cache_age_seconds()
    age_str = "never" if age is None else (f"{age:.0f}s ago" if age < 3600 else f"{age / 3600:.1f}h ago")
    lines = [f"{OK} Sessions: {n} logged under ~/.halo/sessions; stats cache last written {age_str}"]
    cfg = load_improve_config()
    lines.append(
        f"{OK} /improve: enabled={cfg.enabled} hint={cfg.hint} model={cfg.model or '(small/session model)'} "
        f"since_days={cfg.since_days} max_candidates={cfg.max_candidates}"
    )
    return lines


def _check_local_bin_on_path() -> Optional[str]:
    """New (RECOMMENDATIONS.md P0 #2 / H12 brief Part B): `~/.local/bin`
    (pipx/`uv tool install`/`pip install --user`'s own console-script
    location) missing from PATH for a NON-interactive shell is the single
    most common "halo: command not found" right after install --
    checked with `halo_harness.linux_fixes.local_bin_on_noninteractive_path`
    (re-invokes `$SHELL -c 'echo $PATH'`, never this process's own already-
    widened inherited PATH). Linux/macOS only -- Windows has no equivalent
    PATH-for-a-non-interactive-shell concept; `halo init`'s own
    Windows-launcher check covers the Windows side of this instead."""
    if sys.platform == "win32":
        return None
    from halo_harness.linux_fixes import PATH_LINE, local_bin_on_noninteractive_path, rc_file_for_shell
    if local_bin_on_noninteractive_path():
        return f"{OK} ~/.local/bin on PATH for a non-interactive shell"
    rc_path = rc_file_for_shell()
    return _fix(f"{WARN} ~/.local/bin not on PATH for a non-interactive shell",
                cmd=f"echo '{PATH_LINE}' >> {rc_path}")


def _check_tmux_mouse() -> Optional[str]:
    """New (RECOMMENDATIONS.md P0 #2): only relevant -- and only checked --
    when `$TMUX` says we're actually inside a tmux session; `tmux show -g
    mouse` reflects whether tmux itself is forwarding mouse events to
    halo at all (Textual's own mouse capture needs tmux's
    cooperation first, see INSTALL.md's own "Mouse" terminal note)."""
    if not os.environ.get("TMUX"):
        return None
    try:
        proc = subprocess.run(["tmux", "show", "-g", "mouse"], capture_output=True, text=True, timeout=5)
        output = (proc.stdout or "").strip()
    except (OSError, subprocess.SubprocessError) as e:
        return _fix(f"{WARN} tmux mouse mode: could not check ({type(e).__name__}: {e})", cmd="tmux set -g mouse on")
    if "on" in output.split():
        return f"{OK} tmux mouse mode: on ({output})"
    return _fix(f"{WARN} tmux mouse mode: not on ({output or 'no output'}) -- click-to-focus/drag-scroll/"
                f"drag-select-to-copy won't work until it is (Shift+drag still uses the terminal's native "
                f"selection either way)", cmd="tmux set -g mouse on")


_UNSET = object()


def _repo_bin_dir() -> Path:
    """This checkout's own `bin/` directory -- a `halo` that
    resolves there is the checkout's own convenience wrapper (`bin/
    halo`/`bin/halo.cmd`), never a real installed console
    script, and only ever works while the current directory is inside the
    checkout."""
    return Path(__file__).resolve().parent.parent / "bin"


def _externally_managed_marker_present() -> bool:
    """1.0.1 part 2 fixpass finding 16: PEP 668 -- Debian 12+/Kali's own
    system Python refuses a bare `pip install --user` outright once this
    marker file exists next to it. Located via `sysconfig`'s own stdlib
    directory (the directory the marker is documented to live beside),
    never a hardcoded path, so this is correct on every platform/venv
    layout; `BRIDGE_TEST_EXTERNALLY_MANAGED` (unset/"0"/"1") is a test seam
    that skips the real filesystem check entirely."""
    override = os.environ.get("BRIDGE_TEST_EXTERNALLY_MANAGED")
    if override is not None:
        return override == "1"
    import sysconfig
    try:
        stdlib = Path(sysconfig.get_path("stdlib"))
    except Exception:
        return False
    return (stdlib / "EXTERNALLY-MANAGED").exists()


def _detect_install_tool(*, uv_found: Optional[bool] = None, pipx_found: Optional[bool] = None,
                          externally_managed: Optional[bool] = None) -> str:
    """"uv" when `uv` is on PATH; else, when PEP 668's own EXTERNALLY-
    MANAGED marker is present (a bare `pip install --user` is refused
    outright there -- Kali/Debian 12+), "pipx" if `pipx` is ALSO on PATH,
    else "venv" (nothing safe to run automatically -- the fix line points
    at making one instead); else "pip" (every other box, unchanged from
    before this fix). Every probe is a test seam, defaulting to a real
    `shutil.which`/marker-file check when omitted."""
    if uv_found is None:
        uv_found = bool(shutil.which("uv"))
    if uv_found:
        return "uv"
    if externally_managed is None:
        externally_managed = _externally_managed_marker_present()
    if externally_managed:
        if pipx_found is None:
            pipx_found = bool(shutil.which("pipx"))
        return "pipx" if pipx_found else "venv"
    return "pip"


def reinstall_command(*, uv_found: Optional[bool] = None, pipx_found: Optional[bool] = None,
                       externally_managed: Optional[bool] = None) -> str:
    """The exact command the owner's own report asked for: `uv tool
    install --reinstall .` (run from this checkout) when `uv` is present --
    both it and every other branch below reinstall the CONSOLE SCRIPT a
    bare `halo` from any directory needs, and all need re-running
    after every `git pull` (an editable/tool install does not auto-update
    the installed script's own dependency pins or entry point).

    1.0.1 part 2 fixpass finding 16: without `uv`, a bare `pip install
    --user -e .` fails outright under PEP 668 on Kali/Debian 12+
    (`error: externally-managed-environment`) -- `pipx install --force
    -e .` is suggested instead when that marker is present and `pipx` is
    on PATH, else a venv (never a bare `pip install --user` on such a
    box).

    M5 (1.0.1 final pass): the venv branch also links the venv's own
    `.venv/bin/halo` console script into `~/.local/bin` -- a plain
    `python -m venv` has no console-script-on-PATH story of its own (unlike
    `uv tool install`/`pipx install`, both of which register one), so
    without this step `check_command_on_path`'s own `shutil.which
    ("halo")` kept failing even right after running the suggested
    venv command; `_check_local_bin_on_path` already covers getting
    `~/.local/bin` itself onto a non-interactive shell's PATH, so linking
    into it (rather than some venv-specific location) is what makes THAT
    existing fix actually resolve the command too."""
    tool = _detect_install_tool(uv_found=uv_found, pipx_found=pipx_found, externally_managed=externally_managed)
    if tool == "uv":
        return "uv tool install --reinstall ."
    if tool == "pipx":
        return "pipx install --force -e ."
    if tool == "venv":
        return ('python3 -m venv .venv && .venv/bin/pip install -e . && mkdir -p ~/.local/bin && '
                'ln -sf "$PWD/.venv/bin/halo" ~/.local/bin/halo')
    return "pip install --user -e ."


def check_command_on_path(*, resolved=_UNSET, uv_found: Optional[bool] = None, pipx_found: Optional[bool] = None,
                           externally_managed: Optional[bool] = None) -> str:
    """New (owner report from the work VM, H15 addendum): a bare
    `halo` typed OUTSIDE the checkout directory did not work --
    only the checkout's own `bin/halo` wrapper had ever been used,
    always run from inside it. `shutil.which("halo")` must resolve
    to a REAL installed console script (the `uv tool install`/`pip
    install` entry point), never this repo's own `bin/` wrapper and never
    nothing -- the OK line names the resolved path; the WARN line's own
    `-> fix:` names the exact reinstall command for whichever tool is
    detected and reminds that it's needed again after every `git pull`.

    2.0.1 ("run from any directory" release): the message now spells out
    WHICH of the three copies `halo` resolves to, by name, and says
    plainly on BOTH warning branches that it is not the installed one --
    the installed console script itself (OK), this checkout's own
    `bin/halo`/`bin/halo.cmd` wrapper (WARN -- only works from inside the
    checkout), or neither at all, meaning the only thing that currently
    runs `halo` is a bare `PYTHONPATH=<repo> python -m halo_harness`
    fallback (WARN -- that's how `bin/halo` itself behaves once it finds
    no installed script either, and exactly how this repo's own test
    suite drives the harness without installing it).

    `resolved`/`uv_found`/`pipx_found`/`externally_managed` are test seams;
    omitted, this does real `shutil.which`/marker-file lookups."""
    if resolved is _UNSET:
        resolved = shutil.which("halo") or shutil.which("halo.exe")
    fix_cmd = (f"{reinstall_command(uv_found=uv_found, pipx_found=pipx_found, externally_managed=externally_managed)}"
               f" (run from this checkout -- repeat after every `git pull`)")
    if not resolved:
        return _fix(f"{WARN} halo command: not found on PATH -- not an installed console script, only a "
                    f"PYTHONPATH fallback", cmd=fix_cmd)
    try:
        is_repo_wrapper = Path(resolved).resolve().parent == _repo_bin_dir().resolve()
    except OSError:
        is_repo_wrapper = False
    if is_repo_wrapper:
        return _fix(f"{WARN} halo command: PATH resolves to this checkout's own bin/ wrapper "
                    f"({resolved}) -- not the installed console script, only works from inside the checkout",
                    cmd=fix_cmd)
    return f"{OK} halo command: {resolved} (installed console script)"


def _check_install() -> str:
    """Halo 2.0.2 round 6: a purely informative line (never WARN/MISSING --
    `check_command_on_path` right below is what flags a PROBLEM) naming
    HOW this running `halo` was installed, from where, and which commit --
    `halo update`'s own `update.installed_build`/`install_kind`."""
    from halo_harness import update as upd
    label = {"uv_tool": "uv tool", "pipx": "pipx", "pip": "pip",
             "editable_checkout": "editable checkout", "dir_checkout": "checkout",
             "bare_checkout": "checkout on PYTHONPATH",
             # Halo 2.0.2 round C: "treat it as a known kind (source_dir)
             # in doctor's install line" -- a plain source directory (no
             # .git, no dist metadata), never "unknown".
             "source_dir": "plain source directory (no git metadata)", "unknown": "unknown"}
    try:
        build = upd.installed_build()
        kind = upd.install_kind()
        where = kind.get("spec") or build.get("checkout") or "?"
        return f"{OK} install: {label.get(kind['kind'], kind['kind'])} ({where}) -- {upd.format_version_line(build)}"
    except Exception as e:
        return f"{OK} install: could not be determined ({type(e).__name__}: {e})"


def _check_old_rolo_claude_leftover(*, resolved=_UNSET, uv_found: Optional[bool] = None,
                                     pipx_found: Optional[bool] = None,
                                     externally_managed: Optional[bool] = None) -> Optional[str]:
    """2.0.1: `rolo-claude` is no longer one of this distribution's own
    console scripts -- `pyproject.toml`'s `[project.scripts]` ships exactly
    one executable, `halo`, since an old, separately-installed `rolo-claude`
    tool owning that name made a fresh `uv tool install --editable .` of
    Halo fail outright ("Executable already exists: rolo-claude") and
    `halo` never got installed at all (see CHANGELOG [2.0.1]). So any
    `rolo-claude` found on PATH now, other than this checkout's own
    `bin/rolo-claude` (a plain script, never an installed console script --
    same `is_repo_wrapper` check `check_command_on_path` uses for `halo`
    itself), is a leftover from that old, separate install -- still runnable
    by mistake, and never updated by a `halo`-side `git pull`/reinstall.

    `None` (skipped entirely, same "nothing to report" convention every
    other optional check in this module uses) when nothing is found there,
    or when what's found IS this checkout's own shim. `resolved`/
    `uv_found`/`pipx_found`/`externally_managed` are test seams, identical
    in shape to `check_command_on_path`'s own."""
    if resolved is _UNSET:
        resolved = shutil.which("rolo-claude") or shutil.which("rolo-claude.exe")
    if not resolved:
        return None
    try:
        is_repo_wrapper = Path(resolved).resolve().parent == _repo_bin_dir().resolve()
    except OSError:
        is_repo_wrapper = False
    if is_repo_wrapper:
        return None
    tool = _detect_install_tool(uv_found=uv_found, pipx_found=pipx_found, externally_managed=externally_managed)
    uninstall_cmd = {"uv": "uv tool uninstall rolo-claude", "pipx": "pipx uninstall rolo-claude"}.get(
        tool, "pip uninstall rolo-claude")
    return _fix(f"{WARN} rolo-claude: an old, separate install is still on PATH ({resolved}) -- halo no longer "
                f"ships this executable (use `halo` instead); uninstall the old tool so it can never run by "
                f"mistake", cmd=uninstall_cmd)


# Test hygiene (round B fix pass, notes file): exact names several
# test_*.py files' own fake MCP servers are known to use, plus the
# `plugin_*fakeserver.log` shape a plugin-scoped fake server test uses --
# every one found under a REAL `~/.halo/mcp/` (never a BRIDGE_TEST_HOME-
# scoped one; a hermetic test run's own temp dir is never where a USER
# would run `halo doctor`) is a near-certain leftover from a standalone
# test run that predates `tests/helpers/runner.run_all`'s own fix for
# this (it now scopes BRIDGE_TEST_HOME itself before any test runs).
_TEST_FIXTURE_LOG_NAMES = frozenset({
    "a.log", "b.log", "c.log", "big.log", "crash.log", "eager1.log", "fake.log",
})


def _is_test_fixture_log_name(name: str) -> bool:
    if name in _TEST_FIXTURE_LOG_NAMES:
        return True
    if name.startswith("fake.log."):  # fake.log.1, fake.log.2, ... (rotated)
        return True
    return name.startswith("plugin_") and "fakeserver" in name and name.endswith(".log")


def _check_test_leftovers(state_dir: Optional[Path] = None) -> Optional[str]:
    """`None` (no entry at all -- this is informational, never a MISSING;
    nothing is actually broken) when the mcp log dir doesn't exist or
    nothing in it matches a known test-fixture name."""
    try:
        from halo_harness.config.paths import bridge_home
        sd = Path(state_dir) if state_dir is not None else bridge_home()
        mcp_dir = sd / "mcp"
        if not mcp_dir.is_dir():
            return None
        found = sorted(p.name for p in mcp_dir.iterdir() if p.is_file() and _is_test_fixture_log_name(p.name))
    except OSError:
        return None
    if not found:
        return None
    shown = ", ".join(found[:8]) + (f", +{len(found) - 8} more" if len(found) > 8 else "")
    # H12 Part B: every WARN/MISSING line must end with -> fix:/-> see:
    # (test_doctor_prescriptive_fixes.py's own enforced rule) -- there is
    # no single safe `rm` one-liner for a variable-length file list, so
    # this names the directory to inspect by hand instead of a command.
    return _fix(f"{WARN} probable test leftovers in {mcp_dir}: {shown} -- safe to delete",
                cmd=f"inspect and remove by hand from {mcp_dir}")


def _check_mcp_servers(cwd: Optional[Path]) -> str:
    """New (RECOMMENDATIONS.md P0 #2 / section 3, "MCP startup cost is the
    biggest perceived-speed item"): configured MCP servers -- a pure config
    MERGE enumeration (`mcp.manager.resolve_server_configs`, never connects
    to anything, unlike `halo mcp list`'s own live health check) plus
    the eager-vs-`mcpLazy` breakdown. No connect-time history is recorded
    anywhere yet (a future telemetry pass, RECOMMENDATIONS.md P1), so this
    never fabricates a total-time WARN from an unmeasured guess -- it names
    the real, actionable lever (mcpLazy) as plain information instead."""
    cwd = cwd or Path.cwd()
    try:
        from halo_harness.config.claude_json import load_claude_json
        from halo_harness.mcp.manager import resolve_server_configs
        claude_json = load_claude_json()
        resolved, _notices = resolve_server_configs(cwd=cwd, claude_json=claude_json)
    except Exception as e:
        return _fix(f"{WARN} MCP servers: could not enumerate ({type(e).__name__}: {e})",
                     cmd="halo doctor")
    if not resolved:
        # "explain the zero" (gap-list brief): never a bare 0 -- name every
        # scope searched and its own count, plus another directory's own
        # .mcp.json when the user's history shows one.
        try:
            from halo_harness.mcp import explain
            scope_text = explain.scope_summary_line(cwd=cwd, claude_json=claude_json)
            others = explain.other_project_mcp_jsons(cwd=cwd, claude_json=claude_json)
        except Exception:
            scope_text, others = "", []
        suffix = f" -- {scope_text}" if scope_text else ""
        if others:
            suffix += f" ({'; '.join(others)})"
        return f"{OK} MCP servers: none configured{suffix}"
    eager = sorted(name for name, cfg in resolved.items() if not cfg.lazy)
    lazy_names = sorted(name for name, cfg in resolved.items() if cfg.lazy)
    hint = ("" if not eager else
            " -- no connect-time history recorded yet; if startup feels slow, add "
            "\"mcpLazy\": true to a slow one in .mcp.json/settings.json")
    # H13 Part A: lazy is now the default, so most servers here are lazy --
    # `mcp.tools_cache`'s own age (best-effort, pure file stat, never
    # connects anything -- matches this whole function's "never connects"
    # contract) tells the user whether a lazy server's catalog entry is
    # fresh or has been stale for a while.
    ages = []
    try:
        from halo_harness.mcp import tools_cache
        for name in lazy_names:
            age_s = tools_cache.cache_age_s(name)
            if age_s is not None:
                ages.append(f"{name} {_format_age(age_s)}")
    except Exception:
        pass
    cache_hint = f"; cache ages: {', '.join(ages)}" if ages else ""
    return (f"{OK} MCP servers: {len(resolved)} configured "
            f"({len(eager)} eager, {len(lazy_names)} lazy){hint}{cache_hint}")


def _check_mcp_connectors() -> str:
    """W4 MCP connectors-bridge item 5: "Doctor shows the bridge state."
    Cache-only (never spawns `claude` from a doctor run)."""
    try:
        from halo_harness.mcp import connectors_bridge
    except Exception as e:
        return f"{WARN} claude.ai connectors: could not check ({type(e).__name__}: {e})"
    if not connectors_bridge.bridge_enabled():
        return f"{OK} claude.ai connectors: bridge disabled (connectors.bridge=false in ~/.halo/config.json)"
    reason = connectors_bridge.unavailable_reason()
    if reason:
        return f"{OK} {reason}"
    connectors, _fetched_at = connectors_bridge.load_cache()
    if not connectors:
        return (f"{OK} claude.ai connectors: bridge enabled, none discovered yet "
                f"(discovery runs in the background on the next launch, or `halo mcp list --refresh` now)")
    names = ", ".join(sorted(c.name for c in connectors))
    return f"{OK} claude.ai connectors: bridge enabled, {len(connectors)} discovered ({names})"


def _check_ollama_hosts() -> "list[tuple[str, str]]":
    """Halo 2.0.3 round 3 (brief item 4): "`halo doctor` gains an Ollama
    section, one line per host" -- a LIGHTWEIGHT per-host check
    (`probe_version` + `/api/ps`'s own loaded count only, never the full
    `/api/show`-per-model catalog `/ollama`/`halo ollama` read -- doctor
    must stay fast even with several hosts configured). Never raises: an
    unreachable host is the ordinary case on most boxes (no Ollama
    installed at all), not a WARN-worthy one -- `[OK]` either way, same
    "not configured" vocabulary this whole module already uses for
    OpenRouter/Databricks."""
    try:
        from halo_harness.providers.ollama import fetch_ps, probe_version, resolve_ollama_hosts
    except Exception as e:
        return [("ollama_hosts", f"{WARN} Ollama: could not check ({type(e).__name__}: {e})")]
    try:
        hosts = resolve_ollama_hosts()
    except Exception as e:
        return [("ollama_hosts", f"{WARN} Ollama: could not resolve configured hosts ({type(e).__name__}: {e})")]
    entries: "list[tuple[str, str]]" = []
    for host in hosts:
        cid = f"ollama_host_{host.name}"
        version_info = probe_version(host)
        if not isinstance(version_info, dict):
            entries.append((cid, f"{OK} Ollama ({host.name}): not reachable at {host.url}"))
            continue
        version = version_info.get("version") or "?"
        ps = fetch_ps(host) or {}
        loaded = len(ps.get("models") or [])
        entries.append((cid, f"{OK} Ollama ({host.name}): reachable, version {version}, "
                              f"{loaded} model(s) loaded ({host.url})"))
        # Round 5b part 2 (brief item 4): "the matching section in `halo
        # doctor`" -- the SAME `host_setup_checklist` text `halo ollama
        # doctor` prints, one [OK] line per sentence (never a block) so
        # this stays inside doctor.py's own one-line-per-fact format.
        # Cheap (no extra network call beyond the probe/`/api/ps` already
        # done above) -- never slows `halo doctor` down meaningfully.
        try:
            from halo_harness.providers.ollama_panel import host_setup_checklist
            for i, line in enumerate(host_setup_checklist(host)):
                entries.append((f"{cid}_setup_{i}", f"{OK} Ollama ({host.name}): {line}"))
        except Exception as e:
            entries.append((f"{cid}_setup", f"{WARN} Ollama ({host.name}): could not build the setup "
                                              f"checklist ({type(e).__name__}: {e})"))
    return entries


def _format_age(seconds: float) -> str:
    if seconds < 3600:
        return f"{int(seconds // 60)}m"
    if seconds < 86400:
        return f"{int(seconds // 3600)}h"
    return f"{int(seconds // 86400)}d"


def _provider_configured(ref) -> "tuple[bool, str]":
    """`(is_configured, human_label)` for a `model.ModelRef`'s own
    provider -- shared by `_check_default_model` below; the SAME resolvers
    every other provider-specific check in this module already uses, so
    this never disagrees with the `OpenRouter:`/`Databricks:`/`Claude
    subscription:` lines just above it. Never raises -- same "degrade to a
    clear line" contract as every other check (`_check_openrouter`/
    `_check_databricks`/`_check_claude_subscription` each guard their own
    resolver call the same way; this one guards all of them at once since
    it calls whichever ONE actually applies to `ref.provider`)."""
    _load_env_file_best_effort()
    try:
        if ref.provider == "openrouter":
            from halo_harness.providers.config import resolve_openrouter
            return resolve_openrouter() is not None, "OpenRouter"
        if ref.provider == "databricks":
            from halo_harness.providers.config import resolve_databricks
            return resolve_databricks() is not None, "Databricks"
        if ref.provider == "anthropic":
            from halo_harness.providers.config import resolve_anthropic
            return resolve_anthropic() is not None, "ANTHROPIC_API_KEY"
        if ref.provider == "cc":
            from halo_harness.providers.cc_models import SUBSCRIPTION_AUTH_METHODS, claude_auth_status
            status = claude_auth_status()
            ok = bool(status and status.logged_in and status.auth_method in SUBSCRIPTION_AUTH_METHODS)
            return ok, "Claude subscription"
    except Exception as e:
        return False, f"could not check ({type(e).__name__}: {e})"
    return True, ref.provider


def _guess_provider_for_doctor_message(configured: str) -> "Optional[str]":
    """Best-effort prefix sniff -- the SAME shapes `model.parse_model_ref`
    itself dispatches on, just never resolving aliases/routes.json, since
    this only runs after `parse_model_ref` has ALREADY refused the ref
    (`_check_default_model`'s own `InvalidModelError` branch) and needs to
    know which provider that was ABOUT without re-parsing it."""
    from halo_harness.providers.enablement import PREFIXES, PROVIDER_NAMES
    for name in PROVIDER_NAMES:
        prefix = PREFIXES.get(name)
        if prefix and configured.startswith(prefix):
            return name
    if configured.startswith("databricks-") or configured.startswith("system.ai."):
        return "databricks"
    if ("/" in configured and configured.count("/") == 1
            and not any(ch.isspace() for ch in configured)):
        parts = configured.split("/")
        if parts[0] and parts[1]:
            return "openrouter"
    return None


def _check_default_model() -> str:
    """New (RECOMMENDATIONS.md P0 #2): `~/.halo/config.json`'s own
    `"model"` key (written by `halo init` step 3, or a plain
    `halo config set model ...`) and whether ITS provider actually
    resolves -- via `model.parse_model_ref`, the SAME parser `headless.
    build_session` uses, so this line never disagrees with what a real
    session would actually pick. Not set at all is perfectly normal (the
    built-in default applies) and reported OK, never WARN."""
    from halo_harness.model import DEFAULT_MODEL_REF, parse_model_ref
    from halo_harness.providers.routing import InvalidModelError
    from halo_harness.theme import get_config_value
    configured = get_config_value("model", default=None)
    if not isinstance(configured, str) or not configured:
        return (f"{OK} Default model: not set in config.json -- built-in default {DEFAULT_MODEL_REF!r} "
                f"applies (HALO_MODEL/routes.json still win when set)")
    try:
        ref = parse_model_ref(configured)
    except InvalidModelError as e:
        # H15 part 2 addendum 3.1: parse_model_ref now ALSO refuses a
        # syntactically-fine ref whose provider just isn't auto-detected as
        # enabled (not only an explicit `providers disable`) -- give the
        # SAME specific "not configured"/"not enabled" guidance the
        # provider_ok-but-disabled branch below already gives a ref that
        # parses fine, instead of the generic "does not resolve" wording
        # (meant for an actually garbled ref, where this guess finds
        # nothing and falls through to it unchanged).
        provider_guess = _guess_provider_for_doctor_message(configured)
        if provider_guess is not None:
            from halo_harness.providers.enablement import is_provider_disabled_message
            disabled_msg = is_provider_disabled_message(provider_guess)
            if disabled_msg:
                provider_flag = {"databricks": "databricks", "claude_subscription": "claude",
                                  "anthropic": "anthropic"}.get(provider_guess, "openrouter")
                return _fix(f"{WARN} Default model: {configured} -- {disabled_msg}",
                             cmd=f"halo init --provider {provider_flag}")
        return _fix(f"{WARN} Default model: config.json's model={configured!r} does not resolve ({e})",
                     cmd=f"halo config set model {DEFAULT_MODEL_REF}")
    except Exception as e:
        return _fix(f"{WARN} Default model: config.json's model={configured!r} does not resolve ({e})",
                     cmd=f"halo config set model {DEFAULT_MODEL_REF}")
    provider_ok, provider_label = _provider_configured(ref)
    if provider_ok:
        from halo_harness.providers.enablement import is_provider_disabled_message
        disabled_msg = is_provider_disabled_message(ref.provider)
        if disabled_msg:
            # H15 item 21: credentials resolve but the provider isn't
            # ENABLED -- a `/model`/`-p`/TUI call would be refused with
            # this SAME message, so doctor must not call it just "OK".
            return _fix(f"{WARN} Default model: {configured} -- {disabled_msg}",
                        cmd=f"halo providers enable {ref.provider}")
        return f"{OK} Default model: {configured} ({provider_label} configured)"
    # 1.0.1 hotfix 13 (drive-by): this suggestion still said `--preset
    # work`/`--preset home` -- the deprecated alias still works, but every
    # OTHER user-facing spot already moved to `--provider` at item 13.
    provider_flag = {"databricks": "databricks", "cc": "claude", "anthropic": "anthropic"}.get(
        ref.provider, "openrouter")
    return _fix(f"{WARN} Default model: {configured} -- {provider_label} not configured",
                 cmd=f"halo init --provider {provider_flag}")


def _check_providers_enabled(cwd: Optional[Path] = None, settings_flag: Optional[str] = None) -> str:
    """H15 item 21: a one-line summary of the explicit provider-enablement
    table (`halo providers` has the full detail) -- always INFO
    (never WARN/MISSING: a box with nothing enabled yet is normal before
    the first `init`/`providers enable`, same as every other "not
    configured yet" line in this module).

    Findings 22/23 (2.0.1): `cwd`/`settings_flag` (this command's own
    `--cwd`/`--settings` flags, see `cmd_doctor`) are threaded into
    `listing_effective_env` so this line never disagrees with what a real
    session launched against that same `--cwd`/`--settings` would
    resolve."""
    from halo_harness.providers.config import listing_effective_env
    from halo_harness.providers.enablement import PROVIDER_NAMES, credentials_present, is_enabled
    # 1.0.1 part 2 fixpass finding 3: the merged settings-aware env (a
    # credential living only in a settings.json `env` block is seen here
    # too), and `credentials_present` computed exactly once per provider --
    # same "detection for listing" fix `/providers`'s own `provider_rows()`
    # applies, reused here so doctor never disagrees with it.
    env = listing_effective_env(cwd, settings_flag)
    detected = {p: credentials_present(p, env=env) for p in PROVIDER_NAMES}
    enabled = [p for p in PROVIDER_NAMES if is_enabled(p, detected=detected[p])]
    detected_not_enabled = [p for p in PROVIDER_NAMES if detected[p] and p not in enabled]
    line = f"{OK} Providers: {len(enabled)}/{len(PROVIDER_NAMES)} enabled ({', '.join(enabled) or 'none'})"
    if detected_not_enabled:
        line += f" -- detected but not enabled: {', '.join(detected_not_enabled)} (see `halo providers`)"
    return line


def _check_permission_mode() -> str:
    """1.0.1 hotfix 18.3: the effective STARTING permission mode a new
    session would actually launch with, and which layer of the precedence
    chain decided it -- `--dangerously-skip-permissions` >
    `--permission-mode` (this doctor run has neither, being a standalone
    command) > `~/.halo/config.json`'s own `permission_mode` (item
    18.1's new init step) > settings.json's `permissions.defaultMode` >
    the hardcoded `default`. Mirrors `headless.py::build_session`'s own
    chain exactly (same config key, same settings field) so this line
    never disagrees with what a real session would actually start in."""
    from halo_harness.permissions import normalize_permission_mode
    from halo_harness.theme import get_config_value
    config_mode = get_config_value("permission_mode", default=None)
    if isinstance(config_mode, str) and config_mode:
        mode = normalize_permission_mode(config_mode)
        return f"{OK} Permission mode: {mode} (source: halo config.json)"
    try:
        settings = resolve_settings(Path.cwd())
        settings_mode = settings.permissions_default_mode
    except Exception:
        settings_mode = None
    if settings_mode:
        return f"{OK} Permission mode: {normalize_permission_mode(settings_mode)} (source: settings.json)"
    # An OK/info line never carries the literal " -> fix:"/" -> see:" suffix
    # (`_parse_check_line`'s own regex would then treat this as though it
    # were a WARN/MISSING with a prescriptive fix, tripping run_checks_
    # structured's "an ok/info entry has neither" invariant) -- the
    # suggestion is woven into parenthetical prose instead, same as
    # _check_default_model's own "not set -- built-in default applies"
    # line just above.
    return (f"{OK} Permission mode: default (source: built-in default -- no config.json permission_mode, "
            f"no settings.json permissions.defaultMode; run `halo init` or `halo config set "
            f"permission_mode auto` to set one)")


def _dbx_probe_target():
    """`(host, token)` from the real discovery chain, or `(host, None)`
    when a step has a host but no matching token ("host-only" -- scope E),
    or `(None, None)` when nothing at all resolves -- shared by every
    `--work` check below so they never disagree about which config they're
    evaluating (the same chain `_check_databricks` uses for the plain
    `doctor` line, just returning the raw values instead of a formatted
    string). Without the host-only fallback, `resolve_databricks()`'s own
    all-or-nothing contract (host+token together, or neither) means a real
    "token missing" state can never actually reach `_work_check_token_
    validity`/`_work_check_vpn_reachability` at all -- it always looked
    identical to "nothing configured"."""
    _load_env_file_best_effort()
    try:
        from halo_harness.providers.config import resolve_databricks, resolve_databricks_host_only
        dbx = resolve_databricks()
    except Exception:
        return None, None
    if dbx is not None:
        return dbx.host, dbx.token
    try:
        host_only = resolve_databricks_host_only()
    except Exception:
        host_only = None
    return host_only, None


def _check_ucode_settings() -> str:
    """H8 scope F must-do (`ug`-compatibility note): report whether
    `~/.claude/ucode-settings.json` exists/parses, independent of whether it
    actually WON the discovery chain (an earlier source, e.g. DATABRICKS_
    HOST, may have already resolved first) -- purely informational."""
    from halo_harness.config.paths import claude_config_dir
    from halo_harness.providers.config import load_ucode_settings
    path = claude_config_dir() / "ucode-settings.json"
    if not path.exists():
        return _fix(f"{WARN} ucode-settings.json: not found at {path} (ug/unity-gateway CLI not run on this "
                     f"box, or not installed)", cmd="halo init --preset work")
    parsed = load_ucode_settings(path)
    if parsed is None:
        return _fix(f"{WARN} ucode-settings.json: found at {path} but no recognizable gateway URL/token in it",
                     see="docs/harness/INSTALL.md (ug/unity-gateway compatibility)")
    return f"{OK} ucode-settings.json: found and parsed ({path}) -> host {parsed.host}"


def _work_check_vpn_reachability(host: Optional[str]) -> str:
    """A real TCP+TLS connect attempt (no data sent) to the resolved
    Databricks host -- 1.0.1 hotfix 2: now literally goes THROUGH
    `providers.http.open_upstream` (rather than a hand-rolled, unbounded
    `socket.create_connection`) so a WARN here means a live request would
    fail the identical way, AND so a hung/black-holed DNS lookup is capped
    at `open_upstream`'s own default connect timeout (<=8s) instead of
    whatever the OS resolver's own retry policy would otherwise take
    (verified: ~64s against an unresolvable `*.cloud.databricks.com` host)."""
    if not host:
        return _fix(f"{MISSING} Databricks host: not configured (see the Databricks line above) -- nothing "
                     f"to reach", cmd="halo init --preset work")
    import urllib.parse
    from halo_harness.providers.http import UpstreamConnectError, open_upstream
    parsed = urllib.parse.urlparse(host if "://" in host else f"https://{host}")
    hostname = parsed.hostname or host
    port = parsed.port or 443
    try:
        conn = open_upstream(hostname, port, True)
        conn.close()
        return f"{OK} VPN/reachability: connected to {hostname}:{port}"
    except UpstreamConnectError as e:
        return _fix(f"{MISSING} VPN/reachability: {e}",
                     cmd="connect to the VPN (Databricks is whitelisted), then re-run `halo doctor --work`")
    except Exception as e:
        return _fix(f"{MISSING} VPN/reachability: could not reach {hostname}:{port} ({type(e).__name__}: {e})",
                     cmd="connect to the VPN (Databricks is whitelisted), then re-run `halo doctor --work`")


_IP_ACCESS_LIST_HINTS = (
    "ip access", "ip_access", "not permitted to access", "access is not allowed",
    "ip is not allowed", "blocked by ip", "ip allowlist", "ip acl", "ip-based",
)


def _classify_databricks_probe(status: int, raw: bytes) -> "tuple[str, str]":
    """H14 scope E: `(kind, detail)` for a GET /api/2.0/serving-endpoints
    response -- `kind` is "ok"/"warn"/"missing" (this module's own
    vocabulary). 401 = bad token; 403 carrying Databricks' own IP-access-
    list wording = "connect to the VPN" (`missing`, since nothing further
    can be checked from off the VPN); 403 without that wording = the token
    itself lacks list permission, but inference may still work (`warn`,
    not fatal); 404 = wrong path (the derived workspace root is probably
    incorrect)."""
    if status == 200:
        return "ok", "200"
    text = raw.decode("utf-8", "replace").lower() if raw else ""
    if status == 401:
        return "warn", "401 (bad token)"
    if status == 403:
        if any(h in text for h in _IP_ACCESS_LIST_HINTS):
            return "missing", "403 (IP access list -- connect to the VPN, Databricks is whitelisted there)"
        return "warn", "403 (token lacks permission to list endpoints -- inference may still work)"
    if status == 404:
        return "missing", "404 (wrong path -- the derived workspace root may be incorrect)"
    return "warn", f"HTTP {status}"


def _work_check_token_validity(host: Optional[str], token: Optional[str]) -> str:
    """GET /api/2.0/serving-endpoints -- 200 means the token can list
    endpoints; H14 scope E distinguishes 401 (bad token) / 403 with the IP
    access-list wording (connect to the VPN) / 403 without it (token lacks
    list permission, inference may still work) / a wrong-path 404, instead
    of lumping every non-200 into one generic "may only run inference"
    line. A host with no token yet still probes (scope E: "the
    reachability probe still runs without a token, a 401 proves
    reachability") rather than skipping the network call outright."""
    if not host:
        return _fix(f"{MISSING} Token validity: not configured -- nothing to check",
                     cmd="halo init --preset work")
    from halo_harness.providers.config import derive_workspace_root
    from halo_harness.providers.databricks import probe_databricks_status
    root = derive_workspace_root(host)
    if not token:
        try:
            status, raw = probe_databricks_status(root, "")
        except Exception as e:
            return _fix(f"{MISSING} Token validity: host configured, token missing, and the reachability "
                         f"probe failed ({type(e).__name__}: {e})", cmd="halo init --preset work")
        _kind, detail = _classify_databricks_probe(status, raw)
        return _fix(f"{MISSING} Token validity: host configured, token missing (probe reached the host: "
                     f"{detail})", cmd="halo init --preset work")
    try:
        status, raw = probe_databricks_status(root, token)
    except Exception as e:
        return _fix(f"{MISSING} Token validity: connection failed ({type(e).__name__}: {e})",
                     cmd="connect to the VPN, then re-run `halo doctor --work`")
    kind, detail = _classify_databricks_probe(status, raw)
    if kind == "ok":
        try:
            n = len(json.loads(raw.decode("utf-8", "replace")).get("endpoints", []))
        except (ValueError, AttributeError):
            n = "?"
        return f"{OK} Token validity: valid, can list endpoints ({n} found)"
    if kind == "missing":
        return _fix(f"{MISSING} Token validity: {detail}",
                     cmd="connect to the VPN (Databricks is whitelisted), then re-run `halo doctor --work`")
    return _fix(f"{WARN} Token validity: {detail}", see="your Databricks workspace admin (token scope)")


def _work_check_config_summary() -> "list[str]":
    """H14 scope E: "prints the derived root, the gateway path, the header
    NAMES (never values), the default model and effort, the token
    source" -- the exact config a real session on this box would build,
    so a mismatch between "what's configured" and "what halo
    resolved" is visible before ever making a live call. `[]` when
    Databricks doesn't resolve at all (the plain "Databricks config" line
    already covers that case)."""
    _load_env_file_best_effort()
    from halo_harness.providers.config import resolve_databricks, resolve_databricks_source
    try:
        dbx = resolve_databricks()
    except Exception as e:
        return [_fix(f"{WARN} Work config: could not resolve ({type(e).__name__}: {e})",
                      cmd="halo doctor --work")]
    if dbx is None:
        return []
    gateway = dbx.anthropic_gateway or f"{dbx.host}/ai-gateway/anthropic"
    header_names = sorted({"x-databricks-use-coding-agent-mode"} | set(dbx.custom_headers))
    lines = [
        f"{OK} Workspace root: {dbx.host}",
        f"{OK} Anthropic gateway: {gateway}/v1/messages",
        f"{OK} Headers sent (names only): {', '.join(header_names)}",
    ]
    try:
        from halo_harness.config.paths import bridge_home
        from halo_harness.model import resolve_default_model_raw
        from halo_harness.providers.config import load_routes
        routes = load_routes(bridge_home() / "routes.json")
        lines.append(f"{OK} Default model: {resolve_default_model_raw(routes)}")
    except Exception as e:
        lines.append(_fix(f"{WARN} Default model: could not resolve ({type(e).__name__}: {e})",
                           cmd="halo doctor --work"))
    try:
        from halo_harness.config.settings import resolve_settings
        effort = resolve_settings(Path.cwd()).resolved_effort_level()
    except Exception:
        effort = None
    lines.append(f"{OK} Default effort: {effort or 'not set (provider default)'}")
    lines.append(f"{OK} Token source: {resolve_databricks_source() or '?'}")
    return lines


_THINKING_FAMILY_HINTS = ("deepseek", "kimi", "moonshot", "glm", "zhipu", "z-ai")

# H9 whole-tree review finding 33: the SAME header a real databricks route
# always carries (headless.py's `build_session`: `extra_headers =
# {"x-databricks-use-coding-agent-mode": "true"} if model_ref.provider ==
# "databricks" else None`) -- the probe below used to send NONE at all, so
# its answer to "does Databricks forward replayed reasoning_content" could
# legitimately differ from what a real session actually experiences.
_DATABRICKS_PRODUCTION_HEADERS = {"x-databricks-use-coding-agent-mode": "true"}


def _configured_databricks_thinking_model(state_dir) -> Optional[str]:
    """H9 whole-tree review finding 33: INSTALL.md's own doctor --work
    section promises question 2 is answered "for the configured model" --
    the probe used to always pick the FIRST DeepSeek/Kimi/GLM-family
    endpoint in the live catalog instead, regardless of what the user
    actually has configured (`routes.json`'s "default", `HALO_MODEL`, or
    this harness's own built-in default). Resolves that chain the exact
    same way `headless.build_session` does; returns the bare model id only
    when it's BOTH a databricks ref AND thinking-family (the only shape
    this specific probe -- reasoning_content replay -- can meaningfully
    test at all), else None (the caller falls back to the catalog's first
    match, same as before, but now honestly labelled as a fallback)."""
    try:
        from halo_harness.model import parse_model_ref, resolve_default_model_raw
        from halo_harness.providers.config import load_routes
        routes = load_routes(Path(state_dir) / "routes.json")
        model_raw = resolve_default_model_raw(routes)
        ref = parse_model_ref(model_raw, routes)
    except Exception:
        return None
    if ref.provider != "databricks":
        return None
    if not any(h in ref.model.lower() for h in _THINKING_FAMILY_HINTS):
        return None
    return ref.model


def _work_check_reasoning_replay_after_tool_call(host: Optional[str], token: Optional[str], state_dir) -> list:
    """Plan open question 1: "whether Databricks forwards replayed
    `reasoning_content` into DeepSeek/Kimi/GLM after a tool call" -- and
    plan open question 2: "whether the invocations-without-model vs
    mlflow-with-system.ai split holds for every catalogue model", answered
    TOGETHER by ONE real, cheap (max_tokens=64) request against the
    CONFIGURED model when it's a DeepSeek/Kimi/GLM-family databricks ref
    (finding 33 -- INSTALL.md's own promise), else the first such endpoint
    the live catalog has (still needs SOME thinking-family model to
    exercise reasoning_content replay at all): a synthetic prior assistant
    turn carries `reasoning_content` AND a tool call, a synthetic tool
    result follows it, and the model is asked one more question -- the
    HTTP status this gets back (200 vs a 400 naming reasoning_content)
    directly answers question 1, and whichever candidate route actually
    served it (recorded in routes-cache.json by the SAME call, via the
    ordinary `call_databricks_chat` path) directly answers question 2.
    Never raises -- any failure becomes a WARN/MISSING line naming what
    happened, since this is exploratory (the plan's own "unknowns"), not a
    pass/fail gate on the rest of `doctor --work`."""
    lines = ["Open questions: (1) does Databricks forward replayed reasoning_content into "
             "DeepSeek/Kimi/GLM after a tool call? (2) does the invocations-vs-mlflow route "
             "split hold for the CONFIGURED model?"]
    if not host or not token:
        lines.append("  " + _fix(f"{MISSING} cannot probe -- Databricks not configured",
                                  cmd="halo init --preset work"))
        return lines
    try:
        from halo_harness.providers.databricks import probe_databricks_endpoints_full
        from halo_harness.providers.config import derive_workspace_root
        status, entries = probe_databricks_endpoints_full(derive_workspace_root(host), token)
    except Exception as e:
        lines.append("  " + _fix(f"{MISSING} cannot probe -- connection failed ({type(e).__name__}: {e})",
                                  cmd="connect to the VPN, then re-run `halo doctor --work`"))
        return lines
    if status != 200:
        lines.append("  " + _fix(f"{MISSING} cannot probe -- HTTP {status} listing endpoints",
                                  cmd="halo doctor --work"))
        return lines
    configured = _configured_databricks_thinking_model(state_dir)
    if configured:
        candidate = configured
        lines.append(f"  (probing the CONFIGURED model, {candidate!r})")
    else:
        candidate = next((e.get("name") for e in entries
                           if isinstance(e, dict) and any(h in (e.get("name") or "").lower()
                                                            for h in _THINKING_FAMILY_HINTS)), None)
        if candidate:
            lines.append(f"  (no thinking-family model configured -- probing the catalog's first "
                         f"match instead, {candidate!r})")
    if not candidate:
        lines.append(f"  {WARN} no DeepSeek/Kimi/GLM-family endpoint in the catalog to probe")
        return lines

    try:
        from halo_harness.providers.config import derive_workspace_root
        from halo_harness.providers.databricks import dbx_cache_get_route, databricks_route_candidates
        from halo_harness.providers.http import call_databricks_chat
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
        result = call_databricks_chat(root, token, body, dict(_DATABRICKS_PRODUCTION_HEADERS), state_dir, candidate)
        raw = b""
        try:
            if result.resp is not None:
                raw = result.resp.read()
        except Exception:
            pass
        finally:
            # NEW (H9 post-acceptance): a one-shot diagnostic call --
            # `call_databricks_chat` hands back a live `UpstreamResult`
            # (the SAME wrapper the streaming model-call path uses and
            # closes itself once its stream ends), but this caller never
            # streams from it, so nothing else was ever going to close it.
            try:
                if result.conn is not None:
                    result.conn.close()
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


def _work_check_entries() -> "list[tuple[str, str]]":
    """The `--work` counterpart of `_check_entries` -- one id-tagged table
    shared by `run_work_checks` and `run_work_checks_structured`/`doctor
    --work --json`."""
    from halo_harness.config.paths import bridge_home
    host, token = _dbx_probe_target()
    if host and token:
        databricks_config_line = f"{OK} Databricks config: host {host}"
    elif host:
        databricks_config_line = _fix(f"{MISSING} Databricks config: host configured, token missing ({host})",
                                       cmd="halo init --preset work")
    else:
        databricks_config_line = _fix(f"{MISSING} Databricks config: not configured",
                                       cmd="halo init --preset work")
    entries = [
        ("databricks_config", databricks_config_line),
        ("ucode_settings", _check_ucode_settings()),
        ("vpn_reachability", _work_check_vpn_reachability(host)),
        ("token_validity", _work_check_token_validity(host, token)),
    ]
    config_ids = ("work_root", "work_gateway", "work_headers", "work_default_model", "work_effort", "work_token_source")
    entries.extend(zip(config_ids, _work_check_config_summary(), strict=False))
    probe_lines = _work_check_reasoning_replay_after_tool_call(host, token, bridge_home())
    entries.extend((f"reasoning_replay_probe_{i}", line) for i, line in enumerate(probe_lines))
    return entries


def run_work_checks() -> "tuple[list, bool]":
    """H8 scope F: `halo doctor --work` -- VPN reachability, token
    validity, ucode-settings.json, and the plan's own two open questions as
    runnable probes. Never raises; every check degrades to a clear WARN/
    MISSING line with the VPN hint instead of crashing when the work box
    genuinely isn't reachable from here (expected when run outside the VPN,
    e.g. this exact build/test box)."""
    lines = [line for _cid, line in _work_check_entries()]
    ok = not any(line.strip().startswith(MISSING) for line in lines)
    return lines, ok


def run_work_checks_structured() -> "tuple[list, bool]":
    """H12 Part B: `doctor --work --json`'s own payload -- see
    `run_checks_structured`'s docstring for the shape; `ok` matches
    `run_work_checks`' own definition exactly."""
    checks = []
    for cid, line in _work_check_entries():
        parsed = _parse_check_line(line)
        parsed["id"] = cid
        checks.append(parsed)
    ok = not any(c["status"] == "missing" for c in checks)
    return checks, ok


def _check_entries(cwd: Optional[Path] = None, settings_flag: Optional[str] = None) -> "list[tuple[str, str]]":
    """`[(id, rendered_line), ...]` -- the ONE table both `run_checks`
    (the plain `lines`/`ok` pair every existing caller/test already uses)
    and `run_checks_structured`/`doctor --json` (H12 Part B: "for init to
    reuse") build from, so the two can never drift apart. A check that
    returns `None` (the new PART B checks that only apply sometimes --
    tmux mouse mode off-tmux, `~/.local/bin` on win32) is skipped entirely
    rather than appearing as an empty/placeholder entry."""
    entries: "list[tuple[str, Optional[str]]]" = [("python", _check_python())]
    claude_ids = ("claude_dir", "claude_settings_json", "claude_dot_json")
    entries.extend(zip(claude_ids, _check_claude_layout(), strict=False))
    entries.append(("state_dir_migration", _check_state_dir_migration()))
    entries.append(("env_file", _check_env_file()))
    entries.append(("openrouter", _check_openrouter()))
    entries.append(("databricks", _check_databricks()))
    entries.append(("claude_subscription", _check_claude_subscription()))
    entries.append(("chrome", _check_chrome()))
    entries.append(("playwright", _check_playwright()))
    entries.append(("ripgrep", _check_ripgrep()))
    entries.append(("editor", _check_editor()))
    entries.append(("terminal_program", _check_terminal_program()))
    entries.append(("shell", _check_shell()))
    entries.append(("plugins", _check_plugins()))
    entries.append(("platform", _check_platform()))
    entries.append(("drain_tick_rate", _check_drain_tick_rate()))
    entries.append(("local_bin_on_path", _check_local_bin_on_path()))
    entries.append(("tmux_mouse", _check_tmux_mouse()))
    catalog_ids = ("catalog_models_json", "catalog_dbx_endpoints", "catalog_models_dev")
    entries.extend(zip(catalog_ids, _check_catalog_ages(), strict=False))
    telemetry_ids = ("sessions", "improve")
    entries.extend(zip(telemetry_ids, _check_telemetry_and_improve(), strict=False))
    # U5 leftover / H8 cheap must-do: tui/clipboard.py's own
    # clipboard_doctor_line() was written ready-to-call but never actually
    # wired into a real doctor run. H12 Part B: that function's own WARN
    # (no xclip/wl-copy/xsel on PATH -- a bare Linux box with neither, e.g.
    # a fresh WSL install) gets the fix suffix HERE rather than inside
    # tui/clipboard.py itself, since `_fix`/the WARN/MISSING vocabulary is
    # this module's own convention, not that one's.
    from halo_harness.tui.clipboard import clipboard_doctor_line
    entries.append(("clipboard", _fix(clipboard_doctor_line(), cmd="sudo apt install xclip")))
    entries.append(("mcp_servers", _check_mcp_servers(cwd)))
    entries.append(("test_leftovers", _check_test_leftovers()))
    entries.append(("mcp_connectors", _check_mcp_connectors()))
    entries.extend(_check_ollama_hosts())
    entries.append(("default_model", _check_default_model()))
    entries.append(("permission_mode", _check_permission_mode()))
    entries.append(("providers_enabled", _check_providers_enabled(cwd, settings_flag)))
    entries.append(("install", _check_install()))
    entries.append(("command_on_path", check_command_on_path()))
    entries.append(("old_rolo_claude_on_path", _check_old_rolo_claude_leftover()))
    return [(cid, line) for cid, line in entries if line is not None]


def run_checks(cwd: Optional[Path] = None, settings_flag: Optional[str] = None) -> "tuple[list, bool]":
    """Returns (lines, ok) -- `ok` is True iff nothing came back MISSING
    (a WARN is informational, e.g. "no Databricks configured", and never
    fails doctor as a whole)."""
    lines = [line for _cid, line in _check_entries(cwd, settings_flag)]
    ok = not any(line.startswith(MISSING) for line in lines)
    return lines, ok


def run_checks_structured(cwd: Optional[Path] = None, settings_flag: Optional[str] = None) -> "tuple[list, bool]":
    """H12 Part B: `doctor --json`'s own payload, and what `halo
    init` consumes to build its own Summary step -- `[{"id", "status",
    "message", "fix", "see"}, ...]`, the SAME checks/order/wording
    `run_checks` renders as plain text, just structured instead of
    re-parsed from it. `ok` matches `run_checks`' own definition exactly
    (True iff nothing is "missing")."""
    checks = []
    for cid, line in _check_entries(cwd, settings_flag):
        parsed = _parse_check_line(line)
        parsed["id"] = cid
        checks.append(parsed)
    ok = not any(c["status"] == "missing" for c in checks)
    return checks, ok


def cmd_doctor(argv: list) -> int:
    parser = argparse.ArgumentParser(prog="halo doctor", add_help=True,
                                      description="Check the health of your halo installation.")
    parser.add_argument("--work", action="store_true",
                         help="Run the Databricks work-box preset (VPN reachability, token validity, "
                              "route-split/reasoning-replay probes) instead of the general checks")
    parser.add_argument("--json", action="store_true",
                         help="Machine-readable output: a JSON list of {id, status, message, fix, see}")
    parser.add_argument("--probe-all", action="store_true",
                         help="With --work: the full work matrix -- one short pong per chat-shaped "
                              "Databricks endpoint on its chosen path")
    parser.add_argument("--both", action="store_true",
                         help="With --probe-all: also probe Claude/GLM/Kimi endpoints via the anthropic gateway")
    parser.add_argument("--tools", action="store_true",
                         help="With --probe-all: also check one Read tool-call per endpoint")
    parser.add_argument("--only", default=None, metavar="GLOB",
                         help="With --probe-all: only endpoints matching this glob")
    # Halo 2.0.3 round 5d (brief item 3): the 60-second local-model
    # acceptance check -- its own preset, same shape as --work just above.
    parser.add_argument("--local", action="store_true",
                         help="Run the 60-second local-model acceptance check (load, tool call, structured "
                             "output, compaction summary against a fixture transcript) instead of the "
                             "general checks")
    parser.add_argument("--model", default=None, metavar="REF",
                         help="With --local: the ol: model to check (default: the configured default "
                             "model if it is ol:, else the first model in the default Ollama host's catalog)")
    # Findings 22/23 (2.0.1): threaded into listing_effective_env (via
    # run_checks/_check_providers_enabled) so the provider-enablement line
    # never disagrees with what a real session launched against this same
    # --cwd/--settings would resolve -- matches halo providers' own flags.
    parser.add_argument("--cwd", default=None, metavar="DIR",
                         help="Resolve settings/credentials as if run from DIR")
    parser.add_argument("--settings", default=None, metavar="JSON_OR_PATH",
                         help="Extra settings (inline JSON or a file path), same as a session's --settings")
    args = parser.parse_args(argv)
    doctor_cwd = Path(args.cwd).resolve() if args.cwd else None
    settings_flag = args.settings
    # H15 item 21.4: one-time migration -- a box with credentials from
    # before provider enablement existed gets exactly those enabled. A
    # no-op (returns None) once a `providers` block already exists,
    # however it got there. The note is printed only OUTSIDE --json (a
    # machine reader expects stdout to be ONE parseable JSON value, never
    # a plain-text line ahead of it).
    from halo_harness.providers.enablement import ensure_providers_migrated
    migration_note = ensure_providers_migrated()
    if migration_note and not args.json:
        print(migration_note)
    if args.local:
        from halo_harness.config.paths import bridge_home
        from halo_harness.doctor_local import format_acceptance_lines, run_local_acceptance_check
        print(f"halo doctor --local{f' --model {args.model}' if args.model else ''}")
        print("  Loads the model, makes one real tool call, one structured-output call, and one "
              "compaction-style summary -- real requests against the live host this takes a moment.")
        steps, ok = run_local_acceptance_check(args.model, state_dir=bridge_home())
        if args.json:
            print(json.dumps(steps, indent=2))
        else:
            for line in format_acceptance_lines(steps):
                print(f"  {line}")
        return 0 if ok else 1
    if args.work and args.probe_all:
        from halo_harness.work_matrix import format_table, run_work_matrix
        print("halo doctor --work --probe-all")
        print("  Sends one short pong (and a tool call with --tools) to each endpoint below -- "
              "this spends real tokens/DBUs against your Databricks workspace.")
        rows, report_path = run_work_matrix(only=args.only, both=args.both, tools=args.tools)
        if not rows:
            print("  No chat-shaped endpoints to probe (Databricks not configured, or the catalog is empty -- "
                  "run `halo models --refresh` first).")
            return 1
        print(format_table(rows, tools=args.tools))
        print(f"\n  Report written to {report_path} (endpoint names only -- no host, no token).")
        return 0 if all(r.status == "200" for r in rows) else 1
    if args.work:
        if args.json:
            checks, ok = run_work_checks_structured()
            print(json.dumps(checks, indent=2))
            return 0 if ok else 1
        lines, ok = run_work_checks()
        print("halo doctor --work")
        for line in lines:
            print(f"  {line}")
        return 0 if ok else 1
    if args.json:
        checks, ok = run_checks_structured(doctor_cwd, settings_flag)
        print(json.dumps(checks, indent=2))
        return 0 if ok else 1
    lines, ok = run_checks(doctor_cwd, settings_flag)
    print("halo doctor")
    for line in lines:
        print(f"  {line}")
    return 0 if ok else 1
