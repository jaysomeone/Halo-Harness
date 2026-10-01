from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from halo_harness.config.frontmatter import parse as parse_frontmatter
from halo_harness.config.paths import memory_dir


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


def render_memory_content(*, name: str, description: str, type: str, body: str,
                           origin_session_id: "str | None" = None,
                           provenance_comment: "str | None" = None) -> "tuple[str, str]":
    """The pure formatting half of `MemoryStore.write` -- extracted so
    `halo_harness.improve`'s diff-preview path (an ImproveCard showing what
    an UPDATE to an already-provenanced memory file would look like) can
    render the exact same bytes without touching the filesystem. Returns
    `(content, safe_description)` -- Claude Code's exact frontmatter shape,
    verified against rolo's own real `~/.claude/projects/*/memory/*.md`
    files."""
    import datetime

    now = datetime.datetime.now(datetime.timezone.utc)
    modified = now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"
    safe_description = description.replace('"', "'").replace("\n", " ").strip()
    lines = ["---", f"name: {name}", f'description: "{safe_description}"', "metadata:",
             "  node_type: memory", f"  type: {type}"]
    if origin_session_id:
        lines.append(f"  originSessionId: {origin_session_id}")
    lines.append(f"  modified: {modified}")
    lines.append("---")
    lines.append("")
    content = "\n".join(lines) + "\n" + body.strip("\n") + "\n"
    if provenance_comment:
        content = content.rstrip("\n") + "\n\n" + provenance_comment.strip() + "\n"
    return content, safe_description


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

        # Byte cap is 25,000 bytes, not a binary 25,600 (25KiB) [bin sec.12:
        # "MEMORY.md content exceeds the prompt-index cap" -- finding 13].
        current_text = "".join(lines)
        encoded_bytes = current_text.encode("utf-8")

        if len(encoded_bytes) > 25000:
            # Truncate further by removing lines from the end
            while lines and len("".join(lines).encode("utf-8")) > 25000:
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

                # Finding 11: the documented shape is a TOP-LEVEL `type:`/
                # `modified:` (rolo's real project_vids_1080_reencode.md is
                # like this); some files instead nest them under `metadata:`
                # -- read the top-level pair as the base and let a DICT
                # `metadata` override per-key if present, guarding against a
                # scalar `metadata:` value (which has no `.get()`).
                type_val = frontmatter_dict.get("type")
                modified = frontmatter_dict.get("modified")
                metadata = frontmatter_dict.get("metadata")
                if isinstance(metadata, dict):
                    if "type" in metadata:
                        type_val = metadata.get("type")
                    if "modified" in metadata:
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

    def write(self, *, filename: str, name: str, description: str, type: str, body: str,
              origin_session_id: "str | None" = None, provenance_comment: "str | None" = None) -> Path:
        """H10 Part B: the real implementation this stub was waiting on --
        `/improve`'s own memory candidates are the first caller. Writes a
        NEW topic file at `<memory_dir>/<filename>` with Claude Code's
        EXACT frontmatter shape (verified against rolo's own real
        `~/.claude/projects/*/memory/*.md` files):

            ---
            name: <name>
            description: "<description>"
            metadata:
              node_type: memory
              type: <type>
              originSessionId: <origin_session_id>   # only when given
              modified: <ISO-8601 UTC, millisecond precision, "Z">
            ---

            <body>

        then appends one `update_index` line. Raises `FileExistsError` if
        `filename` already exists -- this is a raw filesystem primitive;
        "only NEW files, never overwrite a user-authored one" is
        `halo_harness.improve.apply`'s policy, enforced before this is ever
        called."""
        if not filename.endswith(".md"):
            filename += ".md"
        path = self.memory_dir_path / filename
        if path.exists():
            raise FileExistsError(str(path))
        path.parent.mkdir(parents=True, exist_ok=True)
        content, safe_description = render_memory_content(
            name=name, description=description, type=type, body=body,
            origin_session_id=origin_session_id, provenance_comment=provenance_comment,
        )
        tmp = path.with_name(path.name + f".tmp{os.getpid()}")
        tmp.write_text(content, encoding="utf-8")
        os.replace(tmp, path)
        self.update_index(filename=filename, title=name, description=safe_description)
        return path

    def update_index(self, *, filename: str, title: str, description: str) -> Path:
        """Append one line to MEMORY.md: `- [title](filename) -- description`
        (the exact shape rolo's own real MEMORY.md index uses). Creates
        MEMORY.md if it doesn't exist yet. H10 Part B: an existing line
        that already links `(filename)` is REPLACED in place, never
        duplicated -- `/improve` re-applying an update to an
        already-provenanced memory file must not grow a second index line
        for the same topic file every time it's refreshed."""
        idx_path = self.memory_dir_path / "MEMORY.md"
        idx_path.parent.mkdir(parents=True, exist_ok=True)
        existing = idx_path.read_text(encoding="utf-8") if idx_path.exists() else ""
        line = f"- [{title}]({filename}) -- {description}"
        marker = f"]({filename})"
        lines = [ln for ln in existing.splitlines() if marker not in ln]
        lines.append(line)
        new_content = "\n".join(lines) + "\n"
        tmp = idx_path.with_name(idx_path.name + f".tmp{os.getpid()}")
        tmp.write_text(new_content, encoding="utf-8")
        os.replace(tmp, idx_path)
        return idx_path
