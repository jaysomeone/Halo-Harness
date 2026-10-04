"""halo_harness.providers.huggingface -- Halo 2.0.3 round 4: dedicated
Inference Endpoints config (`huggingface.endpoints`, mirroring `ollama.
hosts`' shape/resolver style -- providers.ollama.OllamaHost/resolve_ollama_
host is the pattern) and the `huggingface.bill_to` billing-header config.
Round 5 adds `huggingface.local_servers` (MANUAL `hf:local/<model>@<name>`
entries, same shape/resolver style again) -- auto-DETECTED local servers
are a fully separate, background-probed list owned by the sibling module
`providers.huggingface_local_probe`, never config here. The router's `GET
/v1/models` catalog (cached in the state dir with a TTL) lives in the
sibling module `providers.huggingface_catalog` (kept apart so no single
write here passes the house 250-line-per-write habit). The `hf:` dialect
itself reuses the existing openai-chat request/response code unchanged
(providers/request.py, providers/stream.py's stream_completion) -- this
module owns only config resolution, never a wire-shape concern.

Design per `plans/2.0.3-ollama-round2-brief.md` "Round 4"/"Round 5" and
`docs/harness/LOCAL-MODELS-RESEARCH.md` sections 8/9/10.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

log = logging.getLogger("bridge")


@dataclass(frozen=True)
class HFEndpoint:
    """One entry of `huggingface.endpoints` (`~/.halo/config.json`) -- a
    dedicated Inference Endpoint, a fundamentally separate product from the
    router (research doc section 9): its own URL, its own token, billed by
    compute time. `token`, when set, is sent as `Authorization: Bearer
    <token>` on every request to THIS entry -- never HF_TOKEN, never the
    router's base URL (pinned by tests/test_providers_huggingface.py). An
    entry with no token sends no Authorization header at all (the same
    "unauthenticated by definition unless configured otherwise" shape
    `providers.ollama.OllamaHost` uses for a bare local/LAN host) -- a
    dedicated endpoint that actually requires one then 401s, translated
    through the normal error path like any other auth failure."""
    name: str
    url: str
    default: bool = False
    token: Optional[str] = None


def _endpoint_from_dict(d: dict) -> Optional[HFEndpoint]:
    url = d.get("url")
    name = d.get("name")
    if not isinstance(url, str) or not url or not isinstance(name, str) or not name:
        return None
    return HFEndpoint(
        name=name, url=url.rstrip("/"), default=bool(d.get("default", False)),
        token=d.get("token") if isinstance(d.get("token"), str) and d.get("token") else None,
    )


def resolve_huggingface_endpoints() -> "list[HFEndpoint]":
    """`huggingface.endpoints` from `~/.halo/config.json` -- unlike `ollama.
    hosts`, NEVER synthesizes a default entry (there is no env var standing
    in for "the one dedicated endpoint" the way `OLLAMA_HOST` stands in for
    a local daemon): an empty/absent list means no endpoints are configured
    at all, which is the ordinary case for most boxes. A malformed entry
    (no `name`/`url`) is skipped, logged at DEBUG, never raised."""
    from halo_harness.theme import get_config_value
    raw = get_config_value("huggingface.endpoints", default=None)
    out: "list[HFEndpoint]" = []
    if isinstance(raw, list):
        for entry in raw:
            if isinstance(entry, dict):
                ep = _endpoint_from_dict(entry)
                if ep is not None:
                    out.append(ep)
                else:
                    log.debug("huggingface: skipped a config.json huggingface.endpoints entry with no name/url: %r",
                              entry)
    return out


def resolve_huggingface_endpoint(name: Optional[str]) -> Optional[HFEndpoint]:
    """The endpoint `hf:endpoint/<name>` names: an exact (case-insensitive)
    name match, else (a bare/empty `name`, kept for parity with `providers.
    ollama.resolve_ollama_host`'s own "no name given" case even though
    round 4's own parser always supplies one) the entry with `default:
    true`, else the first configured entry, else `None` -- a missing or
    renamed entry, OR no entries configured at all, both resolve to `None`
    here; the caller (`headless._resolve_creds`) turns that into the SAME
    plain `ProviderNotConfigured` message naming `huggingface.endpoints`
    either way (never a live network call -- this is config-only)."""
    entries = resolve_huggingface_endpoints()
    if not entries:
        return None
    if name:
        for ep in entries:
            if ep.name.lower() == name.lower():
                return ep
        return None
    for ep in entries:
        if ep.default:
            return ep
    return entries[0]


@dataclass(frozen=True)
class HFLocalServer:
    """One entry of `huggingface.local_servers` (`~/.halo/config.json`) --
    Halo 2.0.3 round 5: a MANUALLY-configured OpenAI-compatible local/LAN
    server (`llama-server`, vLLM, `transformers serve`, LM Studio, TGI, or
    a LAN box fronting any of those behind a bearer-token proxy -- the
    "one GPU box, several laptops" case the round 5 brief calls out, only
    ever reachable through a manual entry since auto-detection is loopback-
    only). Mirrors `ollama.hosts`/`huggingface.endpoints`' own shape
    (`name`, `url`, `default`). `api_key`, when set, goes out as
    `Authorization: Bearer <api_key>` on every call to THIS entry -- PINNED
    separate from both `HF_TOKEN` (the router's own credential,
    `providers.config.resolve_huggingface`) and an `HFEndpoint.token` (a
    dedicated endpoint's own credential) above: never read as a fallback
    for either, never substitutes for either
    (tests/test_providers_huggingface_local.py pins this three ways). An
    entry with no `api_key` sends no `Authorization` header at all, the
    same "unauthenticated by definition unless configured otherwise"
    default every other bare local/LAN host in this codebase uses."""
    name: str
    url: str
    default: bool = False
    api_key: Optional[str] = None


def _local_server_from_dict(d: dict) -> Optional[HFLocalServer]:
    url = d.get("url")
    if not isinstance(url, str) or not url:
        return None
    name = d.get("name") if isinstance(d.get("name"), str) and d.get("name") else url
    return HFLocalServer(
        name=name, url=url.rstrip("/"), default=bool(d.get("default", False)),
        api_key=d.get("api_key") if isinstance(d.get("api_key"), str) and d.get("api_key") else None,
    )


def resolve_huggingface_local_servers() -> "list[HFLocalServer]":
    """`huggingface.local_servers` from `~/.halo/config.json` -- MANUAL
    entries only (round 5 brief's own "Manual server entries" bullet).
    Never synthesizes one for an auto-detected server: that is `providers.
    huggingface_local_probe.auto_detect_local_servers`'s own, separate,
    background-probed list -- the two are only ever merged by the `/local`
    view (`providers.local_models`), never here. An empty/absent list is
    the ordinary case for a box that relies on auto-detection alone. A
    malformed entry (no `url`) is skipped, logged at DEBUG."""
    from halo_harness.theme import get_config_value
    raw = get_config_value("huggingface.local_servers", default=None)
    out: "list[HFLocalServer]" = []
    if isinstance(raw, list):
        for entry in raw:
            if isinstance(entry, dict):
                s = _local_server_from_dict(entry)
                if s is not None:
                    out.append(s)
                else:
                    log.debug("huggingface: skipped a config.json huggingface.local_servers entry with no url: %r",
                              entry)
    return out


def resolve_huggingface_local_server(name: Optional[str]) -> Optional[HFLocalServer]:
    """The manual entry `hf:local/<model>@<name>` names (or the DEFAULT
    manual entry for a bare `hf:local/<model>`) -- same selection order as
    `resolve_huggingface_endpoint`/`providers.ollama.resolve_ollama_host`:
    an exact case-insensitive name match, else the entry with `default:
    true`, else the first configured entry, else `None`. `None` here does
    NOT by itself mean "no server at all" for a bare `hf:local/<model>` --
    the caller (`headless._resolve_creds`) still falls back to whatever
    `providers.huggingface_local_probe.auto_detect_local_servers` already
    found before giving up; an auto-detected server is never required to
    also be a manual config entry. A NAMED ref (`@<name>`) with no matching
    entry is never silently retried against auto-detection, though -- a
    typo'd name should fail plainly, not fall through to a different
    server than the one asked for."""
    entries = resolve_huggingface_local_servers()
    if not entries:
        return None
    if name:
        for s in entries:
            if s.name.lower() == name.lower():
                return s
        return None
    for s in entries:
        if s.default:
            return s
    return entries[0]


def resolve_huggingface_bill_to() -> Optional[str]:
    """`huggingface.bill_to` (`~/.halo/config.json`) -- a Team/Enterprise
    org name sent as `X-HF-Bill-To` on every ROUTER request (research doc
    section 9: "Team/Enterprise can bill a specific org via an X-HF-Bill-To
    request header"); never sent to a dedicated endpoint (item 1/2 of the
    round 4 brief scope this to the router specifically -- an endpoint has
    its own compute-time billing with no such header). `None` when unset,
    the ordinary case."""
    from halo_harness.theme import get_config_value
    value = get_config_value("huggingface.bill_to", default=None)
    return value if isinstance(value, str) and value else None
