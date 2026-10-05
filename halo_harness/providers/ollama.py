"""halo_harness.providers.ollama -- Halo 2.0.3 round 2: host config, the
native-API GET probes (`/api/version`, `/api/tags`, `/api/show`, `/api/ps`),
the per-host catalog cache, and the context-ownership rule for the `ol:`
provider. The native `/api/chat` request builder lives in
`providers.ollama_request`; the NDJSON streaming decoder lives in
`providers.ollama_stream` -- split the same way `databricks.py`/`dbx_routing.py`
and `oai_stream.py`/`stream.py` already are, so no one file grows past the
250-line-per-write house habit while it's being built.

Design per `plans/2.0.3-ollama-round2-brief.md` "Round 2" and
`docs/harness/LOCAL-MODELS-RESEARCH.md` sections 1/2/4/7: one dialect
("ollama") reaches a local daemon, a named LAN host, or Ollama Cloud, all
through the SAME native `/api/chat` wire shape -- only `base_url` and
(cloud-only) an `Authorization: Bearer <api_key>` header differ (research
doc Q7). Every host is addressed by name (`ol:<model>@<hostname>`); never a
literal LAN address in this file or in any test/doc (`tests/
test_privacy_scan.py`).
"""

from __future__ import annotations

import json
import logging
import threading
import time
import urllib.parse
from dataclasses import dataclass
from typing import Optional

log = logging.getLogger("bridge")

DEFAULT_OLLAMA_URL = "http://127.0.0.1:11434"
# research doc Q2: the hard outer cap Halo never requests past, regardless
# of what a model's trained context or a host's own max_ctx override claim.
HARD_CONTEXT_CAP = 131072
# research doc Q2: no fetched page gives a sane floor for "nothing else is
# known at all" (no `/api/show` yet, no host override) -- 8192 is a
# deliberately conservative placeholder (well under even the lowest
# VRAM-tiered default context-length.md documents, 4096, times two) so a
# first request before the catalog has ever loaded still sends SOME
# `options.num_ctx` rather than inventing a number that looks authoritative;
# every later request on the same model uses the real trained context once
# `/api/show` has been read once.
FALLBACK_NUM_CTX = 8192
# Round 5b MUST-FIX (plans/2.0.3-release-notes-for-fix-pass.md, the LAN-
# host live run): a REMOTE host with NOTHING known at all (no host.max_ctx,
# no learned cap, no live fit estimate) used to fall through to
# HARD_CONTEXT_CAP (131072) and load a 30B MoE partially offloaded on a
# 24 GB card. A remote host can't get an OS-level GPU read (no `ssh`
# configured) the way a local host always can, so "nothing known" is a
# real, common outcome for it in a way it mostly isn't locally -- 32768 is
# the conservative default `resolve_num_ctx_and_source` substitutes for
# the hard cap in that one specific case; `ollama.hosts[].max_ctx` is the
# documented explicit override when an operator wants something else.
REMOTE_UNKNOWN_DEFAULT_NUM_CTX = 32768
# research doc section 6: compaction trigger for a local route, same 75% of
# num_ctx the 2.0.5 brief's Phase 1 specifies.
COMPACTION_TRIGGER_FRACTION = 0.75
_CATALOG_TTL_S = 30.0
_READ_TIMEOUT_S = 10.0
_VERSION_PROBE_TIMEOUT_S = 1.5


