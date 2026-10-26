"""Price-coverage detection: which observed (provider, model) pairs the rate
table cannot price, and which it prices only via a provider default.

The monitor-stability tests are the load-bearing ones. The nightly job uses
``monitor_lines`` as a cron ``monitor_script`` wake gate: identical output
suppresses the agent run entirely. If volumes ever leak into that output the
job wakes every night and the design is silently defeated while still
appearing to work.
"""

import sqlite3

import pytest

from codex_usage_tracker.list_pricing import (
    LIST_PRICES,
    estimate_list_price,
    resolve_rate_provider,
)
from codex_usage_tracker.price_coverage import (
    ACKNOWLEDGED_UNPRICED,
    build_report,
    classify,
    coverage_report,
    monitor_lines,
    observed_pairs,
)


def _pair(provider, model, calls=1, tokens=1000):
    return {"provider": provider, "model": model, "calls": calls, "tokens": tokens}


# ── classification ────────────────────────────────────────────────────────

def test_exact_entry_classifies_exact():
    assert classify("anthropic", "claude-opus-5") == "exact"


def test_no_entry_and_no_default_is_unpriced_not_guessed():
    """openai keeps default=None precisely so a new id surfaces as coverage."""
    assert LIST_PRICES["openai"]["default"] is None
    assert classify("openai", "gpt-99-imaginary") == "unpriced"


def test_gpt_6_1_sol_closes_only_the_verified_provider_gaps():
    for provider in ("openai", "openai-codex"):
        assert classify(provider, "gpt-6.1-sol") == "exact"
        assert coverage_report([_pair(provider, "gpt-6.1-sol")])["actionable"] == []
        assert LIST_PRICES[provider]["default"] is None
        assert classify(provider, "gpt-6.2-sol") == "unpriced"
    # A verified OpenAI rate must not paper over missing or foreign routes.
    for provider in ("custom", "custom:meridian-primary", "custom:meridian-yugen"):
        assert classify(provider, "gpt-6.1-sol") == "unpriced"


def test_k3_256k_uses_verified_k3_api_equivalent_rates():
    """Kimi documents this ID as K3 with a smaller context envelope, not
    a separate API-priced model or a subscription-quota discount to apply."""
    model = "k3-256k"
    assert classify("kimi-coding", model) == "exact"
    assert LIST_PRICES["kimi-coding"]["models"][model] == {
        "input": "3", "output": "15", "cache_read": "0.30", "cache_write": "3.00",
    }
    result = estimate_list_price([{
        "provider": "kimi-coding", "model": model,
        "input_tokens": 1_000_000, "output_tokens": 1_000_000,
        "cache_read_tokens": 1_000_000, "cache_write_tokens": 1_000_000,
        "total_tokens": 4_000_000,
    }])
    assert result["estimate_usd"] == "21.30"
    assert result["priced_token_share"] == 1.0
    assert result["long_context_tokens"] == 0
    assert coverage_report([_pair("kimi-coding", model)])["actionable"] == []
    assert LIST_PRICES["kimi-coding"]["default"] is None
    assert classify("kimi-coding", "k3-128k") == "unpriced"
    assert classify("custom:meridian-yugen", model) == "unpriced"


def test_glm_on_meridian_remains_an_actionable_attribution_gap():
    """A background-review row with a GLM request ID on a Claude endpoint
    must not borrow Z.ai's known GLM rate or be suppressed as permanent."""
    provider, model = "custom:meridian-yugen", "glm-5.3"
    assert resolve_rate_provider(provider, model) == provider
    assert classify(provider, model) == "unpriced"
    assert (provider, model) not in ACKNOWLEDGED_UNPRICED
    report = coverage_report([_pair(provider, model)])
    assert [(row["provider"], row["model"]) for row in report["actionable"]] == [
        (provider, model),
    ]


def test_unknown_provider_is_unpriced():
    assert classify("provider-that-does-not-exist", "anything") == "unpriced"


def test_provider_default_classifies_default():
    # Uses a $0 local provider: after the 2026-09-24 audit, local inference is
    # the only place a provider default still exists on purpose.
    assert classify("custom:omlx", "some-unlisted-local-model") == "default"


# ── proxy aliasing ────────────────────────────────────────────────────────

