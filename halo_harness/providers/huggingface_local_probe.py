"""halo_harness.providers.huggingface_local_probe -- Halo 2.0.3 round 5:
the background auto-detect sweep for a local OpenAI-compatible server
(`hf:local/<model>`, no configured `huggingface.local_servers` entry
naming it), the on-demand probe for a MANUAL entry (`/local refresh`), and
the short-TTL context-length readback `model.resolve_model_profile` uses
for an `hf:local/*` ref. Design: `plans/2.0.3-ollama-round2-brief.md`
"Round 5" and `docs/harness/LOCAL-MODELS-RESEARCH.md` sections 8/10.

Default ports (research doc section 8/10, one correction folded in): 8080
(llama-server, TGI), 8000 (vLLM and `transformers serve` -- both plain
OpenAI-compatible, deliberately treated IDENTICALLY, never disambiguated:
"a host cannot assume 'port 8000 = vLLM' ... or just treat both
identically since both are OpenAI-compatible anyway"), 1234 (LM Studio).
Jan's own default port was explicitly UNCONFIRMED by the research doc (two
documentation URLs 404'd) and is deliberately never guessed here -- a Jan
server needs a manual `huggingface.local_servers` entry.

Loopback-only, by construction: auto-detection only ever probes
127.0.0.1, never a LAN address -- a LAN box fronting a model behind a
bearer-token proxy (the brief's own "one GPU box, several laptops" case)
is reachable ONLY through a manual entry, never guessed at.

"Which server does an `hf:local/*` ref actually mean" (config lookup,
auto-detect fallback) and the short-TTL context-length cache `model.
resolve_model_profile` reads live in the sibling module `providers.
huggingface_local_resolve` -- kept apart so no single write here passes
the house 250-line-per-write habit.
"""

from __future__ import annotations

import logging
import os
import urllib.parse
from dataclasses import dataclass, field
from typing import Optional

log = logging.getLogger("bridge")

# research doc section 8/10 -- see this module's own docstring for the
# vLLM/transformers-serve port-8000 ambiguity note.
DEFAULT_LOCAL_PORTS: "tuple[int, ...]" = (8080, 8000, 1234)

_PROBE_TIMEOUT_S = 1.5
_PROPS_TIMEOUT_S = 1.0
_CONTEXT_CACHE_TTL_S = 30.0  # matches providers.ollama._CATALOG_TTL_S


def local_probe_ports(env: Optional[dict] = None) -> "tuple[int, ...]":
    """The ports `auto_detect_local_servers` probes. `HF_LOCAL_PROBE_PORTS`
    (comma-separated ints; `HF_` prefix, already covered by tests/run_all.
    py's guarded-env sweep) or `huggingface.local_probe_ports` (a
    config.json int list) override `DEFAULT_LOCAL_PORTS` entirely -- round
    5 brief item 2's own "overridable for tests by a config list of ports
    or an env var". The env var wins when both are set. Never raises on a
    malformed override; falls back to `DEFAULT_LOCAL_PORTS` instead."""
    env = env if env is not None else os.environ
    raw_env = env.get("HF_LOCAL_PROBE_PORTS")
    if raw_env:
        try:
            ports = tuple(int(p.strip()) for p in raw_env.split(",") if p.strip())
            if ports:
                return ports
        except ValueError:
            log.debug("huggingface_local_probe: malformed HF_LOCAL_PROBE_PORTS %r, using defaults", raw_env)
    from halo_harness.theme import get_config_value
    raw_cfg = get_config_value("huggingface.local_probe_ports", default=None)
    if isinstance(raw_cfg, list) and raw_cfg:
        try:
            ports = tuple(int(p) for p in raw_cfg)
            if ports:
                return ports
        except (TypeError, ValueError):
            log.debug("huggingface_local_probe: malformed huggingface.local_probe_ports %r, using defaults", raw_cfg)
    return DEFAULT_LOCAL_PORTS


