"""halo_harness.controller -- the non-blocking UI-thread facade over the
agent loop (U2, D-Contract).

The rule this module exists to enforce: **every call here is made from the
UI thread and none of them may block it for more than a moment.** The agent
loop (`agent/loop.Session.run`) owns one worker thread; this class is the
only thing that talks to it.

Two directions, deliberately different mechanisms:

* UI -> loop. `submit`/`set_permission_mode`/`set_model` push a
  `events.Command` onto `self.commands` and return immediately -- the worker
  picks it up between turns. The three exceptions are `interrupt`,
  `answer_permission` and `answer_question`: those must affect a turn that is
  ALREADY in flight (the worker is parked inside that turn's generator, so it
  can never read the queue), and each has a thread-safe direct entry point on
  the Session (`abort.set()`, `resolve_permission`, `resolve_question`).

* loop -> UI. The worker pushes `events.Event`s onto `self.events` (a
  `queue.Queue`). Nothing here ever touches a widget; the TUI's 30 Hz drain
  timer is the only reader.

`quit()` is the one deliberately-blocking call: it asks the worker to stop
and joins it with a 5 s deadline (D-TUI), so a wedged turn can't stop the
process from exiting cleanly.
"""

from __future__ import annotations

import json
import queue
import threading
import uuid
from pathlib import Path
from typing import Callable, Optional

from halo_harness import events
from halo_harness.config.paths import bridge_home, project_slug
from halo_harness.permissions import Decision, add_allow_rule

QUIT_DEADLINE_S = 5.0


def _default_model_resolver(raw: str, *, state_dir: Path, routes: dict, settings=None):
    """`(ModelRef, ModelProfile, creds)` for a `--model`-style string --
    the same resolution headless.py does at startup, reused here so `/model`
    and `set_model` can never drift from it."""
    from halo_harness.headless import _resolve_creds
    from halo_harness.model import parse_model_ref, resolve_model_profile

    ref = parse_model_ref(raw, routes)
    profile = resolve_model_profile(ref, state_dir, routes)
    return ref, profile, _resolve_creds(ref, settings)