def test_meridian_claude_traffic_resolves_to_anthropic_rates():
    """The lane carries real Anthropic models under a local proxy label.

    Before this alias existed the entire Claude lane priced as nothing.
    """
    assert resolve_rate_provider("custom:meridian-yugen", "claude-opus-5") == "anthropic"
    assert classify("custom:meridian-primary", "claude-opus-5") == "exact"


def test_sonnet_5_5_on_meridian_uses_its_published_rate_without_a_long_tier():
    model = "claude-sonnet-5-5"
    assert LIST_PRICES["anthropic"]["models"][model] == {
        "input": "2", "output": "10", "cache_read": "0.20", "cache_write": "2.50",
    }
    assert classify("custom:meridian-primary", model) == "exact"
    result = estimate_list_price([{
        "provider": "custom:meridian-primary", "model": model,
        "input_tokens": 1_000_000, "output_tokens": 1_000_000,
        "cache_read_tokens": 1_000_000, "cache_write_tokens": 1_000_000,
        "total_tokens": 4_000_000, "context_basis": "long",
    }])
    assert result["estimate_usd"] == "14.70"
    assert result["long_context_tokens"] == 0
    assert coverage_report([_pair("custom:meridian-primary", model)])["actionable"] == []
    assert classify("custom:meridian-primary", "gpt-6-sol") == "unpriced"


def test_haiku_4_5_dated_id_on_meridian_uses_verified_haiku_rates():
    """The provider reports the dated API ID, not the requested short alias."""
    model = "claude-haiku-4-5-20251001"
    expected = {"input": "1", "output": "5", "cache_read": "0.10", "cache_write": "1.25"}
    assert LIST_PRICES["anthropic"]["models"][model] == expected
    assert resolve_rate_provider("custom:meridian-yugen", model) == "anthropic"
    assert classify("custom:meridian-yugen", model) == "exact"
    row = {
        "provider": "custom:meridian-yugen", "model": model,
        "input_tokens": 1_000_000, "output_tokens": 1_000_000,
        "cache_read_tokens": 1_000_000, "cache_write_tokens": 1_000_000,
        "total_tokens": 4_000_000,
    }
    result = estimate_list_price([row])
    assert result["estimate_usd"] == "7.35"
    assert result["priced_token_share"] == 1.0
    assert coverage_report([_pair("custom:meridian-yugen", model)])["actionable"] == []

def test_antigravity_bridge_gemini_resolves_to_first_party_google_rates():
    """SIL-114: the Meridian agy bridge carries Gemini 3.8 Flash at a
    thinking-level selector. Google's own card (read 2026-10-23) has one
    rate with no context split and no per-token cache write."""
    provider, model = "custom:meridian-agy", "gemini-3.8-flash-high"
    expected = {"input": "0.75", "output": "3.75", "cache_read": "0.075", "cache_write": "0"}
    for level in ("", "-high", "-medium", "-low"):
        assert LIST_PRICES["google"]["models"]["gemini-3.8-flash" + level] == expected
    assert LIST_PRICES["google"]["default"] is None
    assert resolve_rate_provider(provider, model) == "google"
    assert classify(provider, model) == "exact"
    result = estimate_list_price([{
        "provider": provider, "model": model,
        "input_tokens": 1_000_000, "output_tokens": 1_000_000,
        "cache_read_tokens": 10_000_000, "cache_write_tokens": 1_000_000,
        "total_tokens": 13_000_000, "context_basis": "long",
    }])
    # 0.75 + 3.75 + 10 x 0.075 + 0: no long table, so a long request stays
    # on the single published rate and is not reported as long-billed.
    assert result["estimate_usd"] == "5.25"
    assert result["long_context_tokens"] == 0
    assert result["priced_token_share"] == 1.0
    assert coverage_report([_pair(provider, model)])["actionable"] == []


def test_antigravity_bridge_alias_does_not_price_foreign_or_unlisted_models():
    """The same lane lists Claude and GPT-OSS ids, and agy served some
    bridge generations with claude-opus-4-6-thinking. None may borrow Gemini
    rates, and an unlisted Gemini id must still wake the nightly job."""
    provider = "custom:meridian-agy"
    for model in ("claude-opus-4-6-thinking", "claude-sonnet-4-6",
                  "gpt-oss-120b-medium", "gemini-3.9-flash-high"):
        assert resolve_rate_provider(provider, model) == provider
        assert classify(provider, model) == "unpriced"
    # The Claude Meridian lanes do not gain Gemini rates either.
    assert classify("custom:meridian-yugen", "gemini-3.8-flash-high") == "unpriced"


