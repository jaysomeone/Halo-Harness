"""halo_harness.ax_mode -- W4a `--ax-screen-reader`: "render screen-reader
friendly output (flat text, no decorative borders or animations)" (claude's
own wording) as a real, independent interactive loop -- never the Textual
TUI with CSS stripped (Textual itself draws a screen buffer; a screen reader
wants an ordinary scrolling terminal transcript instead). Reuses `tui.
bootstrap.build_controller` (the SAME session/registry/facade construction
the real TUI uses) so permission/question/plan asks, slash commands and
steering all behave identically -- only the RENDERING differs: one plain
line per event, `input()` for the next prompt and for an ask's answer.

Intentionally the smallest correct loop, not a transcript-replay/scrollback
UI of its own: a screen reader's own terminal/browser already provides
history navigation, so this never tries to reimplement it.
"""

from __future__ import annotations

import queue


def _render_tool_use(data: dict) -> str:
    name = data.get("name") or "?"
    return f"[tool] {name}"


def _render_tool_result(data: dict) -> str:
    ok = data.get("ok", True)
    summary = data.get("summary") or ""
    tag = "ok" if ok else "error"
    return f"[tool {tag}] {summary}"


def _ask_permission(controller, data: dict) -> None:
    request_id = data.get("id")
    name = data.get("name") or "?"
    print(f"\n[permission] {name}({data.get('input')}) -- allow? (y/n): ", end="", flush=True)
    try:
        reply = input().strip().lower()
    except EOFError:
        reply = "n"
    action = "allow" if reply in ("y", "yes", "1") else "deny"
    controller.answer_permission(request_id, {"action": action, "reason": "ax-screen-reader mode"})


def _ask_question(controller, data: dict) -> None:
    request_id = data.get("id")
    input_data = data.get("input") or {}
    question = input_data.get("question") or "(question)"
    options = input_data.get("options") or []
    print(f"\n[question] {question}")
    for i, opt in enumerate(options, 1):
        label = opt.get("label") if isinstance(opt, dict) else str(opt)
        print(f"  {i}. {label}")
    print("your answer: ", end="", flush=True)
    try:
        reply = input().strip()
    except EOFError:
        reply = ""
    # A bare number picks that option's label (closer to what a sighted
    # user clicking the card would send); anything else is free text --
    # `resolve_question`'s own contract explicitly allows a plain string.
    if reply.isdigit() and 1 <= int(reply) <= len(options):
        opt = options[int(reply) - 1]
        reply = opt.get("label") if isinstance(opt, dict) else str(opt)
    controller.answer_question(request_id, reply)


def _review_plan(controller, data: dict) -> None:
    print(f"\n[plan]\n{data.get('plan', '')}\napprove this plan? (y/n): ", end="", flush=True)
    try:
        reply = input().strip().lower()
    except EOFError:
        reply = "n"
    approved = reply in ("y", "yes", "1")
    controller.answer_plan(approved, feedback=("" if approved else reply))


def _drain_until_idle(controller) -> int:
    """Blocks on `controller.events` (a plain `queue.Queue`) printing one
    line per event until `turn_done` (or the worker thread ends, `None`
    sentinel) -- the same queue the Textual drain loop reads, so nothing
    about the session itself needs to know this isn't the real TUI."""
    exit_code = 0
    while True:
        ev = controller.events.get()
        if ev is None:
            return exit_code
        kind, data = ev.kind, (ev.data or {})
        if kind == "text_delta":
            print(data.get("text", ""), end="", flush=True)
        elif kind == "thinking_delta":
            pass  # flat text mode: no decorative dimmed-reasoning stream
        elif kind == "tool_use_ready":
            print(f"\n{_render_tool_use(data)}")
        elif kind == "tool_result":
            print(_render_tool_result(data))
        elif kind == "notification":
            print(f"\n[note] {data.get('text', '')}")
        elif kind == "error":
            print(f"\n[error] {data.get('message', '')}")
            exit_code = 1
        elif kind == "permission_request":
            _ask_permission(controller, data)
        elif kind == "question":
            _ask_question(controller, data)
        elif kind == "plan_review":
            _review_plan(controller, data)
        elif kind == "turn_done":
            print()
            reason = data.get("reason", "end_turn")
            if reason == "error":
                exit_code = 1
            return exit_code


def run_ax_screen_reader_mode(args) -> int:
    from halo_harness.tui.bootstrap import build_controller

    controller, _registry, _facade = build_controller(args)
    controller.start()
    print(f"halo (screen-reader mode) -- cwd {controller.cwd}, type /quit to exit")
    exit_code = 0
    try:
        if getattr(args, "prompt", None):
            controller.submit(args.prompt)
            exit_code = _drain_until_idle(controller)
        while True:
            try:
                line = input("> ")
            except EOFError:
                break
            line = line.strip()
            if not line:
                continue
            if line in ("/quit", "/exit"):
                break
            controller.submit(line)
            try:
                exit_code = _drain_until_idle(controller)
            except queue.Empty:
                pass
    finally:
        quit_fn = getattr(controller, "quit", None)
        if callable(quit_fn):
            try:
                quit_fn()
            except Exception:
                pass
    return exit_code
