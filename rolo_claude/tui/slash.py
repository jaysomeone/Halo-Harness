"""rolo_claude.tui.slash -- slash-command handling for the TUI. A handful of
"ui"-kind builtins (D-TUI: "still return a real string here -- they just
describe what needs the interactive TUI instead of performing it") get REAL
interactive behavior in here -- a picker/dialog, or a direct transcript/
theme mutation -- before ever reaching `Controller.run_slash`'s own
headless-text fallback (used for everything else: /help, /cost, /context,
/status, /skills, /agents, /effort, /doctor, custom commands, skills).
"""

from __future__ import annotations


async def handle_slash(app, name: str, args: str) -> None:
    name = name.lower()
    handler = {
        "model": _handle_model, "mcp": _handle_mcp, "clear": _handle_clear,
        # H14 scope J: /models refresh (alias /dbx) -- always off the UI
        # thread (a live Databricks probe is a real network call).
        "models": _handle_models, "dbx": _handle_dbx,
        "resume": _handle_resume, "permissions": _handle_permissions,
        "exit": _handle_exit, "quit": _handle_exit, "theme": _handle_theme,
        # U5 scope C: sessions UX.
        "rename": _handle_rename, "fork": _handle_fork, "export": _handle_export,
        "stats": _handle_stats,
        # U5 scope B: git-shadow rewind.
        "rewind": _handle_rewind, "undo": _handle_undo, "redo": _handle_redo,
        # U5 scope A: keymap.
        "keybindings": _handle_keybindings,
        # H10 Part B: human-gated /improve.
        "improve": _handle_improve,
    }.get(name)
    if handler is not None:
        await handler(app, args)
        return
    result = app.controller.run_slash(name, args)
    if result:
        await app.transcript.add_note(result, kind="command")


async def _handle_model(app, args: str) -> None:
    from rolo_claude.tui.dialogs.model_picker import ModelPicker

    args = args.strip()
    if args:
        _apply_model(app, args)
        return
    # H14 scope J: auto-refresh the Databricks catalog when stale
    # (databricks.catalog_max_age_hours, default 24), off the UI thread --
    # the picker opens immediately with whatever's cached; a background
    # refresh just notifies once it lands, it never blocks /model itself.
    app.run_worker(lambda: _dbx_auto_refresh_worker(app), thread=True, name="dbx-auto-refresh")
    models = app.controller.list_models()
    app.push_screen(ModelPicker(models, current=app.status_bar.model), lambda ref: _apply_model(app, ref))


def _dbx_auto_refresh_worker(app) -> None:
    from rolo_claude.providers.databricks import format_dbx_diff, refresh_dbx_catalog_if_stale
    state_dir = getattr(app.controller, "state_dir", None)
    if state_dir is None:
        return
    result = refresh_dbx_catalog_if_stale(state_dir)
    if result is None:
        return  # not stale, or Databricks isn't configured -- nothing to do
    ok, diff, _note = result
    if not ok:
        return  # scope J: offline/403 keeps the cache silently here -- doctor/--refresh explain why
    summary = format_dbx_diff(diff)
    if summary != "no changes":
        app.call_from_thread(app.notify, f"Databricks catalog updated: {summary}", title="/model")


def _apply_model(app, ref) -> None:
    if not ref:
        return
    err = app.controller.set_model(ref)
    if err:
        app.notify(err, severity="error", title="/model")
    else:
        app.notify(f"Model set to {ref}", title="/model")


# ============================================================================
# H14 scope J: /models refresh (alias /dbx), off the UI thread.
# ============================================================================

async def _handle_models(app, args: str) -> None:
    do_refresh = (args or "").strip().lower() in ("refresh", "--refresh", "")
    app.run_worker(lambda: _models_refresh_worker(app, do_refresh), thread=True, name="models-refresh")


async def _handle_dbx(app, _args: str) -> None:
    app.run_worker(lambda: _models_refresh_worker(app, True), thread=True, name="models-refresh")


