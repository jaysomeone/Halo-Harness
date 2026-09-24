"""rolo_claude.providers.databricks -- Databricks route-candidate
selection, request body allowlisting, the on-disk route/max_tokens cache,
endpoint/model probing, and models.json read/write. Moved out of bridge.py
unchanged in the H0 package split; see wip/SIGNATURES.md's "M4-M6 additions"
section.

See providers/http.py's module docstring for why open_upstream/
UpstreamConnectError are imported LOCALLY (inside probe_databricks_endpoints/
probe_openrouter_models) instead of at module top level: this module and
http.py depend on each other's functions, and both are only used inside
function bodies, so the cross-imports are deferred to avoid a circular
import at module-load time.
"""

from __future__ import annotations

import http.client
import json
import re
import socket
import ssl
from pathlib import Path

def databricks_route_candidates(model: str) -> list[tuple[str, bool]]:
    """Ordered (path, body_has_model) candidates for a resolved Databricks
    model name (dbx: prefix already stripped). The invocations path uses the
    model name IN FULL, prefix included -- Databricks pay-per-token serving
    endpoint names keep "databricks-" as part of their real, literal name
    (documented form /serving-endpoints/databricks-meta-llama-3-3-70b-instruct
    /invocations; the Claude passthrough route already sends
    "databricks-claude-..." in full). Stripping it 404s on the primary route
    for every such model (finding 8 -- the brief itself was wrong here)."""
    invocations = (f"/serving-endpoints/{model}/invocations", False)
    mlflow = ("/ai-gateway/mlflow/v1/chat/completions", True)
    if model.startswith("system.ai."):
        return [mlflow, invocations]
    return [invocations, mlflow]


def build_databricks_body(oai_body: dict, include_model: bool, model: str) -> dict:
    """Filter an OpenAI-chat body down to the Databricks-accepted key
    allowlist, adding/omitting 'model'. `reasoning_effort`/`stream_options`
    (scope B/D) are additive: the proxy's own `anthropic_to_openai` never
    sets either key, so widening this allowlist changes nothing for the
    unchanged proxy path -- only the harness's new request builder
    (providers/request.py) ever populates them."""
    allowed = ("messages", "max_tokens", "temperature", "top_p", "stop", "stream",
               "tools", "tool_choice", "reasoning_effort", "stream_options")
    result = {key: oai_body[key] for key in allowed if key in oai_body}
    if include_model:
        result["model"] = model
    return result


def parse_databricks_max_tokens_limit(err_msg: str) -> int | None:
    """Parse a Databricks 400 error naming a max_tokens ceiling, e.g.
    'max_tokens must be <= 4096'. Deliberately anchored on the '<=' ceiling
    wording: a looser 'max_tokens ... (\\d+)' pattern also matches the
    UNRELATED "... exceed context limit: A + B > L" overflow wording (it
    contains the substring "max_tokens" too), misreading A as if it were a
    max_tokens ceiling and triggering a nonsensical clamp-retry instead of
    the real overflow handling (finding 3)."""
    m = re.search(r"max[_ ]?tokens?\D{0,20}?<=\s*(\d+)", err_msg, re.IGNORECASE)
    return int(m.group(1)) if m else None


def databricks_unreachable_response(detail: str) -> tuple[int, dict, dict]:
    """Build the (status, json_body, extra_headers) tuple for a Databricks connect/DNS failure."""
    msg = f"{detail} (are you on the VPN? Databricks is whitelisted)"
    return 502, {"error": {"type": "api_error", "message": msg}}, {"x-should-retry": "false"}


# In-memory mirror of <state_dir>/routes-cache.json: model -> {"route": int, "max_tokens_limit": int|None}
_DBX_ROUTE_CACHE: dict = {}


def _dbx_cache_path(state_dir) -> Path:
    """routes-cache.json path under state_dir."""
    return Path(state_dir) / "routes-cache.json"


def _dbx_cache_load(state_dir) -> dict:
    """Merge routes-cache.json from disk into _DBX_ROUTE_CACHE (in-memory entries win) and return it."""
    path = _dbx_cache_path(state_dir)
    if not path.exists():
        return _DBX_ROUTE_CACHE
    try:
        with open(path, encoding="utf-8") as f:
            disk = json.load(f)
    except (json.JSONDecodeError, OSError):
        return _DBX_ROUTE_CACHE
    if isinstance(disk, dict):
        for model, entry in disk.items():
            if model not in _DBX_ROUTE_CACHE:
                _DBX_ROUTE_CACHE[model] = entry
    return _DBX_ROUTE_CACHE


