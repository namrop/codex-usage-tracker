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
    # Looked up online 2026-08-30 (keeper-directed: "the real price tables").
    # Anthropic: published API list prices, cache write = 5-minute tier
    # (1-hour write is 2x base input — not modeled; our traffic is 5m).
    "anthropic": {
        "models": {
            "claude-fable-5": {"input": "10", "output": "50", "cache_read": "1.00", "cache_write": "12.50"},
            "claude-opus-5": {"input": "5", "output": "25", "cache_read": "0.50", "cache_write": "6.25"},
            "claude-opus-4-8": {"input": "5", "output": "25", "cache_read": "0.50", "cache_write": "6.25"},
            "claude-opus-4-6": {"input": "5", "output": "25", "cache_read": "0.50", "cache_write": "6.25"},
            # Introductory rate through 2026-08-31; rises to 3/15/0.30/3.75
            # on Sep 1 — flip these four numbers then.
            "claude-sonnet-5": {"input": "2", "output": "10", "cache_read": "0.20", "cache_write": "2.50"},
            "claude-sonnet-4-6": {"input": "3", "output": "15", "cache_read": "0.30", "cache_write": "3.75"},
            "claude-haiku-4-5": {"input": "1", "output": "5", "cache_read": "0.10", "cache_write": "1.25"},
        },
        # Unlisted anthropic models price at the fable tier (conservative-high).
        "default": {"input": "10", "output": "50", "cache_read": "1.00", "cache_write": "12.50"},
    },
    # OpenAI flagship meter: gpt-5.6-sol and gpt-5.5 share $5/$30, cached
    # input 10% of standard. (A separate long-context meter exists at
    # $10/$45 — not modeled.) terra/luna priced individually.
    "openai": {
        "models": {
            "gpt-5.6-sol": {"input": "5", "output": "30", "cache_read": "0.50", "cache_write": "0"},
            "gpt-5.5": {"input": "5", "output": "30", "cache_read": "0.50", "cache_write": "0"},
            "gpt-5.6-terra": {"input": "2", "output": "12", "cache_read": "0.20", "cache_write": "0"},
            "gpt-5.6-luna": {"input": "0.20", "output": "1.20", "cache_read": "0.02", "cache_write": "0"},
        },
        "default": {"input": "5", "output": "30", "cache_read": "0.50", "cache_write": "0"},
    },
    "openai-codex": {
        "models": {
            "gpt-5.6-sol": {"input": "5", "output": "30", "cache_read": "0.50", "cache_write": "0"},
            "gpt-5.5": {"input": "5", "output": "30", "cache_read": "0.50", "cache_write": "0"},
        },
        "default": {"input": "5", "output": "30", "cache_read": "0.50", "cache_write": "0"},
    },
    # Z.ai list: GLM-5.3 $1.40/$4.40, cached input $0.26 (storage currently
    # free). glm-5.3-flash has no published rate found — falls to the
    # glm-5.3 default (conservative-high).
    "zai": {
        "models": {
            "glm-5.3": {"input": "1.40", "output": "4.40", "cache_read": "0.26", "cache_write": "0"},
        },
        "default": {"input": "1.40", "output": "4.40", "cache_read": "0.26", "cache_write": "0"},
    },
    # Moonshot list: K3 $3/$15, cache-hit input $0.30.
    "kimi-coding": {
        "models": {
            "k3": {"input": "3", "output": "15", "cache_read": "0.30", "cache_write": "0"},
            "kimi-k3": {"input": "3", "output": "15", "cache_read": "0.30", "cache_write": "0"},
        },
        "default": {"input": "3", "output": "15", "cache_read": "0.30", "cache_write": "0"},
    },
    # DeepSeek list: v4-pro $0.95/$4.00, v4-flash $0.14/$0.28. Cache-hit
    # rate not found in the checked sources — ASSUMED 10% of input, the
    # provider convention; verify.
    "deepseek": {
        "models": {
            "deepseek-v4-pro": {"input": "0.95", "output": "4.00", "cache_read": "0.095", "cache_write": "0"},
            "deepseek-v4-flash": {"input": "0.14", "output": "0.28", "cache_read": "0.014", "cache_write": "0"},
        },
        "default": {"input": "0.95", "output": "4.00", "cache_read": "0.095", "cache_write": "0"},
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
