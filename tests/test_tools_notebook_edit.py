"""tests.test_tools_notebook_edit -- H8 scope B: NotebookEditTool, Claude
Code's `.ipynb` cell editor (pure JSON, no nbformat/jupyter dependency).
"""
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.helpers.runner import Ctx, new_registry, print_results, run_all
from rolo_claude.tools.base import ToolContext
from rolo_claude.tools.notebook_edit import NotebookEditTool

test, TESTS = new_registry()


def _notebook(cells: list, *, nbformat_minor: int = 5) -> dict:
    return {"cells": cells, "metadata": {}, "nbformat": 4, "nbformat_minor": nbformat_minor}


def _code_cell(cell_id: str, source: str) -> dict:
    return {"cell_type": "code", "id": cell_id, "metadata": {}, "outputs": [], "execution_count": None,
            "source": source.splitlines(keepends=True)}


def _write(path: Path, notebook: dict) -> None:
    path.write_text(json.dumps(notebook, indent=1), encoding="utf-8")


def _read(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _ctx(cwd: Path) -> ToolContext:
    return ToolContext(cwd=cwd)


@test
def test_notebook_edit_is_registered_in_the_default_tool_registry(ctx: Ctx):
    from rolo_claude.tools.registry import ToolRegistry
    reg = ToolRegistry()
    tool = reg.get("NotebookEdit")
    ctx.check("NotebookEdit is a real registered tool", tool is not None and tool.name == "NotebookEdit")


@test
def test_replace_rejects_relative_path(ctx: Ctx):
    tool = NotebookEditTool()
    result = tool.run({"notebook_path": "relative.ipynb", "cell_id": "a", "new_source": "x = 1"}, _ctx(Path.cwd()))
    ctx.check(f"error about absolute path, got {result.content!r}",
              result.is_error and "absolute path" in result.content)


@test
def test_replace_missing_file_is_an_error(ctx: Ctx):
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "nope.ipynb"
        tool = NotebookEditTool()
        result = tool.run({"notebook_path": str(path), "cell_id": "a", "new_source": "x = 1"}, _ctx(Path(d)))
        ctx.check(f"error about missing file, got {result.content!r}",
                  result.is_error and "does not exist" in result.content)


@test
def test_replace_cell_updates_source_and_preserves_others(ctx: Ctx):
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "nb.ipynb"
        _write(path, _notebook([_code_cell("a", "x = 1"), _code_cell("b", "y = 2")]))
        tool = NotebookEditTool()
        result = tool.run({"notebook_path": str(path), "cell_id": "b", "new_source": "y = 99"}, _ctx(Path(d)))
        ctx.check(f"result names the cell, got {result.content!r}", not result.is_error and "b" in result.content)
        nb = _read(path)
        ctx.check(f"cell a untouched, got {nb['cells'][0]['source']}", nb["cells"][0]["source"] == ["x = 1"])
        ctx.check(f"cell b updated, got {nb['cells'][1]['source']}", nb["cells"][1]["source"] == ["y = 99"])
        ctx.check("still 2 cells", len(nb["cells"]) == 2)


@test
def test_replace_missing_cell_id_is_an_error(ctx: Ctx):
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "nb.ipynb"
        _write(path, _notebook([_code_cell("a", "x = 1")]))
        tool = NotebookEditTool()
        result = tool.run({"notebook_path": str(path), "new_source": "x = 2"}, _ctx(Path(d)))
        ctx.check(f"cell_id required for replace, got {result.content!r}",
                  result.is_error and "cell_id" in result.content)


@test
def test_replace_unknown_cell_id_is_an_error(ctx: Ctx):
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "nb.ipynb"
        _write(path, _notebook([_code_cell("a", "x = 1")]))
        tool = NotebookEditTool()
        result = tool.run({"notebook_path": str(path), "cell_id": "zzz", "new_source": "x = 2"}, _ctx(Path(d)))
        ctx.check(f"no such cell, got {result.content!r}", result.is_error and "zzz" in result.content)


@test
def test_replace_changing_cell_type_to_markdown_drops_outputs_and_execution_count(ctx: Ctx):
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "nb.ipynb"
        _write(path, _notebook([_code_cell("a", "x = 1")]))
        tool = NotebookEditTool()
        result = tool.run({"notebook_path": str(path), "cell_id": "a", "new_source": "# heading",
                            "cell_type": "markdown"}, _ctx(Path(d)))
        ctx.check("no error", not result.is_error)
        cell = _read(path)["cells"][0]
        ctx.check(f"cell_type is markdown, got {cell}", cell["cell_type"] == "markdown")
        ctx.check(f"outputs/execution_count dropped, got {cell}",
                  "outputs" not in cell and "execution_count" not in cell)


@test
def test_insert_requires_cell_type(ctx: Ctx):
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "nb.ipynb"
        _write(path, _notebook([_code_cell("a", "x = 1")]))
        tool = NotebookEditTool()
        result = tool.run({"notebook_path": str(path), "cell_id": "a", "new_source": "y = 2",
                            "edit_mode": "insert"}, _ctx(Path(d)))
        ctx.check(f"cell_type required for insert, got {result.content!r}",
                  result.is_error and "cell_type" in result.content)


@test
def test_insert_after_a_cell_id(ctx: Ctx):
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "nb.ipynb"
        _write(path, _notebook([_code_cell("a", "x = 1"), _code_cell("b", "z = 3")]))
        tool = NotebookEditTool()
        result = tool.run({"notebook_path": str(path), "cell_id": "a", "new_source": "y = 2",
                            "cell_type": "code", "edit_mode": "insert"}, _ctx(Path(d)))
        ctx.check("no error", not result.is_error)
        cells = _read(path)["cells"]
        ctx.check(f"3 cells now, got {len(cells)}", len(cells) == 3)
        ctx.check(f"new cell sits between a and b, got {[c['id'] for c in cells]}",
                  cells[0]["id"] == "a" and cells[1]["source"] == ["y = 2"] and cells[2]["id"] == "b")
        ctx.check(f"new cell has its own fresh id, got {cells[1]['id']!r}",
                  cells[1]["id"] not in ("a", "b") and cells[1]["id"])


@test
def test_insert_with_no_cell_id_goes_at_the_start(ctx: Ctx):
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "nb.ipynb"
        _write(path, _notebook([_code_cell("a", "x = 1")]))
        tool = NotebookEditTool()
        result = tool.run({"notebook_path": str(path), "new_source": "# first", "cell_type": "markdown",
                            "edit_mode": "insert"}, _ctx(Path(d)))
        ctx.check("no error", not result.is_error)
        cells = _read(path)["cells"]
        ctx.check(f"new cell first, got {cells}", cells[0]["source"] == ["# first"] and cells[1]["id"] == "a")


@test
def test_insert_into_a_brand_new_file_creates_it(ctx: Ctx):
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "sub" / "brand-new.ipynb"
        tool = NotebookEditTool()
        result = tool.run({"notebook_path": str(path), "new_source": "print('hi')", "cell_type": "code",
                            "edit_mode": "insert"}, _ctx(Path(d)))
        ctx.check(f"no error, got {result.content!r}", not result.is_error)
        ctx.check("file created (parent dir too)", path.is_file())
        nb = _read(path)
        ctx.check(f"nbformat 4, one cell, got {nb}", nb["nbformat"] == 4 and len(nb["cells"]) == 1)


@test
def test_delete_requires_cell_id(ctx: Ctx):
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "nb.ipynb"
        _write(path, _notebook([_code_cell("a", "x = 1")]))
        tool = NotebookEditTool()
        result = tool.run({"notebook_path": str(path), "new_source": "", "edit_mode": "delete"}, _ctx(Path(d)))
        ctx.check(f"cell_id required for delete, got {result.content!r}",
                  result.is_error and "cell_id" in result.content)


@test
def test_delete_removes_exactly_that_cell(ctx: Ctx):
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "nb.ipynb"
        _write(path, _notebook([_code_cell("a", "x = 1"), _code_cell("b", "y = 2"), _code_cell("c", "z = 3")]))
        tool = NotebookEditTool()
        result = tool.run({"notebook_path": str(path), "cell_id": "b", "new_source": "",
                            "edit_mode": "delete"}, _ctx(Path(d)))
        ctx.check("no error", not result.is_error)
        cells = _read(path)["cells"]
        ctx.check(f"2 cells left, b gone, got {[c['id'] for c in cells]}",
                  [c["id"] for c in cells] == ["a", "c"])


@test
def test_invalid_edit_mode_is_an_error(ctx: Ctx):
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "nb.ipynb"
        _write(path, _notebook([_code_cell("a", "x = 1")]))
        tool = NotebookEditTool()
        result = tool.run({"notebook_path": str(path), "cell_id": "a", "new_source": "x",
                            "edit_mode": "bogus"}, _ctx(Path(d)))
        ctx.check(f"invalid edit_mode rejected, got {result.content!r}",
                  result.is_error and "edit_mode" in result.content)


@test
def test_invalid_cell_type_is_an_error(ctx: Ctx):
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "nb.ipynb"
        _write(path, _notebook([_code_cell("a", "x = 1")]))
        tool = NotebookEditTool()
        result = tool.run({"notebook_path": str(path), "cell_id": "a", "new_source": "x",
                            "cell_type": "prose"}, _ctx(Path(d)))
        ctx.check(f"invalid cell_type rejected, got {result.content!r}",
                  result.is_error and "cell_type" in result.content)


@test
def test_not_a_notebook_file_is_a_clear_error(ctx: Ctx):
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "not-a-notebook.ipynb"
        path.write_text(json.dumps({"hello": "world"}), encoding="utf-8")
        tool = NotebookEditTool()
        result = tool.run({"notebook_path": str(path), "cell_id": "a", "new_source": "x"}, _ctx(Path(d)))
        ctx.check(f"clear error, got {result.content!r}",
                  result.is_error and "does not look like" in result.content)


@test
def test_pre_4_5_notebook_without_cell_ids_gets_ids_assigned_on_touch(ctx: Ctx):
    """Older notebooks (nbformat < 4.5) have no per-cell `id` -- this tool
    must assign one on first touch, the same way a real Jupyter client
    would, so `cell_id` addressing works from then on."""
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "old.ipynb"
        old_cell = {"cell_type": "code", "metadata": {}, "outputs": [], "execution_count": None,
                    "source": ["x = 1"]}  # no "id" key at all
        _write(path, {"cells": [old_cell], "metadata": {}, "nbformat": 4, "nbformat_minor": 2})
        tool = NotebookEditTool()
        # insert at the start (cell_id omitted) still touches/rewrites every cell's id-assignment pass
        result = tool.run({"notebook_path": str(path), "new_source": "y = 2", "cell_type": "code",
                            "edit_mode": "insert"}, _ctx(Path(d)))
        ctx.check("no error", not result.is_error)
        nb = _read(path)
        ctx.check(f"the old cell now has a real id, got {nb['cells'][1]}", bool(nb["cells"][1].get("id")))
        ctx.check(f"nbformat_minor bumped to at least 5, got {nb['nbformat_minor']}", nb["nbformat_minor"] >= 5)


@test
def test_h9b_f15_pre_4_5_notebook_replace_and_delete_via_cell_dash_n_index(ctx: Ctx):
    """H9 whole-tree review finding 15 (finding 34's own critique: the
    existing pre-4.5 test above only ever INSERTS). [bin] Claude Code
    2.1.282 renders an id-less cell's DISPLAY id as `e.id ?? cell-${n}`
    (zero-indexed) and parses that exact `cell-N` shape back as a
    positional index -- verified bug: on a 4.4 notebook with 2 cells and
    NO ids at all, `cell-0`/`cell-1` (and bare `0`/`1`) ALL returned "No
    cell with id" -- REPLACE and DELETE could never address a single cell,
    only insert (which needs no cell_id at all when omitted)."""
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "old.ipynb"
        cell0 = {"cell_type": "code", "metadata": {}, "outputs": [{"output_type": "stream", "text": "2\n"}],
                 "execution_count": 1, "source": ["print(1+1)"]}  # no "id" -- pre-4.5
        cell1 = {"cell_type": "code", "metadata": {}, "outputs": [], "execution_count": None,
                 "source": ["print(2+2)"]}
        _write(path, {"cells": [cell0, cell1], "metadata": {}, "nbformat": 4, "nbformat_minor": 2})
        tool = NotebookEditTool()

        result = tool.run({"notebook_path": str(path), "cell_id": "cell-0", "new_source": "print(3+3)"},
                           _ctx(Path(d)))
        ctx.check(f"replace via cell-0 (a positional index) succeeds, got {result.content!r}",
                  not result.is_error)
        nb = _read(path)
        ctx.check(f"cell 0's source is really updated, got {nb['cells'][0]['source']}",
                  "".join(nb["cells"][0]["source"]) == "print(3+3)")
        # finding 15's OTHER half: a code-cell replace clears the stale
        # outputs/execution_count from the PREVIOUS source.
        ctx.check(f"the stale stream output is gone, got {nb['cells'][0]['outputs']}",
                  nb["cells"][0]["outputs"] == [])
        ctx.check(f"the stale execution_count is cleared, got {nb['cells'][0]['execution_count']}",
                  nb["cells"][0]["execution_count"] is None)
        # ids were minted on THIS call and (since it succeeded) persisted.
        ctx.check("cell 0 now has a real id (persisted, this call succeeded)", bool(nb["cells"][0].get("id")))

        result2 = tool.run({"notebook_path": str(path), "cell_id": "cell-1", "edit_mode": "delete",
                             "new_source": ""}, _ctx(Path(d)))
        ctx.check(f"delete via cell-1 succeeds, got {result2.content!r}", not result2.is_error)
        nb2 = _read(path)
        ctx.check(f"only 1 cell remains, got {len(nb2['cells'])}", len(nb2["cells"]) == 1)
        ctx.check("the surviving cell is the one that was cell-0 (now really print(3+3))",
                  "".join(nb2["cells"][0]["source"]) == "print(3+3)")


@test
def test_h9b_f15_cell_dash_n_out_of_range_is_still_a_clear_error(ctx: Ctx):
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "old.ipynb"
        cell0 = {"cell_type": "code", "metadata": {}, "outputs": [], "execution_count": None, "source": ["x = 1"]}
        _write(path, {"cells": [cell0], "metadata": {}, "nbformat": 4, "nbformat_minor": 2})
        tool = NotebookEditTool()
        result = tool.run({"notebook_path": str(path), "cell_id": "cell-5", "new_source": "y = 2"}, _ctx(Path(d)))
        ctx.check(f"out-of-range cell-N is a clear error, not a crash, got {result.content!r}",
                  result.is_error and "No cell with id" in result.content)


@test
def test_h9b_f15_replace_on_an_already_code_cell_with_no_type_change_still_clears_outputs(ctx: Ctx):
    """The bug specifically: outputs/execution_count were only ever reset
    inside the `cell_type changed` branch -- an ordinary source-only
    replace (the overwhelmingly common case) left them stale."""
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "nb.ipynb"
        cell = {"cell_type": "code", "id": "a", "metadata": {},
                "outputs": [{"output_type": "execute_result", "data": {"text/plain": ["2"]}}],
                "execution_count": 7, "source": ["1 + 1"]}
        _write(path, _notebook([cell]))
        tool = NotebookEditTool()
        result = tool.run({"notebook_path": str(path), "cell_id": "a", "new_source": "3 + 3"}, _ctx(Path(d)))
        ctx.check(f"no error, got {result.content!r}", not result.is_error)
        nb = _read(path)
        ctx.check(f"stale outputs cleared, got {nb['cells'][0]['outputs']}", nb["cells"][0]["outputs"] == [])
        ctx.check(f"stale execution_count cleared, got {nb['cells'][0]['execution_count']}",
                  nb["cells"][0]["execution_count"] is None)


@test
def test_source_as_a_single_string_is_read_fine_and_rewritten_as_a_list(ctx: Ctx):
    """Both `source` shapes (one string, or a list of line strings) are
    valid per the nbformat spec -- this tool must READ either, even though
    it always WRITES the list form."""
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "nb.ipynb"
        cell = {"cell_type": "code", "id": "a", "metadata": {}, "outputs": [], "execution_count": None,
                "source": "x = 1\ny = 2\n"}  # single-string form
        _write(path, {"cells": [cell], "metadata": {}, "nbformat": 4, "nbformat_minor": 5})
        tool = NotebookEditTool()
        result = tool.run({"notebook_path": str(path), "cell_id": "a", "new_source": "z = 3"}, _ctx(Path(d)))
        ctx.check(f"no error reading the single-string form, got {result.content!r}", not result.is_error)
        nb = _read(path)
        ctx.check(f"rewritten as a list, got {nb['cells'][0]['source']}", nb["cells"][0]["source"] == ["z = 3"])


if __name__ == "__main__":
    ctx = Ctx()
    results, passed, failed, skipped = run_all(TESTS, ctx)
    sys.exit(print_results(results, passed, failed, skipped))
