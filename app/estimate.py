"""Uncached grok-4.3 batch estimate.

Short batch is $1.00/M input and $2.00/M output under 200,000 prompt tokens.
At or above 200,000 the whole call is $2.00/M input and $4.00/M output.
Those coefficients times a token count are already micros.
"""

from __future__ import annotations

SHORT_LIMIT = 200_000
COMPENDIUM_OUT = 8_000
DEV_OUT = 1_000
READBACK_OUT = 4_000
COMPENDIUM_MAX = 24_000
DEV_MAX = 4_000
READBACK_MAX = 8_000
ATTACHMENT = 8_000

PASSES = (
    ("promise", 1, "Promise and genre fit"),
    ("plot", 2, "Plot and structure"),
    ("character", 3, "Character"),
    ("stakes", 4, "Stakes and pacing"),
)


def words_to_prompt(words: int) -> int:
    return (words * 133) // 100 + 6_000


def stripe_fee_cents(price_cents: int) -> int:
    return (price_cents * 29 + 999) // 1000 + 30


def micros_to_cents(micros: int) -> int:
    return (micros + 9_999) // 10_000


def _rates(prompt_tokens: int) -> tuple[int, int]:
    if prompt_tokens >= SHORT_LIMIT:
        return 2, 4
    return 1, 2


def call_micros(prompt_tokens: int, output_tokens: int) -> int:
    rate_in, rate_out = _rates(prompt_tokens)
    return rate_in * prompt_tokens + rate_out * output_tokens


def planned_calls(
    prompt_base: int,
    *,
    enabled_asks: list[str],
    compendium: bool,
    readback: bool,
    focus: bool,
) -> list[dict]:
    calls = []
    attachment = ATTACHMENT if compendium else 0
    if compendium:
        calls.append(
            {
                "name": "compendium",
                "prompt_tokens": prompt_base,
                "output_tokens": COMPENDIUM_OUT,
                "max_tokens": COMPENDIUM_MAX,
            }
        )
    for ask, _pos, _title in PASSES:
        if ask not in enabled_asks:
            continue
        calls.append(
            {
                "name": ask,
                "prompt_tokens": prompt_base + attachment,
                "output_tokens": DEV_OUT,
                "max_tokens": DEV_MAX,
            }
        )
    if focus:
        calls.append(
            {
                "name": "focus",
                "prompt_tokens": prompt_base + attachment,
                "output_tokens": DEV_OUT,
                "max_tokens": DEV_MAX,
            }
        )
    if readback and compendium:
        calls.append(
            {
                "name": "readback",
                "prompt_tokens": prompt_base + attachment,
                "output_tokens": READBACK_OUT,
                "max_tokens": READBACK_MAX,
            }
        )
    return calls


def estimate_prompt(
    prompt_base: int,
    *,
    enabled_asks: list[str] | None = None,
    compendium: bool = True,
    readback: bool = True,
    focus: bool = False,
) -> dict:
    asks = list(enabled_asks) if enabled_asks is not None else [p[0] for p in PASSES]
    calls = planned_calls(
        prompt_base,
        enabled_asks=asks,
        compendium=compendium,
        readback=readback,
        focus=focus,
    )
    total = 0
    in_tok = 0
    out_tok = 0
    for call in calls:
        total += call_micros(call["prompt_tokens"], call["output_tokens"])
        in_tok += call["prompt_tokens"]
        out_tok += call["output_tokens"]
    return {
        "calls": calls,
        "input_tokens": in_tok,
        "output_tokens": out_tok,
        "cost_usd_micros": total,
        "token_cents": micros_to_cents(total),
    }


def estimate_words(words: int, **kwargs) -> dict:
    result = estimate_prompt(words_to_prompt(words), **kwargs)
    result["words"] = words
    result["prompt_base"] = words_to_prompt(words)
    return result


def gate(price_cents: int, token_cents: int) -> dict:
    fee = stripe_fee_cents(price_cents)
    net = price_cents - fee - token_cents
    return {
        "stripe_fee_cents": fee,
        "token_cents": token_cents,
        "editor_net_cents": net,
        "ok": fee + token_cents < price_cents and net > 0,
    }


def preflight_allows(price_cents: int, fee_cents: int, spent_micros: int, prompt_tokens: int, max_tokens: int) -> bool:
    nxt = call_micros(prompt_tokens, max_tokens)
    return fee_cents + micros_to_cents(spent_micros + nxt) < price_cents


def dollars(micros: int) -> str:
    return f"${micros / 1_000_000:.4f}".rstrip("0").rstrip(".")