def _models_refresh_worker(app, do_refresh: bool) -> None:
    from rolo_claude.providers.config import derive_workspace_root, resolve_databricks
    from rolo_claude.providers.databricks import (
        dbx_endpoints_age_seconds, format_dbx_diff, load_dbx_endpoints_json, refresh_dbx_catalog,
    )
    state_dir = getattr(app.controller, "state_dir", None)
    dbx = resolve_databricks()
    if dbx is None:
        app.call_from_thread(app.notify, "Databricks is not configured.", severity="warning", title="/models")
        return
    if not do_refresh:
        endpoints = load_dbx_endpoints_json(state_dir)
        age = dbx_endpoints_age_seconds(state_dir)
        age_str = "never" if age is None else f"{age / 3600:.1f}h ago"
        app.call_from_thread(app.transcript.add_note,
                              f"{len(endpoints)} Databricks endpoint(s) cached (last refreshed {age_str}).",
                              kind="command")
        return
    ok, diff, note = refresh_dbx_catalog(state_dir, derive_workspace_root(dbx.host), dbx.token)
    endpoints = load_dbx_endpoints_json(state_dir)
    if not ok:
        app.call_from_thread(app.notify, f"Databricks refresh failed: {note}", severity="warning", title="/models")
        return
    text = f"Refreshed {len(endpoints)} Databricks endpoint(s). {format_dbx_diff(diff)}"
    app.call_from_thread(app.transcript.add_note, text, kind="command")
    app.call_from_thread(app.notify, format_dbx_diff(diff), title="/models refresh")


async def _handle_mcp(app, _args: str) -> None:
    from rolo_claude.tui.dialogs.mcp_status import McpStatus

    list_fn = getattr(app.controller, "list_mcp_servers", None)
    servers = list_fn() if list_fn is not None else []
    app.push_screen(McpStatus(servers, reconnect=app.controller.reconnect_mcp,
                               approve=app.controller.approve_mcp_server))


async def _handle_clear(app, _args: str) -> None:
    # U5 must-do: /clear now actually starts a NEW session log
    # (SessionEnd(clear) + SessionStart(clear), off the UI thread via
    # Controller.clear_session) -- the old version only wiped the visible
    # transcript WIDGET, leaving the underlying context (and everything
    # the next request would derive from it) completely untouched.
    clear = getattr(app.controller, "clear_session", None)
    if not callable(clear):
        await app.transcript.clear_view()
        await app.transcript.add_note("Conversation view cleared.", kind="note")
        return
    error = clear()
    if error:
        await app.transcript.add_note(error, kind="error")
        return
    await app.transcript.clear_view()
    await app.transcript.add_note("Conversation cleared -- starting a new session context.", kind="note")


async def _handle_resume(app, args: str) -> None:
    # U5/review must-do: `list_sessions()` is file I/O -- off the UI thread.
    # H13 Part C: `/resume <text>` opens the picker ALREADY filtered by
    # `<text>` (the SAME fuzzy filter -- title/first prompt/cwd/model -- the
    # picker's own live Input box uses) instead of ignoring it.
    query = (args or "").strip()
    app.run_worker(lambda: _resume_list_worker(app, query), thread=True, name="list-sessions")


def _resume_list_worker(app, query: str) -> None:
    sessions = app.controller.list_sessions()
    app.call_from_thread(_open_resume_picker, app, sessions, query)


def _open_resume_picker(app, sessions, query: str = "") -> None:
    from rolo_claude.tui.dialogs.session_picker import SessionPicker

    def _on_pick(session_id) -> None:
        if session_id:
            app.controller.resume(session_id)

    app.push_screen(SessionPicker(sessions, initial_query=query), _on_pick)


async def _handle_permissions(app, _args: str) -> None:
    from rolo_claude.tui.dialogs.permissions import PermissionsDialog

    list_fn = getattr(app.controller, "list_permission_rules", None)
    rules = list_fn() if list_fn is not None else []
    app.push_screen(PermissionsDialog(rules, mode=app.status_bar.mode,
                                       add_rule=lambda text: app.controller.add_permission_rule(text, "session")))


