from __future__ import annotations

import json
import sqlite3
from datetime import datetime

from codex_usage_tracker.public_projection import (
    _read_usage_rows,
    build_public_projection,
    default_public_projection_path,
    suppress_gap_spikes,
    write_public_projection,
)


def _epoch(value: str) -> float:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


def _make_state_db(path):
    conn = sqlite3.connect(path)
    conn.execute(
        """
        CREATE TABLE llm_usage_events (
            timestamp REAL NOT NULL,
            session_id TEXT,
            provider TEXT,
            model TEXT,
            api_call_index INTEGER,
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
        """
        INSERT INTO llm_usage_events (
            timestamp, session_id, provider, model, api_call_index, record_kind,
            usage_source, measurement_confidence, input_tokens, cache_read_tokens,
            cache_write_tokens, output_tokens, reasoning_tokens
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (_epoch("2026-06-04T01:15:00Z"), "codex-a", "openai-codex", "gpt-5.5", 1, "api_attempt", "provider_reported", "exact", 100, 900, 0, 50, 10),
            (_epoch("2026-06-04T01:30:00Z"), "other-provider", "deepseek", "deepseek-v4-pro", 1, "api_attempt", "provider_reported", "exact", 9999, 9999, 0, 9999, 0),
            (_epoch("2026-06-04T02:10:00Z"), "codex-b", "openai-codex", "gpt-5.5", 3, "historical_aggregate", "reconstructed", "reconstructed", 300, 700, 100, 100, 30),
        ],
    )
    conn.commit()
    conn.close()


def _write_usage_ledger(path, rows):
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")


def test_public_projection_reader_reconciles_retained_weekly_only_payloads(tmp_path):
    ledger = tmp_path / "codex_usage_ledger.jsonl"
    _write_usage_ledger(
        ledger,
        [
            {
                "fetched_at": "2026-07-14T10:00:00Z",
                "session_used_pct": 15,
                "weekly_used_pct": None,
                "session_reset_at": 1784489942,
                "weekly_reset_at": None,
                "raw_payload": {
                    "rate_limit": {
                        "primary_window": {
                            "used_percent": 15,
                            "limit_window_seconds": 604800,
                            "reset_at": 1784489942,
                        },
                        "secondary_window": None,
                    }
                },
            }
        ],
    )

    row = _read_usage_rows(str(ledger))[0]
    assert row["session_used_pct"] is None
    assert row["session_reset_at"] is None
    assert row["weekly_used_pct"] == 15.0
    assert row["weekly_reset_at"] == 1784489942


def test_build_public_projection_exposes_only_derived_chart_payload(tmp_path):
    ledger = tmp_path / "codex_usage_ledger.jsonl"
    state_db = tmp_path / "state.db"
    _make_state_db(state_db)
    _write_usage_ledger(
        ledger,
        [
            {
                "id": "private-a",
                "fetched_at": "2026-06-04T01:00:00+00:00",
                "session_used_pct": 0.0,
                "weekly_used_pct": 0.0,
                "raw_payload": {"secretish": "do-not-project"},
                "credits_balance": "private-wallet-ish",
            },
            {
                "id": "private-b",
                "fetched_at": "2026-06-04T02:00:00+00:00",
                "session_used_pct": 1.0,
                "weekly_used_pct": 0.0,
                "raw_payload": {"secretish": "do-not-project"},
            },
            {
                "id": "private-c",
                "fetched_at": "2026-06-04T03:00:00+00:00",
                "session_used_pct": 3.0,
                "weekly_used_pct": 1.0,
                "raw_payload": {"secretish": "do-not-project"},
            },
        ],
    )

    payload = build_public_projection(str(ledger), state_db_path=str(state_db), limit=2, source="test-public")

    assert payload["source"] == "test-public"
    assert len(payload["rows"]) == 2
    assert payload["rows"][0]["window_start"] == "2026-06-04T01:00:00+00:00"
    assert payload["rows"][0]["api_calls"] == 1
    assert payload["rows"][0]["total_tokens"] == 1050
    assert payload["rows"][0]["noncached_prompt_pct"] == 10.0
    assert payload["rows"][0]["usage_source"] == "provider_reported"
    assert payload["rows"][0]["measurement_confidence"] == "exact"
    assert payload["summary"] == {
        "updated_at": "2026-06-04T03:00:00+00:00",
        "window_count": 2,
        "total_tokens": 2250,
        "api_calls": 4,
        "latest_total_tokens": 1200,
        "latest_cache_hit_pct": 63.6,
    }
    serialized = json.dumps(payload)
    assert "raw_payload" not in serialized
    assert "private-wallet-ish" not in serialized
    assert "do-not-project" not in serialized
    for forbidden in (
        "session_used_pct", "weekly_used_pct", "session_window", "weekly_window",
        "tokens_per_session_pct", "tokens_per_weekly_pct", "reset_or_drop",
    ):
        assert forbidden not in serialized


