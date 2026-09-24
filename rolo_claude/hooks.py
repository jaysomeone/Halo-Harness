"""rolo_claude.hooks -- Claude Code's hooks protocol (H4 scope A), per the
plan's D7/D-CFG "Hooks refinements" sections and
`docs/harness/claude-code-2.1.281-binary-facts.md` sec.2/10: `HookDef` (one
configured hook entry), `HookResult` (one handler invocation's raw exit
code/stdout/stderr), `HookOutcome` (the interpreted -- and, when several
hooks matched, COMBINED -- decision a call site actually acts on),
`normalize_hooks` (settings.hooks' already-merged, already-trust-filtered
shape -> `{event: [HookDef, ...]}`, plus plugin `hooks/hooks.json` and
skill/agent frontmatter loaders), and `HookRunner` (matcher + `if` +
dedup/once filtering, parallel execution across 5 handler types, and the
Stop-cap/SessionEnd-budget special cases).

No safety heuristics live here either (rolo's "no cyber blocks" decision,
same as permissions.py) -- a hook is entirely the USER's own gate; this
module only implements the PROTOCOL Claude Code defines for running one.

Call sites (agent/loop.py) are this module's only caller; see that file's
own comments for exactly where each event fires.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import os
import re
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

log = logging.getLogger("bridge")

# ---- constants [binary-facts sec.2] ----------------------------------------
_ENV_FILE_EVENTS = frozenset({"SessionStart", "Setup", "CwdChanged", "FileChanged"})

DEFAULT_COMMAND_TIMEOUT_S = 600.0
DEFAULT_PROMPT_TIMEOUT_S = 30.0
DEFAULT_AGENT_TIMEOUT_S = 60.0
SESSION_END_DEFAULT_BUDGET_S = 1.5
SESSION_END_MAX_BUDGET_S = 60.0
STOP_HOOK_BLOCK_CAP_DEFAULT = 8

REASON_CAP = 2000
SYSTEM_MESSAGE_CAP = 4000
ADDITIONAL_CONTEXT_CAP = 8000

# D-CFG: "Not emitted in v1 (configs accepted, ignored with a debug line)" --
# a settings.json/hooks.json entry for one of these parses fine (normalize_
# hooks doesn't special-case the event name at all) but no call site in
# agent/loop.py ever calls HookRunner.run() for it yet.
NOT_EMITTED_V1 = frozenset({
    "Setup", "UserPromptExpansion", "MessageDisplay", "TaskCreated", "TaskCompleted",
    "StopFailure", "InstructionsLoaded", "ConfigChange", "CwdChanged", "DirectoryAdded",
    "FileChanged", "WorktreeCreated", "WorktreeRemoved", "PreModelSwitch", "PostModelSwitch",
    "ElicitationRequest", "ElicitationResponse",
})


def _env_positive_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw:
        try:
            v = int(raw)
            if v > 0:
                return v
        except ValueError:
            pass
    return default


def default_timeout_s(hook_type: str) -> float:
    if hook_type == "prompt":
        return DEFAULT_PROMPT_TIMEOUT_S
    if hook_type == "agent":
        return DEFAULT_AGENT_TIMEOUT_S
    return DEFAULT_COMMAND_TIMEOUT_S  # command | http | mcp_tool


# =============================================================================
# HookDef -- one configured hook entry (D-CFG: "HookDef carries type command|
# http|mcp_tool|prompt|agent, matcher, if rule, args ..., shell ..., url/
# headers/allowedEnvVars, server/tool/input, prompt/model, timeout_s, once,
# async, statusMessage, source + plugin_root, scope global|skill|agent").
# =============================================================================

@dataclass
class HookDef:
    type: str  # "command" | "http" | "mcp_tool" | "prompt" | "agent"
    matcher: Optional[str] = None
    if_rule: Optional[str] = None
    args: Optional[list] = None          # command: exec form, no shell
    command: Optional[str] = None        # command: shell text (Claude Code's usual settings.json shape)
    shell: Optional[str] = None          # command: None (platform default) | "bash" | "powershell"
    url: Optional[str] = None            # http
    headers: dict = field(default_factory=dict)
    allowed_env_vars: list = field(default_factory=list)
    server: Optional[str] = None         # mcp_tool
    tool: Optional[str] = None           # mcp_tool
    input: dict = field(default_factory=dict)  # mcp_tool: base arguments (payload is merged in)
    prompt: Optional[str] = None         # prompt | agent: template, "$ARGUMENTS" -> payload JSON
    model: Optional[str] = None          # prompt | agent: informational only (always the small model)
    timeout_s: Optional[float] = None
    once: bool = False
    run_async: bool = False
    status_message: Optional[str] = None
    source: str = ""                     # settings layer name | "plugin" | "skill" | "agent"
    plugin_root: Optional[str] = None
    scope: str = "global"                # "global" | "skill" | "agent"

    def effective_timeout_s(self) -> float:
        return float(self.timeout_s) if self.timeout_s else default_timeout_s(self.type)

    def dedup_key(self):
        """D-CFG: "dedup by (type, command, args, shell, url, prompt)" --
        also used as the `once` identity key (content, not matcher/if, is
        what "the same hook" means for both rules)."""
        return (self.type, self.command, tuple(self.args or ()), self.shell, self.url, self.prompt,
                self.server, self.tool)


# =============================================================================
# Matcher semantics [D-CFG]: omitted/""/"*" -> all; ^[A-Za-z0-9_\-,|]*$ ->
# exact list split on ","/"|"; else unanchored regex (invalid -> skip + warn).
# =============================================================================

_EXACT_LIST_RE = re.compile(r"^[A-Za-z0-9_\-,|]*$")


def matcher_matches(matcher: Optional[str], value: Optional[str]) -> "tuple[bool, Optional[str]]":
    """(matches, warning_or_None)."""
    if not matcher or matcher == "*":
        return True, None
    if _EXACT_LIST_RE.match(matcher):
        names = [n for n in re.split(r"[,|]", matcher) if n]
        if not names:
            return True, None
        return (value in names if value is not None else False), None
    try:
        rx = re.compile(matcher)
    except re.error as e:
        return False, f"invalid hook matcher regex {matcher!r}: {e}"
    return (bool(rx.search(value)) if value is not None else False), None


def if_rule_matches(if_rule: Optional[str], tool_name: Optional[str], tool_input: Optional[dict],
                     tool, *, cwd) -> bool:
    """D-CFG: "`if` uses `parse_rule` on tool events" -- reuses permissions.py's
    own grammar/matching (never a second, divergent implementation) so the
    exact same rule syntax works here as in settings.json permissions. A
    non-tool event (tool_name is None) or an absent `if_rule` always
    matches; an unparseable rule never matches (fails closed -- a hook the
    user gated must not fire just because its own gate was malformed)."""
    if not if_rule or tool_name is None:
        return True
    from rolo_claude.permissions import (
        PermissionEngine, bash_deny_or_ask_matches, parse_rule, powershell_deny_or_ask_matches,
    )
    rule = parse_rule(if_rule, source="hook_if", base_dir=Path(cwd), action="ask")
    if rule.kind == "invalid":
        log.debug("hook `if` rule %r is invalid (%s) -- treating as no match", if_rule, rule.error)
        return False
    tool_input = tool_input or {}
    if tool_name == "Bash" and rule.tool == "Bash" and rule.kind in ("exact", "prefix", "glob"):
        return bash_deny_or_ask_matches(tool_input.get("command", ""), [rule]) is not None
    if tool_name == "PowerShell" and rule.tool == "PowerShell" and rule.kind in ("exact", "prefix", "glob"):
        return powershell_deny_or_ask_matches(tool_input.get("command", ""), [rule]) is not None
    engine = PermissionEngine(cwd=cwd)
    if rule.kind == "path":
        return engine._path_rule_hit([rule], tool_name, tool_input, tool, action="ask") is not None
    return engine._rule_matches(rule, tool_name, tool_input, tool, action="ask")


# =============================================================================
# stdin payload builder [D-CFG]: common fields on every event, plus each
# event's own extras (the caller passes those as `extra`).
# =============================================================================

def build_payload(event: str, *, session_id: str, transcript_path, cwd, scratchpad_dir=None,
                   permission_mode: Optional[str] = None, effort: Optional[str] = None,
                   prompt_id: Optional[str] = None, agent_id: Optional[str] = None,
                   agent_type: Optional[str] = None, extra: Optional[dict] = None) -> dict:
    payload: dict = {
        "session_id": session_id, "transcript_path": str(transcript_path), "cwd": str(cwd),
        "hook_event_name": event,
    }
    if scratchpad_dir is not None:
        payload["scratchpad_dir"] = str(scratchpad_dir)
    if prompt_id is not None:
        payload["prompt_id"] = prompt_id
    if permission_mode is not None:
        payload["permission_mode"] = permission_mode
    if effort is not None:
        payload["effort"] = {"level": effort}
    if agent_id is not None:
        payload["agent_id"] = agent_id
    if agent_type is not None:
        payload["agent_type"] = agent_type
    if extra:
        payload.update(extra)
    return payload


def env_file_path(session_id: str) -> Path:
    """`~/.rolo-claude/session-env/<session_id>.sh` [D-CFG] -- the
    `CLAUDE_ENV_FILE` a SessionStart/Setup/CwdChanged/FileChanged hook's
    own `export NAME=value` lines get appended to."""
    from rolo_claude.config.paths import bridge_home
    return bridge_home() / "session-env" / f"{session_id}.sh"


def read_env_file_exports(path) -> dict:
    """Parse simple `export NAME=value` lines (one per line; an optional
    matching pair of quotes around `value` is stripped -- no general shell
    parsing) out of a `CLAUDE_ENV_FILE` a hook wrote to. `{}` for a
    missing/empty/unparseable file, never raises -- the caller (agent/
    loop.py, after a SessionStart/Setup/CwdChanged/FileChanged `run()`)
    folds the result into the session's own tool env."""
    out: dict = {}
    path = Path(path)
    try:
        if not path.exists():
            return out
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return out
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("export "):
            continue
        rest = line[len("export "):].strip()
        if "=" not in rest:
            continue
        name, _, value = rest.partition("=")
        name, value = name.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if name:
            out[name] = value
    return out


