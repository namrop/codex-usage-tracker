"""OpenCode Go weighted-usage explanation (keeper request 2026-09-14: "what
consumes the shared allowance").

Pure projector ``build_go_usage`` — canonical usage rows and Go quota
observations in, JSON-ready dict out. The provider's native weekly percent
(whole points, floor-rounded, capped at 100) is authoritative; the model rows
are a CURRENT-TARIFF ESTIMATE of weighted quota points, never normalized to
the native total and never called a bill. Nothing here reads the ledgers;
the dashboard route (parent-owned) supplies the rows.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json

import pytest

from codex_usage_tracker.go_usage import (
    GO_TARIFF,
    NATIVE_FRESH_SECONDS,
    NATIVE_SOURCE_SURFACE,
    WEEKLY_LIMIT_QUOTA_UNITS,
    build_go_usage,
)

# Tuesday 2026-09-15 03:39Z: the current UTC week opened Monday 2026-09-14
# 00:00Z and closes Monday 2026-09-21 00:00Z.
NOW = datetime(2026, 9, 15, 3, 39, 0, tzinfo=timezone.utc)
WEEK_START = "2026-09-14T00:00:00Z"
WEEK_END = "2026-09-21T00:00:00Z"
MILLION = 1_000_000


def _usage(event_id, occurred_at="2026-09-14T12:00:00Z", model="glm-5.3", *,
           input_tokens=0, cache_read=0, cache_write=0, output=0, reasoning=0,
           **updates):
    row = {
        "fact_type": "usage_event_v1",
        "schema_version": 1,
        "source_namespace": "private:hermes-usage",
        "source_event_id": event_id,
        "harness": "hermes",
        "purpose": "main",
        "record_kind": "api_attempt",
        "occurred_at": occurred_at,
        "recorded_at": occurred_at,
        "usage_source": "provider_reported",
        "usage_completeness": "complete",
        "measurement_confidence": "exact",
        "cost_status": "unknown",
        "provider": "opencode-go",
        "model_requested": model,
        "model_reported": model,
        "api_mode": "chat_completions",
        "session_id": "sess-private-identifier",
        "input_tokens": input_tokens,
        "cache_read_tokens": cache_read,
        "cache_write_tokens": cache_write,
        "output_tokens": output,
        "reasoning_tokens": reasoning,
        "missing_fields": [],
        "attribution_gaps": [],
        "estimated_cost_usd": None,
        "actual_cost_usd": None,
        # Legacy unweighted local cost under a misleading name: never used.
        "x_opencode_quota_cost_usd": "999",
    }
    row.update(updates)
    return row


def _native(observed_at="2026-09-15T02:50:00Z", percent=24, state="ok", *,
            resets_at=WEEK_END, quota_name="week", surface=NATIVE_SOURCE_SURFACE,
            **updates):
    row = {
        "fact_type": "quota_observation_v1",
        "schema_version": 1,
        "source_namespace": "private:opencode-go-native",
        "source_observation_id": f"{observed_at}:{quota_name}",
        "harness": "opencode_go_api",
        "observed_at": observed_at,
        "provider": "opencode-go",
        "quota_name": quota_name,
        "quota_scope": "account",
        "window_kind": "fixed",
        "unit": "percent",
        "measurement_confidence": "exact",
        "limit_value": "100",
        "used_value": str(percent),
        "remaining_value": str(100 - percent),
        "resets_at": resets_at,
        "account_ref": None,
        "provider_payload_ref": "/private/provider/payload",
        "x_source_surface": surface,
        "x_percent_resolution": 1,
        "x_provider_state": state,
    }
    row.update(updates)
    return row


def _build(usage_rows, quota_rows, *, generated_at=NOW):
    return build_go_usage(usage_rows, quota_rows, generated_at=generated_at)


def _model(payload, name):
    return next(row for row in payload["models"] if row["model"] == name)


# ── tariff and weighting ───────────────────────────────────────────────────


def test_tariff_carries_the_verified_glm_entry_and_provenance():
    entry = GO_TARIFF["models"]["glm-5.3"]
    assert entry == {
        "input_usd_per_m": "1.40",
        "output_usd_per_m": "4.40",
        "cache_read_usd_per_m": "0.26",
        "cache_write_usd_per_m": "0",
        "multiplier": "4",
    }
    assert GO_TARIFF["as_of"] == "2026-09-15T03:31:00Z"
    assert GO_TARIFF["source_url"] == "https://opencode.ai/docs/go/#usage-limits"
    assert WEEKLY_LIMIT_QUOTA_UNITS == Decimal("30")
    assert NATIVE_FRESH_SECONDS == 3 * 3600


def test_weighted_four_x_example_prices_tokens_then_multiplies_then_divides_by_the_weekly_limit():
    """1M input + 100k output on glm-5.3: list 1.40 + 0.44 = 1.84 USD;
    × 4 = 7.36 quota units; / 30 × 100 = 24.533 modeled points."""
    payload = _build([_usage("e1", input_tokens=MILLION, output=100_000)], [_native(percent=24)])
    row = _model(payload, "glm-5.3")
    assert row["event_count"] == 1
    assert row["pricing_status"] == "priced"
    assert row["multiplier"] == "4"
    assert Decimal(row["estimated_list_cost_usd"]) == Decimal("1.84")
    assert Decimal(row["estimated_quota_units"]) == Decimal("7.36")
    assert row["estimated_quota_pct"] == "24.533"
    assert row["basis"] == "current_tariff_estimate"
    assert payload["totals"]["estimated_quota_pct"] == "24.533"
    assert payload["totals"]["weekly_limit_quota_units"] == "30"
    assert payload["tariff"]["as_of"] == "2026-09-15T03:31:00Z"
    assert payload["tariff"]["source_url"] == GO_TARIFF["source_url"]
    assert payload["tariff"]["basis"] == "current_tariff_estimate"


def test_cache_and_output_buckets_price_separately_and_reasoning_is_diagnostic_only():
    """1M cache read = 0.26, 1M cache write = 0, 1M output = 4.40; reasoning
    is already inside canonical output_tokens and is never priced or added
    to total_tokens again."""
    payload = _build(
        [_usage("e1", cache_read=MILLION, cache_write=MILLION, output=MILLION, reasoning=500_000)],
        [],
    )
    row = _model(payload, "glm-5.3")
    assert row["input_tokens"] == 0
    assert row["cache_read_tokens"] == MILLION
    assert row["cache_write_tokens"] == MILLION
    assert row["output_tokens"] == MILLION
    assert row["reasoning_tokens"] == 500_000
    assert row["total_tokens"] == 3 * MILLION
    assert Decimal(row["estimated_list_cost_usd"]) == Decimal("4.66")
    assert Decimal(row["estimated_quota_units"]) == Decimal("18.64")
    assert row["estimated_quota_pct"] == "62.133"
    totals = payload["totals"]
    assert totals["total_tokens"] == 3 * MILLION
    assert totals["reasoning_tokens"] == 500_000


def test_legacy_unweighted_local_cost_field_is_never_the_quota_basis():
    payload = _build([_usage("e1", input_tokens=MILLION, x_opencode_quota_cost_usd="999")], [])
    row = _model(payload, "glm-5.3")
    assert Decimal(row["estimated_quota_units"]) == Decimal("5.6")
    assert "999" not in json.dumps(payload)


# ── model rows: unknown, corrections, missing tokens, ordering ─────────────


def test_unknown_model_keeps_its_tokens_but_is_unpriced_with_no_guessed_multiplier():
    payload = _build(
        [
            _usage("e1", input_tokens=MILLION),
            _usage("e2", model="mystery-model", input_tokens=10, output=5),
            _usage("e3", model=None, input_tokens=7, model_requested=None, model_reported=None),
        ],
        [],
    )
    mystery = _model(payload, "mystery-model")
    assert mystery["pricing_status"] == "unpriced"
    assert mystery["multiplier"] is None
    assert mystery["estimated_list_cost_usd"] is None
    assert mystery["estimated_quota_units"] is None
    assert mystery["estimated_quota_pct"] is None
    assert mystery["total_tokens"] == 15
    unknown = _model(payload, "unknown")
    assert unknown["pricing_status"] == "unpriced"
    assert unknown["total_tokens"] == 7
    totals = payload["totals"]
    assert totals["event_count"] == 3
    assert totals["priced_event_count"] == 1
    assert totals["unpriced_event_count"] == 2
    assert totals["model_count"] == 3
    assert totals["unpriced_model_count"] == 2
    assert payload["coverage"]["unpriced_models"] == ["mystery-model", "unknown"]
    assert payload["reconciliation"]["modeled_complete"] is False
    # Priced rows lead, then unpriced by token volume; deterministic.
    assert [row["model"] for row in payload["models"]] == ["glm-5.3", "mystery-model", "unknown"]
    assert any("unpriced" in note for note in payload["notes"])


def test_duplicated_canonical_identity_counts_once():
    row = _usage("e1", input_tokens=MILLION)
    payload = _build([row, dict(row), _usage("e2", input_tokens=MILLION)], [])
    assert _model(payload, "glm-5.3")["event_count"] == 2
    assert _model(payload, "glm-5.3")["input_tokens"] == 2 * MILLION
    assert payload["coverage"]["duplicate_rows"] == 1
    assert payload["coverage"]["usage_rows_in_window"] == 2


def test_signed_corrections_are_preserved_not_dropped():
    correction = _usage(
        "c1", input_tokens=-200_000, record_kind="correction", latency_ms=None,
        corrects_source_namespace="private:hermes-usage", corrects_source_event_id="e1",
    )
    payload = _build([_usage("e1", input_tokens=MILLION), correction], [])
    row = _model(payload, "glm-5.3")
    assert row["input_tokens"] == 800_000
    assert row["event_count"] == 2
    assert row["correction_count"] == 1
    assert Decimal(row["estimated_list_cost_usd"]) == Decimal("1.12")
    assert payload["coverage"]["correction_rows"] == 1


def test_missing_token_fields_are_counted_not_zeroed_silently():
    payload = _build(
        [_usage("e1", input_tokens=MILLION), _usage("e2", input_tokens=None, output=None,
                                                   missing_fields=["input_tokens", "output_tokens"])],
        [_native(percent=18)],
    )
    row = _model(payload, "glm-5.3")
    assert row["event_count"] == 2
    assert row["missing_token_event_count"] == 1
    assert row["input_tokens"] == MILLION
    assert payload["coverage"]["missing_token_rows"] == 1
    assert payload["reconciliation"]["modeled_complete"] is False
    assert any("token" in note and "missing" in note for note in payload["notes"])


def test_model_contributions_sum_to_the_modeled_total_exactly():
    payload = _build(
        [
            _usage("e1", input_tokens=MILLION, output=100_000),
            _usage("e2", input_tokens=333_333, cache_read=777_777, output=12_345),
            _usage("e3", model="mystery-model", input_tokens=1),
        ],
        [],
    )
    priced = [row for row in payload["models"] if row["pricing_status"] == "priced"]
    assert priced
    units = sum(Decimal(row["estimated_quota_units"]) for row in priced)
    pct = sum(Decimal(row["estimated_quota_pct"]) for row in priced)
    assert Decimal(payload["totals"]["estimated_quota_units"]) == units
    assert Decimal(payload["totals"]["estimated_quota_pct"]) == pct
    assert Decimal(payload["totals"]["estimated_list_cost_usd"]) == sum(
        Decimal(row["estimated_list_cost_usd"]) for row in priced
    )
    shares = [Decimal(row["share_of_modeled_pct"]) for row in priced]
    assert abs(sum(shares) - Decimal("100")) <= Decimal("0.01")
    assert _model(payload, "mystery-model")["share_of_modeled_pct"] is None


# ── window: current UTC week, Monday 00:00 boundary, no future rows ───────


def test_window_is_the_current_utc_week_from_monday_midnight():
    payload = _build([], [])
    assert payload["window"] == {"started_at": WEEK_START, "ends_at": WEEK_END, "as_of": "2026-09-15T03:39:00Z"}
    assert payload["generated_at"] == "2026-09-15T03:39:00Z"


def test_utc_monday_boundary_is_inclusive_at_the_start_and_excludes_the_previous_week():
    payload = _build(
        [
            _usage("last-week", "2026-09-13T23:59:59Z", input_tokens=MILLION),
            _usage("first-second", "2026-09-14T00:00:00Z", input_tokens=1),
            _usage("now", "2026-09-15T03:39:00Z", input_tokens=2),
            _usage("future", "2026-09-15T03:39:01Z", input_tokens=4),
            _usage("naive", "2026-09-14T10:00:00", input_tokens=8),
            _usage("garbage", "not-a-timestamp", input_tokens=16),
        ],
        [],
    )
    row = _model(payload, "glm-5.3")
    assert row["input_tokens"] == 3
    coverage = payload["coverage"]
    assert coverage["source_usage_rows"] == 6
    assert coverage["usage_rows_in_window"] == 2
    assert coverage["ignored_usage_rows"]["before_window"] == 1
    assert coverage["ignored_usage_rows"]["future"] == 1
    assert coverage["ignored_usage_rows"]["invalid_timestamp"] == 2


def test_only_opencode_go_usage_events_count():
    payload = _build(
        [
            _usage("e1", input_tokens=1),
            _usage("other-provider", provider="anthropic", input_tokens=2),
            _usage("no-provider", provider=None, input_tokens=4),
            _usage("other-fact", fact_type="quota_observation_v1", input_tokens=8),
            "not a mapping",
            None,
        ],
        [],
    )
    assert _model(payload, "glm-5.3")["input_tokens"] == 1
    ignored = payload["coverage"]["ignored_usage_rows"]
    assert ignored["other_provider"] == 2
    assert ignored["other_fact_type"] == 1
    assert ignored["malformed"] == 2


def test_generated_at_must_be_timezone_aware():
    with pytest.raises(ValueError):
        _build([], [], generated_at=datetime(2026, 9, 15, 3, 39))


# ── native: latest fresh API reading, integer points, capped, missing ─────


def test_native_is_the_latest_api_weekly_reading_as_a_whole_integer():
    payload = _build(
        [],
        [
            _native("2026-09-15T01:00:00Z", percent=20),
            _native("2026-09-15T02:50:00Z", percent=24),
            _native("2026-09-15T02:00:00Z", percent=22),
            # Rolling and monthly readings are not the weekly pool.
            _native("2026-09-15T03:00:00Z", percent=99, quota_name="five_hour"),
            _native("2026-09-15T03:00:00Z", percent=78, quota_name="month"),
        ],
    )
    native = payload["native"]
    assert native["percent"] == 24
    assert isinstance(native["percent"], int) and not isinstance(native["percent"], bool)
    assert native["status"] == "ok"
    assert native["observed_at"] == "2026-09-15T02:50:00Z"
    assert native["resets_at"] == WEEK_END
    assert native["fresh"] is True
    assert native["age_seconds"] == 49 * 60
    assert native["capped"] is False
    assert native["percent_resolution"] == 1
    assert native["source_surface"] == NATIVE_SOURCE_SURFACE
    assert native["basis"] == "provider_reported"
    assert payload["coverage"]["native_candidates"] == 3


def test_native_ignores_cookie_surface_previous_week_future_and_non_integer_readings():
    payload = _build(
        [],
        [
            _native("2026-09-15T02:50:00Z", percent=55, surface="opencode_go_server_function"),
            _native("2026-09-13T23:00:00Z", percent=90),
            _native("2026-09-15T03:40:00Z", percent=70),
            _native("2026-09-15T02:00:00Z", percent=24, used_value="24.5"),
            _native("2026-09-15T02:10:00Z", percent=24, used_value=None),
            _native("2026-09-15T02:20:00Z", percent=24, unit="usd"),
            _native("2026-09-15T02:30:00Z", percent=24, provider="anthropic"),
            "not a mapping",
        ],
    )
    assert payload["native"]["percent"] is None
    ignored = payload["coverage"]["ignored_quota_rows"]
    assert ignored["other_source_surface"] == 1
    assert ignored["before_window"] == 1
    assert ignored["future"] == 1
    assert ignored["non_integer_percent"] == 1
    assert ignored["missing_percent"] == 1
    assert ignored["other_unit"] == 1
    assert ignored["other_provider"] == 1
    assert ignored["malformed"] == 1


def test_native_stale_reading_stays_visible_but_never_counts_as_fresh_or_zero():
    stale_at = (NOW - timedelta(seconds=NATIVE_FRESH_SECONDS)).strftime("%Y-%m-%dT%H:%M:%SZ")
    payload = _build([_usage("e1", input_tokens=MILLION, output=100_000)], [_native(stale_at, percent=24)])
    native = payload["native"]
    assert native["percent"] == 24
    assert native["fresh"] is False
    assert native["age_seconds"] == NATIVE_FRESH_SECONDS
    reconciliation = payload["reconciliation"]
    assert reconciliation["status"] == "native_stale"
    assert reconciliation["native_lower_pct"] == "24"
    assert reconciliation["native_upper_pct"] == "25"
    assert reconciliation["matches_within_rounding"] is None
    assert any("stale" in note for note in payload["notes"])


def test_missing_native_does_not_crash_and_is_not_zero():
    payload = _build([_usage("e1", input_tokens=MILLION, output=100_000)], [])
    native = payload["native"]
    assert native == {
        "percent": None,
        "status": None,
        "observed_at": None,
        "resets_at": None,
        "fresh": False,
        "age_seconds": None,
        "capped": None,
        "percent_resolution": 1,
        "source_surface": NATIVE_SOURCE_SURFACE,
        "basis": "provider_reported",
    }
    reconciliation = payload["reconciliation"]
    assert reconciliation["status"] == "native_missing"
    assert reconciliation["native_lower_pct"] is None
    assert reconciliation["native_upper_pct"] is None
    assert reconciliation["modeled_pct"] == "24.533"
    assert reconciliation["matches_within_rounding"] is None
    assert reconciliation["band_gap_pct"] is None
    assert any("no native" in note.lower() for note in payload["notes"])
    json.dumps(payload)


def test_native_without_provider_state_reports_null_status_not_a_guess():
    row = _native(percent=24)
    del row["x_provider_state"]
    payload = _build([], [row])
    assert payload["native"]["percent"] == 24
    assert payload["native"]["status"] is None


# ── reconciliation: [p, p+1) band, capped boundary, no forced fit ─────────


def test_reconciliation_within_the_floor_band_is_a_match():
    payload = _build([_usage("e1", input_tokens=MILLION, output=100_000)], [_native(percent=24)])
    reconciliation = payload["reconciliation"]
    assert reconciliation == {
        "status": "within_rounding",
        "native_pct": 24,
        "native_lower_pct": "24",
        "native_upper_pct": "25",
        "modeled_pct": "24.533",
        "modeled_quota_units": "7.36000000",
        "modeled_complete": True,
        "latest_usage_at": "2026-09-14T12:00:00Z",
        "native_observed_at": "2026-09-15T02:50:00Z",
        "usage_ahead_of_native": False,
        "matches_within_rounding": True,
        "band_gap_pct": None,
        "native_basis": "provider_reported",
        "modeled_basis": "current_tariff_estimate",
    }


def test_reconciliation_below_and_above_the_band_carry_a_signed_gap_not_an_exact_residual():
    rows = [_usage("e1", input_tokens=MILLION, output=100_000)]
    below = _build(rows, [_native(percent=30)])["reconciliation"]
    assert below["status"] == "modeled_below"
    assert below["matches_within_rounding"] is False
    assert below["band_gap_pct"] == "-5.467"
    above = _build(rows, [_native(percent=20)])["reconciliation"]
    assert above["status"] == "modeled_above"
    assert above["matches_within_rounding"] is False
    assert above["band_gap_pct"] == "3.533"


def test_capped_native_is_an_at_least_100_boundary_with_no_exact_delta():
    rows = [_usage("e1", input_tokens=10 * MILLION, output=MILLION)]  # 18.4 USD → 73.6 units → 245.333
    payload = _build(rows, [_native(percent=100, state="rate_limited")])
    native = payload["native"]
    assert native["percent"] == 100
    assert native["capped"] is True
    assert native["status"] == "rate_limited"
    reconciliation = payload["reconciliation"]
    assert reconciliation["status"] == "native_capped"
    assert reconciliation["native_lower_pct"] == "100"
    assert reconciliation["native_upper_pct"] is None
    assert reconciliation["modeled_pct"] == "245.333"
    assert reconciliation["band_gap_pct"] is None
    assert reconciliation["matches_within_rounding"] is None
    assert any("at least 100" in note for note in payload["notes"])
    # Model rows are NOT rescaled to fit the native 100.
    assert _model(payload, "glm-5.3")["estimated_quota_pct"] == "245.333"


def test_modeled_rows_are_never_normalized_to_the_native_total():
    # 1.84 + 0.26 = 2.10 USD → 8.40 units → 28.000 points, whatever native says.
    rows = [_usage("e1", input_tokens=MILLION, output=100_000), _usage("e2", model="glm-5.3", cache_read=MILLION)]
    for percent in (5, 24, 60, 100):
        payload = _build(rows, [_native(percent=percent)])
        assert payload["totals"]["estimated_quota_pct"] == "28.000"
        assert payload["reconciliation"]["modeled_pct"] == "28.000"


# ── reconciliation: only co-temporal totals are compared ─────────────────


def test_usage_after_the_native_observation_declines_the_match_but_keeps_the_totals():
    """Native 18 observed 02:00Z; 1M input at 01:00Z (18.667, inside the
    band) then another 1M at 03:00Z, generated 03:39Z. The week's rows and
    totals still count both events, but the two cutoffs differ, so neither a
    rounding match nor a gap is asserted."""
    payload = _build(
        [
            _usage("e1", "2026-09-15T01:00:00Z", input_tokens=MILLION),
            _usage("e2", "2026-09-15T03:00:00Z", input_tokens=MILLION),
        ],
        [_native("2026-09-15T02:00:00Z", percent=18)],
    )
    assert _model(payload, "glm-5.3")["input_tokens"] == 2 * MILLION
    assert payload["totals"]["estimated_quota_pct"] == "37.333"
    reconciliation = payload["reconciliation"]
    assert reconciliation["status"] == "usage_ahead_of_native"
    assert reconciliation["matches_within_rounding"] is None
    assert reconciliation["band_gap_pct"] is None
    assert reconciliation["modeled_pct"] == "37.333"
    assert reconciliation["native_pct"] == 18
    assert reconciliation["native_lower_pct"] == "18"
    assert reconciliation["native_upper_pct"] == "19"
    assert reconciliation["latest_usage_at"] == "2026-09-15T03:00:00Z"
    assert reconciliation["native_observed_at"] == "2026-09-15T02:00:00Z"
    assert reconciliation["usage_ahead_of_native"] is True
    coverage = payload["coverage"]
    assert coverage["latest_usage_at"] == "2026-09-15T03:00:00Z"
    assert coverage["usage_rows_after_native"] == 1
    note = next(note for note in payload["notes"] if "cutoff" in note)
    assert "2026-09-15T03:00:00Z" in note and "2026-09-15T02:00:00Z" in note
    assert not any("within rounding" in note for note in payload["notes"])
    assert not any("outside the native" in note for note in payload["notes"])


def test_usage_entirely_before_the_native_observation_compares_normally():
    payload = _build(
        [
            _usage("e1", "2026-09-15T01:00:00Z", input_tokens=MILLION),
            _usage("e2", "2026-09-15T02:00:00Z", output=100_000),  # same instant: not ahead
        ],
        [_native("2026-09-15T02:00:00Z", percent=24)],
    )
    reconciliation = payload["reconciliation"]
    assert reconciliation["status"] == "within_rounding"
    assert reconciliation["matches_within_rounding"] is True
    assert reconciliation["latest_usage_at"] == "2026-09-15T02:00:00Z"
    assert reconciliation["usage_ahead_of_native"] is False
    assert payload["coverage"]["usage_rows_after_native"] == 0


def test_rows_after_generated_at_are_future_not_post_native_activity():
    payload = _build(
        [
            _usage("e1", "2026-09-15T01:00:00Z", input_tokens=MILLION, output=100_000),
            _usage("future", "2026-09-15T03:39:01Z", input_tokens=MILLION),
        ],
        [_native("2026-09-15T02:00:00Z", percent=24)],
    )
    coverage = payload["coverage"]
    assert coverage["ignored_usage_rows"]["future"] == 1
    assert coverage["latest_usage_at"] == "2026-09-15T01:00:00Z"
    assert coverage["usage_rows_after_native"] == 0
    assert payload["reconciliation"]["usage_ahead_of_native"] is False
    assert payload["reconciliation"]["status"] == "within_rounding"


def test_capped_native_stays_capped_when_usage_runs_ahead_of_it():
    payload = _build(
        [_usage("e1", "2026-09-15T03:00:00Z", input_tokens=10 * MILLION)],
        [_native("2026-09-15T02:00:00Z", percent=100, state="rate_limited")],
    )
    reconciliation = payload["reconciliation"]
    assert reconciliation["status"] == "native_capped"
    assert reconciliation["usage_ahead_of_native"] is True
    assert reconciliation["matches_within_rounding"] is None
    assert reconciliation["band_gap_pct"] is None
    assert any("at least 100" in note for note in payload["notes"])


def test_missing_native_has_no_cutoff_to_compare_against():
    payload = _build([_usage("e1", "2026-09-15T01:00:00Z", input_tokens=MILLION)], [])
    reconciliation = payload["reconciliation"]
    assert reconciliation["status"] == "native_missing"
    assert reconciliation["latest_usage_at"] == "2026-09-15T01:00:00Z"
    assert reconciliation["native_observed_at"] is None
    assert reconciliation["usage_ahead_of_native"] is None
    assert payload["coverage"]["usage_rows_after_native"] is None
    empty = _build([], [_native(percent=24)])
    assert empty["reconciliation"]["latest_usage_at"] is None
    assert empty["coverage"]["latest_usage_at"] is None
    assert empty["reconciliation"]["usage_ahead_of_native"] is False


# ── reconciliation: incomplete coverage never claims a match ──────────────


def test_unpriced_events_make_the_reconciliation_incomplete_not_a_match():
    """1M priced input (18.667, inside [18,19)) plus 9M tokens on an unknown
    model: the priced figure is kept, but no rounding match or gap is
    claimed because the modeled total is not the whole week."""
    payload = _build(
        [
            _usage("e1", "2026-09-15T01:00:00Z", input_tokens=MILLION),
            _usage("e2", "2026-09-15T01:30:00Z", model="mystery-model", input_tokens=9 * MILLION),
        ],
        [_native("2026-09-15T02:00:00Z", percent=18)],
    )
    reconciliation = payload["reconciliation"]
    assert reconciliation["status"] == "modeled_incomplete"
    assert reconciliation["modeled_complete"] is False
    assert reconciliation["matches_within_rounding"] is None
    assert reconciliation["band_gap_pct"] is None
    assert reconciliation["modeled_pct"] == "18.667"
    assert reconciliation["native_lower_pct"] == "18"
    assert reconciliation["native_upper_pct"] == "19"
    assert payload["totals"]["estimated_quota_pct"] == "18.667"
    assert payload["coverage"]["unpriced_events"] == 1
    assert any("incomplete" in note for note in payload["notes"])
    assert not any("within rounding" in note for note in payload["notes"])


def test_missing_token_fields_make_the_reconciliation_incomplete_not_a_match():
    payload = _build(
        [
            _usage("e1", "2026-09-15T01:00:00Z", input_tokens=MILLION),
            _usage("e2", "2026-09-15T01:30:00Z", cache_read=None, missing_fields=["cache_read_tokens"]),
        ],
        [_native("2026-09-15T02:00:00Z", percent=18)],
    )
    reconciliation = payload["reconciliation"]
    assert reconciliation["status"] == "modeled_incomplete"
    assert reconciliation["matches_within_rounding"] is None
    assert reconciliation["band_gap_pct"] is None
    assert payload["coverage"]["missing_token_rows"] == 1
    assert any("incomplete" in note for note in payload["notes"])


def test_native_missing_stale_and_capped_take_priority_over_incomplete_coverage():
    rows = [
        _usage("e1", "2026-09-15T01:00:00Z", input_tokens=MILLION),
        _usage("e2", "2026-09-15T01:30:00Z", model="mystery-model", input_tokens=9 * MILLION),
    ]
    assert _build(rows, [])["reconciliation"]["status"] == "native_missing"
    stale_at = (NOW - timedelta(seconds=NATIVE_FRESH_SECONDS)).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert _build(rows, [_native(stale_at, percent=18)])["reconciliation"]["status"] == "native_stale"
    capped = _build(rows, [_native("2026-09-15T02:00:00Z", percent=100)])["reconciliation"]
    assert capped["status"] == "native_capped"
    assert capped["modeled_complete"] is False
    # Incomplete coverage is reported before the cutoff mismatch when both hold.
    rows.append(_usage("e3", "2026-09-15T03:00:00Z", input_tokens=1))
    both = _build(rows, [_native("2026-09-15T02:00:00Z", percent=18)])["reconciliation"]
    assert both["status"] == "modeled_incomplete"
    assert both["usage_ahead_of_native"] is True
    assert both["matches_within_rounding"] is None


# ── reconciliation: unrounded comparison against the band edges ──────────


def test_reconciliation_compares_the_unrounded_total_not_the_printed_percent():
    """1,017,835 glm-5.3 input tokens are exactly 18.99958666…%, inside
    [18,19), although the printed 3-dp figure reads 19.000."""
    payload = _build([_usage("e1", input_tokens=1_017_835)], [_native(percent=18)])
    assert _model(payload, "glm-5.3")["estimated_quota_pct"] == "19.000"
    assert payload["totals"]["estimated_quota_pct"] == "19.000"
    reconciliation = payload["reconciliation"]
    assert reconciliation["modeled_pct"] == "19.000"
    assert reconciliation["status"] == "within_rounding"
    assert reconciliation["matches_within_rounding"] is True
    assert reconciliation["band_gap_pct"] is None
    json.dumps(payload)


def test_band_edges_are_lower_inclusive_and_upper_exclusive_on_exact_values():
    # 750,000 input tokens: 1.05 USD × 4 = 4.2 units = exactly 14.000 points.
    exact = [_usage("e1", input_tokens=750_000)]
    on_lower = _build(exact, [_native(percent=14)])["reconciliation"]
    assert on_lower["status"] == "within_rounding"
    assert on_lower["matches_within_rounding"] is True
    on_upper = _build(exact, [_native(percent=13)])["reconciliation"]
    assert on_upper["status"] == "modeled_above"
    assert on_upper["matches_within_rounding"] is False
    assert on_upper["band_gap_pct"] == "0.000"
    above_by_one = _build(exact, [_native(percent=15)])["reconciliation"]
    assert above_by_one["status"] == "modeled_below"
    assert above_by_one["band_gap_pct"] == "-1.000"
    # One token short of the lower edge is below it, however small the gap prints.
    just_under = _build([_usage("e1", input_tokens=749_999)], [_native(percent=14)])["reconciliation"]
    assert just_under["status"] == "modeled_below"
    assert just_under["matches_within_rounding"] is False
    assert just_under["band_gap_pct"] == "-0.000"


def test_signed_corrections_feed_the_unrounded_comparison():
    # 1M − 250,000 = 750,000 input tokens → exactly 14.000, so native 14 matches
    # and native 13 is exceeded by exactly 0.
    rows = [
        _usage("e1", input_tokens=MILLION),
        _usage("c1", input_tokens=-250_000, record_kind="correction",
               corrects_source_namespace="private:hermes-usage", corrects_source_event_id="e1"),
    ]
    assert _build(rows, [_native(percent=14)])["reconciliation"]["status"] == "within_rounding"
    above = _build(rows, [_native(percent=13)])["reconciliation"]
    assert above["status"] == "modeled_above"
    assert above["band_gap_pct"] == "0.000"


# ── payload hygiene ────────────────────────────────────────────────────────


def test_payload_is_json_serializable_and_carries_no_private_fields():
    payload = _build(
        [_usage("e1", input_tokens=MILLION, output=100_000)],
        [_native(percent=24)],
    )
    text = json.dumps(payload)
    for forbidden in (
        "provider_payload_ref", "source_namespace", "source_event_id", "source_observation_id",
        "account_ref", "session_id", "sess-private", "/private", "x_", "cookie", "auth", "secret",
    ):
        assert forbidden not in text, forbidden

    def walk(value):
        if isinstance(value, dict):
            for key, inner in value.items():
                assert isinstance(key, str) and not key.startswith("x_")
                walk(inner)
        elif isinstance(value, list):
            for inner in value:
                walk(inner)
        else:
            assert not isinstance(value, Decimal)

    walk(payload)
    assert set(payload) == {
        "generated_at", "window", "account", "native", "models", "totals",
        "reconciliation", "coverage", "tariff", "notes",
    }
    assert payload["account"] == {
        "provider": "opencode-go",
        "label": "OpenCode Go",
        "provider_account_index": 1,
        "provider_account_count": 1,
    }
    assert all(isinstance(note, str) and note for note in payload["notes"])


def test_notes_state_the_basis_and_the_tariff_caveat():
    notes = " ".join(_build([], []).get("notes", []))
    assert "current-tariff estimate" in notes
    assert "floor" in notes or "whole" in notes
    assert "schedule" in notes and "week" in notes
    assert "reasoning" in notes.lower()
