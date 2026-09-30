"""rolo_claude.init_cli -- `rolo-claude init` (H12 brief Part A /
RECOMMENDATIONS.md P0 #1): the whole first run in one command. Interactive
by default (plain prompts via `rich`, no Textual); `--yes` accepts every
default without prompting; `--preset ... --yes` is fully non-interactive
whenever the needed value (a key/token) is already discoverable, and never
BLOCKS even when it isn't (a piped/non-tty stdin is read for one line
instead of hanging on a real terminal prompt -- see `_prompt_secret`).

Seven steps, each printed as it runs and each idempotent: re-running shows
the current state and changes nothing already correctly configured (must-do
for the Kali VM, which already has its key/rg/PATH set up). Never writes
`~/.claude.json` or `~/.claude/settings.json` (this harness only ever READS
those); never prints a key or token anywhere, success or failure.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import sys
from pathlib import Path
from typing import Optional

from rich.console import Console
from rich.prompt import Confirm, Prompt

_PRESET_DEFAULT_MODEL = {
    "home": "or:deepseek/deepseek-v4.1-flash",
    "work": "dbx:databricks-deepseek-v4-1-flash",
    "claude": "cc:sonnet",
}
_WORK_ALTERNATIVES = "dbx:databricks-kimi-k3, dbx:databricks-glm-5-3"


def _non_interactive(args) -> bool:
    """`--yes`, OR stdin isn't a real terminal at all (a test harness, a
    script, a CI runner) -- the stronger of the two the brief names
    (`--preset ... --yes`) plus the standard "no tty means don't block"
    convention every other CLI here already follows (cli.py's own -p stdin
    handling)."""
    return bool(args.yes) or not sys.stdin.isatty()


def _prompt_secret(label: str) -> str:
    """Hidden input via `getpass` on a real terminal; a single line read
    directly off stdin otherwise -- `getpass.getpass()` targets the
    process's CONTROLLING terminal (`/dev/tty`), not whatever this
    process's stdin was redirected to, so a piped/redirected run (every
    test, and any real non-interactive automation) must read stdin
    directly instead or the piped value would never be seen at all."""
    if sys.stdin.isatty():
        import getpass
        try:
            return getpass.getpass(f"{label}: ")
        except (EOFError, KeyboardInterrupt):
            return ""
    line = sys.stdin.readline()
    return (line or "").rstrip("\r\n")


def _prompt_plain(label: str) -> str:
    """Same shape as `_prompt_secret` for a NON-secret value (a host name)
    -- echoed normally on a real terminal, one stdin line otherwise."""
    if sys.stdin.isatty():
        try:
            return input(f"{label}: ").strip()
        except EOFError:
            return ""
    line = sys.stdin.readline()
    return (line or "").strip()


def _confirm(args, console: Console, question: str, *, default: bool) -> bool:
    if _non_interactive(args):
        return default
    try:
        return Confirm.ask(question, default=default)
    except (EOFError, KeyboardInterrupt):
        return default


def _env_file_path() -> Path:
    """The SAME env file every other part of the harness reads (`doctor.py`/
    `headless.py`/`catalog_cli.py` all resolve this identically):
    `BRIDGE_ENV_FILE`, else `~/.config/vibes-hacker/env`."""
    from rolo_claude.config.paths import home
    return Path(os.environ.get("BRIDGE_ENV_FILE", str(home() / ".config" / "vibes-hacker" / "env")))


def _write_env_var(path: Path, key: str, value: str) -> None:
    """Set `KEY=value` in the env file at `path`: creates the directory
    (0700) and file (0600) on POSIX, preserves every other line, replaces
    an existing `KEY=`/`export KEY=` line in place, else appends one.
    Windows has no matching permission-bit concept -- the file is still
    written, just without the chmod calls (a harmless no-op there, not an
    error)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    prefix_re = re.compile(rf"^(export\s+)?{re.escape(key)}\s*=")
    new_lines, replaced = [], False
    for line in lines:
        if prefix_re.match(line.strip()):
            new_lines.append(f"{key}={value}")
            replaced = True
        else:
            new_lines.append(line)
    if not replaced:
        new_lines.append(f"{key}={value}")
    path.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
    if os.name != "nt":
        path.parent.chmod(0o700)
        path.chmod(0o600)