@dataclass(frozen=True)
class OllamaHost:
    """One entry of `ollama.hosts` (`~/.halo/config.json`). `url` always
    carries a scheme (`http://`/`https://`) -- a bare `host:port` from
    `OLLAMA_HOST` is normalized to `http://host:port` at resolve time, never
    stored bare. `api_key`, when set, is sent as `Authorization: Bearer
    <api_key>` on every request to this host (Ollama Cloud; research doc
    Q7) -- every other host is unauthenticated BY DEFINITION (research doc
    section 4: "no authentication exists in Ollama itself"), never assumed
    to have one."""
    name: str
    url: str
    default: bool = False
    keep_alive: Optional[str] = None
    max_ctx: Optional[int] = None
    num_parallel_hint: Optional[int] = None
    api_key: Optional[str] = None
    # Round 5b: a hint for this host's `OLLAMA_KV_CACHE_TYPE` server flag
    # (docs.ollama.com/faq.md: "f16" (default), "q8_0", "q4_0" -- Ollama
    # exposes no per-model readback of what's actually running, so this is
    # a HINT the operator sets to match their own server config, never
    # auto-detected). `None`/unrecognized falls back to f16 in
    # `providers.ollama_fit.kv_bytes_per_elem_for` -- the conservative
    # choice (never under-estimates memory the way guessing a smaller
    # quantized type for an f16 host would).
    kv_cache_type: Optional[str] = None
    # Round 5b: `"user@host"` for an OPTIONAL ssh GPU read on a remote
    # host (`providers.ollama_hw.probe_remote_gpu_memory`) -- never
    # required, never prompted for; `None` (the default) leaves this
    # host's fit estimate exactly as before (its own `/api/ps` only).
    ssh: Optional[str] = None


def _normalize_host_url(raw: str) -> str:
    raw = (raw or "").strip()
    if not raw:
        return DEFAULT_OLLAMA_URL
    if "://" not in raw:
        raw = f"http://{raw}"
    return raw.rstrip("/")


def normalize_ollama_model_name(name: Optional[str]) -> Optional[str]:
    """Halo 2.0.3 round 5c FIX PASS (live run): Ollama's own tag
    convention -- a name with no explicit `:<tag>` means `:latest`.
    Confirmed live: `halo local import <gguf> --name halo-live-test` (no
    tag) is stored by Ollama as `halo-live-test:latest`, so `/api/tags`/
    `/api/ps` both report THAT name back -- a bare `ol:halo-live-test`
    ref then failed to match its own catalog/loaded-model row anywhere
    names were compared by raw string equality, silently losing the
    trained-context lookup and falling back to the conservative 8192
    default, which then overflowed on a real prompt. `None`/empty is
    returned as-is (nothing to normalize)."""
    if not name:
        return name
    return name if ":" in name else f"{name}:latest"


def ollama_names_match(a: Optional[str], b: Optional[str]) -> bool:
    """The ONE helper every Ollama name comparison in this codebase must
    go through (catalog lookup, trained-context lookup, the fit
    estimate's `/api/ps` matching, VRAM-aware role checks, calibration,
    panel rows, `/local`) -- comparing raw strings directly silently
    misses an untagged ref against its own `:latest`-qualified catalog/
    loaded-model row (this fix pass's own root cause). `False` when
    either side is empty/None -- never a vacuous match."""
    if not a or not b:
        return False
    return normalize_ollama_model_name(a) == normalize_ollama_model_name(b)


def _host_from_dict(d: dict) -> Optional[OllamaHost]:
    url = d.get("url")
    if not isinstance(url, str) or not url:
        return None
    name = d.get("name") if isinstance(d.get("name"), str) and d.get("name") else url
    max_ctx = d.get("max_ctx")
    num_parallel_hint = d.get("num_parallel_hint")
    return OllamaHost(
        name=name, url=_normalize_host_url(url), default=bool(d.get("default", False)),
        keep_alive=d.get("keep_alive") if isinstance(d.get("keep_alive"), str) else None,
        max_ctx=int(max_ctx) if isinstance(max_ctx, (int, float)) and not isinstance(max_ctx, bool) else None,
        num_parallel_hint=(int(num_parallel_hint)
                           if isinstance(num_parallel_hint, (int, float)) and not isinstance(num_parallel_hint, bool)
                           else None),
        api_key=d.get("api_key") if isinstance(d.get("api_key"), str) and d.get("api_key") else None,
        kv_cache_type=d.get("kv_cache_type") if isinstance(d.get("kv_cache_type"), str) and d.get("kv_cache_type")
        else None,
        ssh=d.get("ssh") if isinstance(d.get("ssh"), str) and d.get("ssh") else None,
    )