class Controller:
    """`session` is a fully built `agent.loop.Session`; `registry`/`facade`
    are the U0 slash-command surfaces (the facade is the TUI's own, so its
    `run()` implementations see live UI state)."""

    def __init__(
        self, *, session, cwd: Path, state_dir: Optional[Path] = None, routes: Optional[dict] = None,
        registry=None, facade=None, model_resolver: Optional[Callable] = None,
        mcp_status_fn: Optional[Callable] = None, reconnect_fn: Optional[Callable] = None,
        settings=None, mcp_manager: Optional[object] = None,
    ):
        self.session = session
        self.cwd = Path(cwd)
        self.state_dir = Path(state_dir) if state_dir else bridge_home()
        self.routes = routes or {}
        self.registry = registry
        self.facade = facade
        self.settings = settings
        self.model_resolver = model_resolver or (
            lambda raw: _default_model_resolver(raw, state_dir=self.state_dir, routes=self.routes,
                                                settings=self.settings))
        self._mcp_status_fn = mcp_status_fn
        self._reconnect_fn = reconnect_fn
        # tui/bootstrap.py hands this over so quit() can close every MCP
        # subprocess/loop cleanly (mirrors headless.py's own `finally:
        # mcp_manager.close_all()`); None for a print-mode-less bare
        # Session, a unit test, or `--bare`.
        self.mcp_manager = mcp_manager

        self.events: "queue.Queue" = queue.Queue()
        self.commands: "queue.Queue" = queue.Queue()
        self._worker: Optional[threading.Thread] = None
        self._stopped = threading.Event()
        self.quit_called = False
        self.exit_code = 0
        # set by the TUI right before quitting when `tui != "fullscreen"`:
        # the transcript is then printed to normal scrollback on exit.
        self.replay_messages: list = []
        # U5 scope A: `!cmd` inline shell's own persistent `cd` state --
        # deliberately separate from the real session's tool-dispatch
        # `bash_state` (agent/loop.py owns that one): an inline command
        # runs OUTSIDE the model loop entirely, so it gets its own small,
        # independent "current directory" memory across `!cmd` calls.
        self._inline_bash_state: dict = {}

    # ---- worker lifecycle -------------------------------------------------

    def start(self) -> "Controller":
        """Start the Session's command-pump thread. Returns self so a caller
        can write `controller = Controller(...).start()`."""
        if self._worker is not None:
            return self
        self.session.interactive = True  # the loop-side ask/question waits are only for a real UI

        def _target():
            try:
                self.exit_code = self.session.run(self.commands, self.events.put,
                                                  mcp_status_fn=self._mcp_status)
            finally:
                self.events.put(None)  # the sentinel the drain loop treats as "worker gone"

        self._worker = threading.Thread(target=_target, name="halo-session", daemon=True)
        self._worker.start()
        return self

    def _mcp_status(self) -> dict:
        if self._mcp_status_fn is None:
            return {"connected": 0, "total": 0}
        try:
            status = self._mcp_status_fn()
            return status if isinstance(status, dict) else {"connected": 0, "total": 0}
        except Exception:
            return {"connected": 0, "total": 0}

    @property
    def alive(self) -> bool:
        return self._worker is not None and self._worker.is_alive()

    def quit(self) -> int:
        """Ask the worker to finish and join it with the 5 s deadline
        (D-TUI). A turn still running when the deadline expires is
        interrupted first, so `quit` from the app's Ctrl+C path never leaves
        a Bash child behind."""
        if self.quit_called:
            return self.exit_code
        self.quit_called = True
        # review finding 2: set BEFORE `abort` / the sentinel is queued --
        # run()'s command loop checks this first, so a `user_input` typed
        # just before quitting (sitting ahead of the sentinel in the
        # queue) is drained without starting a whole new turn.
        self.session._stopping.set()
        self.session.abort.set()
        self.commands.put(None)
        try:
            if self._worker is not None:
                self._worker.join(QUIT_DEADLINE_S)
        finally:
            self._stopped.set()
            # H4 scope B: SessionEnd(quit) -- fired here, after the worker
            # has stopped touching the log but before MCP servers close (a
            # SessionEnd hook may itself want to reach an MCP server).
            try:
                self.session._fire_session_end("quit")
            except Exception:
                pass
            # H8 scope A: "jobs killed on quit" -- a background Bash job
            # (or one a timed-out foreground command was moved to) must
            # never outlive the session that started it.
            try:
                self.session.job_registry.kill_all()
            except Exception:
                pass
            # H11 Part B: a cc: session's own claude subprocess must never
            # outlive halo either -- a safe no-op when cc: was
            # never used this session.
            try:
                self.session.close_cc()
            except Exception:
                pass
            if self.mcp_manager is not None:
                self.mcp_manager.close_all()
        return self.exit_code

    # ---- UI -> loop (non-blocking) ---------------------------------------

    def submit(self, text: str, pasted=None, meta=None) -> None:
        """scope 0(c): while a turn is running, new input steers it
        instead of queuing a whole separate turn -- `session.busy` is a
        plain Event read, safe from this (the UI) thread; `Session.steer`
        is the same kind of direct, thread-safe call as `interrupt()`/
        `answer_permission` (the worker is parked inside the running
        turn's generator and can't read `self.commands`).

        finding 5: `session.busy` here and the busy check INSIDE
        `steer()` are two separate reads -- the turn can finish in the
        (tiny) window between them, in which case `steer()` now correctly
        returns False (see its own docstring) instead of silently queuing
        text nothing will ever drain. That False is handled here by
        falling through to an ordinary fresh turn, so the text is never
        dropped either way. On success, `steer_queued` is pushed to
        `self.events` immediately (U5: shown the instant the user submits,
        not only once the turn reaches a safe point to actually apply
        it -- which can be much later, e.g. mid-tool-call)."""
        if self.session.busy:
            if self.session.steer(text):
                self.events.put(events.steer_queued(text, turn=self.session.turn_count))
                return
            # else: the turn finished in the race window above -- fall
            # through and start a fresh turn instead of dropping the text.
        self.commands.put(events.Command("user_input", {"text": text, "pasted": pasted, "meta": meta}))

    def interrupt(self) -> None:
        """Esc / Ctrl+C during a turn. Direct, not queued: the worker is
        parked inside the turn's generator and only `abort` can reach it
        (the Bash tool polls this same Event to kill its process group)."""
        self.session.abort.set()

    def set_permission_mode(self, mode: str) -> None:
        """review finding 6: a plain string attribute write is atomic/
        thread-safe (like `abort.set()`) -- writing it directly, instead
        of queuing a `set_mode` Command that `run()` only reads BETWEEN
        turns, is what makes Shift+Tab to `auto` actually skip every
        remaining permission card of a turn already in progress (`auto` =
        uninterrupted, scope 0). The status event is emitted from here
        too, since the worker may be parked deep inside a turn and would
        otherwise never get a chance to push it."""
        if mode:
            self.session.permission_engine.mode = mode
        self.events.put(self.session.status_event())

    def set_model(self, model: str) -> Optional[str]:
        """Resolve `model` and hand the swap to the worker. Returns an error
        string (nothing sent) when the reference doesn't resolve."""
        try:
            ref, profile, creds = self.model_resolver(model)
        except Exception as e:  # InvalidModelError and anything else a bad ref can raise
            return f"{e}"
        self.commands.put(events.Command("set_model", {"model_ref": ref, "model_profile": profile, "creds": creds}))
        return None

    def add_permission_rule(self, rule: str, scope: str = "session") -> Optional[Path]:
        """`session` -> in-memory only (the engine learns it now); anything
        else writes `<dest>/settings[.local].json` through
        `permissions.add_allow_rule` (D-CFG's escaping included). Returns
        the path written, or None for a session rule."""
        if not rule:
            return None
        if scope == "session":
            self.session.permission_engine.add_session_allow_rule(rule)
            return None
        return add_allow_rule(rule, scope, cwd=self.cwd)

    def answer_permission(self, request_id: str, decision) -> bool:
        """Direct (see class docstring): unblocks the parked worker."""
        if isinstance(decision, dict):
            decision = Decision(action=decision.get("action", "deny"),
                                 reason=decision.get("reason", ""),
                                 rule=decision.get("rule"),
                                 message=decision.get("message", ""))
        return self.session.resolve_permission(request_id, decision)

    def reevaluate_pending_permission(self, request_id: str) -> Optional[str]:
        """1.0.1 fixpass finding 4: Shift+Tab/`/permissions` mode-change
        re-evaluation for a still-parked ask -- see `Session.
        reevaluate_pending_permission`'s own docstring for the full
        contract. Returns "allow"/"deny" when it actually resolved the
        waiter, else None (still "ask" under the new mode, or nothing
        waiting for `request_id` any more)."""
        return self.session.reevaluate_pending_permission(request_id)

    def register_pending_permission(self, request_id: str, tool_name: str, tool_input: dict) -> None:
        """H15 Part D2.3: registers a slot in the session's own
        `_permission_waiters` for an ask that is resolved OUTSIDE the
        model loop entirely (today: `tui/app.py`'s `!cmd` inline-shell
        confirmation) so a mode change (Shift+Tab/`/permissions`) can reach
        it through the exact SAME `reevaluate_pending_permission` path a
        live tool-call ask already uses, instead of sitting there
        unaffected by mode changes until answered by hand. Safe by
        construction: no worker thread ever calls `_await_permission_
        decision`/blocks on this slot's own `event` (there is no such call
        for an inline command), so this can never wake a nonexistent
        waiter -- the slot exists purely so `decide()` can be re-run
        against the ORIGINAL `tool_name`/`tool_input` under the new mode,
        exactly like `_resolve_tool_call`'s own ask branch stashes them."""
        import threading
        self.session._permission_waiters[request_id] = {
            "event": threading.Event(), "decision": None,
            "tool_name": tool_name, "tool_input": tool_input, "tool": None,
        }

    def discard_pending_permission(self, request_id: str) -> None:
        """Removes a slot `register_pending_permission` added, once the ask
        is resolved through its own normal path (a digit press) -- without
        this, a stale, already-decided slot would sit in `_permission_
        waiters` forever (harmless -- nothing ever re-reads a request_id
        after its card is done -- but needless)."""
        self.session._permission_waiters.pop(request_id, None)

    def answer_question(self, request_id: str, answer) -> bool:
        return self.session.resolve_question(request_id, answer)

    def answer_plan(self, approved: bool, *, feedback: str = "", mode_after: Optional[str] = None) -> None:
        """`PlanCard`'s reply (D-Contract `plan_reply{approved, feedback,
        mode_after}`). Direct (see class docstring), like `answer_permission`/
        `answer_question`: the worker thread is parked inside `ExitPlanMode`'s
        own wait (`Session._handle_exit_plan_mode`, via `_await_reply`), not
        polling the command queue, so this must unblock it directly rather
        than going through `self.commands` -- a queued `plan_reply` would
        just sit there until the turn ends on its own, defeating the point.
        `Session.run()`'s own `plan_reply` branch calls `resolve_plan` too,
        as a safety net for a reply that arrives with no turn in flight."""
        self.session.resolve_plan({"approved": approved, "feedback": feedback, "mode_after": mode_after})

    def list_mcp_servers(self) -> list:
        """One dict per configured server (`McpManager.status()`'s own
        shape) for the `/mcp` dialog, PLUS one synthetic row per cached
        claude.ai connector (W4b connectors bridge -- `type: "connector"`,
        `mcp.connectors_bridge.connector_status_entry`) so the dialog shows
        them too, labelled `claude.ai connector (via claude)`. Cache-only
        (never spawns `claude` from the UI thread); `[]`/no connector rows
        when no manager was built this session or none are cached yet."""
        rows: list = []
        if self.mcp_manager is not None:
            try:
                rows = list(self.mcp_manager.status())
            except Exception:
                rows = []
        try:
            from halo_harness.mcp import connectors_bridge
            rows += [connectors_bridge.connector_status_entry(info) for info in connectors_bridge.get_connectors()]
        except Exception:
            pass
        return rows

    def list_agent_tasks(self) -> list:
        """Halo 2.0.2 round 3 (brief C): every running/queued/background/
        finished sub-agent AND background Bash job of this session, for
        the `/tasks` panel -- sub-agent rows from `agent.subagent.list_
        agent_task_rows` (read straight off disk: meta.json + each
        child's own jsonl log, correct even right after a `-c` resume),
        PLUS one row per `agent.jobs.JobRegistry.list_jobs()` entry
        (H8 scope A's own docstring already named this exact use: "the
        `/tasks` slash command's own data source" -- written long before
        `/tasks` itself existed) adapted into the SAME row shape, `agent_
        id` prefixed `job-` so it can never collide with a real agent_id
        and `log_path` left None (no transcript viewer for a bash job --
        BashOutput/the pager already cover its captured output)."""
        rows: list = []
        if getattr(self.session, "agent_runtime", None) is not None:
            try:
                from halo_harness.agent.subagent import list_agent_task_rows
                rows = list_agent_task_rows(self.session)
            except Exception:
                rows = []
        job_registry = getattr(self.session, "job_registry", None)
        if job_registry is not None:
            import time
            now = time.time()
            for job in job_registry.list_jobs():
                started = job.get("started_at")
                rows.append({
                    "agent_id": f"job-{job['id']}", "task_id": job["id"],
                    "title": job.get("description") or (job.get("command") or "")[:60], "model": "(bash)",
                    "status": job.get("status") or "running",
                    "is_error": job.get("status") in ("failed", "killed"), "tool_count": 0, "cost_usd": None,
                    "started": started,
                    "elapsed_s": max(0.0, now - started) if isinstance(started, (int, float)) else None,
                    "depth": 0, "parent_agent_id": None, "log_path": None,
                })
        return rows

    def read_task_board(self) -> list:
        """Halo 2.0.2 round 3 (brief C): the shared task board (`TaskCreate`/
        `TaskUpdate`/`TaskList` tools) for the tasks panel's second tab --
        `tools.task_board.read_board`'s own list of `{id, title, status,
        owner, notes, result}` dicts, `[]` if nothing has been created yet
        or this session has no log directory at all."""
        try:
            from halo_harness.tools.task_board import read_board
            return read_board(self.session.log.dir / self.session.log.session_id)
        except Exception:
            return []

    def list_permission_rules(self) -> list:
        """`[{"action": "allow"|"ask"|"deny", "source": ..., "rule": ...},
        ...]` -- the live `PermissionEngine`'s own rule lists, for the
        `/permissions` dialog."""
        engine = self.session.permission_engine
        out: list = []
        for action, rules in (("deny", engine.deny_rules), ("ask", engine.ask_rules), ("allow", engine.allow_rules)):
            for r in rules:
                out.append({"action": action, "source": r.source or "?", "rule": r.raw or r.value or r.tool})
        return out

    # ---- slash commands ---------------------------------------------------

    def run_compact(self, instructions: str = "") -> str:
        """finding 11 (major, h4-h5-h3c review) / U5 must-do: `/compact`
        queued as a worker `Command` (`run_compact`) instead of running
        `session._run_compaction` to completion synchronously ON THIS (the
        UI) thread -- the old path froze the whole TUI (no repaint, no
        Esc) for the entire summarisation call. Deferred/refused outright
        while a turn is already running (`self.session.busy`), matching
        "handled between turns, deferred while busy" -- a compaction and
        an ordinary turn must never race over the same log. Its
        `compaction` events stream through the normal event pipe, so
        `tui/dispatch.py`'s new handler renders live progress instead of
        the UI just hanging."""
        # W4a misc: "`/compact` typed mid-turn is queued and runs when the
        # turn ends" -- queued onto the SAME worker Command queue either
        # way (it's already thread-safe FIFO); `run()`'s own pump loop only
        # ever reaches the NEXT Command once `_pump_turn` returns, so a busy
        # session naturally defers this to right after the current turn
        # ends, with no retyping needed. Only the returned NOTICE differs.
        self.commands.put(events.Command("run_compact", {"instructions": instructions.strip() or None}))
        if self.session.busy:
            return "A turn is already running -- /compact is queued and will run once it finishes."
        return ""

    def clear_session(self) -> str:
        """U5 must-do: `/clear` queued onto the worker thread (`run_clear`)
        -- `Session.clear()` fires real `SessionEnd(clear)`/
        `SessionStart(clear)` hooks (arbitrary user scripts) and builds a
        fresh log, so it gets the same off-the-UI-thread treatment as
        `run_compact`. The OLD behaviour only cleared the TUI's own
        transcript WIDGET -- the underlying log/context the next request
        would still derive from was completely untouched."""
        if self.session.busy:
            return "A turn is already running -- /clear will need to wait until it finishes."
        self.commands.put(events.Command("run_clear", {}))
        return ""

    def run_slash(self, name: str, args: str = "") -> str:
        """Resolve `name` through the U0 registry.

        * a `prompt`-kind command (custom command / skill) is expanded and
          submitted as the next user turn -- text comes back "" and the
          caller should not print anything;
        * `/compact` (finding 11) is queued onto the worker thread via
          `run_compact` instead of running synchronously here;
        * anything else runs through the TUI facade right here (it is local
          and fast) and its returned text is handed back as the result.
        """
        if self.registry is None:
            return f"No command registry is loaded: /{name}"
        cmd = self.registry.resolve(name)
        if cmd is None:
            return f"Unknown command: /{name} (try /help)"
        if name == "compact" and cmd.source == "builtin":
            return self.run_compact(args)
        if cmd.kind == "prompt" and cmd.run is not None:
            from halo_harness.commands.registry import expand_command_body, read_at_mention_snapshots

            body = cmd.run(args, self.facade)
            # finding 9: route through the real session's permission
            # engine + stripped tool env (see commands/registry.py's
            # `run_preexec_commands` docstring).
            result = expand_command_body(
                body, args, allowed_tools=getattr(cmd, "allowed_tools", None), cwd=self.cwd,
                permission_engine=getattr(self.session, "permission_engine", None),
                env=getattr(self.session, "tool_env", None),
            )
            if result.error:
                return result.error
            # H4 scope C: `@path` attachments in the EXPANDED body -- read
            # via the Read tool's own path resolution and appended as
            # snapshots (never inlined into the prompt text itself, same
            # as CLAUDE.md's own @import convention).
            # H5c finding 14: `queue_log_write` (never a direct
            # `self.session.log.append_snapshot`) -- this runs on the UI
            # thread, BEFORE `self.submit(result.text)` below decides
            # steer-vs-new-turn; a direct write here would race the worker
            # thread's own concurrent log writes while the session is busy,
            # same bug as `ingest_at_mentions`'s own two loops.
            for path_str, content in read_at_mention_snapshots(result.text, cwd=self.cwd):
                self.session.queue_log_write(
                    "snapshot", {"blocks": [{"type": "text", "text": f"@{path_str}\n{content}"}],
                                 "snapshot_kind": "at_mention"})
            # H8 scope E: `@server:resource` mentions in the expanded body.
            from halo_harness.mcp.mentions import read_server_resource_snapshots
            for label, content in read_server_resource_snapshots(
                    result.text, mcp_manager=getattr(self.session, "mcp_manager", None)):
                self.session.queue_log_write(
                    "snapshot", {"blocks": [{"type": "text", "text": f"@{label}\n{content}"}],
                                 "snapshot_kind": "at_mention"})
            # W4a: UserPromptExpansion -- see Session.fire_user_prompt_
            # expansion's own docstring for why THIS is the real expansion
            # point (never a plain typed prompt's own @mentions).
            if hasattr(self.session, "fire_user_prompt_expansion"):
                extra_ctx = self.session.fire_user_prompt_expansion(body, result.text)
                if extra_ctx:
                    self.session.queue_log_write(
                        "snapshot", {"blocks": [{"type": "text", "text": extra_ctx}], "snapshot_kind": "hook_context"})
            self.submit(result.text)
            return ""
        if cmd.run is None:
            return f"/{name} has no implementation in this build."
        try:
            return cmd.run(args, self.facade)
        except Exception as e:  # a command must never take the UI down with it
            return f"/{name} failed: {type(e).__name__}: {e}"

    # ---- read-only queries (UI thread, synchronous, cheap) ----------------

    def list_models(self) -> list:
        """`[{ref, context_tokens, max_output_tokens, price_in_per_m,
        price_out_per_m, provider, group, detail, dbu}, ...]` for the
        ModelPicker/init picker -- from the same models.json/dbx-endpoints.
        json + routes.json aliases the resolver uses, so a pick is
        guaranteed to resolve. 1.0.1 hotfix 12: prices are ALWAYS USD per
        MILLION tokens now (`model_display.format_price_per_m`'s own unit),
        normalized at the source here regardless of which raw unit the
        underlying catalog used, so every row -- OpenRouter, cc:, Databricks
        alike -- renders through the exact same `model_display.
        format_model_row` with no per-provider special-casing left at
        render time."""
        from halo_harness.providers.databricks import load_models_json
        from halo_harness.providers.enablement import credentials_present, is_enabled, label_for

        # 1.0.1 part 2 fixpass finding 3: the session's own real `Settings.
        # effective_env` (shell < user < trusted project/local < policy) --
        # a credential living only in a settings.json `env` block is seen
        # here exactly like a real turn would resolve it, instead of this
        # listing surface disagreeing with it (a test-constructed Controller
        # with no `settings=` passes None, falling back to bare os.environ,
        # byte-for-byte unchanged from before this fix).
        env = self.settings.effective_env if self.settings is not None else None

        # H15 item 21.2: one dim hint entry (never a selectable `ref`) per
        # provider that's DETECTED (real credentials/login) but not yet
        # ENABLED -- `tui/dialogs/model_picker.py` renders these as a
        # disabled row at the bottom instead of a selectable model.
        hints: "list[dict]" = []

        def _maybe_hint(name: str, *, detected: "Optional[bool]" = None) -> None:
            is_detected = credentials_present(name, env=env) if detected is None else detected
            if is_enabled(name, detected=is_detected) or not is_detected:
                return
            hints.append({"hint": f"{label_for(name)} detected but not enabled -- "
                                   f"run `halo providers enable {name}`"})

        def _per_m(price_per_token) -> "float | None":
            # 1.0.1 fixpass finding 7: models.json stores EVERY OpenRouter
            # price as a STRING (e.g. "0.0000008", confirmed across all 464
            # cached entries) -- float(v) in a try/except, same as the
            # pre-1.0.1 code, so a numeric string is never treated as
            # unknown and every row's price columns go blank.
            if isinstance(price_per_token, bool):
                return None
            try:
                return float(price_per_token) * 1_000_000
            except (TypeError, ValueError):
                return None

        out: list = []
        seen = set()
        # H15 item 21.2: an openrouter alias is a `vendor/model` string with
        # no `or:` prefix of its own -- still gated the same way, so a
        # disabled OpenRouter never leaks its catalog through the routes.json
        # alias table either.
        or_detected = credentials_present("openrouter", env=env)
        or_enabled = is_enabled("openrouter", detected=or_detected)
        try:
            models = load_models_json(self.state_dir) or {} if or_enabled else {}
        except Exception:
            models = {}
        for name in sorted(models):
            entry = models.get(name) or {}
            ref = f"or:{name}"
            seen.add(ref)
            pricing = entry.get("pricing") or {}
            out.append({
                "ref": ref, "context_tokens": entry.get("context_length") or 128000,
                "max_output_tokens": entry.get("max_output_tokens") or 16384,
                "price_in_per_m": _per_m(pricing.get("prompt")), "price_out_per_m": _per_m(pricing.get("completion")),
                "provider": "openrouter",
            })
        _maybe_hint("openrouter", detected=or_detected)
        if or_enabled:
            for alias, target in sorted((self.routes.get("aliases") or {}).items()):
                if alias not in seen:
                    out.append({"ref": alias, "provider": "alias", "target": target})
        # H11 Part A: the "Claude Code subscription" group (H15 part C's
        # own label, see providers/enablement.py LABELS) -- the cc:
        # aliases, with real profile data when known (providers.cc_models.
        # CC_MODEL_TABLE / a --refresh cache), so they filter/sort/price
        # alongside every OpenRouter row above instead of needing a
        # separate picker. 1.0.1 hotfix addendum 12: group label shortened
        # from "Claude subscription (via Claude Code)" so a full row still
        # fits in 110 columns.
        #
        # 1.0.1 hotfix addendum 9: shown ONLY when the subscription route is
        # actually available -- `claude auth status` reports a real
        # claude.ai login (`SUBSCRIPTION_AUTH_METHODS`, the SAME check
        # `init_cli.py::_claude_login_available`/`model.py`'s bare-alias
        # resolver already use). Verified live: a Databricks WORK box had
        # `claude` logged in via its OWN work settings (authMethod !=
        # "claude.ai") -- the old, unconditional version listed nine cc:
        # models here that a `cc:<name>` call would then refuse at request
        # time, with no hint from the picker that they'd never work.
        # 1.0.1 fixpass finding 1: `cached_claude_auth_status()`, never the
        # real `claude_auth_status()` -- this method is rebuilt on every
        # `/model` open; calling the real one here meant spawning a
        # `claude auth status` subprocess (up to a 10s timeout) synchronously
        # every single time, measured as the single largest piece of a
        # 2-5s TUI freeze on a real work box. Only a startup worker
        # (tui/app.py's on_mount) ever populates that cache now.
        from halo_harness.providers.cc_models import CC_ALIASES, SUBSCRIPTION_AUTH_METHODS, alias_display_detail, \
            cached_claude_auth_status, profile_fields_for_cc_model
        try:
            status = cached_claude_auth_status()
        except Exception:
            status = None
        cc_available = bool(status and status.logged_in and status.auth_method in SUBSCRIPTION_AUTH_METHODS)
        # 1.0.1 part 2 fixpass critical finding 2: `detected=cc_available`
        # -- `cc_available` is the EXACT SAME signal `credentials_present
        # ("claude_subscription")` would compute (a real claude.ai login),
        # just already read from the cache above; without this, both
        # `is_enabled("claude_subscription")` calls below independently
        # re-derived it via `credentials_present` -> `claude_login_
        # available()` -> an UNCACHED `claude auth status` SPAWN apiece --
        # 2 extra subprocess launches (up to a 10s timeout each) every
        # single `/model` open, reintroducing exactly what fixpass finding
        # 1 removed from `cc_available` itself just above.
        # H15 item 21.2/21.6: shown only once the Claude subscription is ALSO
        # enabled -- a real claude.ai login no longer lists cc: models on its
        # own (item 21.1: a login never enables anything by itself).
        if cc_available and is_enabled("claude_subscription", detected=cc_available):
            for alias, cc_target in CC_ALIASES.items():
                ref = f"cc:{alias}"
                if ref in seen:
                    continue
                fields = profile_fields_for_cc_model(cc_target) or {}
                out.append({
                    "ref": ref, "context_tokens": fields.get("context_tokens"),
                    "max_output_tokens": fields.get("max_output_tokens"),
                    "price_in_per_m": _per_m(fields.get("price_in")), "price_out_per_m": _per_m(fields.get("price_out")),
                    # H15 addendum 2: "-> <resolved-id>" next to the alias.
                    "detail": alias_display_detail(alias),
                    "provider": "cc", "group": label_for("claude_subscription"),
                })
        if cc_available and not is_enabled("claude_subscription", detected=cc_available):
            hints.append({"hint": f"{label_for('claude_subscription')} detected but not enabled -- "
                                   f"run `halo providers enable claude_subscription`"})
        # H15 item 21: the `ant:` (direct Anthropic API key) group -- the
        # same nine subscription-model aliases as cc: above, resolved
        # against the real API instead of the installed `claude` binary.
        # Never shown before this item (no gate existed to hide it behind),
        # so this is also the group's first appearance in `/model` at all.
        from halo_harness.providers.config import resolve_anthropic
        ant_available = resolve_anthropic(env) is not None
        if ant_available and is_enabled("anthropic", detected=ant_available):
            from halo_harness.init_providers import _cc_ant_entries
            from halo_harness.providers.cc_models import ANT_ALIASES
            for entry in _cc_ant_entries("ant", ANT_ALIASES):
                if entry["ref"] in seen:
                    continue
                out.append({**entry, "provider": "anthropic", "group": "ant: (Anthropic API)"})
        elif ant_available:
            hints.append({"hint": f"{label_for('anthropic')} detected but not enabled -- "
                                   f"run `halo providers enable anthropic`"})
        # H14 scope I: the discovered Databricks endpoint catalog
        # (~/.halo/dbx-endpoints.json, from `init --preset work`/
        # `models --refresh` -- never a vendored list), grouped by family,
        # each row showing its chosen path type and DBU rate when known;
        # a known non-chat endpoint (embeddings/whisper) is hidden here
        # (`halo models` itself still lists it, for diagnostics).
        dbx_detected = credentials_present("databricks", env=env)
        dbx_enabled = is_enabled("databricks", detected=dbx_detected)
        try:
            from halo_harness.providers.databricks import dbx_endpoints_cache_is_old_shape, load_dbx_endpoints_json
            from halo_harness.providers.dbx_routing import (
                PATH_TYPE_DISPLAY, classify_family, default_path_type, format_dbu_cost,
            )
            endpoints = load_dbx_endpoints_json(self.state_dir) if dbx_enabled else {}
        except Exception:
            endpoints = {}
        _maybe_hint("databricks", detected=dbx_detected)
        # 1.0.1 hotfix 4/addendum 10: an old-shape cache (no `api_types` ever
        # recorded) makes `default_path_type` silently degrade to
        # "invocations" for every openai-chat family -- checked ONCE here,
        # same migration signal `catalog_cli.py`'s own table uses, so this
        # picker never shows that wrong answer as if it were real data.
        old_shape = dbx_endpoints_cache_is_old_shape(endpoints)
        from halo_harness.model_display import databricks_row_fields
        from halo_harness.providers.profiles import load_model_table
        model_table = load_model_table()
        # 1.0.1 fixpass finding 1: loaded ONCE for the whole loop below, not
        # once per endpoint (databricks_row_fields's own `live_models_dev`/
        # `vendored_fallback` params) -- models-dev.json can be several MB;
        # 30 Databricks rows on a real catalog re-read and re-parsed the
        # same file 30 times (1.37s measured on a fast host) before this.
        try:
            from halo_harness.providers.models_dev import (
                databricks_entries_from_full_models_dev, load_models_dev_json, load_vendored_databricks_fallback,
            )
            live_models_dev = databricks_entries_from_full_models_dev(load_models_dev_json(self.state_dir))
            vendored_fallback = load_vendored_databricks_fallback()
        except Exception:
            live_models_dev, vendored_fallback = {}, {}
        for name in sorted(endpoints):
            ref = f"dbx:{name}"
            if ref in seen:
                continue
            e = endpoints[name] if isinstance(endpoints[name], dict) else {}
            family = classify_family(name, foundation_model_name=e.get("foundation_model_name") or "",
                                      model_class=e.get("model_class") or "")
            if family == "non_chat":
                continue
            if old_shape:
                path_type = "unknown"
            else:
                try:
                    path_type = default_path_type(name, self.state_dir)
                except Exception:
                    path_type = "?"
            usage_policy = e.get("usage_policy") if isinstance(e.get("usage_policy"), dict) else {}
            dbu = format_dbu_cost(usage_policy.get("output_dbu_per_1k_tokens"))
            try:
                fields = databricks_row_fields(name, state_dir=self.state_dir, model_table=model_table,
                                                live_models_dev=live_models_dev, vendored_fallback=vendored_fallback)
            except Exception:
                fields = {}
            path_display = PATH_TYPE_DISPLAY.get(path_type, path_type)
            out.append({
                "ref": ref, "context_tokens": fields.get("context_tokens"),
                "max_output_tokens": fields.get("max_output_tokens"),
                "price_in_per_m": fields.get("price_in_per_m"), "price_out_per_m": fields.get("price_out_per_m"),
                "provider": "databricks", "group": f"Databricks ({family})", "path_type": path_type,
                "detail": f"{family} · {path_display}", "dbu": dbu if dbu != "?" else None,
                "task": e.get("task"),
            })
        current = self.session.model_ref.raw
        if current and current not in {m["ref"] for m in out}:
            out.insert(0, {"ref": current, "context_tokens": self.session.model_profile.context_tokens,
                           "max_output_tokens": self.session.model_profile.max_output_tokens,
                           "provider": self.session.model_ref.provider})
        # H15 item 21.2: dim, non-selectable hint rows (no "ref" at all) go
        # LAST -- after the `current` insertion above, which indexes `out`
        # by `m["ref"]` and would KeyError on a hint dict otherwise.
        out.extend(hints)
        return out

    def list_sessions(self) -> list:
        """`[{id, cwd, mtime, summary, title, model, cost_usd, turns}, ...]`,
        newest first, for the `--resume`/`/resume` picker (U5 sessions UX:
        "title/age/cost/turns" -- `title` is whatever `/rename` or the
        after-first-turn auto-title last wrote as a `meta` node's own
        `title` field, `""` if never set; `cost_usd`/`turns` are summed/
        counted straight from that session's own `usage`/`user` nodes, the
        same source `/cost` and `/stats` read). `model` (H13 Part C: "/resume
        search... fuzzy over title, first prompt, cwd and model") is the
        LAST `meta` node's own `model` field seen, `""` if the session
        predates any `meta` node ever recording one. Reads OUR session logs
        only (~/.halo). Synchronous file I/O -- U5 must-do: callers on
        the UI thread (the `/resume` slash handler) run this on a worker,
        never inline (matches `_git_branch`'s own fix)."""
        slug = project_slug(self.cwd)
        directory = self.state_dir / "sessions" / slug
        out: list = []
        if not directory.is_dir():
            return out
        for path in sorted(directory.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True):
            summary, title, model, cost_usd, turns = "", "", "", 0.0, 0
            try:
                for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
                    try:
                        node = json.loads(line)
                    except ValueError:
                        continue
                    ntype = node.get("type")
                    if ntype == "user":
                        turns += 1
                        if not summary:
                            for block in node.get("content") or []:
                                if isinstance(block, dict) and block.get("type") == "text":
                                    summary = block.get("text", "")[:80]
                                    break
                    elif ntype == "meta":
                        if isinstance(node.get("title"), str) and node["title"]:
                            title = node["title"]
                        if isinstance(node.get("model"), str) and node["model"]:
                            model = node["model"]
                    elif ntype == "usage":
                        c = node.get("cost_usd")
                        if isinstance(c, (int, float)):
                            cost_usd += c
            except OSError:
                pass
            out.append({"id": path.stem, "cwd": str(self.cwd), "mtime": path.stat().st_mtime,
                        "summary": summary, "title": title, "model": model,
                        "cost_usd": cost_usd, "turns": turns})
        return out

    def resume(self, session_id: str) -> None:
        """Read a prior session's log and push ONE `replay` event carrying
        its renderable messages. The next prompt continues THAT session:
        the Session's own log is reopened on it (see
        `halo_harness.tui.app` for the swap)."""
        from halo_harness.agent.log import SessionLog
        from halo_harness.termtitle import set_terminal_title

        # Halo 2.0.2 W7 round 1 (brief F): "set it ... on resume" -- this
        # is the one method both the command palette's session pick
        # (tui/app.py's own `_on_palette_pick`) and the `/resume` picker
        # (tui/slash.py's `_open_resume_picker`) call, so hooking it here
        # covers both triggers with no duplication.
        set_terminal_title("halo")
        log = SessionLog(self.cwd, session_id=session_id)
        self.replay_messages = _messages_from_nodes(log.nodes())
        if self.replay_messages:
            self.events.put(events.replay(self.replay_messages))

    def mcp_status(self) -> dict:
        return self._mcp_status()

    def reconnect_mcp(self, name: str, abort=None) -> list:
        """`abort` (u2-h3b finding 9, default None -- unchanged for every
        existing sync caller): a `threading.Event` a caller running THIS
        on its own worker thread (never the UI thread itself -- see
        `tui/dialogs/mcp_status.py`) can set to cut a hung/slow reconnect's
        WAIT short, threaded down to `McpManager.reconnect`'s own
        abort-aware close/start waits. The reconnect keeps running
        regardless (same "abandoned, not stopped" caveat as everywhere
        else abort-aware waiting is used in this codebase) -- aborting
        just stops the CALLER from blocking on it."""
        if self._reconnect_fn is None:
            return [f"MCP support is not connected in this build ({name} unchanged)."]
        # H9 bug fix (item 11, MCP compatibility matrix): `McpManager.
        # reconnect()` (what `self._reconnect_fn` calls) only ever restarts
        # an ALREADY-KNOWN handle using its config from when the session
        # started -- neither it nor this method used to re-read
        # ~/.claude.json/.mcp.json at all, so a real on-disk edit to an
        # existing server's command/args/env/url, or a brand-new server
        # name added since, was invisible no matter how many times /mcp
        # reconnect ran. Re-resolve the config chain from a freshly re-read
        # ~/.claude.json (project/local/user scopes -- the same tiers
        # `resolve_server_configs` itself documents; launch-time-only
        # concerns like --mcp-config/--chrome/--playwright are deliberately
        # NOT re-applied here, since those are a NEW SESSION's decision,
        # not something reconnecting one server should reconsider) and
        # `resync_from` the live manager against it BEFORE the plain
        # restart below -- covers both "config changed" and "brand new
        # name" in one path. Best-effort: any failure here (no `mcp`
        # package, a malformed file, an unresolvable path) must never
        # block the plain reconnect that already worked before this fix.
        if self.mcp_manager is not None:
            try:
                from halo_harness.config.claude_json import load_claude_json
                from halo_harness.mcp.manager import resolve_server_configs
                fresh_configs, _notices = resolve_server_configs(
                    cwd=self.cwd, claude_json=load_claude_json(), settings=self.settings)
                self.mcp_manager.resync_from(fresh_configs)
            except Exception:
                pass
        try:
            result = self._reconnect_fn(name, abort=abort)
            # H3b must-do (unwired seam): a reconnected server's tool list
            # only ever entered the deferred pool once, at session start
            # (`SessionCatalog` built from `McpManager.all_tools()` THEN) --
            # without this, `/mcp reconnect` on a server whose tools
            # changed (or one added mid-session) leaves them permanently
            # unreachable through ToolSearch even though the reconnect
            # itself succeeded.
            catalog = getattr(self.session, "session_catalog", None)
            if catalog is not None:
                try:
                    catalog.refresh_deferred_for_server(name)
                except Exception:
                    pass
            return result if isinstance(result, list) else [str(result)]
        except Exception as e:
            return [f"reconnect {name} failed: {type(e).__name__}: {e}"]

    def approve_mcp_server(self, name: str, abort=None) -> list:
        """H3b must-do (unwired seam): completes the `.mcp.json`
        `pending_approval` interactive flow. Persists the approval via
        `mcp_setup.record_mcp_approval` -- keyed by the entry's own
        sha256 (D-CFG: an edited/tampered entry needs re-approval), read
        straight back off `<cwd>/.mcp.json` by name rather than needing a
        new field threaded through `mcp/manager.py`'s own config objects
        -- so the server stays approved on the NEXT launch too, not just
        this session, then reconnects it (which also now refreshes its
        deferred ToolSearch pool, see `reconnect_mcp`). `abort`: see
        `reconnect_mcp`'s own docstring -- threaded through to the
        reconnect this triggers."""
        if self.mcp_manager is None:
            return [f"MCP support is not connected in this build ({name} unchanged)."]
        handle = self.mcp_manager.handles.get(name)
        if handle is None:
            return [f"unknown MCP server: {name}"]
        if handle.config.scope != "project":
            return [f"{name} is not a .mcp.json (project-scope) server -- nothing to approve"]
        import json as _json

        mcp_json_path = self.cwd / ".mcp.json"
        try:
            raw = _json.loads(mcp_json_path.read_text(encoding="utf-8-sig"))
            raw_entry = (raw.get("mcpServers") or {})[name]
        except (OSError, ValueError, KeyError) as e:
            return [f"could not read {name}'s .mcp.json entry to approve it: {type(e).__name__}: {e}"]
        from halo_harness import mcp_setup
        mcp_setup.record_mcp_approval(name, raw_entry)
        handle.config.pending_approval = False
        return self.reconnect_mcp(name, abort=abort)

    def memory_path(self):
        from halo_harness.config.paths import memory_dir
        return memory_dir(str(self.cwd))

    def ingest_at_mentions(self, text: str) -> None:
        """`@file#L10-20` mentions (U5 scope A): resolve + read via the
        Read tool (the SAME path resolution/line-numbering a model's own
        Read call would use) and append each as a log snapshot -- the
        mention text ITSELF is left untouched in the submitted turn
        (matches `commands/registry.py`'s own `@path` convention for
        custom commands: never inlined, always a separate context block).
        Best-effort per mention: a path that doesn't resolve to a real
        file is silently skipped, never an error (the model still sees
        the literal `@mention` text and can ask/Read it itself)."""
        from halo_harness.tools.base import ToolContext
        from halo_harness.tools.read import ReadTool
        from halo_harness.tui.completion import parse_at_mentions

        # H9 whole-tree review finding 14: `@server:resource` mentions now
        # resolve on a background thread, never the CALLER's own (the UI
        # thread, for a live TUI submit/steer) -- `McpManager.resources()`
        # is one live `resources/list` RPC per connected server
        # (MCP_TIMEOUT, 30s, EACH), and used to run synchronously here,
        # BEFORE the `@path`-only early return below, on EVERY submit/steer
        # regardless of whether `text` even had an `@` in it. Verified with
        # a stub manager: "fix the failing test"/"why is the build red?"/
        # "thanks" each blocked for 1.5s; a genuinely hung server blocked
        # for its full 30s on every Enter. The same cheap regex `mcp.
        # mentions.extract_server_resource_mentions` itself now also runs
        # first is checked again here so the thread is never even spawned
        # for the common case (no `@name:uri`-shaped text at all) --
        # `queue_log_write` (the same thread-safe write every other
        # @mention here already uses -- see the `@path` loop below) is what
        # makes it safe for this to land whenever the fetch actually
        # finishes, possibly well after this method itself has returned.
        from halo_harness.mcp.mentions import _AT_SERVER_RESOURCE_RE
        mcp_manager = getattr(self.session, "mcp_manager", None)
        if mcp_manager is not None and _AT_SERVER_RESOURCE_RE.search(text or ""):
            threading.Thread(target=self._ingest_mcp_resource_mentions, args=(text, mcp_manager),
                              daemon=True, name="halo-mcp-mention-fetch").start()

        mentions = parse_at_mentions(text)
        if not mentions:
            return
        tool = ReadTool()
        # H8 scope B: `vision` threaded through so an `@path.png` mention
        # attaches a REAL image block (same gate/size rule as a model-issued
        # Read call), not just a "no vision" text note. `self.session` may
        # be a lightweight test double with no `model_profile` at all (e.g.
        # test_tui.py's `_real_controller`'s `_MinimalSession`) -- two
        # levels of getattr, never a bare attribute access, so that's a
        # quiet `vision=False` rather than an AttributeError that would
        # abort every @mention in the turn.
        model_profile = getattr(self.session, "model_profile", None)
        ctx = ToolContext(cwd=self.cwd, vision=getattr(model_profile, "vision", False))
        for raw, start, end in mentions:
            path = Path(raw).expanduser()
            if not path.is_absolute():
                path = self.cwd / path
            try:
                resolved = path.resolve()
            except OSError:
                continue
            if not resolved.is_file():
                continue
            input_data = {"file_path": str(resolved)}
            label = raw
            if start is not None:
                input_data["offset"] = max(0, start - 1)
                input_data["limit"] = max(1, (end or start) - start + 1)
                label = f"{raw}#L{start}" if not end or end == start else f"{raw}#L{start}-{end}"
            try:
                result = tool.run(input_data, ctx)
            except Exception:
                continue
            if isinstance(result.content, list):
                # An image block (or list of blocks) from _read_image --
                # keep it a REAL content block, never stringify it into
                # unreadable dict-repr text.
                blocks = [{"type": "text", "text": f"@{label}"}] + list(result.content)
            else:
                content = result.content if isinstance(result.content, str) else str(result.content)
                blocks = [{"type": "text", "text": f"@{label}\n{content}"}]
            try:
                # H5c finding 14: `queue_log_write` (never a direct
                # `self.session.log.append_snapshot`) -- this runs on the UI
                # thread, BEFORE `submit()` decides steer-vs-new-turn; while
                # the session is busy, a direct write here would race the
                # worker thread's own concurrent log writes (verified: a
                # steer carrying `@mention` typed while a Read tool runs
                # made the NEXT request's user message
                # `['text','tool_result','text']`, which Anthropic 400s on).
                # `_ingest_mcp_resource_mentions` below uses the SAME call,
                # from its own background thread.
                self.session.queue_log_write("snapshot", {"blocks": blocks, "snapshot_kind": "at_mention"})
            except Exception:
                pass

    def _ingest_mcp_resource_mentions(self, text: str, mcp_manager) -> None:
        """H9 whole-tree review finding 14: the actual `@server:resource`
        fetch -- `resources/list` (or, once cached, nothing) then
        `resources/read` per real match -- runs HERE, on the background
        thread `ingest_at_mentions` spawned, never on the caller's own.
        `queue_log_write` is thread-safe by design (see `Session.
        queue_log_write`'s own docstring), so this may finish anywhere from
        immediately to well after `ingest_at_mentions` itself already
        returned and the turn is already under way -- the snapshot lands
        in the log at the next safe point either way."""
        from halo_harness.mcp.mentions import read_server_resource_snapshots, unresolved_server_mentions
        try:
            snapshots = read_server_resource_snapshots(text, mcp_manager=mcp_manager)
        except Exception:
            snapshots = []
        for res_label, res_content in snapshots:
            try:
                self.session.queue_log_write(
                    "snapshot", {"blocks": [{"type": "text", "text": f"@{res_label}\n{res_content}"}],
                                 "snapshot_kind": "at_mention"})
            except Exception:
                pass
        # W4a misc: an `@name:uri` naming a server that ISN'T connected at
        # all (never the "server is fine, that exact uri just isn't listed"
        # case `read_server_resource_snapshots` already treats as silent)
        # gets a visible warning, naming the server -- queued the SAME
        # thread-safe way as the snapshots above so it lands in the
        # transcript regardless of whether a turn is already running.
        try:
            for server in unresolved_server_mentions(text, mcp_manager=mcp_manager):
                warning = f"@{server}:... does not match any currently connected MCP server named {server!r}."
                self.session.queue_log_write(
                    "snapshot", {"blocks": [{"type": "text", "text": warning}], "snapshot_kind": "at_mention"})
                # review finding 23 / parity gap: the snapshot above is
                # model-only context (same "never inlined, separate block"
                # rule every @mention here follows) -- the commit this
                # shipped in says the warning itself "warns visibly", which
                # only ever happened to the MODEL. A live TUI session gets
                # an actual notice too, the same sink a background sub-
                # agent's own live asks use (`_event_sink`, set by `run()`;
                # None for -p/a bare Session, where there is no live UI to
                # notice at all -- headless.py's own identical call site
                # prints a stderr line instead for that case).
                sink = getattr(self.session, "_event_sink", None)
                if sink is not None:
                    try:
                        sink(events.notification(warning))
                    except Exception:
                        pass
        except Exception:
            pass

    # ---- U5 scope A: `!cmd` inline shell -- through the Bash tool + the
    # SAME permission rules a model-issued call would get, but never
    # through the model loop (`agent/loop.py`'s command pump is untouched:
    # nothing here ever queues a `Command`). -----------------------------

    def decide_inline_shell(self, command: str) -> Decision:
        """The `Decision` a plain `Bash(command)` call would get under the
        CURRENT permission mode/rules -- read-only, no side effect.
        `tui/app.py`'s `!cmd` handling uses this to decide whether to run
        immediately, show a confirmation card, or refuse outright."""
        return self.session.permission_engine.decide("Bash", {"command": command})

    def run_inline_shell(self, command: str) -> "tuple[str, object]":
        """Execute `command` via the real Bash tool. Synchronous -- always
        called from a Textual worker THREAD (`tui/app.py`'s `run_worker
        (thread=True)`, same pattern as `_git_branch_worker`) -- a
        DIFFERENT thread from the session's OWN dedicated worker thread,
        and nothing here checks `session.busy` before running (by design:
        `!cmd` runs OUTSIDE the model loop entirely, on demand, regardless
        of whether a turn happens to be in flight). Returns `(tool_use_id,
        ToolResult)`; the command runs immediately either way, but H5c
        finding 14: the resulting tool_use/tool_result pair is queued
        (never written directly) so it can never race the session's own
        worker thread's concurrent log writes -- verified: an inline
        `!cmd` landing between an assistant `tool_use` and its own
        `tool_result` broke pairing on every route."""
        from halo_harness.tools.base import ToolContext
        from halo_harness.tools.bash import BashTool
        from halo_harness.hooks import env_file_path

        # finding 9: the session's own stripped tool_env (never a raw
        # os.environ read, which used to leak every provider API key into
        # an inline `!cmd`'s subprocess) + CLAUDE_ENV_FILE (finding 6),
        # same as a model-issued Bash call gets via _dispatch_tools.
        result = BashTool().run(
            {"command": command},
            ToolContext(cwd=self.cwd, bash_state=self._inline_bash_state,
                        env=getattr(self.session, "tool_env", None),
                        env_file=env_file_path(self.session.log.session_id)),
        )
        tool_use_id = f"inline_{uuid.uuid4().hex[:12]}"
        try:
            self.session.queue_log_write("inline_shell", {
                "tool_use_id": tool_use_id, "command": command,
                "content": result.content, "is_error": result.is_error,
            })
        except Exception:
            pass  # logging is best-effort -- the command already ran either way
        return tool_use_id, result

    # ---- U5 scope C: session titles/rename/fork/export/stats ----------

    def get_title(self) -> str:
        """The current session's own title (the last `meta` node carrying
        one), "" if never set."""
        for node in reversed(self.session.log.nodes()):
            if node.get("type") == "meta" and isinstance(node.get("title"), str) and node["title"]:
                return node["title"]
        return ""

    def rename_session(self, title: str) -> None:
        title = (title or "").strip()
        if title:
            self.session.log.append_meta(title=title)

    def maybe_autoname_title(self) -> Optional[str]:
        """After the FIRST turn completes (`tui/app.py`'s `turn_done`
        handling, `turn == 1`, from a worker thread): generate a short
        title via the small model (falling back to a heuristic on any
        failure) and store it, unless one is already set (`/rename` beat
        it, or this already ran). Returns the title actually stored, or
        None if one was already present / there's no first user turn yet."""
        if self.get_title():
            return None
        first_user = ""
        for node in self.session.log.nodes():
            if node.get("type") == "user":
                first_user = "".join(b.get("text", "") for b in node.get("content") or []
                                      if isinstance(b, dict) and b.get("type") == "text")
                break
        if not first_user.strip():
            return None
        title = _generate_title(self.session, first_user)
        self.rename_session(title)
        return title

    def fork_session(self) -> str:
        """Copy the current session's log into a brand-new session id
        under the same project slug (a real, independent file -- a
        `SessionLog` is append-only, so forking is a copy, never a
        symlink/shared-tail scheme) and switch this Controller onto it.
        Returns the new session id; the conversation continues unchanged,
        now diverging independently of the original.

        H11b finding 8: a live `cc:` claude subprocess (still tied to the
        OLD conversation id, now copied verbatim into the new log's own
        `cc_session_id` meta node) is closed here -- otherwise this
        Session would just keep talking to that SAME old process/
        conversation, never actually diverging on the claude side at all,
        and the NEXT restart (Esc/crash) would `--resume` an id that was
        never independently created. `_cc_fork_session` tells `ensure_cc_
        state`'s next start to use `--resume <that id> --fork-session`
        (claude's own "branch into a new conversation" flag) instead of a
        plain resume, which would just continue the SAME shared
        conversation the original session might still be using."""
        from halo_harness.agent.log import SessionLog

        new_id = uuid.uuid4().hex
        new_log = SessionLog(self.cwd, session_id=new_id)
        for node in self.session.log.nodes():
            new_log._append({k: v for k, v in node.items() if k != "seq"})
        self.session.log = new_log
        if getattr(self.session, "_cc_state", None) is not None:
            self.session.close_cc()
            self.session._cc_state = None
            self.session._cc_fork_session = True
        return new_id

    def export_session(self, *, sanitize: bool = False, path: Optional[str] = None) -> str:
        """Render the current session as Markdown (`/export [--sanitize]
        [file]`). Writes to `path` when given, else
        `<state_dir>/exports/<session_id>.md`. Returns the path written.
        `sanitize=True` redacts text shaped like a bearer token/API key/
        password assignment -- best-effort pattern matching, not a
        substitute for care about what gets exported."""
        log = self.session.log
        text = render_transcript_markdown(log.nodes(), session_id=log.session_id)
        if sanitize:
            text = sanitize_transcript(text)
        out_path = Path(path) if path else (self.state_dir / "exports" / f"{log.session_id}.md")
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(text, encoding="utf-8")
        return str(out_path)

    def session_stats(self) -> dict:
        """`/stats`: tokens/cost per model + tool-call counts for the
        CURRENT session's log."""
        return compute_session_stats(self.session.log.nodes())

    # ---- U5 scope B: git-shadow rewind ---------------------------------

    def shadow_steps(self) -> list:
        from halo_harness.shadow import store_for_controller
        store = store_for_controller(self)
        return store.list_steps() if store is not None else []

    def rewind_preview_undo(self) -> Optional[dict]:
        from halo_harness.shadow import store_for_controller
        store = store_for_controller(self)
        return store.preview_undo() if store is not None else None

    def rewind_preview_redo(self) -> Optional[dict]:
        from halo_harness.shadow import store_for_controller
        store = store_for_controller(self)
        return store.preview_redo() if store is not None else None

    def rewind_apply(self, step_id: str, *, verb: str = "rewind") -> Optional[dict]:
        """Actually restore the working tree to `step_id` and log a
        `rewind` node (`agent/log.py`'s own minimal append helper).
        Returns `{"step", "files"}` (`ShadowStore.rewind_to`'s own shape),
        or None if `step_id` matches no recorded step."""
        from halo_harness.shadow import store_for_controller
        store = store_for_controller(self)
        if store is None:
            return None
        result = store.rewind_to(step_id)
        if result is None:
            return None
        try:
            self.session.log.append_rewind(verb=verb, step_id=step_id, files=result.get("files") or [])
        except Exception:
            pass
        return result


