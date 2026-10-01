"""rolo_claude.agent.assemble -- wires config discovery (CLAUDE.md, memory,
settings, trust) together into what agent/prompt.py and agent/loop.py need
(H1 rewrite: findings 5/6 wire real trust into settings/instructions;
memory/CLAUDE.md/environment move OUT of the system prompt into user-role
snapshots per research revision 3).
"""

from __future__ import annotations

import datetime
import platform
from pathlib import Path
from typing import Optional

from rolo_claude.agent.prompt import EnvironmentInfo, build_environment_block, build_system_prompt, collect_git_info
from rolo_claude.config.claude_md import discover_instructions
from rolo_claude.config.claude_json import is_trusted, load_claude_json
from rolo_claude.config.memory import MemoryStore
from rolo_claude.config.settings import resolve_settings
from rolo_claude.tools.registry import ToolRegistry


class SessionContext:
    """Everything agent/loop.py's Session needs that comes from the config
    layer, resolved once at session start (byte-stable inputs to the system
    prompt for the whole session's lifetime). `system_prompt` never
    contains CLAUDE.md/memory/environment content (rev. 3) -- callers fetch
    those separately (`claude_md_text`, `memory_snapshot_text`,
    `environment_snapshot_text`) and log them as `snapshot` nodes."""

    def __init__(self, *, cwd: Path, model_label: str, model_family: str = "generic",
                 settings_flag: Optional[str] = None, setting_sources: Optional[list] = None,
                 append_system_prompt: Optional[str] = None, bare: bool = False,
                 tool_registry: Optional[ToolRegistry] = None, mcp_servers: Optional[list] = None):
        self.cwd = Path(cwd)
        # H4: `--bare` disables hooks too (D-CFG: "disableAllHooks/--bare
        # disable") -- agent/loop.py's Session reads this straight off the
        # SessionContext it was built from, same as it already does for
        # `.settings`, rather than plumbing a second `bare=` kwarg through
        # every Session constructor call site.
        self.bare = bare

        # finding 5/6: real trust, threaded into settings resolution so an
        # untrusted project/local layer's allow/env/hooks/autoMemoryDirectory
        # never reach anything downstream.
        claude_json = load_claude_json()
        self.claude_json = claude_json  # H2 scope C: headless.py reuses this for projects[cwd].allowedTools
        self.trusted = is_trusted(self.cwd, claude_json)
        self.settings = resolve_settings(
            self.cwd, settings_flag=settings_flag, setting_sources=setting_sources, trusted=self.trusted,
        )

        self.instructions = discover_instructions(self.cwd, self.settings, self.trusted, bare=bare)
        self.memory_store = MemoryStore(self.cwd, self.settings, bare=bare)
        self.memory_index = self.memory_store.load_index()
        self.tool_registry = tool_registry or ToolRegistry()

        self.system_prompt = build_system_prompt(
            model_label=model_label, cwd=self.cwd, tool_definitions=self.tool_registry.definitions(),
            family=model_family, append_system_prompt=append_system_prompt, mcp_servers=mcp_servers,
        )

    def claude_md_text(self) -> str:
        """The rendered CLAUDE.md/AGENTS.md/rules chain, or "" if none --
        delivered as a snapshot node (agent/loop.py), matching Claude
        Code's own documented delivery mechanic (a user message after the
        system prompt)."""
        return self.instructions.render()

    def refresh_instructions(self) -> None:
        """H10 Part B: re-runs `discover_instructions` from DISK -- unlike
        compaction's own re-injection (which deliberately REPLAYS this
        session's byte-stable, session-start-cached `self.instructions`),
        `/improve`'s `a`/`e` apply just wrote a NEW `.claude/rules/*.md`
        file and needs the NEXT snapshot to actually see it."""
        self.instructions = discover_instructions(self.cwd, self.settings, self.trusted, bare=self.bare)

    def refresh_memory(self) -> None:
        """The memory-index counterpart to `refresh_instructions` -- a
        fresh `MemoryStore.load_index()` picks up a memory candidate
        `/improve` just wrote to `<memory_dir>/<name>.md`."""
        self.memory_index = self.memory_store.load_index()

    def memory_snapshot_text(self) -> str:
        """MEMORY.md content (already capped by MemoryStore) plus the
        topic-file index, as one snapshot block; "" if auto-memory found
        nothing (or is disabled). finding 14: the memory directory's real
        PATH is included here (not asserted as a writable-by-you claim in
        the system prompt -- prompt.py's own capability sentence is
        registry-driven and says so only when a Write tool actually
        exists) so a model with a Write tool, or the user reading this
        snapshot, knows exactly where it is."""
        parts = []
        if self.memory_index.text:
            parts.append("# Memory (auto-loaded from MEMORY.md)\n\n" + self.memory_index.text)
        if self.memory_index.topics:
            parts.append("## Topic files (read on demand -- not included in full here)\n\n" + self.memory_store.index_text())
        if parts:
            parts.insert(0, f"Memory directory: {self.memory_store.memory_dir_path}")
        return "\n\n".join(parts)

    def environment_snapshot_text(self, model_label: str) -> str:
        """cwd/OS/date/model/git branch+status+log, as one snapshot block
        -- the dynamic counterpart of what H0 used to bake into the system
        prompt itself."""
        git_info = collect_git_info(self.cwd)
        env = EnvironmentInfo(
            cwd=str(self.cwd), os_name=f"{platform.system()} {platform.release()}",
            date_str=datetime.date.today().isoformat(), model_label=model_label, **git_info,
        )
        return build_environment_block(env)


def build_initial_user_message(claude_md_text: str, first_user_text: str, images: Optional[list] = None) -> dict:
    """Kept for backward-compat call sites; agent/loop.py's new Session
    logs CLAUDE.md as its OWN snapshot node instead of prepending it to the
    first user message, but this helper still builds the equivalent single
    Anthropic user message for any caller that wants that shape directly."""
    blocks = []
    if claude_md_text:
        blocks.append({"type": "text", "text": claude_md_text})
    blocks.append({"type": "text", "text": first_user_text})
    for img in (images or []):
        blocks.append(img)
    return {"role": "user", "content": blocks}
