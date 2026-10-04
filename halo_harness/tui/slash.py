"""halo_harness.tui.slash -- slash-command handling for the TUI. A handful of
"ui"-kind builtins (D-TUI: "still return a real string here -- they just
describe what needs the interactive TUI instead of performing it") get REAL
interactive behavior in here -- a picker/dialog, or a direct transcript/
theme mutation -- before ever reaching `Controller.run_slash`'s own
headless-text fallback (used for everything else: /help, /cost, /context,
/status, /skills, /agents, /effort, /doctor, custom commands, skills).
"""

from __future__ import annotations

from typing import Optional


def _pending_card_note(app, cmd: str) -> "Optional[str]":
    """1.0.1 part 2 (reviewer minor): `/effort`, `/rewind` and `/improve`
    must not open their own card while ANY card (a live permission ask,
    most importantly) is already pending -- that would silently replace
    it with no way back to the original (the same one-pending-card-slot
    class of bug deferred as finding 16), leaving a blocked tool call with
    no visible way to answer it. Returns a one-line note to show instead,
    or None when nothing is pending and the caller should proceed."""
    from halo_harness.tui.widgets.cards import PermissionCard
    card = app.pending_card
    if card is None:
        return None
    if isinstance(card, PermissionCard):
        return f"A permission request is pending -- answer it before using /{cmd}."
    return f"Another card is already pending -- answer it before using /{cmd}."


async def handle_slash(app, name: str, args: str) -> None:
    name = name.lower()
    handler = {
        "model": _handle_model, "mcp": _handle_mcp, "clear": _handle_clear,
        # H14 scope J: /models refresh (alias /dbx) -- always off the UI
        # thread (a live Databricks probe is a real network call).
        "models": _handle_models, "dbx": _handle_dbx,
        # 1.0.1 part 2 fixpass critical finding 1: /providers and /doctor
        # used to have no entry here at all, so both fell through to
        # `controller.run_slash` on the UI thread -- `/providers`'s own
        # table build runs a real DNS+TCP+TLS reachability probe per
        # configured host PLUS a `claude auth status` spawn, and `doctor`
        # shells out even more; both now build their result in a
        # `thread=True` worker and post it back with `call_from_thread`.
        "providers": _handle_providers, "doctor": _handle_doctor,
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
        # 1.0.1 hotfix 20: bare /effort opens the inline selector card;
        # /effort <level> still goes through the plain text path below.
        "effort": _handle_effort,
        # 2.0.0 Launch intro: replays the typewriter line.
        "intro": _handle_intro,
        # W4c item 2: /copy, /copy code [N], /copy tool.
        "copy": _handle_copy,
        # Halo 2.0.2 brief A.5: only `/roles edit <name>` needs real
        # interactive behavior (a form, or $EDITOR) -- every other /roles
        # subcommand (and /role, and the bare table) stays on the generic
        # headless-text fallback below.
        "roles": _handle_roles,
        # Halo 2.0.2 round 2 (brief B): same split as /roles -- only
        # `/org edit <name>` needs a real form; `/org run` (which can run
        # a whole real sub-agent tree) also needs its OWN handler so it
        # never blocks the UI thread, even though it stays on the plain
        # headless-text fallback underneath.
        "org": _handle_org,
        # Halo 2.0.2 round 3 (brief C item 1): same toggle Ctrl+T uses.
        "tasks": _handle_tasks,
        # Halo 2.0.2 round 6: /update -- the check (git/network) runs on a
        # worker, never the UI thread; the dialog's own Enter is what
        # actually requests the quit-update-relaunch handoff (see
        # `_on_update_dialog_result`).
        "update": _handle_update,
        # Halo 2.0.2 round 7: /setup [roles|orgs] -- the init wizard's own
        # Roles/Organizations step(s), pushed onto THIS live app's screen
        # stack (same pattern as /roles edit, /org edit).
        "setup": _handle_setup,
    }.get(name)
    if handler is not None:
        await handler(app, args)
        return
    # finding 17 (W6a): `Controller.run_slash` used to be called directly
    # on the UI thread here -- the fallback for every custom command AND
    # skill (nothing else reaches this branch). Its own prompt-kind path
    # fires the UserPromptExpansion command/HTTP hook synchronously (600s
    # default timeout, no abort), so a slow one froze the WHOLE TUI, not
    # just this one command. Same thread-worker + call_from_thread
    # pattern `_handle_doctor`/`_handle_providers` above already use.
    app.run_worker(lambda: _run_slash_worker(app, name, args), thread=True, name="run-slash", group="run-slash")


def _run_slash_worker(app, name: str, args: str) -> None:
    result = app.controller.run_slash(name, args)
    if result:
        app.call_from_thread(app.transcript.add_note, result, kind="command")


async def _handle_model(app, args: str) -> None:
    args = args.strip()
    if args:
        _apply_model_or_defer(app, args)
        return
    # H14 scope J (widened to every enabled provider by the H15 part 2
    # addendum, see `catalog_auto_refresh_worker`): auto-refresh a stale
    # catalog (databricks.catalog_max_age_hours, default 24) off the UI
    # thread -- the picker opens immediately with whatever's cached; a
    # background refresh just notifies once it lands, it never blocks
    # /model itself.
    # 1.0.1 hotfix 15.4: own group= (every slash.py worker below gets one
    # named after its job) so no future exclusive=True worker anywhere in
    # the app can cancel one of these mid-flight by sharing the default
    # group -- see tui/app.py's git-branch/statusline fix for the bug this
    # class of omission caused.
    app.run_worker(lambda: catalog_auto_refresh_worker(app), thread=True, name="catalog-auto-refresh",
                    group="catalog-auto-refresh")
    # 1.0.1 part 2 (reviewer minor): `cached_auth_status_is_stale`'s own
    # TTL was never actually consulted anywhere -- the ONLY other caller
    # (tui/app.py's on_mount) primes the cache exactly ONCE at startup, so
    # a claude.ai login/logout during a long session stayed invisible to
    # `/model` until the whole app restarted. Same "stale -> refresh in
    # the background, off the UI thread" shape `catalog_auto_refresh_worker`
    # already uses just above -- this /model open won't necessarily see
    # the fresh value itself (the refresh races `_list_models_worker`
    # below), but the NEXT one will, same as a Databricks catalog refresh
    # triggered the same way.
    app.run_worker(lambda: _cc_auth_status_auto_refresh_worker(app), thread=True, name="cc-auth-auto-refresh",
                    group="cc-auth-auto-refresh")
    # 1.0.1 fixpass finding 1: `list_models()` -- builds ctx/price columns
    # per Databricks endpoint and (pre-fix) span the `claude auth status`
    # subprocess -- runs off the UI thread now, the exact same `thread=True`
    # worker + `call_from_thread` pattern `_handle_resume`'s own file-I/O-
    # bound `list_sessions()` already uses just below. Measured 2-5s TUI
    # freezes on a real Kali work box before this fix.
    app.run_worker(lambda: _list_models_worker(app), thread=True, name="list-models", group="list-models")


