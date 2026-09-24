#!/usr/bin/env python3
"""ingest_model_table.py -- H2 must-do 2: parse the single JSON block out of
`reports/Open weight model adapter rules.md` (hosts + families + 64 model
rows) and compile it into `rolo_claude/providers/model_table.json`: for
every (model, host) pair where the model has a non-null alias on that host,
compute the EFFECTIVE row as `family ⊕ model ⊕ host` (family fields, model
overrides, then host-specific overlays from `limits_by_host`/
`reasoning.per_host`/`rate_limits`), then flatten it to the exact field
vocabulary `providers/profiles.py.resolve_profile` and the H2 code-branch
hooks (`providers/hooks.py`) consume. Output keeps the SAME top-level shape
the old hand-authored table had -- `{"openrouter": {<alias>: {...}},
"databricks": {<alias>: {...}}}` -- so `resolve_profile`'s existing
`model_table.get(host).get(upstream_model)` lookup needs no change.

`unverified` flags are kept: every compiled row carries an `unverified` list
merged from the model row's own `unverified` key, the top-level
`data["unverified"]` entries scoped to this model/family/host, and the
family's own `unverified` key when present.

Run: `python tools/ingest_model_table.py` (idempotent; re-run whenever the
report changes -- this script is the ONLY thing that reads the .md report at
runtime; the shipped package only ever reads the compiled model_table.json).
"""
from __future__ import annotations

import copy
import json
import re
import sys
from pathlib import Path

REPO_DIR = Path(__file__).resolve().parent.parent
REPORT_PATH = REPO_DIR / "reports" / "Open weight model adapter rules.md"
OUT_PATH = REPO_DIR / "rolo_claude" / "providers" / "model_table.json"

_STANDARD_LIMIT_KEYS = ("context", "output_cap", "output_default")

# Coarse reasoning_replay values providers/profiles.py's ProviderProfile
# already understands: "text" | "empty" | "details" | "thinking".
_REPLAY_MAP = {
    "echo_required_400": "text",
    "echo_required": "text",
    "echo_recommended": "text",
    "drop_after_final": "text",
    "opaque_roundtrip": "details",
    "strip_prior_turns": "empty",
    "strip_prior_turns_keep_tool_steps": "empty",
    "none": "empty",
}

# DeepSeek V4's "required/named -> 400 in thinking mode" caveat means
# "required" being nominally in the family's tool_choice_modes must NOT
# translate into tool_choice_required_supported=True (report section "Native
# tool calling..."; matches the pre-H2 hand-authored table + test_profiles.py).
_TOOL_CHOICE_REQUIRED_OVERRIDE = {"deepseek-v4": False}

GLOBAL_UNVERIFIED: list = []  # filled from data["unverified"] in main()


def extract_json_block(text: str) -> dict:
    """Grab the fenced ```json ... ``` block whose content starts with
    `{"model_table_version"` -- the report's own consolidated table, never
    hand-copied. LINE-based fence matching (not a regex over the whole
    text): several quirks strings inside the block themselves contain an
    inline ` ```tool_code``` ` marker, which a naive non-line-anchored
    ``` -> ``` regex matches as a premature close. A fence line is only
    recognized when the WHOLE (stripped) line is exactly ```/```json."""
    lines = text.splitlines()
    start = None
    for i, line in enumerate(lines):
        if line.strip() == "```json":
            start = i + 1
            break
    if start is None:
        raise SystemExit("no ```json fence found in the report")
    end = None
    for j in range(start, len(lines)):
        if lines[j].strip() == "```":
            end = j
            break
    if end is None:
        raise SystemExit("no closing ``` fence found after the ```json fence")
    block = "\n".join(lines[start:end])
    data = json.loads(block)
    if "model_table_version" not in data:
        raise SystemExit("fenced JSON block found but has no model_table_version key")
    return data


