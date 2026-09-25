"""rolo_claude.headless -- print-mode session driver (U0 scope F rewrite).
Builds a Session and drives one turn (`--input-format text`, the default)
or several (`--input-format stream-json`: one already-parsed JSON object on
stdin per line = one user turn -- cli.py reads/parses stdin, since text-
input's own stdin-prompt read and stream-json's line read are mutually
exclusive ways of consuming the SAME stream) through it, printing via
`output.PrintModeSink` (text/json) or `output.StreamJsonSink` (stream-json).
A leading `/slash-command` resolves and runs through the commands registry's
headless facade BEFORE ever touching a model for a "core"/"ui" kind command;
a "prompt" kind command (a custom command or skill) expands into the turn's
actual prompt text instead.
"""

from __future__ import annotations

import atexit
import collections
import json
import os
import re
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from rolo_claude import events
from rolo_claude.agent import sessions as agent_sessions
from rolo_claude.agent.assemble import SessionContext
from rolo_claude.agent.catalog import SessionCatalog, host_cap, select_preload
from rolo_claude.agent.log import SessionLog
from rolo_claude.agent.loop import Session
from rolo_claude.agent.planmode import ensure_plan_file
from rolo_claude.commands.builtins import HeadlessFacade
from rolo_claude.commands.registry import Registry
from rolo_claude.config.agents_md import discover_agents
from rolo_claude.config.claude_json import is_trusted, load_claude_json
from rolo_claude.config.paths import bridge_home, home, lookup_project
from rolo_claude.config.settings import resolve_settings
from rolo_claude.model import DEFAULT_MODEL_REF, parse_model_ref, resolve_model_profile
from rolo_claude.output import PrintModeSink, StreamJsonSink
from rolo_claude.permissions import (
    PermissionEngine, bare_deny_tool_names, build_rules_from_settings, freeze_tool_registry,
    mcp_deny_tool_names, normalize_permission_mode, split_tool_rule_list,
)
from rolo_claude.providers.config import (
    derive_workspace_root, load_env_file, load_routes, resolve_anthropic, resolve_databricks, resolve_openrouter,
)
from rolo_claude.providers.profiles import model_family
from rolo_claude.providers.stream import ProviderCreds
from rolo_claude.theme import load_config as load_rolo_config, resolve_theme
from rolo_claude.tools.registry import ToolRegistry

_SLASH_RE = re.compile(r"^/(\S+)(?:\s+(.*))?$", re.DOTALL)


def _resolve_creds(ref, settings=None) -> Optional[ProviderCreds]:
    """must-do 6: `settings.effective_env` (shell < user < trusted
    project/local < flag < policy, resolved with the session's real
    trust/cwd) is what actually gates OPENROUTER_API_KEY/DATABRICKS_HOST/
    DATABRICKS_TOKEN etc. -- `settings=None` falls back to bare
    `os.environ`."""
    env = settings.effective_env if settings is not None else None
    if ref.provider == "openrouter":
        orc = resolve_openrouter(env)
        if orc is None:
            return None
        return ProviderCreds(base_url=orc.base_url, api_key=orc.api_key)
    if ref.provider == "databricks":
        dbx = resolve_databricks(env)
        if dbx is None:
            return None
        return ProviderCreds(base_url=derive_workspace_root(dbx.host), api_key=dbx.token)
    if ref.provider == "anthropic":
        # H5 scope C: `ant:` goes through stream_anthropic_completion (the
        # native-dialect sibling of stream_completion), not the openai-chat
        # path this function's name suggests -- but ProviderCreds is the
        # same plain (base_url, api_key) shape either way.
        ant = resolve_anthropic(env)
        if ant is None:
            return None
        return ProviderCreds(base_url=ant.base_url, api_key=ant.api_key)
    return None


def build_hook_runner(*, settings, cwd: Path, session_id: str, transcript_path: str,
                       effort: Optional[str], permission_mode: str, mcp_manager, bare: bool):
    """Shared by `run_print_mode` and `tui/bootstrap.py` (imported from
    there, same reuse pattern as `_resolve_creds`) -- one `HookRunner` per
    session, built from `settings.hooks` (already trust-filtered by
    `config/settings.py`'s own merge) + every enabled plugin's own
    `hooks/hooks.json`. `--bare`/`disableAllHooks` disable the runner
    outright (`enabled=False`), so a caller never has to special-case an
    empty result -- every `HookRunner.has_hooks(...)` call site is then
    simply always False. `prompt_caller` is left unbound here (the CALLER
    binds it to the just-constructed Session's own `_call_model_for_hook`,
    once one exists -- see the call site)."""
    from rolo_claude.config.plugins import load_installed_plugins, _plugin_roots
    from rolo_claude.hooks import HookRunner, load_plugin_hooks, merge_hook_maps, normalize_hooks

    disabled = bare or bool(settings is not None and getattr(settings, "disable_all_hooks", False))
    hooks_by_event: dict = {}
    if not disabled:
        if settings is not None:
            hooks_by_event = normalize_hooks(settings.hooks or {}, default_source="settings")
        # finding 8: `settings.raw` (the `enabledPlugins` gate) and `cwd`
        # (project/local-scoped V2 records) are now threaded through here
        # too -- this call used to use NEITHER, so a plugin the user had
        # explicitly DISABLED via `enabledPlugins` still had its
        # `hooks/hooks.json` loaded and run (the MCP-server discovery path
        # already respected the gate; this one, using the SAME
        # `_plugin_roots`, silently didn't).
        settings_raw = getattr(settings, "raw", None) if settings is not None else None
        manifest = load_installed_plugins()
        for _plugin_name, plugin_root in _plugin_roots(manifest, settings_raw, cwd=cwd):
            hooks_by_event = merge_hook_maps(hooks_by_event, load_plugin_hooks(plugin_root))
    return HookRunner(
        hooks_by_event, cwd=cwd, session_id=session_id, transcript_path=transcript_path,
        effective_env=(settings.effective_env if settings is not None else None),
        effort=effort, permission_mode=permission_mode, mcp_manager=mcp_manager, enabled=not disabled,
    )


