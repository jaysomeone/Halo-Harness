"""rolo_claude.controller -- the non-blocking UI-thread facade over the
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
import time
from pathlib import Path
from typing import Callable, Optional

from rolo_claude import events
from rolo_claude.config.paths import bridge_home, project_slug
from rolo_claude.permissions import Decision, add_allow_rule

QUIT_DEADLINE_S = 5.0


def _default_model_resolver(raw: str, *, state_dir: Path, routes: dict, settings=None):
    """`(ModelRef, ModelProfile, creds)` for a `--model`-style string --
    the same resolution headless.py does at startup, reused here so `/model`
    and `set_model` can never drift from it."""
    from rolo_claude.headless import _resolve_creds
    from rolo_claude.model import parse_model_ref, resolve_model_profile

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

        self._worker = threading.Thread(target=_target, name="rolo-claude-session", daemon=True)
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
        turn's generator and can't read `self.commands`)."""
        if self.session.busy:
            self.session.steer(text)
            return
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

    def answer_question(self, request_id: str, answer) -> bool:
        return self.session.resolve_question(request_id, answer)

    def answer_plan(self, approved: bool, *, feedback: str = "", mode_after: Optional[str] = None) -> None:
        """`PlanCard`'s reply (D-Contract `plan_reply{approved, feedback,
        mode_after}`). Queued, not a direct threading.Event resolve like
        `answer_permission`/`answer_question` -- plan mode isn't wired to
        block the worker thread yet (H6); the command pump's `plan_reply`
        branch is currently a no-op, so this is forward-wiring against the
        `plan_review` event the card already renders."""
        self.commands.put(events.Command("plan_reply", {
            "approved": approved, "feedback": feedback, "mode_after": mode_after,
        }))

    def list_mcp_servers(self) -> list:
        """One dict per configured server (`McpManager.status()`'s own
        shape) for the `/mcp` dialog -- `[]` when no manager was built this
        session (`--bare`, or MCP failed to start)."""
        if self.mcp_manager is None:
            return []
        try:
            return self.mcp_manager.status()
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

    def run_slash(self, name: str, args: str = "") -> str:
        """Resolve `name` through the U0 registry.

        * a `prompt`-kind command (custom command / skill) is expanded and
          submitted as the next user turn -- text comes back "" and the
          caller should not print anything;
        * anything else runs through the TUI facade right here (it is local
          and fast) and its returned text is handed back as the result.
        """
        if self.registry is None:
            return f"No command registry is loaded: /{name}"
        cmd = self.registry.resolve(name)
        if cmd is None:
            return f"Unknown command: /{name} (try /help)"
        if cmd.kind == "prompt" and cmd.run is not None:
            from rolo_claude.commands.registry import expand_command_body, read_at_mention_snapshots

            body = cmd.run(args, self.facade)
            result = expand_command_body(body, args, allowed_tools=getattr(cmd, "allowed_tools", None), cwd=self.cwd)
            if result.error:
                return result.error
            # H4 scope C: `@path` attachments in the EXPANDED body -- read
            # via the Read tool's own path resolution and appended as
            # snapshots (never inlined into the prompt text itself, same
            # as CLAUDE.md's own @import convention).
            for path_str, content in read_at_mention_snapshots(result.text, cwd=self.cwd):
                self.session.log.append_snapshot(
                    [{"type": "text", "text": f"@{path_str}\n{content}"}], kind="at_mention")
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
        """`[{ref, context, output, price_in, price_out, provider}, ...]`
        for the ModelPicker -- from the same models.json + routes.json
        aliases the resolver uses, so a pick is guaranteed to resolve."""
        from rolo_claude.providers.databricks import load_models_json

        out: list = []
        seen = set()
        try:
            models = load_models_json(self.state_dir) or {}
        except Exception:
            models = {}
        for name in sorted(models):
            entry = models.get(name) or {}
            ref = f"or:{name}"
            seen.add(ref)
            out.append({
                "ref": ref, "context": entry.get("context_length") or 128000,
                "output": entry.get("max_output_tokens") or 16384,
                "price_in": (entry.get("pricing") or {}).get("prompt"),
                "price_out": (entry.get("pricing") or {}).get("completion"),
                "provider": "openrouter",
            })
        for alias, target in sorted((self.routes.get("aliases") or {}).items()):
            if alias not in seen:
                out.append({"ref": alias, "context": None, "output": None, "price_in": None,
                            "price_out": None, "provider": "alias", "target": target})
        current = self.session.model_ref.raw
        if current and current not in {m["ref"] for m in out}:
            out.insert(0, {"ref": current, "context": self.session.model_profile.context_tokens,
                           "output": self.session.model_profile.max_output_tokens,
                           "price_in": None, "price_out": None, "provider": self.session.model_ref.provider})
        return out

    def list_sessions(self) -> list:
        """`[{id, cwd, mtime, summary}, ...]`, newest first, for the
        `--resume` picker. Reads OUR session logs only (~/.rolo-claude)."""
        slug = project_slug(self.cwd)
        directory = self.state_dir / "sessions" / slug
        out: list = []
        if not directory.is_dir():
            return out
        for path in sorted(directory.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True):
            summary = ""
            try:
                for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
                    try:
                        node = json.loads(line)
                    except ValueError:
                        continue
                    if node.get("type") == "user":
                        for block in node.get("content") or []:
                            if isinstance(block, dict) and block.get("type") == "text":
                                summary = block.get("text", "")[:80]
                                break
                    if summary:
                        break
            except OSError:
                pass
            out.append({"id": path.stem, "cwd": str(self.cwd), "mtime": path.stat().st_mtime, "summary": summary})
        return out

    def resume(self, session_id: str) -> None:
        """Read a prior session's log and push ONE `replay` event carrying
        its renderable messages. The next prompt continues THAT session:
        the Session's own log is reopened on it (see
        `rolo_claude.tui.app` for the swap)."""
        from rolo_claude.agent.log import SessionLog

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
        from rolo_claude import mcp_setup
        mcp_setup.record_mcp_approval(name, raw_entry)
        handle.config.pending_approval = False
        return self.reconnect_mcp(name, abort=abort)

    def memory_path(self):
        from rolo_claude.config.paths import memory_dir
        return memory_dir(str(self.cwd))


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
