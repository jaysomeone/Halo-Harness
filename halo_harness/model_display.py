"""halo_harness.model_display -- 1.0.1 hotfix 12: ONE row format for every
model-listing surface (`halo models`, `/models`, the `/model` picker,
`init`'s own picker) -- context window, max output and USD/1M-token prices
on every row, Databricks endpoints included, never wrapped onto a second
line. Verified live: the pre-hotfix `/model` picker showed a cc: row as
`ctx=1000k out=128000 in=$10.00/M out=$50.00/M` (mixed k/raw units, wrapped
past 110 columns) and a Databricks row as only `path=invocations dbu=?`
(no ctx/price at all) -- the owner's own ask was "that info is more
important to display... token costs and context" for every row alike.

Pure functions, no textual/rich import, so the CLI/headless table
(`catalog_cli.py`) and the TUI dialogs (`tui/dialogs/model_picker.py`,
`tui/dialogs/init_picker.py`) share identical formatting without either
depending on the other.
"""

from __future__ import annotations

from typing import Optional

_REF_WIDTH = 36


def format_token_count(n) -> str:
    """`200000` -> `"200k"`, `1048576`/`1050000` -> `"1M"`, `128000` ->
    `"128k"` -- rounds to the nearest whole k/M (real-world context/output
    limits are always close to a round number in one of those units; this
    is a display simplification, not a precise unit converter). `""`
    (never `"?"`) for anything unknown/non-numeric/non-positive -- the
    caller's own column padding keeps a blank field aligned."""
    if isinstance(n, bool) or not isinstance(n, (int, float)):
        return ""
    if n <= 0:
        return ""
    n = int(n)
    if n >= 1_000_000:
        return f"{round(n / 1_000_000)}M"
    if n >= 1_000:
        return f"{round(n / 1000)}k"
    return str(n)


def format_price_per_m(v) -> str:
    """`v` is ALREADY USD per million tokens (every row's own `price_in_per_m`/
    `price_out_per_m` field is normalized to this one unit at the source --
    see `model_row_fields` below -- regardless of whether the underlying
    catalog reported per-token or per-million pricing) -> `"$X.XX/M"`; `""`
    (never `"?"`) when unknown. Never multiplies -- a caller that still has
    a per-TOKEN price must convert (`* 1_000_000`) before calling this.

    1.0.1 fixpass finding 7: also accepts a numeric STRING (models.json
    stores every OpenRouter price that way) -- `float(v)` in a try/except,
    same as the pre-1.0.1 code, so a caller that hands this a string
    straight from the catalog (rather than one already normalized to a
    float upstream) still renders a real price instead of a blank column."""
    if isinstance(v, bool):
        return ""
    try:
        return f"${float(v):.2f}/M"
    except (TypeError, ValueError):
        return ""


def _fmt_status_tokens(n) -> str:
    """Like `format_token_count`, but a status bar's own token FIELDS
    (tokens actually seen this session, never "unpublished" the way a
    catalog row's context/output column can be) show a literal `"0"`
    rather than blank at zero -- `format_token_count(0) == ""` is correct
    for a blank catalog column, wrong for "no turns yet"."""
    if not isinstance(n, (int, float)) or isinstance(n, bool) or n < 0:
        n = 0
    n = int(n)
    return "0" if n == 0 else format_token_count(n)


def format_status_context(tokens, limit) -> str:
    """1.0.1 hotfix 14: the status bar's own context field. `"ctx 12k/1M
    1%"` when a limit is known (the same three-tier resolution as a model
    row's own ctx column -- models.dev, then model_table.json, then the
    family profile -- already lands in `ModelProfile.context_tokens` by
    the time this is called, so this function only ever formats, never
    resolves) -- even before the first turn, when `tokens` is 0:
    `"ctx 0/1M 0%"`. `"ctx 12k"` (used tokens alone, no bar, no percent)
    when no limit is known at all. The literal string `"ctx ?"` (the
    pre-1.0.1 permanent state for every Databricks model, which has no
    context limit in the bare dataclass default) never appears."""
    used = tokens if isinstance(tokens, (int, float)) and not isinstance(tokens, bool) and tokens > 0 else 0
    used_str = _fmt_status_tokens(used)
    if isinstance(limit, (int, float)) and not isinstance(limit, bool) and limit > 0:
        pct = 100.0 * used / limit
        return f"ctx {used_str}/{format_token_count(limit)} {pct:.0f}%"
    return f"ctx {used_str}"


def format_status_cost(cost_usd, total_input_tokens=0, total_output_tokens=0) -> str:
    """1.0.1 hotfix 14: the status bar's own cost field. A real dollar
    figure (`"$0.0123"`) -- whether it's the provider's own reported cost
    (OpenRouter `usage.cost`) or `CostMeter`'s fallback formula computed
    from item-12 prices, the bar can't tell the difference and doesn't
    need to -- when one is known. Otherwise the running raw token totals,
    `"in 12k out 3k"`, since the harness always knows how many tokens
    moved even with no idea what they cost. The literal string `"$?"`
    (the pre-1.0.1 permanent state for Databricks) never appears."""
    if isinstance(cost_usd, (int, float)) and not isinstance(cost_usd, bool):
        return f"${cost_usd:.4f}"
    return f"in {_fmt_status_tokens(total_input_tokens)} out {_fmt_status_tokens(total_output_tokens)}"


