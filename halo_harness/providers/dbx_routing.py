"""rolo_claude.providers.dbx_routing -- H14 scope D: the generic Databricks
family x api_type RULES table. Nothing here is a vendored per-model list (see
the brief's own "Correction: discovery only" note) -- `classify_family`
recognizes a family from the ENDPOINT'S OWN NAME (and, when the discovery
cache has them, its `foundation_model_name`/`model_class` hints), and
`chat_route_candidates`/`resolve_databricks_dialect` turn that family plus
the endpoint's OWN discovered `api_types` (cached by `init --preset work`/
`models --refresh` to `~/.rolo-claude/dbx-endpoints.json`, never hand-
maintained) into an ordered, endpoint-specific route.

Two separate decisions, kept apart deliberately (see providers/stream.py's
own module docstring on why the anthropic-passthrough and openai-chat
dialects never mix mid-request):
  * `resolve_databricks_dialect` -- WHICH DIALECT a `dbx:<name>` ref uses
    (anthropic-passthrough vs openai-chat), decided once at model-ref-parse
    time. Claude foundation endpoints default here; GLM/Kimi can opt in
    per-call (`dbx:<endpoint>@anthropic`) or per-model (`databricks.gateway.
    <endpoint>: anthropic` in ~/.rolo-claude/config.json). Bedrock EXTERNAL
    Claude endpoints (`us-anthropic-claude-*`, `claude-3-5-sonnet-...`) are
    force-kept on openai-chat/invocations despite "claude" being in the
    name -- they are not a native Anthropic Messages endpoint at all.
  * `chat_route_candidates` -- once the dialect IS openai-chat, which
    SUB-PATH(S) to try, in what order, with what wire "model" value. Only
    ever consulted by `call_databricks_chat` (providers/http.py); the
    anthropic-passthrough dialect's own two-path fallback lives in
    `call_anthropic_native` unchanged (it needs no family awareness -- every
    family that gets that dialect uses the exact same gateway URL).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

_ANTHROPIC_SUFFIX = "@anthropic"

# api_type-key -> (real api_type string, root-relative gateway path). "anthropic"
# is listed here for classify/override bookkeeping but chat_route_candidates
# (openai-chat dialect only) never emits it as a candidate itself.
API_TYPE_INFO = {
    "anthropic": {"api_type": "anthropic/v1/messages", "path": "/ai-gateway/anthropic/v1/messages"},
    "mlflow": {"api_type": "mlflow/v1/chat/completions", "path": "/ai-gateway/mlflow/v1/chat/completions"},
    "cursor": {"api_type": "cursor/v1/chat/completions", "path": "/ai-gateway/cursor/v1/chat/completions"},
}

# Per-family ordered chat-shaped api_type keys, filtered down at resolve time
# to whichever ones the endpoint's OWN discovered `api_types` actually list
# (brief: "never a type the endpoint does not list"); `invocations` is always
# appended last regardless of family. "claude_external" (Bedrock) gets no
# chat-shaped candidate at all -- invocations only.
FAMILY_ROUTE_ORDER: "dict[str, tuple]" = {
    "claude_foundation": ("anthropic", "mlflow", "cursor"),
    "glm": ("mlflow", "anthropic"),
    "kimi": ("mlflow", "anthropic"),
    "deepseek": ("mlflow",),
    "qwen": ("mlflow",),
    "llama": ("mlflow",),
    "gemma": ("mlflow",),
    "gpt_oss": ("mlflow",),
    "inkling": ("mlflow",),
    "gpt": ("mlflow", "cursor"),
    "gpt_pro": ("cursor",),          # databricks-gpt-5-5-pro: no mlflow chat
    "grok": ("mlflow",),
    "gemini": ("mlflow", "cursor"),
    "claude_external": (),           # Bedrock: invocations ONLY
}
_GENERIC_FALLBACK_ORDER = ("mlflow", "cursor")

_NON_CHAT_NAME_HINTS = ("gte-large", "bge-large", "embedding", "whisper", "titan-embed")

# Bedrock EXTERNAL Claude endpoint names, narrowly matched (brief's verified
# forms: `us-anthropic-claude-*`, `claude-3-5-sonnet-20240620-v1-0` -- AWS
# Bedrock's own dated `-YYYYMMDD-vN[-N]` id convention) -- deliberately NOT
# a bare "claude" substring check, which would also catch an ordinary
# Databricks-hosted or generic Claude ref (e.g. `dbx:claude-sonnet-4-5`,
# no `databricks-` prefix at all) that has always defaulted to the
# passthrough family (see classify_family's own comment on that fallback).
_BEDROCK_EXTERNAL_RE = re.compile(r"^us-anthropic-|claude.*-\d{8}-v\d")


def classify_family(name: str, *, foundation_model_name: str = "", model_class: str = "") -> str:
    """Family key from an endpoint's own name (and, when known, its
    discovered `foundation_model_name`/`model_class` hints) -- scope D:
    "family detection from endpoint name / foundation_model.name /
    model_class". `model_class`, when it already spells out one of this
    table's own keys, wins outright (a future, more explicit Databricks
    field); everything else is pattern matching, checked in an order that
    keeps distinguishing prefixes (Claude foundation vs Bedrock external,
    gpt-oss vs gpt, gpt-5-5-pro vs gpt) from shadowing each other. Returns
    "non_chat" for an embeddings/whisper endpoint, "unknown" when nothing
    matches (a brand-new family Databricks added -- callers fall back to a
    generic mlflow/cursor order for that case, never a hard failure)."""
    hint = (model_class or "").strip().lower()
    if hint in FAMILY_ROUTE_ORDER or hint == "non_chat":
        return hint
    low = (name or "").lower()
    fm_low = (foundation_model_name or "").lower()
    combined = f"{low} {fm_low}"
    if any(h in combined for h in _NON_CHAT_NAME_HINTS):
        return "non_chat"
    if low.startswith("databricks-claude-"):
        return "claude_foundation"
    if _BEDROCK_EXTERNAL_RE.search(low):
        return "claude_external"
    if "claude" in combined:
        # finding 10 (pre-H14): ANY dbx:-prefixed name containing "claude"
        # that isn't specifically a Bedrock EXTERNAL id (matched above)
        # defaults to the passthrough family -- preserves the long-standing
        # "any Claude-named ref, however it's spelled, is passthrough
        # unless proven otherwise" rule (test_launcher_dbx_claude_without_
        # databricks_prefix_is_passthrough).
        return "claude_foundation"
    if "gpt-5-5-pro" in combined:
        return "gpt_pro"
    if "gpt-oss" in combined:
        return "gpt_oss"
    if "glm" in combined:
        return "glm"
    if "kimi" in combined:
        return "kimi"
    if "deepseek" in combined:
        return "deepseek"
    if "qwen" in combined:
        return "qwen"
    if "llama" in combined:
        return "llama"
    if "gemma" in combined:
        return "gemma"
    if "inkling" in combined:
        return "inkling"
    if "grok" in combined:
        return "grok"
    if "gemini" in combined:
        return "gemini"
    if re.search(r"(^|[^a-z])gpt-\d", combined):
        return "gpt"
    return "unknown"


def _cache_entry(name: str, state_dir=None) -> Optional[dict]:
    from rolo_claude.config.paths import bridge_home
    from rolo_claude.providers.databricks import load_dbx_endpoints_json
    state_dir = state_dir if state_dir is not None else bridge_home()
    entry = load_dbx_endpoints_json(state_dir).get(name)
    return entry if isinstance(entry, dict) else None


def _family_for(name: str, entry: Optional[dict]) -> str:
    if entry is None:
        return classify_family(name)
    return classify_family(name, foundation_model_name=entry.get("foundation_model_name") or "",
                             model_class=entry.get("model_class") or "")


def resolve_databricks_dialect(bare: str, state_dir=None) -> "tuple[str, str]":
    """`(clean_model_name, dialect)` for a bare (already dbx:-stripped)
    Databricks model reference. Strips a `@anthropic` suffix (an explicit,
    per-call override -- always wins), else consults `databricks.gateway.
    <endpoint>` in `~/.rolo-claude/config.json` (a standing, per-model
    override), else falls back to the family default: Claude foundation ->
    anthropic-passthrough; every other family (INCLUDING Bedrock external
    Claude, despite "claude" appearing in its name) -> openai-chat."""
    explicit_anthropic = bare.endswith(_ANTHROPIC_SUFFIX)
    clean = bare[: -len(_ANTHROPIC_SUFFIX)] if explicit_anthropic else bare
    if explicit_anthropic:
        return clean, "anthropic-passthrough"
    from rolo_claude.theme import get_config_value
    override = get_config_value(f"databricks.gateway.{clean}", default=None)
    if isinstance(override, str) and override.strip().lower() == "anthropic":
        return clean, "anthropic-passthrough"
    family = _family_for(clean, _cache_entry(clean, state_dir))
    if family == "claude_foundation":
        return clean, "anthropic-passthrough"
    return clean, "openai-chat"


def refuse_if_non_chat(name: str, state_dir=None) -> Optional[str]:
    """An error message when `name` is a KNOWN non-chat endpoint (an
    embeddings/whisper model listed in the discovery cache) -- None when
    it's chat-shaped, or simply not in the cache yet (an unknown endpoint
    is never refused; it just gets today's static candidate order)."""
    entry = _cache_entry(name, state_dir)
    if entry is None:
        return None
    if _family_for(name, entry) != "non_chat":
        return None
    task = entry.get("task") or "?"
    return (f"{name!r} is a non-chat Databricks endpoint (task={task}) -- rolo-claude only drives "
            f"chat-shaped endpoints; pick a different model (see `rolo-claude models --refresh`).")


