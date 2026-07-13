from __future__ import annotations

import json
import sqlite3
from datetime import datetime

from codex_usage_tracker.dashboard import create_app
from codex_usage_tracker.public_projection import build_public_projection
from codex_usage_tracker.token_correlation import build_token_correlation_rows, resolve_state_db_path


def _epoch(value: str) -> float:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


def test_resolve_state_db_path_uses_explicit_env_then_hermes_home(tmp_path, monkeypatch):
    hermes_home = tmp_path / "profile-home"
    explicit = tmp_path / "custom-state.db"

    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    assert resolve_state_db_path() == str(hermes_home / "state.db")

    monkeypatch.setenv("HERMES_STATE_DB_PATH", str(explicit))
    assert resolve_state_db_path() == str(explicit)
    assert resolve_state_db_path("/cli/state.db") == "/cli/state.db"


def test_resolve_state_db_path_falls_back_to_legacy_home(monkeypatch):
    monkeypatch.delenv("HERMES_HOME", raising=False)
    monkeypatch.delenv("HERMES_STATE_DB_PATH", raising=False)

    assert resolve_state_db_path() == "~/.hermes/state.db"


def _make_event_state_db(path):
    conn = sqlite3.connect(path)
    conn.execute(
        """
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY,
            started_at REAL NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE llm_usage_events (
            timestamp REAL NOT NULL,
            session_id TEXT,
            source TEXT,
            purpose TEXT,
            provider TEXT,
            model TEXT,
            api_call_index INTEGER,
            request_status TEXT,
            record_kind TEXT,
            usage_source TEXT,
            measurement_confidence TEXT,
            input_tokens INTEGER DEFAULT 0,
            cache_read_tokens INTEGER DEFAULT 0,
            cache_write_tokens INTEGER DEFAULT 0,
            output_tokens INTEGER DEFAULT 0,
            reasoning_tokens INTEGER DEFAULT 0
        )
        """
    )
    conn.executemany(
        "INSERT INTO sessions (id, started_at) VALUES (?, ?)",
        [
            ("codex-old", _epoch("2026-06-03T22:00:00Z")),
            ("codex-next", _epoch("2026-06-04T02:05:00Z")),
        ],
    )
    conn.executemany(
        """
        INSERT INTO llm_usage_events (
            timestamp, session_id, source, purpose, provider, model,
            api_call_index, request_status, record_kind, usage_source,
            measurement_confidence, input_tokens, cache_read_tokens,
            cache_write_tokens, output_tokens, reasoning_tokens
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                _epoch("2026-06-04T01:15:00Z"), "codex-old", "discord", "interactive",
                "openai-codex", "gpt-5.6", 1, "success", "api_attempt",
                "provider_reported", "exact", 100, 900, 0, 50, 10,
            ),
            (
                _epoch("2026-06-04T01:30:00Z"), "codex-old", "discord", "interactive",
                "deepseek", "deepseek-v4-pro", 2, "success", "api_attempt",
                "provider_reported", "exact", 9999, 9999, 0, 9999, 0,
            ),
            (
                _epoch("2026-06-04T01:45:00Z"), "codex-old", "cron", "background",
                "openai-codex", "gpt-5.6", 3, "success", "api_attempt",
                "provider_reported", "exact", 40, 60, 0, 10, 0,
            ),
            (
                _epoch("2026-06-04T02:10:00Z"), "codex-next", "gateway", "fallback",
                "openai-codex", "gpt-5.6", 1, "success", "api_attempt",
                "provider_reported", "exact", 300, 700, 100, 100, 30,
            ),
        ],
    )
    conn.commit()
    conn.close()


def _usage_rows():
    return [
        {"fetched_at": "2026-06-04T01:00:00+00:00", "session_used_pct": 0.0, "weekly_used_pct": 0.0},
        {"fetched_at": "2026-06-04T02:00:00+00:00", "session_used_pct": 1.0, "weekly_used_pct": 0.0},
        {"fetched_at": "2026-06-04T03:00:00+00:00", "session_used_pct": 3.0, "weekly_used_pct": 1.0},
    ]


def test_events_are_bucketed_by_attempt_timestamp_and_provider(tmp_path):
    state_db = tmp_path / "state.db"
    _make_event_state_db(state_db)

    rows = build_token_correlation_rows(_usage_rows(), state_db_path=str(state_db))

    first = rows[0]
    assert first["window_start"] == "2026-06-04T01:00:00+00:00"
    assert first["window_end"] == "2026-06-04T02:00:00+00:00"
    assert first["codex_sessions"] == 1
    assert first["api_calls"] == 2
    assert first["input_tokens"] == 140
    assert first["cache_read_tokens"] == 960
    assert first["output_tokens"] == 60
    assert first["reasoning_tokens"] == 10
    assert first["prompt_tokens"] == 1100
    assert first["total_tokens"] == 1160
    assert first["cache_hit_pct"] == 87.3
    assert first["tokens_per_session_pct"] == 1160.0
    assert first["tokens_per_weekly_pct"] is None
    assert first["usage_source"] == "provider_reported"
    assert first["measurement_confidence"] == "exact"

    second = rows[1]
    assert second["codex_sessions"] == 1
    assert second["api_calls"] == 1
    assert second["cache_write_tokens"] == 100
    assert second["total_tokens"] == 1200
    assert second["tokens_per_session_pct"] == 600.0
    assert second["tokens_per_weekly_pct"] == 1200.0


def test_retry_and_failure_rows_are_distinct_exact_attempts(tmp_path):
    state_db = tmp_path / "state.db"
    _make_event_state_db(state_db)
    conn = sqlite3.connect(state_db)
    conn.executemany(
        """
        INSERT INTO llm_usage_events (
            timestamp, session_id, source, purpose, provider, model,
            api_call_index, request_status, record_kind, usage_source,
            measurement_confidence, input_tokens, output_tokens
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (_epoch("2026-06-04T01:20:00Z"), "retry", "cron", "job", "openai-codex", "gpt-5.6", 1, "error", "api_attempt", "provider_reported", "exact", 0, 0),
            (_epoch("2026-06-04T01:21:00Z"), "retry", "cron", "job", "openai-codex", "gpt-5.6", 2, "success", "api_attempt", "provider_reported", "exact", 25, 5),
        ],
    )
    conn.commit()
    conn.close()

    first = build_token_correlation_rows(_usage_rows(), state_db_path=str(state_db))[0]

    assert first["api_calls"] == 4
    assert first["codex_sessions"] == 2
    assert first["input_tokens"] == 165
    assert first["output_tokens"] == 65


