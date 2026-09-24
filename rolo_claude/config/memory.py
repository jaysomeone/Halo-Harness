from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from rolo_claude.config.frontmatter import parse as parse_frontmatter
from rolo_claude.config.paths import memory_dir


@dataclass
class TopicFile:
    path: Path
    name: str
    description: str
    type: str | None
    modified: object | None


@dataclass
class MemoryIndex:
    text: str
    exists: bool
    topics: list[TopicFile]


class MemoryStore:
    def __init__(self, cwd, settings, home=None, bare: bool = False):
        self.cwd = cwd
        self.settings = settings
        self._enabled = (
            getattr(settings, "auto_memory_enabled", True)
            and os.environ.get("CLAUDE_CODE_DISABLE_AUTO_MEMORY") != "1"
            and not bare
        )

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def memory_dir_path(self) -> Path:
        return memory_dir(self.cwd, self.settings)

    def load_index(self) -> MemoryIndex:
        if not self.enabled:
            return MemoryIndex(text="", exists=False, topics=[])

        memory_file = self.memory_dir_path / "MEMORY.md"
        topics = self.entries()

        if not memory_file.exists():
            return MemoryIndex(text="", exists=False, topics=topics)

        try:
            with open(memory_file, "r", encoding="utf-8") as f:
                content = f.read()
        except (OSError, UnicodeDecodeError):
            return MemoryIndex(text="", exists=False, topics=topics)

        lines = content.splitlines(keepends=True)
        truncated_by_lines = False
        truncated_by_bytes = False

        # Apply 200-line cap first
        if len(lines) > 200:
            lines = lines[:200]
            truncated_by_lines = True

        # Check UTF-8 byte length (25KB = 25600 bytes)
        current_text = "".join(lines)
        encoded_bytes = current_text.encode("utf-8")

        if len(encoded_bytes) > 25600:
            # Truncate further by removing lines from the end
            while lines and len("".join(lines).encode("utf-8")) > 25600:
                lines.pop()
            truncated_by_bytes = True

        final_text = "".join(lines)
        final_line_count = len(lines)

        # Append truncation marker if either cap was applied
        if truncated_by_lines or truncated_by_bytes:
            final_text += f"\n[... MEMORY.md truncated at {final_line_count} lines / 25KB ...]"

        return MemoryIndex(text=final_text, exists=True, topics=topics)

    def entries(self) -> list[TopicFile]:
        if not self.memory_dir_path.exists():
            return []

        topic_files = []
        for path in self.memory_dir_path.iterdir():
            if path.is_file() and path.suffix.lower() == ".md" and path.name != "MEMORY.md":
                try:
                    with open(path, "r", encoding="utf-8") as f:
                        content = f.read()
                except (OSError, UnicodeDecodeError):
                    # Best-effort fallback
                    topic_files.append(
                        TopicFile(
                            path=path,
                            name=path.stem,
                            description="",
                            type=None,
                            modified=None,
                        )
                    )
                    continue

                frontmatter_dict, _ = parse_frontmatter(content)

                name = frontmatter_dict.get("name")
                if name is None:
                    name = path.stem

                description = frontmatter_dict.get("description", "")

                metadata = frontmatter_dict.get("metadata") or {}
                type_val = metadata.get("type")
                modified = metadata.get("modified")

                topic_files.append(
                    TopicFile(
                        path=path,
                        name=name,
                        description=description,
                        type=type_val,
                        modified=modified,
                    )
                )

        # Sort by filename for deterministic ordering
        topic_files.sort(key=lambda t: t.path.name)
        return topic_files

    def index_text(self) -> str:
        entries = self.entries()
        if not entries:
            return ""

        lines = []
        for topic in entries:
            # str(None) produces "None" as required
            lines.append(f"- {topic.name} ({topic.type}): {topic.description}")

        return "\n".join(lines)

    def write(self, *args, **kwargs):
        raise NotImplementedError(
            "MemoryStore.write/update_index: no tool "
            "exists yet to call this (H0 has no tools; a future milestone wires this "
            "up to a real Write tool)"
        )

    def update_index(self, *args, **kwargs):
        raise NotImplementedError(
            "MemoryStore.write/update_index: no tool "
            "exists yet to call this (H0 has no tools; a future milestone wires this "
            "up to a real Write tool)"
        )
