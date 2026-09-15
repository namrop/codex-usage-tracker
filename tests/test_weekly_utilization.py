"""Weekly allowance utilization (keeper request 2026-09-14, Discord message
1549243774775853067: "what percentage of my weekly windows I am using ...
aggregate, and per provider").

Contract: ``/tmp/ai-usage-weekly-contract.md`` (shared with the frontend
worker). The pure module is exercised directly with a frozen ``now``; the
endpoint tests prove the private read path, validation and the 168-hour
roster rule.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json

import pytest

import codex_usage_tracker.dashboard as dashboard_module
from codex_usage_tracker.dashboard import create_app
from codex_usage_tracker.weekly_utilization import build_weekly_utilization


NOW = datetime(2026, 7, 14, 12, 34, 56, tzinfo=timezone.utc)


class _FrozenNoon(datetime):
    @classmethod
    def now(cls, tz=None):
        return cls(2026, 7, 14, 12, 34, 56, tzinfo=tz)


def _obs(source_id: str, observed_at: str, used, resets_at=None, **updates):
    row = {
        "fact_type": "quota_observation_v1",
        "schema_version": 1,
        "source_namespace": "private:test-quota",
        "source_observation_id": source_id,
        "harness": "claude_code",
        "observed_at": observed_at,
        "provider": "anthropic",
        "quota_name": "seven_day",
        "quota_scope": "account",
        "window_kind": "rolling",
        "unit": "percent",
        "measurement_confidence": "exact",
        "limit_value": "100",
        "used_value": None if used is None else str(used),
        "remaining_value": None if used is None else str(100 - used),
        "resets_at": resets_at,
        "account_ref": None,
        "provider_payload_ref": "/private/provider/payload",
    }
    row.update(updates)
    return row


PRIMARY = (False, "")
SECONDARY = (True, "claude-code-secondary")
ROSTER = {
    "anthropic": {PRIMARY: 1, SECONDARY: 2},
    "openai-codex": {PRIMARY: 1},
}


def _build(rows, *, weeks=4, roster=ROSTER):
    return build_weekly_utilization(rows, generated_at=NOW, weeks=weeks, account_indexes=roster)


def _window(payload, provider, index, quota_name):
    return next(
        window
        for window in payload["windows"]
        if window["provider"] == provider
        and window["provider_account_index"] == index
        and window["quota_name"] == quota_name
    )


def _provider(summary, provider):
    return next(entry for entry in summary["providers"] if entry["provider"] == provider)


# ── current: equal-account mean of fresh primary readings ─────────────────


def test_current_summary_is_the_mean_of_account_percentages():
    """Claude 1 at 80, Claude 2 at 20, Codex at 50: each ACCOUNT counts once,
    so the aggregate is 50.0 (not the mean of provider means, 50.0 here too,
    but the anthropic provider summary shows two accounts). The Fable model
    cap is a detail row and never enters any denominator."""
    rows = [
        _obs("c1", "2026-07-14T12:00:00Z", 80, "2026-07-18T09:00:00Z"),
        _obs("c2", "2026-07-14T12:00:00Z", 20, "2026-07-19T03:00:00Z", account_ref="claude-code-secondary"),
        _obs("f1", "2026-07-14T12:00:00Z", 95, "2026-07-18T09:00:00Z", quota_name="seven_day_fable"),
        _obs("o1", "2026-07-14T12:05:00Z", 50, "2026-07-20T00:00:00Z", provider="openai", quota_name="week", harness="codex"),
    ]
    payload = _build(rows)

    current = payload["current"]
    assert (current["used_pct"], current["remaining_pct"]) == ("50.0", "50.0")
    assert current["account_count"] == 3
    assert current["excluded_account_count"] == 0
    assert current["estimated_account_count"] == 0
    assert current["cycle_count"] == 0
    anthropic = _provider(current, "anthropic")
    assert (anthropic["used_pct"], anthropic["remaining_pct"], anthropic["account_count"]) == ("50.0", "50.0", 2)
    assert anthropic["label"] == "Anthropic / Claude"
    codex = _provider(current, "openai-codex")
    assert (codex["used_pct"], codex["account_count"]) == ("50.0", 1)
    assert [entry["provider"] for entry in current["providers"]] == list(dashboard_module._QUOTA_PROVIDER_ORDER)

    claude_one = _window(payload, "anthropic", 1, "seven_day")
    assert claude_one["label"] == "Claude 1"
    assert claude_one["role"] == "primary"
    assert claude_one["provider_account_count"] == 2
    assert claude_one["current"] == {
        "used_pct": "80.0",
        "remaining_pct": "20.0",
        "observed_at": "2026-07-14T12:00:00Z",
        "resets_at": "2026-07-18T09:00:00Z",
        "confidence": "exact",
        "status": "fresh",
        "eligible": True,
    }
    fable = _window(payload, "anthropic", 1, "seven_day_fable")
    assert fable["role"] == "model_cap"
    assert fable["current"]["used_pct"] == "95.0"
    assert _window(payload, "openai-codex", 1, "week")["label"] == "Codex"
    assert payload["weighting"] == "equal_account"
    assert payload["weeks"] == 4
    assert payload["lookback_days"] == 35


def test_stale_and_invalid_current_readings_are_excluded_not_zero():
    """A 4-hour-old reading is stale (3h threshold), a null used value has no
    ratio, and a reading whose reset has already passed says nothing about
    now. All three stay visible with their status and leave the denominator;
    the estimated contributor is counted, not dropped."""
    rows = [
        _obs("c1", "2026-07-14T12:00:00Z", 40, "2026-07-18T09:00:00Z", measurement_confidence="estimated"),
        _obs("c2", "2026-07-14T08:30:00Z", 10, "2026-07-19T03:00:00Z", account_ref="claude-code-secondary"),
        _obs("o1", "2026-07-14T12:05:00Z", None, None, provider="openai", quota_name="week", harness="codex",
             x_provider_state="inactive_or_not_reported"),
        _obs("k1", "2026-07-14T12:00:00Z", 55, "2026-07-14T12:20:00Z", provider="kimi", quota_name="week", harness="kimi_code"),
    ]
    roster = {**ROSTER, "kimi-k3-coding": {PRIMARY: 1}}
    payload = _build(rows, roster=roster)

    current = payload["current"]
    assert (current["used_pct"], current["remaining_pct"]) == ("40.0", "60.0")
    assert current["account_count"] == 1
    assert current["excluded_account_count"] == 3
    assert current["estimated_account_count"] == 1
    stale = _window(payload, "anthropic", 2, "seven_day")["current"]
    assert (stale["used_pct"], stale["status"], stale["eligible"]) == ("10.0", "stale", False)
    invalid = _window(payload, "openai-codex", 1, "week")["current"]
    assert (invalid["used_pct"], invalid["status"], invalid["eligible"]) == (None, "no_utilization_ratio", False)
    passed = _window(payload, "kimi-k3-coding", 1, "week")["current"]
    assert (passed["used_pct"], passed["status"], passed["eligible"]) == ("55.0", "reset_passed_since_observation", False)
    # The Kimi window's target has passed with no rollover observed: that is
    # one displayed, excluded (unconfirmed) cycle.
    assert payload["coverage"] == {"source_rows": 4, "invalid_rows": 1, "excluded_cycles": 1}


# ── completed: inferred reset periods ─────────────────────────────────────


def _codex(source_id, observed_at, used, resets_at=None, **updates):
    return _obs(source_id, observed_at, used, resets_at, provider="openai", quota_name="week", harness="codex", **updates)


def test_completed_cycles_are_inferred_from_clustered_reset_targets():
    """Two real Codex weeks: reset targets flap by up to two minutes across
    samples and a null-reset sample sits inside the first week; neither
    creates an extra cycle. Each cycle reports the last valid sample before
    its reset, and the current (future-target) window is not a cycle."""
    rows = [
        _codex("a1", "2026-06-28T10:00:00Z", 50, "2026-06-30T10:00:00Z"),
        _codex("a2", "2026-06-30T08:00:00Z", 70, "2026-06-30T10:01:30Z"),
        _codex("a3", "2026-06-30T09:00:00Z", 80, None),
        _codex("a4", "2026-06-30T09:30:00Z", 85, "2026-06-30T09:59:00Z"),
        _codex("b1", "2026-06-30T10:30:00Z", 3, "2026-07-07T10:00:00Z"),
        _codex("b2", "2026-07-07T09:00:00Z", 60, "2026-07-07T10:00:00Z"),
        _codex("c1", "2026-07-07T11:00:00Z", 1, "2026-07-14T10:00:00Z"),
        _codex("c2", "2026-07-14T09:45:00Z", 44, "2026-07-14T10:00:00Z"),
        _codex("d1", "2026-07-14T12:00:00Z", 30, "2026-07-21T10:00:00Z"),
    ]
    payload = _build(rows, roster={"openai-codex": {PRIMARY: 1}})

    window = _window(payload, "openai-codex", 1, "week")
    assert window["completed"] == {
        "used_pct": "63.0",
        "remaining_pct": "37.0",
        "cycle_count": 3,
        "excluded_cycle_count": 0,
    }
    assert [cycle["resets_at"] for cycle in window["cycles"]] == [
        "2026-07-14T10:00:00Z",
        "2026-07-07T10:00:00Z",
        "2026-06-30T09:59:00Z",
    ]
    first_week = window["cycles"][-1]
    assert first_week == {
        "resets_at": "2026-06-30T09:59:00Z",
        "last_observed_at": "2026-06-30T09:30:00Z",
        "used_pct": "85.0",
        "remaining_pct": "15.0",
        "confidence": "exact",
        "eligible": True,
        "reasons": [],
        "last_sample_seconds_before_reset": 1740,
    }
    assert window["cycles"][0]["last_sample_seconds_before_reset"] == 900
    completed = payload["completed"]
    assert (completed["used_pct"], completed["remaining_pct"]) == ("63.0", "37.0")
    assert (completed["account_count"], completed["cycle_count"]) == (1, 3)
    assert _provider(completed, "openai-codex")["cycle_count"] == 3
    assert window["current"]["resets_at"] == "2026-07-21T10:00:00Z"


def test_flagged_cycles_stay_visible_but_leave_the_denominator():
    """Every excluded cycle keeps its observed value and says why. Only the
    clean week reaches the headline; ``coverage.excluded_cycles`` counts the
    rest."""
    rows = [
        # Week 1: last sample five hours before the reset — missing tail.
        _codex("a1", "2026-06-09T05:00:00Z", 70, "2026-06-09T10:00:00Z"),
        # Week 2: replaced a day early (manual reset / early boundary).
        _codex("b1", "2026-06-10T00:00:00Z", 5, "2026-06-16T10:00:00Z"),
        _codex("b2", "2026-06-15T09:30:00Z", 66, "2026-06-16T10:00:00Z"),
        _codex("c0", "2026-06-15T10:00:00Z", 2, "2026-06-23T10:00:00Z"),
        # Week 3: counter went 70 -> 40 under the same target; a null-reset
        # sample carries the drop and must not hide it.
        _codex("c1", "2026-06-22T08:00:00Z", 70, "2026-06-23T10:00:00Z"),
        _codex("c2", "2026-06-22T09:00:00Z", 40, None),
        _codex("c3", "2026-06-23T09:30:00Z", 75, "2026-06-23T10:00:00Z"),
        # Week 4: clean.
        _codex("d1", "2026-06-23T10:30:00Z", 1, "2026-06-30T10:00:00Z"),
        _codex("d2", "2026-06-30T09:00:00Z", 58, "2026-06-30T10:00:00Z"),
        # Week 5: target passed, rollover never observed.
        _codex("e1", "2026-06-30T11:00:00Z", 2, "2026-07-07T10:00:00Z"),
        _codex("e2", "2026-07-07T09:00:00Z", 90, "2026-07-07T10:00:00Z"),
    ]
    payload = _build(rows, roster={"openai-codex": {PRIMARY: 1}})

    window = _window(payload, "openai-codex", 1, "week")
    by_reset = {cycle["resets_at"]: cycle for cycle in window["cycles"]}
    assert by_reset["2026-06-09T10:00:00Z"]["reasons"] == ["missing_tail"]
    assert by_reset["2026-06-09T10:00:00Z"]["used_pct"] == "70.0"
    assert by_reset["2026-06-16T10:00:00Z"]["reasons"] == ["early_boundary_replacement"]
    assert by_reset["2026-06-23T10:00:00Z"]["reasons"] == ["counter_decreased"]
    assert by_reset["2026-06-23T10:00:00Z"]["used_pct"] == "75.0"
    assert by_reset["2026-06-30T10:00:00Z"] == {
        "resets_at": "2026-06-30T10:00:00Z",
        "last_observed_at": "2026-06-30T09:00:00Z",
        "used_pct": "58.0",
        "remaining_pct": "42.0",
        "confidence": "exact",
        "eligible": True,
        "reasons": [],
        "last_sample_seconds_before_reset": 3600,
    }
    assert by_reset["2026-07-07T10:00:00Z"]["reasons"] == ["unconfirmed_completion"]
    assert by_reset["2026-07-07T10:00:00Z"]["used_pct"] == "90.0"
    assert all(not by_reset[key]["eligible"] for key in by_reset if key != "2026-06-30T10:00:00Z")
    assert window["completed"] == {
        "used_pct": "58.0",
        "remaining_pct": "42.0",
        "cycle_count": 1,
        "excluded_cycle_count": 4,
    }
    assert payload["coverage"]["excluded_cycles"] == 4
    assert window["current"]["status"] == "stale"


def test_completed_aggregate_weights_accounts_equally_and_honours_weeks():
    """Claude 1 has three clean weeks at 90 and Claude 2 one clean week at
    30. The aggregate is the mean of account means (60.0), never the pooled
    75.0. With ``weeks=2`` only the newest two Claude 1 cycles count; the
    third is displayed as ``beyond_requested_weeks``."""
    def week(prefix, reset, used, **updates):
        day = reset[:10]
        return [
            _obs(f"{prefix}-start", f"{day}T00:00:00Z", 1, reset, **updates),
            _obs(f"{prefix}-end", f"{day}T08:00:00Z", used, reset, **updates),
        ]

    rows = (
        week("a", "2026-06-23T09:00:00Z", 90)
        + week("b", "2026-06-30T09:00:00Z", 90)
        + week("c", "2026-07-07T09:00:00Z", 90)
        # Week d: only sampled once, seven days before its reset (missing
        # tail); it is displayed, excluded, and its estimated confidence
        # therefore does not mark the account as estimated.
        + [_obs("d0", "2026-07-07T10:00:00Z", 0, "2026-07-14T09:00:00Z", measurement_confidence="estimated")]
        + [_obs("now", "2026-07-14T12:00:00Z", 10, "2026-07-21T09:00:00Z")]
        + week("e", "2026-07-07T09:00:00Z", 30, account_ref="claude-code-secondary")
        + [_obs("e-next", "2026-07-07T10:00:00Z", 2, "2026-07-21T09:00:00Z", account_ref="claude-code-secondary")]
    )
    payload = _build(rows, roster={"anthropic": {PRIMARY: 1, SECONDARY: 2}})
    completed = payload["completed"]
    assert (completed["used_pct"], completed["remaining_pct"]) == ("60.0", "40.0")
    assert (completed["account_count"], completed["cycle_count"]) == (2, 4)
    assert completed["estimated_account_count"] == 0
    claude_one = _window(payload, "anthropic", 1, "seven_day")
    assert claude_one["completed"] == {
        "used_pct": "90.0", "remaining_pct": "10.0", "cycle_count": 3, "excluded_cycle_count": 1,
    }
    assert claude_one["cycles"][0]["reasons"] == ["missing_tail"]

    two_weeks = _build(rows, weeks=2, roster={"anthropic": {PRIMARY: 1, SECONDARY: 2}})
    claude_one = _window(two_weeks, "anthropic", 1, "seven_day")
    assert claude_one["completed"]["cycle_count"] == 2
    assert claude_one["completed"]["excluded_cycle_count"] == 2
    assert claude_one["cycles"][-1]["reasons"] == ["beyond_requested_weeks"]
    assert claude_one["cycles"][-1]["used_pct"] == "90.0"
    assert two_weeks["lookback_days"] == 21


# A lagging post-boundary sample: the provider (or the collector) still
# reported the OLD target for a couple of minutes after the reset. Its
# reading belongs to the new window and must never stand in for, or be
# compared against, the old window's last pre-reset reading.
_R = "2026-09-07T00:00:00Z"
# Mid-week after R: the successor window (target R+7d) is still open.
_R_NOW = datetime(2026, 9, 10, 12, 0, 0, tzinfo=timezone.utc)
_PRE_BOUNDARY = [
    _codex("old-1", "2026-09-06T23:00:00Z", 80, _R),
    _codex("old-2", "2026-09-06T23:58:00Z", 90, _R),
]
_NEXT_WINDOW = [_codex("new-1", "2026-09-07T00:12:00Z", 1, "2026-09-14T00:00:00Z")]
_EXPECTED_PRE_RESET_CYCLE = {
    "resets_at": _R,
    "last_observed_at": "2026-09-06T23:58:00Z",
    "used_pct": "90.0",
    "remaining_pct": "10.0",
    "confidence": "exact",
    "eligible": True,
    "reasons": [],
    "last_sample_seconds_before_reset": 120,
}


@pytest.mark.parametrize(
    "post_boundary",
    [
        pytest.param([_codex("post", "2026-09-07T00:02:00Z", 95, _R)], id="higher-reading-same-target"),
        pytest.param([_codex("post", "2026-09-07T00:02:00Z", 3, _R)], id="rolled-reading-same-target"),
        pytest.param([_codex("post", "2026-09-07T00:02:00Z", 3, None)], id="rolled-reading-null-target"),
    ],
)
def test_post_boundary_samples_never_replace_the_last_pre_reset_reading(post_boundary):
    """The terminal sample is the last valid one observed at/before the
    target (90 at R-2m, lead 120s), not a later sample that still carried
    the old target (95 at R+2m, lead -120s). A post-boundary reading of 3
    is the new window's counter, not a mid-window drop, so it must not
    exclude the cycle as ``counter_decreased`` either."""
    payload = build_weekly_utilization(
        _PRE_BOUNDARY + post_boundary + _NEXT_WINDOW,
        generated_at=_R_NOW,
        weeks=4,
        account_indexes={"openai-codex": {PRIMARY: 1}},
    )
    window = _window(payload, "openai-codex", 1, "week")
    assert [cycle["resets_at"] for cycle in window["cycles"]] == [_R]
    assert window["cycles"][0] == _EXPECTED_PRE_RESET_CYCLE
    assert window["completed"]["cycle_count"] == 1
    assert window["completed"]["used_pct"] == "90.0"


def test_genuine_mid_window_counter_drops_still_exclude_the_cycle():
    """Only samples at/before the target are scanned, and a drop among them
    (80 -> 40 on a null-target sample, then 90 under the same target) is
    still a mid-window decrease: displayed with the last pre-reset value,
    excluded."""
    rows = [
        _codex("old-1", "2026-09-06T23:00:00Z", 80, _R),
        _codex("dip", "2026-09-06T23:30:00Z", 40, None),
        _codex("old-2", "2026-09-06T23:58:00Z", 90, _R),
        _codex("post", "2026-09-07T00:02:00Z", 95, _R),
    ] + _NEXT_WINDOW
    payload = build_weekly_utilization(
        rows, generated_at=_R_NOW, weeks=4, account_indexes={"openai-codex": {PRIMARY: 1}},
    )
    cycle = _window(payload, "openai-codex", 1, "week")["cycles"][0]
    assert cycle["reasons"] == ["counter_decreased"]
    assert cycle["eligible"] is False
    assert (cycle["used_pct"], cycle["last_sample_seconds_before_reset"]) == ("90.0", 120)


def test_cycle_without_a_valid_pre_boundary_sample_is_excluded_not_synthesized():
    """A window whose only usable readings came after its target has no
    terminal figure: nothing is synthesized from the post-boundary sample,
    and the cycle stays visible as ``no_valid_sample``."""
    rows = [
        _codex("old-null", "2026-09-06T23:58:00Z", None, _R),
        _codex("post", "2026-09-07T00:02:00Z", 95, _R),
    ] + _NEXT_WINDOW
    payload = build_weekly_utilization(
        rows, generated_at=_R_NOW, weeks=4, account_indexes={"openai-codex": {PRIMARY: 1}},
    )
    window = _window(payload, "openai-codex", 1, "week")
    assert [cycle["resets_at"] for cycle in window["cycles"]] == [_R]
    cycle = window["cycles"][0]
    assert cycle["reasons"] == ["no_valid_sample"]
    assert cycle["eligible"] is False
    assert (cycle["used_pct"], cycle["last_observed_at"], cycle["last_sample_seconds_before_reset"]) == (None, None, None)
    assert window["completed"] == {
        "used_pct": None, "remaining_pct": None, "cycle_count": 0, "excluded_cycle_count": 1,
    }


def test_estimated_cycles_count_as_estimated_contributors():
    rows = [
        _obs("a1", "2026-07-06T00:00:00Z", 1, "2026-07-07T09:00:00Z"),
        _obs("a2", "2026-07-07T08:00:00Z", 64, "2026-07-07T09:00:00Z", measurement_confidence="estimated"),
        _obs("a3", "2026-07-14T10:00:00Z", 0, "2026-07-21T09:00:00Z"),
    ]
    payload = _build(rows, roster={"anthropic": {PRIMARY: 1}})
    completed = payload["completed"]
    assert completed["used_pct"] == "64.0"
    assert completed["estimated_account_count"] == 1
    cycle = _window(payload, "anthropic", 1, "seven_day")["cycles"][0]
    assert (cycle["confidence"], cycle["eligible"]) == ("estimated", True)


# ── roster and privacy ─────────────────────────────────────────────────────


def test_roster_accounts_without_a_primary_window_are_listed_as_excluded():
    """The roster (168h subscriptions window) owns the ordinals: an account
    outside it never appears and never renumbers ``Claude 2``; a roster
    account with no weekly observation is an explicit excluded row."""
    rows = [
        _obs("old", "2026-07-14T12:00:00Z", 99, "2026-07-18T09:00:00Z", account_ref="retired-account"),
        _obs("two", "2026-07-14T12:00:00Z", 35, "2026-07-19T03:00:00Z", account_ref="claude-code-secondary"),
    ]
    payload = _build(rows, roster={"anthropic": {PRIMARY: 1, SECONDARY: 2}})

    assert [(w["label"], w["quota_name"]) for w in payload["windows"]] == [
        ("Claude 1", "seven_day"),
        ("Claude 2", "seven_day"),
    ]
    missing = _window(payload, "anthropic", 1, "seven_day")
    assert missing["current"]["status"] == "no_observations"
    assert missing["current"]["eligible"] is False
    assert missing["cycles"] == []
    current = payload["current"]
    assert (current["used_pct"], current["account_count"], current["excluded_account_count"]) == ("35.0", 1, 1)
    assert payload["coverage"]["source_rows"] == 1
    assert any("roster" in note for note in payload["notes"])


def test_weighting_note_counts_only_tracked_primary_weekly_accounts():
    """The 168h roster also carries accounts without a weekly quota
    (OpenRouter, DeepSeek). The weighting note must count the accounts that
    actually have a primary weekly window — the same number the summaries
    split into contributing + excluded — and never model caps."""
    rows = [
        _obs("c1", "2026-07-14T12:00:00Z", 80, "2026-07-18T09:00:00Z"),
        _obs("f1", "2026-07-14T12:00:00Z", 95, "2026-07-18T09:00:00Z", quota_name="seven_day_fable"),
        _obs("o1", "2026-07-14T12:05:00Z", 50, "2026-07-20T00:00:00Z", provider="openai", quota_name="week", harness="codex"),
    ]
    roster = {
        "anthropic": {PRIMARY: 1, SECONDARY: 2},
        "openai-codex": {PRIMARY: 1},
        "openrouter": {PRIMARY: 1},
        "deepseek": {PRIMARY: 1},
    }
    payload = _build(rows, roster=roster)

    primary_windows = [window for window in payload["windows"] if window["role"] == "primary"]
    assert len(primary_windows) == 3
    assert len(payload["windows"]) == 4  # + the Fable model cap
    for summary in (payload["current"], payload["completed"]):
        assert summary["account_count"] + summary["excluded_account_count"] == 3
    note = payload["notes"][0]
    assert "3 tracked primary weekly accounts" in note
    assert "5 " not in note and "roster accounts" not in note


def test_payload_never_serializes_private_references():
    rows = [
        _obs("c2", "2026-07-14T12:00:00Z", 20, "2026-07-19T03:00:00Z", account_ref="claude-code-secondary"),
    ]
    serialized = json.dumps(_build(rows))
    for forbidden in ("claude-code-secondary", "provider_payload_ref", "source_namespace", "account_ref", "/private/"):
        assert forbidden not in serialized


def test_empty_input_is_a_null_summary_not_zero_percent():
    payload = _build([], roster={})
    assert payload["current"]["used_pct"] is None
    assert payload["completed"]["remaining_pct"] is None
    assert payload["windows"] == []
    assert payload["coverage"] == {"source_rows": 0, "invalid_rows": 0, "excluded_cycles": 0}


# ── endpoint ───────────────────────────────────────────────────────────────


def _client(tmp_path, rows, name="quota.jsonl"):
    ledger = tmp_path / name
    ledger.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
    return create_app(quota_ledger=str(ledger)).test_client()


def test_endpoint_numbers_accounts_against_the_168h_roster(tmp_path, monkeypatch):
    """``alpha`` last reported ten days ago (inside the 35-day read, outside
    the 168h roster) and ``beta`` today. Beta is Claude 1 of 1, exactly as
    ``/api/subscriptions?hours=168`` numbers it; alpha is not a window."""
    monkeypatch.setattr(dashboard_module, "datetime", _FrozenNoon)
    rows = [
        _obs("x1", "2026-07-04T00:00:00Z", 10, "2026-07-07T04:00:00Z", account_ref="alpha"),
        _obs("b0", "2026-07-07T05:00:00Z", 2, "2026-07-14T04:00:00Z", account_ref="beta"),
        _obs("b1", "2026-07-14T03:00:00Z", 48, "2026-07-14T04:00:00Z", account_ref="beta"),
        _obs("b2", "2026-07-14T11:00:00Z", 20, "2026-07-21T04:00:00Z", account_ref="beta"),
        # Future-dated fact: never admitted into the read.
        _obs("b3", "2026-07-14T13:00:00Z", 90, "2026-07-21T04:00:00Z", account_ref="beta"),
    ]
    client = _client(tmp_path, rows)

    response = client.get("/api/weekly-utilization")
    assert response.status_code == 200
    payload = response.get_json()
    assert set(payload) == {
        "generated_at", "weeks", "lookback_days", "weighting", "current", "completed",
        "windows", "coverage", "notes",
    }
    assert payload["generated_at"] == "2026-07-14T12:34:56Z"
    assert (payload["weeks"], payload["lookback_days"]) == (4, 35)
    assert [(w["label"], w["provider_account_index"], w["provider_account_count"]) for w in payload["windows"]] == [
        ("Claude", 1, 1)
    ]
    window = payload["windows"][0]
    assert window["current"]["used_pct"] == "20.0"
    assert window["completed"]["cycle_count"] == 1
    assert window["cycles"][0]["used_pct"] == "48.0"
    subscriptions = client.get("/api/subscriptions?hours=168&history=0").get_json()
    assert {(row["provider_account_index"], row["provider_account_count"]) for row in subscriptions["latest"]} == {(1, 1)}


def test_endpoint_reads_sqlite_ledgers_through_the_bounded_reader(tmp_path, monkeypatch):
    from codex_usage_tracker.canonical_ledger import append_sqlite_facts

    monkeypatch.setattr(dashboard_module, "datetime", _FrozenNoon)
    ledger = tmp_path / "quota.sqlite3"
    append_sqlite_facts(
        ledger,
        [
            _obs("b0", "2026-07-07T05:00:00Z", 2, "2026-07-14T04:00:00Z", account_ref="beta"),
            _obs("b1", "2026-07-14T03:00:00Z", 48, "2026-07-14T04:00:00Z", account_ref="beta"),
            _obs("b2", "2026-07-14T11:00:00Z", 20, "2026-07-21T04:00:00Z", account_ref="beta"),
        ],
        fact_type="quota_observation_v1",
    )
    client = create_app(quota_ledger=str(ledger)).test_client()
    payload = client.get("/api/weekly-utilization?weeks=3").get_json()
    assert payload["current"]["used_pct"] == "20.0"
    assert payload["completed"]["used_pct"] == "48.0"
    assert (payload["weeks"], payload["lookback_days"]) == (3, 28)

    monkeypatch.setattr(dashboard_module, "_PRIVATE_API_MAX_ROWS", 2)
    assert client.get("/api/weekly-utilization").status_code == 413


def test_endpoint_validates_weeks(tmp_path, monkeypatch):
    monkeypatch.setattr(dashboard_module, "datetime", _FrozenNoon)
    client = _client(tmp_path, [_obs("c1", "2026-07-14T12:00:00Z", 80, "2026-07-18T09:00:00Z")])
    for bad in ("0", "13", "abc", "-1", "2.5"):
        assert client.get(f"/api/weekly-utilization?weeks={bad}").status_code == 400, bad
    twelve = client.get("/api/weekly-utilization?weeks=12")
    assert twelve.status_code == 200
    assert (twelve.get_json()["weeks"], twelve.get_json()["lookback_days"]) == (12, 91)
    one = client.get("/api/weekly-utilization?weeks=1").get_json()
    assert (one["weeks"], one["lookback_days"]) == (1, 14)


def test_endpoint_reports_unconfigured_missing_oversized_and_malformed_sources(tmp_path, monkeypatch):
    monkeypatch.delenv("QUOTA_LEDGER_PATH", raising=False)
    assert create_app().test_client().get("/api/weekly-utilization").status_code == 404
    assert create_app(quota_ledger=str(tmp_path / "absent.jsonl")).test_client().get(
        "/api/weekly-utilization"
    ).status_code == 404

    malformed = tmp_path / "bad.jsonl"
    malformed.write_text('{"fact_type": "quota_observation_v1"\n', encoding="utf-8")
    assert create_app(quota_ledger=str(malformed)).test_client().get("/api/weekly-utilization").status_code == 503

    monkeypatch.setattr(dashboard_module, "datetime", _FrozenNoon)
    monkeypatch.setattr(dashboard_module, "_PRIVATE_API_MAX_ROWS", 1)
    client = _client(tmp_path, [
        _obs("c1", "2026-07-14T12:00:00Z", 80, "2026-07-18T09:00:00Z"),
        _obs("c2", "2026-07-14T12:01:00Z", 81, "2026-07-18T09:00:00Z"),
    ])
    response = client.get("/api/weekly-utilization")
    assert response.status_code == 413
    assert response.get_json() == {"error": "ledger query exceeds private API row limit"}
