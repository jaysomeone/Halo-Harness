"""rolo_claude.improve -- H10 Part B: human-gated `/improve` (L1 memory/
rules + L3 skills). The model never edits its own instructions silently:
every written artifact is approved on an `ImproveCard` (TUI) or by an
explicit headless `improve --apply`; provenance is INFORMATION shown on the
card, never a block, filter or classifier; nothing drafts or writes
automatically in `-p`; nothing interrupts a running turn.

Modules:
  config.py    -- ImproveConfig, ~/.rolo-claude/config.json's "improve" key.
  evidence.py  -- failure clusters from rolo_claude.telemetry's scan.
  draft.py     -- the ONE drafting model call + candidate JSON parsing.
  memory.py    -- Candidate -> a real file on disk (memory/rule/skill).
  apply.py     -- atomic write, dismissed-hash store, improve_applied log
                  node, the fresh instructions/memory-index snapshot.
"""
