"""rolo_claude.providers.models_dev -- models.dev `api.json` fetch + cache
(H8 scope C). A public, unauthenticated, unversioned metadata catalog
(https://models.dev/api.json, ~5 MB, 200+ providers) that the plan uses "for
Databricks rows and to cross-check model_table.json" -- fetched and cached
to `<state_dir>/models-dev.json` by `rolo-claude models --refresh`
(catalog_cli.py), same lifecycle as `models.json`/`dbx-endpoints.json`.

The vendored PACKAGE fallback (`providers/catalog/models_dev_databricks_
fallback.json`) is a small, committed, one-time TRIM of this same live data
down to just the `databricks` provider entry (regenerate it the same way
this module fetches, then re-save via `write_databricks_fallback` below --
a human/release step, never done automatically) -- see
`providers/catalog/__init__.py`'s own docstring for why only that one
provider's ids are a direct model-id match for this harness's own routing.
"""

from __future__ import annotations

import http.client
import json
import socket
import ssl
import urllib.parse
from pathlib import Path
from typing import Optional

MODELS_DEV_BASE_URL = "https://models.dev"


def fetch_models_dev(base_url: str = MODELS_DEV_BASE_URL) -> dict:
    """GET {base_url}/api.json; returns the FULL provider->models dict
    unchanged (no trimming here -- callers decide what to keep). Raises
    providers.http.UpstreamConnectError on connect/DNS failure, same
    vocabulary as every other probe in this package."""
    from rolo_claude.providers.http import open_upstream, UpstreamConnectError
    parsed = urllib.parse.urlparse(base_url)
    host = parsed.hostname
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    tls = parsed.scheme == "https"
    path = parsed.path.rstrip("/") + "/api.json"
    headers = {"Accept-Encoding": "identity"}
    try:
        conn = open_upstream(host, port, tls)
        conn.request("GET", path, headers=headers)
        resp = conn.getresponse()
        raw = resp.read()
    except (OSError, socket.timeout, ssl.SSLError, http.client.HTTPException) as e:
        raise UpstreamConnectError(f"models.dev connection failed: {e}") from e
    if resp.status != 200:
        raise RuntimeError(f"models.dev /api.json returned {resp.status}")
    data = json.loads(raw.decode("utf-8", "replace"))
    if not isinstance(data, dict):
        raise RuntimeError("models.dev /api.json did not return a JSON object")
    return data


def models_dev_json_path(state_dir) -> Path:
    return Path(state_dir) / "models-dev.json"


def write_models_dev_json(state_dir, data: dict) -> None:
    """Cache the FULL fetched dict verbatim. Best-effort; swallows OSError
    (a doctor/refresh failure to WRITE the cache is a warning, never a
    crash -- same contract as every other write_*_json in this package)."""
    try:
        Path(state_dir).mkdir(parents=True, exist_ok=True)
        with open(models_dev_json_path(state_dir), "w", encoding="utf-8") as f:
            json.dump(data, f)
    except OSError:
        pass


def load_models_dev_json(state_dir) -> dict:
    path = models_dev_json_path(state_dir)
    if not path.exists():
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def _vendored_catalog_dir() -> Path:
    return Path(__file__).resolve().parent / "catalog"


def load_vendored_databricks_fallback() -> dict:
    """The package-shipped `{databricks-model-id: {...}}` dict (models.dev's
    own `databricks` provider entry, trimmed and committed to the repo) --
    {} if the file is somehow missing/unparseable (a fresh-enough install
    problem, never a crash)."""
    path = _vendored_catalog_dir() / "models_dev_databricks_fallback.json"
    if not path.exists():
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def load_vendored_openrouter_fallback() -> dict:
    """The package-shipped OpenRouter fallback (same shape as
    `providers.databricks.load_models_json`'s own output) -- {} if missing/
    unparseable."""
    path = _vendored_catalog_dir() / "openrouter_fallback.json"
    if not path.exists():
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def databricks_profile_fields_from_models_dev(entry: dict) -> dict:
    """One models.dev `databricks` provider model entry -> the subset of
    `model.ModelProfile` fields it can actually supply. Never raises on a
    malformed entry -- returns whatever it could parse, {} at worst."""
    out: dict = {}
    limit = entry.get("limit") if isinstance(entry.get("limit"), dict) else {}
    if isinstance(limit.get("context"), int):
        out["context_tokens"] = limit["context"]
    if isinstance(limit.get("output"), int):
        out["max_output_tokens"] = limit["output"]
    modalities = entry.get("modalities") if isinstance(entry.get("modalities"), dict) else {}
    input_modalities = modalities.get("input")
    if isinstance(input_modalities, list):
        out["vision"] = "image" in input_modalities
    if isinstance(entry.get("reasoning"), bool):
        out["reasoning"] = "native" if entry["reasoning"] else "none"
    cost = entry.get("cost") if isinstance(entry.get("cost"), dict) else {}
    # models.dev prices are USD per MILLION tokens; ModelProfile wants USD
    # per single token, matching OpenRouter's own pricing.prompt/completion
    # units (providers.databricks.probe_openrouter_models).
    if isinstance(cost.get("input"), (int, float)):
        out["price_in"] = cost["input"] / 1_000_000
    if isinstance(cost.get("output"), (int, float)):
        out["price_out"] = cost["output"] / 1_000_000
    return out