def _cc_auth_status_auto_refresh_worker(app) -> None:
    """1.0.1 part 2 (reviewer minor): the ONE place `cc_models.
    cached_auth_status_is_stale`'s TTL is actually consulted -- a cold
    cache (nothing primed yet) or one older than `CACHED_AUTH_STATUS_TTL_S`
    gets a real (bounded, off-the-UI-thread) `claude auth status` re-run.
    Best-effort: any failure here is identical to the cache simply staying
    whatever it was (list_models()'s own try/except around the read is
    unaffected either way)."""
    try:
        from halo_harness.providers.cc_models import cached_auth_status_is_stale, refresh_cached_claude_auth_status
        if cached_auth_status_is_stale():
            refresh_cached_claude_auth_status()
    except Exception:
        pass


def _list_models_worker(app) -> None:
    models = app.controller.list_models()
    app.call_from_thread(_open_model_picker, app, models)


def _open_model_picker(app, models) -> None:
    from halo_harness.tui.dialogs.model_picker import ModelPicker
    from halo_harness.theme import get_config_value
    from halo_harness import launch_state
    last_used = launch_state.resolve_last_model(app.cwd, memory=get_config_value("model_memory", default="cwd")) or ""
    app.push_screen(ModelPicker(models, current=app.status_bar.model, last_used=last_used),
                     lambda ref: _apply_model(app, ref))


async def _handle_roles(app, args: str) -> None:
    """Halo 2.0.2 brief A.5: `/roles edit <name>` opens a real form
    (`tui/dialogs/roles_editor.py`), or shells to `$EDITOR` when
    `roles.editor: "external"` is configured -- every other `/roles`
    subcommand (`templates`/`save`/`load`/`new`/`show`/`set`, or the bare
    table) still goes through the SAME headless-text fallback every other
    command uses (`commands/builtins.py::_cmd_roles`, off the UI thread,
    exactly like `/effort`'s own bare case)."""
    parts = (args or "").strip().split(None, 1)
    sub = parts[0].lower() if parts else ""
    if sub != "edit":
        app.run_worker(lambda: _run_slash_worker(app, "roles", args), thread=True, name="run-slash", group="run-slash")
        return
    name = parts[1].strip() if len(parts) > 1 else ""
    if not name:
        await app.transcript.add_note("Usage: /roles edit <name>", kind="command")
        return
    from halo_harness.theme import get_config_value
    if get_config_value("roles.editor", default="") == "external":
        _edit_role_template_externally(app, name)
        return
    app.run_worker(lambda: _roles_edit_form_worker(app, name), thread=True, name="roles-edit-form",
                    group="roles-edit-form")


def _edit_role_template_externally(app, name: str) -> None:
    """`roles.editor: "external"` -- the SAME `app.suspend()` dance
    `action_open_editor` (Ctrl+E, `tui/app.py`) uses: `$EDITOR` needs the
    REAL terminal, not Textual's own screen buffer, so this runs
    synchronously on the UI thread like that one does (`suspend()`'s own
    contract), never inside a `thread=True` worker."""
    import json
    import os
    import subprocess as sp

    from halo_harness.roles import load_role_template, role_templates_dir, save_role_template, validate_role_template

    editor = os.environ.get("VISUAL") or os.environ.get("EDITOR")
    if not editor:
        app.notify("No $VISUAL/$EDITOR set.", severity="warning", title="/roles edit")
        return
    if load_role_template(name) is None:
        ok, problems = save_role_template(name, {"roles": {}})
        if not ok:
            app.notify(f"Could not create {name!r}: " + "; ".join(problems), severity="error", title="/roles edit")
            return
    path = role_templates_dir() / f"{name}.json"
    app._enter_suspend_for_editor()
    try:
        with app.suspend():
            sp.run(f'{editor} "{path}"', shell=True)
    finally:
        app._exit_suspend_for_editor()
    try:
        problems = validate_role_template(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError) as e:
        app.notify(f"Could not re-read role template {name!r}: {e}", severity="error", title="/roles edit")
        return
    if problems:
        app.notify(f"Role template {name!r} is now invalid: " + "; ".join(problems),
                    severity="error", title="/roles edit")
    else:
        app.notify(f"Saved role template {name!r}.", title="/roles edit")


def _roles_edit_form_worker(app, name: str) -> None:
    """A brand-new (not-yet-on-disk) template is only ever built IN
    MEMORY here -- never written until the user actually presses ctrl+s
    inside `RolesEditor.action_save` -- so pressing Esc on a template
    that never existed before leaves nothing on disk at all."""
    from halo_harness.roles import load_role_template
    template = load_role_template(name) or {"name": name, "description": "", "roles": {}}
    models = app.controller.list_models()
    app.call_from_thread(_open_roles_editor, app, name, template, models)


def _open_roles_editor(app, name: str, template: dict, models: list) -> None:
    from halo_harness.tui.dialogs.roles_editor import RolesEditor

    def _after(saved) -> None:
        if saved:
            app.notify(f"Saved role template {name!r}.", title="/roles edit")
        else:
            app.notify("Role template edit cancelled.", title="/roles edit")

    app.push_screen(RolesEditor(name, template["roles"], models, description=template.get("description", "")), _after)


async def _handle_org(app, args: str) -> None:
    """Halo 2.0.2 round 2 (brief B) / round 3 (brief C): `/org edit <name>`
    opens a real form (`tui/dialogs/org_editor.py`); `/org run` gets its
    OWN live worker (round 3 -- see `_run_org_worker`'s own docstring for
    why: the old headless-text fallback dumped a raw `<task_result>` XML
    block into the transcript as a plain note, with no live progress and
    no status-bar refresh); every other subcommand (`list`/`show`/`new`/
    `load`, or the bare list) still goes through the SAME headless-text
    fallback every other command uses (`commands/builtins.py::_cmd_org`,
    off the UI thread)."""
    parts = (args or "").strip().split(None, 1)
    sub = parts[0].lower() if parts else ""
    if sub == "run":
        from halo_harness.orgs import default_org_name, parse_run_args
        rest = parts[1].strip() if len(parts) > 1 else ""
        name, goal = parse_run_args(rest)
        if not goal:
            await app.transcript.add_note('Usage: /org run [<name>] "<goal>" (no name uses orgs.default, '
                                           'see /setup orgs)', kind="command")
            return
        if name is None:
            name = default_org_name()
            if name is None:
                await app.transcript.add_note(
                    'No organization name given, and no default is set -- /org run <name> "<goal>", '
                    'or set a default in /setup orgs.', kind="command")
                return
        app.run_worker(lambda: _run_org_worker(app, name, goal), thread=True, name="run-org", group="run-org")
        return
    if sub != "edit":
        app.run_worker(lambda: _run_slash_worker(app, "org", args), thread=True, name="run-slash", group="run-slash")
        return
    name = parts[1].strip() if len(parts) > 1 else ""
    if not name:
        await app.transcript.add_note("Usage: /org edit <name>", kind="command")
        return
    app.run_worker(lambda: _org_edit_form_worker(app, name), thread=True, name="org-edit-form",
                    group="org-edit-form")