async def _handle_exit(app, _args: str) -> None:
    await app.action_quit_now()


async def _handle_theme(app, args: str) -> None:
    from rolo_claude import theme as theme_mod

    name = args.strip()
    if not name:
        app.notify(f"Current theme: {app.theme_name}", title="/theme")
        return
    if not theme_mod.is_valid_theme(name):
        app.notify(f"Not a valid theme name: {name} (expected one of {sorted(theme_mod.VALID_THEMES)})",
                   severity="error", title="/theme")
        return
    theme_mod.persist_theme(name)
    app.apply_theme(name)
    app.notify(f"Theme set to {name}", title="/theme")


# ============================================================================
# U5 scope C: session titles/rename/fork/export/stats.
# ============================================================================

async def _handle_rename(app, args: str) -> None:
    title = args.strip()
    get_title = getattr(app.controller, "get_title", None)
    rename = getattr(app.controller, "rename_session", None)
    if not title:
        current = get_title() if callable(get_title) else ""
        app.notify(f"Current title: {current or '(untitled)'} -- usage: /rename <title>", title="/rename")
        return
    if not callable(rename):
        app.notify("Renaming needs a real session.", severity="warning", title="/rename")
        return
    rename(title)
    app.notify(f"Session renamed to {title!r}", title="/rename")


async def _handle_fork(app, _args: str) -> None:
    fork = getattr(app.controller, "fork_session", None)
    if not callable(fork):
        app.notify("Forking needs a real session.", severity="warning", title="/fork")
        return
    new_id = fork()
    await app.transcript.add_note(
        f"⑂ Forked into a new session: {new_id} (this session now continues independently).",
        kind="command")
    app.notify(f"Forked to {new_id}", title="/fork")


async def _handle_export(app, args: str) -> None:
    export_fn = getattr(app.controller, "export_session", None)
    if not callable(export_fn):
        app.notify("Export needs a real session.", severity="warning", title="/export")
        return
    tokens = args.split()
    sanitize = "--sanitize" in tokens
    files = [t for t in tokens if t != "--sanitize"]
    out_path = export_fn(sanitize=sanitize, path=(files[0] if files else None))
    await app.transcript.add_note(f"⬇ Exported to {out_path}{' (sanitized)' if sanitize else ''}.",
                                   kind="command")


async def _handle_stats(app, args: str) -> None:
    # H10 Part A: bare `/stats` keeps U5's own current-session behaviour
    # (synchronous, cheap -- one session's own already-in-memory nodes);
    # `/stats --models`/`--tools` is a NEW, richer cross-session path over
    # `rolo_claude.telemetry`, run OFF the UI thread (same worker pattern
    # U5 used for `/resume`'s `list_sessions` -- see `_handle_resume`/
    # `_resume_list_worker` above) since it globs and parses every session
    # JSONL under the project.
    tokens = (args or "").split()
    if "--models" in tokens or "--tools" in tokens:
        show_models, show_tools = "--models" in tokens, "--tools" in tokens
        app.run_worker(lambda: _stats_models_worker(app, show_models, show_tools), thread=True,
                        name="stats-models")
        return
    stats_fn = getattr(app.controller, "session_stats", None)
    if not callable(stats_fn):
        app.notify("Stats need a real session.", severity="warning", title="/stats")
        return
    stats = stats_fn()
    lines = [f"Turns: {stats.get('turns', 0)}", f"Total cost: ${stats.get('total_cost_usd', 0.0):.4f}"]
    for model, bucket in sorted((stats.get("per_model") or {}).items()):
        lines.append(f"  {model}: {bucket['calls']} call(s), "
                     f"{bucket['input_tokens']}in/{bucket['output_tokens']}out tok, ${bucket['cost_usd']:.4f}")
    for name, n in sorted((stats.get("tool_counts") or {}).items()):
        lines.append(f"  tool {name}: {n} call(s)")
    await app.transcript.add_note("\n".join(lines), kind="command")