def _dbx_cache_save(state_dir) -> None:
    """Write _DBX_ROUTE_CACHE to routes-cache.json. Best-effort; swallow OSError."""
    try:
        Path(state_dir).mkdir(parents=True, exist_ok=True)
        with open(_dbx_cache_path(state_dir), "w", encoding="utf-8") as f:
            json.dump(_DBX_ROUTE_CACHE, f)
    except OSError:
        pass


def dbx_cache_get_route(model: str, state_dir) -> int | None:
    """Cached candidate index for model, or None."""
    if model not in _DBX_ROUTE_CACHE:
        _dbx_cache_load(state_dir)
    entry = _DBX_ROUTE_CACHE.get(model)
    return entry.get("route") if isinstance(entry, dict) else None


def dbx_cache_set_route(model: str, index: int, state_dir) -> None:
    """Record which candidate index worked for model, update memory + persist to disk."""
    _DBX_ROUTE_CACHE.setdefault(model, {})["route"] = index
    _dbx_cache_save(state_dir)


def dbx_cache_get_max_tokens_limit(model: str, state_dir) -> int | None:
    """Cached max_tokens ceiling for model, or None."""
    if model not in _DBX_ROUTE_CACHE:
        _dbx_cache_load(state_dir)
    entry = _DBX_ROUTE_CACHE.get(model)
    return entry.get("max_tokens_limit") if isinstance(entry, dict) else None


def dbx_cache_set_max_tokens_limit(model: str, limit: int, state_dir) -> None:
    """Record a discovered max_tokens ceiling for model, update memory + persist to disk."""
    _DBX_ROUTE_CACHE.setdefault(model, {})["max_tokens_limit"] = limit
    _dbx_cache_save(state_dir)




def _probe_databricks_endpoints_raw(root: str, token: str) -> "tuple[int, list[dict]]":
    """GET <root>/api/2.0/serving-endpoints; returns (status, raw endpoint
    dicts). Raises UpstreamConnectError on connect/DNS failure. Shared by
    `probe_databricks_endpoints` (bare names, unchanged proxy contract) and
    `probe_databricks_endpoints_full` (scope I catalog refresh, extended
    fields) so the HTTP call exists exactly once."""
    import urllib.parse
    from rolo_claude.providers.http import open_upstream, UpstreamConnectError
    parsed = urllib.parse.urlparse(root)
    host = parsed.hostname
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    tls = parsed.scheme == "https"
    path = parsed.path.rstrip("/") + "/api/2.0/serving-endpoints"
    headers = {"Authorization": f"Bearer {token}", "Accept-Encoding": "identity"}
    try:
        conn = open_upstream(host, port, tls)
        conn.request("GET", path, headers=headers)
        resp = conn.getresponse()
        raw = resp.read()
    except (OSError, socket.timeout, ssl.SSLError, http.client.HTTPException) as e:
        raise UpstreamConnectError(f"Databricks connection failed: {e}") from e
    if resp.status == 200:
        try:
            data = json.loads(raw.decode("utf-8", "replace"))
            return 200, list(data.get("endpoints", []))
        except (json.JSONDecodeError, ValueError):
            pass
    return resp.status, []


def probe_databricks_endpoints(root: str, token: str) -> tuple[int, list[str]]:
    """GET <root>/api/2.0/serving-endpoints; returns (status, endpoint_names). Raises UpstreamConnectError on connect/DNS failure."""
    status, entries = _probe_databricks_endpoints_raw(root, token)
    return status, [e.get("name", "") for e in entries]


def probe_databricks_endpoints_full(root: str, token: str) -> "tuple[int, list[dict]]":
    """Like `probe_databricks_endpoints` but keeps each endpoint's `name`,
    `task`, `state.ready` and caller `permission_level` (scope I: "Databricks
    endpoints list") instead of collapsing to bare names."""
    status, entries = _probe_databricks_endpoints_raw(root, token)
    out = []
    for e in entries:
        if not isinstance(e, dict) or not e.get("name"):
            continue
        state = e.get("state") if isinstance(e.get("state"), dict) else {}
        out.append({
            "name": e["name"], "task": e.get("task"),
            "ready": state.get("ready"), "permission_level": e.get("permission_level"),
        })
    return status, out


