"""halo_harness.tools.truncate -- result truncation to disk (H2 scope A): a
tool result over its tool's `result_cap` is spilled in full to
`<session_dir>/tool-results/<tool_use_id>.txt`; the model sees the head 60%
+ tail 30% of the cap, with the middle 10% replaced by a pointer line naming
the spill file and how many characters were omitted. Called from
agent/loop.py right after `ToolRegistry.dispatch` returns, never by a tool
itself (a tool doesn't know its own session directory).

H8 scope D: OpenCode's own 2 000-line / 50 KB backstop cap (reports/OpenCode
harness deep review.md item 15) is layered ON TOP of the per-tool char cap
above, for any tool whose own `result_cap` is looser than this OR that has
many short lines under its char cap but over the line count (a grep with a
few thousand short matches, a build log) -- it can only ever make the
EFFECTIVE cap SMALLER than what the per-tool cap alone would pick, never
looser, and it uses OpenCode's own "read the saved file with offset/limit"
hint text when IT is what actually bound the result. A tool with
`result_cap=None` (documented as "this tool manages its own truncation" --
today, only the Agent/Task tool, which already applies its own smaller cap
before this function ever sees the text) is a deliberate opt-out from BOTH
caps, preserved unchanged: re-applying a second, generic cap on top of a
tool that already spilled the FULL text itself would silently overwrite
that first spill file with a truncated copy.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

# OpenCode's own truncation constants (deep review item 15/Appendix D).
MAX_LINES = 2000
MAX_BYTES = 50 * 1024


def spill_and_truncate(content: str, *, cap: Optional[int], session_dir: Optional[Path],
                        tool_use_id: Optional[str]) -> str:
    if cap is None or not isinstance(content, str):
        return content

    # H8 scope D: an equivalent CHARACTER budget for the 2000-line/50KB
    # backstop, computed only when it would actually bind tighter than the
    # per-tool `cap` already given -- never touches the `cap=None` opt-out
    # above, and never loosens an already-tighter per-tool cap.
    effective_cap = cap
    use_backstop_wording = False
    line_count = content.count("\n") + (0 if content == "" or content.endswith("\n") else 1)
    byte_len = len(content.encode("utf-8", errors="replace"))
    if line_count > MAX_LINES or byte_len > MAX_BYTES:
        candidates = []
        if byte_len > MAX_BYTES:
            candidates.append(MAX_BYTES)
        if line_count > MAX_LINES:
            # The character length of just the first MAX_LINES lines -- an
            # ASCII-equivalent approximation of "2000 lines" as a char
            # count, which is exactly what this function's own head/tail
            # slice below already operates in.
            candidates.append(len("\n".join(content.split("\n")[:MAX_LINES])))
        backstop_cap = min(candidates)
        if backstop_cap < effective_cap:
            effective_cap = backstop_cap
            use_backstop_wording = True

    if len(content) <= effective_cap:
        return content

    head_len = int(effective_cap * 0.6)
    tail_len = int(effective_cap * 0.3)
    head = content[:head_len]
    tail = content[-tail_len:] if tail_len > 0 else ""
    omitted = len(content) - head_len - tail_len

    spill_note = ""
    if session_dir is not None and tool_use_id:
        try:
            results_dir = Path(session_dir) / "tool-results"
            results_dir.mkdir(parents=True, exist_ok=True)
            spill_path = results_dir / f"{tool_use_id}.txt"
            spill_path.write_text(content, encoding="utf-8")
            spill_note = f" Full output saved to {spill_path}."
        except OSError:
            spill_path = None
            spill_note = ""
    else:
        spill_path = None

    if use_backstop_wording:
        # OpenCode's own shape (deep review item 15): name the cap that
        # actually bound this result, and point at Read with offset/limit
        # on the saved file rather than the generic "smaller limit" hint.
        hint = (f" Use Read with offset/limit on {spill_path} to see the rest." if spill_path is not None
                else " Use Read with offset/limit on the saved file to see the rest.")
        pointer = (f"\n\n... [{omitted} characters omitted -- output exceeded the "
                   f"{MAX_LINES}-line/{MAX_BYTES // 1024}KB cap.{spill_note}{hint}]\n\n")
    else:
        pointer = f"\n\n... [{omitted} characters omitted -- result truncated at {cap} characters.{spill_note}]\n\n"
    return head + pointer + tail
