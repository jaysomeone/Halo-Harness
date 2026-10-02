"""halo_harness.bugreport -- `halo bugreport` / `/bugreport` (2.0.1 W3a):
"one paste instead of screenshots". Gathers version/env/terminal/install
info, provider enablement (with the REASON, never a key), the current
route, permission mode, MCP servers, a redacted `~/.halo/config.json`,
settings source paths (paths only), catalog cache ages, this session's own
learned permission rules, the last turn's timeline (`debug_timeline.
last_turn()`), the last N session events, and the last 50 `bridge.log`
lines. Every line passes through `redact.redact_for_bugreport` (the SAME
base patterns `halo export --sanitize` uses, plus a stronger pass for this
module's much wider surface) before anything is written or printed.

Works identically from `-p`/the TUI (both hand in a live `HeadlessFacade`)
and headlessly with neither running, against the most recent session for a
cwd (or `--session`) -- `cmd_bugreport` below is `halo bugreport`'s own
entry point; `tui/slash.py::_handle_bugreport` is `/bugreport`'s.

Scope note (reported per WORKER-RULES, not silently dropped): the last
watchdog lines are explicitly 2.0.3 scope per the brief ("when present
(2.0.3)") and are not included here.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from halo_harness import __version__
from halo_harness.redact import _GENERIC_SECRET_NAME, _SECRET_ENV_NAMES, redact_for_bugreport

_GENERIC_SECRET_NAME_RE = re.compile(f"^(?:{_GENERIC_SECRET_NAME})$")


def _collect_known_secret_values() -> tuple:
    """Every CURRENT value of a secret-shaped env var this process can see
    -- `redact_for_bugreport`'s own value-based pass catches a key even
    when it doesn't match any of this module's shape patterns."""
    values = []
    for name, value in os.environ.items():
        if value and (name in _SECRET_ENV_NAMES or _GENERIC_SECRET_NAME_RE.match(name)):
            values.append(value)
    return tuple(values)


def _git_branch(cwd: Path) -> str:
    try:
        result = subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=str(cwd),
                                 capture_output=True, text=True, timeout=2)
        if result.returncode == 0:
            return result.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    return "(not a git repo)"


def _install_mode_line() -> str:
    from halo_harness.doctor import check_command_on_path
    try:
        return check_command_on_path()
    except Exception as e:
        return f"could not determine ({type(e).__name__}: {e})"


def _provider_reason(name: str, env: "Optional[dict]", env_file_names: "frozenset[str]" = frozenset()) -> str:
    """Best-effort "why is this enabled" -- env file, settings env,
    databrickscfg, claude.ai login -- NEVER a key, not even a fingerprint.
    Checked in the order a real credential would actually be found.
    `env_file_names`: the variable names `load_provider_env_files()` added
    to the process environment (they were absent before the load), so a
    key that lives only in the env file reads as "env file" and not as
    "shell env" (the load uses setdefault, so the two are otherwise
    indistinguishable afterwards)."""
    from halo_harness.providers.enablement import credentials_present, is_enabled
    if not is_enabled(name):
        return "disabled"
    if not credentials_present(name, env=env):
        return "enabled (no credentials detected)"
    shell_names = {
        "openrouter": ("OPENROUTER_API_KEY",), "anthropic": ("ANTHROPIC_API_KEY",),
        "databricks": ("DATABRICKS_TOKEN", "DATABRICKS_HOST", "BRIDGE_DBX_TOKEN", "BRIDGE_DBX_BASE_URL"),
        "typesafe": ("TYPESAFE_API_KEY",),
    }.get(name, ())
    if any(n in env_file_names for n in shell_names):
        return "enabled (env file)"
    if any(n in os.environ for n in shell_names):
        return "enabled (shell env)"
    if any(n in (env or {}) for n in shell_names):
        return "enabled (settings env)"
    if name == "databricks" and (Path.home() / ".databrickscfg").exists():
        return "enabled (databrickscfg)"
    if name == "claude_subscription":
        return "enabled (claude.ai login)"
    return "enabled (env file)"


def _providers_lines(settings, env_file_names: "frozenset[str]" = frozenset()) -> list:
    env = getattr(settings, "effective_env", None) if settings is not None else None
    lines = ["Providers (enabled state and REASON, never a key):"]
    for name in ("openrouter", "anthropic", "databricks", "claude_subscription", "typesafe"):
        lines.append(f"  {name}: {_provider_reason(name, env, env_file_names)}")
    return lines


def _launch_permission_mode_text() -> str:
    """The starting permission mode a launch from here would use, and which
    layer decided it -- doctor's own line, minus its status prefix."""
    try:
        from halo_harness.doctor import _check_permission_mode
        text = _check_permission_mode()
    except Exception as e:  # pragma: no cover - diagnostics never crash the report
        return f"? ({type(e).__name__})"
    return text.split("Permission mode: ", 1)[1] if "Permission mode: " in text else text