@dataclass(frozen=True)
class DetectedLocalServer:
    """One answered probe, auto-detected or manual. `name` is `"auto:
    <port>"` for an auto-detected entry (there is no user-given name) or
    the configured entry's own name for a manual one. `context_by_model`
    only ever carries entries for a model this probe found a real number
    for (research doc: unknown falls back to the profile default, never a
    guess here).

    Round 5b part 2 (brief item 6, "mlx_lm.server... tell them apart"):
    `runtime_label` is `"llama-server"`, `"mlx"`, or `None` (genuinely
    unknown -- every other OpenAI-compatible server this module treats
    identically on purpose, see the module docstring's port-8000 note).
    See `_runtime_label_for` for exactly how, and how little, this is
    actually confirmed."""
    name: str
    base_url: str
    model_ids: "tuple[str, ...]" = field(default_factory=tuple)
    context_by_model: "dict" = field(default_factory=dict)
    manual: bool = False
    runtime_label: "Optional[str]" = None


def _probe_get(base_url: str, path_suffix: str, *, api_key: Optional[str], timeout: float):
    """One-shot GET `{base_url}{path_suffix}` + JSON-decode; `None` on ANY
    failure (connect refused/timeout, non-200, bad JSON) -- same best-
    effort, never-raises contract `providers.ollama._get_json` uses, via
    the SAME shared `providers.http.open_upstream` connection opener (proxy/
    TLS handling for free, though every real caller here is loopback
    http://)."""
    import http.client
    import json
    import socket
    import ssl
    from halo_harness.providers.http import UpstreamConnectError, open_upstream
    parsed = urllib.parse.urlparse(base_url)
    host = parsed.hostname
    if not host:
        return None
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    tls = parsed.scheme == "https"
    path = parsed.path.rstrip("/") + path_suffix
    headers = {"Accept-Encoding": "identity"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    conn = None
    try:
        conn = open_upstream(host, port, tls, connect_timeout=max(1, int(timeout)))
        if conn.sock:
            conn.sock.settimeout(timeout)
        conn.request("GET", path, headers=headers)
        resp = conn.getresponse()
        raw = resp.read()
        if resp.status != 200:
            return None
        return json.loads(raw.decode("utf-8", "replace")) if raw else None
    except (UpstreamConnectError, OSError, socket.timeout, ssl.SSLError, http.client.HTTPException, ValueError) as e:
        log.debug("huggingface_local_probe: GET %s%s failed: %s", base_url, path_suffix, e)
        return None
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def _extract_context_length(row: dict) -> Optional[int]:
    """Best-effort context-length readback from one `/v1/models` row
    (research doc section 8/10): vLLM is documented to report
    `max_model_len`; every other runtime's exact field name was NOT
    confirmed this round, so this tries each plausible key and takes the
    first positive int -- a documented heuristic, never a confirmed API,
    same spirit as `providers.huggingface_catalog._parse_catalog_entry`'s
    own assumption."""
    if not isinstance(row, dict):
        return None
    for key in ("max_model_len", "context_length", "context_window", "n_ctx", "max_position_embeddings"):
        value = row.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and value > 0:
            return value
    return None


def probe_llama_server_props(base_url: str, *, timeout: float = _PROPS_TIMEOUT_S) -> Optional[int]:
    """Best-effort `GET {server root}/props` -- llama.cpp's own native
    endpoint (research doc section 10's one-line mention only; no
    confirmed JSON shape, so this is a heuristic, not a verified API).
    `/props` lives at the server ROOT; `base_url` here may carry the `/v1`
    suffix every OpenAI-compatible base this codebase configures does, so
    that suffix is stripped first. `None` on any failure or unrecognized
    shape -- never raises, never required for `probe_models_endpoint` to
    succeed."""
    root = base_url[:-len("/v1")] if base_url.endswith("/v1") else base_url
    data = _probe_get(root, "/props", api_key=None, timeout=timeout)
    if not isinstance(data, dict):
        return None
    n_ctx = data.get("n_ctx")
    if isinstance(n_ctx, int) and not isinstance(n_ctx, bool) and n_ctx > 0:
        return n_ctx
    nested = data.get("default_generation_settings")
    if isinstance(nested, dict):
        n_ctx = nested.get("n_ctx")
        if isinstance(n_ctx, int) and not isinstance(n_ctx, bool) and n_ctx > 0:
            return n_ctx
    return None


def _runtime_label_for(base_url: str, *, props_ctx: Optional[int]) -> Optional[str]:
    """Round 5b part 2 (brief item 6): llama-server is the one local
    runtime in this module's port family (8080) with a CONFIRMED native
    endpoint of its own (`/props`, research doc section 10 -- the shape
    is a heuristic per `probe_llama_server_props`'s own docstring, but
    the endpoint's EXISTENCE is llama.cpp-specific) -- `props_ctx is not
    None` means `/props` answered, so this is confidently "llama-server".
    When it did NOT answer, `mlx_lm.server`'s own flags/endpoints were
    never confirmed by this round's research (docs/harness/GPU-RESEARCH.md
    section 7: "UNCONFIRMED at the exact-flag level") -- there is no
    positive signal for MLX at all, only the ABSENCE of llama-server's
    own marker, so this guesses "mlx" ONLY on Apple Silicon (`sys.
    platform == "darwin"`, round 5b's own framing: "the Mac is
    different"), where MLX is the only other well-known local runtime in
    this exact port family; every other platform stays `None` (genuinely
    unknown -- could be TGI, vLLM mis-configured onto this port, or
    anything else) rather than guess wrong. Round 6's live Mac check
    should confirm or correct this."""
    if props_ctx is not None:
        return "llama-server"
    import sys
    return "mlx" if sys.platform == "darwin" else None


def probe_models_endpoint(base_url: str, *, name: str = "", api_key: Optional[str] = None,
                           manual: bool = False, timeout: float = _PROBE_TIMEOUT_S) -> Optional[DetectedLocalServer]:
    """`GET {base_url}/models` -- `None` when unreachable or not a real
    OpenAI-compatible server (no `data` list back); a `DetectedLocalServer`
    otherwise. Falls back to `probe_llama_server_props` once, for every
    model that reported no context number of its own -- ALSO what tells
    `_runtime_label_for` apart (round 5b part 2, brief item 6)."""
    data = _probe_get(base_url, "/models", api_key=api_key, timeout=timeout)
    if not isinstance(data, dict) or not isinstance(data.get("data"), list):
        return None
    rows = [r for r in data["data"] if isinstance(r, dict) and r.get("id")]
    if not rows:
        return None
    model_ids = tuple(r["id"] for r in rows)
    context_by_model = {}
    missing = []
    for r in rows:
        ctx = _extract_context_length(r)
        if ctx is not None:
            context_by_model[r["id"]] = ctx
        else:
            missing.append(r["id"])
    props_ctx = probe_llama_server_props(base_url) if missing else None
    if props_ctx is not None:
        for mid in missing:
            context_by_model[mid] = props_ctx
    return DetectedLocalServer(name=name, base_url=base_url.rstrip("/"), model_ids=model_ids,
                                context_by_model=context_by_model, manual=manual,
                                runtime_label=_runtime_label_for(base_url, props_ctx=props_ctx))


def auto_detect_local_servers(*, env: Optional[dict] = None, timeout: float = _PROBE_TIMEOUT_S) -> "list":
    """Probes every port `local_probe_ports(env)` names, on 127.0.0.1
    ONLY, for a real OpenAI-compatible `/v1/models` response. A no-op
    (empty list, no network at all) under `BRIDGE_TEST_NO_BACKGROUND_NET`
    -- the SAME background-probe test seam `providers.ollama.probe_hosts_
    background` honours (round 5 brief item 2). Sequential, not threaded:
    at most a handful of default ports, each bounded by `timeout` -- call
    this itself from a worker thread (never the UI thread), same as every
    other live probe in this codebase."""
    from halo_harness.config.paths import background_net_disabled
    if background_net_disabled():
        return []
    out = []
    for port in local_probe_ports(env):
        info = probe_models_endpoint(f"http://127.0.0.1:{port}/v1", name=f"auto:{port}", timeout=timeout)
        if info is not None:
            out.append(info)
    return out


def probe_manual_server(server, *, timeout: float = _PROBE_TIMEOUT_S) -> Optional[DetectedLocalServer]:
    """`/local refresh`'s own on-demand probe for ONE configured
    `huggingface.local_servers` entry (`providers.huggingface.
    HFLocalServer`) -- the SAME `GET /v1/models` an auto-detected server
    gets, sending `server.api_key` as a bearer header when set. Deliberately
    NEVER gated by `BRIDGE_TEST_NO_BACKGROUND_NET`: this is the EXPLICIT,
    user-requested probe the brief calls out ("a manual entry is never
    probed in the background unless the user asks") -- a test that wants
    to avoid even this real network call simply never calls it."""
    return probe_models_endpoint(server.url, name=server.name, api_key=server.api_key, manual=True, timeout=timeout)