def resolve_ollama_hosts(env: Optional[dict] = None) -> "list[OllamaHost]":
    """`ollama.hosts` from `~/.halo/config.json` (list of `{name, url,
    default, keep_alive, max_ctx, num_parallel_hint, api_key}`); when that
    list is empty/absent, synthesizes exactly ONE default host from
    `OLLAMA_HOST` (research doc section 4's documented env var; a bare
    `host:port` is normalized to a full URL, never assumed to already carry
    a scheme) falling back to `127.0.0.1:11434` (Ollama's own documented
    default when `OLLAMA_HOST` is unset), and -- convenience, not in the
    brief's own config-key list but matching research doc Q7's documented
    Ollama Cloud var -- an ambient `OLLAMA_API_KEY` becomes that ONE
    synthesized host's `api_key` so a cloud-only setup (`OLLAMA_HOST=https://
    ollama.com`, `OLLAMA_API_KEY=...`, nothing in config.json yet) works with
    zero `halo config set` calls. Never raises; a malformed config entry
    (no `url`) is skipped, logged at DEBUG."""
    import os
    env = env if env is not None else os.environ
    from halo_harness.theme import get_config_value
    raw_hosts = get_config_value("ollama.hosts", default=None)
    hosts: "list[OllamaHost]" = []
    if isinstance(raw_hosts, list):
        for entry in raw_hosts:
            if isinstance(entry, dict):
                host = _host_from_dict(entry)
                if host is not None:
                    hosts.append(host)
                else:
                    log.debug("ollama: skipped a config.json ollama.hosts entry with no url: %r", entry)
    if hosts:
        return hosts
    url = _normalize_host_url(env.get("OLLAMA_HOST") or DEFAULT_OLLAMA_URL)
    api_key = env.get("OLLAMA_API_KEY") or None
    return [OllamaHost(name="default", url=url, default=True, api_key=api_key)]


def resolve_ollama_host(name: Optional[str] = None, env: Optional[dict] = None) -> Optional[OllamaHost]:
    """The host `ol:<model>@<name>` (or bare `ol:<model>`, `name=None`)
    selects: an exact (case-insensitive) name match, else the entry with
    `default: true`, else the first configured entry, else None only when
    `resolve_ollama_hosts` itself returned an empty list (never happens
    today -- it always synthesizes at least one -- but a caller should not
    have to assume that)."""
    hosts = resolve_ollama_hosts(env)
    if not hosts:
        return None
    if name:
        for h in hosts:
            if h.name.lower() == name.lower():
                return h
        return None
    for h in hosts:
        if h.default:
            return h
    return hosts[0]


# research doc section 1/6: gpt-oss is the only model documented with
# GRADED think levels (low/medium/high); qwen3 and deepseek-r1 are
# documented thinking-capable but bool-only as far as the fetched docs
# showed. Substring match on the bare model id/tag -- same coarse style
# `profiles.model_family` already uses, not a model_table.json row (that
# table has no Ollama-host rows at all).
_GPT_OSS_EFFORT_TO_THINK = {
    "low": "low", "medium": "medium", "high": "high", "xhigh": "high", "max": "high",
}


def think_value_for_effort(effort: Optional[str], model_id: str):
    """The `think` field's value for one request (`True`/`False`/a graded
    level string/`None` to omit the field, i.e. "use the model's own
    default" per research doc section 1). `effort is None` (nothing
    configured anywhere) omits the field entirely rather than guessing a
    value the user never asked for. gpt-oss gets its own three graded
    levels; every other model is bool-only -- off for low effort (research
    doc section 6: "off for low effort on models that default to thinking"),
    on for medium and above. This bypasses `providers.profiles.map_effort`/
    `clamp_effort` entirely -- those build `reasoning_effort`/`thinking.
    budget_tokens` fields that don't exist on this dialect's wire shape."""
    if not effort:
        return None
    low = (model_id or "").lower()
    if "gpt-oss" in low:
        return _GPT_OSS_EFFORT_TO_THINK.get(effort, "high")
    return effort != "low"


