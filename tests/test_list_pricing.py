"""List-price estimation (keeper-ruled 2026-08-30): token + cached-token
aggregates through provider list prices; unpriced is coverage, never guessed."""

from decimal import Decimal

import pytest

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


def test_reasoning_is_not_added_on_top_of_output():
    """Canonical output_tokens already includes reasoning; do not bill it twice.

    A bucket with output 254 and reasoning 39 must price exactly as 254 output
    tokens (the README contract: reasoning is diagnostic, not re-added).
    """
    with_reasoning = estimate_list_price([_bucket("zai", "glm-5.3", output_tokens=254, reasoning_tokens=39)])
    output_only = estimate_list_price([_bucket("zai", "glm-5.3", output_tokens=254)])
    assert with_reasoning["estimate_usd"] == output_only["estimate_usd"]
    # Scale up so the cent rounding cannot mask a double-count.
    big = estimate_list_price([_bucket("zai", "glm-5.3", output_tokens=1_000_000, reasoning_tokens=1_000_000)])
    assert big["estimate_usd"] == "4.40"
    reasoning_only = estimate_list_price([_bucket("zai", "glm-5.3", reasoning_tokens=1_000_000)])
    assert reasoning_only["estimate_usd"] == "0.00"


def test_unknown_provider_is_coverage_not_a_guess():
    result = estimate_list_price([
        _bucket("anthropic", "claude-sonnet-5", input_tokens=1_000_000),
        _bucket("mystery-lab", "whoknows", input_tokens=1_000_000),
    ])
    assert result["estimate_usd"] == "2.00"
    assert 0.4 < result["priced_token_share"] < 0.6
    assert result["unpriced_providers"] == ["mystery-lab"]


def test_provider_default_covers_unlisted_models():
    """Documented provider defaults still cover unlisted models on the
    providers that keep one. (The OpenAI/Codex defaults were removed on
    2026-09-06 — see
    test_unknown_openai_and_codex_models_are_unpriced_coverage_not_zero.)"""
    result = estimate_list_price([_bucket("anthropic", "claude-99-experimental", input_tokens=1_000_000)])
    assert result["estimate_usd"] == "10.00"
    assert result["unpriced_providers"] == []


def test_local_inference_is_zero_not_unpriced():
    result = estimate_list_price([_bucket("custom:gemma-4-moe-it", "gemma-4-26b-a4b-it-mxfp4", input_tokens=5_000_000)])
    assert result["estimate_usd"] == "0.00"
    assert result["priced_token_share"] == 1.0


def test_every_seeded_rate_parses_as_decimal():
    for provider, entry in LIST_PRICES.items():
        tables = list(entry["models"].values())
        if entry.get("default"):
            tables.append(entry["default"])
        # Nested long-context meters — applied ("long") or recorded pending a
        # verified threshold — are held to the same four-rate shape.
        tables.extend([
            nested
            for rates in list(tables)
            for key in ("long", "long_unapplied_pending_threshold")
            if (nested := rates.get(key))
        ])
        for rates in tables:
            for key in ("input", "output", "cache_read", "cache_write"):
                Decimal(rates[key])


def test_claude_fable_5_1_rates():
    rates = LIST_PRICES["anthropic"]["models"]["claude-fable-5-1"]
    assert rates == {"input": "10", "output": "50", "cache_read": "0.25", "cache_write": "12.50"}
    result = estimate_list_price([_bucket(
        "anthropic", "claude-fable-5-1",
        input_tokens=1_000_000, output_tokens=100_000,
        cache_read_tokens=4_000_000, cache_write_tokens=200_000,
    )])
    # 10 + 5 + 1.00 + 2.50 = 18.50
    assert result["estimate_usd"] == "18.50"
    assert result["priced_token_share"] == 1.0


def test_claude_sonnet_5_still_at_published_2_10_rate():
    assert LIST_PRICES["anthropic"]["models"]["claude-sonnet-5"] == {
        "input": "2", "output": "10", "cache_read": "0.20", "cache_write": "2.50",
    }


def test_opencode_go_prices_glm_but_leaves_qwen_unpriced():
    entry = LIST_PRICES["opencode-go"]
    assert entry["default"] is None
    assert entry["models"]["glm-5.3"] == LIST_PRICES["zai"]["models"]["glm-5.3"]

    priced = estimate_list_price([_bucket("opencode-go", "glm-5.3", input_tokens=1_000_000, output_tokens=1_000_000)])
    assert priced["estimate_usd"] == "5.80"
    assert priced["unpriced_providers"] == []

    mixed = estimate_list_price([
        _bucket("opencode-go", "glm-5.3", input_tokens=1_000_000),
        _bucket("opencode-go", "qwen3.7-max", input_tokens=1_000_000),
    ])
    assert mixed["estimate_usd"] == "1.40"
    assert mixed["priced_token_share"] == 0.5
    assert mixed["unpriced_providers"] == ["opencode-go"]


