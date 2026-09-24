"""rolo_claude.agent.assemble -- wires config discovery (CLAUDE.md, memory,
settings) together into what agent/prompt.py and agent/loop.py need. Kept
separate from prompt.py so prompt.py stays pure-string-building and this
module owns all the filesystem/config reads.
"""

from __future__ import annotations

import datetime
import os
import platform
from pathlib import Path
from typing import Optional

from rolo_claude.agent.prompt import EnvironmentInfo, build_system_prompt, collect_git_info
from rolo_claude.config.claude_md import discover_instructions
from rolo_claude.config.claude_json import is_trusted, load_claude_json
from rolo_claude.config.memory import MemoryStore
from rolo_claude.config.settings import resolve_settings


class SessionContext:
    """Everything agent/loop.py's Session needs that comes from the config
    layer, resolved once at session start (byte-stable inputs to the system
    prompt for the whole session's lifetime)."""

    def __init__(self, *, cwd: Path, model_label: str, settings_flag: Optional[str] = None,
                 setting_sources: Optional[list] = None, append_system_prompt: Optional[str] = None,
                 bare: bool = False):
        self.cwd = Path(cwd)
        self.settings = resolve_settings(self.cwd, settings_flag=settings_flag, setting_sources=setting_sources)

        claude_json = load_claude_json()
        self.trusted = is_trusted(self.cwd, claude_json, bridge_trust=None)

        self.instructions = discover_instructions(self.cwd, self.settings, self.trusted, bare=bare)

        self.memory_store = MemoryStore(self.cwd, self.settings, bare=bare)
        self.memory_index = self.memory_store.load_index()

        git_info = collect_git_info(self.cwd)
        env = EnvironmentInfo(
            cwd=str(self.cwd),
            os_name=f"{platform.system()} {platform.release()}",
            date_str=datetime.date.today().isoformat(),
            model_label=model_label,
            **git_info,
        )
        self.system_prompt = build_system_prompt(
            env=env,
            memory_text=self.memory_index.text,
            memory_index_text=self.memory_store.index_text() if self.memory_index.topics else "",
            append_system_prompt=append_system_prompt,
        )

    def claude_md_text(self) -> str:
        """The rendered CLAUDE.md/AGENTS.md chain, or "" if none was found --
        delivered as a separate leading user message (see
        build_initial_user_message below), matching Claude Code's own
        documented delivery mechanic (finding C: "delivered as a user
        message after the system prompt")."""
        return self.instructions.render()


def build_initial_user_message(claude_md_text: str, first_user_text: str, images: Optional[list] = None) -> dict:
    """The session's first user-role message: the CLAUDE.md chain (if any)
    followed by the user's actual first prompt, as separate text blocks in
    ONE Anthropic user message. Only ever used for turn 1 -- later turns
    just send the user's text directly (see agent/loop.py)."""
    blocks = []
    if claude_md_text:
        blocks.append({"type": "text", "text": claude_md_text})
    blocks.append({"type": "text", "text": first_user_text})
    for img in (images or []):
        blocks.append(img)
    return {"role": "user", "content": blocks}
