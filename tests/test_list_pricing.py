"""List-price estimation (keeper-ruled 2026-08-30): token + cached-token
aggregates through provider list prices; unpriced is coverage, never guessed."""

from decimal import Decimal

from codex_usage_tracker.list_pricing import LIST_PRICES, estimate_list_price


def _bucket(provider, model, **tokens):
    base = {"provider": provider, "model": model,
            "input_tokens": 0, "output_tokens": 0, "reasoning_tokens": 0,
            "cache_read_tokens": 0, "cache_write_tokens": 0}
    base.update(tokens)
    base["total_tokens"] = sum(
        base[f] for f in ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens")
    )
    return base


def test_opus_tier_math_covers_all_four_rates():
    result = estimate_list_price([_bucket(
        "anthropic", "claude-opus-5",
        input_tokens=1_000_000, output_tokens=100_000,
        cache_read_tokens=10_000_000, cache_write_tokens=500_000,
    )])
    # 5 + 2.5 + 5 + 3.125 → 15.62 (round-half-even, the repo's parity rule)
    assert result["estimate_usd"] == "15.62"
    assert result["priced_token_share"] == 1.0
    assert result["unpriced_providers"] == []


def test_reasoning_bills_at_the_output_rate():
    plain = estimate_list_price([_bucket("zai", "glm-5.3", output_tokens=1_000_000)])
    with_reasoning = estimate_list_price([_bucket("zai", "glm-5.3", reasoning_tokens=1_000_000)])
    assert plain["estimate_usd"] == with_reasoning["estimate_usd"] == "4.40"


def test_unknown_provider_is_coverage_not_a_guess():
    result = estimate_list_price([
        _bucket("anthropic", "claude-sonnet-5", input_tokens=1_000_000),
        _bucket("mystery-lab", "whoknows", input_tokens=1_000_000),
    ])
    assert result["estimate_usd"] == "2.00"
    assert 0.4 < result["priced_token_share"] < 0.6
    assert result["unpriced_providers"] == ["mystery-lab"]


def test_provider_default_covers_unlisted_models():
    result = estimate_list_price([_bucket("openai-codex", "gpt-9.9-experimental", input_tokens=1_000_000)])
    assert result["estimate_usd"] == "5.00"


def test_local_inference_is_zero_not_unpriced():
    result = estimate_list_price([_bucket("custom:gemma-4-moe-it", "gemma-4-26b-a4b-it-mxfp4", input_tokens=5_000_000)])
    assert result["estimate_usd"] == "0.00"
    assert result["priced_token_share"] == 1.0


def test_every_seeded_rate_parses_as_decimal():
    for provider, entry in LIST_PRICES.items():
        tables = list(entry["models"].values())
        if entry.get("default"):
            tables.append(entry["default"])
        for rates in tables:
            for key in ("input", "output", "cache_read", "cache_write"):
                Decimal(rates[key])