# ---------------------------------------------------------------------------
# Step 1: preset
# ---------------------------------------------------------------------------

def _detect_default_preset(claude_available: bool) -> str:
    from rolo_claude.providers.config import resolve_databricks, resolve_openrouter
    if resolve_openrouter() is not None:
        return "home"
    if resolve_databricks() is not None:
        return "work"
    if claude_available:
        return "claude"
    return "home"


def _claude_login_available() -> bool:
    from rolo_claude.providers.cc_models import SUBSCRIPTION_AUTH_METHODS, claude_auth_status
    status = claude_auth_status()
    return bool(status and status.logged_in and status.auth_method in SUBSCRIPTION_AUTH_METHODS)


def _step_preset(args, console: Console) -> Optional[str]:
    """Returns the chosen preset name, or None when `--preset claude` was
    requested without an actual claude.ai login (a usage/config error --
    the caller exits 2). `claude_auth_status()` (a real `claude auth
    status` subprocess when no test seam is set) is only ever invoked when
    it's actually relevant: no `--preset` at all (to decide whether to
    offer/auto-detect it), or `--preset claude` explicitly -- never for an
    explicit `--preset home`/`--preset work`."""
    if args.preset in ("home", "work"):
        console.print(f"1. Preset: {args.preset} (from --preset)")
        return args.preset

    claude_available = _claude_login_available()
    if args.preset == "claude":
        if not claude_available:
            console.print("[red]--preset claude needs a claude.ai login -- run `claude` once to log in, "
                           "or pick --preset home/work instead.[/red]")
            return None
        console.print("1. Preset: claude (from --preset)")
        return "claude"

    default_preset = _detect_default_preset(claude_available)
    if _non_interactive(args):
        console.print(f"1. Preset: {default_preset} (detected; pass --preset to choose explicitly)")
        return default_preset

    console.print("1. Preset:")
    console.print("   home   -- OpenRouter, or:deepseek/deepseek-v4.1-flash")
    console.print(f"   work   -- Databricks, {_PRESET_DEFAULT_MODEL['work']} "
                   f"(alternatives: {_WORK_ALTERNATIVES})")
    options = ["home", "work"]
    if claude_available:
        console.print("   claude -- your Claude subscription (claude.ai login), cc:sonnet")
        options.append("claude")
    try:
        choice = Prompt.ask("   choose", choices=options, default=default_preset)
    except (EOFError, KeyboardInterrupt):
        choice = default_preset
    return choice


# ---------------------------------------------------------------------------
# Step 2: credentials
# ---------------------------------------------------------------------------

def _ensure_openrouter_key(args, console: Console) -> Optional[Path]:
    from rolo_claude.providers.config import redact, resolve_openrouter
    existing = resolve_openrouter()
    if existing is not None:
        console.print(f"   [OK] OpenRouter key: already configured ({redact(existing.api_key)}) -- not changed.")
        return None
    if args.yes and sys.stdin.isatty():
        console.print("   [WARN] OpenRouter key not found, and --yes skips the prompt -- "
                       "set OPENROUTER_API_KEY (or re-run `rolo-claude init` without --yes).")
        return None
    console.print("   OpenRouter key not found (get one at https://openrouter.ai/keys).")
    value = _prompt_secret("   OPENROUTER_API_KEY")
    if not value:
        console.print("   [WARN] no key entered -- OpenRouter will not be configured yet.")
        return None
    path = _env_file_path()
    _write_env_var(path, "OPENROUTER_API_KEY", value)
    os.environ["OPENROUTER_API_KEY"] = value
    console.print(f"   [OK] wrote OPENROUTER_API_KEY to {path}")
    return path