def resolve_num_ctx_and_source(trained_context: Optional[int], host_max_ctx: Optional[int] = None,
                                fit_estimate=None, *, hard_cap: int = HARD_CONTEXT_CAP,
                                fallback_when_unknown: int = FALLBACK_NUM_CTX,
                                learned_cap: Optional[int] = None, remote: bool = False,
                                remote_unknown_ceiling: int = REMOTE_UNKNOWN_DEFAULT_NUM_CTX):
    """Round 2/3's context-ownership rule, extended by round 5b with a
    learned calibration cap and a conservative remote-unknown ceiling --
    returns `(num_ctx, source)` where `source` is ONE short phrase naming
    whichever candidate actually won ("learned cap", "host max_ctx", "fit
    estimate", "remote default", "fallback", "trained context", or "hard
    cap"); `compute_num_ctx` below is the number-only wrapper every
    pre-5b caller keeps using unchanged.

    Every candidate that's actually known is a SIMULTANEOUS entry in one
    `min(...)` -- round 5b does NOT turn this into an elif-style override
    chain where a bigger `host_max_ctx`/`learned_cap` could win over a
    SMALLER live `fit_estimate` (that would risk the exact overload this
    whole round exists to prevent); the two round-5b-specific changes are
    additive:

    - `learned_cap` (`halo ollama calibrate`'s own measured ground truth),
      when known, REPLACES `fit_estimate` as the "live fit" candidate
      entirely -- a measured fact always outranks a computed guess, but
      still only ever competes in the same `min(...)` as everything else,
      never bypasses `trained_context`/`hard_cap`.
    - A REMOTE host (`remote=True`) with NOTHING ELSE known at all (no
      `host_max_ctx`, no usable fit/learned-cap candidate) gets
      `remote_unknown_ceiling` (32768) as an EXTRA candidate alongside
      `hard_cap` (131072) -- never REPLACING `hard_cap` (`min` picks the
      smaller 32768 regardless), so an operator who sets `host_max_ctx`
      bigger than 32768 still gets exactly that, still never past 131072.

    `fit_estimate` is one of three things: a positive int (an ordinary
    candidate), `None` (genuinely unknown -- simply skipped), or
    `providers.ollama_fit.WEIGHTS_DO_NOT_FIT` (round 3 fix pass: a model
    whose own WEIGHTS don't fit in free memory -- `fallback_when_unknown`
    joins the pool instead of the hard cap being left to win, UNLESS
    `learned_cap` is also known, in which case the measured ground truth
    that it DOES fit at some size wins outright and the fallback is never
    added at all). When `trained_context` itself is unknown, ALSO falls
    back to `fallback_when_unknown`. Always >= 1, never raises."""
    from halo_harness.providers.ollama_fit import WEIGHTS_DO_NOT_FIT
    weights_do_not_fit = fit_estimate is WEIGHTS_DO_NOT_FIT
    has_learned = isinstance(learned_cap, int) and not isinstance(learned_cap, bool) and learned_cap > 0
    effective_fit = learned_cap if has_learned else fit_estimate
    fit_weights_do_not_fit = weights_do_not_fit and not has_learned
    fit_candidate = effective_fit if (isinstance(effective_fit, int) and not isinstance(effective_fit, bool)
                                       and effective_fit > 0) else None
    fit_label = "learned cap" if has_learned else "fit estimate"
    has_host_max = isinstance(host_max_ctx, int) and not isinstance(host_max_ctx, bool) and host_max_ctx > 0
    nothing_known = bool(remote) and not has_host_max and fit_candidate is None
    candidates = [(hard_cap, "hard cap")]
    if isinstance(trained_context, int) and not isinstance(trained_context, bool) and trained_context > 0:
        candidates.append((trained_context, "trained context"))
    if has_host_max:
        candidates.append((host_max_ctx, "host max_ctx"))
    if fit_candidate is not None:
        candidates.append((fit_candidate, fit_label))
    if nothing_known:
        candidates.append((remote_unknown_ceiling, "remote default"))
    if trained_context is None or fit_weights_do_not_fit:
        candidates.append((fallback_when_unknown, "fallback"))
    value = min(v for v, _ in candidates)
    priority = ("learned cap", "fit estimate", "host max_ctx", "fallback", "trained context",
                "remote default", "hard cap")
    label = min((l for v, l in candidates if v == value), key=priority.index)
    return max(1, value), label