class _TurnLineQueue:
    """H5c finding 13: a `--input-format stream-json` turn queue that lets
    a LEFTOVER steer (`Session.turn()`'s own `finally` -- a steer accepted
    after the turn's last internal checkpoint, e.g. during a slow Stop
    hook) be re-queued AHEAD of whatever the stdin reader thread already
    pushed, including its terminal `None` EOF sentinel. A plain FIFO
    `queue.Queue` cannot do this: verified with the CLI -- a line written
    during turn 1's 3s Stop hook, followed by closing stdin, never ran
    (exit 0, a single result, none of the requests carried the line). The
    reader thread's `None` (pushed the moment stdin closes, independent of
    whether the CURRENT turn has finished yet) can land in the queue
    BEFORE the main loop gets a chance to re-queue that turn's own
    leftover once it finishes -- FIFO then reads `None` first and stops,
    leaving the leftover sitting unread right behind it. `put_urgent`
    (`appendleft`) is what the leftover re-queue uses instead of a plain
    `put` (`append`), so it is always read before an already-queued `None`
    -- or before any other already-queued line, for that matter."""

    def __init__(self) -> None:
        self._deque = collections.deque()
        self._cond = threading.Condition()

    def put(self, item) -> None:
        with self._cond:
            self._deque.append(item)
            self._cond.notify()

    def put_urgent(self, item) -> None:
        """Ahead of everything already queued, including a `None` EOF
        sentinel a concurrent reader thread already pushed."""
        with self._cond:
            self._deque.appendleft(item)
            self._cond.notify()

    def get(self):
        with self._cond:
            while not self._deque:
                self._cond.wait()
            return self._deque.popleft()


def _stream_json_stdin_reader(session, out_queue) -> None:
    """finding 10 (major, h4-h5-h3c review): runs on its OWN thread for
    the whole `--input-format stream-json` run -- reads stdin LINE BY
    LINE (never all of it up front, which used to deadlock an SDK-style
    client that writes one line and waits for that turn's `result` before
    writing the next one) and, for each line that resolves to real user
    text: if a turn is CURRENTLY running (`session.busy`), applies it as a
    steer via `Session.steer` directly (thread-safe by design -- see its
    own docstring); otherwise pushes it onto `out_queue` as the next
    turn's prompt. `session.steer`'s own atomic busy-check+enqueue (finding
    5) is what makes the `session.busy` read here safe despite being a
    plain, unlocked check from a second thread: if the turn finishes in
    the tiny window between that read and the `steer()` call itself,
    `steer()` correctly returns False and this falls through to queuing a
    fresh turn instead of losing the text. `None` on `out_queue` marks
    stdin EOF (no more turns will ever start)."""
    try:
        while True:
            raw_line = sys.stdin.readline()
            if raw_line == "":
                return  # EOF
            line = raw_line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            if not isinstance(obj, dict):
                continue
            text = _extract_user_text(obj)
            if not text:
                continue
            if session.busy and session.steer(text):
                continue
            out_queue.put(text)
    finally:
        out_queue.put(None)


def _extract_user_text(obj: dict) -> str:
    """One `--input-format stream-json` line's user text, whether
    `content` is a plain string or an Anthropic-shaped block list."""
    message = obj.get("message") if isinstance(obj.get("message"), dict) else obj
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")
    return ""


def _maybe_run_slash_command(prompt_text: str, *, registry, facade, disable_slash_commands: bool):
    """`(final_prompt_or_None, direct_output_or_None)` -- exactly one is
    non-None when `prompt_text` resolves to a known command; both None
    when it isn't one (sent through unchanged)."""
    if disable_slash_commands or not prompt_text.startswith("/"):
        return None, None
    m = _SLASH_RE.match(prompt_text)
    if not m:
        return None, None
    cmd = registry.resolve(m.group(1))
    if cmd is None or cmd.run is None:
        return None, None
    output = cmd.run(m.group(2) or "", facade)
    return (output, None) if cmd.kind == "prompt" else (None, output)


def _append_at_mention_snapshots(session, text: Optional[str], cwd: Path) -> None:
    """H4 scope C: a slash-invoked command/skill's EXPANDED body's `@path`
    mentions, read via the Read tool's own path resolution and appended
    as snapshots BEFORE the turn that will carry `text` -- a no-op when
    `text` is None (an ordinary, non-slash prompt was never expanded)."""
    if not text:
        return
    from rolo_claude.commands.registry import read_at_mention_snapshots
    for path_str, content in read_at_mention_snapshots(text, cwd=cwd):
        session.log.append_snapshot([{"type": "text", "text": f"@{path_str}\n{content}"}], kind="at_mention")
    # H8 scope E (deferred by H3): `@server:resource` mentions, the MCP
    # sibling of the `@path` handling just above -- same "separate context
    # block, never inlined" rule.
    from rolo_claude.mcp.mentions import read_server_resource_snapshots
    for label, content in read_server_resource_snapshots(text, mcp_manager=session.mcp_manager):
        session.log.append_snapshot([{"type": "text", "text": f"@{label}\n{content}"}], kind="at_mention")


def _resolve_local_file_spec(raw: str, *, cwd: Path) -> "Optional[Path]":
    """A `--file` SPEC that is (or resolves to) a real local file, or
    `None`. Tried BEFORE the `file_id:relative_path` cloud-resource form
    (see `attach_cli_files`) specifically so a Windows absolute path like
    `C:\\Users\\rolo\\shot.png` -- which also contains a `:` -- is never
    misread as that form: an actual local file always wins."""
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = cwd / path
    try:
        resolved = path.resolve()
    except OSError:
        return None
    return resolved if resolved.is_file() else None


