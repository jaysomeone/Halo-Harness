"""halo_harness.providers.ollama_fit -- Halo 2.0.3 round 3: the pure
arithmetic behind the fit estimate (brief item 2) and the ollama
ProviderProfile's context-class tool-catalog sizing (brief item 3). No
I/O, no import of providers.ollama/ollama_hw -- every function here takes
already-read numbers and returns an answer or `None`; the modules that
actually read hardware/catalogs (providers.ollama_hw) call INTO this one,
never the other way, so there is no import cycle.

KV-cache bytes/token (research doc section 2: "standard GGML/llama.cpp
accounting, not independently re-derived this round" -- the `/ollama`
panel's own help text repeats this origin note; see
`providers.ollama_panel.KV_FORMULA_ORIGIN_NOTE`):

    head_dim  = embedding_length / head_count
    bytes/tok = 2 * block_count * head_count_kv * head_dim * bytes_per_elem

`bytes_per_elem` defaults to 2.0 (f16 KV cache) -- Ollama's own KV-cache
quantization (`OLLAMA_KV_CACHE_TYPE`) is a server-wide flag this round
found no per-model API to read back, so f16 is the documented,
conservative assumption (it never UNDER-estimates memory use).

Tool-catalog sizing by context class (brief item 3's own proposal,
overridable by the `ollama.tools_max` config key -- docs/CONFIG.md;
never below `core_tools_floor()`):

    effective num_ctx         tools_max
    ------------------------  ---------
    < 16,384                  16
    16,384 .. 32,767          32
    32,768 .. 65,535          64
    >= 65,536                 128

The floor (`core_tools_floor()`) is the real, live count of this
platform's built-in tools (`tools/registry.py::default_tools`) -- Read,
Write, Edit, Bash, Glob, Grep, Agent, the task-board tools, ToolSearch,
and the rest (`CORE_TOOL_NAMES` below is the brief's own illustrative
SUBSET of that same list, kept for prose, not the numeric floor itself)
-- so even the smallest class always leaves room for every tool a
session cannot function without: those built-ins are seeded into every
session's FROZEN catalog before any cap is ever computed, and eviction
can never remove one, so a `tools_max` below this count would be
unreachable.
"""

from __future__ import annotations

from typing import Optional

# brief item 3's own illustrative list -- documentation only, NOT the
# numeric floor below (see core_tools_floor's own docstring for why).
CORE_TOOL_NAMES = ("Read", "Write", "Edit", "Bash", "Glob", "Grep", "Agent",
                   "TaskCreate", "TaskUpdate", "TaskList", "ToolSearch")


_builtin_tool_count_cache: Optional[int] = None


def core_tools_floor() -> int:
    """The TRUE floor for `tools_max`: `halo_harness.tools.registry.
    default_tools()` is unconditionally seeded into every session's
    FROZEN catalog before `headless.build_session` ever looks at a
    provider's cap/cap_budget, and `agent.catalog.SessionCatalog._evict_
    one` can only ever remove a LOADED-DEFERRED tool, never a frozen one
    -- a `tools_max` below this count is unreachable (every ollama
    session would exceed it before a single MCP/ToolSearch tool joins
    the catalog, turning `ToolCatalogTooLarge` into a permanent error
    instead of the backstop it's meant to be). Platform-dependent
    (PowerShell joins the list on win32); `CORE_TOOL_NAMES` above is the
    brief's own illustrative subset of this same list, kept for the
    panel's/docs' prose, not used as the numeric floor itself. Computed
    lazily and cached (never at import time -- avoids paying the cost of
    building every built-in tool object just from `import ollama_fit`,
    and avoids any import-order fragility against `tools/registry.py`);
    `reset_core_tools_floor_cache` is a test seam for a fake/filtered
    registry."""
    global _builtin_tool_count_cache
    if _builtin_tool_count_cache is None:
        from halo_harness.tools.registry import default_tools
        _builtin_tool_count_cache = len(default_tools())
    return _builtin_tool_count_cache


def reset_core_tools_floor_cache() -> None:
    """Test seam: force the next `core_tools_floor()` call to recompute."""
    global _builtin_tool_count_cache
    _builtin_tool_count_cache = None