def _stats_models_worker(app, show_models: bool, show_tools: bool) -> None:
    from rolo_claude import telemetry
    from rolo_claude.config.paths import project_slug

    cwd = getattr(app.controller, "cwd", None) or "."
    summaries = telemetry.scan(since="7d", slug=project_slug(cwd))
    # H10b defect 2: reflects whichever of --models/--tools was actually
    # asked for -- used to hardcode "--models" even for a bare --tools run.
    flags = " ".join(f for f, on in (("--models", show_models), ("--tools", show_tools)) if on)
    lines = [f"/stats {flags} ({len(summaries)} session(s), last 7d):"]
    if show_models:
        for r in telemetry.aggregate_by_model(summaries):
            lines.append(f"  [{r['model']} / {r['provider'] or '-'}] {r['calls']} call(s), "
                         f"${r['cost_usd']:.4f}, repair-hit%={r['repair_hit_pct']}, "
                         f"edit-fail%={r['edit_failure_pct']}, tool-err%={r['tool_error_pct']}")
    if show_tools:
        for r in telemetry.aggregate_by_tool(summaries):
            lines.append(f"  tool {r['tool']}: {r['calls']} call(s), error%={r['error_pct']}")
    app.call_from_thread(app.transcript.add_note, "\n".join(lines), kind="command")


# ============================================================================
# U5 scope B: git-shadow rewind (/rewind [step-id], /undo, /redo) -- a
# RewindCard always confirms before touching the real working tree.
# ============================================================================

async def _handle_rewind(app, args: str) -> None:
    step_id = args.strip()
    steps_fn = getattr(app.controller, "shadow_steps", None)
    if not callable(steps_fn):
        app.notify("Rewind needs a real session.", severity="warning", title="/rewind")
        return
    if step_id:
        step = next((s for s in steps_fn() if step_id in (s.get("id"), s.get("hash"))), None)
        if step is None:
            app.notify(f"No recorded step matches {step_id!r}.", severity="error", title="/rewind")
            return
        await _show_rewind_confirmation(app, step, "rewind")
        return
    app.run_worker(lambda: _rewind_picker_worker(app), thread=True, name="rewind-list")


def _rewind_picker_worker(app) -> None:
    steps = app.controller.shadow_steps()
    app.call_from_thread(_open_rewind_picker, app, steps)


def _open_rewind_picker(app, steps: list) -> None:
    from rolo_claude.tui.dialogs.rewind_picker import RewindPicker

    def _on_pick(step_id) -> None:
        if not step_id:
            return
        step = next((s for s in steps if s.get("id") == step_id), None)
        if step is not None:
            app.call_next(_show_rewind_confirmation, app, step, "rewind")

    app.push_screen(RewindPicker(steps), _on_pick)


async def _show_rewind_confirmation(app, step: dict, verb: str) -> None:
    from rolo_claude.tui.widgets.cards import RewindCard

    def on_decide(confirmed: bool) -> None:
        app.clear_pending_card()
        if confirmed:
            app.run_worker(lambda: _apply_rewind_worker(app, step["id"], verb), thread=True, name="rewind-apply")

    card = RewindCard(step=step, verb=verb, on_decide=on_decide)
    await app.transcript.mount_widget(card)
    app.set_pending_card(card)


def _apply_rewind_worker(app, step_id: str, verb: str) -> None:
    result = app.controller.rewind_apply(step_id, verb=verb)
    if result is None:
        app.call_from_thread(app.notify, "Rewind failed (step vanished?).", severity="error", title=f"/{verb}")
        return
    files = result.get("files") or []
    text = f"↩ {verb.capitalize()} complete -- restored {len(files)} file(s) to step {step_id}."
    app.call_from_thread(app.transcript.add_note, text, kind="command")


async def _handle_undo(app, _args: str) -> None:
    await _handle_undo_redo(app, "undo")


async def _handle_redo(app, _args: str) -> None:
    await _handle_undo_redo(app, "redo")


