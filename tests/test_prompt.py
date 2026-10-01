"""tests.test_prompt -- agent/prompt.py: finding 1 (no refusal/safety/cyber
language anywhere in the assembled system prompt), byte-stability, the
harness self-description section, and per-family notation.
"""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.helpers.runner import Ctx, new_registry, print_results, run_all
from halo_harness.agent.prompt import build_system_prompt, family_notation
from halo_harness.tools.registry import ToolRegistry

test, TESTS = new_registry()

_FORBIDDEN_RE = re.compile(r"refus|safety|cyber|sensitive", re.IGNORECASE)


def _sample_prompt(**overrides) -> str:
    kwargs = dict(
        model_label="or:deepseek/deepseek-v4.1-flash",
        cwd="/tmp/project",
        tool_definitions=ToolRegistry().definitions(),
        family="deepseek",
    )
    kwargs.update(overrides)
    return build_system_prompt(**kwargs)


@test
def test_finding_1_no_refusal_safety_cyber_language(ctx: Ctx):
    """H0 finding 1: IDENTITY_TEXT told the model "no built-in refusal
    policy, no safety heuristic, and no 'sensitive' or 'cyber' content
    classifier" -- exactly the words rolo ruled out. The fix deletes that
    sentence; this asserts none of those words appear ANYWHERE in the
    assembled prompt, across every family and with an append-system-prompt
    (which the CALLER controls -- but the harness's OWN text must be clean)."""
    for family in ("deepseek", "kimi", "glm", "qwen", "gemini", "grok", "claude", "gpt", "generic", "unknown-family"):
        prompt = _sample_prompt(family=family)
        m = _FORBIDDEN_RE.search(prompt)
        ctx.check(f"family={family!r}: no refus|safety|cyber|sensitive, found {m.group() if m else None!r}", m is None)


@test
def test_byte_stable_across_two_constructions(ctx: Ctx):
    p1 = _sample_prompt()
    p2 = _sample_prompt()
    ctx.check("identical inputs -> byte-identical prompt", p1 == p2)


@test
def test_model_label_appears_in_persona(ctx: Ctx):
    prompt = _sample_prompt(model_label="or:moonshotai/kimi-k3")
    ctx.check("persona line names the model", "moonshotai/kimi-k3" in prompt)


@test
def test_cwd_appears_at_the_end(ctx: Ctx):
    prompt = _sample_prompt(cwd="/home/user/proj")
    ctx.check("cwd line present", "Your working directory is /home/user/proj." in prompt)
    ctx.check("cwd line is the LAST section", prompt.rstrip().endswith("/home/user/proj."))


@test
def test_harness_self_description_present(ctx: Ctx):
    prompt = _sample_prompt()
    ctx.check("mentions CLAUDE.md", "CLAUDE.md" in prompt)
    ctx.check("mentions auto-memory / MEMORY.md", "MEMORY.md" in prompt or "Auto-memory" in prompt)
    ctx.check("mentions MCP servers", "MCP" in prompt)
    ctx.check("mentions permission modes / plan mode", "plan mode" in prompt.lower())
    ctx.check("mentions sub-agents", "sub-agent" in prompt.lower() or "sub-agents" in prompt.lower())
    ctx.check("tells the model to use native tool calling, never text", "native" in prompt.lower())


@test
def test_read_tool_listed_by_name(ctx: Ctx):
    prompt = _sample_prompt(tool_definitions=ToolRegistry().definitions())
    ctx.check("Read tool listed", "**Read**" in prompt)


@test
def test_per_family_notation_differs_and_is_present(ctx: Ctx):
    deepseek_note = family_notation("deepseek")
    kimi_note = family_notation("kimi")
    generic_note = family_notation("unknown-family-xyz")
    ctx.check("deepseek notation mentions reasoning replay", "reasoning" in deepseek_note.lower())
    ctx.check("kimi notation mentions tool-call ids", "id" in kimi_note.lower())
    ctx.check("unknown family falls back to the generic notation", generic_note == family_notation("generic"))
    prompt = _sample_prompt(family="kimi")
    ctx.check("kimi's own notation is embedded in the assembled prompt", kimi_note in prompt)


@test
def test_append_system_prompt_appended_last(ctx: Ctx):
    prompt = _sample_prompt(append_system_prompt="MARKER-XYZ-123")
    ctx.check("appended text present", "MARKER-XYZ-123" in prompt)
    ctx.check("appended text comes after the cwd line", prompt.rindex("MARKER-XYZ-123") > prompt.index("Your working directory"))


if __name__ == "__main__":
    ctx = Ctx()
    results, passed, failed, skipped = run_all(TESTS, ctx)
    sys.exit(print_results(results, passed, failed, skipped))