def _strip_task_result(text: str) -> str:
    """`agent.subagent._wrap_task_result`'s own `<task_result task_id=
    "...">...</task_result>` wrapper, stripped for display -- a no-op
    (returns `text` unchanged) when it isn't present at all (an error
    ToolResult from `run_org_call` -- unknown org, validation failure --
    never gets wrapped in the first place)."""
    import re
    m = re.match(r'^<task_result task_id="[^"]*">\n(.*)\n</task_result>$', text or "", re.DOTALL)
    return m.group(1) if m else (text or "")


def _run_org_worker(app, name: str, goal: str) -> None:
    """Halo 2.0.2 round 3 (brief C): runs `name` for real, LIVE -- unlike
    the old headless-text path (`commands/builtins.py::_cmd_org`'s own
    "run" branch, still used by `halo org run` from a real CLI with no
    TUI at all), this passes `app.controller.events.put` as `run_org_
    call`'s `on_event`, so the root position (and everything it
    delegates to) gets a real SubAgentCard with its own position title --
    the SAME live dock rendering an ordinary `Agent(subagent_type=...)`
    tool call already gets -- instead of the whole run happening silently
    and then dumping a raw `<task_result>` block into the transcript as a
    plain note once it's done."""
    session = getattr(app.controller, "session", None)
    if session is None or getattr(session, "agent_runtime", None) is None:
        app.call_from_thread(app.transcript.add_note,
                              "Organizations can only be run once a session is running.", kind="command")
        return
    from halo_harness.agent.subagent import run_org_call
    # 2.0.2 review finding 4 (major): `run_org_call`'s own contract is
    # "never raises", but this is a Textual thread worker, where Textual's
    # default `exit_on_error=True` takes the WHOLE app down the one time
    # that contract is violated by a bug anywhere in the call tree (an
    # org position's own `model:`/`/role` is validated up front now --
    # see `orgs.validate_org`/`run_agent_call`'s own guard -- but this is
    # still the right belt-and-suspenders place to stop any OTHER bug
    # from taking the TUI down with it).
    try:
        _events, result = run_org_call(
            runtime=session.agent_runtime, tool_id=f"org-run-{name}", tool_name="Agent",
            tool_input={"org": name, "prompt": goal, "description": f"Run org {name}"},
            on_event=app.controller.events.put,
        )
        text = _strip_task_result(result.content)
        is_error = result.is_error
    except Exception as e:
        text = f"Organization {name!r} failed to run: {type(e).__name__}: {e}"
        is_error = True
    app.call_from_thread(_finish_org_run, app, name, text, is_error)


async def _finish_org_run(app, name: str, text: str, is_error: bool) -> None:
    # `call_from_thread`'s own `invoke()` awaits an async callable like
    # this one -- a plain sync function here would silently create-but-
    # never-await the `add_note` coroutine (Transcript.add_note is async)
    # and the note would never actually appear.
    await app.transcript.add_note(f"org {name!r} finished:\n{text}", kind=("error" if is_error else "command"))
    # round 3 (brief C): the whole org tree already rolled its spend into
    # THIS session's own cost_meter/log (agent/subagent.py's `_rollup_
    # child_cost_into_parent`, called once per child, bubbling all the way
    # up to the top session) -- nothing else ever asks the status bar to
    # re-read it, since `/org run` happens entirely OUTSIDE the normal
    # turn loop (a `status` event otherwise only fires from Session.
    # status_event()/message_end). The SAME status_event() the Controller
    # already uses after a mode/model change -- just queued here too.
    session = getattr(app.controller, "session", None)
    if session is not None and hasattr(session, "status_event"):
        app.controller.events.put(session.status_event())


def _org_edit_form_worker(app, name: str) -> None:
    """A brand-new (not-yet-on-disk) org is only ever built IN MEMORY
    here -- never written until the user actually saves inside the
    editor -- so cancelling one that never existed before leaves nothing
    on disk at all."""
    from halo_harness.orgs import load_org
    org = load_org(name) or {"name": name, "description": "", "positions": [
        {"title": "Orchestrator", "role": "orchestrator", "reports": [], "instructions": ""}]}
    models = app.controller.list_models()
    app.call_from_thread(_open_org_editor, app, name, org, models)


def _open_org_editor(app, name: str, org: dict, models: list) -> None:
    from halo_harness.tui.dialogs.org_editor import OrgEditor

    def _after(saved) -> None:
        if saved:
            app.notify(f"Saved organization {name!r}.", title="/org edit")
        else:
            app.notify("Organization edit cancelled.", title="/org edit")

    app.push_screen(OrgEditor(name, org, models), _after)


def catalog_auto_refresh_worker(app) -> None:
    """H15 part 2 addendum 3.2a: staleness-gated (missing or
    `databricks.catalog_max_age_hours`-old, default 24h) background refresh
    for EVERY enabled provider's catalog -- OpenRouter/Anthropic/Databricks
    alike -- never on the UI thread. Called both when `/model` opens
    (`_handle_model` above) AND once at app launch (`tui/app.py`'s own
    `on_mount`), so a provider set up with just a key/token (no `init` ever
    run) still gets a real catalog without the user doing anything extra.
    One combined dim notification per provider that actually changed;
    Databricks keeps its own richer added/removed/path-type diff text,
    OpenRouter/Anthropic just name the model count once populated."""
    from halo_harness.config.paths import background_net_disabled
    if background_net_disabled():
        return  # 1.0.1 part 2 fixpass finding 10: test seam, never touch the network
    state_dir = getattr(app.controller, "state_dir", None)
    if state_dir is None:
        return
    # N2c (1.0.1 final pass): the session's own trust-filtered `Settings.
    # effective_env` (shell < user < trusted project/local < policy) --
    # `None` (a test-constructed controller with no `settings=`) falls back
    # to bare `os.environ`/the settings chain re-derivation, unchanged.
    settings = getattr(app.controller, "settings", None)
    env = settings.effective_env if settings is not None else None
    from halo_harness.providers.enablement import is_enabled_with_env
    notes = []
    if is_enabled_with_env("openrouter", env):
        from halo_harness.providers.databricks import load_models_json, refresh_openrouter_catalog_if_stale
        if refresh_openrouter_catalog_if_stale(state_dir, env=env):
            notes.append(f"OpenRouter ({len(load_models_json(state_dir))} models)")
    if is_enabled_with_env("anthropic", env):
        from halo_harness.providers.anthropic_catalog import load_ant_models_json, refresh_anthropic_catalog_if_stale
        if refresh_anthropic_catalog_if_stale(state_dir, env=env):
            notes.append(f"Anthropic ({len(load_ant_models_json(state_dir))} models)")
    if is_enabled_with_env("databricks", env):
        from halo_harness.providers.databricks import format_dbx_diff, refresh_dbx_catalog_if_stale
        result = refresh_dbx_catalog_if_stale(state_dir, env=env)
        if result is not None:
            ok, diff, _note = result
            if ok:
                summary = format_dbx_diff(diff)
                if summary != "no changes":
                    notes.append(f"Databricks ({summary})")
    if notes:
        app.call_from_thread(app.notify, f"Catalog refreshed: {'; '.join(notes)}", title="/model")


