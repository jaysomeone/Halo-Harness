"""rolo_claude.improve.hint -- H10 Part B4: a per-session, COUNTERS-ONLY
hint check -- never a model call, never interrupts a running turn (auto
mode identical: status-bar hint only). `improve.hint: false` disables.
"""

from __future__ import annotations

from typing import Optional


def count_repair_hits(nodes) -> int:
    n = 0
    for node in nodes:
        if isinstance(node, dict) and node.get("type") == "assistant":
            for meta in (node.get("tool_meta") or {}).values():
                if isinstance(meta, dict) and meta.get("repaired"):
                    n += 1
    return n


def count_edit_failures(nodes) -> int:
    n = 0
    for node in nodes:
        if (isinstance(node, dict) and node.get("type") == "tool_result" and node.get("tool") == "Edit"
                and node.get("is_error") and node.get("error_class") in ("not_found", "multiple_matches")):
            n += 1
    return n


def count_loop_breaker_trips(nodes) -> int:
    return sum(1 for node in nodes if isinstance(node, dict) and node.get("type") == "tool_result"
               and node.get("error_class") == "loop_breaker")


def should_hint(nodes, cfg) -> Optional[int]:
    """Returns an estimated candidate-cluster count (>= 1) once the
    session's own counters cross `cfg.hint_threshold`
    (default: >= 3 repair hits OR >= 2 edit failures OR 1 loop-breaker
    trip); None when nothing crosses, or when `cfg.hint` is False."""
    if not cfg.hint:
        return None
    threshold = cfg.hint_threshold or {}
    hits = 0
    if count_repair_hits(nodes) >= threshold.get("repairs", 3):
        hits += 1
    if count_edit_failures(nodes) >= threshold.get("edit_failures", 2):
        hits += 1
    if count_loop_breaker_trips(nodes) >= threshold.get("loop_breaker", 1):
        hits += 1
    return hits or None