def compute_num_ctx(trained_context: Optional[int], host_max_ctx: Optional[int] = None,
                     fit_estimate=None, *, hard_cap: int = HARD_CONTEXT_CAP,
                     fallback_when_unknown: int = FALLBACK_NUM_CTX,
                     learned_cap: Optional[int] = None, remote: bool = False,
                     remote_unknown_ceiling: int = REMOTE_UNKNOWN_DEFAULT_NUM_CTX) -> int:
    """The number-only wrapper around `resolve_num_ctx_and_source` -- see
    that function's own docstring for the full rule. Every pre-5b caller
    (three positional args, no `learned_cap`/`remote`) gets EXACTLY the
    same number as before; this is additive, not a breaking change."""
    value, _source = resolve_num_ctx_and_source(
        trained_context, host_max_ctx, fit_estimate, hard_cap=hard_cap,
        fallback_when_unknown=fallback_when_unknown, learned_cap=learned_cap, remote=remote,
        remote_unknown_ceiling=remote_unknown_ceiling,
    )
    return value


def ollama_overflow_retry_ceiling(*, host_max_ctx: Optional[int] = None, fit_estimate=None,
                                   learned_cap: Optional[int] = None, hard_cap: int = HARD_CONTEXT_CAP) -> int:
    """Halo 2.0.3 round 5c FIX PASS: the retry ceiling `providers.stream.
    _run_phase1_ollama`'s "prompt exceeds num_ctx" 400 handler checks
    before asking for a bigger window -- `min(learned_cap, host_max_ctx,
    fit_estimate, hard_cap)` for whichever of the first three are
    actually known positive ints (never `trained_context`/the
    conservative fallback -- see `providers.stream.CompletionRequest.
    ollama_ctx_retry_ceiling`'s own docstring for why those two are
    deliberately excluded). `fit_estimate` may be `None` or `providers.
    ollama_fit.WEIGHTS_DO_NOT_FIT` (neither is an int, both are simply
    skipped). Always returns an int (never `None`) -- `hard_cap` alone
    when nothing else is known."""
    candidates = [hard_cap]
    for v in (host_max_ctx, fit_estimate, learned_cap):
        if isinstance(v, int) and not isinstance(v, bool) and v > 0:
            candidates.append(v)
    return min(candidates)


def compaction_trigger_tokens(num_ctx: int) -> int:
    """75% of `num_ctx` (research doc section 6) -- the same local-route
    compaction trigger the 2.0.5 brief's Phase 1 specifies, computed from
    whatever `num_ctx` THIS request actually sent rather than a second,
    possibly-stale copy of the context math."""
    return max(1, int(num_ctx * COMPACTION_TRIGGER_FRACTION))


def _auth_headers(host: OllamaHost) -> dict:
    return {"Authorization": f"Bearer {host.api_key}"} if host.api_key else {}