def _messages_from_nodes(nodes: list) -> list:
    """The renderable subset of a session log: `{role, text}` for user and
    assistant text (tool calls/results are dropped -- a replay is a
    transcript, not a tool-by-tool reconstruction)."""
    out: list = []
    for node in nodes:
        ntype = node.get("type")
        if ntype == "user":
            text = "".join(b.get("text", "") for b in node.get("content") or []
                           if isinstance(b, dict) and b.get("type") == "text")
            if text:
                out.append({"role": "user", "text": text})
        elif ntype == "assistant":
            text = "".join(b.get("text", "") for b in node.get("content") or []
                           if isinstance(b, dict) and b.get("type") == "text")
            if text.strip():
                out.append({"role": "assistant", "text": text})
    return out


# ============================================================================
# U5 scope C: session titles, export, stats -- pure helpers over a session
# log's own node list, kept module-level (no `self`) so they're directly
# unit-testable against a plain list of dicts, no Session/Controller needed.
# ============================================================================

def _heuristic_title(text: str) -> str:
    """A short title with no model call: the first ~6 words of the first
    user message. Always available, used as `_generate_title`'s fallback
    and its own return value when there's no `session` to ask."""
    title = " ".join(text.strip().split()[:6])
    return title[:60] if title else "Untitled session"