# ── 2026-10-04 GPT-6.1 Sol pricing round ─────────────────────────────────


@pytest.mark.parametrize("provider", ["openai", "openai-codex"])
def test_gpt_6_1_sol_has_its_own_published_rates(provider):
    # OpenAI model page + Standard rate card, read 2026-10-04. In particular,
    # the $0.10 cached-input rate is NOT GPT-6 Sol's $0.20 sibling rate.
    assert LIST_PRICES[provider]["models"]["gpt-6.1-sol"] == {
        "input": "2", "output": "10", "cache_read": "0.10", "cache_write": "2.50",
        "long": {"input": "4", "output": "15", "cache_read": "0.20", "cache_write": "5"},
    }
    assert LIST_PRICES["openai"]["models"]["gpt-6.1-sol"] is not (
        LIST_PRICES["openai-codex"]["models"]["gpt-6.1-sol"]
    )


@pytest.mark.parametrize("provider", ["openai", "openai-codex"])
@pytest.mark.parametrize("basis, expected", [
    ("standard", "14.60"), ("long", "24.20"), ("aggregate", "14.60"),
])
def test_gpt_6_1_sol_prices_all_four_buckets_on_the_declared_basis(provider, basis, expected):
    result = estimate_list_price([dict(_bucket(
        provider, "gpt-6.1-sol",
        input_tokens=1_000_000, output_tokens=1_000_000,
        cache_read_tokens=1_000_000, cache_write_tokens=1_000_000,
    ), context_basis=basis)])
    assert result["estimate_usd"] == expected
    assert result["priced_token_share"] == 1.0
    assert result["long_context_tokens"] == (4_000_000 if basis == "long" else 0)
    assert result["aggregate_context_tokens"] == (4_000_000 if basis == "aggregate" else 0)
    assert result["long_context_unverified_tokens"] == 0
    assert result["rate_basis"] == "standard"


@pytest.mark.parametrize("provider", ["openai", "openai-codex"])
@pytest.mark.parametrize("cache_write, expected_basis", [(1_000, "standard"), (1_001, "long")])
def test_gpt_6_1_sol_request_boundary_includes_cache(provider, cache_write, expected_basis):
    from codex_usage_tracker.list_pricing import accumulate_price_bucket, finalize_price_buckets

    buckets = {}
    accumulate_price_bucket(buckets, {
        "provider": provider, "model_reported": "gpt-6.1-sol", "record_kind": "api_attempt",
        "input_tokens": 1_000, "cache_read_tokens": 270_000,
        "cache_write_tokens": cache_write, "output_tokens": 1_000,
    })
    rows = finalize_price_buckets(buckets)
    assert rows[0]["context_basis"] == expected_basis
    result = estimate_list_price(rows)
    assert result["priced_token_share"] == 1.0
    assert result["long_context_tokens"] == (
        rows[0]["total_tokens"] if expected_basis == "long" else 0
    )


# ── 2026-09-06 Astra / promotional-sol pricing round ─────────────────────
# Rates read from developers.openai.com/api/docs/models/gpt-6-astra and
# /api/docs/pricing by the parent AGENT on 2026-09-06. Agent-sourced, not
# keeper-confirmed: these tests pin what the table claims, not that the
# vendor still charges it.


def test_gpt_6_astra_standard_rates_on_both_openai_meters():
    expected = {
        "input": "10", "output": "50", "cache_read": "1.00", "cache_write": "12.50",
        "long": {"input": "20", "output": "75", "cache_read": "2.00", "cache_write": "25"},
    }
    assert LIST_PRICES["openai"]["models"]["gpt-6-astra"] == expected
    assert LIST_PRICES["openai-codex"]["models"]["gpt-6-astra"] == expected

    result = estimate_list_price([_bucket(
        "openai-codex", "gpt-6-astra",
        input_tokens=1_000_000, output_tokens=100_000,
        cache_read_tokens=2_000_000, cache_write_tokens=400_000,
    )])
    # 10 + 5 + 2 + 5 = 22.00
    assert result["estimate_usd"] == "22.00"
    assert result["priced_token_share"] == 1.0
    assert result["long_context_tokens"] == 0


