"""halo_harness.testing.fake_controller -- scripted Controller stand-in (U0
scope E), used by `--demo`/`--stress` (this module) and, per
`docs/harness/U2-brief.md`, U2's own `test_tui.py` pilots later. Implements
the same method surface D-Contract gives the real `halo_harness.controller`
(not built until U2): `submit`/`interrupt`/`set_permission_mode`/
`set_model`/`add_permission_rule`/`answer_permission`/`answer_question`/
`run_slash`/`list_models`/`list_sessions`/`resume`/`mcp_status`/
`reconnect_mcp`/`memory_path`/`quit`. Every call is also recorded so a test
can assert on what the caller actually did (`.model`, `.interrupts`,
`.quit_called`, ...).
"""

from __future__ import annotations

from itertools import chain
from typing import Iterator, Optional

from halo_harness import events as ev

DEFAULT_MODEL = "or:deepseek/deepseek-v4.1-flash"


def default_demo_turns() -> list:
    """One small, self-contained scripted transcript -- user message,
    streamed assistant text, a tool call + result, more text, turn_done --
    exercised by `--demo` and this module's own tests without ever touching
    a real model or network."""
    return [[
        ev.user_message("Show me a quick demo", turn=1),
        ev.status(phase="thinking", model=DEFAULT_MODEL, turn=1),
        ev.Event("message_start", {"model": DEFAULT_MODEL}, turn=1),
        ev.text_delta("Sure -- ", turn=1),
        ev.text_delta("let me check something first.\n", turn=1),
        ev.Event("tool_use_start", {"id": "toolu_demo1", "name": "Bash"}, turn=1),
        ev.Event("tool_use_ready", {"id": "toolu_demo1", "name": "Bash",
                                     "input": {"command": "echo demo"}, "repaired": False}, turn=1),
        ev.Event("tool_result", {"id": "toolu_demo1", "ok": True, "summary": "demo"}, turn=1),
        ev.message_end(turn=1, stop_reason="tool_use", usage={"input_tokens": 120, "output_tokens": 40}),
        ev.Event("message_start", {"model": DEFAULT_MODEL}, turn=1),
        ev.text_delta("All done -- that's a scripted demo turn.", turn=1),
        ev.message_end(turn=1, stop_reason="end_turn", usage={"input_tokens": 150, "output_tokens": 60}, cost_usd=0.0),
        ev.status(phase="idle", model=DEFAULT_MODEL, turn=1),
        ev.turn_done(turn=1, reason="end_turn"),
    ]]


def stress_turns(n: int) -> list:
    """The default transcript plus `n` extra minimal (plain-text) turns --
    `--demo --stress N`'s job: push a lot of events through a sink/TUI
    cheaply, without hand-writing each one."""
    turns = default_demo_turns()
    for i in range(max(0, n)):
        turn_no = i + 2
        turns.append([
            ev.user_message(f"stress turn {i}", turn=turn_no),
            ev.Event("message_start", {"model": DEFAULT_MODEL}, turn=turn_no),
            ev.text_delta(f"stress reply {i}", turn=turn_no),
            ev.message_end(turn=turn_no, stop_reason="end_turn", usage={"input_tokens": 10, "output_tokens": 5}),
            ev.turn_done(turn=turn_no, reason="end_turn"),
        ])
    return turns


