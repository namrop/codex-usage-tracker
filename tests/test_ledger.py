from __future__ import annotations

from codex_usage_tracker.ledger import append_row


def test_append_row_preserves_unknown_usage_and_availability_fields(tmp_path):
    ledger = tmp_path / "usage.jsonl"
    row = append_row(
        {
            "plan_type": "pro",
            "rate_limit": {
                "allowed": False,
                "limit_reached": True,
                "primary_window": {"used_percent": None, "reset_at": 1780700000},
                "secondary_window": {"reset_at": 1781137839},
            },
            "credits": {"balance": "0", "has_credits": False},
        },
        str(ledger),
    )

    assert row["session_used_pct"] is None
    assert row["weekly_used_pct"] is None
    assert row["allowed"] is False
    assert row["limit_reached"] is True
    assert row["raw_payload_present"] is True
    assert row["unknown_state"] is True
    assert "session_reset_after_seconds" in row
    assert "weekly_reset_after_seconds" in row
    assert "hours_until_session_reset" in row
    assert "hours_until_weekly_reset" in row


def test_append_row_preserves_unknown_weekly_usage_when_secondary_window_is_null(tmp_path):
    ledger = tmp_path / "usage.jsonl"

    row = append_row(
        {
            "plan_type": "pro",
            "rate_limit": {
                "primary_window": {"used_percent": 12.0},
                "secondary_window": None,
            },
        },
        str(ledger),
    )

    assert row["session_used_pct"] == 12.0
    assert row["weekly_used_pct"] is None
    assert row["unknown_state"] is True


def test_append_row_classifies_duration_annotated_primary_as_weekly(tmp_path):
    ledger = tmp_path / "usage.jsonl"
    row = append_row(
        {
            "plan_type": "pro",
            "rate_limit": {
                "primary_window": {
                    "used_percent": 15.0,
                    "limit_window_seconds": 604800,
                    "reset_at": 1784489942,
                },
                "secondary_window": None,
            },
            "additional_rate_limits": [
                {
                    "limit_name": "GPT-5.3-Codex-Spark",
                    "rate_limit": {
                        "primary_window": {
                            "used_percent": 3.0,
                            "limit_window_seconds": 604800,
                            "reset_at": 1784628001,
                        },
                        "secondary_window": None,
                    },
                }
            ],
        },
        str(ledger),
    )

    assert row["session_used_pct"] is None
    assert row["session_reset_at"] is None
    assert row["weekly_used_pct"] == 15.0
    assert row["weekly_reset_at"] == 1784489942
    assert row["spark_session_used_pct"] is None
    assert row["spark_weekly_used_pct"] == 3.0
    assert row["spark_weekly_reset_at"] == 1784628001
    assert row["unknown_state"] is True


def test_append_row_classifies_duration_annotated_five_hour_and_weekly_windows(tmp_path):
    ledger = tmp_path / "usage.jsonl"
    row = append_row(
        {
            "plan_type": "pro",
            "rate_limit": {
                "primary_window": {
                    "used_percent": 8.0,
                    "limit_window_seconds": 18000,
                    "reset_at": 1780700000,
                },
                "secondary_window": {
                    "used_percent": 21.0,
                    "limit_window_seconds": 604800,
                    "reset_at": 1781137839,
                },
            },
        },
        str(ledger),
    )

    assert row["session_used_pct"] == 8.0
    assert row["weekly_used_pct"] == 21.0
    assert row["session_reset_at"] == 1780700000
    assert row["weekly_reset_at"] == 1781137839
    assert row["unknown_state"] is False


def test_append_row_preserves_unknown_usage_when_rate_limit_is_null(tmp_path):
    ledger = tmp_path / "usage.jsonl"

    row = append_row({"plan_type": "pro", "rate_limit": None}, str(ledger))

    assert row["session_used_pct"] is None
    assert row["weekly_used_pct"] is None
    assert row["unknown_state"] is True


def test_append_row_captures_banked_reset_credits(tmp_path):
    ledger = tmp_path / "usage.jsonl"
    row = append_row(
        {
            "plan_type": "pro",
            "rate_limit": {
                "allowed": True,
                "limit_reached": False,
                "primary_window": {"used_percent": 8.0, "reset_at": 1780700000},
                "secondary_window": {"used_percent": 12.0, "reset_at": 1781137839},
            },
            "rate_limit_reset_credits": {"available_count": 2},
        },
        str(ledger),
    )

    assert row["rate_limit_reset_credits_available"] == 2


def test_append_row_normalizes_all_additional_rate_limits_and_keeps_spark_compat_fields(tmp_path):
    ledger = tmp_path / "usage.jsonl"
    row = append_row(
        {
            "plan_type": "pro",
            "rate_limit": {
                "allowed": True,
                "limit_reached": False,
                "primary_window": {"used_percent": 5.0, "reset_at": 1780700000},
                "secondary_window": {"used_percent": 17.0, "reset_at": 1781137839},
            },
            "additional_rate_limits": [
                {
                    "limit_name": "GPT-5.3-Codex-Spark",
                    "rate_limit": {
                        "allowed": True,
                        "limit_reached": False,
                        "primary_window": {"used_percent": 11.0, "reset_at": 1780700100},
                        "secondary_window": {"used_percent": 22.0, "reset_at": 1781137900},
                    },
                },
                {
                    "limit_name": "GPT-5.5-Codex",
                    "rate_limit": {
                        "allowed": False,
                        "limit_reached": True,
                        "primary_window": {"used_percent": 33.0, "reset_at": 1780700200},
                        "secondary_window": {"used_percent": 44.0, "reset_at": 1781138000},
                    },
                },
            ],
        },
        str(ledger),
    )

    normalized = row["additional_rate_limits_normalized"]
    assert [entry["limit_name"] for entry in normalized] == ["GPT-5.3-Codex-Spark", "GPT-5.5-Codex"]
    assert normalized[0]["session_used_pct"] == 11.0
    assert normalized[0]["weekly_used_pct"] == 22.0
    assert normalized[1]["allowed"] is False
    assert normalized[1]["limit_reached"] is True
    assert row["spark_session_used_pct"] == 11.0
    assert row["spark_weekly_used_pct"] == 22.0
    assert row["unknown_state"] is False