# =============================================================================
# normalize_hooks -- settings.hooks' already-merged, already-trust-filtered
# shape (config/settings.py's `_merge_layers` drops an untrusted project/
# local layer's `hooks` key entirely before this module ever sees it, and
# tags every surviving matcher-group with `_source`) -> {event: [HookDef]}.
# Plugin `hooks/hooks.json` and skill/agent frontmatter use the SAME shape,
# so their loaders below just call this with a different `default_source`/
# `scope`/`plugin_root`.
# =============================================================================

def normalize_hooks(hooks_by_event: dict, *, default_source: str = "settings",
                     plugin_root: Optional[str] = None, scope: str = "global") -> dict:
    out: dict = {}
    for event, groups in (hooks_by_event or {}).items():
        if not isinstance(groups, list):
            continue
        defs: list = []
        for group in groups:
            if not isinstance(group, dict):
                continue
            matcher = group.get("matcher")
            if_rule = group.get("if")
            source = group.get("_source") or default_source
            entries = group.get("hooks")
            if not isinstance(entries, list):
                continue
            for h in entries:
                if not isinstance(h, dict):
                    continue
                defs.append(HookDef(
                    type=h.get("type", "command"), matcher=matcher, if_rule=if_rule,
                    args=h.get("args") if isinstance(h.get("args"), list) else None,
                    command=h.get("command"), shell=h.get("shell"),
                    url=h.get("url"), headers=dict(h.get("headers") or {}),
                    allowed_env_vars=list(h.get("allowedEnvVars") or []),
                    server=h.get("server"), tool=h.get("tool"), input=dict(h.get("input") or {}),
                    prompt=h.get("prompt"), model=h.get("model"),
                    timeout_s=h.get("timeout"), once=bool(h.get("once", False)),
                    run_async=bool(h.get("async", False)), status_message=h.get("statusMessage"),
                    source=source, plugin_root=plugin_root, scope=scope,
                ))
        if defs:
            out.setdefault(event, []).extend(defs)
    return out


