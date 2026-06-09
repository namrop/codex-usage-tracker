from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

from codex_usage_tracker.dashboard import create_app
from codex_usage_tracker.token_correlation import build_token_correlation_rows


def _epoch(value: str) -> float:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


def _make_state_db(path):
    conn = sqlite3.connect(path)
    conn.execute(
        """
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY,
            started_at REAL NOT NULL,
            billing_provider TEXT,
            model TEXT,
            api_call_count INTEGER DEFAULT 0,
            input_tokens INTEGER DEFAULT 0,
            cache_read_tokens INTEGER DEFAULT 0,
            cache_write_tokens INTEGER DEFAULT 0,
            output_tokens INTEGER DEFAULT 0,
            reasoning_tokens INTEGER DEFAULT 0
        )
        """
    )
    conn.executemany(
        """
        INSERT INTO sessions (
            id, started_at, billing_provider, model, api_call_count,
            input_tokens, cache_read_tokens, cache_write_tokens, output_tokens, reasoning_tokens
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            ("codex-a", _epoch("2026-06-04T01:15:00Z"), "openai-codex", "gpt-5.5", 2, 100, 900, 0, 50, 10),
            ("codex-b", _epoch("2026-06-04T01:45:00Z"), "openai-codex", "gpt-5.5", 1, 40, 60, 0, 10, 0),
            ("other-provider", _epoch("2026-06-04T01:30:00Z"), "deepseek", "deepseek-v4-pro", 9, 9999, 9999, 0, 9999, 0),
            ("codex-next", _epoch("2026-06-04T02:10:00Z"), "openai-codex", "gpt-5.5", 3, 300, 700, 100, 100, 30),
        ],
    )
    conn.commit()
    conn.close()


def test_build_token_correlation_rows_maps_usage_delta_to_previous_activity_window(tmp_path):
    state_db = tmp_path / "state.db"
    _make_state_db(state_db)
    usage_rows = [
        {"fetched_at": "2026-06-04T01:00:00+00:00", "session_used_pct": 0.0, "weekly_used_pct": 0.0},
        {"fetched_at": "2026-06-04T02:00:00+00:00", "session_used_pct": 1.0, "weekly_used_pct": 0.0},
        {"fetched_at": "2026-06-04T03:00:00+00:00", "session_used_pct": 3.0, "weekly_used_pct": 1.0},
    ]

    rows = build_token_correlation_rows(usage_rows, state_db_path=str(state_db))

    assert len(rows) == 2
    first = rows[0]
    assert first["window_start"] == "2026-06-04T01:00:00+00:00"
    assert first["window_end"] == "2026-06-04T02:00:00+00:00"
    assert first["session_delta_pct"] == 1.0
    assert first["weekly_delta_pct"] == 0.0
    assert first["codex_sessions"] == 2
    assert first["api_calls"] == 3
    assert first["input_tokens"] == 140
    assert first["cache_read_tokens"] == 960
    assert first["output_tokens"] == 60
    assert first["reasoning_tokens"] == 10
    assert first["prompt_tokens"] == 1100
    assert first["total_tokens"] == 1170
    assert first["cache_hit_pct"] == 87.3
    assert first["tokens_per_session_pct"] == 1170.0
    assert first["tokens_per_weekly_pct"] is None

    second = rows[1]
    assert second["window_start"] == "2026-06-04T02:00:00+00:00"
    assert second["session_delta_pct"] == 2.0
    assert second["weekly_delta_pct"] == 1.0
    assert second["cache_write_tokens"] == 100
    assert second["tokens_per_session_pct"] == 615.0
    assert second["tokens_per_weekly_pct"] == 1230.0


def test_build_token_correlation_rows_marks_reset_drops_without_tokens_per_pct(tmp_path):
    state_db = tmp_path / "state.db"
    _make_state_db(state_db)
    usage_rows = [
        {"fetched_at": "2026-06-04T01:00:00+00:00", "session_used_pct": 15.0, "weekly_used_pct": 28.0},
        {"fetched_at": "2026-06-04T02:00:00+00:00", "session_used_pct": 0.0, "weekly_used_pct": 0.0},
    ]

    rows = build_token_correlation_rows(usage_rows, state_db_path=str(state_db))

    assert rows[0]["session_delta_pct"] == -15.0
    assert rows[0]["weekly_delta_pct"] == -28.0
    assert rows[0]["reset_or_drop"] is True
    assert rows[0]["tokens_per_session_pct"] is None
    assert rows[0]["tokens_per_weekly_pct"] is None


def _write_usage_ledger(path, rows):
    path.write_text(
        "\n".join(json.dumps(row) for row in rows) + "\n",
        encoding="utf-8",
    )


def test_token_ledger_api_returns_derived_rows(tmp_path, monkeypatch):
    state_db = tmp_path / "state.db"
    _make_state_db(state_db)
    monkeypatch.setenv("HERMES_STATE_DB_PATH", str(state_db))
    ledger = tmp_path / "usage.jsonl"
    _write_usage_ledger(
        ledger,
        [
            {"fetched_at": "2026-06-04T01:00:00+00:00", "session_used_pct": 0.0, "weekly_used_pct": 0.0},
            {"fetched_at": "2026-06-04T02:00:00+00:00", "session_used_pct": 1.0, "weekly_used_pct": 0.0},
        ],
    )

    app = create_app(ledger=str(ledger))
    response = app.test_client().get("/api/token-ledger")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload[0]["window_start"] == "2026-06-04T01:00:00+00:00"
    assert payload[0]["api_calls"] == 3
    assert payload[0]["session_delta_pct"] == 1.0


def test_token_chart_api_returns_chronological_usage_and_token_series(tmp_path, monkeypatch):
    state_db = tmp_path / "state.db"
    _make_state_db(state_db)
    monkeypatch.setenv("HERMES_STATE_DB_PATH", str(state_db))
    ledger = tmp_path / "usage.jsonl"
    _write_usage_ledger(
        ledger,
        [
            {"fetched_at": "2026-06-04T01:00:00+00:00", "session_used_pct": 0.0, "weekly_used_pct": 0.0},
            {"fetched_at": "2026-06-04T02:00:00+00:00", "session_used_pct": 1.0, "weekly_used_pct": 0.0},
            {"fetched_at": "2026-06-04T03:00:00+00:00", "session_used_pct": 3.0, "weekly_used_pct": 1.0},
        ],
    )

    app = create_app(ledger=str(ledger))
    response = app.test_client().get("/api/token-chart")

    assert response.status_code == 200
    payload = response.get_json()
    assert [row["window_start"] for row in payload] == [
        "2026-06-04T01:00:00+00:00",
        "2026-06-04T02:00:00+00:00",
    ]
    assert payload[0]["session_used_pct"] == 1.0
    assert payload[0]["weekly_used_pct"] == 0.0
    assert payload[0]["session_delta_pct"] == 1.0
    assert payload[0]["total_tokens"] == 1170
    assert payload[0]["noncached_prompt_tokens"] == 140
    assert payload[1]["session_used_pct"] == 3.0
    assert payload[1]["weekly_delta_pct"] == 1.0
    assert payload[1]["cache_read_tokens"] == 700