def _launch_default_lines(cwd: Path) -> list:
    """No live session: what the next launch from `cwd` would use -- the
    configured default model (doctor's own resolution) and the remembered
    last model/effort for this directory (`launch_state`, 2.0.1)."""
    lines = ["Route: (no live session; what the next launch here would use)"]
    try:
        from halo_harness.doctor import _check_default_model
        text = _check_default_model()
        lines.append("  default model: " + (text.split("Default model: ", 1)[1] if "Default model: " in text else text))
    except Exception as e:  # pragma: no cover
        lines.append(f"  default model: ? ({type(e).__name__})")
    try:
        from halo_harness.launch_state import resolve_last_effort, resolve_last_model
        lines.append(f"  last used here (state.json): model={resolve_last_model(cwd) or '-'} "
                     f"effort={resolve_last_effort(cwd) or '-'}")
    except Exception as e:  # pragma: no cover
        lines.append(f"  last used here (state.json): ? ({type(e).__name__})")
    return lines


def _log_meta_model(session) -> "Optional[str]":
    """The `model` recorded in the session log's own `meta` node -- what a
    log-only session (headless `halo bugreport`) knows about its route."""
    log = getattr(session, "log", None)
    try:
        for node in (log.read_all() if log is not None else []):
            if isinstance(node, dict) and node.get("type") == "meta" and node.get("model"):
                return str(node["model"])
    except Exception:
        return None
    return None


def _route_lines(session, cwd: "Optional[Path]" = None) -> list:
    if session is None:
        return _launch_default_lines(cwd or Path.cwd())
    ref = getattr(session, "model_ref", None)
    profile = getattr(session, "model_profile", None)
    ref_text = getattr(ref, "raw", None) or (
        f"{_log_meta_model(session)} (from the session log)" if _log_meta_model(session) else "?")
    lines = [
        "Route:",
        f"  ref: {ref_text}",
        f"  provider: {getattr(ref, 'provider', '?')}",
        f"  api_type/dialect: {getattr(ref, 'dialect', '?')}",
        f"  effort requested: {getattr(session, 'effort_requested', None)}",
        f"  effort sent: {getattr(session, 'effort', None)}",
    ]
    if profile is not None:
        lines.append(f"  profile: context={getattr(profile, 'context_tokens', '?')} "
                     f"max_output={getattr(profile, 'max_output_tokens', '?')} "
                     f"reasoning={getattr(profile, 'reasoning', '?')} "
                     f"vision={getattr(profile, 'vision', '?')}")
    return lines


def _mcp_lines(mcp_status: "Optional[list]", mcp_servers: "Optional[dict]") -> list:
    if mcp_status:
        lines = ["MCP servers:"]
        for s in mcp_status:
            err = f" -- {s.get('error')}" if s.get("error") else ""
            lines.append(f"  {s.get('name')}: {s.get('state')}{err}")
        return lines
    if mcp_servers:
        return ["MCP servers (configured, not started this session):"] + [f"  {n}" for n in mcp_servers]
    return ["MCP servers: none configured"]