class FakeController:
    """`turns`: a list of "turn scripts", each either a list of
    `events.Event` or a zero-arg callable returning one -- consumed one per
    `submit()` call, looping back to the start once exhausted (never raises
    StopIteration at the caller)."""

    def __init__(self, turns: Optional[list] = None, *, model: str = DEFAULT_MODEL,
                 permission_mode: str = "default", agent_tasks: Optional[list] = None,
                 task_board: Optional[list] = None, mcp_servers: Optional[list] = None):
        self.turns = turns if turns is not None else default_demo_turns()
        self._next_turn_idx = 0
        self.model = model
        self.permission_mode = permission_mode
        self.submitted: list = []
        self.interrupts = 0
        self.quit_called = False
        self.permission_replies: list = []
        # 1.0.1 fixpass finding 4: request ids `reevaluate_pending_
        # permission` below must report as STILL "ask" regardless of mode
        # (a test-only stand-in for "an explicit ask: rule still asks even
        # in auto mode" -- this fake has no real PermissionEngine/category
        # logic to derive that from itself).
        self.still_ask_request_ids: set = set()
        self.question_replies: list = []
        self.plan_replies: list = []
        self.added_rules: list = []
        self.slash_calls: list = []
        self.reconnects = 0
        # U5: sessions UX / !cmd / @file#L / git-shadow rewind recording state.
        self.title = ""
        self.renames: list = []
        self.forks = 0
        self.exports: list = []
        self.inline_shell_decisions: list = []
        self.inline_shell_runs: list = []
        self.ingested_mentions: list = []
        self.shadow_step_list: list = []
        self.rewind_applies: list = []
        # Halo 2.0.2 round 3 (brief C): `/tasks`/Ctrl+T's own panel --
        # a test sets these directly, or via the constructor, to render a
        # fake running/queued/finished agent and a fake task-board row
        # with no real Session/agent_runtime/sub-agent ever involved.
        self.agent_tasks: list = list(agent_tasks) if agent_tasks is not None else []
        self.task_board: list = list(task_board) if task_board is not None else []
        # Halo 2.0.2 round 4 (brief D): `/mcp`'s own repair actions -- a
        # test sets `mcp_servers` directly (or via the constructor) to
        # render fake rows with no real McpManager/subprocess involved;
        # every action is recorded the same way `reconnects` already is.
        self.mcp_servers: list = list(mcp_servers) if mcp_servers is not None else []
        self.approvals: list = []
        self.reconnect_all_calls: int = 0
        self.logins: list = []
        self.tests: list = []
        self.disables: list = []

    def submit(self, text: str, pasted=None, meta=None) -> Iterator[ev.Event]:
        self.submitted.append(text)
        if not self.turns:
            return iter(())
        script = self.turns[self._next_turn_idx % len(self.turns)]
        self._next_turn_idx += 1
        return iter(script() if callable(script) else script)

    def interrupt(self) -> None:
        self.interrupts += 1

    def set_permission_mode(self, mode: str) -> None:
        self.permission_mode = mode

    def set_model(self, model: str) -> None:
        self.model = model

    def add_permission_rule(self, rule: str, scope: str) -> None:
        self.added_rules.append((rule, scope))

    def answer_permission(self, request_id: str, decision) -> None:
        self.permission_replies.append((request_id, decision))

    def reevaluate_pending_permission(self, request_id: str) -> Optional[str]:
        """1.0.1 fixpass finding 4: test-fixture mirror of `Controller.
        reevaluate_pending_permission` -- mode-only (this fake has no real
        PermissionEngine/category logic): `auto`/`bypassPermissions` allow,
        `dontAsk` denies, every other mode still "asks" (returns None, the
        card stays pending), UNLESS `request_id` is in `still_ask_request_
        ids` (see that attribute's own docstring), which always stays
        "ask" regardless of mode. Recorded into `permission_replies`
        exactly like `answer_permission` above, so an existing assertion
        on that list keeps working unchanged for the auto-resolved case."""
        if request_id in self.still_ask_request_ids:
            return None
        if self.permission_mode in ("auto", "bypassPermissions"):
            decision = {"action": "allow", "reason": "", "rule": None, "message": ""}
            self.permission_replies.append((request_id, decision))
            return "allow"
        if self.permission_mode == "dontAsk":
            decision = {"action": "deny", "reason": "", "rule": None, "message": ""}
            self.permission_replies.append((request_id, decision))
            return "deny"
        return None

    def answer_question(self, request_id: str, answer) -> None:
        self.question_replies.append((request_id, answer))

    def answer_plan(self, approved: bool, *, feedback: str = "", mode_after=None) -> None:
        # additive, matches halo_harness.controller.Controller.answer_plan --
        # no existing test touches this (plan mode/`plan_review` didn't
        # exist before U2's PlanCard).
        self.plan_replies.append({"approved": approved, "feedback": feedback, "mode_after": mode_after})

    def run_slash(self, name: str, args: str = "") -> str:
        self.slash_calls.append((name, args))
        return f"(fake) ran /{name} {args}".rstrip()

    def list_models(self) -> list:
        return [self.model]

    def list_sessions(self) -> list:
        return []

    def resume(self, session_id: str) -> None:
        self.submitted.append(f"__resume__:{session_id}")

    def mcp_status(self) -> dict:
        return {"connected": 0, "total": 0}

    def reconnect_mcp(self, name: str, abort=None) -> list:
        self.reconnects += 1
        for entry in self.mcp_servers:
            if entry.get("name") == name:
                entry["state"] = "connected"
                entry["error"] = None
        return [f"{name}: connected (fake)"]

    def list_mcp_servers(self) -> list:
        return list(self.mcp_servers)

    def approve_mcp_server(self, name: str, abort=None) -> list:
        self.approvals.append(name)
        return self.reconnect_mcp(name, abort=abort)

    def reconnect_all_mcp(self, abort=None) -> list:
        self.reconnect_all_calls += 1
        lines = []
        for entry in self.mcp_servers:
            lines.extend(self.reconnect_mcp(entry.get("name"), abort=abort))
        return lines or ["No MCP servers configured."]

    def login_mcp_server(self, name: str, abort=None) -> list:
        self.logins.append(name)
        return [f"{name}: authorized (fake)"]

    def test_mcp_server(self, name: str, abort=None) -> list:
        self.tests.append(name)
        return [f"{name}: tools/list ok in 1ms -- 0 tool(s). (fake)"]

    def set_mcp_server_disabled(self, name: str, disabled: bool) -> list:
        self.disables.append((name, disabled))
        for entry in self.mcp_servers:
            if entry.get("name") == name:
                entry["state"] = "disabled" if disabled else "pending"
        return [f"{name}: {'disabled' if disabled else 'enabled'} (fake)"]

    def resolve_mcp_config(self, name: str):
        return None

    def list_agent_tasks(self) -> list:
        return list(self.agent_tasks)

    def read_task_board(self) -> list:
        return list(self.task_board)

    def memory_path(self):
        from halo_harness.config.paths import memory_dir
        return memory_dir(".")

    def quit(self) -> int:
        self.quit_called = True
        return 0

    # ---- U5: sessions UX / `!cmd` / `@file#L` / git-shadow rewind --------
    # Simple recording stubs -- real behaviour (the small-model title call,
    # the real Bash tool, the real git-shadow repo) lives only in the real
    # `halo_harness.controller.Controller`; these just make the SAME
    # `tui/slash.py`/`tui/app.py` code paths exercisable against a script,
    # same spirit as `answer_permission`/`add_permission_rule` above.

    def get_title(self) -> str:
        return self.title

    def rename_session(self, title: str) -> None:
        self.title = title
        self.renames.append(title)

    def maybe_autoname_title(self):
        if self.title:
            return None
        self.title = "Fake auto title"
        return self.title

    def fork_session(self) -> str:
        self.forks += 1
        return f"fake-fork-{self.forks}"

    def export_session(self, *, sanitize: bool = False, path=None) -> str:
        self.exports.append({"sanitize": sanitize, "path": path})
        return path or f"/fake/exports/{self.model}.md"

    def session_stats(self) -> dict:
        return {"turns": len(self.submitted), "total_cost_usd": 0.0, "per_model": {}, "tool_counts": {}}

    def decide_inline_shell(self, command: str):
        from halo_harness.permissions import Decision
        self.inline_shell_decisions.append(command)
        return Decision("allow", "fake: always allowed")

    def run_inline_shell(self, command: str):
        from halo_harness.tools.base import ToolResult
        self.inline_shell_runs.append(command)
        return f"fake_inline_{len(self.inline_shell_runs)}", ToolResult(content=f"(fake output of: {command})")

    def ingest_at_mentions(self, text: str) -> None:
        self.ingested_mentions.append(text)

    def shadow_steps(self) -> list:
        return list(self.shadow_step_list)

    def rewind_preview_undo(self):
        return self.shadow_step_list[-1] if self.shadow_step_list else None

    def rewind_preview_redo(self):
        return None

    def rewind_apply(self, step_id: str, *, verb: str = "rewind"):
        self.rewind_applies.append((step_id, verb))
        return {"step": {"id": step_id}, "files": []}


def run_demo(*, output_format: str = "text", stress: Optional[int] = None, stream=None) -> int:
    """`--demo`/`--stress` (U0 scope E): replay a scripted transcript
    through the SAME text/json print-mode sink a real turn uses -- no
    network, no permission engine, no tool dispatch. All scripted turns are
    flattened into ONE event stream fed to a single `PrintModeSink`, so a
    multi-turn demo prints exactly like a multi-call real session would.
    Returns the process exit code."""
    from halo_harness.output import PrintModeSink

    turns = stress_turns(stress) if stress else default_demo_turns()
    flat = list(chain.from_iterable(t() if callable(t) else t for t in turns))
    sink = PrintModeSink(output_format=output_format, session_id="demo-session", model=DEFAULT_MODEL, stream=stream)
    return sink.consume(iter(flat))