def _clean_title(raw: str) -> str:
    """The small model's raw reply -> a bare title: first line only,
    surrounding quotes/punctuation/whitespace stripped, capped at 60
    chars."""
    first_line = (raw or "").strip().splitlines()[0].strip() if (raw or "").strip() else ""
    return first_line.strip(" \t\"'.,;:-")[:60]


def _generate_title(session, first_user_text: str) -> str:
    """The small model's own one-line title for `first_user_text`,
    falling back to `_heuristic_title` on any failure (no small model
    configured, a network error, an empty/unusable reply, ...) -- title
    generation must never be able to fail a session or block on a dead
    provider. `session._call_model_for_hook` (agent/loop.py) already
    implements "one-shot small-model call, never logged" for hooks; reused
    here with a title-specific prompt rather than duplicating the request-
    building plumbing."""
    heuristic = _heuristic_title(first_user_text)
    caller = getattr(session, "_call_model_for_hook", None)
    if not callable(caller):
        return heuristic
    try:
        raw = caller(
            "Reply with ONLY a short 3-6 word title (no quotes, no trailing punctuation) "
            f"summarizing this conversation's request:\n\n{first_user_text[:2000]}",
            4.0,
        )
    except Exception:
        return heuristic
    cleaned = _clean_title(raw)
    return cleaned or heuristic