async def _handle_undo_redo(app, verb: str) -> None:
    preview_fn = getattr(app.controller, f"rewind_preview_{verb}", None)
    if not callable(preview_fn):
        app.notify(f"{verb.capitalize()} needs a real session.", severity="warning", title=f"/{verb}")
        return
    step = preview_fn()
    if step is None:
        app.notify(f"Nothing to {verb}.", title=f"/{verb}")
        return
    await _show_rewind_confirmation(app, step, verb)


# ============================================================================
# U5 scope A: /keybindings.
# ============================================================================

async def _handle_keybindings(app, _args: str) -> None:
    keymap = getattr(app, "_keymap", None)
    if keymap is None:
        from rolo_claude.tui.keys import load_keymap
        keymap = load_keymap()
    lines = ["Keybindings (~/.claude/keybindings.json merges onto these):"]
    for ctx in sorted(keymap):
        lines.append(f"  [{ctx}]")
        for chord in sorted(keymap[ctx]):
            lines.append(f"    {chord:<20} {keymap[ctx][chord]}")
    await app.transcript.add_note("\n".join(lines), kind="command")


# ============================================================================
# H10 Part B: human-gated /improve -- runs the evidence scan + ONE drafting
# model call off the UI thread, then reviews candidates one ImproveCard at a
# time (RewindCard's own confirm/cancel pattern, extended to 5 actions).
# Typed while a turn runs -> queued until the turn ends (handle_slash is
# only ever invoked between turns, same as every other slash command).
# ============================================================================

async def _handle_improve(app, _args: str) -> None:
    session = getattr(app.controller, "session", None)
    if session is None:
        app.notify("/improve needs a real session.", severity="warning", title="/improve")
        return
    from rolo_claude.improve.config import load_improve_config
    cfg = load_improve_config()
    if not cfg.enabled:
        app.notify("/improve is disabled (improve.enabled=false).", title="/improve")
        return
    app.notify("Scanning recent sessions and drafting candidates…", title="/improve")
    app.run_worker(lambda: _improve_draft_worker(app, session, cfg), thread=True, name="improve-draft")


def _improve_draft_worker(app, session, cfg) -> None:
    from rolo_claude.config.paths import project_slug
    from rolo_claude.improve import draft as draft_mod
    from rolo_claude.improve import evidence as evidence_mod
    from rolo_claude.improve.config import since_str

    clusters, candidates, error = [], [], None
    try:
        slug = project_slug(session.cwd)
        clusters = evidence_mod.build_clusters(since=since_str(cfg), slug=slug)
        if clusters:
            candidates, error = draft_mod.draft_candidates(
                session, clusters, max_candidates=cfg.max_candidates, configured_model=cfg.model)
        else:
            error = "no evidence clusters in this window"
    except Exception as e:
        error = f"{type(e).__name__}: {e}"
    app.call_from_thread(_start_improve_review, app, session, clusters, candidates, error)


def _start_improve_review(app, session, clusters: list, candidates: list, error) -> None:
    from rolo_claude.improve.dismissed import is_dismissed, load_dismissed

    if not candidates:
        app.notify(f"/improve: {error or 'no candidates'}", title="/improve")
        return
    dismissed = load_dismissed()
    remaining = [c for c in candidates if not is_dismissed(c, dismissed)]
    if not remaining:
        app.notify("/improve: every candidate was already dismissed.", title="/improve")
        return
    app._improve_state = {"session": session, "clusters": clusters, "queue": remaining, "index": 0}
    app.call_next(_show_next_improve_card, app)


def _excerpt_text_for(clusters: list, ref: str) -> str:
    for c in clusters:
        d = c.to_dict() if hasattr(c, "to_dict") else c
        for ex in d.get("excerpts", []):
            if ex.get("ref") == ref:
                return ex.get("text", "")
    return ""


