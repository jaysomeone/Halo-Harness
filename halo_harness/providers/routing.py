"""rolo_claude.providers.routing -- model-ref routing (Route/route_model),
tool selection/conversion, tool_choice mapping, profile resolution, and the
Databricks Claude passthrough body/header builders. Moved out of bridge.py
unchanged in the H0 package split; see wip/SIGNATURES.md parts 2 and
"M4-M6 additions" (passthrough section).
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from rolo_claude.providers.config import extract_custom_headers
from rolo_claude.providers.databricks import load_models_json


class InvalidModelError(Exception):
    pass


@dataclass
class Route:
    provider: str  # "openrouter"|"databricks"|"invalid"
    upstream_model: str
    dialect: str  # "openai-chat"|"anthropic-passthrough"


def _dbx_dialect(stripped_model: str) -> str:
    """Databricks dialect for an already dbx:-stripped model name. H14
    scope D: delegates to the shared family-aware
    `providers.dbx_routing.resolve_databricks_dialect` instead of a bare
    "claude" substring check -- that check alone wrongly sent Bedrock
    EXTERNAL Claude endpoints (`us-anthropic-claude-*`,
    `claude-3-5-sonnet-...`, no `databricks-claude-` prefix) down the
    native Anthropic passthrough dialect too; those are invocations-only,
    openai-chat-shaped endpoints despite "claude" being in the name."""
    from rolo_claude.providers.dbx_routing import resolve_databricks_dialect
    return resolve_databricks_dialect(stripped_model)[1]


def _dbx_route(stripped_model: str) -> Route:
    """`Route("databricks", <clean name>, <dialect>)` -- the clean name
    strips a `@anthropic` suffix (scope D's per-call gateway override) so
    the proxy never sends that suffix upstream as if it were part of the
    real endpoint/model name."""
    from rolo_claude.providers.dbx_routing import resolve_databricks_dialect
    clean, dialect = resolve_databricks_dialect(stripped_model)
    return Route("databricks", clean, dialect)


def route_model(name: str, headers: dict[str, str], cfg) -> Route:
    """Resolve model name to route; raises InvalidModelError for invalid."""
    # Helper to strip a dbx:/or: prefix and return the matching Route, else
    # None. NOTE: dbx:'s dialect must be decided the SAME way as the
    # unprefixed databricks-*/system.ai. fallback further down (via
    # _dbx_route/_dbx_dialect) -- a bug in an earlier draft hardcoded
    # "anthropic-passthrough" for every dbx: name regardless of whether it
    # was actually a Claude model, silently sending non-Claude Databricks
    # models down the raw-relay path instead of the openai-chat translation.
    def prefixed(name_to_check):
        if name_to_check.startswith("dbx:"):
            return _dbx_route(name_to_check[len("dbx:"):])
        if name_to_check.startswith("or:"):
            return Route("openrouter", name_to_check[len("or:"):], "openai-chat")
        return None

    # Prefix wins first
    if r := prefixed(name):
        return r

    # Tier/alias resolution
    resolved = name
    if "/" not in name:  # not a vendor/model
        # Determine tier
        is_small = "haiku" in name.lower() or name.endswith("-small")
        header_key = "x-bridge-small" if is_small else "x-bridge-main"
        if header_key in headers:
            resolved = headers[header_key]
        else:
            env_key = "BRIDGE_MODEL_SMALL" if is_small else "BRIDGE_MODEL"
            resolved = os.environ.get(env_key, name)
        # Re-run prefix logic on resolved ref
        if r := prefixed(resolved):
            return r

    # Direct vendor/model
    if "/" in resolved and resolved.count("/") == 1:
        return Route("openrouter", resolved, "openai-chat")

    # Databricks patterns
    if resolved.startswith("databricks-") or resolved.startswith("system.ai."):
        return _dbx_route(resolved)

    raise InvalidModelError(
        f"no route: {name!r} (accepted forms are dbx:, or:, vendor/model, or a configured tier alias)"
    )


def is_passthrough_ref(model: str) -> bool:
    """True if a launcher --model value resolves to the Databricks Claude
    passthrough dialect. Mirrors route_model's own two prefix-checking code
    paths exactly, reusing _dbx_dialect for the actual "claude" check in
    both, instead of independently requiring a databricks-/system.ai. prefix
    on a dbx:-prefixed name (finding 10) -- route_model sends ANY dbx:X name
    containing "claude" to passthrough regardless of what X starts with, so
    a bare `dbx:claude-sonnet-4-5` ref used to be misclassified here as a
    plain openai-chat ref, wrongly forcing MAX_THINKING_TOKENS=0 and
    ENABLE_TOOL_SEARCH on what the server actually treats as a raw Claude
    relay."""
    if model.startswith("dbx:"):
        return _dbx_dialect(model[len("dbx:"):]) == "anthropic-passthrough"
    if model.startswith("databricks-") or model.startswith("system.ai."):
        return _dbx_dialect(model) == "anthropic-passthrough"
    return False


def select_tools(tools: list[dict] | None, messages: list[dict]) -> list[dict]:
    """Select tools based on references, cap at 128, strip defer_loading."""
    if not tools:
        return []
    referenced = collect_tool_references(messages)
    core = []
    deferred_refd = []
    for t in tools:
        name = t.get("name")
        if not name:
            continue
        defer = t.get("defer_loading", False)
        if not defer:
            core.append(t)
        elif name in referenced:
            deferred_refd.append(t)
    selected = core + deferred_refd
    selected = selected[:128]
    # Strip defer_loading key
    for t in selected:
        t.pop("defer_loading", None)
    return selected


def collect_tool_references(messages) -> set[str]:
    """Recursively collect tool names from tool_reference blocks."""
    refs = set()

    def walk(obj):
        if isinstance(obj, dict):
            if obj.get("type") == "tool_reference":
                refs.add(obj.get("tool_name") or obj.get("name") or "")
            for v in obj.values():
                walk(v)
        elif isinstance(obj, list):
            for item in obj:
                walk(item)

    walk(messages)
    return refs


def sanitize_tool_schema(schema: dict) -> dict:
    """Shallow copy, pop "$schema" only."""
    copy = dict(schema)
    copy.pop("$schema", None)
    return copy


def anthropic_tool_to_openai(t: dict) -> dict:
    """Convert Anthropic tool definition to OpenAI format."""
    return {
        "type": "function",
        "function": {
            "name": t["name"],
            "description": t.get("description", ""),
            "parameters": sanitize_tool_schema(t.get("input_schema") or {"type": "object", "properties": {}})
        }
    }


def map_tool_choice(tc: dict | str | None) -> str | dict | None:
    """Map Anthropic tool_choice to OpenAI format."""
    if tc is None or tc == "auto":
        return "auto"
    if isinstance(tc, str):
        return tc  # fallback
    if not isinstance(tc, dict):
        return None
    typ = tc.get("type")
    if typ == "auto":
        return "auto"
    if typ == "any":
        return "required"
    if typ == "none":
        return "none"
    if typ == "tool":
        name = tc.get("name")
        if name:
            return {"type": "function", "function": {"name": name}}
    return None


def resolve_profile(model_id: str, state_dir, routes_cfg: dict | None) -> dict:
    """Resolve {'context_tokens': int, 'max_output_tokens': int} for a model id: models.json entry, else routes_cfg['profiles'] entry, else routes_cfg['profiles']['default'], else the hardcoded 16384/128000 fallback."""
    models = load_models_json(state_dir)
    entry = models.get(model_id)
    if entry:
        return {"context_tokens": entry.get("context_length") or 128000,
                "max_output_tokens": entry.get("max_output_tokens") or 16384}
    profiles = (routes_cfg or {}).get("profiles") or {}
    entry = profiles.get(model_id) or profiles.get("default")
    if entry:
        return {"context_tokens": entry.get("context_tokens") or 128000,
                "max_output_tokens": entry.get("max_output_tokens") or 16384}
    return {"context_tokens": 128000, "max_output_tokens": 16384}


def clamp_max_tokens(requested: int, profile: dict, prompt_estimate: int) -> int:
    """Up-front max_tokens clamp: min(requested, profile max output, context -
    1.1*estimate - 512), floor 1. If that headroom term collapses below a
    sane floor (min(requested, 4096) -- e.g. our own estimate got inflated
    by a large embedded image), the headroom term is skipped entirely rather
    than clamping max_tokens down to a near-useless value: a genuine overflow
    is then left for the real upstream 400/retry path, which has the actual
    numbers instead of our own estimate (finding 1)."""
    max_output = profile.get("max_output_tokens", 16384)
    headroom = profile.get("context_tokens", 128000) - int(1.1 * prompt_estimate) - 512
    floor = min(requested, 4096) if requested > 0 else 4096
    if headroom < floor:
        return max(1, min(requested, max_output))
    candidate = min(requested, max_output, headroom)
    return max(1, candidate)



def strip_bridge_signed_thinking(messages: list) -> list:
    """Return a copy of messages with any assistant 'thinking' content block whose signature starts with 'bridge1.' removed."""
    result = []
    for msg in messages:
        content = msg.get("content") if isinstance(msg, dict) else None
        if isinstance(content, list):
            new_content = []
            for block in content:
                if isinstance(block, dict) and block.get("type") == "thinking" and \
                        str(block.get("signature", "")).startswith("bridge1."):
                    continue
                new_content.append(block)
            result.append({**msg, "content": new_content})
        else:
            result.append(dict(msg) if isinstance(msg, dict) else msg)
    return result


def filter_beta_header(value: str) -> str:
    """Drop any comma-separated anthropic-beta flag matching 'tool-search-tool-*'; return the rejoined remainder (possibly empty string)."""
    if not value:
        return ""
    parts = [p.strip() for p in value.split(",")]
    filtered = [p for p in parts if not p.lower().startswith("tool-search-tool-")]
    return ", ".join(filtered)


def replace_tool_reference_blocks(messages: list) -> list:
    """Return a copy of messages with every {"type":"tool_reference",...}
    content block replaced by a plain text block naming the tool. Databricks'
    native Claude endpoint has no tool-search-tool-* beta enabled on the
    passthrough relay (we strip it) and 400s on this unrecognized block type
    -- the first ToolSearch result in the history breaks every later
    passthrough request otherwise (finding 5)."""
    def walk(obj):
        if isinstance(obj, dict):
            if obj.get("type") == "tool_reference":
                name = obj.get("tool_name") or obj.get("name") or "unknown"
                return {"type": "text", "text": f"(tool available: {name})"}
            return {k: walk(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [walk(item) for item in obj]
        return obj

    return [walk(msg) for msg in messages]


def build_passthrough_body(body: dict, route: Route) -> dict:
    """Rewrite an Anthropic-format request body for Databricks Claude passthrough: model rewrite, tool cap/reference handling, thinking-block strip."""
    result = dict(body)
    result["model"] = route.upstream_model
    if body.get("tools"):
        result["tools"] = select_tools(body["tools"], body.get("messages", []))
    messages = strip_bridge_signed_thinking(body.get("messages", []))
    result["messages"] = replace_tool_reference_blocks(messages)
    return result


def build_passthrough_headers(incoming_headers, gateway_token: str) -> dict:
    """Build the outgoing header dict for a Databricks Claude passthrough request."""
    result = extract_custom_headers(incoming_headers)
    beta_key = None
    for k in list(result.keys()):
        if k.lower() == "anthropic-beta":
            beta_key = k
            break
    if beta_key is not None:
        filtered = filter_beta_header(result[beta_key])
        if filtered:
            result[beta_key] = filtered
        else:
            result.pop(beta_key, None)
    result["Authorization"] = f"Bearer {gateway_token}"
    return result