def truncate_label_left(label: str, max_width: int) -> str:
    """1.0.1 hotfix 14: truncates `label` (the status bar's own model ref)
    from the LEFT with a leading ellipsis so the end of it -- the part
    that actually distinguishes `dbx:databricks-deepseek-v4-1-flash` from
    `...-v4-1-thinking`, or an OpenRouter slug's own model name from its
    org prefix -- stays visible when the terminal is too narrow for the
    whole ref, instead of the ctx/cost fields after it being pushed
    off-screen entirely. A no-op when `label` already fits."""
    if max_width <= 0:
        return ""
    if len(label) <= max_width:
        return label
    if max_width == 1:
        return "…"
    return "…" + label[-(max_width - 1):]


def format_live_token_count(n) -> str:
    """Halo 2.0.1 W2b (HALO-2.0.1-liveness-tips-brief.md Part A1/A3): a
    LIVE, still-growing token counter during streaming -- "412"/"1.2k" --
    shared by the transcript's own phase line (`✻ Thinking… (18 s · 412
    reasoning tokens)`/`✻ Writing… (23 s · 1.2k tokens)`) and the status
    bar's received-token segment (`↓412`/`↓1.2k`) so neither surface can
    ever show a different number for the same count. Unlike
    `format_token_count` (a catalog column, rounds a stable number of
    tokens to the nearest whole k/M), this keeps ONE decimal place from
    1,000 up to 10,000 -- a live counter changes every delta, so a reader
    watching it tick needs the extra digit "1.2k" gives over a 1,000-wide
    "1k" bucket that won't visibly move again for a while. "0" at zero
    (never blank -- a live counter that hasn't received anything yet is
    still a real reading, same reasoning as `_fmt_status_tokens`)."""
    if not isinstance(n, (int, float)) or isinstance(n, bool) or n < 0:
        n = 0
    n = int(n)
    if n < 1000:
        return str(n)
    if n < 10_000:
        return f"{n / 1000:.1f}k"
    if n < 1_000_000:
        return f"{round(n / 1000)}k"
    return f"{round(n / 1_000_000)}M"


def format_ollama_throughput(tokens_per_second, prefill_seconds, offloaded) -> str:
    """Halo 2.0.3 round 5b (brief item 7): the status bar's `ol:`-only
    throughput segment, shown right next to the model chip -- e.g.
    `"41 tok/s · prefill 1.2 s"`, with `"· offloaded"` appended when the
    last `/api/ps` read found the model partially in system RAM. `""`
    (the whole segment omitted -- same "blank, not a placeholder"
    convention as every other optional status-bar field) when NEITHER
    figure is known yet (no `ol:` reply this session, or a non-ollama
    route -- `Session.status_event` never passes these for one)."""
    parts = []
    if (isinstance(tokens_per_second, (int, float)) and not isinstance(tokens_per_second, bool)
            and tokens_per_second > 0):
        parts.append(f"{tokens_per_second:.0f} tok/s")
    if (isinstance(prefill_seconds, (int, float)) and not isinstance(prefill_seconds, bool)
            and prefill_seconds >= 0):
        parts.append(f"prefill {prefill_seconds:.1f} s")
    if not parts:
        return ""
    text = " · ".join(parts)
    return f"{text} · offloaded" if offloaded else text


def format_elapsed_seconds(seconds) -> str:
    """Halo 2.0.1 W2b (liveness-tips-brief Part A1/A3/A4/A5): the ONE
    "N s" elapsed-time wording every liveness surface shares -- the phase
    line (`12 s`), the status-bar cluster (`18 s`), a running tool card's
    header (`12 s`) and a sub-agent card (`9 s`). Always whole seconds
    (floored, never rounded up past a second that hasn't fully elapsed
    yet), always with the space before "s" -- a surface that drifted to
    "18s" (no space) would read as a different convention than the rest
    of the liveness UI for no reason."""
    try:
        n = int(max(0.0, float(seconds)))
    except (TypeError, ValueError):
        n = 0
    return f"{n} s"


# Shown ONCE above any list of `format_model_row` lines (not per-row).
ROW_HEADER = "ctx = context window · out = max output · prices in USD per 1M tokens · blank = not published"