def render_transcript_markdown(nodes: list, *, session_id: str) -> str:
    """`/export`'s own Markdown rendering of a session log's node list --
    user/assistant text, tool calls (as a fenced JSON block of their
    input), tool results, and rewind markers, in log order."""
    lines = [f"# halo session {session_id}", ""]
    for node in nodes:
        ntype = node.get("type")
        if ntype == "user":
            text = "".join(b.get("text", "") for b in node.get("content") or []
                           if isinstance(b, dict) and b.get("type") == "text")
            if text:
                lines.append(f"## User\n\n{text}\n")
        elif ntype == "assistant":
            blocks = node.get("content") or []
            text = "".join(b.get("text", "") for b in blocks if isinstance(b, dict) and b.get("type") == "text")
            if text.strip():
                lines.append(f"## Assistant\n\n{text}\n")
            for call in blocks:
                if isinstance(call, dict) and call.get("type") == "tool_use":
                    body = json.dumps(call.get("input", {}), indent=2, default=str, ensure_ascii=False)
                    lines.append(f"### Tool call: {call.get('name', '?')}\n\n```json\n{body}\n```\n")
        elif ntype == "tool_result":
            content = node.get("content")
            text = content if isinstance(content, str) else json.dumps(content, default=str, ensure_ascii=False)
            label = " (error)" if node.get("is_error") else ""
            lines.append(f"### Tool result{label}\n\n```\n{text[:4000]}\n```\n")
        elif ntype == "rewind":
            lines.append(f"### Rewind ({node.get('verb')}) to step {node.get('step_id')}\n")
    return "\n".join(lines)


