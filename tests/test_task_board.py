"""tests.test_task_board -- Halo 2.0.2 round 3 (brief C item 3):
`halo_harness.tools.task_board`'s file round trip (TaskCreate -> TaskList
-> TaskUpdate, read back from a FRESH read -- never the in-memory dict a
less careful implementation might have cached), `_top_session_dir`
collapsing a nested sub-agent's own `ctx.session_dir` down to the shared
top-level file every position in a tree writes through, and a genuine
concurrent claim race (two threads, standing in for "two fake agents"
sharing one session) resolving to exactly one winner with no lost update.
Hermetic: every path is a fresh `tempfile.mkdtemp()`, nothing touches the
real `~/.halo`.
"""
from __future__ import annotations

import json
import sys
import tempfile
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.helpers.runner import Ctx, new_registry, print_results, run_all

test, TESTS = new_registry()


def _ctx_at(session_dir: Path):
    from halo_harness.tools.base import ToolContext
    return ToolContext(cwd=session_dir, session_dir=session_dir)


@test
def test_top_session_dir_collapses_any_depth(ctx: Ctx):
    from halo_harness.tools.task_board import _top_session_dir
    top = Path(tempfile.mkdtemp(prefix="board-top-"))
    ctx.check("a bare top-level dir is unchanged", _top_session_dir(top) == top)
    depth1 = top / "subagents" / "agent-AAAA"
    depth2 = top / "subagents" / "agent-AAAA" / "subagents" / "agent-BBBB"
    ctx.check("depth-1 child collapses to top", _top_session_dir(depth1) == top)
    ctx.check("depth-2 grandchild also collapses to top", _top_session_dir(depth2) == top)


@test
def test_create_list_update_round_trip_from_a_fresh_read(ctx: Ctx):
    """TaskCreate's own `summary`/content never gets consulted by the
    round trip -- only a FRESH TaskList (and TaskUpdate's own re-read of
    the file) proves this actually persisted, not just an in-memory
    object the tool instance happened to keep around."""
    from halo_harness.tools.task_board import TaskCreateTool, TaskListTool, TaskUpdateTool
    top = Path(tempfile.mkdtemp(prefix="board-roundtrip-"))
    ctx_top = _ctx_at(top)

    created = TaskCreateTool().run({"title": "write the report", "notes": "due friday"}, ctx_top)
    ctx.check(f"create ok, got {created.content!r}", not created.is_error)
    task_id = created.content.split("'")[1]

    # A completely FRESH tool instance + fresh ToolContext -- nothing
    # shared in-process with the one that created it except the file.
    listed = TaskListTool().run({}, _ctx_at(top))
    ctx.check(f"the new task is listed, got {listed.content!r}", task_id in listed.content)
    ctx.check("status starts open", "(open)" in listed.content)

    updated = TaskUpdateTool().run({"id": task_id, "status": "claimed", "owner": "Worker-1"}, _ctx_at(top))
    ctx.check(f"claim ok, got {updated.content!r}", not updated.is_error)

    relisted = TaskListTool().run({}, _ctx_at(top))
    ctx.check("owner persisted", "owner=Worker-1" in relisted.content)
    ctx.check("status persisted as claimed", "(claimed)" in relisted.content)

    done = TaskUpdateTool().run({"id": task_id, "status": "done", "result": "report.md"}, _ctx_at(top))
    ctx.check(f"mark done ok, got {done.content!r}", not done.is_error)
    final = json.loads((top / "tasks.json").read_text(encoding="utf-8"))
    row = final["tasks"][0]
    ctx.check(f"on-disk row reflects every update, got {row}",
              row["status"] == "done" and row["owner"] == "Worker-1" and row["result"] == "report.md")


