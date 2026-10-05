"""halo_harness.gym_reply_tasks -- Halo 2.0.3 round 5d: the two battery
tasks judged on the plain TEXT reply alone (no tool offered at all) --
context recall and instruction adherence. See `gym.py`'s module docstring
for what each score means; see `gym_send.py` for the shared real-turn
helper.
"""

from __future__ import annotations

from typing import Optional

from halo_harness.gym import RatioScore, sample_excerpt
from halo_harness.gym_send import send_turn_for

# Fix pass (2026-10-04 live-run finding, qwen3.8:27b, a thinking-by-
# default model): both reply-only tasks used to request only 16/32 output
# tokens -- too small for a model that spends its OWN tokens reasoning
# before ever emitting the real answer. Measured directly against the
# live daemon: the one-word/no-preamble prompts came back completely
# EMPTY at 16 tokens (`stop_reason="max_tokens"`, every token spent on
# thinking) and needed 64+ to reliably finish; the recall prompt (much
# longer, so more to reason about) needed 64+ too, after coming back
# empty at 32. 160 is a generous, flat margin above every measured
# threshold -- cheap for a non-thinking model (it stops at `end_turn`
# long before 160 regardless) and safe for a thinking one.
_THINKING_SAFE_MAX_TOKENS = 160

# A FIXED needle (never randomized) -- this task measures whether a fact
# planted at a known depth survives this host's actual context handling,
# not memorization resistance; a fixed, inspectable value also makes the
# hermetic mock_ollama scenarios (pass/fail/half-pass) trivial to script.
_NEEDLE_CODE = "7k2p9"
_NEEDLE_FACT = f"The secret checkpoint code is {_NEEDLE_CODE}."
_NEEDLE_QUESTION = "\n\nWhat is the secret checkpoint code mentioned above? Reply with only the code."
_FILLER_SENTENCE = "The quick brown fox jumps over the lazy dog near the quiet river at dawn. "
_NEEDLE_DEPTH_FRACTION = 0.12
# Never builds a multi-hundred-KB prompt even against a 131072-token fitted
# context -- plenty to exercise real context handling without turning a
# single gym attempt into a multi-minute prefill.
_RECALL_TOKEN_CAP_FULL = 32_000
_RECALL_TOKEN_CAP_QUICK = 4_000
_CHARS_PER_TOKEN = 4


def _build_recall_prompt(fitted_context: Optional[int], *, quick: bool) -> str:
    cap = _RECALL_TOKEN_CAP_QUICK if quick else _RECALL_TOKEN_CAP_FULL
    budget_tokens = min(fitted_context or cap, cap)
    # leaves headroom for the system prompt, the question, and the reply.
    target_chars = max(256, int(budget_tokens * 0.6)) * _CHARS_PER_TOKEN
    reps = max(1, target_chars // len(_FILLER_SENTENCE))
    filler = _FILLER_SENTENCE * reps
    depth_idx = int(len(filler) * _NEEDLE_DEPTH_FRACTION)
    depth_idx = filler.rfind(" ", 0, depth_idx) if depth_idx else 0  # land on a word boundary
    return f"{filler[:depth_idx]} {_NEEDLE_FACT} {filler[depth_idx:]}{_NEEDLE_QUESTION}"


def run_context_recall_task(*, host, route, profile, decision, n: int, quick: bool, state_dir,
                             timing: Optional[list] = None) -> RatioScore:
    """N attempts at the SAME needle-at-depth prompt, sized to this
    model's own fitted context (`decision.num_ctx`) -- brief: "a needle at
    about 12 percent depth of a prompt sized to the model's fitted
    window". `succeeded` requires the needle code to appear in the reply
    text (case-insensitive, and tolerant of surrounding punctuation/quotes
    -- a plain substring check already is, since "'7k2p9'." still
    contains "7k2p9"; case-folding is the one further tolerance added by
    this fix pass)."""
    prompt = _build_recall_prompt(decision.num_ctx, quick=quick)
    result = RatioScore(attempted=n)
    for _ in range(n):
        turn = send_turn_for(
            host=host, route=route, profile=profile, decision=decision,
            system_text="Answer the user's question using only the text they provide.",
            messages=[{"role": "user", "content": [{"type": "text", "text": prompt}]}],
            tools=None, requested_max_tokens=_THINKING_SAFE_MAX_TOKENS, state_dir=state_dir,
        )
        if timing is not None and not turn.error and turn.timing_ns:
            timing.append(turn.timing_ns)
        result.samples.append(sample_excerpt(turn.text) if not turn.error else sample_excerpt(f"[error] {turn.error}"))
        if not turn.error and _NEEDLE_CODE.lower() in turn.text.lower():
            result.succeeded += 1
    return result


# Instruction-adherence checks (brief: "the plain-sentence reply rules:
# one word when asked for one word, no preamble") -- two fixed prompts,
# alternated across N attempts so the final ratio reflects both rules
# rather than just one repeated.
_ONE_WORD_PROMPT = "Reply with exactly one word, nothing else: what color is grass on a healthy lawn?"
_NO_PREAMBLE_PROMPT = "No preamble, no explanation, no restating the question -- reply with only the number: what is 7 + 5?"


def _check_one_word(text: str) -> bool:
    cleaned = text.strip().strip(".!\"'")
    return bool(cleaned) and len(cleaned.split()) == 1 and cleaned.lower() == "green"


def _check_no_preamble(text: str) -> bool:
    cleaned = text.strip()
    return cleaned.startswith("12") and len(cleaned) <= 8


def run_instruction_adherence_task(*, host, route, profile, decision, n: int, state_dir,
                                    timing: Optional[list] = None) -> RatioScore:
    result = RatioScore(attempted=n)
    for i in range(n):
        one_word_turn = (i % 2 == 0)
        prompt = _ONE_WORD_PROMPT if one_word_turn else _NO_PREAMBLE_PROMPT
        turn = send_turn_for(
            host=host, route=route, profile=profile, decision=decision,
            system_text="Follow the reply-format instruction in the user's message exactly.",
            messages=[{"role": "user", "content": [{"type": "text", "text": prompt}]}],
            tools=None, requested_max_tokens=_THINKING_SAFE_MAX_TOKENS, state_dir=state_dir,
        )
        if timing is not None and not turn.error and turn.timing_ns:
            timing.append(turn.timing_ns)
        result.samples.append(sample_excerpt(turn.text) if not turn.error else sample_excerpt(f"[error] {turn.error}"))
        if turn.error:
            continue
        passed = _check_one_word(turn.text) if one_word_turn else _check_no_preamble(turn.text)
        if passed:
            result.succeeded += 1
    return result