def sanitize_transcript(text: str) -> str:
    """`/export --sanitize`'s own redaction -- best-effort, not a security
    boundary (see `export_session`'s own note).

    H9 whole-tree review finding 4: this used to be a SEPARATE regex
    (`\\b((?:api|secret|access)?[-_]?(?:key|token|password...)\\s*[:=]\\s*)`)
    from `halo export --sanitize`'s own -- and a broken one: it
    requires a `\\b` word boundary immediately before the literal "key"/
    "token"/etc, but a namespaced name like `OPENROUTER_API_KEY` has NO
    such boundary there (`_` and the following letter are both `\\w`, so
    `\\b` can never fire between them) -- verified, this regex matched a
    bare `token=...`/`api_key: ...` but missed `OPENROUTER_API_KEY=...`
    entirely (5 of 6 verified leak cases; only `Bearer` was ever caught).
    Delegates to `export_cli.sanitize_text` now -- the ONE sanitizer both
    `/export --sanitize` (this function) and `halo export
    --sanitize` (export_cli.cmd_export) use, so a fix to one is a fix to
    both."""
    from halo_harness.export_cli import sanitize_text
    return sanitize_text(text)


# H9 whole-tree review finding 29: a "user" log node carrying one of these
# `kind` tags (agent/log.py's own `append_user(kind=...)`) is NOT a genuine
# new user prompt -- a background sub-agent/job completion notice, a
# Stop-hook continuation, a steer's own text, or a compaction's own
# summary/re-appended tail. Untagged (every node from before this tagging
# existed, AND the one real case: `Session.turn()`'s own new-prompt
# append) counts as a real turn -- old session logs keep reporting exactly
# what they always did; only NEW logs get the more accurate count.
_NON_PROMPT_USER_KINDS = frozenset({
    "compaction_summary", "compaction_tail", "continuation", "agent_notice", "job_notice", "steer",
})


