"""Quota-burn review regressions (2026-09-14).

Two independently reproduced backend defects in ``/api/quota-burn``:

1. ``_burn_projection_status`` compared ``seconds_to_empty`` (anchored at
   the latest observation) with ``seconds_to_reset`` (anchored at now), so a
   window whose observation-anchored ETA landed well before its reset could
   still be classified ``resets_first``. The classification must compare the
   same instants; the rate estimator and the observation-anchored ETA are
   unchanged.

2. ``_build_quota_burn_projection`` merged accounts absent from a supplied
   ``account_indexes`` roster into it and re-sorted, renumbering the accounts
   the subscriptions endpoint had already numbered. With an authoritative
   roster, series for accounts outside it are historical-only and are
   filtered out; without a roster (``account_indexes=None``) ordinals are
   derived normally.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json

import pytest

import codex_usage_tracker.dashboard as dashboard_module
from codex_usage_tracker.dashboard import (
    _build_quota_burn_projection,
    _burn_projection_status,
    create_app,
)


class _FrozenNoon(datetime):
    """2026-07-14T12:34:56Z, the frozen "now" every expectation below uses."""

    @classmethod
    def now(cls, tz=None):
        return cls(2026, 7, 14, 12, 34, 56, tzinfo=tz)


NOW = datetime(2026, 7, 14, 12, 34, 56, tzinfo=timezone.utc)


def _observation(source_id: str, **updates):
    row = {
        "fact_type": "quota_observation_v1",
        "schema_version": 1,
        "source_namespace": "private:test-quota",
        "source_observation_id": source_id,
        "harness": "claude_code",
        "observed_at": "2026-07-14T12:00:00Z",
        "provider": "anthropic",
        "quota_name": "five_hour",
        "quota_scope": "account",
        "window_kind": "rolling",
        "unit": "percent",
        "measurement_confidence": "exact",
        "limit_value": "100",
        "account_ref": None,
        "provider_payload_ref": "/private/provider/payload",
    }
    row.update(updates)
    if "used_value" in row and row.get("unit") == "percent" and "remaining_value" not in row:
        row["remaining_value"] = str(100 - float(row["used_value"]))
    return row


def _pct(source_id: str, observed_at: str, used: float, **updates):
    return _observation(source_id, observed_at=observed_at, used_value=str(used), **updates)


def _client(tmp_path, rows):
    ledger = tmp_path / "quota.jsonl"
    ledger.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
    return create_app(quota_ledger=str(ledger)).test_client()


# ── 1. projection status compares the same instants ──────────────────────


def test_projection_status_compares_eta_and_reset_from_the_same_anchor():
    """Reproduction: now 12:34:56Z, latest observation 10:30Z at 50 used,
    80 pts/h, reset 13:00Z (1504 s from now). The observation-anchored ETA is
    11:07:30Z, before the reset, so the honest claim is
    ``projected_before_reset`` — not ``resets_first`` because 2250 s of
    burn exceeds the 1504 s left *from now*."""
    status, reason, seconds_to_empty = _burn_projection_status(
        latest_pct=50.0,
        latest_observed=datetime(2026, 7, 14, 10, 30, tzinfo=timezone.utc),
        window_kind="rolling",
        pct_per_hour=80.0,
        resets_at="2026-07-14T13:00:00Z",
        seconds_to_reset=1504.0,
        observation_count=2,
        now=NOW,
    )
    assert status == "projected_before_reset"
    assert reason == "empty_before_reset_at_observed_rate"
    # The estimator is untouched: 50 points left at 80 pts/h is 2250 s.
    assert seconds_to_empty == pytest.approx(2250.0)


def test_projection_status_still_says_resets_first_when_eta_lands_after_reset():
    """Same anchor rule, opposite verdict: 50 left at 10 pts/h from 10:30Z
    lands 15:30Z, after the 13:00Z reset."""
    status, reason, seconds_to_empty = _burn_projection_status(
        latest_pct=50.0,
        latest_observed=datetime(2026, 7, 14, 10, 30, tzinfo=timezone.utc),
        window_kind="rolling",
        pct_per_hour=10.0,
        resets_at="2026-07-14T13:00:00Z",
        seconds_to_reset=1504.0,
        observation_count=2,
        now=NOW,
    )
    assert (status, reason, seconds_to_empty) == (
        "resets_first",
        "reset_before_empty_at_observed_rate",
        None,
    )


def test_projection_status_eta_exactly_at_reset_is_projected_before_reset():
    """Boundary: 50 left at 20 pts/h from 10:30Z is exactly 13:00Z, the reset
    instant; the estimator's inclusive rule (``<=``) is preserved."""
    status, _, seconds_to_empty = _burn_projection_status(
        latest_pct=50.0,
        latest_observed=datetime(2026, 7, 14, 10, 30, tzinfo=timezone.utc),
        window_kind="rolling",
        pct_per_hour=20.0,
        resets_at="2026-07-14T13:00:00Z",
        seconds_to_reset=1504.0,
        observation_count=2,
        now=NOW,
    )
    assert status == "projected_before_reset"
    assert seconds_to_empty == pytest.approx(9000.0)