def test_openrouter_opus_5_5_has_its_own_verified_route_price():
    model = "anthropic/claude-opus-5.5"
    assert classify("openrouter", model) == "exact"
    assert LIST_PRICES["openrouter"]["models"][model] == {
        "input": "4", "output": "20", "cache_read": "0.20", "cache_write": "5",
    }
    result = estimate_list_price([{
        "provider": "openrouter", "model": model,
        "input_tokens": 1_000_000, "output_tokens": 1_000_000,
        "cache_read_tokens": 1_000_000, "cache_write_tokens": 1_000_000,
        "total_tokens": 4_000_000, "context_basis": "long",
    }])
    assert result["estimate_usd"] == "29.20"
    assert result["long_context_tokens"] == 0
    assert coverage_report([_pair("openrouter", model)])["actionable"] == []
    assert classify("openrouter", "anthropic/claude-opus-5.6") == "unpriced"


def test_openrouter_gpt_6_1_sol_closes_only_its_verified_route_gap():
    model = "openai/gpt-6.1-sol"
    assert classify("openrouter", model) == "exact"
    assert LIST_PRICES["openrouter"]["models"][model] == {
        "input": "2", "output": "10", "cache_read": "0.10", "cache_write": "2.50",
        "long": {"input": "4", "output": "15", "cache_read": "0.20", "cache_write": "5"},
    }
    assert coverage_report([_pair("openrouter", model)])["actionable"] == []
    assert LIST_PRICES["openrouter"]["default"] is None
    assert classify("openrouter", "openai/gpt-6.2-sol") == "unpriced"
    assert classify("openrouter", "gpt-6.1-sol") == "unpriced"
    for provider in ("custom", "custom:meridian-primary", "custom:meridian-yugen"):
        assert classify(provider, model) == "unpriced"


@pytest.mark.parametrize("basis, expected", [
    ("standard", "14.60"), ("long", "24.20"), ("aggregate", "14.60"),
])
def test_openrouter_gpt_6_1_sol_prices_all_four_published_rates(basis, expected):
    result = estimate_list_price([{
        "provider": "openrouter", "model": "openai/gpt-6.1-sol",
        "input_tokens": 1_000_000, "output_tokens": 1_000_000,
        "cache_read_tokens": 1_000_000, "cache_write_tokens": 1_000_000,
        "total_tokens": 4_000_000, "context_basis": basis,
    }])
    assert result["estimate_usd"] == expected
    assert result["priced_token_share"] == 1.0
    assert result["long_context_tokens"] == (4_000_000 if basis == "long" else 0)
    assert result["aggregate_context_tokens"] == (4_000_000 if basis == "aggregate" else 0)
    assert result["long_context_unverified_tokens"] == 0
    assert result["rate_basis"] == "standard"


