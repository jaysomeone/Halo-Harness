"""halo_harness.providers.anthropic_catalog -- H15 part 2 addendum 3.2: a
live catalog cache for the `ant:` (direct Anthropic API key) provider,
fetched from the real `GET /v1/models` the same way OpenRouter's own
models.json / Databricks' own dbx-endpoints.json already are -- so a box
that only ever sets ANTHROPIC_API_KEY (no `halo init` run) still
gets a live catalog without any extra step, the same "already finds the
available keys ... and uses those" promise the rest of this addendum makes.

The `ant:`/`cc:` routed models themselves stay on the static nine-alias
table (`providers.cc_models`) -- real, pinned Claude model ids a live
catalog listing would not improve on -- this cache exists so `/providers`/
doctor/a future picker can show what the key can ACTUALLY see today
without an extra network round trip of their own.
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Optional

# 1.0.1 part 2 fixpass finding 11: same pair every catalog probe in this
# tree now uses -- see providers/databricks.py's own module-level comment.
_CATALOG_READ_TIMEOUT_S = 30
_ant_refresh_lock = threading.Lock()
_ant_last_failure_at: "dict[str, float]" = {}  # keyed by str(state_dir)
_AUTO_REFRESH_BACKOFF_S = 300.0


def fetch_anthropic_models(base_url: str, api_key: str) -> "list[dict]":
    """`GET {base_url}/v1/models` -- returns a list of `{"id", "display_name",
    "created_at"}` dicts (whatever fields the response carries; this harness
    only ever reads `id`/`display_name` back out). Raises UpstreamConnectError
    on connect/DNS failure, same bounded-connect contract every other probe
    in this harness uses."""
    import urllib.parse
    from halo_harness.providers.http import format_connect_error, open_upstream, UpstreamConnectError
    parsed = urllib.parse.urlparse(base_url)
    host = parsed.hostname
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    tls = parsed.scheme == "https"
    path = parsed.path.rstrip("/") + "/v1/models"
    headers = {"x-api-key": api_key, "anthropic-version": "2023-06-01", "Accept-Encoding": "identity"}
    conn = None
    try:
        conn = open_upstream(host, port, tls)
        # finding 11: 30s, never open_upstream's own 300s idle timeout.
        if conn.sock:
            conn.sock.settimeout(_CATALOG_READ_TIMEOUT_S)
        conn.request("GET", path, headers=headers)
        resp = conn.getresponse()
        raw = resp.read()
    except Exception as e:
        import http.client
        import socket
        import ssl
        if isinstance(e, (OSError, socket.timeout, ssl.SSLError, http.client.HTTPException)):
            raise UpstreamConnectError(format_connect_error(host, e), host=host) from e
        raise
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
    if resp.status != 200:
        raise RuntimeError(f"Anthropic /v1/models returned {resp.status}")
    data = json.loads(raw.decode("utf-8", "replace"))
    out = []
    for entry in data.get("data", []):
        out.append({"id": entry.get("id"), "display_name": entry.get("display_name")})
    return out


def ant_models_json_path(state_dir) -> Path:
    return Path(state_dir) / "ant-models.json"


def write_ant_models_json(state_dir, models: "list[dict]") -> None:
    """Best-effort; swallows OSError -- same contract as `write_models_json`.

    1.0.1 part 2 fixpass finding 11: tmp file + `os.replace` (atomic on
    both POSIX and Windows for a same-filesystem rename) -- a reader never
    observes a truncated file mid-write."""
    try:
        path = ant_models_json_path(state_dir)
        path.parent.mkdir(parents=True, exist_ok=True)
        out = {m["id"]: {"display_name": m.get("display_name")} for m in models if m.get("id")}
        tmp_path = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(out, f, indent=2)
        os.replace(tmp_path, path)
    except OSError:
        pass


def load_ant_models_json(state_dir) -> dict:
    path = ant_models_json_path(state_dir)
    if not path.exists():
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def ant_models_age_seconds(state_dir) -> Optional[float]:
    path = ant_models_json_path(state_dir)
    try:
        return time.time() - path.stat().st_mtime
    except OSError:
        return None


def refresh_anthropic_catalog_if_stale(state_dir, *, max_age_hours: Optional[float] = None,
                                        force: bool = False, env: "Optional[dict]" = None) -> Optional[bool]:
    """The `ant:` mirror of `refresh_dbx_catalog_if_stale`/
    `refresh_openrouter_catalog_if_stale` -- `None` when nothing needed
    doing (not stale, or no ANTHROPIC_API_KEY configured), else whether the
    refresh actually succeeded. Never raises.

    `env` (N2c, 1.0.1 final pass): forwarded to `resolve_anthropic` -- see
    `providers.databricks.refresh_dbx_catalog_if_stale`'s own docstring for
    the full rationale (`None` keeps every pre-existing call site unchanged).

    1.0.1 part 2 fixpass finding 11: single-flight + post-failure backoff
    for AUTO (`force=False`) callers, same shape as `providers.databricks`'s
    own refresh pair -- see that module's docstrings for the full rationale."""
    key = str(state_dir)
    try:
        if max_age_hours is None:
            from halo_harness.theme import get_config_value
            max_age_hours = get_config_value("databricks.catalog_max_age_hours", default=24)
        age = ant_models_age_seconds(state_dir)
        always = float(max_age_hours) <= 0
        if age is not None:
            age = max(0.0, float(age))
        if not force and not always and age is not None and age < float(max_age_hours) * 3600:
            return None
        # M1 (1.0.1 final pass): time.monotonic() counts from OS boot -- see
        # providers.databricks.refresh_dbx_catalog_if_stale's own comment.
        # None (never recorded) must never read as "just failed at t=0".
        last_failure_at = _ant_last_failure_at.get(key)
        if not force and last_failure_at is not None and (time.monotonic() - last_failure_at) < _AUTO_REFRESH_BACKOFF_S:
            return None
        from halo_harness.providers.config import resolve_anthropic
        ant = resolve_anthropic(env)
        if ant is None:
            return None
        if not _ant_refresh_lock.acquire(blocking=False):
            return False
        try:
            fetched = fetch_anthropic_models(ant.base_url, ant.api_key)
            write_ant_models_json(state_dir, fetched)
            _ant_last_failure_at.pop(key, None)
            return True
        except Exception:
            _ant_last_failure_at[key] = time.monotonic()
            return False
        finally:
            _ant_refresh_lock.release()
    except Exception:
        return False
