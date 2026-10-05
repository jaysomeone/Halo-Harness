"""halo_harness.providers.huggingface_catalog -- Halo 2.0.3 round 4: the
router's `GET /v1/models` catalog, cached in the state dir with a TTL,
mirroring `providers.databricks.probe_openrouter_models`/`write_models_
json`/`refresh_openrouter_catalog_if_stale`. Split out of `providers.
huggingface` (which owns `huggingface.endpoints`/`huggingface.bill_to`
config resolution) purely to keep each file's own writes under the house
250-line-per-write habit while this was being built -- both modules are
Halo 2.0.3 round 4, same design doc.

The router's exact `GET /v1/models` per-entry field names were NOT
confirmed by any fetched page this round (`docs/harness/LOCAL-MODELS-
RESEARCH.md` section 9: "GET /v1/models ... returns ... pricing, context
length, latency, and throughput where available" -- no concrete example
body). `_parse_catalog_entry` below ASSUMES the same shape `probe_
openrouter_models` already parses (OpenRouter's own `/api/v1/models`:
top-level `context_length`/`pricing.{prompt,completion}` per entry), since
both are multi-provider routers and this is the closest confirmed
precedent -- a DOCUMENTED ASSUMPTION (see docs/MODELS.md), not a confirmed
fact; the parser degrades to omitted fields rather than raising if a real
response differs.
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


def _parse_catalog_entry(entry: dict) -> Optional[dict]:
    """One `GET /v1/models` row -> `{"id":, "context_length":, "pricing":}`
    (the two latter keys omitted when absent). Falls back to scanning a
    nested `providers`/`endpoints` list's first entry carrying either field
    when the top level has neither -- a router response plausibly nests
    per-provider numbers instead of flattening them; degrades gracefully
    either way instead of raising."""
    if not isinstance(entry, dict) or not entry.get("id"):
        return None
    context_length = entry.get("context_length")
    pricing = entry.get("pricing") if isinstance(entry.get("pricing"), dict) else None
    if context_length is None or pricing is None:
        for nest_key in ("providers", "endpoints"):
            nested = entry.get(nest_key)
            if isinstance(nested, list):
                for row in nested:
                    if not isinstance(row, dict):
                        continue
                    if context_length is None and isinstance(row.get("context_length"), int):
                        context_length = row["context_length"]
                    if pricing is None and isinstance(row.get("pricing"), dict):
                        pricing = row["pricing"]
    out = {"id": entry["id"]}
    if isinstance(context_length, int):
        out["context_length"] = context_length
    if isinstance(pricing, dict):
        out["pricing"] = pricing
    return out


def probe_huggingface_models(base_url: str, api_key: str) -> "list[dict]":
    """`GET {base_url}/models` on the router -- returns a `probe_openrouter_
    models`-shaped list (`id`, optional `context_length`/`pricing`). Raises
    `UpstreamConnectError` on connect/DNS failure, `RuntimeError` on a
    non-200, same contract as `probe_openrouter_models`."""
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
        raise RuntimeError(f"Hugging Face /models returned {resp.status}")
    data = json.loads(raw.decode("utf-8", "replace"))
    out = []
    for entry in data.get("data", []) if isinstance(data, dict) else []:
        parsed_entry = _parse_catalog_entry(entry)
        if parsed_entry is not None:
            out.append(parsed_entry)
    return out


def hf_models_json_path(state_dir) -> Path:
    """Deliberately a SEPARATE file from `providers.databricks.models_json_
    path` (OpenRouter's `models.json`): both files are bare-id-keyed with
    no provider namespace of their own, and an OpenRouter id colliding with
    a Hugging Face one (e.g. the same `org/model` string served by both)
    would silently cross-contaminate a shared flat file -- kept apart
    instead so `model.resolve_model_profile`'s huggingface branch only ever
    reads what THIS probe actually wrote."""
    return Path(state_dir) / "huggingface-models.json"


def write_hf_models_json(state_dir, models: "list[dict]") -> None:
    """Write huggingface-models.json as {"<id>": {"context_length":,
    "pricing":}, ...}, same tmp-file-plus-replace atomicity as `providers.
    databricks.write_models_json`. Best-effort; swallows OSError."""
    try:
        Path(state_dir).mkdir(parents=True, exist_ok=True)
        out = {}
        for m in models:
            mid = m.get("id")
            if not mid:
                continue
            entry = {}
            if "context_length" in m:
                entry["context_length"] = m["context_length"]
            if "pricing" in m:
                entry["pricing"] = m["pricing"]
            out[mid] = entry
        path = hf_models_json_path(state_dir)
        tmp_path = path.with_name(f".{path.name}.tmp")
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(out, f, indent=2)
        import os
        os.replace(tmp_path, path)
    except OSError:
        pass


def load_hf_models_json(state_dir) -> dict:
    """Read huggingface-models.json (dict keyed by id); {} if missing/
    unparseable. Pure file read, no network -- the picker's first paint
    (`Controller.list_models()`) calls this directly, never the probe."""
    path = hf_models_json_path(state_dir)
    if not path.exists():
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def hf_models_json_age_seconds(state_dir) -> Optional[float]:
    path = hf_models_json_path(state_dir)
    try:
        return time.time() - path.stat().st_mtime
    except OSError:
        return None


_hf_refresh_lock = threading.Lock()
_hf_last_failure_at: "dict[str, float]" = {}


def refresh_huggingface_catalog_if_stale(state_dir, *, max_age_hours: Optional[float] = None,
                                          force: bool = False, env: Optional[dict] = None) -> Optional[bool]:
    """The Hugging Face mirror of `providers.databricks.refresh_openrouter_
    catalog_if_stale` -- same staleness knob (`databricks.catalog_max_age_
    hours`, shared across every network catalog this harness caches, per
    that function's own "one shared staleness knob, never a second config
    key" rule), same single-flight lock/post-failure backoff shape, same
    `None`-means-nothing-needed-doing / `True`/`False` contract. `None`
    when Hugging Face isn't configured (no `HF_TOKEN`) -- an endpoint-only
    setup has no router catalog to refresh either, by design."""
    key = str(state_dir)
    try:
        if max_age_hours is None:
            from halo_harness.theme import get_config_value
            max_age_hours = get_config_value("databricks.catalog_max_age_hours", default=24)
        age = hf_models_json_age_seconds(state_dir)
        always = float(max_age_hours) <= 0
        if age is not None:
            age = max(0.0, float(age))
        if not force and not always and age is not None and age < float(max_age_hours) * 3600:
            return None
        last_failure_at = _hf_last_failure_at.get(key)
        if not force and last_failure_at is not None and (time.monotonic() - last_failure_at) < _AUTO_REFRESH_BACKOFF_S:
            return None
        from halo_harness.providers.config import resolve_huggingface
        hf = resolve_huggingface(env)
        if hf is None:
            return None
        from halo_harness.providers.databricks import CATALOG_REFRESH_BUSY
        if not _hf_refresh_lock.acquire(blocking=False):
            return CATALOG_REFRESH_BUSY
        try:
            fetched = probe_huggingface_models(hf.base_url, hf.api_key)
            write_hf_models_json(state_dir, fetched)
            _hf_last_failure_at.pop(key, None)
            return True
        except Exception:
            _hf_last_failure_at[key] = time.monotonic()
            return False
        finally:
            _hf_refresh_lock.release()
    except Exception:
        return False