@pytest.mark.parametrize("record_kind, cache_write, expected_basis", [
    ("api_attempt", 1_000, "standard"),
    ("api_attempt", 1_001, "long"),
    ("historical_aggregate", 1_001, "aggregate"),
])
def test_openrouter_gpt_6_1_sol_context_tier_uses_the_request_boundary(
    record_kind, cache_write, expected_basis,
):
    from codex_usage_tracker.list_pricing import accumulate_price_bucket, finalize_price_buckets

    buckets = {}
    accumulate_price_bucket(buckets, {
        "provider": "openrouter", "model_reported": "openai/gpt-6.1-sol",
        "record_kind": record_kind,
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
    assert result["aggregate_context_tokens"] == (
        rows[0]["total_tokens"] if expected_basis == "aggregate" else 0
    )


def test_meridian_priced_identically_to_direct_anthropic():
    tokens = dict(input_tokens=1_000_000, output_tokens=100_000)
    direct = estimate_list_price([
        {"provider": "anthropic", "model": "claude-opus-5",
         "cache_read_tokens": 0, "cache_write_tokens": 0,
         "total_tokens": 1_100_000, **tokens}])
    proxied = estimate_list_price([
        {"provider": "custom:meridian-yugen", "model": "claude-opus-5",
         "cache_read_tokens": 0, "cache_write_tokens": 0,
         "total_tokens": 1_100_000, **tokens}])
    assert direct["estimate_usd"] == proxied["estimate_usd"]
    assert proxied["unpriced_providers"] == []


def test_alias_never_borrows_the_target_providers_default():
    """The load-bearing restriction.

    The Meridian lanes carry a few non-Claude ids from routed auxiliary
    calls. A plain provider-level alias would have inherited Anthropic's
    conservative-high Fable default and priced GPT traffic at $10/$50 — a
    fabricated number, which is the exact failure this table exists to stop.
    """
    assert LIST_PRICES["anthropic"]["default"] is not None, (
        "precondition: anthropic keeps a default, so the alias must not "
        "expose it to non-Claude models"
    )
    assert resolve_rate_provider("custom:meridian-yugen", "gpt-5.5") == (
        "custom:meridian-yugen"
    )
    assert classify("custom:meridian-yugen", "gpt-5.5") == "unpriced"


def test_new_claude_model_on_meridian_is_unpriced_until_given_an_entry():
    """This is the nightly job's wake signal, not a regression."""
    assert classify("custom:meridian-yugen", "claude-future-6") == "unpriced"


def test_bare_custom_provider_is_not_aliased():
    """Bare ``custom`` on Meridian traffic is a ledger-writer labelling defect.

    Cron runs pinned to ``custom:meridian-*`` sometimes record plain
    ``custom``. Aliasing the label here would hide that defect, so these rows
    stay visible as coverage until the writer records the real provider.
    """
    assert resolve_rate_provider("custom", "claude-opus-5") == "custom"
    assert classify("custom", "claude-opus-5") == "unpriced"
    assert classify("custom", "claude-opus-5-5") == "unpriced"


def test_opus_5_5_1m_variant_prices_as_opus_5_5_on_meridian():
    """``[1m]`` selects Opus 5.5's 1M window, which Anthropic bills at
    standard rates. Background-review rows carry the suffixed id because they
    have no provider-reported identity."""
    assert classify("custom:meridian-yugen", "claude-opus-5-5[1m]") == "exact"
    assert LIST_PRICES["anthropic"]["models"]["claude-opus-5-5[1m]"] == (
        LIST_PRICES["anthropic"]["models"]["claude-opus-5-5"]
    )
    tokens = dict(input_tokens=34, output_tokens=15_510,
                  cache_read_tokens=4_380_317, cache_write_tokens=323_481,
                  total_tokens=4_719_342)
    plain = estimate_list_price([
        {"provider": "custom:meridian-yugen", "model": "claude-opus-5-5",
         "context_basis": "long", **tokens}])
    suffixed = estimate_list_price([
        {"provider": "custom:meridian-yugen", "model": "claude-opus-5-5[1m]",
         "context_basis": "long", **tokens}])
    assert suffixed["estimate_usd"] == plain["estimate_usd"]
    # No long-context meter exists for this model, so none may be reported.
    assert suffixed["long_context_tokens"] == 0


def test_1m_suffix_is_not_stripped_generically():
    """Only models whose 1M pricing was checked get a suffixed entry. On a
    model with a long-context premium the suffix changes the price, so a new
    ``<model>[1m]`` must surface as a gap."""
    assert classify("custom:meridian-yugen", "claude-opus-5[1m]") == "unpriced"
    assert classify("custom:meridian-primary", "claude-sonnet-4-6[1m]") == "unpriced"


# ── actionable vs acknowledged ────────────────────────────────────────────

def test_unattributed_provider_is_acknowledged_never_actionable():
    """Contract rule: unattributed beats misattributed."""
    report = coverage_report([_pair("unknown", "glm-5.3")])
    assert report["actionable"] == []
    assert len(report["acknowledged"]) == 1


def test_acknowledged_pairs_are_excluded_from_actionable():
    provider, model = next(iter(ACKNOWLEDGED_UNPRICED))
    report = coverage_report([_pair(provider, model)])
    assert report["actionable"] == []
    assert report["acknowledged"][0]["acknowledged_reason"]


def test_unsanctioned_provider_default_is_reported_as_a_defect():
    """A default hit outside the sanctioned set is silent pricing.

    It is technically "priced", which is exactly why it must still be
    actionable — the number is produced without anyone having looked it up.
    """
    report = coverage_report([_pair("anthropic", "claude-99-experimental")])
    assert len(report["actionable"]) == 1
    assert report["actionable"][0]["classification"] == "default"
    assert "unsanctioned" in report["actionable"][0]["defect"]


def test_sanctioned_default_is_not_a_defect():
    # Was written against zai/glm-5.3-flash, which now has a real entry — so
    # it was passing as "exact" and no longer exercised the sanction at all.
    report = coverage_report([_pair("custom:omlx", "some-unlisted-local-model")])
    assert report["rows"][0]["classification"] == "default"
    assert report["actionable"] == []


def test_foreign_model_under_a_provider_label_is_no_longer_priced_silently():
    """The concrete defect that narrowed the sanctioned set.

    ~119M tokens of gpt-5.5 sat in the ledger under a 'deepseek' provider
    label and priced at DeepSeek's v4-pro rate because that provider kept a
    default. It must now surface as coverage instead.
    """
    assert classify("deepseek", "gpt-5.5") == "unpriced"
    report = coverage_report([_pair("deepseek", "gpt-5.5")])
    assert len(report["actionable"]) == 1


def test_deepseek_reported_flash_is_exact_but_foreign_model_remains_a_gap():
    """The provider echoes the new id for a request using the old flash id."""
    report = coverage_report([
        _pair("deepseek", "deepseek-flash", tokens=77005),
        _pair("deepseek", "gpt-5.5", tokens=10),
    ])
    assert classify("deepseek", "deepseek-flash") == "exact"
    assert report["token_totals"]["exact"] == 77005
    assert [(row["provider"], row["model"]) for row in report["actionable"]] == [
        ("deepseek", "gpt-5.5")
    ]


def test_published_rates_reached_the_providers_that_serve_them():
    """Keeper directive 2026-09-24: price the models used from the providers
    used. Each of these carries real traffic under that exact label."""
    for provider, model in (
        ("zai", "glm-5.3"), ("zai", "glm-5.3-flash"), ("zai", "glm-5.2"),
        ("opencode-go", "glm-5.3"), ("opencode-go", "glm-5.2"),
        ("kimi-coding", "k3"), ("kimi-coding", "kimi-k3"),
        ("kimi-coding", "k3.1-pro"), ("kimi-coding", "kimi-k3.1-pro"),
    ):
        assert classify(provider, model) == "exact", f"{provider}/{model}"


def test_k3_1_pro_carries_its_own_card_row_and_highspeed_stays_unpriced():
    """SIL-150: K3.1 Pro is priced from its own row on Moonshot's card
    (cheaper than K3), and the Highspeed id -- a distinct, dearer row -- must
    surface as coverage rather than borrow the base model's rate."""
    from codex_usage_tracker.list_pricing import LIST_PRICES

    rates = LIST_PRICES["kimi-coding"]["models"]["k3.1-pro"]
    assert rates == {"input": "2.00", "output": "10.00", "cache_read": "0.20", "cache_write": "2.00"}
    assert "long" not in rates
    assert classify("kimi-coding", "k3.1-pro-highspeed") == "unpriced"


def test_local_zero_dollar_providers_are_covered():
    for provider in ("custom:omlx", "custom:minicpm5", "custom:gemma-4-moe-it"):
        assert classify(provider, "any-local-model") == "default"
        assert coverage_report([_pair(provider, "any-local-model")])["actionable"] == []


# ── monitor stability ─────────────────────────────────────────────────────

def test_monitor_output_excludes_volumes_so_it_only_moves_on_real_change():
    """If token counts leaked in, the agent would wake every single night."""
    quiet = monitor_lines(coverage_report([_pair("openai", "gpt-99", tokens=5)]))
    busy = monitor_lines(coverage_report([_pair("openai", "gpt-99", tokens=5_000_000_000)]))
    assert quiet == busy
    assert "5" not in quiet.replace("gpt-99", "")


def test_monitor_output_is_order_independent():
    a = monitor_lines(coverage_report([_pair("openai", "gpt-a"), _pair("openai", "gpt-b")]))
    b = monitor_lines(coverage_report([_pair("openai", "gpt-b"), _pair("openai", "gpt-a")]))
    assert a == b


def test_monitor_output_changes_when_a_new_gap_appears():
    before = monitor_lines(coverage_report([_pair("openai", "gpt-a")]))
    after = monitor_lines(coverage_report([_pair("openai", "gpt-a"), _pair("openai", "gpt-b")]))
    assert before != after


def test_monitor_says_so_when_there_is_nothing_to_do():
    assert "no actionable gaps" in monitor_lines(
        coverage_report([_pair("anthropic", "claude-opus-5")]))


# ── ledger reads ──────────────────────────────────────────────────────────

def test_absent_ledger_is_zero_rows_not_an_error(tmp_path):
    """Bootstrap is lazy by contract: no file and no traffic are the same."""
    assert observed_pairs(tmp_path / "nope.db") == []
    assert build_report(tmp_path / "nope.db")["actionable"] == []


@pytest.fixture
def ledger(tmp_path):
    path = tmp_path / "usage_events.db"
    con = sqlite3.connect(path)
    con.execute(
        "CREATE TABLE usage_events (timestamp REAL, provider TEXT, model TEXT, "
        "model_reported TEXT, input_tokens INT, output_tokens INT, "
        "cache_read_tokens INT, cache_write_tokens INT)"
    )
    con.commit()
    return path, con


def test_observed_pairs_prefers_reported_identity_but_keeps_requested_only_rows(ledger):
    path, con = ledger
    con.execute(
        "INSERT INTO usage_events VALUES (strftime('%s','now'), 'openai-codex', "
        "'requested-id', 'reported-id', 10, 1, 0, 0)")
    # A row that never carried a reported id must NOT be dropped: dropping it
    # would understate coverage gaps, which is the one thing this must not do.
    con.execute(
        "INSERT INTO usage_events VALUES (strftime('%s','now'), 'openai-codex', "
        "'requested-only', NULL, 10, 1, 0, 0)")
    con.commit()
    con.close()
    models = {row["model"] for row in observed_pairs(path)}
    assert models == {"reported-id", "requested-only"}


def test_window_excludes_older_traffic(ledger):
    path, con = ledger
    con.execute(
        "INSERT INTO usage_events VALUES (strftime('%s','now') - 40*86400, "
        "'openai-codex', 'ancient', 'ancient', 10, 1, 0, 0)")
    con.commit()
    con.close()
    assert observed_pairs(path, window_days=14) == []
    assert len(observed_pairs(path, window_days=90)) == 1


# ── bucket identity ───────────────────────────────────────────────────────

def test_price_bucket_falls_back_to_the_sidecars_model_column():
    """The canonical ledger calls the requested id ``model_requested``; the
    Hermes sidecar calls it ``model`` and has no ``model_requested`` column.
    Without the third fallback every sidecar row lacking a provider-echoed
    identity bucketed as ``unknown`` and silently left the estimate.
    """
    from codex_usage_tracker.list_pricing import (
        accumulate_price_bucket, finalize_price_buckets, estimate_list_price,
    )
    buckets = {}
    accumulate_price_bucket(buckets, {
        "provider": "zai", "model": "glm-5.3", "model_reported": None,
        "input_tokens": 1_000_000, "output_tokens": 0,
        "cache_read_tokens": 0, "cache_write_tokens": 0, "reasoning_tokens": 0,
    })
    rows = finalize_price_buckets(buckets)
    assert rows[0]["model"] == "glm-5.3"
    result = estimate_list_price(rows)
    assert result["estimate_usd"] == "1.40"
    assert result["unpriced_providers"] == []


def test_reported_identity_still_wins_over_the_fallback():
    from codex_usage_tracker.list_pricing import (
        accumulate_price_bucket, finalize_price_buckets,
    )
    buckets = {}
    accumulate_price_bucket(buckets, {
        "provider": "zai", "model": "requested-id",
        "model_reported": "reported-id",
        "input_tokens": 10, "output_tokens": 1,
        "cache_read_tokens": 0, "cache_write_tokens": 0, "reasoning_tokens": 0,
    })
    assert finalize_price_buckets(buckets)[0]["model"] == "reported-id"