def or_balance_refresh_worker(app, *, force: bool = False) -> None:
    """H15 part 2 addendum 4 (corrected): background refresh for the
    OpenRouter account-balance status bar segment -- never on the UI
    thread. Called at app launch and every `BALANCE_REFRESH_INTERVAL_S`
    (both `force=True`, always attempt) from `tui/app.py`'s own `on_mount`,
    and once after every turn that used an OpenRouter route (`force=False`,
    gated by the post-turn debounce -- a chatty multi-turn session must not
    hammer this endpoint once per turn). `GET /key` always uses the ordinary
    `OPENROUTER_API_KEY`; `GET /credits` only runs (with the SEPARATE
    `OPENROUTER_MANAGEMENT_KEY`) when that key is actually configured.
    Scope for this round is OpenRouter only (the one provider with a
    balance API)."""
    from halo_harness.config.paths import background_net_disabled
    if background_net_disabled():
        return  # 1.0.1 part 2 fixpass finding 10: test seam, never touch the network
    # N2c (1.0.1 final pass): the session's own trust-filtered `Settings.
    # effective_env`, same as catalog_auto_refresh_worker just above --
    # `None` (no controller/settings attached, e.g. a bare test double)
    # falls back to bare os.environ exactly as before this fix. The
    # enablement check uses the same env, so a key that lives only in a
    # settings.json env block still gets its balance segment.
    settings = getattr(getattr(app, "controller", None), "settings", None)
    env = settings.effective_env if settings is not None else None
    from halo_harness.providers.enablement import is_enabled_with_env
    if not is_enabled_with_env("openrouter", env):
        return
    from halo_harness.providers.config import resolve_openrouter, resolve_openrouter_management_key
    orc = resolve_openrouter(env)
    if orc is None:
        return
    if not force:
        from halo_harness.providers.openrouter_account import openrouter_balance_refresh_due_after_turn
        if not openrouter_balance_refresh_due_after_turn():
            return
    management_key = resolve_openrouter_management_key(env)
    from halo_harness.providers.openrouter_account import format_status_bar_segment, refresh_cached_openrouter_balance
    entry = refresh_cached_openrouter_balance(orc.base_url, orc.api_key, management_key=management_key)
    if entry is not None:
        segment = format_status_bar_segment()
        app.call_from_thread(app.status_bar.set_or_balance, segment, fetched_at=entry["fetched_at"])


def _apply_model_or_defer(app, ref: str) -> None:
    """2.0.1 finding 26: a BARE alias (`/model opus`, no provider prefix)
    with no ANTHROPIC_API_KEY resolves through `providers.cc_models.
    default_bare_alias_route`, which -- outside the gateway case (already
    short-circuited) -- falls back to a LIVE `claude auth status` spawn (up
    to 10s) whenever the cache is cold. `_apply_model` below runs
    SYNCHRONOUSLY on the UI thread (`Controller.set_model` resolves the ref
    before ever reaching a worker), so that spawn used to freeze the whole
    TUI for its duration. `default_bare_alias_route` itself now prefers the
    cache when one exists (covers the common case: the startup worker has
    almost always already primed it by the time a user actually types a
    command) -- this covers the remaining cold-cache edge case by warming
    the cache in a worker FIRST, applying once it lands, so NO path here
    ever spawns the subprocess on the UI thread. Anything else (already
    prefixed, vendor/model, a key set, a primed cache, or gateway-driven)
    applies immediately, exactly as before this fix."""
    from halo_harness.providers.cc_models import BARE_ALIAS_NAMES, cached_claude_auth_status, is_claude_gateway_driven
    import os
    needs_live_check = (
        ref in BARE_ALIAS_NAMES and not os.environ.get("ANTHROPIC_API_KEY")
        and not is_claude_gateway_driven() and cached_claude_auth_status() is None
    )
    if not needs_live_check:
        _apply_model(app, ref)
        return
    app.notify(f"Checking Claude Code login status for {ref!r}...", title="/model")
    app.run_worker(lambda: _resolve_bare_alias_worker(app, ref), thread=True,
                    name="model-bare-alias-auth-check", group="model-bare-alias-auth-check")


def _resolve_bare_alias_worker(app, ref: str) -> None:
    from halo_harness.providers.cc_models import refresh_cached_claude_auth_status
    refresh_cached_claude_auth_status()
    app.call_from_thread(_apply_model, app, ref)


def _apply_model(app, ref) -> None:
    if not ref:
        return
    err = app.controller.set_model(ref)
    if err:
        app.notify(err, severity="error", title="/model")
    else:
        # 2.0.1 W3a ("launch with the last session's model and effort"):
        # every successful /model change persists -- the NEXT launch (in
        # this cwd, then globally; headless.build_session's own precedence
        # chain) starts from it instead of always falling back to the
        # provider default.
        from halo_harness import launch_state
        launch_state.record_last_model(ref, cwd=app.cwd)
        app.notify(f"Model set to {ref} -- saved as the default for next launch", title="/model")


# ============================================================================
# 1.0.1 hotfix 20: /effort -- bare opens the inline selector card (Claude
# Code style), /effort <level> is left to the plain-text path (commands.
# builtins._cmd_effort, via Controller.run_slash) exactly like every other
# argument-taking builtin.
# ============================================================================

def _effort_status_text(session) -> "str | None":
    """1.0.1 part 2 (item 22 remainder): the status bar's own effort tag --
    "<value> (tools)" whenever this route forces an explicit override
    alongside tools, else the plain configured value.

    Halo 2.0.1 W2b (liveness-tips-brief Part C): "the status-bar effort
    chip shows the SENT value" -- `session.effort` already IS that (every
    branch that sets it clamps first, see `Session.__init__`/`_cmd_
    effort`), so the plain case needs no change. When what was last
    explicitly REQUESTED (`session.effort_requested`) differs from it, the
    chip says so inline (`"medium (sent as high on this route)"`), the
    same wording the bare `/effort` card/text use -- never just the sent
    value alone with no explanation of why it isn't what was asked for."""
    from halo_harness.providers.profiles import effort_display_override
    profile = getattr(session, "provider_profile", None) if session is not None else None
    override = effort_display_override(profile)
    if override:
        return override
    if session is None:
        return None
    sent = getattr(session, "effort", None)
    if sent is None:
        return None
    requested = getattr(session, "effort_requested", None)
    if requested is not None and requested != sent:
        return f"{requested} (sent as {sent} on this route)"
    return sent