def dbx_endpoints_path(state_dir) -> Path:
    """dbx-endpoints.json path under state_dir (scope I)."""
    return Path(state_dir) / "dbx-endpoints.json"


def write_dbx_endpoints_json(state_dir, endpoints: list) -> None:
    """Write dbx-endpoints.json as {"<name>": {"task", "ready", "permission_level"}, ...}. Best-effort."""
    try:
        Path(state_dir).mkdir(parents=True, exist_ok=True)
        out = {e["name"]: {k: v for k, v in e.items() if k != "name"} for e in endpoints if e.get("name")}
        with open(dbx_endpoints_path(state_dir), "w", encoding="utf-8") as f:
            json.dump(out, f, indent=2)
    except OSError:
        pass


def load_dbx_endpoints_json(state_dir) -> dict:
    """Read dbx-endpoints.json (dict keyed by name); {} if missing/unparseable."""
    path = dbx_endpoints_path(state_dir)
    if not path.exists():
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def probe_openrouter_models(base_url: str, api_key: str) -> list[dict]:
    """GET {base_url}/models; returns a list of dicts with 'id',
    'context_length', 'max_output_tokens' (the proxy's own resolve_profile
    reads only these two, unchanged), plus -- when OpenRouter's response
    includes them -- 'input_modalities' (from architecture.input_modalities),
    'supported_parameters', and 'pricing' (H0: rolo_claude.model's
    ModelProfile reads these three for vision/reasoning/price)."""
    import urllib.parse
    from rolo_claude.providers.http import open_upstream, UpstreamConnectError
    parsed = urllib.parse.urlparse(base_url)
    host = parsed.hostname
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    tls = parsed.scheme == "https"
    path = parsed.path.rstrip("/") + "/models"
    headers = {"Authorization": f"Bearer {api_key}", "Accept-Encoding": "identity"}
    try:
        conn = open_upstream(host, port, tls)
        conn.request("GET", path, headers=headers)
        resp = conn.getresponse()
        raw = resp.read()
    except (OSError, socket.timeout, ssl.SSLError, http.client.HTTPException) as e:
        raise UpstreamConnectError(f"OpenRouter connection failed: {e}") from e
    if resp.status != 200:
        raise RuntimeError(f"OpenRouter /models returned {resp.status}")
    data = json.loads(raw.decode("utf-8", "replace"))
    models = []
    for entry in data.get("data", []):
        model_dict = {
            "id": entry.get("id"),
            "context_length": entry.get("context_length"),
            "max_output_tokens": (entry.get("top_provider") or {}).get("max_completion_tokens"),
        }
        architecture = entry.get("architecture")
        if isinstance(architecture, dict) and architecture.get("input_modalities") is not None:
            model_dict["input_modalities"] = architecture.get("input_modalities")
        if entry.get("supported_parameters") is not None:
            model_dict["supported_parameters"] = entry.get("supported_parameters")
        if entry.get("pricing") is not None:
            model_dict["pricing"] = entry.get("pricing")
        models.append(model_dict)
    return models


def models_json_path(state_dir) -> Path:
    """models.json path under state_dir."""
    return Path(state_dir) / "models.json"


def write_models_json(state_dir, models: list[dict]) -> None:
    """Write models.json as {"<id>": {"context_length":..., "max_output_tokens":...,
    [input_modalities], [supported_parameters], [pricing]}, ...}. The three
    bracketed keys are included only when the probe actually returned them
    (H0: extended probe_openrouter_models) -- the proxy's own resolve_profile
    only ever reads the first two keys, so their presence is harmless to it.
    Best-effort; swallows OSError."""
    try:
        Path(state_dir).mkdir(parents=True, exist_ok=True)
        out = {}
        for m in models:
            mid = m.get("id")
            if not mid:
                continue
            entry = {"context_length": m.get("context_length"), "max_output_tokens": m.get("max_output_tokens")}
            for extra_key in ("input_modalities", "supported_parameters", "pricing"):
                if extra_key in m:
                    entry[extra_key] = m[extra_key]
            out[mid] = entry
        with open(models_json_path(state_dir), "w", encoding="utf-8") as f:
            json.dump(out, f, indent=2)
    except OSError:
        pass


def load_models_json(state_dir) -> dict:
    """Read models.json (dict keyed by id); {} if missing/unparseable."""
    path = models_json_path(state_dir)
    if not path.exists():
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