def test_quota_burn_api_projects_observation_anchored_eta_before_reset(tmp_path, monkeypatch):
    """End to end through the endpoint: 10 -> 50 over 10:00Z-10:30Z is
    80 pts/h; the ETA is 11:07:30Z, a conditional historical extrapolation
    that precedes the 13:00Z reset, so the row is projected_before_reset
    and carries that ETA. Past-ETA rows are not asserted exhausted."""
    monkeypatch.setattr(dashboard_module, "datetime", _FrozenNoon)
    rows = [
        _pct("a1", "2026-07-14T10:00:00Z", 10, resets_at="2026-07-14T13:00:00Z"),
        _pct("a2", "2026-07-14T10:30:00Z", 50, resets_at="2026-07-14T13:00:00Z"),
    ]
    client = _client(tmp_path, rows)
    [row] = client.get("/api/quota-burn?hours=3").get_json()["series"]
    assert row["burn_rate_pct_per_hour"] == pytest.approx(80.0)
    assert row["seconds_to_reset"] == 1504
    assert row["projection_status"] == "projected_before_reset"
    assert row["projection_reason"] == "empty_before_reset_at_observed_rate"
    assert row["projected_exhaustion_at"] == "2026-07-14T11:07:30Z"
    assert row["exhausted"] is False


# ── 2. a supplied roster is authoritative ────────────────────────────────


def _alpha_and_beta_rows():
    return [
        # Historical-only account, older than the roster.
        _pct("x1", "2026-07-07T11:30:00Z", 10, resets_at="2026-07-07T15:00:00Z", account_ref="alpha"),
        # Current account, burning now.
        _pct("b1", "2026-07-14T11:00:00Z", 20, resets_at="2026-07-14T15:00:00Z", account_ref="beta"),
        _pct("b2", "2026-07-14T12:00:00Z", 40, resets_at="2026-07-14T15:00:00Z", account_ref="beta"),
    ]


def test_projection_filters_series_outside_a_supplied_roster():
    """With the authoritative roster {beta: 1}, the older ``alpha`` account
    is not inserted and re-sorted (which renumbered beta to 2 of 2): its
    series is dropped and beta keeps ordinal 1 of 1 with every observation."""
    payload = _build_quota_burn_projection(
        _alpha_and_beta_rows(),
        generated_at=NOW,
        account_indexes={"anthropic": {(True, "beta"): 1}},
    )
    [row] = payload["series"]
    assert row["provider_account_index"] == 1
    assert row["provider_account_count"] == 1
    assert row["latest_used_pct"] == 40.0
    assert [o["used_pct"] for o in row["observations"]] == [20.0, 40.0]
    assert [d["rise_pct"] for d in row["deltas"]] == [20.0]