async def _handle_effort(app, args: str) -> None:
    # 1.0.1 part 2 (reviewer minor): must not open the card while a
    # permission (or any other) card is already pending -- it would
    # silently replace it, same class of bug as finding 16.
    note = _pending_card_note(app, "effort")
    if note:
        app.notify(note, title="/effort")
        return
    session = getattr(app.controller, "session", None)
    args = args.strip()
    if args:
        result = app.controller.run_slash("effort", args)
        if result:
            await app.transcript.add_note(result, kind="command")
        # commands.builtins._cmd_effort mutates the live session's own
        # `effort` directly (no event round-trip) -- push the status bar's
        # tag right away rather than waiting for the next turn's status
        # event to happen to carry it.
        app.status_bar.set_effort(_effort_status_text(session))
        # 2.0.1 W3a: persist the session's own (already-clamped) effort --
        # reading it back rather than the raw typed `args` means a rejected/
        # unsupported level is never persisted as if it had applied.
        if session is not None:
            from halo_harness import launch_state
            launch_state.record_last_effort(getattr(session, "effort", None), cwd=app.cwd)
        return

    from halo_harness.providers.profiles import effort_display_override
    from halo_harness.tui.widgets.cards import EffortCard

    profile = getattr(session, "provider_profile", None) if session is not None else None
    if session is None or profile is None or not profile.reasoning_effort_supported:
        levels, current, requested = [], None, None
        model_id = app.status_bar.model
        override_note = None
    else:
        levels = list(profile.effort_values_supported or ())
        current = getattr(session, "effort", None)
        # Part C: the RAW value last explicitly requested, before clamping
        # -- `EffortCard` marks the gap inline when it differs from `current`.
        requested = getattr(session, "effort_requested", None)
        model_id = session.model_ref.raw
        override_display = effort_display_override(profile)
        override_note = (f"Note: this route sends reasoning_effort={profile.reasoning_effort_with_tools!r} "
                          f"whenever a turn carries tools, regardless of the level picked here "
                          f"(effective: {override_display}).") if override_display else None

    def on_select(level) -> None:
        app.clear_pending_card()
        if level is None:
            return  # Esc -- keep the old value, nothing to announce
        result = app.controller.run_slash("effort", level)
        app.notify(result or f"Effort level set to '{level}'", title="/effort")
        app.status_bar.set_effort(_effort_status_text(session))
        # 2.0.1 W3a: same persistence as the plain-text /effort <level> path.
        if session is not None:
            from halo_harness import launch_state
            launch_state.record_last_effort(getattr(session, "effort", None), cwd=app.cwd)

    card = EffortCard(levels=levels, current=current, model_id=model_id, on_select=on_select,
                       override_note=override_note, requested=requested)
    await app.transcript.mount_widget(card)
    app.set_pending_card(card)


# ============================================================================
# H14 scope J: /models refresh (alias /dbx), off the UI thread.
# ============================================================================

async def _handle_models(app, args: str) -> None:
    # 1.0.1 hotfix 3: bare `/models` (empty args) used to ALSO set
    # do_refresh=True here (`"" in (..., "")` is trivially true) -- the
    # exact opposite of the documented/intended contract ("bare reports the
    # cache without touching the network"), and the reason a DNS/VPN-down
    # box saw `/models` itself hang. Only an explicit "refresh"/"--refresh"
    # (or the /dbx alias below) ever goes to the network now.
    do_refresh = (args or "").strip().lower() in ("refresh", "--refresh")
    app.run_worker(lambda: _models_refresh_worker(app, do_refresh), thread=True, name="models-refresh",
                    group="models-refresh")


async def _handle_dbx(app, _args: str) -> None:
    app.run_worker(lambda: _models_refresh_worker(app, True, dbx_explicit=True), thread=True, name="models-refresh",
                    group="models-refresh")


def _models_refresh_worker(app, do_refresh: bool, *, dbx_explicit: bool = False) -> None:
    """1.0.1 part 2 fixpass finding 12: `/models [refresh]` and `/dbx`
    (`dbx_explicit=True`) now refresh/report on EVERY enabled provider
    (OpenRouter/Anthropic/Databricks alike), matching the headless `/models`
    surface (`commands/builtins.py::_cmd_models`) that already did this --
    before this fix the TUI's own worker only ever touched Databricks, so
    an OpenRouter-only box's `/models refresh` answered "Databricks is not
    configured" instead of doing anything useful. `dbx_explicit` (set only
    by `/dbx`, an explicit ask about Databricks specifically) is what makes
    Databricks resolve even when it isn't formally enabled, and is the ONLY
    path that still shows the "Databricks is not configured" wording -- a
    plain `/models` on a box where Databricks just isn't enabled silently
    skips it instead, same as it already silently skips a disabled
    OpenRouter/Anthropic."""
    from halo_harness.providers.config import derive_workspace_root, resolve_databricks
    from halo_harness.providers.databricks import (
        dbx_endpoints_age_seconds, format_dbx_diff, load_dbx_endpoints_json, load_models_json,
        models_json_age_seconds, refresh_dbx_catalog, refresh_openrouter_catalog_if_stale,
    )
    from halo_harness.providers.anthropic_catalog import (
        ant_models_age_seconds, load_ant_models_json, refresh_anthropic_catalog_if_stale,
    )
    from halo_harness.catalog_cli import format_dbx_table_lines, _dbx_rows
    from halo_harness.providers.enablement import is_enabled
    state_dir = getattr(app.controller, "state_dir", None)

    def _table_text(endpoints: dict, root: str) -> str:
        if not endpoints:
            return ""
        rows = _dbx_rows(endpoints, root, state_dir, urls=False)
        return "\n".join(format_dbx_table_lines(rows)) + "\n\n"

    sections: list = []
    dbx_enabled = dbx_explicit or is_enabled("databricks")
    dbx = resolve_databricks() if dbx_enabled else None
    if dbx is not None:
        root = derive_workspace_root(dbx.host)
        if not do_refresh:
            # 1.0.1 hotfix 3: renders the CACHED table immediately -- no
            # network call at all (family/path/chat, same columns/wording
            # `halo models` prints) -- plus the catalog age.
            endpoints = load_dbx_endpoints_json(state_dir)
            age = dbx_endpoints_age_seconds(state_dir)
            age_str = "never" if age is None else f"{age / 3600:.1f}h ago"
            sections.append(f"{_table_text(endpoints, root)}{len(endpoints)} Databricks endpoint(s) cached "
                             f"(last refreshed {age_str}).")
        else:
            ok, diff, note = refresh_dbx_catalog(state_dir, root, dbx.token)
            endpoints = load_dbx_endpoints_json(state_dir)
            if not ok:
                # 1.0.1 hotfix 3: a failed refresh still shows the
                # (unchanged) cached table, plus exactly ONE line naming
                # the error -- never just a bare toast with no context for
                # what's still usable.
                # 2.0.1 finding 24: a lock LOST to a concurrent refresh is
                # not a failure -- never prefixed "Databricks refresh
                # failed:", which would read as self-contradictory next to
                # REFRESH_BUSY_NOTE's own "already running" wording.
                from halo_harness.providers.databricks import REFRESH_BUSY_NOTE
                prefix = "Databricks: " if note == REFRESH_BUSY_NOTE else "Databricks refresh failed: "
                sections.append(f"{_table_text(endpoints, root)}{prefix}{note}")
                app.call_from_thread(app.notify, f"{prefix}{note}", severity="warning", title="/models")
            else:
                # 1.0.1 hotfix 12: also refreshes models.dev (best-effort
                # -- a failure here never fails the whole refresh, it just
                # leaves the existing ctx/price data, if any, in place).
                from halo_harness.providers.models_dev import refresh_models_dev_cache
                md_ok, md_note = refresh_models_dev_cache(state_dir)
                text = (f"{_table_text(endpoints, root)}Refreshed {len(endpoints)} Databricks endpoint(s). "
                        f"{format_dbx_diff(diff)}")
                if not md_ok:
                    text += (f"\n(models.dev refresh failed: {md_note} -- the vendored/cached price data "
                             f"stays in use.)")
                sections.append(text)
                app.call_from_thread(app.notify, format_dbx_diff(diff), title="/models refresh")
    elif dbx_explicit:
        sections.append("Databricks is not configured.")
        app.call_from_thread(app.notify, "Databricks is not configured.", severity="warning", title="/models")

    if is_enabled("openrouter"):
        if not do_refresh:
            age = models_json_age_seconds(state_dir)
            age_str = "never" if age is None else f"{age / 3600:.1f}h ago"
            sections.append(f"OpenRouter: {len(load_models_json(state_dir))} model(s) cached "
                             f"(last refreshed {age_str}).")
        else:
            from halo_harness.providers.databricks import CATALOG_REFRESH_BUSY, REFRESH_BUSY_NOTE
            ok = refresh_openrouter_catalog_if_stale(state_dir, force=True)
            if ok is CATALOG_REFRESH_BUSY:
                sections.append(f"OpenRouter: {REFRESH_BUSY_NOTE}.")
            elif ok is False:
                sections.append("OpenRouter refresh failed -- see `halo doctor`.")
            elif ok is None:
                sections.append("OpenRouter: not configured -- nothing to refresh.")
            else:
                sections.append(f"OpenRouter refreshed: {len(load_models_json(state_dir))} model(s) cached.")

    if is_enabled("anthropic"):
        if not do_refresh:
            age = ant_models_age_seconds(state_dir)
            age_str = "never" if age is None else f"{age / 3600:.1f}h ago"
            sections.append(f"Anthropic: {len(load_ant_models_json(state_dir))} model(s) cached "
                             f"(last refreshed {age_str}).")
        else:
            from halo_harness.providers.databricks import CATALOG_REFRESH_BUSY, REFRESH_BUSY_NOTE
            ok = refresh_anthropic_catalog_if_stale(state_dir, force=True)
            if ok is CATALOG_REFRESH_BUSY:
                sections.append(f"Anthropic: {REFRESH_BUSY_NOTE}.")
            elif ok is False:
                sections.append("Anthropic refresh failed -- see `halo doctor`.")
            elif ok is None:
                sections.append("Anthropic: not configured -- nothing to refresh.")
            else:
                sections.append(f"Anthropic refreshed: {len(load_ant_models_json(state_dir))} model(s) cached.")

    if not sections:
        sections.append("No providers are enabled yet -- see `halo providers`.")
    app.call_from_thread(app.transcript.add_note, "\n\n".join(sections), kind="command")