def _known_databricks_host() -> Optional[str]:
    """H14 scope E/I: host-only discovery -- Claude Code's own settings env
    may set ANTHROPIC_BASE_URL/DATABRICKS_HOST without a usable token yet
    (a fresh box, or the work settings.json template before the token line
    is filled in); `resolve_databricks()` itself returns None in that case
    (it requires both), so this mirrors just the host half of its chain.

    Gated on `databricks_work_signal_present` (ANTHROPIC_MODEL/ANTHROPIC_
    DEFAULT_*_MODEL, the SAME signal scope C uses) -- a bare, incidental
    DATABRICKS_HOST/ANTHROPIC_BASE_URL with none of those set is NOT treated
    as "already known" here. Without this gate, a box that happens to have
    an unrelated DATABRICKS_HOST exported (a different Databricks tool,
    nothing to do with Claude Code) would silently skip the host prompt on
    a fresh `init --preset work` -- verified live: this exact box's own
    ambient DATABRICKS_HOST, with no ANTHROPIC_MODEL alongside it, is NOT
    Claude Code's work settings and must never be adopted as one."""
    from rolo_claude.providers.config import (
        databricks_work_signal_present, derive_workspace_root, load_settings_env_chain, looks_like_databricks_host,
    )
    settings_env = load_settings_env_chain(Path.cwd())
    for source in (os.environ, settings_env):
        if not databricks_work_signal_present(source):
            continue
        anth_host = source.get("ANTHROPIC_BASE_URL")
        if anth_host and looks_like_databricks_host(anth_host):
            return derive_workspace_root(anth_host)
        dbx_host = source.get("DATABRICKS_HOST")
        if dbx_host:
            return derive_workspace_root(dbx_host)
    return None


def _load_team_config(args, console: Console) -> Optional[dict]:
    from rolo_claude.team_config import load_team_config
    cfg, warnings = load_team_config(Path.cwd(), team_flag=getattr(args, "team", None))
    for w in warnings:
        console.print(f"   [WARN] {w}")
    return cfg


def _ensure_databricks_creds(args, console: Console) -> Optional[Path]:
    from rolo_claude.providers.config import databricks_work_env_active, redact, resolve_databricks
    existing = resolve_databricks()
    if existing is not None:
        source = "Claude Code's settings" if databricks_work_env_active() else "existing config"
        console.print(f"   [OK] Databricks: already configured via {source} (host={existing.host}, "
                       f"token={redact(existing.token)}) -- not changed.")
        return None

    # H14 scope I: "the team just inserts their databricks token and
    # they're off" -- a host already known (Claude Code's own settings env,
    # or a shared team.json) means ONLY the token is asked for.
    team_cfg = _load_team_config(args, console)
    host = _known_databricks_host() or (team_cfg or {}).get("host")
    if host:
        console.print(f"   [OK] Databricks host configured: {host} -- only the token is needed.")
        if args.yes and sys.stdin.isatty():
            console.print("   [WARN] token not found, and --yes skips the prompt -- "
                           "set DATABRICKS_TOKEN, or re-run without --yes.")
            return None
        token = _prompt_secret("   DATABRICKS_TOKEN (hidden)")
        if not token:
            console.print("   [WARN] no token entered -- Databricks will not be configured yet.")
            return None
        path = _env_file_path()
        _write_env_var(path, "DATABRICKS_HOST", host)
        _write_env_var(path, "DATABRICKS_TOKEN", token)
        os.environ["DATABRICKS_HOST"] = host
        os.environ["DATABRICKS_TOKEN"] = token
        console.print(f"   [OK] wrote DATABRICKS_HOST/DATABRICKS_TOKEN to {path}")
        if team_cfg and team_cfg.get("gateway_preference"):
            from rolo_claude.team_config import apply_gateway_preference
            apply_gateway_preference(team_cfg["gateway_preference"])
        if team_cfg and team_cfg.get("roles"):
            # V2c (H15): the SAME "seed config.json, never clobber a local
            # override" idiom as gateway_preference above, for team.json's
            # own shared `roles` table.
            from rolo_claude.roles import apply_role_preference
            apply_role_preference(team_cfg["roles"])
        return path

    if args.yes and sys.stdin.isatty():
        console.print("   [WARN] Databricks host/token not found, and --yes skips the prompt -- "
                       "set DATABRICKS_HOST/DATABRICKS_TOKEN (or ~/.databrickscfg), or re-run without --yes.")
        return None
    console.print("   Databricks host/token not found (checked env, team.json, ~/.databrickscfg, "
                   "ucode-settings.json).")
    host = _prompt_plain("   DATABRICKS_HOST (e.g. https://your-workspace.cloud.databricks.com)")
    if not host:
        console.print("   [WARN] no host entered -- Databricks will not be configured yet.")
        return None
    token = _prompt_secret("   DATABRICKS_TOKEN")
    if not token:
        console.print("   [WARN] no token entered -- Databricks will not be configured yet.")
        return None
    path = _env_file_path()
    _write_env_var(path, "DATABRICKS_HOST", host)
    _write_env_var(path, "DATABRICKS_TOKEN", token)
    os.environ["DATABRICKS_HOST"] = host
    os.environ["DATABRICKS_TOKEN"] = token
    console.print(f"   [OK] wrote DATABRICKS_HOST/DATABRICKS_TOKEN to {path}")
    return path