def test_historical_aggregate_uses_reconstructed_residual_call_count_and_provenance(tmp_path):
    state_db = tmp_path / "state.db"
    _make_event_state_db(state_db)
    conn = sqlite3.connect(state_db)
    conn.execute(
        """
        INSERT INTO llm_usage_events (
            timestamp, session_id, source, purpose, provider, model,
            api_call_index, request_status, record_kind, usage_source,
            measurement_confidence, input_tokens, cache_read_tokens,
            cache_write_tokens, output_tokens, reasoning_tokens
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            _epoch("2026-06-04T01:10:00Z"), "historical", "migration", "backfill",
            "openai-codex", "gpt-5.5", 4, "historical", "historical_aggregate",
            "reconstructed", "reconstructed", 10, 20, 30, 40, 5,
        ),
    )
    conn.commit()
    conn.close()

    first = build_token_correlation_rows(_usage_rows(), state_db_path=str(state_db))[0]

    assert first["api_calls"] == 6
    assert first["input_tokens"] == 150
    assert first["cache_read_tokens"] == 980
    assert first["cache_write_tokens"] == 30
    assert first["output_tokens"] == 100
    assert first["reasoning_tokens"] == 15
    assert first["total_tokens"] == 1260
    assert first["usage_source"] == "mixed"
    assert first["measurement_confidence"] == "reconstructed"


def test_legacy_state_db_without_event_ledger_returns_zero_totals(tmp_path):
    state_db = tmp_path / "state.db"
    conn = sqlite3.connect(state_db)
    conn.execute(
        "CREATE TABLE sessions (id TEXT, started_at REAL, billing_provider TEXT, api_call_count INTEGER, input_tokens INTEGER)"
    )
    conn.execute(
        "INSERT INTO sessions VALUES (?, ?, ?, ?, ?)",
        ("legacy", _epoch("2026-06-04T01:10:00Z"), "openai-codex", 99, 9999),
    )
    conn.commit()
    conn.close()

    first = build_token_correlation_rows(_usage_rows(), state_db_path=str(state_db))[0]

    assert first["codex_sessions"] == 0
    assert first["api_calls"] == 0
    assert first["total_tokens"] == 0
    assert first["usage_source"] is None
    assert first["measurement_confidence"] is None


def test_build_token_correlation_rows_marks_reset_drops_without_tokens_per_pct(tmp_path):
    state_db = tmp_path / "state.db"
    _make_event_state_db(state_db)
    usage_rows = [
        {"fetched_at": "2026-06-04T01:00:00+00:00", "session_used_pct": 15.0, "weekly_used_pct": 28.0},
        {"fetched_at": "2026-06-04T02:00:00+00:00", "session_used_pct": 0.0, "weekly_used_pct": 0.0},
    ]

    row = build_token_correlation_rows(usage_rows, state_db_path=str(state_db))[0]

    assert row["session_delta_pct"] == -15.0
    assert row["weekly_delta_pct"] == -28.0
    assert row["reset_or_drop"] is True
    assert row["tokens_per_session_pct"] is None
    assert row["tokens_per_weekly_pct"] is None


def _write_usage_ledger(path, rows):
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")


def test_dashboard_and_public_projection_share_event_query(tmp_path, monkeypatch):
    state_db = tmp_path / "state.db"
    _make_event_state_db(state_db)
    monkeypatch.setenv("HERMES_STATE_DB_PATH", str(state_db))
    ledger = tmp_path / "usage.jsonl"
    _write_usage_ledger(ledger, _usage_rows())

    app = create_app(ledger=str(ledger))
    client = app.test_client()
    ledger_payload = client.get("/api/token-ledger").get_json()
    chart_payload = client.get("/api/token-chart").get_json()
    public_payload = build_public_projection(str(ledger), state_db_path=str(state_db))

    oldest_ledger_row = ledger_payload[-1]
    assert oldest_ledger_row["api_calls"] == 2
    assert oldest_ledger_row["total_tokens"] == 1160
    assert chart_payload[0]["api_calls"] == 2
    assert chart_payload[0]["total_tokens"] == 1160
    assert public_payload["rows"][0]["api_calls"] == 2
    assert public_payload["rows"][0]["total_tokens"] == 1160
