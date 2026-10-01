"""halo_harness.tui.events -- pure event-queue helpers for the TUI's drain
loop (D-TUI: "merges consecutive deltas per block"). No textual import, no
I/O, no widget access -- unit-testable on its own, imported by both
`tui/app.py` (real use) and `test_tui.py` (coalescing tests).
"""

from __future__ import annotations

from halo_harness.events import Event

# Kinds that may be coalesced: consecutive deltas for the SAME (kind, turn,
# index) are merged into one Event carrying the concatenated text, so a
# fast-streaming model doesn't force one widget update per token. Every
# other kind passes through untouched, in order.
_DELTA_KINDS = frozenset({"text_delta", "thinking_delta"})


def _same_block(a: Event, b: Event) -> bool:
    return (
        a.kind == b.kind
        and a.kind in _DELTA_KINDS
        and a.turn == b.turn
        and a.agent_id == b.agent_id
        and a.data.get("index") == b.data.get("index")
    )


def drain_queue(raw_events: "list[Event]") -> "list[Event]":
    """Coalesce a batch of events pulled off the queue this tick: any run of
    consecutive `text_delta`/`thinking_delta` events sharing (kind, turn,
    agent_id, index) collapses into ONE event whose `data["text"]` is their
    concatenation (every other field taken from the FIRST event in the
    run -- `ts` in particular, so ordering-by-arrival stays meaningful).
    Anything that isn't a delta, or a delta that doesn't chain onto the
    previous output event, is appended as-is. Order is always preserved;
    this never reorders or drops a non-delta event."""
    out: "list[Event]" = []
    for ev in raw_events:
        if out and _same_block(out[-1], ev):
            prev = out[-1]
            merged_text = prev.data.get("text", "") + ev.data.get("text", "")
            out[-1] = Event(prev.kind, {**prev.data, "text": merged_text}, turn=prev.turn,
                             agent_id=prev.agent_id, ts=prev.ts)
            continue
        out.append(ev)
    return out