# ============================================================================
# 1.0.1 part 2 fixpass critical finding 1: /providers and /doctor, off the
# UI thread. `enable`/`disable` stay synchronous (a plain local config.json
# write, no network/subprocess); listing builds its text in a worker.
# ============================================================================

async def _handle_providers(app, args: str) -> None:
    from halo_harness.providers.enablement import PROVIDER_NAMES, canonical, disable, enable, label_for
    tokens = (args or "").split()
    action = tokens[0] if tokens else "list"
    if action in ("enable", "disable"):
        if len(tokens) < 2:
            app.notify(f"/providers {action}: needs a provider name ({', '.join(PROVIDER_NAMES)})",
                       severity="error", title="/providers")
            return
        name = canonical(tokens[1])
        if name not in PROVIDER_NAMES:
            app.notify(f"/providers {action}: unknown provider {tokens[1]!r} "
                       f"(expected one of {', '.join(PROVIDER_NAMES)})", severity="error", title="/providers")
            return
        if action == "enable":
            enable(name)
            await app.transcript.add_note(f"{label_for(name)}: enabled", kind="command")
        else:
            disable(name)
            await app.transcript.add_note(f"{label_for(name)}: disabled", kind="command")
        return
    if action == "setup":
        name = tokens[1] if len(tokens) > 1 else "<name>"
        await app.transcript.add_note(
            f"/providers setup needs the interactive picker -- run `halo providers setup {name}` "
            f"(or `halo init`) from a real terminal.", kind="command")
        return
    if action != "list":
        await app.transcript.add_note("Usage: /providers [list|enable <name>|disable <name>|setup <name>]",
                                       kind="command")
        return
    app.run_worker(lambda: _providers_list_worker(app), thread=True, name="providers-list", group="providers-list")


def _providers_list_worker(app) -> None:
    from halo_harness.providers_cli import format_providers_table, provider_rows
    text = format_providers_table(provider_rows())
    app.call_from_thread(app.transcript.add_note, text, kind="command")


async def _handle_doctor(app, _args: str) -> None:
    app.run_worker(lambda: _doctor_worker(app), thread=True, name="doctor", group="doctor")


def _doctor_worker(app) -> None:
    from halo_harness.doctor import run_checks
    lines, _ok = run_checks(cwd=app.cwd)
    app.call_from_thread(app.transcript.add_note, "\n".join(lines), kind="command")


async def _handle_update(app, _args: str) -> None:
    app.run_worker(lambda: _update_worker(app), thread=True, name="update", group="update")


def _update_worker(app) -> None:
    """Halo 2.0.2 round 6: the check itself (installed_build/
    latest_available/commits_between -- a git/network call) off the UI
    thread; the dialog only ever gets built, pushed and read from the UI
    thread via `call_from_thread`, same `_palette_worker`/`_on_palette_
    pick` shape `tui/app.py`'s own Ctrl+P already uses."""
    from pathlib import Path
    from halo_harness import update as upd
    from halo_harness.tui.dialogs.update_dialog import UpdateDialog
    build = upd.installed_build()
    avail = upd.latest_available(upd.default_channel(build))
    kind = upd.install_kind()
    checkout = Path(build["checkout"]) if build.get("checkout") else None
    lines, _count = upd.commits_between(build.get("commit"), avail.get("commit"), checkout=checkout)
    up_to_date = bool(build.get("commit") and avail.get("commit") and build["commit"] == avail["commit"])
    installed_line = upd.format_version_line(build)[len("halo "):]
    if avail.get("commit"):
        ref = f", {avail['ref']}" if avail.get("ref") else ""
        available_line = f"{avail['commit']}{ref}"
    else:
        available_line = f"unknown ({avail.get('reason') or 'no reason given'})"
    dialog = UpdateDialog(installed_line, available_line, lines, kind.get("reinstall_cmd"), up_to_date=up_to_date)
    app.call_from_thread(app.push_screen, dialog, lambda result: _on_update_dialog_result(app, result))


def _on_update_dialog_result(app, result) -> None:
    """Enter ("update"): the ONLY thing this does is exit the TUI itself
    with `update.RESTART_EXIT_CODE` -- `cli.main` (past `run_tui()`,
    Textual already torn down) is what actually runs the reinstall and
    relaunches with `--continue`; see `cli._apply_update_and_relaunch`.
    Esc (`None`): nothing happens at all."""
    if result == "update":
        from halo_harness.update import RESTART_EXIT_CODE
        app.exit(return_code=RESTART_EXIT_CODE)