def _catalog_cache_lines(state_dir: Path) -> list:
    def _fmt(age):
        return "never" if age is None else f"{age / 3600:.1f}h ago"
    try:
        from halo_harness.providers.databricks import dbx_endpoints_age_seconds, models_json_age_seconds
        dbx_age, or_age = dbx_endpoints_age_seconds(state_dir), models_json_age_seconds(state_dir)
    except Exception:
        dbx_age = or_age = None
    try:
        from halo_harness.providers.anthropic_catalog import ant_models_age_seconds
        ant_age = ant_models_age_seconds(state_dir)
    except Exception:
        ant_age = None
    return ["Catalog cache age:", f"  Databricks: {_fmt(dbx_age)}", f"  OpenRouter: {_fmt(or_age)}",
            f"  Anthropic: {_fmt(ant_age)}"]


def _learned_rules_lines(session) -> list:
    engine = getattr(session, "permission_engine", None) if session is not None else None
    if engine is None:
        return ["Learned rules: (no live session)"]
    learned = []
    for bucket_name in ("allow_rules", "deny_rules", "ask_rules"):
        for rule in getattr(engine, bucket_name, None) or []:
            if getattr(rule, "source", "") == "session":
                learned.append(f"{bucket_name.replace('_rules', '')}: {getattr(rule, 'raw', '?')}")
    if not learned:
        return ["Learned rules: none this session"]
    return ["Learned rules (this session's memory only, never written to disk unless 'always' was chosen):"] + \
           [f"  {r}" for r in learned]


def _settings_paths_lines(settings) -> list:
    layers = getattr(settings, "layers", None) if settings is not None else None
    if not layers:
        return ["Settings sources: (none resolved)"]
    found = [str(layer.path) for layer in layers if getattr(layer, "path", None)]
    if not found:
        return ["Settings sources: (none with a real file on disk)"]
    return ["Settings sources (paths only):"] + [f"  {p}" for p in found]


def _recent_log_lines(state_dir: Path, n: int = 50) -> list:
    try:
        lines = (state_dir / "bridge.log").read_text(encoding="utf-8", errors="replace").splitlines()
        return lines[-n:]
    except OSError:
        return ["(bridge.log not found)"]


def _session_events_lines(log, n: int, *, include_content: bool) -> list:
    if log is None:
        return ["Last session events: (no session found)"]
    try:
        nodes = log.read_all()[-max(n, 0):] if n > 0 else []
    except Exception as e:
        return [f"Last session events: (could not read: {type(e).__name__}: {e})"]
    lines = [f"Last {len(nodes)} session event(s) (type, tool names, status/error):"]
    for node in nodes:
        kind = node.get("type", "?")
        bits = [kind]
        if kind == "assistant":
            stop = node.get("stop_reason")
            if stop:
                bits.append(f"stop_reason={stop}")
            for block in node.get("content") or []:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    bits.append(f"tool={block.get('name')}")
        if kind == "user" and include_content:
            for block in node.get("content") or []:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    status = "error" if block.get("is_error") else "ok"
                    bits.append(f"tool_result={status}")
        lines.append("  " + " ".join(bits))
    return lines


def _timeline_lines() -> list:
    from halo_harness import debug_timeline
    record = debug_timeline.last_turn()
    if record is None:
        return ["Last turn's timeline: (none recorded yet this process)"]
    return ["Last turn's timeline:", f"  {json.dumps(record, default=str, sort_keys=True)}"]