def merge_hook_maps(*maps: dict) -> dict:
    out: dict = {}
    for m in maps:
        for event, defs in (m or {}).items():
            out.setdefault(event, []).extend(defs)
    return out


def _substitute_plugin_root(value, root: str):
    if isinstance(value, str):
        return value.replace("${CLAUDE_PLUGIN_ROOT}", root)
    if isinstance(value, list):
        return [_substitute_plugin_root(v, root) for v in value]
    if isinstance(value, dict):
        return {k: _substitute_plugin_root(v, root) for k, v in value.items()}
    return value


def load_plugin_hooks(plugin_root) -> dict:
    """`<plugin_root>/hooks/hooks.json` (D-CFG: "plugin hooks/hooks.json
    (${CLAUDE_PLUGIN_ROOT})") -- same `{event: [{matcher, hooks:[...]}]}`
    shape as settings.json's own `hooks` key, optionally wrapped in one more
    `{"hooks": {...}}` level (Claude Code accepts both). Never raises --
    `{}` for a missing/malformed file."""
    path = Path(plugin_root) / "hooks" / "hooks.json"
    if not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, dict):
        return {}
    hooks_block = raw.get("hooks", raw)
    if not isinstance(hooks_block, dict):
        return {}
    substituted = _substitute_plugin_root(hooks_block, str(plugin_root))
    return normalize_hooks(substituted, default_source="plugin", plugin_root=str(plugin_root), scope="global")


def hooks_from_frontmatter(fm: dict, *, scope: str, source: str) -> dict:
    """Skill/agent frontmatter `hooks:` (D-CFG: "skill/agent frontmatter
    hooks: with scope"). Best-effort ONLY: `config/frontmatter.py`'s YAML
    subset cannot represent a real settings.json-shaped `hooks:` block (a
    list of dicts each holding ANOTHER list of dicts is past its one-
    nested-mapping-level ceiling), so this fires only for a frontmatter
    value that HAPPENS to already be dict-shaped (built programmatically,
    or a future frontmatter.py enhancement) -- `{}` (never raises) for
    anything else, same contract as `load_plugin_hooks`."""
    hooks_block = fm.get("hooks") if isinstance(fm, dict) else None
    if not isinstance(hooks_block, dict):
        return {}
    return normalize_hooks(hooks_block, default_source=source, scope=scope)


