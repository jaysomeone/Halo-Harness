"""halo_harness.providers.openai_catalog -- Halo 2.0.3 round 5i part 1:
`GET /v1/models` on the real OpenAI API, cached in its own state file with
a TTL -- mirrors `providers.huggingface_catalog` (itself modeled on
`providers.databricks.probe_openrouter_models`/`write_models_json`/
`refresh_openrouter_catalog_if_stale`). Kept in its OWN file (`openai-
models.json`, never `models.json`/`huggingface-models.json`) so an id that
happens to collide across hosts never cross-contaminates a shared flat
file -- same reasoning `hf_models_json_path`'s own docstring gives.

`docs/harness/OPENAI-RESEARCH.md` section 3 (confirmed live, 2026-10-04):
`GET /v1/models` returns `{"object":"list","data":[{"id","object",
"created","owned_by",...}]}` -- NO context length, NO pricing, on this
endpoint at all. This probe therefore only ever proves "this key can see
this id" (the picker group / `halo providers` model-count column); price
and context for `model.resolve_model_profile` come from the SEPARATE
models.dev cross-check (`providers.models_dev.load_vendored_openai_
fallback` / a refreshed `models-dev.json`), never from this file.
"""

from __future__ import annotations

import http.client
import json
import socket
import ssl
import threading
import time
import urllib.parse
from pathlib import Path
from typing import Optional

_CATALOG_READ_TIMEOUT_S = 30
_AUTO_REFRESH_BACKOFF_S = 300.0


def probe_openai_models(base_url: str, api_key: str) -> "list[str]":
    """`GET {base_url}/models` -- returns the bare `id` list (order as the
    API sent it). Raises `UpstreamConnectError` on connect/DNS failure,
    `RuntimeError` on a non-200, same contract as `probe_huggingface_
    models`/`probe_openrouter_models`."""
    from halo_harness.providers.http import UpstreamConnectError, format_connect_error, open_upstream
    parsed = urllib.parse.urlparse(base_url)
    host = parsed.hostname
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    tls = parsed.scheme == "https"
    path = parsed.path.rstrip("/") + "/models"
    headers = {"Authorization": f"Bearer {api_key}", "Accept-Encoding": "identity"}
    conn = None
    try:
        conn = open_upstream(host, port, tls)
        if conn.sock:
            conn.sock.settimeout(_CATALOG_READ_TIMEOUT_S)
        conn.request("GET", path, headers=headers)
        resp = conn.getresponse()
        raw = resp.read()
    except (OSError, socket.timeout, ssl.SSLError, http.client.HTTPException) as e:
        raise UpstreamConnectError(format_connect_error(host, e), host=host) from e
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
    if resp.status != 200:
        raise RuntimeError(f"OpenAI /models returned {resp.status}")
    data = json.loads(raw.decode("utf-8", "replace"))
    out = []
    for entry in data.get("data", []) if isinstance(data, dict) else []:
        mid = entry.get("id") if isinstance(entry, dict) else None
        if isinstance(mid, str) and mid:
            out.append(mid)
    return out


def oai_models_json_path(state_dir) -> Path:
    return Path(state_dir) / "openai-models.json"


def write_oai_models_json(state_dir, model_ids: "list[str]") -> None:
    """Write openai-models.json as {"<id>": {}, ...} (an empty per-id dict
    -- see this module's own docstring on why there is nothing else to
    store from this probe). Best-effort; swallows OSError."""
    try:
        Path(state_dir).mkdir(parents=True, exist_ok=True)
        out = {mid: {} for mid in model_ids}
        path = oai_models_json_path(state_dir)
        tmp_path = path.with_name(f".{path.name}.tmp")
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(out, f, indent=2)
        import os
        os.replace(tmp_path, path)
    except OSError:
        pass


def load_oai_models_json(state_dir) -> dict:
    """Read openai-models.json (dict keyed by id); {} if missing/
    unparseable. Pure file read, no network."""
    path = oai_models_json_path(state_dir)
    if not path.exists():
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def oai_models_json_age_seconds(state_dir) -> Optional[float]:
    path = oai_models_json_path(state_dir)
    try:
        return time.time() - path.stat().st_mtime
    except OSError:
        return None


_oai_refresh_lock = threading.Lock()
_oai_last_failure_at: "dict[str, float]" = {}


def refresh_openai_catalog_if_stale(state_dir, *, max_age_hours: Optional[float] = None,
                                     force: bool = False, env: Optional[dict] = None) -> Optional[bool]:
    """The OpenAI twin of `refresh_huggingface_catalog_if_stale` -- same
    shared staleness knob (`databricks.catalog_max_age_hours`), same
    single-flight lock/post-failure backoff shape, same `None`-means-
    nothing-needed-doing / `True`/`False` contract. `None` when
    `OPENAI_API_KEY` isn't set -- nothing to refresh."""
    key = str(state_dir)
    try:
        if max_age_hours is None:
            from halo_harness.theme import get_config_value
            max_age_hours = get_config_value("databricks.catalog_max_age_hours", default=24)
        age = oai_models_json_age_seconds(state_dir)
        always = float(max_age_hours) <= 0
        if age is not None:
            age = max(0.0, float(age))
        if not force and not always and age is not None and age < float(max_age_hours) * 3600:
            return None
        last_failure_at = _oai_last_failure_at.get(key)
        if not force and last_failure_at is not None and (time.monotonic() - last_failure_at) < _AUTO_REFRESH_BACKOFF_S:
            return None
        from halo_harness.providers.config import resolve_openai
        oai = resolve_openai(env)
        if oai is None:
            return None
        from halo_harness.providers.databricks import CATALOG_REFRESH_BUSY
        if not _oai_refresh_lock.acquire(blocking=False):
            return CATALOG_REFRESH_BUSY
        try:
            fetched = probe_openai_models(oai.base_url, oai.api_key)
            write_oai_models_json(state_dir, fetched)
            _oai_last_failure_at.pop(key, None)
            return True
        except Exception:
            _oai_last_failure_at[key] = time.monotonic()
            return False
        finally:
            _oai_refresh_lock.release()
    except Exception:
        return False


def oai_picker_fields(model_id: str, state_dir) -> dict:
    """`{"context_tokens", "price_in_per_m", "price_out_per_m"}` (any
    subset, possibly empty) for the `/model` picker's OpenAI group --
    sourced from models.dev (the refreshed `~/.halo/models-dev.json` cache
    first, else the vendored package fallback), NEVER from this module's
    own `GET /v1/models` probe (which carries no price/context at all --
    see this module's own docstring). models.dev's `cost.input`/`output`
    are already USD-per-MILLION-tokens, matching the picker's own column
    units directly (no /1e6 needed here, unlike `model.ModelProfile`'s
    per-token fields)."""
    from halo_harness.providers.models_dev import (
        load_models_dev_json, load_vendored_openai_fallback, openai_entries_from_full_models_dev,
    )
    refreshed = openai_entries_from_full_models_dev(load_models_dev_json(state_dir)).get(model_id)
    entry = refreshed or load_vendored_openai_fallback().get(model_id)
    if not isinstance(entry, dict):
        return {}
    limit = entry.get("limit") if isinstance(entry.get("limit"), dict) else {}
    cost = entry.get("cost") if isinstance(entry.get("cost"), dict) else {}
    out = {}
    if isinstance(limit.get("context"), int):
        out["context_tokens"] = limit["context"]
    if isinstance(cost.get("input"), (int, float)):
        out["price_in_per_m"] = cost["input"]
    if isinstance(cost.get("output"), (int, float)):
        out["price_out_per_m"] = cost["output"]
    return out