@test
def test_a_descendant_sub_agent_shares_the_same_board_as_the_top(ctx: Ctx):
    """An org worker's own `ctx.session_dir` is nested several levels
    under the top session's -- it must still see (and be able to claim)
    a task the root created, through the SAME file."""
    from halo_harness.tools.task_board import TaskCreateTool, TaskListTool, TaskUpdateTool
    top = Path(tempfile.mkdtemp(prefix="board-nested-"))
    nested = top / "subagents" / "agent-ROOT" / "subagents" / "agent-WORKER"

    created = TaskCreateTool().run({"title": "do the nested thing"}, _ctx_at(top))
    task_id = created.content.split("'")[1]

    listed_from_nested = TaskListTool().run({}, _ctx_at(nested))
    ctx.check("the nested worker sees the root's task", task_id in listed_from_nested.content)

    claimed = TaskUpdateTool().run({"id": task_id, "status": "claimed", "owner": "Worker"}, _ctx_at(nested))
    ctx.check(f"the nested worker can claim it, got {claimed.content!r}", not claimed.is_error)
    seen_from_top = TaskListTool().run({}, _ctx_at(top))
    ctx.check("the root sees the nested worker's claim", "owner=Worker" in seen_from_top.content)


@test
def test_concurrent_claims_from_two_fake_agents_resolve_to_one_winner(ctx: Ctx):
    from halo_harness.tools.task_board import TaskCreateTool, TaskUpdateTool
    top = Path(tempfile.mkdtemp(prefix="board-race-"))
    task_id = TaskCreateTool().run({"title": "contested task"}, _ctx_at(top)).content.split("'")[1]

    results: list = []
    results_lock = threading.Lock()

    def _claim(owner: str) -> None:
        r = TaskUpdateTool().run({"id": task_id, "status": "claimed", "owner": owner}, _ctx_at(top))
        with results_lock:
            results.append((owner, r.is_error, r.content))

    threads = [threading.Thread(target=_claim, args=(f"Agent-{i}",)) for i in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    ctx.check(f"both threads finished, got {len(results)}", len(results) == 2)
    wins = [r for r in results if not r[1]]
    losses = [r for r in results if r[1]]
    ctx.check(f"exactly one winner, got {results}", len(wins) == 1)
    ctx.check(f"exactly one clear-error loser, got {results}", len(losses) == 1 and "already" in losses[0][2])
    final = json.loads((top / "tasks.json").read_text(encoding="utf-8"))
    ctx.check(f"the on-disk owner matches the winner, got {final['tasks'][0]}",
              final["tasks"][0]["owner"] == wins[0][0])


@test
def test_claiming_an_already_claimed_task_is_a_clear_error(ctx: Ctx):
    from halo_harness.tools.task_board import TaskCreateTool, TaskUpdateTool
    top = Path(tempfile.mkdtemp(prefix="board-reclaim-"))
    task_id = TaskCreateTool().run({"title": "t"}, _ctx_at(top)).content.split("'")[1]
    first = TaskUpdateTool().run({"id": task_id, "status": "claimed", "owner": "A"}, _ctx_at(top))
    ctx.check("first claim ok", not first.is_error)
    second = TaskUpdateTool().run({"id": task_id, "status": "claimed", "owner": "B"}, _ctx_at(top))
    ctx.check(f"second claim refused, got {second.content!r}", second.is_error and "A" in second.content)


@test
def test_claim_without_owner_is_rejected(ctx: Ctx):
    from halo_harness.tools.task_board import TaskCreateTool, TaskUpdateTool
    top = Path(tempfile.mkdtemp(prefix="board-noowner-"))
    task_id = TaskCreateTool().run({"title": "t"}, _ctx_at(top)).content.split("'")[1]
    r = TaskUpdateTool().run({"id": task_id, "status": "claimed"}, _ctx_at(top))
    ctx.check(f"rejected with no owner, got {r.content!r}", r.is_error and "owner" in r.content)


@test
def test_unknown_task_id_and_bad_status_are_clear_errors(ctx: Ctx):
    from halo_harness.tools.task_board import TaskCreateTool, TaskUpdateTool
    top = Path(tempfile.mkdtemp(prefix="board-errs-"))
    unknown = TaskUpdateTool().run({"id": "nope", "status": "done"}, _ctx_at(top))
    ctx.check("unknown id is an error", unknown.is_error)
    task_id = TaskCreateTool().run({"title": "t"}, _ctx_at(top)).content.split("'")[1]
    bad_status = TaskUpdateTool().run({"id": task_id, "status": "finished"}, _ctx_at(top))
    ctx.check(f"invalid status is an error, got {bad_status.content!r}", bad_status.is_error)


if __name__ == "__main__":
    ctx = Ctx()
    results, passed, failed, skipped = run_all(TESTS, ctx)
    sys.exit(print_results(results, passed, failed, skipped))