# =============================================================================
# HookResult -- one handler invocation's raw outcome (before interpretation).
# =============================================================================

@dataclass
class HookResult:
    exit_code: int
    stdout: str = ""
    stderr: str = ""


def _shell_argv(hook: HookDef, command_text: str) -> list:
    if hook.shell == "powershell":
        return ["powershell", "-NoProfile", "-NonInteractive", "-Command", command_text]
    if hook.shell == "bash" or sys.platform != "win32":
        if sys.platform == "win32":
            from rolo_claude.config.paths import git_bash
            bash = git_bash()
            if bash is not None:
                return [str(bash), "-c", command_text]
            log.warning("hook requested shell=bash but no Git Bash was found -- falling back to PowerShell")
            return ["powershell", "-NoProfile", "-NonInteractive", "-Command", command_text]
        return ["/bin/sh", "-c", command_text]
    # default (no explicit `shell`): Git Bash on win32, /bin/sh on POSIX [D-CFG].
    from rolo_claude.config.paths import git_bash
    bash = git_bash()
    if bash is not None:
        return [str(bash), "-c", command_text]
    log.warning("hook: no Git Bash found on PATH -- falling back to PowerShell")
    return ["powershell", "-NoProfile", "-NonInteractive", "-Command", command_text]


def run_command_hook(hook: HookDef, payload: dict, *, cwd, env: dict) -> HookResult:
    """`args` (exec form) spawns WITHOUT a shell; else `command` (shell
    text) runs through `shell: powershell` or the platform default (Git
    Bash `bash -c` on win32, `/bin/sh -c` on POSIX) [D-CFG]. stdin = the
    payload JSON; timeout -> process-tree kill (subprocess.run's own,
    non-blocking -- exit code 1, never a hard block)."""
    timeout_s = hook.effective_timeout_s()
    stdin_bytes = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    try:
        argv = [str(a) for a in hook.args] if hook.args else _shell_argv(hook, hook.command or "")
        proc = subprocess.run(argv, input=stdin_bytes, capture_output=True, cwd=str(cwd),
                               env=env, timeout=timeout_s)
        return HookResult(proc.returncode, proc.stdout.decode("utf-8", "replace"),
                           proc.stderr.decode("utf-8", "replace"))
    except subprocess.TimeoutExpired:
        return HookResult(1, "", f"hook timed out after {timeout_s:.0f}s")
    except OSError as e:
        return HookResult(1, "", f"failed to launch hook: {e}")


def _interpolate_allowed_env(value, allowed: list, env: dict):
    if not isinstance(value, str) or "${" not in value:
        return value

    def _sub(m: "re.Match") -> str:
        name = m.group(1)
        if name in (allowed or ()) and name in env:
            return env[name]
        return m.group(0)  # credential names (not in `allowed`) never interpolate

    return re.sub(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}", _sub, value)


