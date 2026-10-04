"""halo_harness.providers.ollama_capability -- Halo 2.0.3 round 2's
capability probe (research doc Q5/Q6), the deferred model gym's stand-in
for this release: one cheap REAL tool-call turn per newly-seen model
digest, cached durably (`~/.halo/ollama-capabilities.json`) so the cost
(one real inference call) is paid at most once per distinct model digest,
not once per catalog read/session/host probe. Declared capability (what
`/api/show`'s `capabilities` list claims) and demonstrated capability (what
the model actually does when asked) are deliberately kept separate --
`providers.ollama.get_catalog` surfaces the former; this module surfaces
the latter -- a measured badge, not a vendor claim, per the brief.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Optional

from halo_harness.providers.ollama import OllamaHost, FALLBACK_NUM_CTX, _get_json

log = logging.getLogger("bridge")

_CAPABILITY_PROBE_TIMEOUT_S = 30.0
_PROBE_TOOL_NAME = "halo_capability_probe"


def _capability_cache_path(state_dir: Path) -> Path:
    return Path(state_dir) / "ollama-capabilities.json"


def load_capability_cache(state_dir: Path) -> dict:
    """`{digest: {"tool_calls": bool, "checked_at": float, "model": str,
    "host": str}}`; `{}` if missing/unreadable -- never raises (a lost
    cache just means every digest gets re-probed once, same cost as a
    fresh install, never a crash)."""
    path = _capability_cache_path(state_dir)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_capability_cache(state_dir: Path, data: dict) -> None:
    path = _capability_cache_path(state_dir)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        import os
        os.replace(tmp, path)
    except OSError:
        log.debug("ollama: could not persist capability cache to %s", path, exc_info=True)


def _run_capability_probe(host: OllamaHost, model: str, timeout: float) -> bool:
    """One real, non-streaming `/api/chat` turn with a trivial tool
    definition and a prompt asking the model to call it; True iff the
    response's `message.tool_calls` came back non-empty -- independent of
    whatever `/api/show`'s `capabilities` list declares (research doc Q5:
    declared and demonstrated capability are not the same thing for any
    family except qwen3/gpt-oss/deepseek-r1)."""
    tool = {
        "type": "function",
        "function": {
            "name": _PROBE_TOOL_NAME,
            "description": "Call this tool with no arguments to confirm tool calling works.",
            "parameters": {"type": "object", "properties": {}},
        },
    }
    body = {
        "model": model, "stream": False,
        "messages": [{"role": "user", "content": f"Call the {_PROBE_TOOL_NAME} tool now, with no arguments."}],
        "tools": [tool],
        "options": {"num_ctx": FALLBACK_NUM_CTX},
        "keep_alive": host.keep_alive or "5m",
    }
    result = _get_json(host, "/api/chat", method="POST", body=body, timeout=timeout)
    if not isinstance(result, dict):
        return False
    tool_calls = (result.get("message") or {}).get("tool_calls")
    return isinstance(tool_calls, list) and len(tool_calls) > 0


def probe_tool_capability(host: OllamaHost, model: str, digest: str, state_dir: Path, *,
                           timeout: float = _CAPABILITY_PROBE_TIMEOUT_S, prober=None) -> Optional[bool]:
    """`None` means "unknown, never probed" -- a declared-but-unprobed
    model must never read as "proven not to support tools" just because
    nothing has asked yet. `True`/`False` once `digest` has been probed
    (cached from THAT point forward, no re-probe for the same digest ever
    again, including across catalog TTL windows -- a cache hit costs no
    network call at all). `digest` keys the cache, not the model name/tag,
    so a model re-pulled under the same name with different weights (a new
    digest) gets probed exactly once more. Honours `BRIDGE_TEST_NO_
    BACKGROUND_NET` the same as every other background probe: a cache MISS
    under that flag returns `None` rather than ever touching the network.
    `prober` is a test seam (defaults to `_run_capability_probe`)."""
    cache = load_capability_cache(state_dir)
    entry = cache.get(digest)
    if isinstance(entry, dict) and "tool_calls" in entry:
        return bool(entry["tool_calls"])
    from halo_harness.config.paths import background_net_disabled
    if background_net_disabled():
        return None
    runner = prober or _run_capability_probe
    try:
        result = bool(runner(host, model, timeout))
    except Exception:
        log.debug("ollama: capability probe for %s (digest %s) raised", model, digest, exc_info=True)
        return None
    cache[digest] = {"tool_calls": result, "checked_at": time.time(), "model": model, "host": host.name}
    save_capability_cache(state_dir, cache)
    return result
