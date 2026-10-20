"""Versioned pricing and cost estimation (§42.2).

Cost must be reproducible after the fact, so the price table carries a
``version`` and *that version is recorded in every* ``llm.completed`` (Checklist
#40): a cost report must still add up after a provider changes its prices.
Cache reads are billed separately because most providers charge 3–10× less for
them and treating them as normal input silently overstates cost.
"""

from __future__ import annotations

from .canonical import Cost, Usage

#: Bump whenever any price below changes.
PRICING_VERSION = "2025-01"

#: Prices are USD per 1,000,000 tokens.  Example values — override with real ones.
DEFAULT_PRICING: dict = {
    "version": PRICING_VERSION,
    "models": {
        "deepseek-flash": {"input": 0.27, "cache_read": 0.07, "output": 1.10},
        "deepseek-v4-pro": {"input": 0.55, "cache_read": 0.14, "output": 2.19},
        "gpt-4o": {"input": 2.50, "cache_read": 1.25, "output": 10.00},
        "gpt-4o-mini": {"input": 0.15, "cache_read": 0.075, "output": 0.60},
        "claude-3-5-sonnet": {"input": 3.00, "cache_read": 0.30, "output": 15.00},
        "_default": {"input": 1.00, "cache_read": 0.10, "output": 3.00},
    },
}


def price_for(model: str, pricing: dict | None = None) -> dict:
    table = (pricing or DEFAULT_PRICING)["models"]
    if model in table:
        return table[model]
    # tolerate versioned names like "deepseek-flash-2025-01"
    for name, price in table.items():
        if name != "_default" and model.startswith(name):
            return price
    return table["_default"]


def cost_for(usage: Usage, model: str, pricing: dict | None = None) -> Cost:
    """Compute a :class:`Cost` from usage, splitting out the cache read."""
    table = pricing or DEFAULT_PRICING
    p = price_for(model, table)
    input_price = p["input"] / 1_000_000
    output_price = p["output"] / 1_000_000
    cache_price = p["cache_read"] / 1_000_000

    billable_input = max(0, usage.input_tokens - usage.cache_read)
    input_usd = billable_input * input_price
    output_usd = usage.output_tokens * output_price
    cache_usd = usage.cache_read * cache_price
    return Cost(
        input_usd=input_usd,
        output_usd=output_usd,
        cache_usd=cache_usd,
        total_usd=input_usd + output_usd + cache_usd,
        pricing_version=str(table.get("version", "")),
    )
