"""rolo_claude.tools.notebook_edit -- the NotebookEdit tool (H8 scope B):
Claude Code's own `.ipynb` cell editor. A notebook is plain JSON (nbformat
4) -- no nbformat/jupyter dependency needed, just `json` plus the cell shape
itself (`source` as either one string or a list of line strings; both are
valid per the spec -- this tool always WRITES the list form, the more
common convention real notebook-authoring tools use, and reads either).
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

from rolo_claude.tools.base import Tool, ToolContext, ToolResult

DESCRIPTION = (
    "Replace, insert, or delete a cell in a Jupyter notebook (.ipynb file).\n\n"
    "Usage notes:\n"
    "- The notebook_path parameter must be an absolute path, not a relative path\n"
    "- The cell_id parameter is the ID of the cell to edit -- for edit_mode=insert, the new cell is "
    "inserted AFTER the cell with this ID, or at the start of the notebook when cell_id is left out; "
    "for replace/delete, cell_id is required and must name an existing cell\n"
    "- cell_type (code or markdown) is required for edit_mode=insert; for replace it defaults to the "
    "cell's current type\n"
    "- edit_mode defaults to replace"
)

_EDIT_MODES = ("replace", "insert", "delete")


def _split_source(text: str) -> list:
    return text.splitlines(keepends=True) if text else []


def _new_cell(cell_type: str, source: str) -> dict:
    cell = {"cell_type": cell_type, "id": uuid.uuid4().hex[:8], "metadata": {}, "source": _split_source(source)}
    if cell_type == "code":
        cell["outputs"] = []
        cell["execution_count"] = None
    return cell


class NotebookEditTool(Tool):
    name = "NotebookEdit"
    description = DESCRIPTION
    is_destructive = True
    input_schema = {
        "type": "object",
        "properties": {
            "notebook_path": {"type": "string",
                               "description": "The absolute path to the Jupyter notebook file to edit"},
            "cell_id": {"type": "string",
                        "description": "The ID of the cell to edit. For insert, the new cell is added AFTER "
                                       "this cell, or at the beginning if omitted"},
            "new_source": {"type": "string", "description": "The new source for the cell"},
            "cell_type": {"type": "string", "enum": ["code", "markdown"],
                          "description": "The type of the cell. Required for edit_mode=insert; defaults to "
                                         "the existing cell's type for replace"},
            "edit_mode": {"type": "string", "enum": list(_EDIT_MODES),
                          "description": "replace, insert, or delete (default replace)"},
        },
        "required": ["notebook_path", "new_source"],
    }

    def summary(self, input: dict) -> str:
        input = input if isinstance(input, dict) else {}
        return f"NotebookEdit({input.get('notebook_path', '')}, {input.get('edit_mode') or 'replace'})"

    def permission_content(self, input: dict) -> str:
        return input.get("notebook_path", "") if isinstance(input, dict) else ""

    def run(self, input: dict, ctx: ToolContext) -> ToolResult:
        input = input if isinstance(input, dict) else {}
        notebook_path = input.get("notebook_path")
        if not notebook_path or not isinstance(notebook_path, str):
            return ToolResult("The notebook_path parameter must be an absolute path, not a relative path",
                               is_error=True)
        path = Path(notebook_path)
        if not path.is_absolute():
            return ToolResult(
                f"The notebook_path parameter must be an absolute path, not a relative path: {notebook_path!r}",
                is_error=True,
            )

        edit_mode = input.get("edit_mode") or "replace"
        if edit_mode not in _EDIT_MODES:
            return ToolResult(f"Invalid edit_mode: {edit_mode!r} (expected one of {_EDIT_MODES})", is_error=True)

        new_source = input.get("new_source")
        if edit_mode != "delete" and not isinstance(new_source, str):
            return ToolResult("The new_source parameter is required for edit_mode=replace/insert", is_error=True)

        cell_type = input.get("cell_type")
        if cell_type is not None and cell_type not in ("code", "markdown"):
            return ToolResult(f"Invalid cell_type: {cell_type!r} (expected code or markdown)", is_error=True)
        if edit_mode == "insert" and cell_type is None:
            return ToolResult("The cell_type parameter is required when edit_mode is insert", is_error=True)

        cell_id = input.get("cell_id") or None

        if not path.exists():
            if edit_mode != "insert":
                return ToolResult(f"File does not exist: {notebook_path}", is_error=True)
            notebook = {"cells": [], "metadata": {}, "nbformat": 4, "nbformat_minor": 5}
        else:
            try:
                notebook = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as e:
                return ToolResult(f"Error reading notebook: {e}", is_error=True)
            if not isinstance(notebook, dict) or not isinstance(notebook.get("cells"), list):
                return ToolResult(f"{notebook_path} does not look like a valid Jupyter notebook (no cells array)",
                                   is_error=True)

        cells = notebook["cells"]
        # Older notebooks (nbformat < 4.5) have no per-cell `id` -- assign
        # one now so cell_id addressing always works, same as a real
        # Jupyter client does the first time it touches such a file.
        assigned_ids = False
        for cell in cells:
            if not isinstance(cell, dict):
                continue
            if not cell.get("id"):
                cell["id"] = uuid.uuid4().hex[:8]
                assigned_ids = True
        if assigned_ids:
            notebook["nbformat_minor"] = max(notebook.get("nbformat_minor") or 0, 5)

        index = None
        if cell_id:
            for i, cell in enumerate(cells):
                if cell.get("id") == cell_id:
                    index = i
                    break
            if index is None:
                return ToolResult(f"No cell with id {cell_id!r} in {notebook_path}", is_error=True)
        if edit_mode in ("replace", "delete") and index is None:
            return ToolResult(f"The cell_id parameter is required for edit_mode={edit_mode}", is_error=True)

        if edit_mode == "replace":
            cell = cells[index]
            cell["source"] = _split_source(new_source)
            if cell_type is not None and cell_type != cell.get("cell_type"):
                cell["cell_type"] = cell_type
                if cell_type == "code":
                    cell.setdefault("outputs", [])
                    cell.setdefault("execution_count", None)
                else:
                    cell.pop("outputs", None)
                    cell.pop("execution_count", None)
            result_note = f"Updated cell {cell_id}"
        elif edit_mode == "insert":
            new_cell = _new_cell(cell_type, new_source)
            insert_at = 0 if index is None else index + 1
            cells.insert(insert_at, new_cell)
            result_note = f"Inserted a new {cell_type} cell (id {new_cell['id']}) at position {insert_at}"
        else:  # delete
            del cells[index]
            result_note = f"Deleted cell {cell_id}"

        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(notebook, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
        except OSError as e:
            return ToolResult(f"Error writing notebook: {e}", is_error=True)

        if isinstance(getattr(ctx, "read_cache", None), dict):
            try:
                ctx.read_cache[str(path)] = path.stat().st_mtime
            except OSError:
                pass
        return ToolResult(f"{result_note} in {notebook_path} ({len(cells)} cell(s) total).")
