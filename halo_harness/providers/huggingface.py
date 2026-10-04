"""halo_harness.providers.huggingface -- Halo 2.0.3 round 4: dedicated
Inference Endpoints config (`huggingface.endpoints`, mirroring `ollama.
hosts`' shape/resolver style -- providers.ollama.OllamaHost/resolve_ollama_
host is the pattern) and the `huggingface.bill_to` billing-header config.
The router's `GET /v1/models` catalog (cached in the state dir with a TTL)
lives in the sibling module `providers.huggingface_catalog` (kept apart so
no single write here passes the house 250-line-per-write habit). The `hf:`
dialect itself reuses the existing openai-chat request/response code
unchanged (providers/request.py, providers/stream.py's stream_completion)
-- this module owns only config resolution, never a wire-shape concern.

Design per `plans/2.0.3-ollama-round2-brief.md` "Round 4" and
`docs/harness/LOCAL-MODELS-RESEARCH.md` section 9.
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