def format_cache_tokens_suffix(bucket: dict) -> str:
    """H9 whole-tree review finding 29: a short ", Ncr/Mcw cache" suffix
    for a stats display line, empty when a route never reported either
    field (Databricks, most non-Claude models) -- shared by the TUI's own
    `/stats` (commands/builtins.py) and the headless `stats_cli.py` so
    both surfaces show cache tokens identically, once, from one place."""
    cache_read = bucket.get("cache_read_input_tokens", 0)
    cache_write = bucket.get("cache_creation_input_tokens", 0)
    if not cache_read and not cache_write:
        return ""
    return f", {cache_read}cr/{cache_write}cw cache tok"


def compute_session_stats(nodes: list) -> dict:
    """`/stats`: `{turns, total_cost_usd, per_model: {model: {input_
    tokens, output_tokens, cache_read_input_tokens, cache_creation_
    input_tokens, cost_usd, calls}}, tool_counts: {name: n}}`."""
    per_model: dict = {}
    tool_counts: dict = {}
    total_cost = 0.0
    turns = 0
    current_model = "?"
    for node in nodes:
        ntype = node.get("type")
        if ntype == "meta" and node.get("model"):
            current_model = node["model"]
        elif ntype == "user":
            # finding 29: count real prompts only -- see _NON_PROMPT_USER_KINDS.
            if node.get("kind") not in _NON_PROMPT_USER_KINDS:
                turns += 1
        elif ntype == "assistant":
            for block in node.get("content") or []:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    name = block.get("name", "?")
                    tool_counts[name] = tool_counts.get(name, 0) + 1
        elif ntype == "usage":
            bucket = per_model.setdefault(current_model, {
                "input_tokens": 0, "output_tokens": 0,
                # finding 29: a cached Claude route's own `message_start`
                # reports ONLY the uncached portion in `input_tokens` (see
                # agent/loop.py's own H5b finding 4 comment on this exact
                # split) -- omitting these two under-reported a heavily
                # cached session's real input by however much was served
                # from cache.
                "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0,
                "cost_usd": 0.0, "calls": 0,
            })
            usage = node.get("usage") or {}
            bucket["input_tokens"] += int(usage.get("input_tokens") or 0)
            bucket["output_tokens"] += int(usage.get("output_tokens") or 0)
            bucket["cache_read_input_tokens"] += int(usage.get("cache_read_input_tokens") or 0)
            bucket["cache_creation_input_tokens"] += int(usage.get("cache_creation_input_tokens") or 0)
            cost = node.get("cost_usd")
            if isinstance(cost, (int, float)):
                bucket["cost_usd"] += cost
                total_cost += cost
            bucket["calls"] += 1
    return {"turns": turns, "total_cost_usd": total_cost, "per_model": per_model, "tool_counts": tool_counts}
