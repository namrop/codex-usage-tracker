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

Reasoning tokens are NOT billed on top of output tokens: canonical
``output_tokens`` already includes reasoning (see README, "reasoning is
diagnostic and is not added twice where canonical output already includes
it"), so the estimate bills ``output_tokens`` alone at the output rate.

Coverage limitations, stated rather than papered over (see ``RATE_BASIS``):

- Service tiers are NOT modeled. OpenAI's Batch and Flex tiers bill at 0.5x
  and Fast/Priority at 2x, and the canonical ledger does not record which
  tier served a request, so every estimate assumes the standard tier and
  says so through the ``rate_basis`` field. The estimate is a list-price
  ESTIMATE, never a claim of actual billed rate.
- ``historical_aggregate`` rows bundle many calls into one row, so their
  summed prompt cannot classify any single request's context length. They
  price at the standard-context rate and are reported separately as
  ``aggregate_context_tokens``.
- Long-context thresholds are verified per model. Astra and GPT-5.6 Sol
  independently document the same 272,000-token boundary. Models awaiting
  threshold evidence can be listed in ``LONG_CONTEXT_UNVERIFIED``; this set
  is currently empty.

Provenance note: the 2026-09-06 OpenAI rates below were retrieved from the
vendor pages by the parent agent. They are dated source observations, not
keeper rulings, and remain list-price estimates under the stated limits.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Dict, Iterable, Mapping, Optional, Tuple

_MTOK = Decimal(1_000_000)

# The gpt-6-astra long-context meter, read from
# developers.openai.com/api/docs/models/gpt-6-astra and /api/docs/pricing by
# the parent agent on 2026-09-06: a request whose PROMPT exceeds this many
# tokens bills every token of that request — input, cache read, cache write
# and output alike — at the long-context rate.
#
# Canonical ``input_tokens`` EXCLUDES cache, so the prompt this threshold
# tests is input + cache_read + cache_write for ONE request. A model's
# aggregate token total says nothing about it.
#
# This threshold is applied ONLY to models whose long meter carries it (a
# nested "long" table). It is NOT generalized to other OpenAI models.
LONG_CONTEXT_PROMPT_TOKENS = 272_000

# (provider, model) pairs that publish a long-context rate we deliberately do
# NOT apply, because the token threshold that triggers it has not been
# verified for that model. Their traffic prices at the short rate and the
# estimate reports the affected tokens, so the understatement is visible
# instead of being hidden behind a borrowed threshold.
LONG_CONTEXT_UNVERIFIED: frozenset[tuple[str, str]] = frozenset()

# Which service tier every estimate assumes. Batch/Flex (0.5x) and
# Fast/Priority (2x) are not modeled because the ledger does not record the
# tier; the estimate carries this marker so no consumer can read it as an
# actual-rate figure.
RATE_BASIS = "standard"


def request_context_basis(row: Mapping[str, Any]) -> str:
    """Classify ONE canonical usage row for long-context pricing.

    Returns ``"long"`` when this single request's prompt exceeds
    :data:`LONG_CONTEXT_PROMPT_TOKENS`, ``"standard"`` when it does not, and
    ``"aggregate"`` when the row is not a single request at all — a
    ``historical_aggregate`` bundles many calls, so its summed prompt is not
    evidence that any one call was long-context. Aggregates price at the
    standard rate and are reported as such rather than guessed either way.
    """
    if row.get("record_kind") != "api_attempt":
        return "aggregate"
    prompt = sum(
        int(row.get(field) or 0)
        for field in ("input_tokens", "cache_read_tokens", "cache_write_tokens")
    )
    return "long" if prompt > LONG_CONTEXT_PROMPT_TOKENS else "standard"

# provider -> {"models": {model: rates}, "default": rates | None}
# rates: {"input": x, "output": y, "cache_read": z, "cache_write": w}
LIST_PRICES: Dict[str, Dict[str, Any]] = {
    # Looked up online 2026-08-30 (keeper-directed: "the real price tables").
    # Anthropic: published API list prices, cache write = 5-minute tier
    # (1-hour write is 2x base input — not modeled; our traffic is 5m).
    "anthropic": {
        "models": {
            "claude-fable-5": {"input": "10", "output": "50", "cache_read": "1.00", "cache_write": "12.50"},
            # claude-fable-5-1: cache-read $0.25 confirmed from Anthropic's
            # current published table (checked 2026-09-02); cache write
            # follows the Fable-tier 1.25x-input convention used for
            # claude-fable-5 above.
            "claude-fable-5-1": {"input": "10", "output": "50", "cache_read": "0.25", "cache_write": "12.50"},
            "claude-opus-5": {"input": "5", "output": "25", "cache_read": "0.50", "cache_write": "6.25"},
            "claude-opus-4-8": {"input": "5", "output": "25", "cache_read": "0.50", "cache_write": "6.25"},
            "claude-opus-4-6": {"input": "5", "output": "25", "cache_read": "0.50", "cache_write": "6.25"},
            # Published rate as of 2026-09-02 is still 2/10/0.20/2.50 (the
            # earlier "rises on Sep 1" note did not materialize in the
            # published table). Re-verify against the live table before
            # changing these four numbers.
            "claude-sonnet-5": {"input": "2", "output": "10", "cache_read": "0.20", "cache_write": "2.50"},
            "claude-sonnet-4-6": {"input": "3", "output": "15", "cache_read": "0.30", "cache_write": "3.75"},
            "claude-haiku-4-5": {"input": "1", "output": "5", "cache_read": "0.10", "cache_write": "1.25"},
        },
        # Unlisted anthropic models price at the fable tier (conservative-high).
        "default": {"input": "10", "output": "50", "cache_read": "1.00", "cache_write": "12.50"},
    },
    # OpenAI, read from developers.openai.com/api/docs/models/gpt-6-astra and
    # /api/docs/pricing by the parent AGENT on 2026-09-06 (agent-sourced, not
    # keeper-confirmed).
    #
    # gpt-6-astra standard: 10 / 1.00 / 12.50 / 50 (in / cache read / cache
    # write / out per Mtok). Above a 272,000-token prompt the WHOLE request
    # bills on the long-context meter at 20 / 2.00 / 25 / 75 — that is what
    # the nested "long" table is, and _rates_with_context selects it per
    # request. Each model's threshold was verified independently.
    #
    # gpt-5.6-sol is on a promotional rate through at least 2026-11-21,
    # replacing the stale 5/30 entry seeded 2026-08-30: short
    # 4 / 0.40 / 5 / 20, long 8 / 0.80 / 10 / 30. Its own model page at
    # developers.openai.com/api/docs/models/gpt-5.6-sol independently
    # confirms the >272,000-token prompt threshold (agent-read 2026-09-06).
    # RE-VERIFY the promotional rates after 2026-11-21.
    #
    # There is deliberately NO provider default (removed 2026-09-06): a new
    # OpenAI model ID must surface as unpriced coverage, because pricing it
    # off a stale flagship rate is a fabricated number and reporting $0 would
    # read as an actual cost of zero.
    "openai": {
        "models": {
            "gpt-6-astra": {
                "input": "10", "output": "50", "cache_read": "1.00", "cache_write": "12.50",
                "long": {"input": "20", "output": "75", "cache_read": "2.00", "cache_write": "25"},
            },
            "gpt-5.6-sol": {
                "input": "4", "output": "20", "cache_read": "0.40", "cache_write": "5",
                "long": {
                    "input": "8", "output": "30", "cache_read": "0.80", "cache_write": "10",
                },
            },
            "gpt-5.5": {"input": "5", "output": "30", "cache_read": "0.50", "cache_write": "0"},
            "gpt-5.6-terra": {"input": "2", "output": "12", "cache_read": "0.20", "cache_write": "0"},
            "gpt-5.6-luna": {"input": "0.20", "output": "1.20", "cache_read": "0.02", "cache_write": "0"},
        },
        "default": None,
    },
    # The Codex meter carries the same published rates for the models the
    # Codex lane actually runs. Subscription-included traffic still records
    # $0 ACTUAL; this table only estimates what it would have cost.
    "openai-codex": {
        "models": {
            "gpt-6-astra": {
                "input": "10", "output": "50", "cache_read": "1.00", "cache_write": "12.50",
                "long": {"input": "20", "output": "75", "cache_read": "2.00", "cache_write": "25"},
            },
            "gpt-5.6-sol": {
                "input": "4", "output": "20", "cache_read": "0.40", "cache_write": "5",
                "long": {
                    "input": "8", "output": "30", "cache_read": "0.80", "cache_write": "10",
                },
            },
            "gpt-5.5": {"input": "5", "output": "30", "cache_read": "0.50", "cache_write": "0"},
        },
        "default": None,
    },
    # Z.ai list: GLM-5.3 $1.40/$4.40, cached input $0.26 (storage currently
    # free).
    #
    # DOCUMENTED ASSUMPTION (re-stated 2026-09-06): glm-5.3-flash has no
    # published rate in the checked sources. Rather than invent a "flash"
    # discount, it falls to the zai default, which is the full GLM-5.3 rate —
    # deliberately conservative-high, so the estimate over-states rather than
    # under-states flash traffic. Replace with a published flash rate when
    # one is verified; do not guess a multiplier.
    "zai": {
        "models": {
            "glm-5.3": {"input": "1.40", "output": "4.40", "cache_read": "0.26", "cache_write": "0"},
        },
        "default": {"input": "1.40", "output": "4.40", "cache_read": "0.26", "cache_write": "0"},
    },
    # OpenCode Go is a subscription (no per-token bill); these are the
    # underlying model's list rates (same as the zai glm-5.3 entry) used to
    # estimate quota consumption. qwen3.7-max stays unpriced until a rate is
    # known — no provider default, so it surfaces as coverage.
    "opencode-go": {
        "models": {
            "glm-5.3": {"input": "1.40", "output": "4.40", "cache_read": "0.26", "cache_write": "0"},
        },
        "default": None,
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


def _rates_with_context(
    provider: str, model: str, context_basis: str = "standard"
) -> Tuple[Optional[Dict[str, Any]], str]:
    """Resolve one rate table plus the context basis actually applied.

    A model without a nested ``long`` table has no long-context meter, so a
    long request prices at its single published rate and the applied basis
    reads ``"standard"`` — the caller must not report long-context billing
    that did not happen.
    """
    entry = LIST_PRICES.get(provider)
    if entry is None:
        return None, "standard"
    rates = entry["models"].get(model)
    if rates is None:
        rates = entry.get("default")
    if rates is None:
        return None, "standard"
    if context_basis == "long" and rates.get("long"):
        return rates["long"], "long"
    return rates, "standard"


def _rates_for(provider: str, model: str) -> Optional[Dict[str, Any]]:
    """Keep the existing rate-only contract used by quota derivation."""
    rates, _basis = _rates_with_context(provider, model)
    return rates


def estimate_list_price(buckets: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    """Estimate list-price USD over (provider, model, context basis) aggregates.

    Each bucket may carry ``context_basis`` — ``"standard"`` (the default),
    ``"long"`` for traffic whose per-request prompt exceeded
    :data:`LONG_CONTEXT_PROMPT_TOKENS`, or ``"aggregate"`` for rows that
    bundle many calls and therefore cannot be classified. Callers build those
    buckets per request with :func:`request_context_basis`; grouping by model
    first and testing the aggregate would misprice both directions.

    Returns exact-decimal strings; unpriced tokens are surfaced as coverage,
    never silently priced, and the estimate names the service tier it assumed.
    """
    total = Decimal(0)
    priced_tokens = 0
    unpriced_tokens = 0
    long_context_tokens = 0
    aggregate_context_tokens = 0
    long_context_unverified_tokens = 0
    unpriced_providers: set[str] = set()
    for bucket in buckets:
        provider = str(bucket.get("provider") or "unknown")
        model = str(bucket.get("model") or "")
        context_basis = str(bucket.get("context_basis") or "standard")
        tokens = int(bucket.get("total_tokens") or 0)
        rates, applied_basis = _rates_with_context(provider, model, context_basis)
        if rates is None:
            unpriced_tokens += tokens
            unpriced_providers.add(provider)
            continue
        priced_tokens += tokens
        if (provider, model) in LONG_CONTEXT_UNVERIFIED:
            long_context_unverified_tokens += tokens
        if applied_basis == "long":
            long_context_tokens += tokens
        elif context_basis == "aggregate":
            aggregate_context_tokens += tokens
        total += (
            Decimal(int(bucket.get("input_tokens") or 0)) * Decimal(rates["input"])
            # output_tokens already includes reasoning — do not add
            # reasoning_tokens on top (it is diagnostic only).
            + Decimal(int(bucket.get("output_tokens") or 0)) * Decimal(rates["output"])
            + Decimal(int(bucket.get("cache_read_tokens") or 0)) * Decimal(rates["cache_read"])
            + Decimal(int(bucket.get("cache_write_tokens") or 0)) * Decimal(rates["cache_write"])
        ) / _MTOK
    denominator = priced_tokens + unpriced_tokens
    return {
        "estimate_usd": str(total.quantize(Decimal("0.01"))),
        "priced_token_share": (priced_tokens / denominator) if denominator else 1.0,
        "unpriced_providers": sorted(unpriced_providers),
        # Basis disclosure, so no consumer can read the estimate as actual.
        "rate_basis": RATE_BASIS,
        "long_context_tokens": long_context_tokens,
        "aggregate_context_tokens": aggregate_context_tokens,
        # Tokens on a model that publishes a long-context meter we cannot
        # apply for lack of a verified threshold: priced at the short rate,
        # so the estimate may understate them. Stated, not hidden.
        "long_context_unverified_tokens": long_context_unverified_tokens,
    }