def _get_json(host: OllamaHost, path: str, *, timeout: float = _READ_TIMEOUT_S,
               method: str = "GET", body: Optional[dict] = None):
    """One-shot GET/POST + JSON-decode against `host.url + path`; returns
    the parsed body on a 200, `None` on ANY failure (connect error, non-200,
    bad JSON) -- every caller here is a best-effort probe/catalog read,
    never a turn-blocking call, so "couldn't reach it" and "reached it but
    said something odd" both just mean "nothing to report" rather than an
    exception a background thread would have to catch anyway."""
    from halo_harness.providers.http import UpstreamConnectError, open_upstream
    parsed = urllib.parse.urlparse(host.url)
    hostname = parsed.hostname
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    tls = parsed.scheme == "https"
    full_path = parsed.path.rstrip("/") + path
    headers = {"Accept-Encoding": "identity", "Content-Type": "application/json"}
    headers.update(_auth_headers(host))
    body_bytes = json.dumps(body).encode("utf-8") if body is not None else None
    conn = None
    try:
        conn = open_upstream(hostname, port, tls, connect_timeout=timeout)
        if conn.sock:
            conn.sock.settimeout(timeout)
        conn.request(method, full_path, body=body_bytes, headers=headers)
        resp = conn.getresponse()
        raw = resp.read()
        if resp.status != 200:
            log.debug("ollama: %s %s -> HTTP %s", method, full_path, resp.status)
            return None
        return json.loads(raw.decode("utf-8", "replace")) if raw else {}
    except UpstreamConnectError as e:
        log.debug("ollama: %s %s unreachable: %s", method, full_path, e)
        return None
    except (OSError, ValueError) as e:
        log.debug("ollama: %s %s failed: %s", method, full_path, e)
        return None
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def probe_version(host: OllamaHost, *, timeout: float = _VERSION_PROBE_TIMEOUT_S) -> Optional[dict]:
    """`GET /api/version` -- the enablement probe (brief: "background GET
    /api/version with a short timeout"). `None` means unreachable; the
    caller decides what that means for `/ollama`/doctor display, never this
    function (never raises, never logs above DEBUG -- an unreachable local
    host is the ordinary case on most boxes, not a warning-worthy one)."""
    return _get_json(host, "/api/version", timeout=timeout)


def fetch_tags(host: OllamaHost, *, timeout: float = _READ_TIMEOUT_S) -> Optional[dict]:
    """`GET /api/tags` -- `{"models": [{name, model, modified_at, size,
    digest, details: {family, families, parameter_size,
    quantization_level, format, parent_model}}]}` (research doc Q1)."""
    return _get_json(host, "/api/tags", timeout=timeout)


def fetch_show(host: OllamaHost, model: str, *, timeout: float = _READ_TIMEOUT_S) -> Optional[dict]:
    """`POST /api/show {"model": <name>}` -- `modelfile`, `parameters`,
    `template`, `details`, `capabilities` (list), and `model_info` (the
    `<family>.context_length` trained-context field this provider's
    context-ownership rule reads; research doc Q1)."""
    return _get_json(host, "/api/show", method="POST", body={"model": model}, timeout=timeout)


def fetch_ps(host: OllamaHost, *, timeout: float = _READ_TIMEOUT_S) -> Optional[dict]:
    """`GET /api/ps` -- currently-loaded models: `size` vs `size_vram`
    (partial CPU offload), `expires_at`, and the loaded `context_length`
    (research doc Q1/Q3)."""
    return _get_json(host, "/api/ps", timeout=timeout)


def _open_conn(host: OllamaHost, *, timeout: float):
    from halo_harness.providers.http import open_upstream
    parsed = urllib.parse.urlparse(host.url)
    hostname = parsed.hostname
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    tls = parsed.scheme == "https"
    conn = open_upstream(hostname, port, tls, connect_timeout=timeout)
    if conn.sock:
        conn.sock.settimeout(timeout)
    return conn, parsed.path.rstrip("/")