def update_check_startup_worker(app) -> None:
    """Halo 2.0.2 round 6: the SAME "stale -> background refresh" shape
    `catalog_auto_refresh_worker` uses, called once at TUI launch
    (`tui/app.py`'s own `on_mount`) -- a one-line transcript note, at most
    once a day, ONLY when the (cache-first, 24h TTL) check already knows
    an update is available; never a fresh forced check, never on the UI
    thread. Off entirely with `update.check: false`/`update.notify:
    false`, or `BRIDGE_TEST_NO_BACKGROUND_NET=1` (never touch the network
    from a test)."""
    from halo_harness.config.paths import background_net_disabled
    if background_net_disabled():
        return
    from halo_harness.theme import get_config_value
    if get_config_value("update.check", True) is False or get_config_value("update.notify", True) is False:
        return
    state_dir = getattr(app.controller, "state_dir", None)
    if state_dir is None:
        return
    from halo_harness import update as upd
    build = upd.installed_build()
    avail = upd.latest_available(upd.default_channel(build), state_dir=state_dir)
    if not avail.get("commit") or not build.get("commit") or avail["commit"] == build["commit"]:
        return
    if not upd.note_due_today(state_dir):
        return
    new_version = avail["ref"][1:] if (avail.get("ref") or "").startswith("v") else build["version"]
    text = f"update available: {build['version']} {build['commit']} -> {new_version} {avail['commit']}, /update"
    app.call_from_thread(app.transcript.add_note, text, kind="note")


async def _handle_mcp(app, _args: str) -> None:
    from halo_harness.tui.dialogs.mcp_status import McpStatus

    list_fn = getattr(app.controller, "list_mcp_servers", None)
    servers = list_fn() if list_fn is not None else []
    # round4 brief item 1: every new repair action is its own OPTIONAL
    # Controller callable -- `getattr(..., None)` throughout so an older
    # FakeController (or a future test double) missing one just disables
    # that single key instead of making `/mcp` itself unusable.
    app.push_screen(McpStatus(
        servers, reconnect=app.controller.reconnect_mcp,
        approve=app.controller.approve_mcp_server,
        reconnect_all=getattr(app.controller, "reconnect_all_mcp", None),
        login=getattr(app.controller, "login_mcp_server", None),
        test=getattr(app.controller, "test_mcp_server", None),
        disable=getattr(app.controller, "set_mcp_server_disabled", None),
        resolve_config=getattr(app.controller, "resolve_mcp_config", None),
    ))


async def _handle_tasks(app, _args: str) -> None:
    app.action_toggle_tasks()


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


async def _handle_intro(app, _args: str) -> None:
    """2.0.0 Launch intro: `/intro` replays it -- a FRESH `IntroLine`
    mounted at the current transcript position (never the launch-time
    one, which may have scrolled/folded away by now) and started typing
    again from scratch. `app.intro_line` is repointed at this new widget
    so a keypress/submitted prompt during the replay still skips it
    instantly, same contract as the launch-time line."""
    from halo_harness import __version__ as _halo_version
    from halo_harness.tui.widgets.transcript import IntroLine

    widget = IntroLine(f"I am just a copy, of a copy, of a copy... halo {_halo_version}")
    app.intro_line = widget
    await app.transcript.mount_widget(widget)


async def _handle_resume(app, args: str) -> None:
    # U5/review must-do: `list_sessions()` is file I/O -- off the UI thread.
    # H13 Part C: `/resume <text>` opens the picker ALREADY filtered by
    # `<text>` (the SAME fuzzy filter -- title/first prompt/cwd/model -- the
    # picker's own live Input box uses) instead of ignoring it.
    query = (args or "").strip()
    app.run_worker(lambda: _resume_list_worker(app, query), thread=True, name="list-sessions",
                    group="list-sessions")


def _resume_list_worker(app, query: str) -> None:
    sessions = app.controller.list_sessions()
    app.call_from_thread(_open_resume_picker, app, sessions, query)


def _open_resume_picker(app, sessions, query: str = "") -> None:
    from halo_harness.tui.dialogs.session_picker import SessionPicker

    def _on_pick(session_id) -> None:
        if session_id:
            app.controller.resume(session_id)

    app.push_screen(SessionPicker(sessions, initial_query=query), _on_pick)


async def _handle_permissions(app, _args: str) -> None:
    from halo_harness.tui.dialogs.permissions import PermissionsDialog

    list_fn = getattr(app.controller, "list_permission_rules", None)
    rules = list_fn() if list_fn is not None else []
    app.push_screen(PermissionsDialog(rules, mode=app.status_bar.mode,
                                       add_rule=lambda text: app.controller.add_permission_rule(text, "session")))


async def _handle_exit(app, _args: str) -> None:
    await app.action_quit_now()


async def _handle_theme(app, args: str) -> None:
    from halo_harness import theme as theme_mod

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
    # `halo_harness.telemetry`, run OFF the UI thread (same worker pattern
    # U5 used for `/resume`'s `list_sessions` -- see `_handle_resume`/
    # `_resume_list_worker` above) since it globs and parses every session
    # JSONL under the project.
    tokens = (args or "").split()
    if "--models" in tokens or "--tools" in tokens:
        show_models, show_tools = "--models" in tokens, "--tools" in tokens
        app.run_worker(lambda: _stats_models_worker(app, show_models, show_tools), thread=True,
                        name="stats-models", group="stats-models")
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
    from halo_harness import telemetry
    from halo_harness.config.paths import project_slug

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
    app.run_worker(lambda: _rewind_picker_worker(app), thread=True, name="rewind-list", group="rewind-list")


def _rewind_picker_worker(app) -> None:
    steps = app.controller.shadow_steps()
    app.call_from_thread(_open_rewind_picker, app, steps)


def _open_rewind_picker(app, steps: list) -> None:
    from halo_harness.tui.dialogs.rewind_picker import RewindPicker

    def _on_pick(step_id) -> None:
        if not step_id:
            return
        step = next((s for s in steps if s.get("id") == step_id), None)
        if step is not None:
            app.call_next(_show_rewind_confirmation, app, step, "rewind")

    app.push_screen(RewindPicker(steps), _on_pick)


async def _show_rewind_confirmation(app, step: dict, verb: str) -> None:
    # 1.0.1 part 2 (reviewer minor): the ONE choke point every /rewind,
    # /undo, /redo and rewind-picker-pick path funnels through before
    # mounting the actual confirmation card -- see _pending_card_note.
    note = _pending_card_note(app, verb)
    if note:
        app.notify(note, title=f"/{verb}")
        return
    from halo_harness.tui.widgets.cards import RewindCard

    def on_decide(confirmed: bool) -> None:
        app.clear_pending_card()
        if confirmed:
            app.run_worker(lambda: _apply_rewind_worker(app, step["id"], verb), thread=True, name="rewind-apply",
                            group="rewind-apply")

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
        from halo_harness.tui.keys import load_keymap
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
    # 1.0.1 part 2 (reviewer minor): checked before the scan/draft worker
    # even starts (a real model call) -- no point drafting candidates only
    # to discover the review card can't be shown.
    note = _pending_card_note(app, "improve")
    if note:
        app.notify(note, title="/improve")
        return
    session = getattr(app.controller, "session", None)
    if session is None:
        app.notify("/improve needs a real session.", severity="warning", title="/improve")
        return
    from halo_harness.improve.config import load_improve_config
    cfg = load_improve_config()
    if not cfg.enabled:
        app.notify("/improve is disabled (improve.enabled=false).", title="/improve")
        return
    app.notify("Scanning recent sessions and drafting candidates…", title="/improve")
    app.run_worker(lambda: _improve_draft_worker(app, session, cfg), thread=True, name="improve-draft",
                    group="improve-draft")