def attach_cli_files(session, file_specs: Optional[list], *, cwd: Path) -> None:
    """`--file SPEC [SPEC ...]` (H8 scope B). Claude Code's own `--file`
    downloads a claude.ai-hosted "file resource" at startup (SPEC format
    `file_id:relative_path`, e.g. `file_abc:doc.txt`) -- a cloud-account
    feature this standalone, bring-your-own-model harness has no backing
    store for, so that form gets one clear stderr notice per spec rather
    than either faking success or crashing. The practically useful case --
    a local path -- IS fully supported and is what `@path` image mentions
    in the TUI use too: read via the SAME Read tool a model-issued Read
    call would use (an image becomes a real `image` content block when the
    session's model advertises vision, same `tools/imageutil.py` gate
    every other image source in this harness shares; anything else becomes
    a text snapshot), appended as a log snapshot BEFORE the session's
    first turn -- so the file is context the model already has, not
    something it has to go looking for. Shared by both entry points
    (`run_print_mode` below and `tui/bootstrap.py`'s `build_controller`),
    called exactly once per process."""
    if not file_specs:
        return
    from rolo_claude.tools.base import ToolContext
    from rolo_claude.tools.read import ReadTool
    tool = ReadTool()
    model_profile = getattr(session, "model_profile", None)
    ctx = ToolContext(cwd=cwd, vision=getattr(model_profile, "vision", False))
    for raw in file_specs:
        resolved = _resolve_local_file_spec(raw, cwd=cwd)
        if resolved is None and ":" in raw:
            file_id, _, rel_path = raw.partition(":")
            print(f"rolo-claude: --file: {raw!r} looks like Claude Code's "
                  f"file_id:relative_path cloud-resource form ({file_id!r} -> {rel_path!r}) -- "
                  f"rolo-claude has no claude.ai-hosted file store to download it from, skipped. "
                  f"A local path (relative to --cwd, or absolute) attaches directly.", file=sys.stderr)
            continue
        if resolved is None:
            print(f"rolo-claude: --file: {raw}: not a file, skipped", file=sys.stderr)
            continue
        try:
            result = tool.run({"file_path": str(resolved)}, ctx)
        except Exception as e:
            print(f"rolo-claude: --file: {raw}: {e}", file=sys.stderr)
            continue
        if isinstance(result.content, list):
            blocks = [{"type": "text", "text": f"@{raw}"}] + list(result.content)
        else:
            content = result.content if isinstance(result.content, str) else str(result.content)
            blocks = [{"type": "text", "text": f"@{raw}\n{content}"}]
        try:
            session.log.append_snapshot(blocks, kind="at_mention")
        except Exception:
            pass


def _append_agent_mention_snapshot(session, text: Optional[str], agents: Optional[dict]) -> None:
    """H6 scope A: `@agent-<name>` in the RAW prompt text (not just an
    expanded slash body -- unlike `_append_at_mention_snapshots`, this
    runs on whatever text the turn is actually about to carry) forces that
    sub-agent (brief: "@agent-<name> in a prompt forces one") by telling
    the model to use it, as a snapshot so it survives compaction the same
    way every other dynamic instruction does."""
    if not text or not agents:
        return
    from rolo_claude.config.agents_md import agent_mention_instruction, find_agent_mentions
    names = find_agent_mentions(text, agents)
    if names:
        session.log.append_snapshot([{"type": "text", "text": agent_mention_instruction(names)}],
                                     kind="agent_mention")


def _events_for_direct_output(output: str, *, turn_no: int = 1):
    """A minimal, well-formed event sequence carrying `output` as the
    turn's whole reply -- lets a slash command's direct text run through
    the SAME sinks (text/json/stream-json) a real model turn does, instead
    of every output format needing its own special case."""
    yield events.user_message("(slash command)", turn=turn_no)
    yield events.message_start(turn=turn_no)
    yield events.text_delta(output, turn=turn_no)
    yield events.message_end(turn=turn_no, stop_reason="end_turn", usage={})
    yield events.turn_done(turn=turn_no, reason="end_turn")


_BACKGROUND_JOB_WAIT_S = 120.0  # matches Bash's own default foreground timeout


def _drain_background_jobs_for_print_mode(session, sink) -> int:
    """H8 scope A must-do (acceptance: "line count first, completion notice
    later"): dsh's "background jobs ... report completion as a user-role
    notice in the next step" has no NEXT step in a single `-p` text-input
    call -- the process is about to exit right after this turn. Rather than
    spend a whole extra model call just to announce it (token thrift -- see
    MEMORY.md), wait (bounded) for any Bash job still running when the
    visible turn ended, and fold each completion notice into the SAME sink
    that will print the real turn's own (one and only) result. Genuinely
    long-running jobs outlive the bound and are simply killed on the way
    out (`finally: session.job_registry.kill_all()`) like any other
    background job the session never got back to -- this never turns `-p`
    into an unbounded hang.

    H9 whole-tree review finding 9: this used to call `sink.consume(...)`
    -- a full `finish()` cycle -- once per notice, ON TOP OF the real
    turn's own already-`finish()`-ed `consume()` call. For `--output-format
    json` that meant TWO (or more) complete JSON objects on one stdout
    stream (`json.loads(stdout)` on the real caller's side raised "Extra
    data"), and the LAST notice's own always-`end_turn`/never-`is_error`
    result silently overwrote a genuinely FAILED real turn's exit code with
    0. stream-json gained an extra, unsolicited `result` line the same way.
    Callers now pass `finish=False` to their own `sink.consume(session.
    turn(...))` call so `sink.finish()` -- called exactly ONCE, here, for
    the whole process -- is the only thing that ever prints a result
    object, background notices included (see Sink.add_background_notice)."""
    registry = getattr(session, "job_registry", None)
    if registry is not None and registry.jobs:
        def _drain_once() -> None:
            with session._job_notices_lock:
                notices, session._pending_job_notices = session._pending_job_notices, []
            for text in notices:
                sink.add_background_notice(text)

        deadline = time.monotonic() + _BACKGROUND_JOB_WAIT_S
        while any(rec.status == "running" for rec in registry.jobs.values()) and time.monotonic() < deadline:
            time.sleep(0.2)
            _drain_once()
        _drain_once()  # a completion landed between the loop's last check and now
    return sink.finish()


@dataclass
class SessionBuild:
    """u2-h3b finding 9: everything `-p` (`run_print_mode` below) and the
    TUI (`tui/bootstrap.build_controller`) build IDENTICALLY, up through a
    ready-to-drive `Session` -- one function, `build_session`, instead of
    two hand-maintained copies that had already drifted (the TUI's copy
    was missing server-level `alwaysLoad` and the "keep ToolSearch when
    deferred tools exist" rule -- see `build_session`'s own docstring).
    Each caller then does its OWN tail: `run_print_mode` drives `session.
    turn()` through a sink; `build_controller` wraps `session` in a
    `Controller` and wires the TUI's own `/mcp` status+reconnect
    closures."""
    session: object                 # agent.loop.Session
    command_registry: object        # commands.registry.Registry (slash commands)
    tool_registry: object           # tools.registry.ToolRegistry (frozen, growable via ToolSearch)
    facade: object                  # commands.builtins.HeadlessFacade
    mcp_manager: Optional[object]
    mcp_notices: list
    session_catalog: Optional[object]
    settings: object
    claude_json: dict
    resolved_mode: str
    routes: dict
    state_dir: Path
    model_ref: object
    model_profile: object
    creds: Optional[object]
    ctx: object                     # agent.assemble.SessionContext
    session_log: object
    hook_runner: Optional[object]
    resume_error: Optional[str] = None   # H6 scope D: --continue/--resume didn't resolve to a session
    agents: "dict" = None                # H6 scope A: the discovered AgentSpec catalog (for /agents)


