"""rolo_claude.tools.truncate -- result truncation to disk (H2 scope A): a
tool result over its tool's `result_cap` is spilled in full to
`<session_dir>/tool-results/<tool_use_id>.txt`; the model sees the head 60%
+ tail 30% of the cap, with the middle 10% replaced by a pointer line naming
the spill file and how many characters were omitted. Called from
agent/loop.py right after `ToolRegistry.dispatch` returns, never by a tool
itself (a tool doesn't know its own session directory).
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional


def spill_and_truncate(content: str, *, cap: Optional[int], session_dir: Optional[Path],
                        tool_use_id: Optional[str]) -> str:
    if cap is None or not isinstance(content, str) or len(content) <= cap:
        return content

    head_len = int(cap * 0.6)
    tail_len = int(cap * 0.3)
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
            spill_note = ""

    pointer = f"\n\n... [{omitted} characters omitted -- result truncated at {cap} characters.{spill_note}]\n\n"
    return head + pointer + tail
