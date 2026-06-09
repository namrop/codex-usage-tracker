from __future__ import annotations

from codex_usage_tracker.policy_state import build_burn_projection_rows, latest_policy_state


def test_positive_burn_ignores_negative_reset_delta():
    rows = [
        {"fetched_at": "2026-06-04T00:00:00+00:00", "weekly_used_pct": 20.0, "session_used_pct": 0.0},
        {"fetched_at": "2026-06-04T01:00:00+00:00", "weekly_used_pct": 25.0, "session_used_pct": 2.0},
        {"fetched_at": "2026-06-04T02:00:00+00:00", "weekly_used_pct": 3.0, "session_used_pct": 0.0},
        {"fetched_at": "2026-06-04T03:00:00+00:00", "weekly_used_pct": 7.0, "session_used_pct": 1.0},
    ]

    projection = build_burn_projection_rows(rows, horizons_hours=(24,))[-1]

    assert projection["positive_weekly_burn_last_24h_pct"] == 9.0
    assert projection["net_weekly_delta_last_24h_pct"] == -13.0
    assert projection["positive_weekly_burn_rate_24h_pct_per_hour"] == 3.0


def test_latest_policy_state_modes_and_reason_codes():
    normal = latest_policy_state([
        {
            "fetched_at": "2026-06-04T01:00:00+00:00",
            "weekly_used_pct": 20.0,
            "session_used_pct": 2.0,
            "weekly_reset_at": 1781137839,
            "allowed": True,
            "limit_reached": False,
        }
    ])
    assert normal["policy_mode"] == "normal"
    assert normal["recommended_default_provider"] == "openai-codex"

    preserve = latest_policy_state([
        {
            "fetched_at": "2026-06-04T01:00:00+00:00",
            "weekly_used_pct": 83.0,
            "session_used_pct": 2.0,
            "weekly_reset_at": 1781137839,
            "allowed": True,
            "limit_reached": False,
        }
    ])
    assert preserve["policy_mode"] == "preserve"
    assert preserve["recommended_default_provider"] == "deepseek-v4-pro"
    assert "weekly_at_or_above_preserve_threshold" in preserve["reason_codes"]

    emergency = latest_policy_state([
        {
            "fetched_at": "2026-06-04T01:00:00+00:00",
            "weekly_used_pct": 5.0,
            "session_used_pct": 2.0,
            "allowed": False,
            "limit_reached": True,
        }
    ])
    assert emergency["policy_mode"] == "emergency"
    assert "codex_not_allowed" in emergency["reason_codes"]


def test_latest_policy_state_unknown_when_latest_usage_is_null():
    state = latest_policy_state([
        {"fetched_at": "2026-06-04T01:00:00+00:00", "weekly_used_pct": None, "session_used_pct": None}
    ])

    assert state["policy_mode"] == "unknown"
    assert state["recommended_default_provider"] is None
    assert "latest_usage_unknown" in state["reason_codes"]