def build_session(
    *, cwd: Path, model_ref_raw: Optional[str] = None, small_model_ref_raw: Optional[str] = None,
    settings_flag: Optional[str] = None, setting_sources: Optional[list] = None,
    effort: Optional[str] = None, allowed_tools: Optional[str] = None, disallowed_tools: Optional[str] = None,
    permission_mode: Optional[str] = None, dangerously_skip_permissions: bool = False,
    tools: Optional[str] = None, add_dir: Optional[list] = None, bare: bool = False,
    session_id: Optional[str] = None, max_turns: int = 50,
    append_system_prompt: Optional[str] = None, system_prompt: Optional[str] = None,
    json_schema: Optional[str] = None, chrome: bool = False, no_chrome: bool = False,
    playwright: bool = False, playwright_cdp: Optional[str] = None, playwright_headless: bool = False,
    mcp_config: Optional[list] = None, strict_mcp_config: bool = False, print_mode: bool = True,
    continue_: bool = False, resume: Optional[str] = None, fork_session_flag: bool = False,
    agent: Optional[str] = None, agents_flag: Optional[str] = None,
) -> SessionBuild:
    """The ONE shared builder (finding 9). `print_mode` (True for `-p`,
    False for the TUI) is the single knob that decides `PermissionEngine.
    print_mode`, `build_manager`'s own `print_mode` (approve/spawn every
    `.mcp.json` server outright vs. respect `pending_approval`), and
    `resolve_chrome_enabled`'s `interactive` (`claudeInChromeDefaultEnabled`
    is never consulted for `-p` -- finding 14). Everything else is common.

    Fixes folded in here that the TUI's OLD hand-copy (`tui/bootstrap.py`,
    pre-finding-9) never had: server-level `alwaysLoad` (`always_load_
    servers`, below) preloads every tool from that server, and ToolSearch
    is kept in the frozen registry whenever the deferred pool is non-empty
    even if `--tools`/a deny rule would otherwise have excluded it -- a
    `--tools Read,Bash`-style TUI launch used to leave the whole deferred
    MCP pool unreachable while the prompt still said "call ToolSearch"."""
    claude_json = load_claude_json()
    trusted = is_trusted(cwd, claude_json)
    settings = resolve_settings(cwd, settings_flag=settings_flag, setting_sources=setting_sources, trusted=trusted)
    claude_json_allowed_tools = lookup_project(claude_json, cwd).get("allowedTools")

    cli_allow = split_tool_rule_list(allowed_tools) if allowed_tools else []
    cli_disallow = split_tool_rule_list(disallowed_tools) if disallowed_tools else []
    deny_rules, ask_rules, allow_rules = build_rules_from_settings(
        settings, cwd=cwd, claude_json_allowed_tools=claude_json_allowed_tools,
        cli_allow=cli_allow, cli_disallow=cli_disallow,
    )

    if dangerously_skip_permissions:
        resolved_mode = "bypassPermissions"
    elif permission_mode:
        resolved_mode = normalize_permission_mode(permission_mode)
    elif settings.permissions_default_mode:
        resolved_mode = normalize_permission_mode(settings.permissions_default_mode)
    else:
        resolved_mode = "default"

    frozen_registry = freeze_tool_registry(
        ToolRegistry(), tools_flag=tools, disallowed_tools=cli_disallow, deny_rules=deny_rules,
    )
    extra_dirs = [Path(d) for d in settings.permissions_additional_directories] + [Path(d) for d in (add_dir or [])]
    permission_engine = PermissionEngine(
        deny_rules=deny_rules, ask_rules=ask_rules, allow_rules=allow_rules, mode=resolved_mode,
        cwd=cwd, extra_dirs=extra_dirs, print_mode=print_mode,
    )
    if resolved_mode == "plan":
        # H6 scope C: `--permission-mode plan` from the very start of the
        # session (Shift+Tab/a model-initiated EnterPlanMode mid-session
        # both go through agent/loop.py's own `_handle_enter_plan_mode`
        # instead) -- the plan file is created NOW so it exists before the
        # first turn even runs, matching "created per session".
        plan_path = ensure_plan_file(cwd, settings)
        permission_engine.set_plan_file(plan_path)

    env_path = Path(os.environ.get("BRIDGE_ENV_FILE", home() / ".config" / "vibes-hacker" / "env"))
    load_env_file(env_path)

    state_dir = bridge_home()
    routes = load_routes(state_dir / "routes.json")

    model_raw = model_ref_raw or os.environ.get("BRIDGE_MODEL") or routes.get("default") or DEFAULT_MODEL_REF
    model_ref = parse_model_ref(model_raw, routes)
    small_raw = small_model_ref_raw or os.environ.get("BRIDGE_MODEL_SMALL") or routes.get("small") or model_raw
    small_ref = parse_model_ref(small_raw, routes) if small_raw else None
    model_profile = resolve_model_profile(model_ref, state_dir, routes)
    family = model_family(model_ref.model)

    # finding 7 (major, h4-h5-h3c review): WebSearch must be registered
    # BEFORE `session_catalog = SessionCatalog(..., names=frozen_registry.
    # names())` below snapshots the wire-catalog ORDER (agent/loop.py's
    # Session.__init__ uses `session_catalog.names`, never a later re-read
    # of the live registry, for the first `meta` node it logs) -- added
    # here, before the MCP block, so `frozen_registry.names()` already
    # includes it by the time that snapshot is taken. The OLD position
    # (after the MCP block) meant WebSearch was in the registry (so the
    # system prompt, built straight from `tool_registry.definitions()`,
    # still described it) but missing from the actual wire `tools` field
    # of every request -- never offered to the model at all.
    # finding 7 follow-up: WebSearch is a tool `freeze_tool_registry`
    # never saw (it's added straight to the registry after the fact), so
    # it must apply the SAME `--tools`/`--disallowedTools`/deny-rule gate
    # that call already applied to every BUILT-IN tool -- otherwise
    # `--tools Bash,Read` or `--tools ""` silently fails to restrict it
    # (verified once finding 7's own fix stopped ALSO accidentally hiding
    # WebSearch from the wire catalog for an unrelated reason -- the two
    # bugs previously masked each other for exactly this case).
    websearch_allowed = True
    if tools is not None and tools.strip() not in ("", "default") and "WebSearch" not in split_tool_rule_list(tools):
        websearch_allowed = False
    elif tools is not None and tools.strip() == "":
        websearch_allowed = False
    if "WebSearch" in bare_deny_tool_names(deny_rules) or "WebSearch" in cli_disallow:
        websearch_allowed = False

    creds = _resolve_creds(model_ref, settings)
    if (not bare and websearch_allowed and model_ref.provider == "openrouter" and creds is not None
            and frozen_registry.get("WebSearch") is None):
        from rolo_claude.tools.websearch import build_websearch_tool
        ws_tool = build_websearch_tool(
            main_provider=model_ref.provider, creds=creds,
            small_model_raw=(small_ref.model if small_ref else None), main_model_raw=model_ref.model,
        )
        if ws_tool is not None:
            frozen_registry.add_tool(ws_tool)

    mcp_manager = None
    mcp_notices: list = []
    session_catalog = None
    mcp_servers_for_prompt: list = []
    if not bare:
        from rolo_claude.mcp_setup import build_manager, resolve_chrome_enabled
        chrome_enabled = resolve_chrome_enabled(claude_json, chrome_flag=chrome, no_chrome_flag=no_chrome,
                                                 interactive=not print_mode)
        mcp_manager, mcp_notices = build_manager(
            cwd=cwd, claude_json=claude_json, settings=settings, print_mode=print_mode,
            mcp_config_flag=mcp_config, strict_mcp_config=strict_mcp_config,
            chrome=chrome_enabled, playwright=playwright,
            playwright_cdp=playwright_cdp, playwright_headless=playwright_headless,
            bypass_mode=(resolved_mode in ("auto", "bypassPermissions")), start=True, trusted=trusted,
        )
        if mcp_manager is not None:
            from rolo_claude.tools.mcp_tool import ListMcpResourcesTool, McpTool, ReadMcpResourceTool
            tools_subset = None
            if tools is not None and tools.strip() not in ("", "default"):
                tools_subset = set(split_tool_rule_list(tools))
            elif tools is not None and tools.strip() == "":
                tools_subset = set()
            bare_denied_names = set(bare_deny_tool_names(deny_rules))
            bare_denied_names |= {r.strip() for r in cli_disallow if "(" not in r}

            def _builtin_allowed(name: str) -> bool:
                if tools_subset is not None and name not in tools_subset:
                    return False
                return name not in bare_denied_names

            if _builtin_allowed("ListMcpResourcesTool"):
                frozen_registry.add_tool(ListMcpResourcesTool())
            if _builtin_allowed("ReadMcpResourceTool"):
                frozen_registry.add_tool(ReadMcpResourceTool())

            all_mcp = mcp_manager.all_tools()
            candidate_names = [t[1] for t in all_mcp]
            denied = mcp_deny_tool_names(deny_rules, candidate_names)
            denied |= {r.strip() for r in cli_disallow if "(" not in r and r.strip().startswith("mcp__")}
            survivors = [t for t in all_mcp if t[1] not in denied]

            cap = host_cap(model_ref.provider)
            cap_budget = max(0, cap - len(frozen_registry.names()))
            preload_names = set((load_rolo_config().get("mcpPreload") or []))
            # finding 13 must-do: server-level "alwaysLoad" preloads every
            # tool from that server (was missing from the TUI's own copy).
            always_load_servers = {name for name, h in mcp_manager.handles.items() if h.config.always_load}
            preload, deferred = select_preload(survivors, preload_names=preload_names,
                                                always_load_servers=always_load_servers, cap_budget=cap_budget)

            for server_name, _wire_name, sdk_tool in preload:
                frozen_registry.add_tool(McpTool(server_name, sdk_tool, mcp_manager,
                                                  vision=model_profile.vision, family=family))

            # finding 13 must-do: keep ToolSearch whenever the deferred pool
            # is non-empty, even when --tools/a deny rule would otherwise
            # have excluded it (was missing from the TUI's own copy).
            if deferred and frozen_registry.get("ToolSearch") is None:
                from rolo_claude.tools.tool_search import ToolSearchTool
                frozen_registry.add_tool(ToolSearchTool())

            session_catalog = SessionCatalog(
                registry=frozen_registry, deferred=deferred, manager=mcp_manager, cap=cap,
                vision=model_profile.vision, family=family, names=frozen_registry.names(),
            )
            mcp_servers_for_prompt = [
                {"name": name, "instructions": h.instructions}
                for name, h in mcp_manager.handles.items() if h.state == "connected"
            ]

    effective_append = append_system_prompt
    if json_schema:
        note = f"Respond with JSON matching this schema (no prose outside the JSON): {json_schema}"
        effective_append = f"{effective_append}\n\n{note}" if effective_append else note

    # H6 scope A: `--agent <name>` runs the WHOLE session as that agent --
    # resolved against the FULLY assembled catalog (built-ins + MCP +
    # WebSearch, all already added to `frozen_registry` above) so
    # `spec.resolved_tools()` can actually see everything it might keep.
    discovered_agents = {} if bare else discover_agents(cwd, settings=settings, agents_flag=agents_flag)
    agent_spec = discovered_agents.get(agent) if agent else None
    agent_type_restriction = None
    if agent_spec is not None:
        frozen_registry = frozen_registry.filtered(agent_spec.resolved_tools(frozen_registry.names()))
        agent_type_restriction = agent_spec.allowed_subagent_types()
        if agent_spec.permission_mode and not permission_mode and not dangerously_skip_permissions:
            resolved_mode = agent_spec.permission_mode
            permission_engine.mode = resolved_mode
            if resolved_mode == "plan" and permission_engine.plan_file is None:
                permission_engine.set_plan_file(ensure_plan_file(cwd, settings))
    elif agent:
        print(f"rolo-claude: unknown --agent {agent!r} (known: {', '.join(sorted(discovered_agents)) or '(none)'})",
              file=sys.stderr)

    ctx = SessionContext(
        cwd=cwd, model_label=model_ref.raw, model_family=family, settings_flag=settings_flag,
        setting_sources=setting_sources, append_system_prompt=effective_append, bare=bare,
        tool_registry=frozen_registry, mcp_servers=mcp_servers_for_prompt,
    )
    if agent_spec is not None and agent_spec.body:
        ctx.system_prompt = agent_spec.body
    if system_prompt:
        ctx.system_prompt = system_prompt

    openrouter_base_url = os.environ.get("BRIDGE_OPENROUTER_BASE_URL") if model_ref.provider == "openrouter" else None
    extra_headers = {"x-databricks-use-coding-agent-mode": "true"} if model_ref.provider == "databricks" else None

    # H6 scope D: --continue/--resume/--fork-session all resolve to a
    # concrete session_id BEFORE the log is opened -- an explicit
    # --session-id always wins outright (validated by the caller); a
    # resolved --continue/--resume id is then optionally forked (a fresh
    # id, log copied) before Session.__init__ ever sees it, so the
    # ORIGINAL session's own file is never appended to by the fork.
    resolved_session_id = session_id
    resume_error: Optional[str] = None
    if resolved_session_id is None and continue_:
        resolved_session_id = agent_sessions.resolve_continue(cwd)
        if resolved_session_id is None:
            resume_error = "no sessions found for this directory to --continue"
    if resolved_session_id is None and resume is not None:
        resolved_session_id, resume_error = agent_sessions.resolve_resume(cwd, resume if resume else None)
    if resolved_session_id is not None and fork_session_flag:
        resolved_session_id = agent_sessions.fork_session(cwd, resolved_session_id)
    session_log = SessionLog(cwd, session_id=resolved_session_id or uuid.uuid4().hex)
    # critical fix: `SessionLog.__init__` never reads its own file (only
    # `SessionLog.latest_for_cwd` does that, via this exact same line) --
    # without this, EVERY --session-id/--continue/--resume/--fork-session
    # onto a session whose .jsonl already has content looks EMPTY to
    # `Session.__init__`, which then takes the "brand new session" branch
    # and re-appends a SECOND meta/system/snapshot set instead of resuming
    # (silently dropping the entire prior transcript from every derived
    # request from that point on). A brand-new id's file doesn't exist yet,
    # so `read_all()` is a harmless no-op ([]) in that case.
    if session_log.path.exists():
        session_log._nodes = session_log.read_all()
    hook_runner = build_hook_runner(
        settings=settings, cwd=cwd, session_id=session_log.session_id, transcript_path=str(session_log.path),
        effort=effort, permission_mode=resolved_mode, mcp_manager=mcp_manager, bare=bare,
    )
    session = Session(
        cwd=cwd, model_ref=model_ref, model_profile=model_profile, creds=creds, state_dir=state_dir,
        model_label=model_ref.raw, session_context=ctx, small_model_ref=small_ref, session_log=session_log,
        max_turns=max_turns, openrouter_base_url=openrouter_base_url, effort=effort,
        extra_headers=extra_headers, permission_engine=permission_engine,
        session_catalog=session_catalog, mcp_manager=mcp_manager, hook_runner=hook_runner,
        agents=discovered_agents, routes=routes, agent_type_restriction=agent_type_restriction,
    )
    if hook_runner is not None:
        hook_runner.prompt_caller = session._call_model_for_hook
    # H9 whole-tree review finding 3 ("plus an atexit kill_all"): a safety
    # net alongside the explicit `finally: session.job_registry.kill_all()`
    # every caller already has (run_print_mode below, Controller.quit()) --
    # covers an exit path that bypasses BOTH (an exception raised while
    # still building the rest of this SessionBuild, before either caller's
    # own try/finally starts; a bare `sys.exit()` from somewhere with no
    # matching `finally` in between). `kill_all()` is idempotent (an
    # already-killed/completed job is a no-op), so running it twice on a
    # normal clean exit is harmless.
    atexit.register(session.job_registry.kill_all)

    command_registry = Registry.discover(cwd, home())
    if not bare:
        # H8 scope E (deferred by H3): every connected MCP server's own
        # prompts become `/mcp__<server>__<prompt>` slash commands, in both
        # -p and the TUI (bootstrap.py reuses this same builder).
        from rolo_claude.commands.registry import register_mcp_prompts
        register_mcp_prompts(command_registry, mcp_manager)
    facade = HeadlessFacade(
        cwd=cwd, settings=settings, claude_json=claude_json, model_ref=model_ref.raw,
        permission_mode=resolved_mode, tool_registry=frozen_registry, registry=command_registry,
        memory_store=ctx.memory_store, instructions=ctx.instructions, session_id=session_log.session_id,
        effort=effort, theme=resolve_theme(settings_theme=settings.theme),
        context_limit=model_profile.context_tokens,
        mcp_servers={h.config.name: {"type": h.config.type, "command": h.config.command, "args": h.config.args,
                                      "url": h.config.url} for h in (mcp_manager.handles.values() if mcp_manager else [])},
        mcp_status=(mcp_manager.status() if mcp_manager is not None else None),
        session=session,  # H5 scope D: live /cost, /context, /status, /compact
    )

    return SessionBuild(
        session=session, command_registry=command_registry, tool_registry=frozen_registry, facade=facade,
        mcp_manager=mcp_manager, mcp_notices=mcp_notices, session_catalog=session_catalog, settings=settings,
        claude_json=claude_json, resolved_mode=resolved_mode, routes=routes, state_dir=state_dir,
        model_ref=model_ref, model_profile=model_profile, creds=creds, ctx=ctx, session_log=session_log,
        hook_runner=hook_runner, resume_error=resume_error, agents=discovered_agents,
    )