def deep_merge(base: dict, override: dict) -> dict:
    """`override` wins; dict values merge recursively (model row overrides
    only the family sub-keys it actually sets), list/scalar values replace
    outright (a model row's own list is authoritative, never concatenated)."""
    out = copy.deepcopy(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def overlay_limits(limits: dict, by_host: dict) -> dict:
    """Only the documented schema keys (context/output_cap/output_default)
    are structural overrides; a row's other `limits_by_host` keys (e.g.
    `top_provider_output`) are informal notes the base `limits` already
    accounts for when we pin to the right endpoint, so they're intentionally
    NOT applied here."""
    out = dict(limits)
    for k in _STANDARD_LIMIT_KEYS:
        if k in by_host:
            out[k] = by_host[k]
    return out


def compile_openrouter_pin(provider_pin) -> "dict | None":
    """A handful of rows (the home default `deepseek-v4.1-flash`, `kimi-k3`)
    carry BOTH `order` (a strict primary pin, `allow_fallbacks: false`) and
    an informal `fallback_order` -- not a real OpenRouter `provider` field
    at all, just the report's own two-tier annotation ("prefer these, fall
    back to these, never fall back further than that"). Verified live: with
    `allow_fallbacks: false` and ONLY the primary in `order`, an account
    whose OpenRouter Guardrails (ZDR / no-paid-model-training) exclude that
    one provider gets a hard "No endpoints found" with no fallback at all.
    OpenRouter's real `provider` object has no two-tier concept -- the
    faithful mapping is `order = primary + fallback_order` with
    `allow_fallbacks: true`, so OpenRouter tries the primary(s) first and
    only moves to the named fallbacks (never further) if genuinely needed."""
    if not isinstance(provider_pin, dict):
        return provider_pin
    fallback = provider_pin.get("fallback_order")
    if not fallback:
        return provider_pin
    pin = dict(provider_pin)
    pin["order"] = list(pin.get("order") or []) + list(fallback)
    pin["allow_fallbacks"] = True
    pin.pop("fallback_order", None)
    return pin


def pick_output_default(output_default, *, default_on: bool, output_cap) -> int:
    """`limits.output_default` is either a plain int, null, or (DeepSeek V4)
    a dict of {non_thinking, thinking, effort_max} -- pick the value for
    this row's default thinking state, falling back to a conservative
    fraction of the output cap when the report gives no number at all."""
    if isinstance(output_default, dict):
        key = "thinking" if default_on else "non_thinking"
        val = output_default.get(key) or output_default.get("non_thinking") or output_default.get("thinking")
        if isinstance(val, int):
            return val
    elif isinstance(output_default, int):
        return output_default
    cap = output_cap if isinstance(output_cap, int) and output_cap > 0 else 16384
    return min(cap, 32768)


def compile_row(model: dict, families: dict, host: str) -> "dict | None":
    alias = (model.get("aliases") or {}).get(host)
    if not alias:
        return None
    family_key = model["family"]
    family = families[family_key]
    merged = deep_merge(family, model)

    limits = merged.get("limits") or {}
    by_host = (merged.get("limits_by_host") or {}).get(host) or {}
    limits = overlay_limits(limits, by_host)

    reasoning = dict(merged.get("reasoning") or {})
    per_host = reasoning.pop("per_host", None) or {}
    # Most per_host[host] entries are structured overrides; a few (grok's
    # databricks entry) are informal prose notes ("configurable (grok-4-6)")
    # -- only a dict is a real override, anything else is left as a note.
    if isinstance(per_host.get(host), dict):
        reasoning = deep_merge(reasoning, per_host[host])

    rate_limits = (merged.get("rate_limits") or {}).get(host)

    sampling = merged.get("sampling") or {}
    mode = sampling.get("mode", "vendor_default")
    temperature = top_p = top_k = None
    if mode != "omit":
        default_on = bool(reasoning.get("default_on") or reasoning.get("mandatory"))
        pick = sampling.get("thinking_on") if default_on else sampling.get("thinking_off")
        if not isinstance(pick, dict):
            pick = sampling.get("thinking_on") if isinstance(sampling.get("thinking_on"), dict) else (sampling.get("thinking_off") or {})
        temperature = pick.get("temperature")
        top_p = pick.get("top_p")
        top_k = pick.get("top_k")
    use_temperature = bool(
        temperature is not None or (top_p is not None and top_p != 1.0) or top_k is not None
    )

    default_on = bool(reasoning.get("default_on") or reasoning.get("mandatory"))
    output_cap = limits.get("output_cap")
    max_tokens_default = pick_output_default(limits.get("output_default"), default_on=default_on, output_cap=output_cap)
    if host == "databricks" and isinstance(rate_limits, dict) and isinstance(rate_limits.get("otpm"), int):
        otpm = rate_limits["otpm"]
        max_tokens_cap = otpm
        max_tokens_default = min(max_tokens_default, otpm)
    else:
        max_tokens_cap = output_cap if isinstance(output_cap, int) and output_cap > 0 else max_tokens_default

    # "named" (a specific {"type":"function","function":{"name":...}} call)
    # is a DIFFERENT tool_choice mode than "required" (any tool) -- only
    # "required" gates whether an Anthropic {"type":"any"} downgrades to
    # "auto" (request.py); Qwen/DashScope explicitly rejects "required" even
    # though several qwen rows list "named" as accepted (test_profiles.py:
    # qwen/qwen3-coder must resolve False here).
    tool_choice_modes = merged.get("tool_choice_modes") or []
    tc_required = "required" in tool_choice_modes
    if family_key in _TOOL_CHOICE_REQUIRED_OVERRIDE:
        tc_required = _TOOL_CHOICE_REQUIRED_OVERRIDE[family_key]

    replay_raw = reasoning.get("replay")
    reasoning_replay = _REPLAY_MAP.get(replay_raw, "empty")
    effort_values = reasoning.get("effort_values") or []
    reasoning_effort_supported = bool(effort_values) or bool(reasoning.get("mandatory"))

    unverified = list(model.get("unverified") or []) + list(family.get("unverified") or [])
    prefix_model = f"models.{model['id']}."
    prefix_family = f"families.{family_key}."
    prefix_host = f"hosts.{host}."
    for entry in GLOBAL_UNVERIFIED:
        if entry.startswith(prefix_model) or entry.startswith(prefix_family) or entry.startswith(prefix_host):
            unverified.append(entry)

    return {
        "family": family_key,
        "use_temperature": use_temperature,
        "temperature": temperature,
        "top_p": top_p,
        "top_k": top_k,
        "context_tokens": limits.get("context"),
        "max_tokens_default": max_tokens_default,
        "max_tokens_cap": max_tokens_cap,
        "reasoning_replay": reasoning_replay,
        "reasoning_dual_field": replay_raw == "echo_required_400",
        "reasoning_effort_supported": reasoning_effort_supported,
        "reasoning_default_effort": reasoning.get("default_effort"),
        "reasoning_no_disable": bool(reasoning.get("mandatory", False)),
        "reasoning_field": reasoning.get("field"),
        "tool_choice_required_supported": tc_required,
        "tool_id_format": merged.get("tool_id_format", "preserve"),
        "tool_leak_patterns": merged.get("tool_leak_patterns") or [],
        "system_placement": merged.get("system_placement", "first"),
        "rate_limits": rate_limits,
        "sampling_unsupported_params": sampling.get("unsupported_params") or [],
        "openrouter_pin": compile_openrouter_pin(merged.get("provider_pin")) if host == "openrouter" else None,
        "edit_format": "diff",
        "unverified": sorted(set(unverified)),
    }


def main() -> int:
    global GLOBAL_UNVERIFIED
    data = extract_json_block(REPORT_PATH.read_text(encoding="utf-8"))
    families = data["families"]
    models = data["models"]
    GLOBAL_UNVERIFIED = list(data.get("unverified") or [])

    out: dict = {"openrouter": {}, "databricks": {}}
    row_count = 0
    for model in models:
        for host in ("openrouter", "databricks"):
            row = compile_row(model, families, host)
            if row is None:
                continue
            alias = model["aliases"][host]
            out[host][alias] = row
            row_count += 1

    OUT_PATH.write_text(json.dumps(out, indent=2, ensure_ascii=False, sort_keys=False) + "\n", encoding="utf-8")
    print(f"ingested {len(models)} model rows -> {row_count} (host, alias) rows "
          f"({len(out['openrouter'])} openrouter, {len(out['databricks'])} databricks) -> {OUT_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