@dataclass(frozen=True)
class RouteCandidate:
    key: str                        # "mlflow" | "cursor" | "invocations"
    path: str                       # root-relative gateway/serving path
    include_model: bool             # whether the wire body needs a "model" field
    model_value: Optional[str]      # the exact value for that field, or None


def chat_route_candidates(name: str, state_dir=None) -> "list[RouteCandidate]":
    """Ordered OPENAI-CHAT-dialect candidates for `name` (bare, dbx:-
    stripped). Only ever reached once `resolve_databricks_dialect` already
    picked "openai-chat" for this ref, so "anthropic" is never emitted here
    even for a family that lists it. Unknown endpoint (not yet in the
    discovery cache) -> today's static [mlflow-or-invocations-first] order
    (`databricks.databricks_route_candidates`), cache-oblivious, exactly as
    before this milestone. A known non-chat endpoint returns `[]` (callers
    are expected to have already refused it via `refuse_if_non_chat`)."""
    entry = _cache_entry(name, state_dir)
    if entry is None:
        from rolo_claude.providers.databricks import databricks_route_candidates
        out = []
        for path, include_model in databricks_route_candidates(name):
            key = "mlflow" if "/mlflow/" in path else "invocations"
            out.append(RouteCandidate(key=key, path=path, include_model=include_model,
                                       model_value=name if include_model else None))
        return out

    family = _family_for(name, entry)
    if family == "non_chat":
        return []
    listed = set(entry.get("api_types") or [])
    order_keys = FAMILY_ROUTE_ORDER.get(family, _GENERIC_FALLBACK_ORDER)
    order_keys = [k for k in order_keys if k != "anthropic"]
    wire_model = entry.get("foundation_model_name") or name

    candidates = []
    for key in order_keys:
        info = API_TYPE_INFO[key]
        if info["api_type"] in listed:
            candidates.append(RouteCandidate(key=key, path=info["path"], include_model=True, model_value=wire_model))
    candidates.append(RouteCandidate(key="invocations", path=f"/serving-endpoints/{name}/invocations",
                                      include_model=False, model_value=None))
    return candidates