def format_model_row(entry: dict) -> str:
    """`entry`: `{"ref", "context_tokens", "max_output_tokens",
    "price_in_per_m", "price_out_per_m", "dbu", "detail"}` (every key but
    `ref` optional). `dbu` (a Databricks DBU/token rate or its USD
    conversion -- `dbx_routing.format_dbu_cost`'s own string, a wholly
    different unit than the USD/1M prices) is appended only when known,
    never as a bare "?". `detail` is a short extra tag in brackets after
    everything else -- a Databricks row's own "<family> · <path>",
    typically; omitted entirely (no empty `[]`) when not given, since an
    OpenRouter/cc: row has no equivalent.

    Always exactly one line -- ellipsizing a too-long ref/detail for a
    fixed-width display is the CALLER's job (Rich `Text(no_wrap=True,
    overflow="ellipsis")` in the TUI, plain truncation in the CLI table),
    never done here."""
    ref = entry.get("ref", "")
    ctx = format_token_count(entry.get("context_tokens"))
    out = format_token_count(entry.get("max_output_tokens"))
    price_in = format_price_per_m(entry.get("price_in_per_m"))
    price_out = format_price_per_m(entry.get("price_out_per_m"))
    row = f"{ref:<{_REF_WIDTH}} ctx={ctx:<5} out={out:<5} in={price_in:<8} out={price_out:<8}"
    dbu = entry.get("dbu")
    if dbu:
        row += f" dbu={dbu}"
    detail = entry.get("detail")
    if detail:
        row += f"  [{detail}]"
    return row


def databricks_row_fields(name: str, *, state_dir=None, model_table: Optional[dict] = None,
                           live_models_dev: Optional[dict] = None,
                           vendored_fallback: Optional[dict] = None) -> dict:
    """`{"context_tokens", "max_output_tokens", "price_in_per_m",
    "price_out_per_m"}` for a Databricks endpoint `name`, per the ordered
    source rule:

      (a) the models.dev `databricks` provider entry whose id EQUALS `name`
          (the live `~/.halo/models-dev.json` cache first, then the
          vendored `catalog/models_dev_databricks_fallback.json`) --
          `limit.context`/`limit.output` for ctx/out, `cost.input`/
          `cost.output` for prices, all used AS-IS (models.dev's own cost
          unit already IS USD per million tokens -- no `/1_000_000` or
          `*1_000_000` conversion here, unlike `databricks_profile_fields_
          from_models_dev`, which converts to per-TOKEN for `ModelProfile`
          instead; this is a deliberately SEPARATE reading of the same
          entry for DISPLAY, not routed through that per-token conversion
          and back).
      (b) missing (a): `model_table.json`'s own `context_tokens` for this
          endpoint (request-shaping data, not economics -- output/prices
          stay blank).
      (c) neither: every field blank.

    Never raises; a malformed/missing entry at any step just leaves the
    corresponding field(s) out of the returned dict (the caller's own
    `.get(...)` then reads them as `None`, which `format_token_count`/
    `format_price_per_m` both render as "", never "?").

    1.0.1 fixpass finding 1: `live_models_dev`/`vendored_fallback` let a
    caller about to call this once per Databricks endpoint (Controller.
    list_models()'s own loop) load+parse `models-dev.json` (several MB on
    a real catalog) and the vendored fallback file ONCE for the whole
    batch, instead of once per endpoint -- 30 rows on a real catalog
    re-read and re-parsed the same file 30 times (1.37s measured on a fast
    host, the dominant cost behind `/model`'s multi-second freeze). Both
    default to the original per-call load when omitted, so every OTHER
    caller (doctor, the CLI table, existing tests) is unaffected."""
    from halo_harness.config.paths import bridge_home
    from halo_harness.providers.models_dev import (
        databricks_entries_from_full_models_dev, load_models_dev_json, load_vendored_databricks_fallback,
    )
    if live_models_dev is None:
        state_dir = state_dir if state_dir is not None else bridge_home()
        live_models_dev = databricks_entries_from_full_models_dev(load_models_dev_json(state_dir))
    if vendored_fallback is None:
        vendored_fallback = load_vendored_databricks_fallback()
    entry = live_models_dev.get(name) or vendored_fallback.get(name)
    if isinstance(entry, dict):
        limit = entry.get("limit") if isinstance(entry.get("limit"), dict) else {}
        cost = entry.get("cost") if isinstance(entry.get("cost"), dict) else {}
        out: dict = {}
        if isinstance(limit.get("context"), (int, float)):
            out["context_tokens"] = limit["context"]
        if isinstance(limit.get("output"), (int, float)):
            out["max_output_tokens"] = limit["output"]
        if isinstance(cost.get("input"), (int, float)):
            out["price_in_per_m"] = cost["input"]
        if isinstance(cost.get("output"), (int, float)):
            out["price_out_per_m"] = cost["output"]
        return out

    # (b) model_table.json's own context_tokens (request-shaping data --
    # see model.py::resolve_model_profile's identical lookup for pricing-
    # free routing defaults); output/prices stay blank.
    if model_table is None:
        from halo_harness.providers.profiles import load_model_table
        model_table = load_model_table()
    table_entry = (model_table.get("databricks") or {}).get(name) if isinstance(model_table, dict) else None
    if isinstance(table_entry, dict) and isinstance(table_entry.get("context_tokens"), int):
        return {"context_tokens": table_entry["context_tokens"]}
    return {}