def test_gpt_6_astra_long_context_bills_the_whole_request_at_the_long_rate():
    result = estimate_list_price([dict(_bucket(
        "openai", "gpt-6-astra",
        input_tokens=1_000_000, output_tokens=100_000,
        cache_read_tokens=2_000_000, cache_write_tokens=400_000,
    ), context_basis="long")])
    # 20 + 7.5 + 4 + 10 = 41.50 — output and cache flip too, not just input.
    assert result["estimate_usd"] == "41.50"
    assert result["long_context_tokens"] == 3_500_000


def test_long_context_threshold_is_the_request_prompt_including_cache():
    """Canonical input_tokens EXCLUDES cache, so the >272000 test is over
    input + cache_read + cache_write for one request."""
    from codex_usage_tracker.list_pricing import (
        LONG_CONTEXT_PROMPT_TOKENS,
        request_context_basis,
    )

    assert LONG_CONTEXT_PROMPT_TOKENS == 272_000

    def attempt(**tokens):
        row = {"record_kind": "api_attempt", "input_tokens": 0,
               "cache_read_tokens": 0, "cache_write_tokens": 0, "output_tokens": 0}
        row.update(tokens)
        return row

    # Boundary: exactly 272000 is standard, one more is long.
    assert request_context_basis(attempt(input_tokens=272_000)) == "standard"
    assert request_context_basis(attempt(input_tokens=272_001)) == "long"
    # Cache counts toward the prompt even though input_tokens alone is small.
    assert request_context_basis(
        attempt(input_tokens=1_000, cache_read_tokens=270_000, cache_write_tokens=1_001)
    ) == "long"
    assert request_context_basis(
        attempt(input_tokens=1_000, cache_read_tokens=270_000, cache_write_tokens=1_000)
    ) == "standard"
    # Output never counts toward the prompt.
    assert request_context_basis(attempt(input_tokens=10, output_tokens=500_000)) == "standard"
    # A historical aggregate bundles many calls: its summed prompt says
    # nothing about any single request, so it prices at standard and says so.
    assert request_context_basis(
        {"record_kind": "historical_aggregate", "input_tokens": 900_000}
    ) == "aggregate"


def test_aggregate_rows_price_standard_and_are_reported_as_such():
    result = estimate_list_price([dict(_bucket(
        "openai-codex", "gpt-6-astra", input_tokens=1_000_000,
    ), context_basis="aggregate")])
    assert result["estimate_usd"] == "10.00"
    assert result["long_context_tokens"] == 0
    assert result["aggregate_context_tokens"] == 1_000_000


def test_estimate_declares_its_standard_service_tier_basis():
    """Batch/Flex (0.5x) and Fast/Priority (2x) are not modeled; the estimate
    must say which tier it assumed rather than imply actual-rate correctness."""
    from codex_usage_tracker.list_pricing import RATE_BASIS

    assert RATE_BASIS == "standard"
    result = estimate_list_price([_bucket("openai", "gpt-6-astra", input_tokens=1)])
    assert result["rate_basis"] == "standard"


def test_gpt_5_6_sol_carries_the_current_promotional_rates():
    expected = {
        "input": "4", "output": "20", "cache_read": "0.40", "cache_write": "5",
        # Sol's model page independently confirms the 272,000 threshold.
        "long": {
            "input": "8", "output": "30", "cache_read": "0.80", "cache_write": "10",
        },
    }
    assert LIST_PRICES["openai"]["models"]["gpt-5.6-sol"] == expected
    assert LIST_PRICES["openai-codex"]["models"]["gpt-5.6-sol"] == expected
    short = estimate_list_price([_bucket(
        "openai-codex", "gpt-5.6-sol", input_tokens=1_000_000, output_tokens=1_000_000,
    )])
    assert short["estimate_usd"] == "24.00"


def test_sol_long_context_meter_uses_its_independently_verified_threshold():
    from codex_usage_tracker.list_pricing import LONG_CONTEXT_UNVERIFIED

    assert ("openai-codex", "gpt-5.6-sol") not in LONG_CONTEXT_UNVERIFIED
    assert ("openai-codex", "gpt-6-astra") not in LONG_CONTEXT_UNVERIFIED

    long_basis = estimate_list_price([dict(_bucket(
        "openai-codex", "gpt-5.6-sol", input_tokens=1_000_000, output_tokens=1_000_000,
    ), context_basis="long")])
    assert long_basis["estimate_usd"] == "38.00"
    assert long_basis["long_context_tokens"] == 2_000_000
    assert long_basis["long_context_unverified_tokens"] == 0

    # Both independently sourced meters apply without an unverified flag.
    astra = estimate_list_price([dict(_bucket(
        "openai-codex", "gpt-6-astra", input_tokens=1_000_000,
    ), context_basis="long")])
    assert astra["estimate_usd"] == "20.00"
    assert astra["long_context_unverified_tokens"] == 0