_TOOLS_MAX_CLASSES = (
    (16_384, 16),
    (32_768, 32),
    (65_536, 64),
)
_TOOLS_MAX_ABOVE_HIGHEST = 128

KV_BYTES_PER_ELEM_F16 = 2.0


def tools_max_for_num_ctx(num_ctx: Optional[int], *, floor: Optional[int] = None) -> int:
    """The context-class `tools_max` for `num_ctx` (module docstring's own
    table) -- `None`/non-positive `num_ctx` (nothing known yet) is treated
    as the SMALLEST class, never the largest: an unknown context is a
    reason to be conservative about prompt cost, not generous. Always
    `>= floor` (default `core_tools_floor()`) regardless of which class is
    picked -- pass `floor` explicitly only in a test that wants to pin the
    class boundaries independent of this platform's real built-in count."""
    if floor is None:
        floor = core_tools_floor()
    value = _TOOLS_MAX_ABOVE_HIGHEST
    if not isinstance(num_ctx, int) or num_ctx <= 0:
        value = _TOOLS_MAX_CLASSES[0][1]
    else:
        for ceiling, class_value in _TOOLS_MAX_CLASSES:
            if num_ctx < ceiling:
                value = class_value
                break
    return max(floor, value)


def ollama_tools_max_override() -> Optional[int]:
    """The `ollama.tools_max` config key (docs/CONFIG.md) -- a positive
    int, or `None` when unset/invalid (never raises on a bad value, just
    ignores it so a typo'd config.json can't break every ollama turn)."""
    from halo_harness.theme import get_config_value
    value = get_config_value("ollama.tools_max", default=None)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = int(value)
    return value if value > 0 else None


def resolve_ollama_tools_max(num_ctx: Optional[int], *, floor: Optional[int] = None) -> int:
    """`tools_max_for_num_ctx`, unless `ollama.tools_max` overrides it --
    the override still never drops below `floor` (default `core_tools_
    floor()`; brief: "never below the core tool set", stated once,
    applied to both the computed default AND an explicit override
    alike)."""
    if floor is None:
        floor = core_tools_floor()
    override = ollama_tools_max_override()
    if override is not None:
        return max(floor, override)
    return tools_max_for_num_ctx(num_ctx, floor=floor)


def _model_info_value(model_info: dict, suffix: str) -> Optional[float]:
    """The first `model_info` key ENDING in `suffix` (e.g. ".block_count")
    -- the `<family>.*` prefix varies per model (`providers.ollama.
    trained_context_for` uses the same suffix-match idiom for `.context_
    length`); `None` when no such key exists or its value isn't numeric."""
    for key, value in (model_info or {}).items():
        if key.endswith(suffix) and isinstance(value, (int, float)) and not isinstance(value, bool):
            return float(value)
    return None


def kv_bytes_per_token(model_info: dict, *, bytes_per_elem: float = KV_BYTES_PER_ELEM_F16) -> Optional[float]:
    """The KV-cache bytes/token figure (module docstring) from an already-
    fetched `/api/show` `model_info` dict -- `None` when any of the four
    fields it needs is missing (an older/unusual model, or a catalog row
    that hasn't been `/api/show`n yet). `head_count` (total attention
    heads, for `head_dim`) falls back to `head_count_kv` when `model_info`
    has no separate total-head-count key at all -- an approximation for a
    GQA model (noted here, not hidden): `head_dim` comes out larger than
    the model's real one, which makes this function's OWN estimate MORE
    conservative (a bigger bytes/token figure), never less."""
    block_count = _model_info_value(model_info, ".block_count")
    head_count_kv = _model_info_value(model_info, ".attention.head_count_kv")
    embedding_length = _model_info_value(model_info, ".embedding_length")
    head_count = _model_info_value(model_info, ".attention.head_count") or head_count_kv
    if not block_count or not head_count_kv or not embedding_length or not head_count:
        return None
    head_dim = embedding_length / head_count
    return 2.0 * block_count * head_count_kv * head_dim * bytes_per_elem


