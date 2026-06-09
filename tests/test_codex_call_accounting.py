from __future__ import annotations

from codex_usage_tracker.codex_call_accounting import build_codex_accounting_rows


def test_build_codex_accounting_rows_labels_hourly_correlation_source(tmp_path):
    rows = build_codex_accounting_rows(
        [
            {"fetched_at": "2026-06-04T01:00:00+00:00", "session_used_pct": 0.0, "weekly_used_pct": 0.0},
            {"fetched_at": "2026-06-04T02:00:00+00:00", "session_used_pct": 1.0, "weekly_used_pct": 0.5},
        ],
        state_db_path=str(tmp_path / "missing.db"),
    )

    assert len(rows) == 1
    row = rows[0]
    assert row["id"].startswith("codex-hourly-")
    assert row["started_at"] == "2026-06-04T01:00:00+00:00"
    assert row["completed_at"] == "2026-06-04T02:00:00+00:00"
    assert row["provider"] == "openai-codex"
    assert row["source"] == "hourly_correlation"
    assert row["quota_session_delta_pct"] == 1.0
    assert row["quota_weekly_delta_pct"] == 0.5
    assert row["latency_ms"] is None
    assert row["error_class"] is None
    assert "billed_usd" not in row