def test_unknown_openai_and_codex_models_are_unpriced_coverage_not_zero():
    """Contract change 2026-09-06: a new OpenAI/Codex model ID must surface as
    unpriced coverage, never be silently priced off a stale provider default
    (and never read as $0 actual)."""
    assert LIST_PRICES["openai"]["default"] is None
    assert LIST_PRICES["openai-codex"]["default"] is None
    for provider in ("openai", "openai-codex"):
        result = estimate_list_price([_bucket(provider, "gpt-9.9-experimental", input_tokens=1_000_000)])
        assert result["estimate_usd"] == "0.00"
        assert result["priced_token_share"] == 0.0
        assert result["unpriced_providers"] == [provider]


def test_reasoning_is_not_double_billed_on_astra_long_context():
    with_reasoning = estimate_list_price([dict(_bucket(
        "openai-codex", "gpt-6-astra", output_tokens=1_000_000, reasoning_tokens=900_000,
    ), context_basis="long")])
    output_only = estimate_list_price([dict(_bucket(
        "openai-codex", "gpt-6-astra", output_tokens=1_000_000,
    ), context_basis="long")])
    assert with_reasoning["estimate_usd"] == output_only["estimate_usd"] == "75.00"


def test_glm_flash_carries_its_published_rate_not_a_stand_in():
    """SUPERSEDED 2026-09-24. This test previously asserted the opposite: that
    glm-5.3-flash had no published rate and must fall to the conservative-high
    zai default. Z.ai does publish one, on its own pricing page, and the
    stand-in was overstating flash traffic by ~8.9x. The discipline the old
    test protected is unchanged — never invent a rate — but the honest way to
    satisfy it here is the published number, not a family fallback.
    """
    zai = LIST_PRICES["zai"]
    assert zai["models"]["glm-5.3-flash"] == {
        "input": "0.15", "output": "0.50", "cache_read": "0.03", "cache_write": "0",
    }
    # And the stand-in is gone, so a FUTURE Z.ai model surfaces as coverage
    # instead of silently pricing at the GLM-5.3 rate.
    assert zai["default"] is None


def test_deepseek_current_offpeak_floor_uses_vendor_rates_not_seeded_guesses():
    """The current vendor card publishes two time tariffs, but our buckets
    cannot encode the tariff window. Pin the *published* off-peak floor here,
    not the old inferred cache-hit convention or a made-up blended rate.
    """
    deepseek = LIST_PRICES["deepseek"]
    assert deepseek["default"] is None
    flash = {"input": "0.15", "output": "0.60", "cache_read": "0.003", "cache_write": "0"}
    assert deepseek["models"]["deepseek-flash"] == flash
    assert deepseek["models"]["deepseek-v4-flash"] == flash
    assert deepseek["models"]["deepseek-v4-pro"] == {
        "input": "0.66", "output": "1.98", "cache_read": "0.022", "cache_write": "0",
    }
    assert "long" not in flash
    priced = estimate_list_price([_bucket(
        "deepseek", "deepseek-flash", input_tokens=1_000_000,
        output_tokens=1_000_000, cache_read_tokens=1_000_000,
    )])
    assert priced["estimate_usd"] == "0.75"
    assert priced["priced_token_share"] == 1.0


def test_no_paid_provider_keeps_a_rate_bearing_default():
    """The invariant the 2026-09-24 audit settled on.

    A provider default cannot distinguish an unlisted sibling from a foreign
    model. The 'deepseek' label was carrying ~119M tokens of gpt-5.5 plus
    Gemini and local-mlx traffic, every token of it priced silently at
    DeepSeek rates. So a default is now permitted only where it is $0 — local
    inference, where there is no rate to get wrong.
    """
    for provider, entry in LIST_PRICES.items():
        default = entry.get("default")
        if default is None:
            continue
        rates = {k: v for k, v in default.items() if k != "long"}
        is_free = all(str(v) in ("0", "0.0", "0.00") for v in rates.values())
        assert is_free or provider == "anthropic", (
            f"{provider} keeps a rate-bearing default ({rates}); it will price "
            f"unlisted AND foreign models silently. Either give the models real "
            f"entries or set default=None so they surface as coverage."
        )
