"""halo_harness.headless -- print-mode session driver (U0 scope F rewrite).
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
import re
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from halo_harness import events
from halo_harness.agent import sessions as agent_sessions
from halo_harness.agent.assemble import SessionContext
from halo_harness.agent.catalog import SessionCatalog, host_cap, select_preload
from halo_harness.agent.log import SessionLog
from halo_harness.agent.loop import Session
from halo_harness.agent.planmode import ensure_plan_file
from halo_harness.commands.builtins import HeadlessFacade
from halo_harness.commands.registry import Registry
from halo_harness.config.agents_md import discover_agents
from halo_harness.config.claude_json import is_trusted, load_claude_json
from halo_harness.config.paths import bridge_home, env_compat, home, lookup_project
from halo_harness.config.settings import resolve_settings
from halo_harness.model import DEFAULT_MODEL_REF, parse_model_ref, resolve_default_model_raw, resolve_model_profile
from halo_harness.output import PrintModeSink, StreamJsonSink
from halo_harness.providers.routing import InvalidModelError
from halo_harness.permissions import (
    PermissionEngine, bare_deny_tool_names, build_rules_from_settings, freeze_tool_registry,
    mcp_deny_tool_names, normalize_permission_mode, split_tool_rule_list,
)
from halo_harness.providers.config import (
    derive_workspace_root, load_provider_env_files, load_routes, resolve_anthropic, resolve_databricks,
    resolve_openrouter,
)
from halo_harness.providers.profiles import model_family
from halo_harness.providers.stream import ProviderCreds
from halo_harness.theme import load_config as load_rolo_config, resolve_theme
from halo_harness.tools.registry import ToolRegistry

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
    if ref.provider == "ollama":
        # Halo 2.0.3 round 2b: the ONE place print mode (`build_session`
        # below), the TUI (`controller.py`'s own `_default_model_resolver`),
        # `/model`, and the fallback chain (`agent/loop.py`'s
        # `apply_next_fallback_model`) all resolve an `ol:` ref's
        # credentials -- no other call site needs its own ollama creds
        # code. `host.api_key` is unset for every host but an Ollama Cloud
        # one (research doc Q7); `or ""` keeps `ProviderCreds.api_key` a
        # plain str (its declared type), matching `stream_ollama_
        # completion`'s own `req.creds.api_key or None` read on the way
        # back out.
        from halo_harness.providers.ollama import resolve_ollama_host
        host = resolve_ollama_host(getattr(ref, "host", None), env)
        if host is None:
            return None
        return ProviderCreds(base_url=host.url, api_key=host.api_key or "")
    if ref.provider == "huggingface":
        # Halo 2.0.3 round 4: `ref.host` set means `hf:endpoint/<name>` --
        # a dedicated Inference Endpoint's OWN url/token, resolved from
        # `huggingface.endpoints` and used as-is; `ref.host is None` means
        # the router, resolved from HF_TOKEN. Pinned by
        # tests/test_providers_huggingface.py: these two sources must never
        # cross-wire (an endpoint's own token never substitutes for
        # HF_TOKEN or vice versa).
        #
        # Round 5: `ref.local` (checked FIRST -- a local ref's `host` can
        # ALSO be `None`, for the bare/default-server shape, which must
        # never fall into the endpoint/router branches below) resolves
        # through `providers.huggingface_local_resolve.resolve_local_
        # server` -- a manual `huggingface.local_servers` entry (named or
        # default) or, with none configured, the first auto-detected
        # server; its OWN `api_key` (when set) is sent as this request's
        # bearer, pinned apart from both HF_TOKEN and an endpoint's token
        # the exact same way those two are already pinned apart from each
        # other below.
        if getattr(ref, "mlx", False):
            # Round 5f: an EXACT registry lookup by repo id (`ref.model`),
            # never the generic `ref.local` "most recently started wins"
            # fallback below -- an `hf:mlx/<repo>` ref always names its
            # exact repo, so resolving it must never return a DIFFERENT
            # repo's managed server just because that one happened to
            # start more recently. Lookup-ONLY, deliberately no side
            # effect (`_resolve_creds` is called from several places,
            # including a bare "is this configured" probe -- line ~790 --
            # that must never itself spawn a process): the actual ENSURE/
            # start-with-consent step runs once, earlier, in `build_
            # session` below (or `doctor_local.py` for `--local`). `None`
            # here (nothing started yet) is the ordinary "not configured"
            # outcome every other provider branch on this page already has.
            from halo_harness.providers.huggingface_local_resolve import resolve_managed_server_by_model
            target = resolve_managed_server_by_model(ref.model)
            if target is None:
                return None
            return ProviderCreds(base_url=target.base_url, api_key=target.api_key or "")
        if ref.local:
            from halo_harness.providers.huggingface_local_resolve import resolve_local_server
            target = resolve_local_server(ref.host, env)
            if target is None:
                return None
            return ProviderCreds(base_url=target.base_url, api_key=target.api_key or "")
        if ref.host:
            from halo_harness.providers.huggingface import resolve_huggingface_endpoint
            ep = resolve_huggingface_endpoint(ref.host)
            if ep is None:
                return None
            return ProviderCreds(base_url=ep.url, api_key=ep.token or "")
        from halo_harness.providers.config import resolve_huggingface
        hf = resolve_huggingface(env)
        if hf is None:
            return None
        return ProviderCreds(base_url=hf.base_url, api_key=hf.api_key)
    if ref.provider == "openai":
        # Halo 2.0.3 round 5i part 1: `OPENAI_API_KEY` only -- no
        # endpoint/local-server concept the way `huggingface` has; both
        # the `openai-chat` and `openai-responses` dialects share this
        # SAME credential pair (`providers.stream._run_phase1`/`_run_
        # phase1_responses` each pick their own call_* with it).
        from halo_harness.providers.config import resolve_openai
        oai = resolve_openai(env)
        if oai is None:
            return None
        return ProviderCreds(base_url=oai.base_url, api_key=oai.api_key)
    return None


def _ensure_mlx_server_for_ref(ref, state_dir) -> None:
    """Round 5f: the ONE place a session (print mode via `build_session`
    below, and the TUI's initial launch via `tui/bootstrap.py`'s own call
    to `build_session`) ensures a Halo-managed `mlx_lm.server` exists for
    an `hf:mlx/<repo>` ref BEFORE the turn/profile-read-back that needs it
    runs -- `_resolve_creds`'s own `ref.mlx` branch is deliberately lookup-
    only (no side effect; it's called from several places, including a
    bare "is this configured" probe) so this is where the real start-with-
    consent step lives instead. A no-op for every other ref shape, and a
    cheap registry-read no-op on every call AFTER the first for the SAME
    repo (`ensure_mlx_server`'s own "already running" short-circuit) --
    safe to call more than once per session (main ref, then the small
    role's ref, which may name the same or a different mlx repo).
    `confirm` is left at `ensure_mlx_server`'s own default (auto-proceed,
    print the notice) -- the brief's own "`--yes` in print mode": there is
    no interactive prompt loop wired this deep, and typing `hf:mlx/<repo>`
    at all is the same explicit-action-is-consent rule every other `hf:`/
    `or:` cloud ref already follows with no separate gate."""
    if getattr(ref, "provider", None) != "huggingface" or not getattr(ref, "mlx", False):
        return
    from halo_harness.providers.huggingface_mlx import ensure_mlx_server
    _target, lines = ensure_mlx_server(ref.model, state_dir=state_dir)
    for line in lines:
        print(f"halo: {line}", file=sys.stderr)


def _resolve_dbx_config_for_headers(settings=None):
    """H14 scope B: a second, cheap (no network) `resolve_databricks` call
    purely to get at `DbxConfig.custom_headers` -- `_resolve_creds` above
    only ever keeps `(base_url, api_key)`, and threading a header dict
    through `ProviderCreds` (shared by OpenRouter/Databricks/Anthropic
    alike) would widen that dataclass for one provider's own concern.
    `None` when Databricks isn't configured at all (extra_headers then
    falls back to just the default coding-agent-mode header)."""
    env = settings.effective_env if settings is not None else None
    return resolve_databricks(env)


def build_hook_runner(*, settings, cwd: Path, session_id: str, transcript_path: str,
                       effort: Optional[str], permission_mode: str, mcp_manager, bare: bool,
                       extra_plugin_roots: Optional[list] = None):
    """Shared by `run_print_mode` and `tui/bootstrap.py` (imported from
    there, same reuse pattern as `_resolve_creds`) -- one `HookRunner` per
    session, built from `settings.hooks` (already trust-filtered by
    `config/settings.py`'s own merge) + every enabled plugin's own
    `hooks/hooks.json`. `--bare`/`disableAllHooks` disable the runner
    outright (`enabled=False`), so a caller never has to special-case an
    empty result -- every `HookRunner.has_hooks(...)` call site is then
    simply always False. `prompt_caller` is left unbound here (the CALLER
    binds it to the just-constructed Session's own `_call_model_for_hook`,
    once one exists -- see the call site). `extra_plugin_roots` (W5,
    carried from W4a) is `build_session`'s own `--plugin-dir`/`--plugin-url`
    resolution -- merged in the SAME way as an installed plugin's own
    `hooks/hooks.json` (last, so an installed plugin's hooks still run even
    when a CLI-supplied plugin ALSO configures the same event -- both fire,
    `merge_hook_maps` concatenates rather than replacing)."""
    from halo_harness.config.plugins import load_installed_plugins, _plugin_roots
    from halo_harness.hooks import HookRunner, load_plugin_hooks, merge_hook_maps, normalize_hooks

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
        for plugin_root in (extra_plugin_roots or []):
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
    from halo_harness.commands.registry import read_at_mention_snapshots
    for path_str, content in read_at_mention_snapshots(text, cwd=cwd):
        session.log.append_snapshot([{"type": "text", "text": f"@{path_str}\n{content}"}], kind="at_mention")
    # H8 scope E (deferred by H3): `@server:resource` mentions, the MCP
    # sibling of the `@path` handling just above -- same "separate context
    # block, never inlined" rule.
    from halo_harness.mcp.mentions import read_server_resource_snapshots, unresolved_server_mentions
    for label, content in read_server_resource_snapshots(text, mcp_manager=session.mcp_manager):
        session.log.append_snapshot([{"type": "text", "text": f"@{label}\n{content}"}], kind="at_mention")
    # W4a misc: see controller.py's own identical comment -- a visible
    # warning naming the server, never a silent no-op, for an `@name:uri`
    # whose server isn't connected at all.
    for server in unresolved_server_mentions(text, mcp_manager=session.mcp_manager):
        warning = f"@{server}:... does not match any currently connected MCP server named {server!r}."
        session.log.append_snapshot([{"type": "text", "text": warning}], kind="at_mention")
        # review finding 23 / parity gap: the snapshot above is model-only
        # context; there is no live UI to notice here (print/headless
        # mode -- controller.py's own identical call site pushes a TUI
        # notice instead), so the user's only view of it is this line.
        print(f"halo: {warning}", file=sys.stderr)


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
    from halo_harness.tools.base import ToolContext
    from halo_harness.tools.read import ReadTool
    tool = ReadTool()
    model_profile = getattr(session, "model_profile", None)
    ctx = ToolContext(cwd=cwd, vision=getattr(model_profile, "vision", False))
    for raw in file_specs:
        resolved = _resolve_local_file_spec(raw, cwd=cwd)
        if resolved is None and ":" in raw:
            file_id, _, rel_path = raw.partition(":")
            print(f"halo: --file: {raw!r} looks like Claude Code's "
                  f"file_id:relative_path cloud-resource form ({file_id!r} -> {rel_path!r}) -- "
                  f"halo has no claude.ai-hosted file store to download it from, skipped. "
                  f"A local path (relative to --cwd, or absolute) attaches directly.", file=sys.stderr)
            continue
        if resolved is None:
            print(f"halo: --file: {raw}: not a file, skipped", file=sys.stderr)
            continue
        try:
            result = tool.run({"file_path": str(resolved)}, ctx)
        except Exception as e:
            print(f"halo: --file: {raw}: {e}", file=sys.stderr)
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
    from halo_harness.config.agents_md import agent_mention_instruction, find_agent_mentions
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


def _maybe_add_prompt_suggestion(session, sink, cli_flags: dict) -> None:
    """W4a/W5 `--prompt-suggestions`: one extra small-model call predicting
    the user's likely next message, ONLY when the flag was given (never a
    default-on cost -- see MEMORY.md's token-thrift rule). Both call sites
    thread the SAME `finish=False`-then-`sink.finish()` gap through: the
    single-turn text-input path (`_drain_background_jobs_for_print_mode` is
    what actually calls `finish()` there, folding in any background-job
    notice too) and (W5, carried from W4a) the `--input-format stream-json`
    multi-turn loop's own per-turn `sink.consume(..., finish=False)` /
    `sink.finish()` pair. Never raises: a prediction failure must never
    affect the real turn's result."""
    if not cli_flags.get("prompt_suggestions"):
        return
    try:
        last_assistant_text = ""
        for node in reversed(session.log.nodes()):
            if node.get("type") == "assistant":
                parts = [b.get("text", "") for b in (node.get("content") or [])
                         if isinstance(b, dict) and b.get("type") == "text"]
                last_assistant_text = "".join(parts).strip()
                if last_assistant_text:
                    break
        if not last_assistant_text:
            return
        suggestion = session.call_small_model(
            system_text=("Given the assistant's last message in a coding-assistant conversation, predict the "
                         "user's most likely next message. Reply with ONLY that predicted message text, nothing else."),
            user_text=last_assistant_text, max_tokens=200, timeout_s=20.0,
        )
        suggestion = (suggestion or "").strip()
        if suggestion:
            sink.add_prompt_suggestion(suggestion)
    except Exception:
        pass


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
    # finding 1 (W6a): whether `session_log`'s own file already existed on
    # disk (a -c/--resume/--session-id/--fork-session target) before this
    # call touched it, and its exact byte length at that moment --
    # `run_print_mode`'s `--no-session-persistence` cleanup needs both, but
    # SessionLog is built inside THIS function, not there.
    pre_existing_log: bool = False
    pre_existing_log_size: int = 0


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
    agent: Optional[str] = None, agents_flag: Optional[str] = None, roles_flag: Optional[list] = None,
    cli_flags: Optional[dict] = None,
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
    from halo_harness import debug_timeline
    cli_flags = dict(cli_flags or {})
    # W5 (carried from W4a): `--plugin-dir`/`--plugin-url` roots, resolved
    # ONCE here (before `build_manager`/`build_hook_runner`/`Registry.
    # discover` below, which previously had no access to this at all --
    # only `discover_agents` did, further down) so a CLI-supplied plugin's
    # MCP servers, hooks, skills and commands load alongside its agents,
    # the same four precedence tiers `config/plugins.py` already covers for
    # an INSTALLED plugin. Stashed back onto `cli_flags["resolved_plugin_
    # roots"]` too (read by `Session.__init__` as `self.plugin_roots`) so
    # tool-side discovery (`tools/skill.py`'s Skill tool) can see the same
    # list a model-invoked skill call needs, not just the slash-command
    # surface built below.
    plugin_roots: list = []
    if cli_flags.get("plugin_dir") or cli_flags.get("plugin_url"):
        # `bridge_home` is already a module-level import (top of this file)
        # -- NOT re-imported here on purpose: a local `from ... import
        # bridge_home` inside this `if` would make Python treat the name as
        # local to the WHOLE `build_session` function (even on the branch
        # that skips this block), breaking the unconditional `state_dir =
        # bridge_home()` call later in this same function.
        from halo_harness.plugin_fetch import resolve_plugin_roots
        plugin_roots = resolve_plugin_roots(cli_flags.get("plugin_dir"), cli_flags.get("plugin_url"),
                                             state_dir=bridge_home())
    cli_flags["resolved_plugin_roots"] = plugin_roots
    claude_json = load_claude_json()
    trusted = is_trusted(cwd, claude_json)
    settings = resolve_settings(cwd, settings_flag=settings_flag, setting_sources=setting_sources, trusted=trusted)
    claude_json_allowed_tools = lookup_project(claude_json, cwd).get("allowedTools")
    debug_timeline.mark("settings")

    # H14 scope C: `effortLevel`/`modelSettings.<id>.effortLevel` from
    # settings.json become the session's default effort -- only when
    # nothing more specific (`--effort`) was already given; a settings
    # value can never override an explicit flag.
    #
    # 1.0.1 hotfix 19/20: `effort_source` rides alongside for `/effort`'s
    # own "effective value and its source" line -- "flag"/"settings" here;
    # Session itself labels its own Anthropic-family "high" default (see
    # `agent/loop.py::__init__`) as "default" when NEITHER of these fired.
    #
    # 2.0.1 W3a ("launch with the last session's model and effort"):
    # precedence is now --effort flag > last_effort (persisted by `/effort`,
    # cwd-then-global per `model_memory`) > ~/.halo/config.json's own
    # "effort" key > settings.json's effortLevel > the route's own default
    # (unchanged below). Never consulted for -c/--resume -- a resumed
    # session keeps its own prior effort, read from its log, not this chain.
    from halo_harness import launch_state
    from halo_harness.theme import get_config_value
    effort_source = "flag" if effort is not None else None
    model_memory = get_config_value("model_memory", default="cwd")
    if effort is None and not (continue_ or resume):
        last_effort = launch_state.resolve_last_effort(cwd, memory=model_memory)
        if last_effort is not None:
            effort = last_effort
            effort_source = "last"
    if effort is None:
        config_effort = get_config_value("effort", default=None)
        if isinstance(config_effort, str) and config_effort:
            effort = config_effort
            effort_source = "config"
    if effort is None:
        effort = settings.resolved_effort_level()
        if effort is not None:
            effort_source = "settings"

    cli_allow = split_tool_rule_list(allowed_tools) if allowed_tools else []
    cli_disallow = split_tool_rule_list(disallowed_tools) if disallowed_tools else []
    deny_rules, ask_rules, allow_rules = build_rules_from_settings(
        settings, cwd=cwd, claude_json_allowed_tools=claude_json_allowed_tools,
        cli_allow=cli_allow, cli_disallow=cli_disallow,
    )
    # W4 MCP connectors-bridge item 6: the user's own deny/ask (and, for
    # symmetry, allow) rules for `mcp__claude_ai_<Name>__*` in Claude Code's
    # own settings are translated to the matching `connector__<slug>` rule,
    # so a rule written for claude still applies to halo's own bridge tool
    # -- ADDED alongside the original (never replacing it; the original
    # `mcp__claude_ai_*` rule simply never matches anything here).
    from halo_harness.mcp.connectors_bridge import translate_rules
    deny_rules = translate_rules(deny_rules, action="deny")
    ask_rules = translate_rules(ask_rules, action="ask")
    allow_rules = translate_rules(allow_rules, action="allow")

    # 1.0.1 hotfix 18.2: `~/.halo/config.json`'s own `permission_mode`
    # (item 18.1's new init step) is a NEW layer, spliced in between the CLI
    # flag and settings.json's `permissions.defaultMode` -- an explicit
    # `--permission-mode` this run still wins outright, but a user's own
    # standing default (set once at `init` time) now beats whatever a
    # project's settings.json happens to declare, which is what "I always
    # want auto on MY boxes" actually means. `doctor` (doctor.py) prints
    # this exact chain's own winning source so it's never a mystery which
    # layer decided.
    from halo_harness.theme import get_config_value
    config_permission_mode = get_config_value("permission_mode", default=None)
    if dangerously_skip_permissions:
        resolved_mode = "bypassPermissions"
    elif permission_mode:
        resolved_mode = normalize_permission_mode(permission_mode)
    elif isinstance(config_permission_mode, str) and config_permission_mode:
        resolved_mode = normalize_permission_mode(config_permission_mode)
    elif settings.permissions_default_mode:
        resolved_mode = normalize_permission_mode(settings.permissions_default_mode)
    else:
        resolved_mode = "default"

    # W4a `--restricted`: claude's own "refuses bypassPermissions" -- an
    # explicit --dangerously-skip-permissions alongside --restricted falls
    # back to "default" (asks) rather than silently granting bypass anyway.
    if cli_flags.get("restricted") and resolved_mode == "bypassPermissions":
        print("halo: --restricted is read-only, so --dangerously-skip-permissions is ignored -- the default permission mode applies",
              file=sys.stderr)
        resolved_mode = "default"

    base_tools = None
    if cli_flags.get("brief"):
        # claude's own --brief: "Enable SendUserMessage tool for
        # agent-to-user communication" -- added to the catalog BEFORE
        # freeze_tool_registry so --tools/deny-rule filtering still applies
        # to it exactly like any other built-in tool.
        from halo_harness.tools.registry import default_tools
        from halo_harness.tools.send_user_message import SendUserMessageTool
        base_tools = default_tools() + [SendUserMessageTool()]

    frozen_registry = freeze_tool_registry(
        ToolRegistry(base_tools), tools_flag=tools, disallowed_tools=cli_disallow, deny_rules=deny_rules,
    )
    if cli_flags.get("restricted"):
        # claude's own --restricted: "removes the built-in tools that run
        # commands or code (Bash, PowerShell, ...) and WebFetch unless
        # --tools names them". `tools` (the raw --tools string, not yet
        # catalog-filtered) is the one signal for "named them explicitly".
        #
        # parity gap (W6a): part 8's own commit message calls this "a
        # read-only tool set", but this used to strip only Bash/
        # PowerShell/WebFetch -- Edit/Write/NotebookEdit (arbitrary file
        # writes) and every MCP server tool (arbitrary server-side
        # actions) stayed reachable. `--tools` still names an exception
        # through exactly the same `named` set.
        named = set(split_tool_rule_list(tools)) if tools else set()
        mcp_tool_names = {n for n in frozen_registry.names() if n.startswith("mcp__")}
        restricted_deny = ({"Bash", "PowerShell", "WebFetch", "Edit", "Write", "NotebookEdit"}
                            | mcp_tool_names) - named
        if restricted_deny:
            frozen_registry = frozen_registry.without(restricted_deny)
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

    # 2.0.0 fixpass finding 4: the new env file, then the legacy one too.
    load_provider_env_files()

    state_dir = bridge_home()
    routes = load_routes(state_dir / "routes.json")

    # H14 scope C: `settings.effective_env` (trust-filtered, shell < settings
    # chain) rather than bare os.environ, so "at work" default-model
    # resolution honors the SAME trust rules every other provider lookup
    # here already does.
    #
    # 2.0.1 W3a: --model flag > last_model (persisted by `/model`, cwd-
    # then-global per `model_memory`) > resolve_default_model_raw's own
    # existing chain (HALO_MODEL env > routes.json default > config.json's
    # "model" key > the work-env Databricks shortcut > the hardcoded
    # default). Never consulted for -c/--resume -- a resumed session keeps
    # its own prior model, read from its log. A persisted ref whose
    # provider is no longer enabled (or that otherwise fails to resolve)
    # falls through to the next source with one notice line naming it,
    # rather than failing the whole launch over a stale choice.
    model_raw = model_ref_raw
    if model_raw is None and not (continue_ or resume):
        last_model_raw = launch_state.resolve_last_model(cwd, memory=model_memory)
        if last_model_raw:
            try:
                last_ref = parse_model_ref(last_model_raw, routes)  # validate only -- parsed for real just below
            except InvalidModelError as e:
                print(f"halo: the last-used model {last_model_raw!r} is no longer available ({e}) -- "
                      f"using the configured default instead", file=sys.stderr)
            else:
                # review finding 35: `parse_model_ref` only ever rejects an
                # explicitly DISABLED provider -- a model whose provider is
                # merely "not set up" (no credentials at all, e.g. a global
                # last model remembered from a DIFFERENT box) used to be
                # accepted silently and built with `creds=None`, failing
                # every turn with no notice at all. `cc:` (and anything
                # else `_resolve_creds` doesn't model) needs no
                # `ProviderCreds` at all -- that is not this check's
                # business, so it is skipped entirely for those. Halo
                # 2.0.3 round 2b: `ollama` added -- a missing/renamed
                # `ollama.hosts` entry (a stale `ol:<model>@<hostname>`
                # remembered from a DIFFERENT box that actually had that
                # named host) is this provider's own equivalent of
                # "no credentials configured", now that `_resolve_creds`
                # models it too. 2.0.3 round 4: `huggingface` added for the
                # SAME reason -- a stale `hf:endpoint/<name>` remembered
                # from a box whose `huggingface.endpoints` no longer has
                # that name (or no HF_TOKEN for a stale router ref).
                if (last_ref.provider in ("openrouter", "databricks", "anthropic", "ollama", "huggingface", "openai")
                        and _resolve_creds(last_ref, settings) is None):
                    print(f"halo: the last-used model {last_model_raw!r} has no credentials configured on "
                          f"this box -- using the configured default instead", file=sys.stderr)
                else:
                    model_raw = last_model_raw
    if model_raw is None:
        model_raw = resolve_default_model_raw(routes, env=settings.effective_env)
    model_ref = parse_model_ref(model_raw, routes)
    if model_ref.provider == "databricks":
        # Halo 2.0.2 round 5 (Qwen-at-work brief, item 1): a decision-only/
        # judge endpoint (databricks-openjev-qwen35-4b and any other
        # `decision_only_info` match) is never the session model, however
        # it was chosen (--model, the last-used model, or the configured
        # default) -- routed to the `judge` role automatically instead
        # (the same action `Controller.set_model` takes for a MID-session
        # `/model` switch), falling this session back to the ordinary
        # configured default so launch never just fails outright over it.
        from halo_harness.providers.profiles import decision_only_notice
        _decision_notice = decision_only_notice(model_ref.model)
        if _decision_notice:
            from halo_harness.theme import set_config_value
            set_config_value("roles.judge", model_ref.raw)
            print(f"halo: {_decision_notice}\n`judge` role set to {model_ref.raw} -- "
                  f"using the configured default model for this session instead", file=sys.stderr)
            model_raw = resolve_default_model_raw(routes, env=settings.effective_env)
            model_ref = parse_model_ref(model_raw, routes)
            # 2.0.2 review finding 8 (major): `resolve_default_model_raw`'s
            # OWN "configured default" chain (config.model, HALO_MODEL,
            # routes.default, or the work-env `dbx:<ANTHROPIC_MODEL>`
            # shortcut) is exactly what pointed at the decision-only
            # endpoint in the first place -- calling it again with the
            # SAME inputs just returned the SAME ref, uncaught, so "a
            # session can never start on it" failed precisely when the
            # CONFIGURED default itself was the decision-only endpoint.
            # Falls all the way through to the hardcoded DEFAULT_MODEL_REF
            # instead, which this whole block never routes to `roles.
            # judge` (it isn't Databricks), so this loop can run at most
            # once.
            if model_ref.provider == "databricks" and decision_only_notice(model_ref.model):
                print(f"halo: the configured default model is ALSO a decision-only endpoint -- "
                      f"using {DEFAULT_MODEL_REF!r} for this session instead", file=sys.stderr)
                model_raw = DEFAULT_MODEL_REF
                model_ref = parse_model_ref(model_raw, routes)
    # V2c (H15): the persisted role table (config.json's own "roles" --
    # itself already seeded from a team.json at `init` time -- or, only
    # when that's completely empty AND this session's own model is a
    # Databricks one, the documented cost-aware default) plus this run's
    # own `--role name=model` CLI overrides (always win, even over the
    # persisted table -- see `config.agents_md.resolve_agent_model`'s own
    # docstring for the full chain). Resolved HERE (moved up from just
    # below the small-model block) so `roles.small`/`--role small=...`
    # can actually be consulted for `small_raw` right below -- 2.0.2
    # review finding 12 (major): neither was ever read before this;
    # the session's own small model came only from `--small-model`/
    # `HALO_MODEL_SMALL`/`routes.json`'s own "small"/the main model,
    # even though both the built-in presets AND the Databricks cost-
    # aware table set `roles.small`, and ROLES.md documents it driving
    # summaries/titles/`/improve`/the compaction fallback.
    from halo_harness.roles import parse_role_flags, resolve_role_table, role_value_parts
    cli_roles = parse_role_flags(roles_flag)
    persisted_roles = resolve_role_table(provider=model_ref.provider)
    _small_role_raw = cli_roles.get("small")
    _small_role_from_table = _small_role_raw is None
    if _small_role_raw is None:
        _small_role_raw = persisted_roles.get("small")
    if _small_role_from_table and _small_role_raw is not None:
        # Round 5b part 2 (brief item 3): a TABLE value only (never a
        # `--role small=...` override THIS run -- that stays untouched,
        # same rule `roles.resolve_role_ref` already applies for every
        # other role) redirected to the main model when it would not fit
        # beside it on the SAME Ollama host -- see `roles.vram_aware_
        # override`'s own docstring. `model_ref` is already resolved
        # above (the session's own main ref).
        from halo_harness.roles import vram_aware_override
        _small_role_raw, _small_vram_reason = vram_aware_override("small", _small_role_raw, main_ref=model_ref)
    _small_role_model, small_effort = role_value_parts(_small_role_raw)

    explicit_small_raw = small_model_ref_raw or env_compat("MODEL_SMALL")
    small_raw = explicit_small_raw or _small_role_model or routes.get("small") or model_raw
    try:
        small_ref = parse_model_ref(small_raw, routes) if small_raw else None
    except InvalidModelError:
        # 1.0.1 part 2 fixpass finding 3: a shared/team-wide routes.json may
        # pin "small" to a provider this particular box never set up (e.g.
        # routes.example.json ships `"small": "or:..."`, refused outright on
        # a Databricks-only box) -- the small model only ever backs a
        # background compaction/title call, never the user's own turn, so
        # refusing the WHOLE session over it is disproportionate. Falls back
        # to the already-resolved MAIN ref (which just parsed fine above)
        # instead of failing the session outright.
        small_ref = model_ref
        # M4 (1.0.1 final pass): an EXPLICIT --small-model/HALO_MODEL_SMALL
        # that gets refused must never fail SILENTLY -- only routes.json/
        # routes.example's own lenient "small" default (nobody typed this;
        # it's a shared file's own choice) stays quiet about falling back.
        if explicit_small_raw:
            print(f"halo: --small-model/HALO_MODEL_SMALL {small_raw!r} does not resolve -- "
                  f"using the main model {model_raw!r} for background calls instead", file=sys.stderr)
    # Round 5f: ensure an `hf:mlx/<repo>` main or small-role ref has a
    # running Halo-managed mlx_lm.server BEFORE the profile read-back just
    # below needs one -- see `_ensure_mlx_server_for_ref`'s own docstring.
    _ensure_mlx_server_for_ref(model_ref, state_dir)
    if small_ref is not None:
        _ensure_mlx_server_for_ref(small_ref, state_dir)
    model_profile = resolve_model_profile(model_ref, state_dir, routes)
    family = model_family(model_ref.model)

    # H12 Part C (RECOMMENDATIONS.md P0 #3): a per-family Edit context-line
    # hint, appended to the frozen registry's OWN Edit tool INSTANCE
    # (default_tools() built a fresh one for this call, never shared with
    # any other session) exactly once, here -- before session_catalog/the
    # first request ever reads tool_registry.definitions(), so the wire
    # tool definitions stay stable turn to turn (never recomputed mid-
    # session, which would bust the provider's own prompt cache prefix).
    edit_tool = frozen_registry.get("Edit")
    if edit_tool is not None:
        from halo_harness.providers.profiles import edit_hint_for
        edit_hint = edit_hint_for(model_ref.provider, model_ref.model)
        if edit_hint:
            edit_tool.description = f"{edit_tool.description}\n\n{edit_hint}"

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

    # H14 scope J: auto-refresh the Databricks catalog on session start when
    # stale, silently, on a daemon thread -- never blocks/delays this turn;
    # the visible one-line diff notification is `/model` open and `/models
    # refresh`/`/dbx`'s own job (headless.py has no UI to post one to). Only
    # ever refreshes an ALREADY-cached catalog (never first-time discovery
    # from a plain session start, which every -p/TUI/test session goes
    # through) -- a session against a host with no cache yet is left to
    # `init --preset work`/`/models refresh`, so an unreachable/fake `dbx:`
    # host (common in this harness's own test suite) never spawns a live
    # background probe just from building a session.
    from halo_harness.config.paths import background_net_disabled
    if not bare and model_ref.provider == "databricks" and creds is not None and not background_net_disabled():
        import threading as _threading
        def _bg_dbx_refresh() -> None:
            try:
                from halo_harness.providers.databricks import dbx_endpoints_age_seconds, refresh_dbx_catalog_if_stale
                if dbx_endpoints_age_seconds(state_dir) is not None:
                    # N2c (1.0.1 final pass): this session's own trust-
                    # filtered `settings.effective_env` (already resolved
                    # above, with THIS session's real cwd/trust) -- never
                    # bare os.environ, which could resolve a DIFFERENT
                    # (wrong-layer-mixed, or untrusted-project-sourced)
                    # Databricks config than the turn itself will use.
                    refresh_dbx_catalog_if_stale(state_dir, env=settings.effective_env)
            except Exception:
                pass
        _threading.Thread(target=_bg_dbx_refresh, daemon=True, name="halo-dbx-auto-refresh").start()

    # 2.0.1 launch-hang fix, item (b) ("headless -p runs refresh in a
    # thread under the same rule"): deliberately NOT a thread spawned from
    # every single `build_session` call -- an earlier draft did exactly
    # that (mirroring the TUI's own once-per-App-lifetime worker) and,
    # verified live on a box with a real `claude` install on PATH and no
    # gateway signal, it spawned one real `claude auth status` subprocess
    # PER SESSION BUILT -- a far higher frequency than the TUI case, since
    # `build_session` runs once per turn in many print-mode/test flows.
    # `claude_login_available()`/`credentials_present("claude_subscription")`
    # are cache-only now and never spawn it themselves, so the equivalent
    # "refresh in a thread" for headless mode lives where it's actually
    # needed instead: `/model`/`/providers` run headlessly
    # (`commands/builtins.py::_cmd_providers`) are ALREADY fully
    # synchronous (no UI thread to protect in print mode at all), so that
    # command does its own one-time, staleness-gated refresh right there --
    # never here, on every session build regardless of whether either
    # command is ever used.

    if (not bare and websearch_allowed and model_ref.provider == "openrouter" and creds is not None
            and frozen_registry.get("WebSearch") is None):
        from halo_harness.tools.websearch import build_websearch_tool
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
        from halo_harness.mcp_setup import build_manager, resolve_chrome_enabled
        chrome_enabled = resolve_chrome_enabled(claude_json, chrome_flag=chrome, no_chrome_flag=no_chrome,
                                                 interactive=not print_mode)
        mcp_manager, mcp_notices = build_manager(
            cwd=cwd, claude_json=claude_json, settings=settings, print_mode=print_mode,
            mcp_config_flag=mcp_config, strict_mcp_config=strict_mcp_config,
            chrome=chrome_enabled, playwright=playwright,
            playwright_cdp=playwright_cdp, playwright_headless=playwright_headless,
            bypass_mode=(resolved_mode in ("auto", "bypassPermissions")), start=True, trusted=trusted,
            extra_plugin_roots=plugin_roots,
        )
        if mcp_manager is not None:
            from halo_harness.tools.mcp_tool import ListMcpResourcesTool, McpTool, ReadMcpResourceTool
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

            # Halo 2.0.3 round 3 (brief item 3): an ollama ref's initial
            # freeze/preload budget already respects its context class --
            # without this, the INITIAL preload could fill the catalog
            # past the (smaller) cap `Session._sync_ollama_tools_cap`
            # would otherwise only discover and shrink to on the first
            # real turn, and eviction can never remove a FROZEN/preloaded
            # tool (`SessionCatalog._evict_one`'s own contract), so getting
            # this right at build time avoids a permanently-stuck-over-cap
            # catalog for a small-context local model.
            ollama_tools_max = None
            if model_ref.provider == "ollama":
                try:
                    from halo_harness.providers.ollama_hw import resolve_context_decision
                    ollama_tools_max = resolve_context_decision(model_ref).tools_max
                except Exception:
                    ollama_tools_max = None
            cap = host_cap(model_ref.provider, ollama_tools_max)
            cap_budget = max(0, cap - len(frozen_registry.names()))
            preload_names = set((load_rolo_config().get("mcpPreload") or []))
            # finding 13 must-do: server-level "alwaysLoad" preloads every
            # tool from that server (was missing from the TUI's own copy).
            always_load_servers = {name for name, h in mcp_manager.handles.items() if h.config.always_load}
            preload, deferred = select_preload(survivors, preload_names=preload_names,
                                                always_load_servers=always_load_servers, cap_budget=cap_budget)

            for server_name, _wire_name, sdk_tool in preload:
                frozen_registry.add_tool(McpTool(server_name, sdk_tool, mcp_manager, vision=model_profile.vision,
                                                  audio=model_profile.audio, family=family))

            # W4b claude.ai connectors bridge: one `connector__<slug>` tool
            # per discovered connector (cache-only read here -- discovery
            # itself is a background worker, never inline on a session
            # build by default), gated by --tools/deny the same way a
            # built-in would be, preloaded or deferred same as any MCP tool.
            # W5b ("connector cold start, properly"): a synchronous (bounded
            # 20s) discovery on a genuinely empty cache is no longer
            # unconditional here -- print mode must never block a plain
            # `-p` run on a `claude` spawn by default, only when THIS run
            # actually wants a connector tool (named explicitly via
            # `--tools`, or `connectors.discover_on_start` opts every run
            # in); eligibility reads the CACHED claude.ai login, so the
            # auth cache is primed first the way `halo mcp list` already
            # does. The TUI (print_mode=False) never runs this
            # synchronously at all -- `tui/bootstrap.py` kicks off the SAME
            # background discovery AFTER its Controller exists, with an
            # `on_done` that adds whatever it finds straight into this live
            # catalog (`SessionCatalog.add_connector_tools`) and posts one
            # transcript line, so a cold cache still fills in time for the
            # first turn without ever blocking startup. A mid-session
            # ToolSearch call that actually asks for a connector by name
            # gets its own bounded discovery too -- see `SessionCatalog.
            # ensure_connectors_discovered_for_query`, called from
            # `ToolSearchTool.run()`.
            from halo_harness.mcp import connectors_bridge
            from halo_harness.tools.connector_tool import ConnectorTool
            connector_requested = bool(tools_subset) and any(
                n.startswith("connector__") for n in tools_subset)
            if print_mode and (connector_requested or connectors_bridge.discover_on_start_configured()):
                connectors_bridge.prime_auth_cache_if_stale()
                connectors_bridge.ensure_discovered_synchronously_if_cold()
            for info in connectors_bridge.get_connectors():
                if not connectors_bridge.connector_enabled(info.slug):
                    continue
                connector_wire_name = f"connector__{info.slug}"
                if tools_subset is not None and connector_wire_name not in tools_subset:
                    continue
                if connector_wire_name in bare_denied_names:
                    continue
                connector_tool = ConnectorTool(info)
                if connector_tool.always_load():
                    frozen_registry.add_tool(connector_tool)
                else:
                    deferred[connector_wire_name] = (None, connector_tool)
            if print_mode:
                # The TUI's own equivalent background kick is
                # `tui/bootstrap.build_controller`'s job, not this shared
                # builder's -- it needs a real `on_done` wired to a live
                # `Controller`, which doesn't exist yet at this point.
                connectors_bridge.ensure_discovered_in_background()

            # finding 13 must-do: keep ToolSearch whenever the deferred pool
            # is non-empty, even when --tools/a deny rule would otherwise
            # have excluded it (was missing from the TUI's own copy).
            if deferred and frozen_registry.get("ToolSearch") is None:
                from halo_harness.tools.tool_search import ToolSearchTool
                frozen_registry.add_tool(ToolSearchTool())

            # Halo 2.0.3 round 3: `cap` above was sized from the context
            # class BEFORE `ToolSearchTool`/the preload loop could add
            # anything further to `frozen_registry` -- never let the
            # catalog's own stored cap end up SMALLER than what's already
            # irrevocably frozen in it (eviction can only ever remove a
            # LOADED-DEFERRED tool, never one of these), or the very next
            # request would raise ToolCatalogTooLarge against a catalog
            # that was never actually allowed to shrink.
            cap = max(cap, len(frozen_registry.names()))
            session_catalog = SessionCatalog(
                registry=frozen_registry, deferred=deferred, manager=mcp_manager, cap=cap,
                vision=model_profile.vision, audio=model_profile.audio, family=family, names=frozen_registry.names(),
                # review finding 30: so a connector `add_connector_tools`
                # loads LATER (background discovery, or a mid-session
                # ToolSearch hit) is filtered the same way the warm-cache
                # loop just above already filtered every OTHER connector.
                tools_subset=tools_subset, bare_denied_names=bare_denied_names,
            )
            mcp_servers_for_prompt = [
                {"name": name, "instructions": h.instructions}
                for name, h in mcp_manager.handles.items() if h.state == "connected"
            ]
    debug_timeline.mark("mcp discovery")

    effective_append = append_system_prompt
    if json_schema:
        note = f"Respond with JSON matching this schema (no prose outside the JSON): {json_schema}"
        effective_append = f"{effective_append}\n\n{note}" if effective_append else note

    # H6 scope A: `--agent <name>` runs the WHOLE session as that agent --
    # resolved against the FULLY assembled catalog (built-ins + MCP +
    # WebSearch, all already added to `frozen_registry` above) so
    # `spec.resolved_tools()` can actually see everything it might keep.
    # `plugin_roots` itself was already resolved at the top of this
    # function (shared with `build_manager`/`build_hook_runner` above and
    # `Registry.discover` below).
    discovered_agents = {} if bare else discover_agents(cwd, settings=settings, agents_flag=agents_flag,
                                                          plugin_roots=(plugin_roots or None))
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
        print(f"halo: unknown --agent {agent!r} (known: {', '.join(sorted(discovered_agents)) or '(none)'})",
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
    debug_timeline.mark("instructions")

    openrouter_base_url = env_compat("OPENROUTER_BASE_URL") if model_ref.provider == "openrouter" else None
    extra_headers = None
    if model_ref.provider == "databricks":
        # H14 scope B: ANTHROPIC_CUSTOM_HEADERS (one or more "Name: value"
        # lines from the settings env) is sent on every Databricks request,
        # native passthrough and chat alike -- merged under the default
        # x-databricks-use-coding-agent-mode (overridden by the custom
        # header of the same name when the env sets one).
        from halo_harness.providers.config import merge_databricks_headers
        dbx_cfg = _resolve_dbx_config_for_headers(settings)
        extra_headers = merge_databricks_headers(dbx_cfg.custom_headers if dbx_cfg else None)
    if model_ref.provider == "huggingface" and not model_ref.host and not model_ref.local:
        # Halo 2.0.3 round 4 (brief item 1): `X-HF-Bill-To` is a ROUTER-only
        # header (research doc section 9: Team/Enterprise billing a
        # specific org) -- `model_ref.host` set means `hf:endpoint/<name>`
        # (a dedicated endpoint, its own compute-time billing, no such
        # header), so this is gated to the router shape only. Round 5:
        # `not model_ref.local` added -- a bare `hf:local/<model>` ALSO
        # leaves `host` unset, and a local server gets no router billing
        # header either.
        from halo_harness.providers.huggingface import resolve_huggingface_bill_to
        bill_to = resolve_huggingface_bill_to()
        if bill_to:
            extra_headers = {**(extra_headers or {}), "X-HF-Bill-To": bill_to}
    if cli_flags.get("betas"):
        # W4a `--betas`: "Beta headers to include in API requests (API key
        # users only)" -- a comma-joined `anthropic-beta` header, the real
        # wire name an Anthropic-family route knows.
        #
        # W5 (carried from W4a, live bug found wiring this in): the OLD
        # comment here claimed this was "inert (never sent) on a chat-
        # dialect route that has no such header to merge into its own
        # request builder" -- false. `providers/http.py::call_openai_chat`
        # (the `or:`/Databricks-CHAT wire path) does `headers.update(
        # extra_headers)` just like every other dialect's own call
        # function -- it merges WHATEVER headers it's handed, unconditionally,
        # with no per-header allowlist of its own. `extra_headers` was being
        # set here regardless of route, so `--betas` was actually sending
        # `anthropic-beta` to OpenRouter and Databricks-chat too. Gated
        # here instead, at the one place that already knows the route:
        # Anthropic-family means `ant:` (`model_ref.provider ==
        # "anthropic"`) or Databricks Claude PASSTHROUGH specifically
        # (`provider == "databricks" and dialect == "anthropic-passthrough"`
        # -- excludes an ordinary Databricks-hosted non-Claude model, which
        # is chat-dialect and goes through the exact same `call_openai_chat`
        # an `or:` request does).
        is_anthropic_family = model_ref.provider == "anthropic" or (
            model_ref.provider == "databricks" and model_ref.dialect == "anthropic-passthrough")
        if is_anthropic_family:
            extra_headers = {**(extra_headers or {}), "anthropic-beta": ",".join(cli_flags["betas"])}

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
    # finding 1 (W6a): remember whether a log already exists on disk for
    # this id -- i.e. whether -c/--resume/--session-id/--fork-session
    # handed us a PRE-EXISTING session -- before `SessionLog` touches
    # anything. `--no-session-persistence` must only ever erase a log
    # THIS process created; `_pre_existing_log_size` is the exact byte
    # length to truncate back to for one it merely resumed.
    _pre_existing_log_path = (
        agent_sessions.sessions_dir(cwd) / f"{resolved_session_id}.jsonl") if resolved_session_id else None
    _pre_existing_log = bool(_pre_existing_log_path and _pre_existing_log_path.is_file())
    _pre_existing_log_size = _pre_existing_log_path.stat().st_size if _pre_existing_log else 0
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
        extra_plugin_roots=plugin_roots,
    )
    session = Session(
        cwd=cwd, model_ref=model_ref, model_profile=model_profile, creds=creds, state_dir=state_dir,
        model_label=model_ref.raw, session_context=ctx, small_model_ref=small_ref,
        small_model_effort=small_effort, session_log=session_log,
        max_turns=max_turns, openrouter_base_url=openrouter_base_url, effort=effort,
        effort_source=effort_source,
        extra_headers=extra_headers, permission_engine=permission_engine,
        session_catalog=session_catalog, mcp_manager=mcp_manager, hook_runner=hook_runner,
        agents=discovered_agents, routes=routes, agent_type_restriction=agent_type_restriction,
        roles=persisted_roles, cli_roles=cli_roles, cli_flags=cli_flags, settings=settings,
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
    # H11 Part B: same belt-and-suspenders atexit safety net for a cc:
    # session's own claude subprocess -- a no-op session.close_cc() when
    # cc: was never used.
    atexit.register(session.close_cc)
    # Halo 2.0.3 round 5c (brief item 3): "stopped when Halo exits unless
    # keep: true" -- same belt-and-suspenders atexit safety net alongside
    # the two just above; a no-op when `halo local serve` was never used
    # this run (an empty/absent registry file).
    atexit.register(_stop_managed_local_servers_quietly)

    command_registry = Registry.discover(cwd, home(), plugin_roots=plugin_roots)
    if not bare:
        # H8 scope E (deferred by H3): every connected MCP server's own
        # prompts become `/mcp__<server>__<prompt>` slash commands, in both
        # -p and the TUI (bootstrap.py reuses this same builder).
        from halo_harness.commands.registry import register_mcp_prompts
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
    debug_timeline.mark("session build")

    return SessionBuild(
        session=session, command_registry=command_registry, tool_registry=frozen_registry, facade=facade,
        mcp_manager=mcp_manager, mcp_notices=mcp_notices, session_catalog=session_catalog, settings=settings,
        claude_json=claude_json, resolved_mode=resolved_mode, routes=routes, state_dir=state_dir,
        model_ref=model_ref, model_profile=model_profile, creds=creds, ctx=ctx, session_log=session_log,
        hook_runner=hook_runner, resume_error=resume_error, agents=discovered_agents,
        pre_existing_log=_pre_existing_log, pre_existing_log_size=_pre_existing_log_size,
    )


def maybe_create_worktree(cwd: Path, cli_flags: dict) -> Path:
    """W4a `-w/--worktree [name]`: the session runs in a FRESH git worktree
    instead of the real working tree -- created here, before anything else
    touches `cwd`, so settings/CLAUDE.md/instructions/tool access are all
    resolved against the worktree from the very first line. Falls back to
    the original `cwd` (one stderr notice, never a hard failure) when this
    isn't a git repo or `git worktree add` itself fails for any reason.

    finding 2 (W6a): pulled out of `run_print_mode` so `tui.bootstrap.
    build_controller` can call the exact same thing before ITS OWN
    `build_session` call -- the TUI used to have no worktree-creation code
    at all, so `halo -w` (without `-p`) silently ran in the real working
    tree. Mutates `cli_flags` in place on success (stashes
    `_worktree_created_path`, the same key `maybe_remove_worktree_on_exit`
    below reads for the exit-time removal logic) and returns the cwd the
    caller should actually use."""
    if cli_flags.get("worktree") is None:
        return cwd
    from halo_harness.config.paths import bridge_home
    from halo_harness.worktree import create_worktree
    wt_path, wt_error = create_worktree(cwd, cli_flags.get("worktree") or None, state_dir=bridge_home())
    if wt_path is not None:
        print(f"halo: --worktree -> {wt_path}", file=sys.stderr)
        # W4a WorktreeCreated: fired once Session (and its hook_runner)
        # exists -- the worktree itself is created before build_session
        # ever runs, so this just carries the path through cli_flags.
        cli_flags["_worktree_created_path"] = str(wt_path)
        return wt_path
    print(f"halo: --worktree could not create a worktree ({wt_error}) -- "
          f"continuing in the current directory", file=sys.stderr)
    return cwd


def _stop_managed_local_servers_quietly() -> None:
    """Halo 2.0.3 round 5c (brief item 3): "stopped when Halo exits unless
    keep: true" -- called from BOTH the explicit `finally:` block below
    AND the `atexit.register` safety net right after `SessionBuild`
    (mirrors `session.job_registry.kill_all`/`session.close_cc`'s own
    belt-and-suspenders pair). Idempotent (an already-empty registry is a
    no-op) and never raises -- a managed server is a nice-to-have cleanup,
    never worth crashing the exit path over."""
    try:
        from halo_harness.providers.local_runtime import stop_all_managed_servers_except_kept
        stop_all_managed_servers_except_kept()
    except Exception:
        pass


def maybe_remove_worktree_on_exit(cli_flags: dict, session) -> bool:
    """W5 (carried from W4a): the session-end half of `-w/--worktree`'s own
    WorktreeRemoved trigger (the other half is the explicit `halo worktree
    rm <path>` command, `worktree_cli.cmd_worktree`). A no-op unless BOTH
    are true: THIS session actually created a worktree
    (`cli_flags["_worktree_created_path"]`, stashed by `run_print_mode`
    right after `create_worktree` succeeds) AND the user opted into
    removing it (config `worktree.remove_on_exit`, default False -- Claude
    Code's own `--worktree` leaves the tree behind by default too, so a
    user can keep inspecting/committing from it after the session ends
    unless they asked otherwise). Pulled out of `run_print_mode`'s own
    `finally` block into this plain function so the decision itself is
    unit-testable without driving a whole print-mode session end to end.
    Returns whether a removal was actually attempted AND succeeded (for
    the caller/a test to assert on; `run_print_mode` itself ignores the
    return value, same as every other best-effort cleanup step there).
    Never raises."""
    wt_created_path = cli_flags.get("_worktree_created_path")
    if not wt_created_path:
        return False
    from halo_harness.theme import get_config_value
    if not get_config_value("worktree.remove_on_exit", default=False):
        return False
    try:
        from halo_harness.worktree import remove_worktree
        removed, reason = remove_worktree(Path(wt_created_path))
        if not removed:
            # review finding 28: a dirty worktree is kept, never force-
            # removed (its uncommitted edits are this -p -w run's own
            # output) -- said plainly instead of a silent no-op, so it is
            # never simply stranded with no indication it is still there.
            if reason == "dirty":
                print(f"halo: kept the worktree at {wt_created_path} -- it has uncommitted changes "
                      f"(remove it yourself with `halo worktree rm {wt_created_path}` once you're "
                      f"done with it)", file=sys.stderr)
            return False
        session._fire_worktree_removed(wt_created_path)
        return True
    except Exception:
        return False


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
    roles_flag: Optional[list] = None,
    name: Optional[str] = None,
    file_specs: Optional[list] = None,
    cli_flags: Optional[dict] = None,
) -> int:
    """Run one turn (`input_format="text"`) or several (`"stream-json"`,
    one turn per entry of `stdin_lines`) in print mode; returns the
    process's exit code (the LAST turn's, for stream-json input)."""
    cwd = Path(cwd).resolve() if cwd else Path.cwd()
    cli_flags = dict(cli_flags or {})
    cwd = maybe_create_worktree(cwd, cli_flags)

    # Halo 2.0.2 W7 round 1 (brief F): "set halo at start and restore the
    # previous title at exit" -- captured/set as early as possible (before
    # even the --session-id/--resume validation below, which can exit
    # this function in just a few lines), restored on EVERY exit path:
    # each early `return 2` below calls `_pm_title_exit`, and the existing
    # outer `finally` further down (already the one place guaranteed to
    # run on every OTHER exit -- a normal return or an exception) restores
    # it last, after every other cleanup. `_pm_prev_title` is None on a
    # platform/terminal this process can't read a title back from (POSIX)
    # -- `restore()` is then a no-op, i.e. "otherwise leave it" (brief).
    from halo_harness.termtitle import get_terminal_title, set_terminal_title
    _pm_prev_title = get_terminal_title()
    set_terminal_title("halo")

    def _pm_title_exit(code: int) -> int:
        if _pm_prev_title is not None:
            set_terminal_title(_pm_prev_title)
        return code

    # must-do: validated FIRST, before anything (incl. MCP) starts -- the
    # old position (right before `Session(...)`, well after `build_manager`
    # already spawned real MCP subprocesses) meant a bad --session-id
    # returned 2 with those servers still running, `close_all()` never
    # called. Validating up front means a bad id never starts them at all,
    # rather than starting-then-cleaning-up.
    if session_id and not agent_sessions.is_valid_session_id(session_id):
        print(f"halo: --session-id must be a valid UUID, got {session_id!r}", file=sys.stderr)
        return _pm_title_exit(2)
    if resume is not None and resume != "" and not continue_:
        # H13 Part C ("--resume <text> picks the unique match or opens the
        # picker filtered"): print mode has no picker to open, so a genuine
        # AMBIGUOUS match (2+ sessions fuzzy-match the given text) is its
        # own honest error -- listing the candidates so the user can be more
        # specific or pass an exact id -- rather than silently guessing the
        # most-recently-modified one the way a bare `resolve_resume` call
        # would. A UNIQUE match, or the classic zero-match error, are both
        # unchanged (still `resolve_resume`'s own contract below).
        matches = agent_sessions.find_resume_matches(cwd, resume)
        if len(matches) > 1:
            print(f"halo: --resume {resume!r} matches {len(matches)} sessions -- "
                  f"be more specific, or pass an exact session id (run without -p to pick interactively):",
                  file=sys.stderr)
            for m in matches[:8]:
                label = m.get("title") or m.get("summary") or "(no summary)"
                print(f"  {m['id']}  {label}", file=sys.stderr)
            return _pm_title_exit(2)
        _resolved_probe, resume_err = agent_sessions.resolve_resume(cwd, resume)
        if _resolved_probe is None and resume_err:
            print(f"halo: --resume: {resume_err}", file=sys.stderr)
            return _pm_title_exit(2)

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
        agent=agent, agents_flag=agents_flag, roles_flag=roles_flag, cli_flags=cli_flags,
    )
    session, frozen_registry, model_ref = (build.session, build.tool_registry, build.model_ref)
    mcp_manager, resolved_mode, session_log = build.mcp_manager, build.resolved_mode, build.session_log
    _pre_existing_log, _pre_existing_log_size = build.pre_existing_log, build.pre_existing_log_size
    family = model_family(model_ref.model)
    attach_cli_files(session, file_specs, cwd=cwd)

    if mcp_manager is None and build.mcp_notices:
        print(f"halo: {build.mcp_notices[0]}", file=sys.stderr)
    elif verbose:
        for n in build.mcp_notices:
            print(f"[halo] mcp: {n}", file=sys.stderr)

    if verbose:
        print(f"[halo] model={model_ref.raw} provider={model_ref.provider} "
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
                # Halo 2.0.1 W2a: `effort_sent` mirrors the status bar's own
                # chip logic (`Session.status_event`'s `effort_tag`) -- a
                # route forcing an explicit `reasoning_effort_with_tools`
                # override is what's ACTUALLY sent on essentially every
                # real (tool-carrying) turn, so it wins over the plain
                # configured value there too.
                from halo_harness.providers.profiles import effort_display_override
                effort_sent = effort_display_override(session.provider_profile) or session.effort
                return StreamJsonSink(
                    session_id=session_log.session_id, cwd=str(cwd), model=model_ref.raw,
                    permission_mode=resolved_mode, tools=frozen_registry.names(), mcp_servers=mcp_servers_status,
                    slash_commands=slash_names, include_partial_messages=include_partial_messages,
                    max_budget_usd=max_budget_usd, permission_denials=session.permission_denials,
                    json_schema=json_schema, effort=session.effort_requested, effort_sent=effort_sent,
                    hook_events_fn=(session.drain_hook_events if cli_flags.get("include_hook_events") else None),
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
                    print("halo: --input-format stream-json requires at least one user message on stdin",
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
                    name="halo-stdin-reader",
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
                    if final_prompt is not None:
                        extra_ctx = session.fire_user_prompt_expansion(turn_text, final_prompt)
                        if extra_ctx:
                            session.log.append_snapshot([{"type": "text", "text": extra_ctx}], kind="hook_context")
                    # W5 (carried from W4a): `finish=False` + an explicit
                    # `sink.finish()` (byte-identical to the old single
                    # `consume(...)` call when `--prompt-suggestions` is
                    # off -- `consume`'s own `finish` kwarg does exactly
                    # this split internally) opens the same gap the
                    # single-turn `-p` path already has, so `--prompt-
                    # suggestions` now also works per-turn in THIS loop
                    # (previously left for a follow-up -- see the worker
                    # report).
                    sink.consume(session.turn(final_prompt or turn_text), finish=False)
                    _maybe_add_prompt_suggestion(session, sink, cli_flags)
                    exit_code = sink.finish()
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
                print("halo: --input-format stream-json requires at least one user message on stdin",
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
        if final_prompt is not None:
            extra_ctx = session.fire_user_prompt_expansion(prompt_text, final_prompt)
            if extra_ctx:
                session.log.append_snapshot([{"type": "text", "text": extra_ctx}], kind="hook_context")
        try:
            # H9 whole-tree review finding 9: `finish=False` -- the turn's
            # events are drained into `sink` but nothing is printed and no
            # exit code decided yet; `_drain_background_jobs_for_print_mode`
            # folds in any background-job notice and is the ONE place that
            # calls `sink.finish()` for this whole process (see its own
            # docstring for the multi-JSON-object/exit-code bug this closes).
            sink.consume(session.turn(final_prompt or prompt_text), finish=False)
            _maybe_add_prompt_suggestion(session, sink, cli_flags)
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
        # H11 Part B: never leave a cc: session's own claude subprocess
        # running past this process's own exit either (a no-op when cc:
        # was never used this session).
        try:
            session.close_cc()
        except Exception:
            pass
        # H8 scope A: "jobs killed on quit" -- never leave a background
        # Bash job (or a timed-out foreground command moved to one)
        # running past this process's own exit.
        try:
            session.job_registry.kill_all()
        except Exception:
            pass
        # Halo 2.0.3 round 5c (brief item 3): a model served by `halo local
        # serve`/the `/local` dialog's `s` key must never outlive the
        # session that started it either, unless `keep: true`.
        _stop_managed_local_servers_quietly()
        if mcp_manager is not None:
            mcp_manager.close_all()
        if cli_flags.get("no_session_persistence"):
            # W4a `--no-session-persistence`: "sessions will not be saved to
            # disk and cannot be resumed" -- the session still runs/logs
            # normally (SessionLog's own write path is deeply embedded
            # elsewhere; bypassing it is a bigger change than this flag is
            # worth), but nothing it wrote survives past this process's own
            # exit: the log file/directory and its index entry are removed
            # here, last, after every other finally-block cleanup above.
            #
            # finding 1 (W6a): that used to run unconditionally, so
            # combined with -c/--resume/--session-id/--fork-session it
            # deleted a PRE-EXISTING session -- its transcript, sub-agent
            # logs and rewind shadow repo -- that this process never
            # created. A resumed log is now only ever truncated back to
            # the exact byte length it had before this run touched it;
            # its index entry and session dir (sub-agent logs, shadow
            # repo) are left alone. Only a log THIS run created from
            # scratch still gets the full delete.
            try:
                import shutil as _shutil
                if _pre_existing_log:
                    with open(session_log.path, "r+b") as f:
                        f.truncate(_pre_existing_log_size)
                else:
                    agent_sessions.forget_session(cwd, session_log.session_id)
                    if session_log.path.exists():
                        session_log.path.unlink()
                    session_dir = session_log.dir / session_log.session_id
                    if session_dir.is_dir():
                        _shutil.rmtree(session_dir, ignore_errors=True)
            except Exception:
                pass
        # W5 (carried from W4a): `-w/--worktree` created a FRESH worktree
        # for THIS session -- removed here, last, only when the user opted
        # in. Pulled into its own function (below) so the decision itself
        # is unit-testable without driving a whole print-mode session.
        maybe_remove_worktree_on_exit(cli_flags, session)
        # Halo 2.0.2 W7 round 1 (brief F): restores whatever title was
        # there before this run started (a no-op when nothing was
        # captured) -- last, after every other cleanup above, on every
        # exit path this outer `finally` already covers (a normal
        # return, an early stream-json `return 2`, or an exception).
        if _pm_prev_title is not None:
            set_terminal_title(_pm_prev_title)