def _step_credentials(preset: str, args, console: Console) -> list:
    console.print("2. Credentials:")
    if preset == "home":
        path = _ensure_openrouter_key(args, console)
    elif preset == "work":
        path = _ensure_databricks_creds(args, console)
    else:
        console.print("   claude: nothing stored -- your existing `claude` login is used as-is.")
        path = None
    return [str(path)] if path else []


# ---------------------------------------------------------------------------
# Step 3: default model
# ---------------------------------------------------------------------------

def _step_default_model(preset: str, args, console: Console) -> "tuple[str, Optional[Path]]":
    from rolo_claude.config.paths import bridge_home
    from rolo_claude.theme import get_config_value, set_config_value
    team_default = None
    if preset == "work" and not args.model:
        from rolo_claude.team_config import load_team_config
        team_cfg, _warnings = load_team_config(Path.cwd(), team_flag=getattr(args, "team", None))
        team_default = (team_cfg or {}).get("default_model")
    chosen = args.model or team_default or _PRESET_DEFAULT_MODEL[preset]
    current = get_config_value("model", default=None)
    if current == chosen:
        console.print(f"3. Default model: {chosen} (unchanged)")
        return chosen, None
    set_config_value("model", chosen)
    path = bridge_home() / "config.json"
    console.print(f"3. Default model: {chosen} -- wrote {path}")
    return chosen, path


# ---------------------------------------------------------------------------
# Step 4: checks (doctor + models --refresh)
# ---------------------------------------------------------------------------

def _print_work_catalog_summary(console: Console, model_raw: str) -> None:
    """H14 scope I: "prints the count of models available and the default"
    -- read straight back from the JUST-refreshed dbx-endpoints.json, so
    the number always matches what `/model`/`models --refresh` would show,
    never a separately-maintained count."""
    from rolo_claude.config.paths import bridge_home
    from rolo_claude.providers.databricks import load_dbx_endpoints_json
    endpoints = load_dbx_endpoints_json(bridge_home())
    chat = sum(1 for e in endpoints.values() if isinstance(e, dict) and e.get("api_types"))
    console.print(f"   {len(endpoints)} Databricks endpoint(s) cached ({chat} chat-capable) -- default: {model_raw}")


def _step_checks(args, console: Console, cwd: Path, *, preset: str = "", model_raw: str = "") -> "tuple[list, bool]":
    console.print("4. Checks:")
    from rolo_claude.doctor import run_checks
    lines, ok = run_checks(cwd=cwd)
    for line in lines:
        console.print(f"   {line}")
    if args.no_live:
        console.print("   (catalog refresh skipped: --no-live)")
    else:
        console.print("   refreshing model catalogs (models --refresh)...")
        from rolo_claude.catalog_cli import cmd_models
        try:
            cmd_models(["--refresh"])
            if preset == "work":
                _print_work_catalog_summary(console, model_raw)
        except Exception as e:
            console.print(f"   [WARN] catalog refresh failed: {type(e).__name__}: {e}")
    return lines, ok


# ---------------------------------------------------------------------------
# Step 5: live pong
# ---------------------------------------------------------------------------

