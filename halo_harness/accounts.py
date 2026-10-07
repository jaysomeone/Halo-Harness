"""Subscription account profiles managed by Halo.

Each profile gives an official provider CLI its own configuration directory.
Halo never reads or copies the credentials stored there; it only launches the
provider CLI with the profile directory selected and asks the CLI for status.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

_LABEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_CODEX_CONFIG = 'cli_auth_credentials_store = "file"\n'


class AccountSetupError(ValueError):
    """A profile could not be created or authenticated safely."""


@dataclass(frozen=True)
class AccountProfile:
    provider: str
    name: str
    profile_dir: Path
    config_home: Path
    created_at: Optional[int] = None


def accounts_root(state_dir: Optional[Path] = None) -> Path:
    if state_dir is None:
        from halo_harness.config.paths import bridge_home
        state_dir = bridge_home()
    return Path(state_dir) / "accounts"


def validate_account_name(name: str) -> str:
    value = (name or "").strip()
    if not _LABEL_RE.fullmatch(value):
        raise AccountSetupError(
            "account name must be 1-64 letters, numbers, dots, dashes, or underscores "
            "and must start with a letter or number"
        )
    return value


def codex_profile(name: str, *, state_dir: Optional[Path] = None) -> AccountProfile:
    name = validate_account_name(name)
    profile_dir = accounts_root(state_dir) / "codex" / name
    created_at = None
    metadata = profile_dir / "account.json"
    try:
        data = json.loads(metadata.read_text(encoding="utf-8"))
        value = data.get("created_at")
        if isinstance(value, int):
            created_at = value
    except (OSError, json.JSONDecodeError, AttributeError):
        pass
    return AccountProfile("codex", name, profile_dir, profile_dir / "codex-home", created_at)


def prepare_codex_profile(name: str, *, state_dir: Optional[Path] = None) -> AccountProfile:
    profile = codex_profile(name, state_dir=state_dir)
    profile.config_home.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        os.chmod(profile.profile_dir, 0o700)
        os.chmod(profile.config_home, 0o700)
    except OSError:
        pass

    config_path = profile.config_home / "config.toml"
    if not config_path.exists():
        config_path.write_text(_CODEX_CONFIG, encoding="utf-8")
        try:
            os.chmod(config_path, 0o600)
        except OSError:
            pass

    metadata_path = profile.profile_dir / "account.json"
    created_at = profile.created_at or int(time.time())
    if not metadata_path.exists():
        metadata_path.write_text(json.dumps({"provider": "codex", "name": profile.name,
                                             "created_at": created_at}, indent=2) + "\n", encoding="utf-8")
        try:
            os.chmod(metadata_path, 0o600)
        except OSError:
            pass
    return AccountProfile(profile.provider, profile.name, profile.profile_dir, profile.config_home, created_at)


def list_account_profiles(*, state_dir: Optional[Path] = None) -> list[AccountProfile]:
    root = accounts_root(state_dir)
    out: list[AccountProfile] = []
    provider_dir = root / "codex"
    try:
        entries = sorted(p for p in provider_dir.iterdir() if p.is_dir())
    except OSError:
        entries = []
    for entry in entries:
        try:
            out.append(codex_profile(entry.name, state_dir=state_dir))
        except AccountSetupError:
            continue
    return out


def _selection_path(state_dir: Optional[Path] = None) -> Path:
    return accounts_root(state_dir) / "active.json"


def active_account_name(provider: str, *, state_dir: Optional[Path] = None) -> Optional[str]:
    """Return the selected managed profile name without touching credentials."""
    try:
        data = json.loads(_selection_path(state_dir).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    value = data.get(provider) if isinstance(data, dict) else None
    if not isinstance(value, str):
        return None
    try:
        profile = codex_profile(value, state_dir=state_dir) if provider == "codex" else None
    except AccountSetupError:
        return None
    return value if profile is not None and profile.profile_dir.is_dir() else None


def set_active_account(provider: str, name: str, *, state_dir: Optional[Path] = None) -> None:
    """Persist one provider's active profile; this file contains no secrets."""
    if provider != "codex":
        raise AccountSetupError(f"unsupported account provider: {provider}")
    profile = codex_profile(name, state_dir=state_dir)
    if not profile.profile_dir.is_dir():
        raise AccountSetupError(f"Codex account {profile.name!r} does not exist")
    path = _selection_path(state_dir)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    data[provider] = profile.name
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    try:
        os.chmod(path.parent, 0o700)
        os.chmod(path, 0o600)
    except OSError:
        pass


def active_codex_profile(*, state_dir: Optional[Path] = None) -> Optional[AccountProfile]:
    """Selected managed profile, or the first profile when none was selected yet."""
    profiles = list_account_profiles(state_dir=state_dir)
    if not profiles:
        return None
    selected = active_account_name("codex", state_dir=state_dir)
    return next((profile for profile in profiles if profile.name == selected), profiles[0])


def codex_profile_env(profile: AccountProfile, base_env: Optional[dict] = None) -> dict:
    from halo_harness.providers.cx_models import cx_child_env
    env = cx_child_env(dict(os.environ) if base_env is None else dict(base_env))
    env["CODEX_HOME"] = str(profile.config_home)
    return env