def check_blob_exists(host: OllamaHost, digest_hex: str, *, timeout: float = 30.0) -> Optional[bool]:
    """Halo 2.0.3 round 5c FIX PASS (live run, build 0.34.2): `HEAD /api/
    blobs/sha256:<hex>` -- `True` (200, already present), `False` (404,
    needs uploading), `None` (unreachable/unexpected -- the caller treats
    this the same as `False`, i.e. "try uploading anyway", since a create
    attempt would fail for the same reason regardless)."""
    import http.client
    from halo_harness.providers.http import UpstreamConnectError
    conn = None
    try:
        conn, root = _open_conn(host, timeout=timeout)
        headers = {"Accept-Encoding": "identity"}
        headers.update(_auth_headers(host))
        conn.request("HEAD", f"{root}/api/blobs/sha256:{digest_hex}", headers=headers)
        resp = conn.getresponse()
        resp.read()
        if resp.status == 200:
            return True
        if resp.status == 404:
            return False
        return None
    except (UpstreamConnectError, OSError, ValueError, http.client.HTTPException) as e:
        log.debug("ollama: check_blob_exists failed: %s", e)
        return None
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def upload_blob(host: OllamaHost, digest_hex: str, file_path, *, on_progress=None,
                 timeout: float = 600.0) -> "tuple[bool, str]":
    """Halo 2.0.3 round 5c FIX PASS: `POST /api/blobs/sha256:<hex>` with
    the file's RAW bytes streamed from disk in fixed-size chunks (never
    reading the whole file into memory -- these are commonly hundreds of
    MB) -- 201 on success. `on_progress(bytes_sent, total_bytes)`, when
    given, is called after every chunk."""
    import http.client
    from pathlib import Path as _Path
    from halo_harness.providers.http import UpstreamConnectError
    path = _Path(file_path)
    size = path.stat().st_size
    conn = None
    try:
        conn, root = _open_conn(host, timeout=timeout)
        headers = {"Accept-Encoding": "identity", "Content-Type": "application/octet-stream",
                   "Content-Length": str(size)}
        headers.update(_auth_headers(host))
        conn.putrequest("POST", f"{root}/api/blobs/sha256:{digest_hex}")
        for k, v in headers.items():
            conn.putheader(k, v)
        conn.endheaders()
        sent = 0
        with open(path, "rb") as f:
            while True:
                chunk = f.read(1 << 20)
                if not chunk:
                    break
                conn.send(chunk)
                sent += len(chunk)
                if on_progress is not None:
                    try:
                        on_progress(sent, size)
                    except Exception:
                        log.debug("ollama: upload_blob on_progress callback raised", exc_info=True)
        resp = conn.getresponse()
        resp.read()
        if resp.status in (200, 201):
            return True, f"uploaded {sent} bytes"
        return False, f"HTTP {resp.status}"
    except (UpstreamConnectError, OSError, ValueError, http.client.HTTPException) as e:
        return False, f"{type(e).__name__}: {e}"
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def create_model(host: OllamaHost, name: str, *, files: dict, on_status=None,
                  timeout: float = 300.0) -> "tuple[bool, str]":
    """Halo 2.0.3 round 5c (brief item 4, "option B"), FIX PASS: `POST
    /api/create {"model": name, "files": {"<basename>": "sha256:<hex>"},
    "stream": true}` -- the live run found the OLDER `modelfile` form
    obsolete (`HTTP 400 {"error":"neither 'from' or 'files' was
    specified"}` on build 0.34.2); `files` names already-uploaded blobs
    by their own sha256 digest (`check_blob_exists`/`upload_blob` above
    run first). Streams NDJSON status lines (the same one-object-per-line
    shape `/api/chat`'s own stream uses) to `on_status`. Returns `(True,
    "success")` on the final `{"status": "success"}` line, `(False,
    message)` on an `{"error": ...}` line, a non-200 response, or any
    transport failure."""
    import http.client
    from halo_harness.providers.http import UpstreamConnectError
    conn = None
    try:
        conn, root = _open_conn(host, timeout=timeout)
        headers = {"Accept-Encoding": "identity", "Content-Type": "application/json"}
        headers.update(_auth_headers(host))
        body = json.dumps({"model": name, "files": files, "stream": True}).encode("utf-8")
        conn.request("POST", f"{root}/api/create", body=body, headers=headers)
        resp = conn.getresponse()
        if resp.status != 200:
            return False, f"HTTP {resp.status}"
        last_status = None
        while True:
            raw_line = resp.readline()
            if not raw_line:
                break
            line = raw_line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            if not isinstance(obj, dict):
                continue
            if on_status is not None:
                try:
                    on_status(obj)
                except Exception:
                    log.debug("ollama: create_model on_status callback raised", exc_info=True)
            if obj.get("error"):
                return False, str(obj["error"])
            if obj.get("status"):
                last_status = obj["status"]
                if last_status == "success":
                    return True, "success"
        return (last_status == "success"), (last_status or "no response from /api/create")
    except UpstreamConnectError as e:
        return False, f"unreachable: {e}"
    except (OSError, ValueError, http.client.HTTPException) as e:
        return False, f"{type(e).__name__}: {e}"
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


