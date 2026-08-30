"""List-price estimation over canonical token aggregates.

Keeper-ruled 2026-08-30: "run the token data and the cached token data
through the pricing info for the providers to create an estimate" — the
subscription lanes report $0 actual cost, so the useful number is what the
same traffic WOULD cost at providers' list prices.

The table below is the pricing source of truth and is deliberately a
commented Python dict, not JSON, so every rate can carry its provenance.
Rates are USD per MILLION tokens. Entries marked ASSUMED were seeded
2026-08-30 from the nearest known tier and want keeper verification; a
model with no entry (and no provider default) counts as unpriced and is
reported as coverage, never silently priced.

Reasoning tokens bill at the output rate (the provider convention).
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Dict, Iterable, Optional

_MTOK = Decimal(1_000_000)

# provider -> {"models": {model: rates}, "default": rates | None}
# rates: {"input": x, "output": y, "cache_read": z, "cache_write": w}
LIST_PRICES: Dict[str, Dict[str, Any]] = {
    "anthropic": {
        "models": {
            # Opus tier (published API list prices).
            "claude-opus-5": {"input": "15", "output": "75", "cache_read": "1.50", "cache_write": "18.75"},
            "claude-opus-4-8": {"input": "15", "output": "75", "cache_read": "1.50", "cache_write": "18.75"},
            "claude-opus-4-6": {"input": "15", "output": "75", "cache_read": "1.50", "cache_write": "18.75"},
            # ASSUMED: fable priced at the opus tier — verify.
            "claude-fable-5": {"input": "15", "output": "75", "cache_read": "1.50", "cache_write": "18.75"},
            "claude-sonnet-5": {"input": "3", "output": "15", "cache_read": "0.30", "cache_write": "3.75"},
            "claude-haiku-4-5": {"input": "1", "output": "5", "cache_read": "0.10", "cache_write": "1.25"},
        },
        # Unlisted anthropic models price at the opus tier (conservative-high).
        "default": {"input": "15", "output": "75", "cache_read": "1.50", "cache_write": "18.75"},
    },
    # ASSUMED: gpt-5 family list pricing carried to 5.5/5.6 — verify.
    "openai": {
        "models": {},
        "default": {"input": "1.25", "output": "10", "cache_read": "0.125", "cache_write": "0"},
    },
    "openai-codex": {
        "models": {},
        "default": {"input": "1.25", "output": "10", "cache_read": "0.125", "cache_write": "0"},
    },
    # ASSUMED: GLM-4.5-era API list carried to glm-5.x — verify.
    "zai": {
        "models": {},
        "default": {"input": "0.60", "output": "2.20", "cache_read": "0.11", "cache_write": "0"},
    },
    # ASSUMED: kimi k2 list carried to k3 — verify.
    "kimi-coding": {
        "models": {},
        "default": {"input": "0.60", "output": "2.50", "cache_read": "0.15", "cache_write": "0"},
    },
    "deepseek": {
        "models": {},
        "default": {"input": "0.27", "output": "1.10", "cache_read": "0.07", "cache_write": "0"},
    },
    # Local inference is $0 by definition, and saying so beats "unpriced".
    "custom:gemma-4-moe-it": {
        "models": {},
        "default": {"input": "0", "output": "0", "cache_read": "0", "cache_write": "0"},
    },
}


def _rates_for(provider: str, model: str) -> Optional[Dict[str, Any]]:
    entry = LIST_PRICES.get(provider)
    if entry is None:
        return None
    return entry["models"].get(model) or entry.get("default")


def estimate_list_price(buckets: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    """Estimate list-price USD over (provider, model) token aggregates.

    Returns exact-decimal strings; unpriced tokens are surfaced as coverage,
    never silently priced.
    """
    total = Decimal(0)
    priced_tokens = 0
    unpriced_tokens = 0
    unpriced_providers: set[str] = set()
    for bucket in buckets:
        provider = str(bucket.get("provider") or "unknown")
        model = str(bucket.get("model") or "")
        tokens = int(bucket.get("total_tokens") or 0)
        rates = _rates_for(provider, model)
        if rates is None:
            unpriced_tokens += tokens
            unpriced_providers.add(provider)
            continue
        priced_tokens += tokens
        total += (
            Decimal(int(bucket.get("input_tokens") or 0)) * Decimal(rates["input"])
            + (
                Decimal(int(bucket.get("output_tokens") or 0))
                + Decimal(int(bucket.get("reasoning_tokens") or 0))
            ) * Decimal(rates["output"])
            + Decimal(int(bucket.get("cache_read_tokens") or 0)) * Decimal(rates["cache_read"])
            + Decimal(int(bucket.get("cache_write_tokens") or 0)) * Decimal(rates["cache_write"])
        ) / _MTOK
    denominator = priced_tokens + unpriced_tokens
    return {
        "estimate_usd": str(total.quantize(Decimal("0.01"))),
        "priced_token_share": (priced_tokens / denominator) if denominator else 1.0,
        "unpriced_providers": sorted(unpriced_providers),
    }