def run_http_hook(hook: HookDef, payload: dict, *, timeout_s: float, env: dict) -> HookResult:
    """POST JSON; headers interpolate ONLY `allowedEnvVars` names (a
    credential-named var never interpolates, whether allowed or not is
    irrelevant -- the CALLER is responsible for never putting a credential
    name in `allowedEnvVars` in the first place, same trust model as the
    rest of this harness's env handling)."""
    import urllib.error
    import urllib.request

    headers = {k: _interpolate_allowed_env(v, hook.allowed_env_vars, env) for k, v in (hook.headers or {}).items()}
    headers.setdefault("Content-Type", "application/json")
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(hook.url or "", data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            text = resp.read().decode("utf-8", "replace")
            return HookResult(0, text, "")
    except urllib.error.HTTPError as e:
        try:
            body_text = e.read().decode("utf-8", "replace")
        except Exception:
            body_text = ""
        return HookResult(1, body_text, f"HTTP {e.code}: {e.reason}")
    except (urllib.error.URLError, OSError, ValueError) as e:
        return HookResult(1, "", f"http hook failed: {e}")


def run_mcp_tool_hook(hook: HookDef, payload: dict, *, mcp_manager, timeout_s: float, abort=None) -> HookResult:
    """`mcp.call(server, tool, input ∪ payload)` [D-CFG]. `isError` maps to
    a non-blocking failure (exit 1) -- an MCP-backed hook signals a real
    block through its OWN JSON reply's `decision:"block"`, same as any
    other handler type, not by raising."""
    if mcp_manager is None:
        return HookResult(1, "", "no MCP manager is available for this hook")
    arguments = dict(hook.input or {})
    arguments["payload"] = payload
    try:
        result = mcp_manager.call(hook.server, hook.tool, arguments, timeout=timeout_s, abort=abort)
    except Exception as e:
        return HookResult(1, "", f"mcp_tool hook failed: {type(e).__name__}: {e}")
    is_error = bool(getattr(result, "isError", False) or getattr(result, "is_error", False))
    text_parts = [getattr(item, "text", "") for item in (getattr(result, "content", None) or [])
                  if isinstance(getattr(item, "text", None), str)]
    text = "\n".join(text_parts)
    return HookResult(1 if is_error else 0, text, text if is_error else "")


def run_prompt_hook(hook: HookDef, payload: dict, *, prompt_caller: Optional[Callable], timeout_s: float) -> HookResult:
    """`prompt`/`agent`: `$ARGUMENTS` -> payload JSON, through the small
    model (`prompt_caller`, built by `build_prompt_caller` below) --
    expects a `{"ok":.., "reason":..}` reply, but any plain-text reply is
    still honoured as non-JSON `additionalContext` (for SessionStart-like
    events) or simply ignored otherwise. `prompt_caller is None` (no small
    model configured/reachable) degrades to a silent no-op allow -- a
    missing model must never hard-block every turn."""
    if prompt_caller is None:
        return HookResult(0, "", "")
    template = hook.prompt or "$ARGUMENTS"
    text = template.replace("$ARGUMENTS", json.dumps(payload, ensure_ascii=False))
    try:
        reply = prompt_caller(text, timeout_s)
    except Exception as e:
        return HookResult(1, "", f"prompt hook failed: {type(e).__name__}: {e}")
    return HookResult(0, reply or "", "")


# =============================================================================
# HookOutcome -- the interpreted (and, for several matched hooks, COMBINED)
# decision a call site acts on. [D-CFG]: "exit 0 -> JSON iff stdout is
# `{…}`, plain stdout -> context only for UserPromptSubmit/
# UserPromptExpansion/SessionStart/PostModelSwitch; exit 2 blocks regardless
# of JSON (PermissionRequest ignored; Stop prevents stopping, cap 8); other
# non-zero non-blocking, JSON honoured; caps reason 2000/systemMessage 4000/
# additionalContext 8000 chars; deny > defer > ask > allow, contexts
# concatenated, last updatedInput wins; updatedPermissions[setMode] -> mode
# change."
# =============================================================================

@dataclass
class HookOutcome:
    blocked: bool = False
    block_reason: str = ""
    permission_decision: Optional[str] = None  # "allow" | "deny" | "ask" | "defer"
    permission_decision_reason: str = ""
    updated_input: Optional[dict] = None
    additional_context: str = ""
    system_messages: list = field(default_factory=list)
    continue_: bool = True
    set_mode: Optional[str] = None
    raw_json: Optional[dict] = None


_CONTEXT_ONLY_EVENTS = frozenset({"UserPromptSubmit", "UserPromptExpansion", "SessionStart", "PostModelSwitch"})


def _apply_json_fields(outcome: HookOutcome, parsed: dict, event: str) -> None:
    if parsed.get("continue") is False:
        outcome.continue_ = False
    stop_reason = parsed.get("stopReason")
    if isinstance(stop_reason, str) and stop_reason:
        outcome.block_reason = stop_reason[:REASON_CAP]
    system_message = parsed.get("systemMessage")
    if isinstance(system_message, str) and system_message:
        outcome.system_messages.append(system_message[:SYSTEM_MESSAGE_CAP])
    if parsed.get("decision") == "block":
        outcome.blocked = True
        if not outcome.block_reason:
            outcome.block_reason = str(parsed.get("reason") or "blocked by hook")[:REASON_CAP]
    hso = parsed.get("hookSpecificOutput")
    if isinstance(hso, dict):
        pd = hso.get("permissionDecision")
        # PreToolUse-only vocabulary member -- elsewhere, "defer" means
        # exactly what "no decision at all" means (never overrides).
        if pd == "defer" and event != "PreToolUse":
            pd = None
        if pd in ("allow", "deny", "ask", "defer"):
            outcome.permission_decision = pd
        reason = hso.get("permissionDecisionReason")
        if isinstance(reason, str) and reason:
            outcome.permission_decision_reason = reason[:REASON_CAP]
        updated_input = hso.get("updatedInput")
        if isinstance(updated_input, dict):
            outcome.updated_input = updated_input
        extra_ctx = hso.get("additionalContext")
        if isinstance(extra_ctx, str) and extra_ctx:
            merged = f"{outcome.additional_context}\n{extra_ctx}" if outcome.additional_context else extra_ctx
            outcome.additional_context = merged[:ADDITIONAL_CONTEXT_CAP]
        updated_perms = hso.get("updatedPermissions")
        if isinstance(updated_perms, dict):
            mode = updated_perms.get("setMode")
            if isinstance(mode, str) and mode:
                outcome.set_mode = mode


def interpret_hook_result(event: str, result: HookResult) -> HookOutcome:
    stdout_stripped = (result.stdout or "").strip()
    parsed: Optional[dict] = None
    if stdout_stripped.startswith("{"):
        try:
            candidate = json.loads(stdout_stripped)
            if isinstance(candidate, dict):
                parsed = candidate
        except ValueError:
            parsed = None

    outcome = HookOutcome(raw_json=parsed)

    if result.exit_code == 2:
        outcome.blocked = True
        outcome.block_reason = (result.stderr or "").strip()[:REASON_CAP] or "blocked by hook (exit 2)"
        if parsed is not None:
            _apply_json_fields(outcome, parsed, event)
        return outcome

    if parsed is not None:
        _apply_json_fields(outcome, parsed, event)
        return outcome

    if result.exit_code == 0 and stdout_stripped and event in _CONTEXT_ONLY_EVENTS:
        outcome.additional_context = stdout_stripped[:ADDITIONAL_CONTEXT_CAP]

    return outcome


_PERM_PRIORITY = {"deny": 3, "defer": 2, "ask": 1, "allow": 0}


def combine_outcomes(outcomes: list) -> HookOutcome:
    """D-CFG: "deny > defer > ask > allow, contexts concatenated, last
    updatedInput wins" -- `outcomes` is iterated in the SAME order the
    hooks themselves were matched/run in (parallel execution doesn't
    reorder the RESULT list, only the wall-clock timing of getting there),
    so "last" is well-defined."""
    combined = HookOutcome()
    contexts: list = []
    for o in outcomes:
        if o.blocked:
            combined.blocked = True
            if not combined.block_reason:
                combined.block_reason = o.block_reason
        if not o.continue_:
            combined.continue_ = False
            if o.block_reason and not combined.block_reason:
                combined.block_reason = o.block_reason
        if o.permission_decision is not None:
            if (combined.permission_decision is None
                    or _PERM_PRIORITY.get(o.permission_decision, -1) > _PERM_PRIORITY.get(combined.permission_decision, -1)):
                combined.permission_decision = o.permission_decision
                combined.permission_decision_reason = o.permission_decision_reason
        if o.updated_input is not None:
            combined.updated_input = o.updated_input  # last wins (iteration order)
        if o.additional_context:
            contexts.append(o.additional_context)
        combined.system_messages.extend(o.system_messages)
        if o.set_mode is not None:
            combined.set_mode = o.set_mode  # last wins (iteration order)
    combined.additional_context = "\n\n".join(contexts)[:ADDITIONAL_CONTEXT_CAP]
    return combined


# =============================================================================
# HookRunner -- matcher + `if` + dedup/once filtering, parallel execution,
# and the Stop-cap/SessionEnd-budget special cases. One instance lives for a
# session's whole lifetime (agent/loop.py's Session owns it), like
# PermissionEngine.
# =============================================================================

class HookRunner:
    def __init__(self, hooks_by_event: dict, *, cwd, session_id: str, transcript_path,
                 scratchpad_dir=None, effective_env: Optional[dict] = None,
                 permission_mode: Optional[str] = None, effort: Optional[str] = None,
                 mcp_manager=None, prompt_caller: Optional[Callable[[str, float], str]] = None,
                 enabled: bool = True, agent_id: Optional[str] = None, agent_type: Optional[str] = None):
        self.hooks_by_event = hooks_by_event
        self.cwd = Path(cwd)
        self.session_id = session_id
        self.transcript_path = transcript_path
        self.scratchpad_dir = scratchpad_dir
        self.effective_env = dict(effective_env or {})
        self.permission_mode = permission_mode
        self.effort = effort
        self.mcp_manager = mcp_manager
        self.prompt_caller = prompt_caller
        self.enabled = enabled
        self.agent_id = agent_id
        self.agent_type = agent_type
        self._once_ran: set = set()
        self.stop_block_count = 0

    def has_hooks(self, event: str) -> bool:
        """Cheap presence check a call site can use to skip building a
        payload at all when nothing is configured for `event`."""
        return self.enabled and bool(self.hooks_by_event.get(event))

    def payload(self, event: str, *, extra: Optional[dict] = None, prompt_id: Optional[str] = None) -> dict:
        return build_payload(
            event, session_id=self.session_id, transcript_path=self.transcript_path, cwd=self.cwd,
            scratchpad_dir=self.scratchpad_dir, permission_mode=self.permission_mode, effort=self.effort,
            prompt_id=prompt_id, agent_id=self.agent_id, agent_type=self.agent_type, extra=extra,
        )

    def _env_for(self, hook: HookDef, event: Optional[str] = None) -> dict:
        env = dict(self.effective_env)
        env["CLAUDE_PROJECT_DIR"] = str(self.cwd)
        env["CLAUDE_CODE_REMOTE"] = "false"
        if hook.plugin_root:
            env["CLAUDE_PLUGIN_ROOT"] = hook.plugin_root
        if self.effort:
            env["CLAUDE_EFFORT"] = self.effort
        if event in _ENV_FILE_EVENTS:
            # D-CFG: "CLAUDE_ENV_FILE for SessionStart/Setup/CwdChanged/
            # FileChanged, whose `export NAME=value` lines then feed the
            # Bash tool env" -- the file itself doesn't need to exist yet
            # (a hook script APPENDS to it); the CALLER (agent/loop.py,
            # after `run()` returns for one of these events) is what
            # actually reads it back via `read_env_file_exports` and folds
            # it into the session's tool env.
            path = env_file_path(self.session_id)
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
            except OSError:
                pass
            env["CLAUDE_ENV_FILE"] = str(path)
        return env

    def matched_hooks(self, event: str, *, matched: Optional[str], tool_name: Optional[str],
                       tool_input: Optional[dict], tool) -> list:
        candidates = self.hooks_by_event.get(event) or []
        out: list = []
        seen_dedup: set = set()
        for hook in candidates:
            ok, warning = matcher_matches(hook.matcher, matched)
            if warning:
                log.warning(warning)
            if not ok:
                continue
            if not if_rule_matches(hook.if_rule, tool_name, tool_input, tool, cwd=self.cwd):
                continue
            if hook.once and hook.dedup_key() in self._once_ran:
                continue
            dkey = hook.dedup_key()
            if dkey in seen_dedup:
                continue
            seen_dedup.add(dkey)
            out.append(hook)
        return out

    def _run_one(self, event: str, hook: HookDef, payload: dict, *, abort=None) -> HookOutcome:
        if hook.once:
            self._once_ran.add(hook.dedup_key())
        env = self._env_for(hook, event)
        if hook.type == "command":
            result = run_command_hook(hook, payload, cwd=self.cwd, env=env)
        elif hook.type == "http":
            result = run_http_hook(hook, payload, timeout_s=hook.effective_timeout_s(), env=env)
        elif hook.type == "mcp_tool":
            result = run_mcp_tool_hook(hook, payload, mcp_manager=self.mcp_manager,
                                        timeout_s=hook.effective_timeout_s(), abort=abort)
        elif hook.type in ("prompt", "agent"):
            result = run_prompt_hook(hook, payload, prompt_caller=self.prompt_caller,
                                      timeout_s=hook.effective_timeout_s())
        else:
            result = HookResult(1, "", f"unknown hook type {hook.type!r}")
        return interpret_hook_result(event, result)

    def run(self, event: str, payload: dict, *, matched: Optional[str] = None,
            tool_name: Optional[str] = None, tool_input: Optional[dict] = None, tool=None,
            abort=None) -> HookOutcome:
        if not self.enabled:
            return HookOutcome()
        hooks = self.matched_hooks(event, matched=matched, tool_name=tool_name, tool_input=tool_input, tool=tool)
        if not hooks:
            return HookOutcome()
        blocking = [h for h in hooks if not h.run_async]
        for h in hooks:
            if h.run_async:
                threading.Thread(target=self._run_one, args=(event, h, payload), kwargs={"abort": abort},
                                  daemon=True).start()
        if not blocking:
            return HookOutcome()
        if len(blocking) == 1:
            outcomes = [self._run_one(event, blocking[0], payload, abort=abort)]
        else:
            with ThreadPoolExecutor(max_workers=min(8, len(blocking))) as pool:
                futures = [pool.submit(self._run_one, event, h, payload, abort=abort) for h in blocking]
                outcomes = [f.result() for f in futures]
        return combine_outcomes(outcomes)

    def run_stop(self, event: str, *, last_assistant_message: str = "", prompt_id: Optional[str] = None) -> HookOutcome:
        """`event` in ("Stop", "SubagentStop"). Applies the consecutive-
        block cap (`CLAUDE_CODE_STOP_HOOK_BLOCK_CAP`, default 8 [bin]): the
        (cap+1)th consecutive block force-ends the turn instead of blocking
        again, with Claude Code's own exact override message."""
        payload = self.payload(event, prompt_id=prompt_id, extra={
            "stop_hook_active": self.stop_block_count > 0,
            "last_assistant_message": last_assistant_message,
        })
        outcome = self.run(event, payload)
        cap = _env_positive_int("CLAUDE_CODE_STOP_HOOK_BLOCK_CAP", STOP_HOOK_BLOCK_CAP_DEFAULT)
        if outcome.blocked:
            self.stop_block_count += 1
            if self.stop_block_count > cap:
                override = (f"A hook blocked the turn from ending {self.stop_block_count - 1} "
                            f"consecutive times -- overriding and ending turn.")
                outcome.blocked = False
                outcome.system_messages = list(outcome.system_messages) + [override]
                self.stop_block_count = 0
        else:
            self.stop_block_count = 0
        return outcome

    def run_session_end(self, reason: str) -> HookOutcome:
        """SessionEnd's own budget rule [bin sec.2]: default 1.5s, raised to
        the largest matched hook's own timeout, capped at 60s (or
        `CLAUDE_CODE_SESSIONEND_HOOKS_TIMEOUT_MS` outright) -- applied as a
        per-hook cap so the WHOLE parallel batch finishes within that
        window (they all run concurrently, never sequentially)."""
        if not self.enabled:
            return HookOutcome()
        hooks = self.matched_hooks("SessionEnd", matched=None, tool_name=None, tool_input=None, tool=None)
        if not hooks:
            return HookOutcome()
        budget = session_end_budget_s(hooks)
        capped = [dataclasses.replace(h, timeout_s=min(h.effective_timeout_s(), budget)) for h in hooks]
        payload = self.payload("SessionEnd", extra={"reason": reason})
        outcomes: list = []
        with ThreadPoolExecutor(max_workers=min(8, len(capped))) as pool:
            futures = [pool.submit(self._run_one, "SessionEnd", h, payload) for h in capped]
            for f in futures:
                try:
                    outcomes.append(f.result(timeout=budget + 1.0))
                except Exception:
                    outcomes.append(HookOutcome())
        return combine_outcomes(outcomes)


def session_end_budget_s(hooks: list) -> float:
    override = os.environ.get("CLAUDE_CODE_SESSIONEND_HOOKS_TIMEOUT_MS")
    if override:
        try:
            return max(0.0, float(int(override)) / 1000.0)
        except ValueError:
            pass
    largest = max((h.effective_timeout_s() for h in hooks), default=0.0)
    return min(max(SESSION_END_DEFAULT_BUDGET_S, largest), SESSION_END_MAX_BUDGET_S)


# =============================================================================
# build_prompt_caller -- the `prompt`/`agent` handler types' "small model
# via the provider layer" [D-CFG]: a `(prompt_text, timeout_s) -> reply`
# closure driving ONE completion through the SAME provider stack the main
# session uses (providers.stream.stream_completion), reusing it rather than
# inventing a second request path.
# =============================================================================

def build_prompt_caller(model_ref, model_profile, creds, state_dir, *,
                         openrouter_base_url=None, extra_headers=None) -> Optional[Callable[[str, float], str]]:
    """`creds=None` (the small/main model isn't configured) -> None -- the
    caller (HookRunner via run_prompt_hook) then treats a prompt/agent
    hook as a silent no-op allow rather than a hard failure."""
    if creds is None:
        return None
    from rolo_claude.providers.profiles import resolve_profile
    from rolo_claude.providers.request import build_request_body
    from rolo_claude.providers.routing import Route
    from rolo_claude.providers.stream import CompletionRequest, stream_completion

    route = Route(provider=model_ref.provider, upstream_model=model_ref.model, dialect=model_ref.dialect)
    profile = resolve_profile(route)

    def _call(prompt_text: str, timeout_s: float) -> str:
        messages = [{"role": "user", "content": [{"type": "text", "text": prompt_text}]}]
        body = build_request_body(
            system_text="", messages=messages, tools=None, route=route, profile=profile,
            context_tokens=model_profile.context_tokens,
            prompt_estimate=max(1, len(prompt_text) // 4), requested_max_tokens=1024,
        )
        req = CompletionRequest(
            body={"messages": []}, route=route,
            profile={"context_tokens": model_profile.context_tokens, "max_output_tokens": model_profile.max_output_tokens},
            creds=creds, state_dir=state_dir, extra_headers=extra_headers or {},
            model_label=model_ref.raw, openrouter_base_url=openrouter_base_url,
            harness_mode=True, prebuilt_oai_body=body, tool_id_format=profile.tool_id_format,
        )
        abort = threading.Event()
        timer = threading.Timer(max(0.01, timeout_s), abort.set)
        timer.daemon = True
        timer.start()
        try:
            parts: list = []
            for ev in stream_completion(req, abort=abort):
                if ev.get("type") == "content_block_delta":
                    delta = ev.get("delta") or {}
                    if delta.get("type") == "text_delta":
                        parts.append(delta.get("text", ""))
            return "".join(parts)
        finally:
            timer.cancel()

    return _call