# ---------------------------------------------------------------------------
# 1.0.1 hotfix 4: "chat-capable" and the display `path` column both come
# from data the cache ALREADY carries (task, and the family/api_types
# routing decision above) -- never a separate, easily-disagreeing heuristic
# like "family != non_chat" (which says "yes" for a family classified as
# chat-shaped by NAME even when the endpoint's own `task` says otherwise).
# ---------------------------------------------------------------------------

CHAT_TASK = "llm/v1/chat"

# Display labels for a route candidate's raw `key` (also `resolve_databricks_
# dialect`'s "anthropic-passthrough" case) -- used ONLY for the human-
# readable text table; machine-readable `--json`/`--urls` output keeps the
# raw key ("mlflow"/"cursor"/"invocations"/"anthropic") unchanged, since
# that shape is already pinned by test_cmd_models_refresh_urls_json_shape.
PATH_TYPE_DISPLAY = {"mlflow": "mlflow-chat", "cursor": "cursor-chat",
                      "anthropic": "anthropic", "invocations": "invocations"}


def is_chat_task(task: Optional[str]) -> bool:
    """`rolo-claude models`/`/models`/`init`'s own "chat-capable" count all
    key off this ONE check now (task == "llm/v1/chat") instead of two
    different, disagreeing heuristics (`family != "non_chat"` in the table,
    `bool(api_types)` in init's summary -- verified live: a workspace whose
    cache predates hotfix 4's own `api_types` migration showed "chat yes"
    from the first and "0 chat-capable" from the second for the SAME rows)."""
    return task == CHAT_TASK


def default_path_type(name: str, state_dir=None) -> str:
    """The raw route-candidate key (`anthropic`/`mlflow`/`cursor`/
    `invocations`) a request for `name` would try FIRST, right now, with NO
    network call -- everything `resolve_databricks_dialect`/
    `chat_route_candidates` need is already local (the discovery cache +
    config.json), so this is always cheap and offline-safe. `"none"` for a
    known non-chat endpoint (nothing to route); `"?"` only if `name` isn't
    even in the cache AND the today's-static-order fallback somehow still
    produced nothing (defensive -- `chat_route_candidates` always returns at
    least the invocations candidate otherwise)."""
    _clean, dialect = resolve_databricks_dialect(name, state_dir)
    if dialect == "anthropic-passthrough":
        return "anthropic"
    cands = chat_route_candidates(name, state_dir)
    if not cands:
        return "none"
    return cands[0].key


def dbu_price_usd() -> Optional[float]:
    """`databricks.dbu_price_usd` (`~/.rolo-claude/config.json`) -- a
    workspace's own $/DBU conversion rate; unset -> cost is shown in raw
    DBUs instead of dollars (scope D/H)."""
    from rolo_claude.theme import get_config_value
    value = get_config_value("databricks.dbu_price_usd", default=None)
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def format_dbu_cost(dbus: Optional[float]) -> str:
    """`dbus` (a raw DBU count, or None when unknown) -> a display string:
    a dollar figure when `databricks.dbu_price_usd` is configured, else the
    raw DBU count, else "?"."""
    if dbus is None:
        return "?"
    price = dbu_price_usd()
    if price is not None:
        return f"${dbus * price:.4f}"
    return f"{dbus:.3f} DBU"