def _profile_limit_is_active(profile: AccountProfile, *, now: Optional[float] = None) -> bool:
    from halo_harness.providers.cx_models import load_cx_models_cache
    limits = load_cx_models_cache(profile.profile_dir).get("rate_limits") or {}
    if not isinstance(limits, dict):
        return False
    now = time.time() if now is None else now
    windows = [limits.get(key) for key in ("primary", "secondary")]
    active = [w for w in windows if isinstance(w, dict) and
              (not isinstance(w.get("resets_at"), (int, float)) or w.get("resets_at") > now)]
    if any(isinstance(w.get("used_percent"), (int, float)) and w["used_percent"] >= 100 for w in active):
        return True
    # A reached primary window must not borrow a still-open secondary
    # window's reset time. Workspace credit errors have no window reset
    # semantics; a cached one cannot prove today's balance is exhausted.
    reached = limits.get("reached")
    return reached in ("primary", "secondary") and limits.get(reached) in active


def next_codex_profile(current_name: Optional[str], *, state_dir: Optional[Path] = None,
                       login_status: Optional[Callable] = None, now: Optional[float] = None,
                       base_env: Optional[dict] = None,
                       excluded_names: Optional[set[str]] = None) -> Optional[AccountProfile]:
    """Find another subscription login, preferring apparently available profiles.

    Cached limits are advisory: a top-up, reset, or credit-backed request
    can make a profile usable again. Try cached-exhausted profiles last,
    once per turn, before concluding that every account is unavailable.
    """
    from halo_harness.providers.cx_models import codex_login_status, is_subscription_login
    profiles = list_account_profiles(state_dir=state_dir)
    if not profiles:
        return None
    names = [profile.name for profile in profiles]
    if current_name in names:
        pos = names.index(current_name) + 1
        profiles = profiles[pos:] + profiles[:pos]
    check = login_status or codex_login_status
    excluded = set(excluded_names or ())
    profiles.sort(key=lambda p: _profile_limit_is_active(p, now=now))
    for profile in profiles:
        if profile.name == current_name or profile.name in excluded:
            continue
        if is_subscription_login(check(env=codex_profile_env(profile, base_env))):
            return profile
    return None


def codex_failover_unavailable_message(attempted_names: set[str], *,
                                      state_dir: Optional[Path] = None) -> str:
    """Explain exhausted logins separately from missing/unusable profiles."""
    names = {p.name for p in list_account_profiles(state_dir=state_dir)}
    if names and names <= attempted_names:
        attempted = ", ".join(sorted(names))
        return (f"All configured Codex accounts were tried this turn ({attempted}) and reported usage/credit limits. "
                "Work is saved; continue after an account resets or has credits available, "
                "or add another subscription with /accounts add codex <name>.")
    if names - attempted_names:
        unavailable = ", ".join(sorted(names - attempted_names))
        return (f"Other configured Codex profiles could not provide a verified ChatGPT login ({unavailable}). "
                "Check /accounts and their login status. Work is saved.")
    return ("No other Codex subscription profile is configured. Work is saved; "
            "add one with /accounts add codex <name>.")


def add_codex_account(name: str, *, state_dir: Optional[Path] = None,
                      run: Callable = subprocess.run, login_status: Optional[Callable] = None,
                      base_env: Optional[dict] = None) -> tuple[AccountProfile, str]:
    """Create one isolated profile and run the official interactive login."""
    from halo_harness.providers.cx_models import codex_login_status, resolve_codex_launch_argv

    existing = codex_profile(name, state_dir=state_dir)
    if (existing.config_home / "auth.json").exists():
        raise AccountSetupError(
            f"Codex account {existing.name!r} already has a saved login; choose a different name"
        )
    profile = prepare_codex_profile(name, state_dir=state_dir)
    env = codex_profile_env(profile, base_env)
    argv = resolve_codex_launch_argv()
    try:
        completed = run(argv + ["login"], env=env, check=False)
    except OSError as exc:
        raise AccountSetupError(f"could not start Codex login: {exc}") from exc
    if getattr(completed, "returncode", 1) != 0:
        raise AccountSetupError(f"Codex login exited with status {completed.returncode}")

    check_status = login_status or codex_login_status
    status = check_status(env=env)
    if status is None or not status.logged_in:
        raise AccountSetupError("Codex login finished, but this profile is not logged in")
    if status.method != "chatgpt":
        raise AccountSetupError("this profile used an API key; choose Sign in with ChatGPT for subscription access")
    if active_account_name("codex", state_dir=state_dir) is None:
        set_active_account("codex", profile.name, state_dir=state_dir)
    return profile, status.text or "Logged in using ChatGPT"


def format_accounts(*, state_dir: Optional[Path] = None) -> str:
    profiles = list_account_profiles(state_dir=state_dir)
    if not profiles:
        return ("No managed subscription accounts yet.\n"
                "Add one with: /accounts add codex <name>\n"
                "Example: /accounts add codex personal")
    selected = active_account_name("codex", state_dir=state_dir)
    lines = ["Managed subscription accounts:"]
    for profile in profiles:
        auth_file = profile.config_home / "auth.json"
        state = "login saved" if auth_file.exists() else "login not verified"
        active = " · active" if profile.name == selected else ""
        lines.append(f"  codex  {profile.name}  ({state}{active})")
    return "\n".join(lines)
