"""halo_harness.init_cli -- `halo init` (H12 brief Part A /
RECOMMENDATIONS.md P0 #1): the whole first run in one command. Interactive
by default (plain prompts via `rich`, or a small Textual list picker on a
real terminal -- see `halo_harness.init_providers`/`tui/dialogs/init_picker.py`);
`--yes` accepts every default without prompting; `--provider ... --yes` is
fully non-interactive whenever the needed value (a key/token) is already
discoverable, and never BLOCKS even when it isn't (a piped/non-tty stdin is
read for one line instead of hanging on a real terminal prompt -- see
`_prompt_secret`).

1.0.1 hotfix 13: step 1 is now "select a provider to set up" (Databricks,
OpenRouter, Anthropic API, or your Claude subscription), never a home/work/
claude PRESET naming a bundle of choices the owner found confusing ("work is
actually setting up databricks... the init should scroll thru all possible
providers and the user go thru that path of setup") -- each provider runs
its own credentials -> catalog/discovery -> default-model-pick -> live-pong
path, and `init` offers to set up another when one finishes, looping until
the user is done; when more than one provider ends up configured, one last
cross-provider pick chooses the actual default. `--preset home|work|claude`
still parses, as a deprecated one-line-noticed alias for `--provider
openrouter|databricks|claude` (`docs/COMMANDS.md`'s own examples keep
working verbatim).

Idempotent throughout: re-running shows the current state and changes
nothing already correctly configured (must-do for the Kali VM, which
already has its key/rg/PATH set up). Never writes `~/.claude.json` or
`~/.claude/settings.json` (this harness only ever READS those); never
prints a key or token anywhere, success or failure.
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
from rich.prompt import Confirm

from halo_harness.init_providers import (
    PRESET_TO_PROVIDER, PROVIDER_DEFAULT_MODEL, PROVIDER_LABEL, PROVIDERS,
    claude_login_available, configured_providers, detect_default_provider, model_entries_for_provider,
    provider_status,
)


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
    """The env file `init` reads from and writes to (2.0.0 fixpass finding
    4): `HALO_ENV_FILE`, else legacy `BRIDGE_ENV_FILE`, else always
    `~/.config/halo/env` -- copying the legacy `~/.config/vibes-hacker/env`
    file forward (content + 0700/0600 permissions) the FIRST time this is
    called when the new one doesn't exist yet but the legacy one does, so
    every credential already there survives into the new file instead of
    being silently orphaned. See `config.paths.env_file_path_for_write`."""
    from halo_harness.config.paths import env_file_path_for_write
    return env_file_path_for_write()


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
# Step 1: provider selection (1.0.1 hotfix 13)
# ---------------------------------------------------------------------------

def _requested_providers(args, console: Console) -> "Optional[list[str]]":
    """`None` means "run the fully interactive picker loop"; otherwise the
    exact, ordered list of providers to walk through non-interactively (no
    "set up another?" prompt between them, no picker shown at all) --
    either `--provider` (repeatable, first-listed first) or the deprecated
    `--preset` alias (one line, exactly once, per the hotfix 13 spec:
    "docs/COMMANDS.md's own examples keep working verbatim")."""
    if args.provider:
        return list(dict.fromkeys(args.provider))  # de-dup, keep first-seen order
    if args.preset:
        provider = PRESET_TO_PROVIDER[args.preset]
        console.print(f"[dim](--preset {args.preset} is a deprecated alias for --provider {provider})[/dim]")
        return [provider]
    return None


def _provider_row_label(name: str, *, mark_default: bool) -> str:
    status = provider_status(name)
    marker = "  (detected)" if mark_default else ""
    return f"{name:<11} {status:<11} {PROVIDER_LABEL[name].split(' -- ', 1)[1]}{marker}"


def _step_select_provider(args, console: Console, *, header: str) -> Optional[str]:
    """Returns the chosen provider name, `"done"` (stop the loop -- only
    reachable interactively, via the picker/prompt's own "Done" row), or
    `None` on cancel. `provider_status`/`detect_default_provider` are
    re-read fresh on every call, so a second lap through the loop (after
    "set up another provider?") shows updated tags."""
    default_provider = detect_default_provider()
    rows = [(p, _provider_row_label(p, mark_default=(p == default_provider))) for p in PROVIDERS]
    rows.append(("done", "Done -- finish init"))
    if sys.stdin.isatty() and sys.stdout.isatty() and not args.yes:
        from halo_harness.tui.dialogs.init_picker import run_simple_picker
        try:
            return run_simple_picker(header, rows, initial_ref=default_provider)
        except Exception as e:
            console.print(f"   [WARN] interactive picker failed ({type(e).__name__}: {e}) -- falling back to a numbered list.")
    if _non_interactive(args):
        console.print(f"{header} {default_provider} (detected; pass --provider to choose explicitly)")
        return default_provider
    console.print(header)
    for i, (ref, label) in enumerate(rows, 1):
        console.print(f"   {i}. {label}")
    default_idx = [r for r, _l in rows].index(default_provider) + 1
    raw = _prompt_plain(f"   choose [1-{len(rows)}], default {default_idx}")
    raw = (raw or "").strip()
    if not raw:
        return default_provider
    try:
        idx = int(raw)
    except ValueError:
        return default_provider
    return rows[idx - 1][0] if 1 <= idx <= len(rows) else default_provider


# ---------------------------------------------------------------------------
# Step 2: credentials
# ---------------------------------------------------------------------------

def _ensure_openrouter_key(args, console: Console) -> Optional[Path]:
    from halo_harness.providers.config import redact, resolve_openrouter
    existing = resolve_openrouter()
    if existing is not None:
        console.print(f"   [OK] OpenRouter key: already configured ({redact(existing.api_key)}) -- not changed.")
        return None
    if args.yes and sys.stdin.isatty():
        console.print("   [WARN] OpenRouter key not found, and --yes skips the prompt -- "
                       "set OPENROUTER_API_KEY (or re-run `halo init` without --yes).")
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
    from halo_harness.providers.config import (
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
    from halo_harness.team_config import load_team_config
    cfg, warnings = load_team_config(Path.cwd(), team_flag=getattr(args, "team", None))
    for w in warnings:
        console.print(f"   [WARN] {w}")
    return cfg


def _ensure_databricks_creds(args, console: Console) -> Optional[Path]:
    from halo_harness.providers.config import databricks_work_env_active, redact, resolve_databricks
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
            from halo_harness.team_config import apply_gateway_preference
            apply_gateway_preference(team_cfg["gateway_preference"])
        if team_cfg and team_cfg.get("roles"):
            # V2c (H15): the SAME "seed config.json, never clobber a local
            # override" idiom as gateway_preference above, for team.json's
            # own shared `roles` table.
            from halo_harness.roles import apply_role_preference
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


def _ensure_anthropic_key(args, console: Console) -> Optional[Path]:
    """1.0.1 hotfix 13: the fourth provider -- a direct `ANTHROPIC_API_KEY`
    against api.anthropic.com (`ant:` models), previously not offered by
    `init` at all (the old home/work/claude presets had no slot for it)."""
    from halo_harness.providers.config import redact, resolve_anthropic
    existing = resolve_anthropic()
    if existing is not None:
        console.print(f"   [OK] Anthropic API key: already configured ({redact(existing.api_key)}) -- not changed.")
        return None
    if args.yes and sys.stdin.isatty():
        console.print("   [WARN] ANTHROPIC_API_KEY not found, and --yes skips the prompt -- "
                       "set ANTHROPIC_API_KEY (or re-run `halo init` without --yes).")
        return None
    console.print("   ANTHROPIC_API_KEY not found (get one at https://console.anthropic.com/settings/keys).")
    value = _prompt_secret("   ANTHROPIC_API_KEY")
    if not value:
        console.print("   [WARN] no key entered -- the Anthropic API will not be configured yet.")
        return None
    path = _env_file_path()
    _write_env_var(path, "ANTHROPIC_API_KEY", value)
    os.environ["ANTHROPIC_API_KEY"] = value
    console.print(f"   [OK] wrote ANTHROPIC_API_KEY to {path}")
    return path


def _step_credentials(provider: str, args, console: Console) -> list:
    console.print("Credentials:")
    if provider == "openrouter":
        path = _ensure_openrouter_key(args, console)
    elif provider == "databricks":
        path = _ensure_databricks_creds(args, console)
    elif provider == "anthropic":
        path = _ensure_anthropic_key(args, console)
    elif provider == "codex":
        console.print("   codex: nothing stored -- your existing `codex` ChatGPT login is used as-is.")
        path = None
    else:
        console.print("   claude: nothing stored -- your existing `claude` login is used as-is.")
        path = None
    return [str(path)] if path else []


# ---------------------------------------------------------------------------
# Step 3: default model
# ---------------------------------------------------------------------------

def _model_belongs_to_provider(model_ref_raw: str, provider: str) -> bool:
    """1.0.1 part 2 fixpass finding 7: whether an already-configured custom
    `config.json` model ref plausibly belongs to `provider` -- the gate for
    init's own "keep a custom value" rule below, which must never keep
    ANOTHER provider's ref just because the user happens to have one
    configured. Verified live: config pinned to `or:deepseek/...`, running
    `init --provider databricks --yes` "kept" it, so the live pong -- and
    every later launch -- targeted OpenRouter through a Databricks setup
    run, which the gate then refused outright (every later launch failed
    "invalid --model")."""
    prefix = {"databricks": "dbx:", "openrouter": "or:", "anthropic": "ant:", "claude": "cc:",
              "codex": "cx:"}.get(provider)
    if prefix and model_ref_raw.startswith(prefix):
        return True
    if provider == "databricks":
        if model_ref_raw.startswith("databricks-") or model_ref_raw.startswith("system.ai."):
            return True
        from halo_harness.model import _is_cached_databricks_endpoint
        return _is_cached_databricks_endpoint(model_ref_raw)
    if provider == "openrouter":
        # The bare `vendor/model` OpenRouter form (no `or:` prefix).
        if "/" in model_ref_raw and model_ref_raw.count("/") == 1 and not any(ch.isspace() for ch in model_ref_raw):
            vendor, _, rest = model_ref_raw.partition("/")
            return bool(vendor and rest)
    return False


def _step_default_model(provider: str, args, console: Console, *, write: bool = True) -> "tuple[str, Optional[Path]]":
    """The provider's own STARTING default -- `write=False` (used when this
    provider isn't the only one in play, see `_step_finalize_default_model`
    below) computes it without touching config.json at all, so a later
    cross-provider pick is never fighting an intermediate write."""
    from halo_harness.config.paths import bridge_home
    from halo_harness.theme import get_config_value, set_config_value
    team_default = None
    if provider == "databricks" and not args.model:
        from halo_harness.team_config import load_team_config
        team_cfg, _warnings = load_team_config(Path.cwd(), team_flag=getattr(args, "team", None))
        team_default = (team_cfg or {}).get("default_model")
    chosen = args.model or team_default or PROVIDER_DEFAULT_MODEL[provider]
    if not write:
        return chosen, None
    current = get_config_value("model", default=None)
    if current == chosen:
        console.print(f"   Default model: {chosen} (unchanged)")
        return chosen, None
    # 1.0.1 part 2 (reviewer minor): a custom model already configured must
    # survive a re-run of init for the SAME (or any) provider unless the
    # user explicitly passed --model THIS run -- init's own module
    # docstring already promises "re-running shows the current state and
    # changes nothing already correctly configured", but this step used to
    # overwrite ANY existing value with the provider's own hardcoded
    # default the moment they merely differed (e.g. `init --provider
    # databricks --yes` on a box already pinned to a non-default endpoint
    # silently reset it back to the Databricks preset's own default).
    # `_step_finalize_default_model` already applies this exact rule for
    # the cross-provider pick (1.0.1 fixpass finding 9); this is the SAME
    # rule for the earlier per-provider step finding 9 never touched.
    #
    # 1.0.1 part 2 fixpass finding 7: kept ONLY when `current` actually
    # belongs to the PROVIDER BEING SET UP right now (never another
    # provider's ref -- see `_model_belongs_to_provider`'s own docstring)
    # AND no team.json default applies (a team default is an explicit,
    # shared decision that outranks whatever one person's box happened to
    # have configured before).
    if (isinstance(current, str) and current and not args.model and not team_default
            and _model_belongs_to_provider(current, provider)):
        console.print(f"   Default model: {current} (kept -- custom value already configured)")
        return current, None
    set_config_value("model", chosen)
    path = bridge_home() / "config.json"
    console.print(f"   Default model: {chosen} -- wrote {path}")
    return chosen, path


# ---------------------------------------------------------------------------
# Step 4: checks (doctor + models --refresh)
# ---------------------------------------------------------------------------

def _print_work_catalog_summary(console: Console, model_raw: str) -> None:
    """H14 scope I: "prints the count of models available and the default"
    -- read straight back from the JUST-refreshed dbx-endpoints.json, so
    the number always matches what `/model`/`models --refresh` would show,
    never a separately-maintained count. 1.0.1 hotfix 4: "chat-capable" now
    counts by the endpoint's own `task` (`dbx_routing.is_chat_task`), not
    `bool(api_types)` -- verified live: a workspace whose cache predates
    this milestone's own `api_types` field showed "0 chat-capable" here
    while `halo models`' own table said "chat yes" for the SAME
    rows (that table used a different, name-based heuristic) -- the two
    must never disagree again."""
    from halo_harness.config.paths import bridge_home
    from halo_harness.providers.databricks import load_dbx_endpoints_json
    from halo_harness.providers.dbx_routing import is_chat_task
    endpoints = load_dbx_endpoints_json(bridge_home())
    chat = sum(1 for e in endpoints.values() if isinstance(e, dict) and is_chat_task(e.get("task")))
    console.print(f"   {len(endpoints)} Databricks endpoint(s) cached ({chat} chat-capable) -- default: {model_raw}")


def _step_checks(args, console: Console, cwd: Path, *, provider: str = "", model_raw: str = "") -> "tuple[list, bool]":
    console.print("Checks:")
    from halo_harness.doctor import run_checks
    lines, ok = run_checks(cwd=cwd)
    from rich.markup import escape
    for line in lines:
        # Doctor lines carry paths and brackets; print them literally so a
        # `[/home/...]` in a path can never read as a rich closing tag.
        console.print(f"   {escape(line)}")
    if args.no_live:
        console.print("   (catalog refresh skipped: --no-live)")
    else:
        console.print("   refreshing model catalogs (models --refresh)...")
        from halo_harness.catalog_cli import cmd_models
        try:
            cmd_models(["--refresh"])
            if provider == "databricks":
                _print_work_catalog_summary(console, model_raw)
        except Exception as e:
            console.print(f"   [WARN] catalog refresh failed: {type(e).__name__}: {e}")
    return lines, ok


# ---------------------------------------------------------------------------
# 1.0.1 hotfix 5: an interactive model picker, offered right after the
# catalog is refreshed (step 4) -- `--yes`/`--model` skip it outright (the
# user already made a choice); with no real terminal, a numbered list is
# printed and read off one stdin line instead of the full-screen picker.
# ---------------------------------------------------------------------------

def _run_entry_picker(args, console: Console, entries: "list[dict]") -> "Optional[str]":
    """The shared TTY-check/full-screen-picker/numbered-fallback dance for
    ANY `model_display`-shaped entries list -- used by both the per-provider
    default-model pick and the final cross-provider one (1.0.1 hotfix 13)."""
    if sys.stdin.isatty() and sys.stdout.isatty():
        try:
            from halo_harness.tui.dialogs.init_picker import run_init_picker
            return run_init_picker(entries)
        except Exception as e:
            console.print(f"   [WARN] interactive picker failed ({type(e).__name__}: {e}) -- falling back to a numbered list.")
    # `--yes` is always checked by the CALLER before this is ever reached --
    # a non-tty run still gets the numbered fallback (a piped stdin, e.g. a
    # script/CI driving init non-interactively on purpose, can still answer
    # it); `_prompt_plain` itself degrades an EOF/empty line to "keep the
    # default" rather than blocking, so this never hangs a truly unattended
    # run.
    return _numbered_model_pick(entries, console)


def _chat_capable_dbx_entries(state_dir) -> "list[dict]":
    """`[{"ref": "dbx:<name>", "group": "<family>", "context_tokens",
    "max_output_tokens", "price_in_per_m", "price_out_per_m", "detail"},
    ...]`, chat-capable only (`dbx_routing.is_chat_task`) -- the SAME
    "family x api_types" data `halo models`/the `/model` picker
    already show (1.0.1 hotfix 12: including ctx/output/price, via
    `model_display.databricks_row_fields`), never a separately-maintained
    list. Rendered through `model_display.format_model_row`, same as every
    other model-listing surface."""
    from halo_harness.model_display import databricks_row_fields
    from halo_harness.providers.databricks import dbx_endpoints_cache_is_old_shape, load_dbx_endpoints_json
    from halo_harness.providers.dbx_routing import PATH_TYPE_DISPLAY, classify_family, default_path_type, is_chat_task
    endpoints = load_dbx_endpoints_json(state_dir)
    old_shape = dbx_endpoints_cache_is_old_shape(endpoints)
    out = []
    for name in sorted(endpoints):
        e = endpoints[name] if isinstance(endpoints[name], dict) else {}
        if not is_chat_task(e.get("task")):
            continue
        family = classify_family(name, foundation_model_name=e.get("foundation_model_name") or "",
                                  model_class=e.get("model_class") or "")
        path_type = "unknown" if old_shape else default_path_type(name, state_dir)
        path_display = PATH_TYPE_DISPLAY.get(path_type, path_type)
        try:
            fields = databricks_row_fields(name, state_dir=state_dir)
        except Exception:
            fields = {}
        out.append({"ref": f"dbx:{name}", "group": family, "detail": f"{family} · {path_display}",
                    "context_tokens": fields.get("context_tokens"), "max_output_tokens": fields.get("max_output_tokens"),
                    "price_in_per_m": fields.get("price_in_per_m"), "price_out_per_m": fields.get("price_out_per_m")})
    return out


def _numbered_model_pick(entries: "list[dict]", console: Console) -> "Optional[str]":
    """The no-TTY fallback: a plain numbered list, one stdin line read for
    the choice -- `None` (keep whatever default is already chosen) on
    empty/invalid input or EOF, never blocks, never raises."""
    from halo_harness.model_display import ROW_HEADER, format_model_row
    console.print("   Pick a default model (or press Enter to keep the current default):")
    console.print(f"   {ROW_HEADER}")
    for i, e in enumerate(entries, 1):
        console.print(f"     {i}. {format_model_row(e)}")
    raw = _prompt_plain("   choice")
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        idx = int(raw)
    except ValueError:
        return None
    return entries[idx - 1]["ref"] if 1 <= idx <= len(entries) else None


def _step_pick_model(args, console: Console, provider: str, model_ref_raw: str, *, write: bool = True) -> str:
    """Returns the (possibly user-picked) model ref -- unchanged from
    `model_ref_raw` whenever the picker is skipped, cancelled, offers
    nothing (no chat-capable endpoint cached), or picks nothing. `write`
    (default True) persists the pick to config.json the SAME way step 3
    does; `_step_finalize_default_model` passes `write=False` when more
    than one provider is in play for THIS call (its own single final pick,
    across every configured provider, is what actually gets written)."""
    if args.yes or args.model:
        return model_ref_raw
    from halo_harness.config.paths import bridge_home
    state_dir = bridge_home()
    entries = model_entries_for_provider(provider, state_dir)
    if not entries:
        return model_ref_raw
    chosen = _run_entry_picker(args, console, entries)
    if not chosen or chosen == model_ref_raw:
        return model_ref_raw
    if write:
        from halo_harness.theme import set_config_value
        set_config_value("model", chosen)
        console.print(f"   Default model: {chosen} (picked interactively) -- wrote {state_dir / 'config.json'}")
    return chosen


def _step_finalize_default_model(args, console: Console, configured_this_run: "list[str]",
                                  picked_per_provider: "dict[str, str]") -> str:
    """1.0.1 hotfix 13: "when more than one provider ends up configured,
    one last arrow-key pick 'Default model' across all configured
    providers". `configured_providers()` re-checks ALL FOUR providers'
    real credential state (not just what this run itself touched), so a
    box that already had e.g. OpenRouter configured from before still
    offers it alongside whatever `init` just set up this time. With
    exactly one provider configured (now or already), that provider's own
    per-provider pick (already written) stands as-is -- no second prompt."""
    all_configured = configured_providers()
    if len(all_configured) <= 1:
        only = all_configured[0] if all_configured else (configured_this_run[-1] if configured_this_run else None)
        return picked_per_provider.get(only, "") if only else ""
    from halo_harness.config.paths import bridge_home
    state_dir = bridge_home()
    entries = []
    for provider in all_configured:
        for e in model_entries_for_provider(provider, state_dir):
            entries.append({**e, "group": e.get("group") or provider})
    if not entries:
        # Nothing to pick from (e.g. every configured provider's own catalog
        # is still empty/unrefreshed) -- keep whichever provider was set up
        # LAST this run, same as the single-provider case above.
        last = configured_this_run[-1] if configured_this_run else all_configured[-1]
        return picked_per_provider.get(last, PROVIDER_DEFAULT_MODEL.get(last, ""))
    console.print(f"Default model ({len(all_configured)} providers configured: {', '.join(all_configured)}):")
    if args.yes:
        chosen = None
    else:
        chosen = _run_entry_picker(args, console, entries)
    if not chosen:
        # 1.0.1 fixpass finding 9: --yes / Esc'd out of the picker is NOT
        # "the user chose a value" -- keep the model each provider's own
        # setup step already wrote to config.json (the REAL current
        # default) instead of overwriting it with an arbitrary guess keyed
        # off all_configured's fixed definition order (which provider ends
        # up "last" there has nothing to do with what the user actually
        # just set up or already had -- verified: a box with Databricks AND
        # OpenRouter configured got `or:deepseek/...` written in place of
        # its real `dbx:` default on a plain double-Esc).
        from halo_harness.theme import get_config_value
        existing = get_config_value("model", default=None)
        if isinstance(existing, str) and existing:
            console.print(f"   Default model: {existing} (kept -- nothing chosen this run)")
            return existing
        # A genuinely fresh box with no model at all yet still needs ONE
        # written so init leaves it in a working state -- same fallback the
        # pre-fix code always used.
        last = configured_this_run[-1] if configured_this_run else all_configured[-1]
        chosen = picked_per_provider.get(last, PROVIDER_DEFAULT_MODEL.get(last, ""))
    from halo_harness.theme import set_config_value
    set_config_value("model", chosen)
    console.print(f"   Default model: {chosen} -- wrote {state_dir / 'config.json'}")
    return chosen


# ---------------------------------------------------------------------------
# 1.0.1 hotfix 18.1: default permission mode, after the provider(s)/default
# model are all settled.
# ---------------------------------------------------------------------------

_PERMISSION_MODE_ROWS = [
    ("auto", "auto (recommended) -- allow everything except explicit deny/ask rules"),
    ("acceptEdits", "acceptEdits -- file edits apply automatically, other tools still ask"),
    ("default", "default -- ask before edits and non-read-only tools"),
    ("plan", "plan -- research and propose a plan before making any change"),
]


def _step_pick_permission_mode(args, console: Console) -> str:
    """1.0.1 hotfix 18.1: "Default permission mode" -- an arrow-key list
    (same `run_simple_picker` widget the provider/model steps already use;
    numbered fallback with no TTY), `auto` first and recommended since
    that's what `halo` itself runs in day to day and `default`
    (Claude Code's own factory setting) is the one new users most often
    find confusing on their FIRST run. Writes the flat `permission_mode`
    key straight to `~/.halo/config.json` -- `headless.py::
    build_session`'s own precedence chain (hotfix 18.2) reads it back as
    the layer between an explicit `--permission-mode` and settings.json's
    `permissions.defaultMode`. Never touches `~/.claude/settings.json`."""
    from halo_harness.config.paths import bridge_home
    from halo_harness.theme import get_config_value, set_config_value

    def _existing() -> "Optional[str]":
        v = get_config_value("permission_mode", default=None)
        return v if isinstance(v, str) and v else None

    console.print("Default permission mode:")
    if sys.stdin.isatty() and sys.stdout.isatty() and not args.yes:
        from halo_harness.tui.dialogs.init_picker import run_simple_picker
        try:
            chosen = run_simple_picker("Default permission mode", _PERMISSION_MODE_ROWS, initial_ref="auto")
        except Exception as e:
            console.print(f"   [WARN] interactive picker failed ({type(e).__name__}: {e}) -- falling back to a numbered list.")
            chosen = None
        if chosen is None:
            # 1.0.1 fixpass finding 9: Esc'd out of the picker (or it
            # failed) -- nothing was actually CHOSEN this run. Keep
            # whatever's already in config.json, and write NOTHING at all
            # when there's nothing there yet, rather than forcing the
            # literal string "default" -- config.json's permission_mode
            # outranks settings.json's own defaultMode, so writing one here
            # unasked silently shadowed it forever.
            existing = _existing()
            if existing:
                console.print(f"   Default permission mode: {existing} (kept -- nothing chosen this run)")
            else:
                console.print("   not set (nothing chosen -- settings.json's own permissions.defaultMode, "
                               "or the app's built-in default, applies)")
            return existing or ""
    elif _non_interactive(args):
        # 1.0.1 fixpass finding 9: --yes / no tty is NOT "the user chose a
        # value" either -- same rule as the Esc case just above.
        existing = _existing()
        if existing:
            console.print(f"   {existing} (non-interactive; kept the existing config.json value)")
            return existing
        console.print("   not set (non-interactive; pass `halo config set permission_mode ...` "
                       "or settings.json's own permissions.defaultMode to choose one)")
        return ""
    else:
        for i, (ref, label) in enumerate(_PERMISSION_MODE_ROWS, 1):
            console.print(f"   {i}. {label}")
        raw = _prompt_plain(f"   choose [1-{len(_PERMISSION_MODE_ROWS)}], default 1 (auto)")
        raw = (raw or "").strip()
        idx = 1
        if raw:
            try:
                idx = int(raw)
            except ValueError:
                idx = 1
        idx = idx if 1 <= idx <= len(_PERMISSION_MODE_ROWS) else 1
        chosen = _PERMISSION_MODE_ROWS[idx - 1][0]
    set_config_value("permission_mode", chosen)
    console.print(f"   Default permission mode: {chosen} -- wrote {bridge_home() / 'config.json'}")
    return chosen


# ---------------------------------------------------------------------------
# Step 5: live pong
# ---------------------------------------------------------------------------

def _run_live_pong(model_raw: str, cwd: Path) -> "tuple[bool, str]":
    """Builds a real (bare) session for `model_raw` and drives one turn
    in-process (never shells out to `halo` itself), capturing the
    JSON result into an in-memory stream rather than real stdout -- the
    printed summary line is built from THAT (model/provider/reply/cost),
    so a key/token is never anywhere near what this function prints."""
    import io
    import json as json_mod
    try:
        from halo_harness import headless
        from halo_harness.output import PrintModeSink
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
    console.print(f"Live pong ({model_raw}):")
    ok, line = _run_live_pong(model_raw, cwd)
    console.print(f"   {'[OK]' if ok else '[WARN]'} {line}")
    if not ok:
        console.print("   Next: `halo doctor` explains what's missing.")
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
    from halo_harness.linux_fixes import install_static_ripgrep
    ok, msg = install_static_ripgrep(dest)
    if ok:
        console.print(f"   [OK] installed rg -> {msg}")
        return msg
    console.print(f"   [WARN] could not install a static rg -- fix: {msg}")
    return None


def _fix_local_bin_path(args, console: Console) -> Optional[str]:
    from halo_harness.linux_fixes import ensure_local_bin_on_rc, local_bin_on_noninteractive_path, rc_file_for_shell
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
    exe = shutil.which("halo") or shutil.which("halo.exe")
    if exe:
        console.print(f"   [OK] halo launcher on PATH: {exe}")
        return
    console.print("   [WARN] halo launcher not found on PATH -- add the directory `uv tool install`/"
                   "`pip install --user` put it in (typically %USERPROFILE%\\.local\\bin, or a venv's own "
                   "Scripts\\ dir), or run `python -m halo_harness` from this checkout instead.")


def _step_linux_fixes(args, console: Console) -> list:
    console.print("Windows PATH:" if sys.platform == "win32" else "Linux fixes:")
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


def _step_summary(console: Console, written: list, pong_ok: bool, doctor_lines: list, *, no_live: bool,
                   configured_this_run: "list[str]", final_model: str) -> None:
    console.print("Summary:")
    if configured_this_run:
        console.print(f"   provider(s) set up this run: {', '.join(configured_this_run)}")
    if final_model:
        console.print(f"   default model: {final_model}")
    if written:
        for w in written:
            console.print(f"   wrote {w}")
    else:
        console.print("   nothing new written (already configured).")
    if no_live or pong_ok:
        console.print("   Run `halo` to start.")
    else:
        hint = _first_fix_hint(doctor_lines)
        console.print(f"   Next: {hint}" if hint else "   Next: run `halo doctor` for details.")
    # Addendum to H15 (owner report from the work VM): `halo` typed
    # OUTSIDE the checkout did not work -- only the checkout's own bin/
    # wrapper had ever been used. The SAME doctor check/fix line ends every
    # init run, success or not, so this is never missed on a first run.
    from halo_harness.doctor import MISSING, WARN, check_command_on_path
    path_line = check_command_on_path()
    console.print(f"   {path_line}")
    if path_line.startswith(WARN) or path_line.startswith(MISSING):
        console.print("   Run it from any directory -- the checkout is only for `git pull`.")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="halo init", add_help=True,
        description="Set up halo in one command: pick a provider to set up, configure its "
                     "credentials, set a default model, run doctor, send a live pong, and offer the "
                     "Linux setup fixes. Repeat for another provider, then pick the overall default.",
    )
    parser.add_argument("--provider", action="append", choices=list(PROVIDERS), default=None,
                         metavar="{databricks,openrouter,anthropic,claude,codex}",
                         help="set up this provider non-interactively (repeatable, first-listed first); "
                              "omit for the interactive provider picker")
    parser.add_argument("--preset", choices=["home", "work", "claude"], default=None,
                         help="deprecated alias for --provider: home=openrouter, work=databricks, "
                              "claude=claude")
    parser.add_argument("--model", default=None, metavar="REF", help="override the provider's own default model")
    parser.add_argument("--yes", action="store_true", help="accept every default without prompting")
    parser.add_argument("--no-live", action="store_true", help="skip the catalog refresh and the live pong")
    parser.add_argument("--no-fixes", action="store_true", help="skip the Linux rg/PATH fixes step")
    parser.add_argument("--team", default=None, metavar="PATH|URL",
                         help="a team.json preset (host/default model/gateway preference/DBU price -- "
                              "never a token); overrides .halo/team.json / ~/.halo/team.json")
    return parser


def _run_provider_setup(provider: str, args, console: Console, cwd: Path,
                         *, write_model: bool) -> "Optional[tuple]":
    """Runs ONE provider's whole path: credentials -> checks/catalog
    refresh -> default-model pick -> live pong. Returns `(model_ref_raw,
    written_paths, doctor_lines, pong_ok)`, or `None` when the provider
    flatly can't be set up right now (today: only `--provider claude`/
    `--preset claude` with no actual claude.ai login -- a usage/config
    error the old single-preset flow also rejected outright)."""
    console.print(f"[bold]{PROVIDER_LABEL[provider].split(' -- ', 1)[0]}[/bold]")
    if provider == "claude" and not claude_login_available():
        console.print("[red]Claude subscription needs a claude.ai login -- run `claude` once to log in, "
                       "or pick a different provider.[/red]")
        return None
    if provider == "codex":
        from halo_harness.agent.cx_runtime import preflight_cx
        err = preflight_cx()
        if err:
            console.print(f"[red]{err}[/red]")
            return None
    written = _step_credentials(provider, args, console)
    # H15 item 21.1: enabled as soon as credentials/a login actually
    # resolve -- BEFORE the catalog refresh/model-pick/live-pong steps
    # below, every one of which resolves a real `--model`-style ref for
    # THIS SAME provider and would otherwise refuse it outright (item
    # 21.3's own refusal rule applies the instant a `providers` block
    # exists, e.g. from the one-time migration above) -- completing a tab
    # is what enables it, but "completing" starts the moment credentials
    # are confirmed present, not only once every later step has also run.
    # Only when the provider actually HAS credentials/a login now (the
    # user may have declined every prompt and left it unconfigured, in
    # which case there's nothing to enable yet).
    #
    # 1.0.1 part 2 fixpass finding 15: `enable_if_was_explicitly_disabled`,
    # never a bare `enable()` -- auto-detection already covers the ordinary
    # case; writing a permanent override here would survive a LATER
    # revocation (a claude.ai logout, a deleted key) that auto-detection
    # alone would otherwise have reflected immediately. Only an existing
    # explicit `enabled: false` (e.g. a previous `providers disable`) gets
    # flipped back to `true` by completing this provider's setup again.
    if provider_status(provider) in ("configured", "logged in"):
        from halo_harness.providers.enablement import enable_if_was_explicitly_disabled
        enable_if_was_explicitly_disabled(provider)
    model_ref_raw, model_path = _step_default_model(provider, args, console, write=write_model)
    if model_path:
        written.append(str(model_path))
    doctor_lines, _doctor_ok = _step_checks(args, console, cwd, provider=provider, model_raw=model_ref_raw)
    model_ref_raw = _step_pick_model(args, console, provider, model_ref_raw, write=write_model)
    pong_ok = True
    if args.no_live:
        console.print("Live pong: skipped (--no-live)")
    else:
        pong_ok = _step_live_pong(model_ref_raw, cwd, console)
    return model_ref_raw, written, doctor_lines, pong_ok


def _run_init_tabs(args, console: Console, cwd: Path) -> "Optional[tuple]":
    """H15 Part A: runs the tabbed provider view; returns `(configured_
    this_run, written, doctor_lines, picked_per_provider)` on success --
    including an ordinary "nothing configured" close (Esc with nothing set
    up still returns a real, empty result, never None) -- or None only
    when the tabs app itself could not run at all (no real terminal after
    all, a Textual import/runtime failure), the same "interactive picker
    failed -> fallback" shape `_step_select_provider`/`_run_entry_picker`
    already use elsewhere in this file.

    1.0.1 part 2 fixpass finding 6: `--team`/`--no-live` are threaded into
    the tabs app (`team`/`no_live`) -- before this fix, both flags were
    silently ignored on the tabs path (the team host never prefilled, its
    gateway/role preferences never applied, and `--no-live` never stopped
    the tabs' own reachability probes/catalog fetches)."""
    try:
        from halo_harness.tui.dialogs.init_tabs import run_init_tabs
        app = run_init_tabs(team=getattr(args, "team", None), no_live=args.no_live)
    except Exception as e:
        console.print(f"[WARN] tabbed provider setup failed ({type(e).__name__}: {e}) -- "
                       f"falling back to the one-provider-at-a-time picker.")
        return None
    configured_this_run = list(app.configured_this_run)
    written: list = list(getattr(app, "written", []) or [])
    for w in getattr(app, "team_warnings", None) or []:
        console.print(f"   [WARN] {w}")
    picked_per_provider: "dict[str, str]" = {}
    for provider in configured_this_run:
        if provider not in PROVIDERS:
            # 1.0.1 part 2 fixpass finding 5: TypeSafe (TAB_PROVIDERS has it,
            # PROVIDERS does not -- it has no default-model concept of its
            # own, see init_providers.py's own TAB_PROVIDERS docstring) --
            # `PROVIDER_LABEL`/`PROVIDER_DEFAULT_MODEL` have no entry for it
            # at all, so indexing either raised a KeyError here, skipping
            # every step after it (default-model/permission-mode/summary).
            continue
        console.print(f"[bold]{PROVIDER_LABEL[provider].split(' -- ', 1)[0]}[/bold] -- configured via its tab")
        note = app.catalog_notes.get(provider)
        if note:
            console.print(f"   catalog: {note}")
        # write=True (matching `_run_provider_setup`'s own `write_model=
        # True` in the sequential flow): with exactly one provider
        # configured this IS the real default, and `_step_finalize_
        # default_model`'s own "only one provider" branch does NOT write
        # again (it assumes a per-provider step already did, same as the
        # sequential flow guarantees) -- a second configured provider's
        # pick below simply overwrites config.json again, then `_step_
        # finalize_default_model`'s own cross-provider pick corrects it.
        picked_per_provider[provider], model_path = _step_default_model(provider, args, console, write=True)
        if model_path:
            written.append(str(model_path))
    doctor_lines, _ok = _step_checks(args, console, cwd)
    return configured_this_run, written, doctor_lines, picked_per_provider


def cmd_init(argv: list) -> int:
    args = _build_parser().parse_args(argv)
    console = Console()

    from halo_harness.providers.config import load_env_file
    load_env_file(_env_file_path())

    # H15 item 21.4: one-time migration before anything else runs -- a box
    # that already had credentials in its OWN env file from before provider
    # enablement existed gets exactly those enabled; a no-op once a
    # `providers` block already exists, however it got there.
    from halo_harness.providers.enablement import ensure_providers_migrated
    migration_note = ensure_providers_migrated()
    if migration_note:
        console.print(f"[dim]{migration_note}[/dim]")

    console.print("[bold]halo init[/bold]")
    cwd = Path.cwd()
    written: list = []
    doctor_lines: list = []
    pong_ok = True
    configured_this_run: "list[str]" = []
    picked_per_provider: "dict[str, str]" = {}

    requested = _requested_providers(args, console)
    interactive_loop = requested is None
    if not interactive_loop and not requested:
        console.print("[red]--provider needs at least one value.[/red]")
        return 2

    # H15 Part A: the tabbed provider view replaces the one-at-a-time
    # picker loop below on a REAL terminal -- same TTY gate every other
    # interactive picker in this file already uses (item 2/5/13's own
    # convention), so a piped/non-tty run (every existing test, any script)
    # is byte-for-byte unaffected and keeps using the sequential loop +
    # numbered fallback exactly as before this item.
    if interactive_loop and sys.stdin.isatty() and sys.stdout.isatty() and not args.yes:
        tabs_result = _run_init_tabs(args, console, cwd)
        if tabs_result is not None:
            configured_this_run, written, doctor_lines, picked_per_provider = tabs_result
            pong_ok = True
            final_model = _step_finalize_default_model(args, console, configured_this_run, picked_per_provider)
            _step_pick_permission_mode(args, console)
            written.extend(_step_linux_fixes(args, console))
            _step_summary(console, written, pong_ok, doctor_lines, no_live=args.no_live,
                          configured_this_run=configured_this_run, final_model=final_model)
            return 0 if (args.no_live or pong_ok) else 1
        # The tabs app failed to run at all (not a provider failure --
        # see _run_init_tabs's own docstring) -- fall through to the
        # sequential loop below, same "picker failed -> fallback" shape
        # every OTHER interactive picker in this file already uses.

    # 2.0.1 launch-hang fix: `claude_login_available()` (read below by
    # `_step_select_provider`/`detect_default_provider`, `_run_provider_
    # setup`'s own "claude" branch, and `configured_providers()`) is now
    # cache-only -- it never spawns `claude auth status` itself any more.
    # Only the paths that reach HERE (never the tabs branch above, which
    # already does its own live check off a worker thread -- see
    # tui/dialogs/init_tabs.py's `_claude_state_worker`/`_save_claude_
    # worker`) are a plain console flow with no first paint to protect, so
    # one explicit, synchronous, best-effort live refresh right here keeps
    # every one of those reads accurate, exactly like before this fix,
    # instead of always seeing a cold cache.
    from halo_harness.providers.cc_models import refresh_cached_claude_auth_status
    from halo_harness.providers.cx_models import refresh_cached_codex_login_status
    try:
        refresh_cached_claude_auth_status()
        refresh_cached_codex_login_status()
    except Exception:
        pass

    if interactive_loop:
        while True:
            header = ("Select a provider to set up:" if not configured_this_run
                      else "Select another provider to set up (or Done):")
            choice = _step_select_provider(args, console, header=header)
            if choice is None or choice == "done":
                break
            # `write_model` is always True here -- with only one provider
            # configured so far this IS the real default; if a second one
            # gets configured later, `_step_finalize_default_model` below
            # overwrites config.json with its own single cross-provider pick.
            result = _run_provider_setup(choice, args, console, cwd, write_model=True)
            if result is None:
                continue  # error already printed -- back to the picker
            model_ref_raw, prov_written, prov_doctor, prov_pong_ok = result
            written.extend(prov_written)
            doctor_lines = prov_doctor
            pong_ok = prov_pong_ok
            configured_this_run.append(choice)
            picked_per_provider[choice] = model_ref_raw
            if args.yes or not sys.stdin.isatty():
                break  # never loop in a non-interactive run
            if not _confirm(args, console, "Set up another provider?", default=False):
                break
    else:
        for choice in requested:
            result = _run_provider_setup(choice, args, console, cwd, write_model=True)
            if result is None:
                return 2
            model_ref_raw, prov_written, prov_doctor, prov_pong_ok = result
            written.extend(prov_written)
            doctor_lines = prov_doctor
            pong_ok = prov_pong_ok
            configured_this_run.append(choice)
            picked_per_provider[choice] = model_ref_raw

    final_model = _step_finalize_default_model(args, console, configured_this_run, picked_per_provider)

    _step_pick_permission_mode(args, console)  # 1.0.1 hotfix 18.1

    written.extend(_step_linux_fixes(args, console))

    _step_summary(console, written, pong_ok, doctor_lines, no_live=args.no_live,
                  configured_this_run=configured_this_run, final_model=final_model)

    if not args.no_live and not pong_ok:
        return 1
    return 0