_CATALOG_LOCK = threading.Lock()
_CATALOG_CACHE: dict = {}  # host.url -> (monotonic_ts, catalog_dict)


def _build_catalog(host: OllamaHost) -> dict:
    tags = fetch_tags(host) or {}
    models = []
    for entry in (tags.get("models") or []):
        if not isinstance(entry, dict):
            continue
        name = entry.get("model") or entry.get("name")
        row = dict(entry)
        if name:
            show = fetch_show(host, name) or {}
            row["model_info"] = show.get("model_info") if isinstance(show.get("model_info"), dict) else {}
            row["capabilities"] = show.get("capabilities") if isinstance(show.get("capabilities"), list) else []
            if isinstance(show.get("details"), dict):
                row["details"] = {**(row.get("details") or {}), **show["details"]}
        models.append(row)
    return {"models": models, "fetched_at": time.time()}


def get_catalog(host: OllamaHost, *, ttl_s: float = _CATALOG_TTL_S, force: bool = False) -> dict:
    """`{"models": [...]}` merging `/api/tags` with a per-model `/api/show`,
    cached per `host.url` with a short TTL (brief: "/api/tags + /api/show
    per model, cached per host with a short TTL") -- re-fetched once the
    cache entry is older than `ttl_s`, or always when `force` is set (an
    explicit refresh command). A host that's unreachable right now returns
    the LAST good cache entry if one exists (never worse than silently
    empty); refreshed again on the next call once `ttl_s` elapses."""
    with _CATALOG_LOCK:
        cached = _CATALOG_CACHE.get(host.url)
    now = time.monotonic()
    if cached is not None and not force and (now - cached[0]) < ttl_s:
        return cached[1]
    fresh = _build_catalog(host)
    if not fresh["models"] and cached is not None:
        return cached[1]
    with _CATALOG_LOCK:
        _CATALOG_CACHE[host.url] = (now, fresh)
    return fresh


def reset_catalog_cache() -> None:
    """Test seam: force the next get_catalog() call on any host to re-fetch."""
    with _CATALOG_LOCK:
        _CATALOG_CACHE.clear()


def trained_context_for(catalog: dict, model: str) -> Optional[int]:
    """`model_info["<family>.context_length"]` for `model` in an already-
    fetched `catalog` (research doc Q1/Q2) -- `None` when the model isn't in
    the catalog yet, or the field isn't present under any key ending in
    `.context_length` (the exact `<family>` prefix varies per model).
    FIX PASS: matched via `ollama_names_match` (an untagged `model` must
    still find its own `:latest`-qualified catalog row)."""
    for row in catalog.get("models") or []:
        if not isinstance(row, dict) or not (ollama_names_match(row.get("model"), model)
                                              or ollama_names_match(row.get("name"), model)):
            continue
        info = row.get("model_info") or {}
        family = (row.get("details") or {}).get("family")
        if family and isinstance(info.get(f"{family}.context_length"), int):
            return info[f"{family}.context_length"]
        for key, value in info.items():
            if key.endswith(".context_length") and isinstance(value, int):
                return value
    return None


def probe_hosts_background(hosts, *, on_result=None, timeout: float = _VERSION_PROBE_TIMEOUT_S) -> None:
    """Fire one daemon thread per host calling `probe_version` and handing
    `(host, result_dict_or_None)` to `on_result` -- honours `BRIDGE_TEST_
    NO_BACKGROUND_NET` like every other background probe in the codebase (a
    no-op then, never touching the network even to a loopback address a
    test's own mock server might be listening on). Fire-and-forget: no
    thread is joined here, matching every other background catalog/balance
    worker in this codebase."""
    from halo_harness.config.paths import background_net_disabled
    if background_net_disabled():
        return

    def _one(h: OllamaHost) -> None:
        result = probe_version(h, timeout=timeout)
        if on_result is not None:
            try:
                on_result(h, result)
            except Exception:
                log.debug("ollama: probe_hosts_background on_result callback raised", exc_info=True)

    for h in hosts:
        t = threading.Thread(target=_one, args=(h,), daemon=True, name=f"ollama-probe-{h.name}")
        t.start()