def run_print_mode(
    *,
    prompt: Optional[str] = None,
    model_ref_raw: Optional[str] = None,
    small_model_ref_raw: Optional[str] = None,
    cwd: Optional[Path] = None,
    output_format: str = "text",
    input_format: str = "text",
    max_turns: int = 50,
    append_system_prompt: Optional[str] = None,
    system_prompt: Optional[str] = None,
    settings_flag: Optional[str] = None,
    setting_sources: Optional[list] = None,
    verbose: bool = False,
    effort: Optional[str] = None,
    allowed_tools: Optional[str] = None,
    disallowed_tools: Optional[str] = None,
    permission_mode: Optional[str] = None,
    dangerously_skip_permissions: bool = False,
    tools: Optional[str] = None,
    add_dir: Optional[list] = None,
    bare: bool = False,
    disable_slash_commands: bool = False,
    session_id: Optional[str] = None,
    include_partial_messages: bool = False,
    max_budget_usd: Optional[float] = None,
    json_schema: Optional[str] = None,
    replay_user_messages: bool = False,
    stdin_lines: Optional[list] = None,
    chrome: bool = False,
    no_chrome: bool = False,
    playwright: bool = False,
    playwright_cdp: Optional[str] = None,
    playwright_headless: bool = False,
    mcp_config: Optional[list] = None,
    strict_mcp_config: bool = False,
    continue_: bool = False,
    resume: Optional[str] = None,
    fork_session_flag: bool = False,
    agent: Optional[str] = None,
    agents_flag: Optional[str] = None,
    name: Optional[str] = None,
    file_specs: Optional[list] = None,
) -> int:
    """Run one turn (`input_format="text"`) or several (`"stream-json"`,
    one turn per entry of `stdin_lines`) in print mode; returns the
    process's exit code (the LAST turn's, for stream-json input)."""
    cwd = Path(cwd).resolve() if cwd else Path.cwd()

    # must-do: validated FIRST, before anything (incl. MCP) starts -- the
    # old position (right before `Session(...)`, well after `build_manager`
    # already spawned real MCP subprocesses) meant a bad --session-id
    # returned 2 with those servers still running, `close_all()` never
    # called. Validating up front means a bad id never starts them at all,
    # rather than starting-then-cleaning-up.
    if session_id and not agent_sessions.is_valid_session_id(session_id):
        print(f"rolo-claude: --session-id must be a valid UUID, got {session_id!r}", file=sys.stderr)
        return 2
    if resume is not None and resume != "" and not continue_:
        _resolved_probe, resume_err = agent_sessions.resolve_resume(cwd, resume)
        if _resolved_probe is None and resume_err:
            print(f"rolo-claude: --resume: {resume_err}", file=sys.stderr)
            return 2

    # u2-h3b finding 9: everything through a ready-to-drive Session is now
    # the ONE shared builder both -p and the TUI call -- see
    # `build_session`/`SessionBuild` above for what it does and why.
    build = build_session(
        cwd=cwd, model_ref_raw=model_ref_raw, small_model_ref_raw=small_model_ref_raw,
        settings_flag=settings_flag, setting_sources=setting_sources, effort=effort,
        allowed_tools=allowed_tools, disallowed_tools=disallowed_tools, permission_mode=permission_mode,
        dangerously_skip_permissions=dangerously_skip_permissions, tools=tools, add_dir=add_dir, bare=bare,
        session_id=session_id, max_turns=max_turns, append_system_prompt=append_system_prompt,
        system_prompt=system_prompt, json_schema=json_schema, chrome=chrome, no_chrome=no_chrome,
        playwright=playwright, playwright_cdp=playwright_cdp, playwright_headless=playwright_headless,
        mcp_config=mcp_config, strict_mcp_config=strict_mcp_config, print_mode=True,
        continue_=continue_, resume=resume, fork_session_flag=fork_session_flag,
        agent=agent, agents_flag=agents_flag,
    )
    session, frozen_registry, model_ref = (build.session, build.tool_registry, build.model_ref)
    mcp_manager, resolved_mode, session_log = build.mcp_manager, build.resolved_mode, build.session_log
    family = model_family(model_ref.model)
    attach_cli_files(session, file_specs, cwd=cwd)

    if mcp_manager is None and build.mcp_notices:
        print(f"rolo-claude: {build.mcp_notices[0]}", file=sys.stderr)
    elif verbose:
        for n in build.mcp_notices:
            print(f"[rolo-claude] mcp: {n}", file=sys.stderr)

    if verbose:
        print(f"[rolo-claude] model={model_ref.raw} provider={model_ref.provider} "
              f"dialect={model_ref.dialect} family={family} session={session_log.session_id}", file=sys.stderr)

    # H6 scope D: index.json's `first_prompt/started` are set ONCE, on the
    # very first touch of this session_id -- a --continue/--resume/
    # --fork-session onto an EXISTING entry must never reset its counters,
    # so this only fires when nothing is there yet (a brand-new session,
    # including a fresh fork -- fork_session copies the log but never the
    # index entry under the NEW id, see agent/sessions.fork_session).
    if agent_sessions.load_index(cwd).get(session_log.session_id) is None:
        first_prompt_text = prompt or (
            _extract_user_text(stdin_lines[0]) if stdin_lines else "")
        agent_sessions.record_session_start(cwd, session_log.session_id, first_prompt_text)
    if name:
        # `-n/--name` at invocation time is an immediate `/rename` (brief
        # D): the small-model auto-title, if any, would only ever apply
        # when no explicit name was ever given.
        agent_sessions.set_title(cwd, session_log.session_id, name)

    # H3: everything past this point may have live MCP subprocess/loop
    # state to tear down -- `finally` guarantees `mcp_manager.close_all()`
    # runs on EVERY exit path (an ordinary return, an early stream-json
    # error return, or an exception escaping `session.turn`), so a
    # `-p` invocation never leaves an orphaned server process or a live
    # daemon thread behind, and `~/.claude.json`/other state is quiescent
    # before the process actually exits (the checksum-stability contract).
    try:
        registry, facade = build.command_registry, build.facade
        # finding 7 must-do: init.mcp_servers is [{name, status}, ...]
        # (Claude Code's own stream-json shape), never a bare name list.
        mcp_servers_status = sorted(
            ({"name": s.get("name"), "status": s.get("state")} for s in (facade.mcp_status or [])),
            key=lambda s: s["name"] or "",
        )
        slash_names = [f"/{c.name}" for c in registry.all()]

        def _make_sink():
            if output_format == "stream-json":
                return StreamJsonSink(
                    session_id=session_log.session_id, cwd=str(cwd), model=model_ref.raw,
                    permission_mode=resolved_mode, tools=frozen_registry.names(), mcp_servers=mcp_servers_status,
                    slash_commands=slash_names, include_partial_messages=include_partial_messages,
                    max_budget_usd=max_budget_usd, permission_denials=session.permission_denials,
                    json_schema=json_schema,
                )
            return PrintModeSink(output_format=output_format, session_id=session_log.session_id, model=model_ref.raw,
                                  verbose=verbose, permission_denials=session.permission_denials, json_schema=json_schema,
                                  max_budget_usd=max_budget_usd)

        if input_format == "stream-json":
            if stdin_lines is not None:
                # Backward-compat path: a caller (a test, or any future
                # embedder) that already collected the lines itself --
                # replay them synchronously, exactly as before. Real CLI
                # runs never take this branch any more (cli.py no longer
                # pre-reads stdin for stream-json -- see its own comment).
                # `None` is queued only after every pre-known turn (and
                # any leftover steer each one generates -- see the shared
                # loop below) has actually been drained, mirroring a real
                # reader thread's EOF timing (never queued UP FRONT, which
                # would race ahead of a same-turn leftover re-queue).
                turns = [t for t in (_extract_user_text(obj) for obj in stdin_lines) if t]
                if not turns:
                    print("rolo-claude: --input-format stream-json requires at least one user message on stdin",
                          file=sys.stderr)
                    return 2
                line_queue = _TurnLineQueue()
                _remaining_known_turns = list(turns)
                line_queue.put(_remaining_known_turns.pop(0))

                def _feed_next_known_turn():
                    if _remaining_known_turns:
                        line_queue.put(_remaining_known_turns.pop(0))
                    else:
                        line_queue.put(None)
            else:
                _feed_next_known_turn = None
                # finding 10: read stdin INCREMENTALLY on a background
                # thread -- the first line starts the first turn; a line
                # arriving while the session is busy is applied as a
                # steer; otherwise it's queued as the next turn. The
                # reader thread is a daemon: an unread EOF never blocks
                # process exit.
                line_queue = _TurnLineQueue()
                reader = threading.Thread(
                    target=_stream_json_stdin_reader, args=(session, line_queue), daemon=True,
                    name="rolo-claude-stdin-reader",
                )
                reader.start()

            exit_code = 0
            sink = _make_sink()  # ONE sink for the whole run: `init` (stream-json) is per-SESSION, not per-turn
            turn_no = 0
            saw_any_turn = False
            while True:
                turn_text = line_queue.get()
                if turn_text is None:
                    break  # stdin EOF, no more turns
                saw_any_turn = True
                turn_no += 1
                if replay_user_messages:
                    print(json.dumps({"type": "user", "message": {"role": "user", "content": turn_text}}))
                facade.cost_usd = session.cost_meter.total_usd if session.cost_meter.has_cost_data else facade.cost_usd
                final_prompt, direct_output = _maybe_run_slash_command(
                    turn_text, registry=registry, facade=facade, disable_slash_commands=disable_slash_commands)
                if direct_output is not None:
                    exit_code = sink.consume(_events_for_direct_output(direct_output, turn_no=turn_no))
                else:
                    _append_at_mention_snapshots(session, final_prompt, cwd)
                    _append_agent_mention_snapshot(session, final_prompt or turn_text, build.agents)
                    exit_code = sink.consume(session.turn(final_prompt or turn_text))
                    # finding 5/10: a steer that landed after the turn's
                    # very last checkpoint (session.turn()'s own `finally`
                    # -- see agent/loop.py) is queued right back here as
                    # the NEXT turn, same as Session.run()'s own worker
                    # loop does for the interactive/TUI path.
                    if session._leftover_steer_texts:
                        leftover, session._leftover_steer_texts = session._leftover_steer_texts, []
                        # H5c finding 13: `put_urgent` (never a plain
                        # `put`) -- the reader thread may have ALREADY
                        # pushed a `None` EOF sentinel onto this queue
                        # while this turn's own slow Stop hook was still
                        # running (stdin closing is independent of when
                        # the turn itself finishes). A plain FIFO put would
                        # land the leftover BEHIND that `None`, where it is
                        # never read once the loop below sees the `None`
                        # and stops. Reversed so multiple leftovers keep
                        # their original relative order ahead of it.
                        for t in reversed(leftover):
                            line_queue.put_urgent(t)
                if _feed_next_known_turn is not None:
                    _feed_next_known_turn()
                facade.num_turns += 1
                agent_sessions.record_session_turn(
                    cwd, session_log.session_id,
                    cost_usd=(session.cost_meter.total_usd if session.cost_meter.has_cost_data else None))
            if not saw_any_turn:
                print("rolo-claude: --input-format stream-json requires at least one user message on stdin",
                      file=sys.stderr)
                return 2
            return exit_code

        prompt_text = prompt or ""
        final_prompt, direct_output = _maybe_run_slash_command(
            prompt_text, registry=registry, facade=facade, disable_slash_commands=disable_slash_commands)
        sink = _make_sink()
        if direct_output is not None:
            return sink.consume(_events_for_direct_output(direct_output))
        _append_at_mention_snapshots(session, final_prompt, cwd)
        _append_agent_mention_snapshot(session, final_prompt or prompt_text, build.agents)
        try:
            # H9 whole-tree review finding 9: `finish=False` -- the turn's
            # events are drained into `sink` but nothing is printed and no
            # exit code decided yet; `_drain_background_jobs_for_print_mode`
            # folds in any background-job notice and is the ONE place that
            # calls `sink.finish()` for this whole process (see its own
            # docstring for the multi-JSON-object/exit-code bug this closes).
            sink.consume(session.turn(final_prompt or prompt_text), finish=False)
            # H8 scope A must-do: a single -p text-input call has no later
            # turn to deliver a background job's completion notice through
            # (see `_drain_background_jobs_for_print_mode`'s own docstring)
            # -- print it here, before the process exits, instead of
            # silently losing it.
            return _drain_background_jobs_for_print_mode(session, sink)
        finally:
            agent_sessions.record_session_turn(
                cwd, session_log.session_id,
                cost_usd=(session.cost_meter.total_usd if session.cost_meter.has_cost_data else None))
    finally:
        try:
            session._fire_session_end("quit")
        except Exception:
            pass
        # H8 scope A: "jobs killed on quit" -- never leave a background
        # Bash job (or a timed-out foreground command moved to one)
        # running past this process's own exit.
        try:
            session.job_registry.kill_all()
        except Exception:
            pass
        if mcp_manager is not None:
            mcp_manager.close_all()
