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


def codex_profile_env(profile: AccountProfile, base_env: Optional[dict] = None) -> dict:
    from halo_harness.providers.cx_models import cx_child_env
    env = cx_child_env(dict(os.environ) if base_env is None else dict(base_env))
    env["CODEX_HOME"] = str(profile.config_home)
    return env


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
    return profile, status.text or "Logged in using ChatGPT"


def format_accounts(*, state_dir: Optional[Path] = None) -> str:
    profiles = list_account_profiles(state_dir=state_dir)
    if not profiles:
        return ("No managed subscription accounts yet.\n"
                "Add one with: /accounts add codex <name>\n"
                "Example: /accounts add codex personal")
    lines = ["Managed subscription accounts:"]
    for profile in profiles:
        auth_file = profile.config_home / "auth.json"
        state = "login saved" if auth_file.exists() else "login not verified"
        lines.append(f"  codex  {profile.name}  ({state})")
    return "\n".join(lines)