def _run_live_pong(model_raw: str, cwd: Path) -> "tuple[bool, str]":
    """Builds a real (bare) session for `model_raw` and drives one turn
    in-process (never shells out to `rolo-claude` itself), capturing the
    JSON result into an in-memory stream rather than real stdout -- the
    printed summary line is built from THAT (model/provider/reply/cost),
    so a key/token is never anywhere near what this function prints."""
    import io
    import json as json_mod
    try:
        from rolo_claude import headless
        from rolo_claude.output import PrintModeSink
    except Exception as e:
        return False, f"could not load the agent loop: {type(e).__name__}: {e}"
    try:
        build = headless.build_session(cwd=cwd, model_ref_raw=model_raw, bare=True, print_mode=True, max_turns=1)
    except Exception as e:
        return False, f"could not start a session for {model_raw!r}: {type(e).__name__}: {e}"
    session, mcp_manager = build.session, build.mcp_manager
    stream = io.StringIO()
    sink = PrintModeSink(output_format="json", session_id=build.session_log.session_id,
                          model=build.model_ref.raw, stream=stream, permission_denials=session.permission_denials)
    try:
        exit_code = sink.consume(session.turn("Reply with the single word pong"))
    except Exception as e:
        return False, f"live call failed: {type(e).__name__}: {e}"
    finally:
        try:
            session._fire_session_end("quit")
        except Exception:
            pass
        try:
            session.job_registry.kill_all()
        except Exception:
            pass
        if mcp_manager is not None:
            try:
                mcp_manager.close_all()
            except Exception:
                pass
    try:
        obj = json_mod.loads(stream.getvalue())
    except ValueError:
        return False, "could not parse the live response"
    reply = str(obj.get("result") or "").strip()
    cost = obj.get("total_cost_usd")
    cost_str = f"${cost:.6f}" if isinstance(cost, (int, float)) else "n/a"
    line = f"model={build.model_ref.raw} provider={build.model_ref.provider} reply={reply!r} cost={cost_str}"
    if exit_code != 0 or not reply:
        return False, f"{line} (no valid reply)"
    return True, line


def _step_live_pong(model_raw: str, cwd: Path, console: Console) -> bool:
    console.print(f"5. Live pong ({model_raw}):")
    ok, line = _run_live_pong(model_raw, cwd)
    console.print(f"   {'[OK]' if ok else '[WARN]'} {line}")
    if not ok:
        console.print("   Next: `rolo-claude doctor` explains what's missing.")
    return ok


# ---------------------------------------------------------------------------
# Step 6: Linux fixes / Windows PATH check
# ---------------------------------------------------------------------------

def _fix_ripgrep(args, console: Console) -> Optional[str]:
    existing = shutil.which("rg")
    if existing:
        console.print(f"   [OK] rg (ripgrep): already on PATH ({existing}) -- not changed.")
        return None
    dest = Path.home() / ".local" / "bin"
    if not _confirm(args, console, f"   install a static rg into {dest}?", default=True):
        console.print("   skipped rg install.")
        return None
    from rolo_claude.linux_fixes import install_static_ripgrep
    ok, msg = install_static_ripgrep(dest)
    if ok:
        console.print(f"   [OK] installed rg -> {msg}")
        return msg
    console.print(f"   [WARN] could not install a static rg -- fix: {msg}")
    return None


def _fix_local_bin_path(args, console: Console) -> Optional[str]:
    from rolo_claude.linux_fixes import ensure_local_bin_on_rc, local_bin_on_noninteractive_path, rc_file_for_shell
    if local_bin_on_noninteractive_path():
        console.print("   [OK] ~/.local/bin already on PATH for a non-interactive shell -- not changed.")
        return None
    rc_path = rc_file_for_shell()
    if not _confirm(args, console, f"   add ~/.local/bin to PATH via {rc_path}?", default=True):
        console.print("   skipped the PATH rc-file line.")
        return None
    written, path = ensure_local_bin_on_rc()
    if written:
        console.print(f"   [OK] added the PATH line to {path}")
        return str(path)
    console.print(f"   [OK] {path} already has the PATH line -- not changed.")
    return None