def build_bugreport_text(*, facade=None, session=None, settings=None, state_dir: Path, cwd: Path,
                          permission_mode: str = "?", include_content: bool = False, last: int = 20,
                          env_file_names: "frozenset[str]" = frozenset()) -> str:
    """The ONE builder both `cmd_bugreport` (headless/`-p`) and `/bugreport`
    (the TUI, which has a real `facade`) call. `facade` is optional --
    headless-with-no-live-session mode (the common `halo bugreport` case:
    read the most recent session for this cwd) passes only `session`/
    `settings`."""
    session = session if session is not None else getattr(facade, "session", None)
    settings = settings if settings is not None else getattr(facade, "settings", None)
    if facade is not None:
        permission_mode = getattr(facade, "permission_mode", permission_mode)
        mcp_status, mcp_servers = getattr(facade, "mcp_status", None), getattr(facade, "mcp_servers", None)
    else:
        mcp_status, mcp_servers = None, None
        if permission_mode == "?":
            permission_mode = _launch_permission_mode_text()

    lines = [
        f"# halo bugreport -- {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}",
        "",
        f"halo version: {__version__}",
        f"Python: {platform.python_version()}",
        f"OS: {platform.system()} {platform.release()}",
        f"Terminal: TERM={os.environ.get('TERM', '?')!r} WT_SESSION={'set' if os.environ.get('WT_SESSION') else 'unset'} "
        f"tmux={'set' if os.environ.get('TMUX') else 'unset'}",
        f"Shell: {os.environ.get('SHELL') or os.environ.get('ComSpec') or '?'}",
        f"cwd: {cwd}",
        f"git branch: {_git_branch(cwd)}",
        f"Install mode: {_install_mode_line()}",
        f"Permission mode: {permission_mode}",
        "",
    ]
    lines += _providers_lines(settings, env_file_names)
    lines.append("")
    lines += _route_lines(session, cwd)
    lines.append("")
    lines += _mcp_lines(mcp_status, mcp_servers)
    lines.append("")
    try:
        config_text = (state_dir / "config.json").read_text(encoding="utf-8")
    except OSError:
        config_text = "(not found)"
    lines.append("~/.halo/config.json (secret-shaped values replaced):")
    lines.append(config_text)
    lines.append("")
    lines += _settings_paths_lines(settings)
    lines.append("")
    lines += _catalog_cache_lines(state_dir)
    lines.append("")
    lines += _learned_rules_lines(session)
    lines.append("")
    lines += _timeline_lines()
    lines.append("")
    log = getattr(session, "log", None) if session is not None else None
    lines += _session_events_lines(log, last, include_content=include_content)
    lines.append("")
    lines.append("Last 50 bridge.log line(s):")
    lines.extend(f"  {ln}" for ln in _recent_log_lines(state_dir))

    text = "\n".join(str(ln) for ln in lines) + "\n"
    return redact_for_bugreport(text, known_secret_values=_collect_known_secret_values())


def write_bugreport(text: str, state_dir: Path) -> Path:
    out_dir = state_dir / "bugreports"
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = out_dir / f"{ts}.md"
    path.write_text(text, encoding="utf-8")
    return path


def copy_to_clipboard(text: str) -> bool:
    """Best-effort, whatever exists: `clip` on Windows, `pbcopy` on macOS,
    `wl-copy` or `xclip` on Linux; False (caller just shows the path) when
    none is found."""
    import shutil
    if sys.platform == "win32":
        candidates = [["clip"]]
    elif sys.platform == "darwin":
        candidates = [["pbcopy"]]
    else:
        candidates = [["wl-copy"], ["xclip", "-selection", "clipboard"]]
    for argv in candidates:
        if shutil.which(argv[0]):
            try:
                subprocess.run(argv, input=text.encode("utf-8"), timeout=5)
                return True
            except (OSError, subprocess.SubprocessError):
                continue
    return False


