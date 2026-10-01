"""halo_harness.team_config -- H14 scope I: a shared `team.json` preset (the
brief's "the team just inserts their databricks token and they're off"). It
holds host, default model, per-family gateway preference and a DBU price --
**never a token, never an endpoint list** (the workspace listing is
discovered per user by `init --preset work`/`models --refresh` and cached to
`~/.halo/dbx-endpoints.json`; team.json is not a substitute for that).

Discovery order: `--team <path|url>` (explicit, always wins), else
`<cwd>/.halo/team.json` (checked into the project, alongside
`.claude/`), else `~/.halo/team.json` (a personal copy/override, not
project-specific). None of these existing is normal, not an error -- a lone
user with no team preset still runs `init --preset work` off Claude Code's
own settings env alone.
"""

from __future__ import annotations

import json
import sys
import urllib.request
from pathlib import Path
from typing import Optional

_ALLOWED_KEYS = frozenset({"host", "default_model", "gateway_preference", "dbu_price_usd", "roles"})
_FORBIDDEN_KEY_HINTS = ("token", "secret", "key", "password", "credential", "auth")


def team_config_path(cwd: Path, *, team_flag: Optional[str] = None) -> Optional[str]:
    """The path/URL that would be used, or None if nothing is configured
    and no `--team` was given (`team_flag` itself, even a bad one, always
    wins -- the caller reports its own read/parse failure rather than
    silently falling through to project/user discovery)."""
    if team_flag:
        return team_flag
    project = Path(cwd) / ".halo" / "team.json"
    if project.exists():
        return str(project)
    # 2.0.0 fixpass finding 5: the project preset moved from
    # `<cwd>/.rolo-claude/team.json` with no fallback -- a repo whose
    # checked-in preset still lives at the OLD path (nobody has run `git
    # mv` on it yet) must keep being found, not silently ignored.
    legacy_project = Path(cwd) / ".rolo-claude" / "team.json"
    if legacy_project.exists():
        print(f"halo: {legacy_project} is deprecated, move it to {project} "
              f"(.rolo-claude -> .halo)", file=sys.stderr)
        return str(legacy_project)
    from halo_harness.config.paths import bridge_home
    user = bridge_home() / "team.json"
    if user.exists():
        return str(user)
    return None


def _read_source(source: str) -> str:
    if source.startswith("http://") or source.startswith("https://"):
        # 1.0.1 hotfix 11: urlopen_tls -- see providers/http.py's own docstring.
        from halo_harness.providers.http import urlopen_tls
        with urlopen_tls(source, timeout=10) as resp:  # noqa: S310 -- explicit, user-initiated
            return resp.read().decode("utf-8", "replace")
    return Path(source).read_text(encoding="utf-8")


def load_team_config(cwd: Path, *, team_flag: Optional[str] = None) -> "tuple[Optional[dict], list[str]]":
    """`(config_or_None, warnings)`. `warnings` names anything dropped (an
    unrecognized key, and ESPECIALLY a token/secret-shaped one -- team.json
    is meant to be checked into a shared repo/wiki, so a key that looks
    like a credential is stripped and loudly warned about, never silently
    accepted). Returns `(None, [])` when nothing is configured; a source
    that exists but fails to read/parse returns `(None, [<error>])` instead
    of raising -- discovery must never crash `init`/`models --refresh`."""
    source = team_config_path(cwd, team_flag=team_flag)
    if source is None:
        return None, []
    try:
        raw = _read_source(source)
        data = json.loads(raw)
    except Exception as e:
        return None, [f"could not read team config {source!r}: {type(e).__name__}: {e}"]
    if not isinstance(data, dict):
        return None, [f"team config {source!r} is not a JSON object -- ignored"]
    warnings = []
    cleaned = {}
    for key, value in data.items():
        if any(hint in key.lower() for hint in _FORBIDDEN_KEY_HINTS):
            warnings.append(f"team config {source!r}: dropped {key!r} -- team.json must never hold a token/secret")
            continue
        if key not in _ALLOWED_KEYS:
            warnings.append(f"team config {source!r}: unrecognized key {key!r} ignored")
            continue
        cleaned[key] = value
    return (cleaned or None), warnings


def apply_gateway_preference(gateway_preference: dict) -> None:
    """Seeds each `databricks.gateway.<endpoint>` config knob (scope D's
    own per-model anthropic-opt-in override) from team.json's
    `gateway_preference` map -- idempotent, never overwrites a value the
    user already set locally for that same endpoint (a personal
    `config.json` override always wins over the shared team default)."""
    from halo_harness.theme import get_config_value, set_config_value
    for endpoint, preference in (gateway_preference or {}).items():
        key = f"databricks.gateway.{endpoint}"
        if get_config_value(key, default=None) is None:
            set_config_value(key, preference)