def _improve_draft_worker(app, session, cfg) -> None:
    from halo_harness.config.paths import project_slug
    from halo_harness.improve import draft as draft_mod
    from halo_harness.improve import evidence as evidence_mod
    from halo_harness.improve.config import since_str

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
    from halo_harness.improve.dismissed import is_dismissed, load_dismissed

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
    from halo_harness.tui.widgets.cards import ImproveCard

    state = getattr(app, "_improve_state", None)
    if not state:
        return
    queue, idx, session = state["queue"], state["index"], state["session"]
    if idx >= len(queue):
        app.clear_pending_card()
        app.notify("/improve: review complete.", title="/improve")
        app._improve_state = None
        return

    from halo_harness.improve import apply as apply_mod

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
            app.run_worker(lambda: _apply_worker(app, session, candidate), thread=True, name="improve-apply",
                            group="improve-apply")
        elif action == "edit":
            _edit_candidate_then_apply(app, session, candidate)
        elif action == "dismiss":
            from halo_harness.improve.apply import candidate_hash
            from halo_harness.improve.dismissed import add_dismissed
            add_dismissed(candidate_hash(candidate))
        elif action == "quit":
            state["index"] = len(queue)
        app.call_next(_show_next_improve_card, app)

    card = ImproveCard(candidate=candidate, provenance_line=comment, index=idx + 1, total=len(queue),
                        diff_lines=diff_lines, excerpt_text=excerpt_text, on_action=on_action)
    await app.transcript.mount_widget(card)
    app.set_pending_card(card)


def _apply_worker(app, session, candidate) -> None:
    from halo_harness.improve import apply as apply_mod

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
        app.run_worker(lambda: _apply_worker(app, session, candidate), thread=True, name="improve-apply",
                        group="improve-apply")
        return
    fd, tmp_path_str = tempfile.mkstemp(suffix=".md", prefix="halo-improve-")
    tmp_path = Path(tmp_path_str)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(candidate.body)
        # 1.0.1 part 2 fixpass finding 14: same watchdog-pause pair
        # tui/app.py's own Ctrl+E handler uses -- `app.suspend()` blocks the
        # main thread for as long as the external editor runs, which is not
        # a hang.
        app._enter_suspend_for_editor()
        try:
            with app.suspend():
                sp.run(f'{editor} "{tmp_path}"', shell=True)
        finally:
            app._exit_suspend_for_editor()
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
    app.run_worker(lambda: _apply_worker(app, session, candidate), thread=True, name="improve-apply",
                    group="improve-apply")


# ============================================================================
# W4c item 2: /copy (last assistant reply), /copy code [N] (its last fenced
# block, or the Nth), /copy tool (last tool output). All three are synchronous
# (plain list/string work over widgets already in memory, no worker needed)
# and go through `app.perform_copy` (tui/app.py), the one choke point that
# applies `clean_copy_text`/the CRLF config and shows the "Copied ..." toast.
# ============================================================================

def _last_widget_of_type(app, widget_type):
    for widget in reversed(app.transcript.widgets_in_order()):
        if isinstance(widget, widget_type):
            return widget
    return None


async def _handle_copy(app, args: str) -> None:
    from halo_harness.tui.widgets.cards import ToolCard
    from halo_harness.tui.widgets.transcript import AssistantText

    tokens = (args or "").split()
    sub = tokens[0].lower() if tokens else ""

    if sub == "tool":
        card = _last_widget_of_type(app, ToolCard)
        if card is None:
            app.notify("No tool output yet to copy.", severity="warning", title="/copy")
            return
        from halo_harness.tui.clipboard import clean_copy_text
        app.perform_copy(clean_copy_text(card.copy_text()), label="last tool output")
        return

    if sub == "code":
        widget = _last_widget_of_type(app, AssistantText)
        if widget is None:
            app.notify("No assistant reply yet to copy from.", severity="warning", title="/copy")
            return
        from halo_harness.tui.clipboard import clean_code_text, extract_fenced_code_blocks
        blocks = extract_fenced_code_blocks(widget.raw_text)
        if not blocks:
            app.notify("The last reply has no code blocks.", severity="warning", title="/copy")
            return
        index_text = tokens[1] if len(tokens) > 1 else ""
        if not index_text:
            app.perform_copy(clean_code_text(blocks[-1]), label="last code block")
            return
        try:
            n = int(index_text)
        except ValueError:
            app.notify(f"/copy code: {index_text!r} is not a number.", severity="error", title="/copy")
            return
        if not (1 <= n <= len(blocks)):
            app.notify(f"The last reply has {len(blocks)} code block(s); {n} is out of range.",
                       severity="error", title="/copy")
            return
        app.perform_copy(clean_code_text(blocks[n - 1]), label=f"code block {n}")
        return

    if sub:
        app.notify("Usage: /copy, /copy code [N], or /copy tool", severity="error", title="/copy")
        return

    widget = _last_widget_of_type(app, AssistantText)
    if widget is None:
        app.notify("No assistant reply yet to copy.", severity="warning", title="/copy")
        return
    from halo_harness.tui.clipboard import clean_copy_text
    app.perform_copy(clean_copy_text(widget.copy_text()), label="last reply")


# ============================================================================
# Halo 2.0.2 round 7: /setup [roles|orgs] -- pushes the init wizard's own
# Roles/Organizations step(s) straight onto THIS live app's screen stack
# (the "modal screen stack over the session" the brief names), reusing
# the EXACT SAME step classes `halo init`/`halo setup`'s own standalone
# `InitWizardApp` uses. Bare /setup chains roles -> orgs -> a short
# summary (the "set up roles and orgs later" path); /setup roles or
# /setup orgs opens just that one step. Finishing (or quitting) pops
# every screen THIS call pushed, landing back on the live session with
# nothing else to do -- config.json changes are already live the next
# time anything reads them, and the roles step pushes a freshly-applied
# template into the LIVE session's own role table too (see its own
# `commit()`).
# ============================================================================

async def _handle_setup(app, args: str) -> None:
    from halo_harness.tui.dialogs.init_wizard import build_truncated_state, first_step_screen
    sub = (args or "").strip().lower()
    if sub == "roles":
        step_keys = ("roles",)
    elif sub == "orgs":
        step_keys = ("orgs",)
    elif sub:
        await app.transcript.add_note(f"Usage: /setup, /setup roles, or /setup orgs (got {sub!r})", kind="command")
        return
    else:
        step_keys = ("roles", "orgs", "summary")
    state = build_truncated_state(app.cwd, step_keys=step_keys, on_finish=lambda _app: None)
    app.push_screen(first_step_screen(state))