def _resolve_headless_session_log(cwd: Path, session_arg: "Optional[str]"):
    from halo_harness.agent import sessions as agent_sessions
    from halo_harness.agent.log import SessionLog
    if session_arg:
        session_id, err = agent_sessions.resolve_resume(cwd, session_arg)
        if session_id is None:
            return None, err
        return SessionLog(cwd, session_id=session_id), None
    log = SessionLog.latest_for_cwd(cwd)
    if log is None:
        return None, "no sessions found for this directory"
    return log, None


class _LogOnlySession:
    """A minimal stand-in so `build_bugreport_text`'s `session`-shaped
    readers (`_learned_rules_lines`/`_route_lines`/`_session_events_lines`)
    degrade gracefully to "no live session"/"(no session found)" rather
    than crashing, while still exposing the one thing headless `halo
    bugreport` (no live Session object at all) actually has: the log."""

    def __init__(self, log) -> None:
        self.log = log
        self.model_ref = None
        self.permission_engine = None
        self.effort = None
        self.effort_requested = None


def cmd_bugreport(argv: list) -> int:
    parser = argparse.ArgumentParser(prog="halo bugreport", add_help=True,
                                      description="Write a redacted diagnostic report (one paste instead of screenshots).")
    parser.add_argument("--last", type=int, default=20, metavar="N", help="Session events to include (default 20)")
    parser.add_argument("--session", default=None, metavar="ID", help="Session id or unique prefix")
    parser.add_argument("--out", default=None, metavar="FILE", help="Write to FILE instead of ~/.halo/bugreports/")
    parser.add_argument("--copy", action="store_true", help="Also copy the report text to the clipboard")
    parser.add_argument("--include-content", action="store_true", help="Include prompt/output text, not just shapes")
    parser.add_argument("--cwd", default=None, metavar="DIR")
    args = parser.parse_args(argv)

    from halo_harness.config.claude_json import is_trusted, load_claude_json
    from halo_harness.config.paths import bridge_home
    from halo_harness.config.settings import resolve_settings
    from halo_harness.providers.config import load_provider_env_files

    # Same first step as `halo providers`/doctor: a key that lives only in
    # the env file must count, or every provider reads "disabled" here (seen
    # live on the Kali VM). The names the load ADDED are remembered so the
    # provider reason can say "env file" rather than "shell env".
    present_before = set(os.environ)
    env_file_names = frozenset(k for k in load_provider_env_files() if k not in present_before)
    # Prime the subscription cache once (gateway-driven `claude` is never
    # spawned; see cc_models.refresh_cached_claude_auth_status), the same
    # staleness-gated step the providers listing does.
    try:
        from halo_harness.providers.cc_models import cached_auth_status_is_stale, refresh_cached_claude_auth_status
        if cached_auth_status_is_stale():
            refresh_cached_claude_auth_status()
    except Exception:
        pass

    cwd = Path(args.cwd).resolve() if args.cwd else Path.cwd()
    state_dir = bridge_home()
    claude_json = load_claude_json()
    settings = resolve_settings(cwd, trusted=is_trusted(cwd, claude_json))

    log, err = _resolve_headless_session_log(cwd, args.session)
    if log is None and err:
        print(f"halo bugreport: note: {err} -- report will have no session-specific data", file=sys.stderr)
    session = _LogOnlySession(log) if log is not None else None

    text = build_bugreport_text(session=session, settings=settings, state_dir=state_dir, cwd=cwd,
                                 include_content=args.include_content, last=args.last,
                                 env_file_names=env_file_names)
    if args.out:
        try:
            Path(args.out).write_text(text, encoding="utf-8")
        except OSError as e:
            print(f"halo bugreport: could not write {args.out}: {e}", file=sys.stderr)
            return 1
        print(f"halo bugreport: wrote {args.out}")
    else:
        path = write_bugreport(text, state_dir)
        print(f"halo bugreport: wrote {path}")
    if args.copy:
        print("halo bugreport: copied to clipboard" if copy_to_clipboard(text)
              else "halo bugreport: no clipboard tool found -- see the path above")
    return 0