def test_write_public_projection_writes_pretty_json_next_to_ledger_by_default(tmp_path):
    ledger = tmp_path / "codex_usage_ledger.jsonl"
    state_db = tmp_path / "state.db"
    _make_state_db(state_db)
    _write_usage_ledger(
        ledger,
        [
            {"fetched_at": "2026-06-04T01:00:00+00:00", "session_used_pct": 0.0, "weekly_used_pct": 0.0},
            {"fetched_at": "2026-06-04T02:00:00+00:00", "session_used_pct": 1.0, "weekly_used_pct": 0.0},
        ],
    )

    output_path = write_public_projection(str(ledger), state_db_path=str(state_db), source="test-public")

    assert output_path == default_public_projection_path(str(ledger))
    payload = json.loads(output_path.read_text(encoding="utf-8"))
    assert payload["source"] == "test-public"
    assert payload["rows"][0]["total_tokens"] == 1050
    assert output_path.name == "codex_token_chart_public.json"


def test_public_projection_suppresses_multi_hour_sampling_gap_spikes():
    rows = [
        {
            "window_start": "2026-06-12T22:00:00+00:00",
            "window_end": "2026-06-20T01:00:00+00:00",
            "span_hours": 171.0,
            "codex_sessions": 593,
            "api_calls": 19617,
            "input_tokens": 138993064,
            "cache_read_tokens": 1950931456,
            "cache_write_tokens": 0,
            "output_tokens": 6775923,
            "reasoning_tokens": 1965825,
            "prompt_tokens": 2089924520,
            "total_tokens": 2098666268,
            "cache_hit_pct": 93.3,
            "noncached_prompt_pct": 6.7,
            "tokens_per_session_pct": None,
            "tokens_per_weekly_pct": None,
            "models": ["gpt-5.5"],
        },
        {
            "window_start": "2026-06-20T01:00:00+00:00",
            "window_end": "2026-06-20T02:00:00+00:00",
            "span_hours": 1.0,
            "codex_sessions": 9,
            "api_calls": 347,
            "input_tokens": 4156933,
            "cache_read_tokens": 39144448,
            "cache_write_tokens": 0,
            "output_tokens": 158038,
            "reasoning_tokens": 34293,
            "prompt_tokens": 43301381,
            "total_tokens": 43493712,
            "cache_hit_pct": 90.4,
            "noncached_prompt_pct": 9.6,
            "tokens_per_session_pct": 3953974.0,
            "tokens_per_weekly_pct": 21746856.0,
            "models": ["gpt-5.5"],
        },
    ]

    sanitized = suppress_gap_spikes(rows)

    assert sanitized[0]["data_gap"] is True
    assert sanitized[0]["span_hours"] == 171.0
    assert sanitized[0]["total_tokens"] == 0
    assert sanitized[0]["api_calls"] == 0
    assert sanitized[0]["codex_sessions"] == 0
    assert sanitized[0]["cache_hit_pct"] is None
    assert sanitized[0]["models"] == []
    assert sanitized[1]["data_gap"] is False
    assert sanitized[1]["total_tokens"] == 43493712
    assert sanitized[1]["api_calls"] == 347