def _power_of_two_floor(n: int) -> int:
    power = 1
    while power * 2 <= n:
        power *= 2
    return power


class _WeightsDoNotFit:
    """Sentinel (round 3 fix pass, live-run finding): `free_memory_bytes
    - resident_weight_bytes <= 0` -- the model's own WEIGHTS alone do not
    fit (partial/full CPU offload), a positively-known fact, distinct
    from plain `None` ("unknown, the hard cap stands"). `providers.
    ollama.compute_num_ctx` reads this one specific value as "use the
    conservative documented fallback (8192) instead of the hard cap" --
    a model already spilling to system RAM must not ALSO be handed a
    131072-token KV cache budget. Never compared with `==`; always `is
    WEIGHTS_DO_NOT_FIT`."""

    def __repr__(self) -> str:
        return "WEIGHTS_DO_NOT_FIT"


WEIGHTS_DO_NOT_FIT = _WeightsDoNotFit()


def fit_estimate(*, kv_bytes_per_token: Optional[float], free_memory_bytes: Optional[int],
                  resident_weight_bytes: Optional[int]):
    """Round 3's pure fit-estimate function (brief item 2): the largest
    power-of-two context whose KV cache fits in `free_memory_bytes` AFTER
    `resident_weight_bytes` (the model's own weights) are subtracted --
    plain `None` when any INPUT is unknown/non-positive (genuinely
    unknown -- the caller's hard cap stands); `WEIGHTS_DO_NOT_FIT` when
    every input WAS known and headroom still computes `<= 0` (partial/
    full CPU offload -- a real situation `providers.ollama_panel`'s
    offload sentence reports, but also -- round 3 fix pass -- a fact
    `compute_num_ctx` must act on, never silently treat the same as
    "unknown"). Callers (`providers.ollama_hw.estimate_fit_for_host`)
    augment `free_memory_bytes` with other loaded models' reclaimable
    VRAM before calling this -- this function itself only ever sees the
    one final number."""
    values = (kv_bytes_per_token, free_memory_bytes, resident_weight_bytes)
    if not all(isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0 for v in values):
        return None
    headroom = free_memory_bytes - resident_weight_bytes
    if headroom <= 0:
        return WEIGHTS_DO_NOT_FIT
    max_tokens = int(headroom // kv_bytes_per_token)
    return _power_of_two_floor(max_tokens) if max_tokens > 0 else None


def remote_loaded_context_as_fit_estimate(ps_entry: Optional[dict]) -> Optional[int]:
    """Remote hosts expose no OS-level VRAM read at all (research doc
    section 3) -- the only evidence that a given context size fits on a
    remote host is that it is ALREADY running there: an `/api/ps` entry's
    own `context_length`, rounded down to a power of two for the same
    contract `fit_estimate` returns (`None` when this model isn't
    currently loaded on that host at all)."""
    if not isinstance(ps_entry, dict):
        return None
    ctx = ps_entry.get("context_length")
    if not isinstance(ctx, int) or ctx <= 0:
        return None
    return _power_of_two_floor(ctx)


_CHARS_PER_TOKEN_ESTIMATE = 4


def estimate_catalog_prompt_tokens(tools_max: int) -> int:
    """Rough per-request prompt-token cost of a `tools_max`-sized catalog
    (brief item 3: "show the per-request prompt cost of the catalog in
    the host panel") -- NOT a real tokenizer count, just `len(json) / 4`
    (a commonly-used rough heuristic for English/JSON text) over the
    FIRST `tools_max` built-in tool definitions, name-sorted (the same
    order `ToolRegistry.definitions()` uses) -- a real session's catalog
    also carries MCP tool definitions this estimate never sees, so the
    number is a FLOOR, not a measurement; callers label it as an estimate."""
    import json
    from halo_harness.tools.registry import default_tools
    defs = sorted((t.definition() for t in default_tools()), key=lambda d: d.get("name", ""))
    subset = defs[:max(0, tools_max)]
    total_chars = sum(len(json.dumps(d, ensure_ascii=False)) for d in subset)
    return total_chars // _CHARS_PER_TOKEN_ESTIMATE