def test_projection_derives_ordinals_when_no_roster_is_supplied():
    """Pure helper callers pass no roster: every account in the rows is
    numbered by the shared sort, alpha 1 and beta 2 of 2."""
    payload = _build_quota_burn_projection(_alpha_and_beta_rows(), generated_at=NOW)
    ordinals = [
        (row["latest_used_pct"], row["provider_account_index"], row["provider_account_count"])
        for row in payload["series"]
    ]
    assert ordinals == [(10.0, 1, 2), (40.0, 2, 2)]


def _assert_burn_matches_subscription_roster(burn, subscriptions):
    latest = subscriptions["latest"]
    roster = {
        (row["provider_canonical"], row["provider_account_index"]): row["provider_account_count"]
        for row in latest
    }
    assert burn["series"], "burn payload should carry the current account"
    for entry in burn["series"]:
        key = (entry["provider"], entry["provider_account_index"])
        assert key in roster, entry
        assert entry["provider_account_count"] == roster[key], entry
        match = [
            row for row in latest
            if row["provider_canonical"] == entry["provider"]
            and row["quota_name"] == entry["quota_name"]
            and row["harness"] == entry["harness"]
            and row["provider_account_index"] == entry["provider_account_index"]
        ]
        assert len(match) == 1, entry
        assert float(match[0]["used_value"]) == entry["latest_used_pct"]


def test_quota_burn_hours_168_ordinals_match_subscriptions_hours_168(tmp_path, monkeypatch):
    """At hours=168 the burn read reaches 90 minutes past the 168-hour roster
    floor (2026-07-07T12:30Z from a 12:34:56Z now), so an account observed
    only at 2026-07-07T11:30Z is in the burn rows but not in the roster.
    The subscriptions endpoint numbers beta 1 of 1; the burn endpoint must
    say the same and must not surface the historical-only account."""
    monkeypatch.setattr(dashboard_module, "datetime", _FrozenNoon)
    client = _client(tmp_path, _alpha_and_beta_rows())

    subscriptions = client.get("/api/subscriptions?hours=168&history=0").get_json()
    assert [
        (row["provider_account_index"], row["provider_account_count"]) for row in subscriptions["latest"]
    ] == [(1, 1)]

    burn = client.get("/api/quota-burn?hours=168").get_json()
    assert "alpha" not in json.dumps(burn)
    assert [
        (row["latest_used_pct"], row["provider_account_index"], row["provider_account_count"])
        for row in burn["series"]
    ] == [(40.0, 1, 1)]
    _assert_burn_matches_subscription_roster(burn, subscriptions)


def test_quota_burn_default_window_ordinals_match_subscriptions_hours_168(tmp_path, monkeypatch):
    """Without hours= the burn read spans the default eight-day private
    window, wider than the 168-hour roster the dashboard numbers accounts
    against (``/api/subscriptions?hours=168``). An account last seen seven
    and a half days ago is outside that roster and stays out of the burn
    payload; the current account keeps the roster's ordinal."""
    monkeypatch.setattr(dashboard_module, "datetime", _FrozenNoon)
    rows = [
        _pct("x1", "2026-07-07T00:00:00Z", 10, resets_at="2026-07-07T04:00:00Z", account_ref="alpha"),
        _pct("b1", "2026-07-14T11:00:00Z", 20, resets_at="2026-07-14T15:00:00Z", account_ref="beta"),
        _pct("b2", "2026-07-14T12:00:00Z", 40, resets_at="2026-07-14T15:00:00Z", account_ref="beta"),
    ]
    client = _client(tmp_path, rows)

    subscriptions = client.get("/api/subscriptions?hours=168&history=0").get_json()
    assert [
        (row["provider_account_index"], row["provider_account_count"]) for row in subscriptions["latest"]
    ] == [(1, 1)]

    burn = client.get("/api/quota-burn").get_json()
    assert "alpha" not in json.dumps(burn)
    assert [
        (row["latest_used_pct"], row["provider_account_index"], row["provider_account_count"])
        for row in burn["series"]
    ] == [(40.0, 1, 1)]
    _assert_burn_matches_subscription_roster(burn, subscriptions)