def _check_windows_launcher(console: Console) -> None:
    exe = shutil.which("rolo-claude") or shutil.which("rolo-claude.exe")
    if exe:
        console.print(f"   [OK] rolo-claude launcher on PATH: {exe}")
        return
    console.print("   [WARN] rolo-claude launcher not found on PATH -- add the directory `uv tool install`/"
                   "`pip install --user` put it in (typically %USERPROFILE%\\.local\\bin, or a venv's own "
                   "Scripts\\ dir), or run `python -m rolo_claude` from this checkout instead.")


def _step_linux_fixes(args, console: Console) -> list:
    console.print("6. Windows PATH:" if sys.platform == "win32" else "6. Linux fixes:")
    if args.no_fixes:
        console.print("   skipped (--no-fixes)")
        return []
    if sys.platform == "win32":
        _check_windows_launcher(console)
        return []
    written = []
    rg_path = _fix_ripgrep(args, console)
    if rg_path:
        written.append(rg_path)
    rc_path = _fix_local_bin_path(args, console)
    if rc_path:
        written.append(rc_path)
    if not (os.environ.get("VISUAL") or os.environ.get("EDITOR")):
        console.print("   $VISUAL/$EDITOR is not set -- add e.g. `export EDITOR=nano` to your shell rc "
                       "if you want Ctrl+E (edit the prompt draft) to work.")
    return written


# ---------------------------------------------------------------------------
# Step 7: summary
# ---------------------------------------------------------------------------

_FIX_RE = re.compile(r"-> (?:fix|see): (.+)$")


def _first_fix_hint(lines: list) -> Optional[str]:
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("[WARN]") or stripped.startswith("[MISSING]"):
            m = _FIX_RE.search(stripped)
            if m:
                return m.group(1).strip()
    return None


def _step_summary(console: Console, written: list, pong_ok: bool, doctor_lines: list, *, no_live: bool) -> None:
    console.print("7. Summary:")
    if written:
        for w in written:
            console.print(f"   wrote {w}")
    else:
        console.print("   nothing new written (already configured).")
    if no_live or pong_ok:
        console.print("   Run `rolo-claude` to start.")
        return
    hint = _first_fix_hint(doctor_lines)
    console.print(f"   Next: {hint}" if hint else "   Next: run `rolo-claude doctor` for details.")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="rolo-claude init", add_help=True,
        description="Set up rolo-claude in one command: pick a preset, configure credentials, set a "
                     "default model, run doctor, send a live pong, and offer the Linux setup fixes.",
    )
    parser.add_argument("--preset", choices=["home", "work", "claude"], default=None,
                         help="home=OpenRouter, work=Databricks, claude=your Claude subscription")
    parser.add_argument("--model", default=None, metavar="REF", help="override the preset's own default model")
    parser.add_argument("--yes", action="store_true", help="accept every default without prompting")
    parser.add_argument("--no-live", action="store_true", help="skip the catalog refresh and the live pong")
    parser.add_argument("--no-fixes", action="store_true", help="skip the Linux rg/PATH fixes step")
    parser.add_argument("--team", default=None, metavar="PATH|URL",
                         help="a team.json preset (host/default model/gateway preference/DBU price -- "
                              "never a token); overrides .rolo-claude/team.json / ~/.rolo-claude/team.json")
    return parser


def cmd_init(argv: list) -> int:
    args = _build_parser().parse_args(argv)
    console = Console()

    from rolo_claude.providers.config import load_env_file
    load_env_file(_env_file_path())

    console.print("[bold]rolo-claude init[/bold]")
    cwd = Path.cwd()
    written: list = []

    preset = _step_preset(args, console)
    if preset is None:
        return 2

    written.extend(_step_credentials(preset, args, console))

    model_ref_raw, model_path = _step_default_model(preset, args, console)
    if model_path:
        written.append(str(model_path))

    doctor_lines, _doctor_ok = _step_checks(args, console, cwd, preset=preset, model_raw=model_ref_raw)

    pong_ok = True
    if args.no_live:
        console.print("5. Live pong: skipped (--no-live)")
    else:
        pong_ok = _step_live_pong(model_ref_raw, cwd, console)

    written.extend(_step_linux_fixes(args, console))

    _step_summary(console, written, pong_ok, doctor_lines, no_live=args.no_live)

    if not args.no_live and not pong_ok:
        return 1
    return 0