async def _show_next_improve_card(app) -> None:
    from rolo_claude.tui.widgets.cards import ImproveCard

    state = getattr(app, "_improve_state", None)
    if not state:
        return
    queue, idx, session = state["queue"], state["index"], state["session"]
    if idx >= len(queue):
        app.clear_pending_card()
        app.notify("/improve: review complete.", title="/improve")
        app._improve_state = None
        return

    from rolo_claude.improve import apply as apply_mod

    candidate = queue[idx]
    settings = getattr(session.session_context, "settings", None)
    target = apply_mod.resolve_target_path(candidate, cwd=session.cwd, settings=settings)
    comment = apply_mod.provenance_comment(
        sessions=[e.split("#")[0] for e in candidate.evidence], evidence_count=len(candidate.evidence),
        model=session.model_ref.raw, from_tool_output=candidate.from_tool_output)
    diff_lines = apply_mod.diff_preview(target, candidate, comment) if target.is_update else []
    excerpt_text = _excerpt_text_for(state["clusters"], candidate.evidence[0]) if candidate.evidence else ""

    def on_action(action: str, _data) -> None:
        app.clear_pending_card()
        state["index"] += 1
        if action == "apply":
            app.run_worker(lambda: _apply_worker(app, session, candidate), thread=True, name="improve-apply")
        elif action == "edit":
            _edit_candidate_then_apply(app, session, candidate)
        elif action == "dismiss":
            from rolo_claude.improve.apply import candidate_hash
            from rolo_claude.improve.dismissed import add_dismissed
            add_dismissed(candidate_hash(candidate))
        elif action == "quit":
            state["index"] = len(queue)
        app.call_next(_show_next_improve_card, app)

    card = ImproveCard(candidate=candidate, provenance_line=comment, index=idx + 1, total=len(queue),
                        diff_lines=diff_lines, excerpt_text=excerpt_text, on_action=on_action)
    await app.transcript.mount_widget(card)
    app.set_pending_card(card)


def _apply_worker(app, session, candidate) -> None:
    from rolo_claude.improve import apply as apply_mod

    settings = getattr(session.session_context, "settings", None)
    try:
        result = apply_mod.apply_candidate(candidate, cwd=session.cwd, settings=settings,
                                            model_label=session.model_ref.raw, session=session)
        app.call_from_thread(app.transcript.add_note,
                              f"✦ /improve applied [{result.kind}] -> {result.path}", kind="command")
        app.call_from_thread(app.notify, f"Applied: {result.path}", title="/improve")
    except Exception as e:
        app.call_from_thread(app.notify, f"/improve apply failed: {type(e).__name__}: {e}",
                              severity="error", title="/improve")


def _edit_candidate_then_apply(app, session, candidate) -> None:
    """`e`: edit the candidate's own body in `$VISUAL`/`$EDITOR` via
    `app.suspend()` (the SAME mechanism `app.py`'s own Ctrl+E prompt-editor
    uses), then apply -- runs on the UI thread (`app.suspend()` is an App
    method) but the actual file write still happens on a worker."""
    import os
    import subprocess as sp
    import tempfile
    from pathlib import Path

    editor = os.environ.get("VISUAL") or os.environ.get("EDITOR")
    if not editor:
        app.notify("No $VISUAL/$EDITOR set -- applying as drafted.", severity="warning", title="/improve")
        app.run_worker(lambda: _apply_worker(app, session, candidate), thread=True, name="improve-apply")
        return
    fd, tmp_path_str = tempfile.mkstemp(suffix=".md", prefix="rolo-claude-improve-")
    tmp_path = Path(tmp_path_str)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(candidate.body)
        with app.suspend():
            sp.run(f'{editor} "{tmp_path}"', shell=True)
        new_body = tmp_path.read_text(encoding="utf-8", errors="replace").rstrip("\n")
        if new_body:
            candidate.body = new_body
    except Exception as e:
        app.notify(f"$EDITOR failed: {type(e).__name__}: {e}", severity="error", title="/improve")
    finally:
        try:
            tmp_path.unlink()
        except OSError:
            pass
    app.run_worker(lambda: _apply_worker(app, session, candidate), thread=True, name="improve-apply")
