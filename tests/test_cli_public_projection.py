from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime

from codex_usage_tracker import cli


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
    conn.execute(
        """
        INSERT INTO sessions (
            id, started_at, billing_provider, model, api_call_count,
            input_tokens, cache_read_tokens, cache_write_tokens, output_tokens, reasoning_tokens
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        ("codex-a", _epoch("2026-06-04T01:15:00Z"), "openai-codex", "gpt-5.5", 2, 100, 900, 0, 50, 10),
    )
    conn.commit()
    conn.close()


def _write_usage_ledger(path):
    path.write_text(
        "\n".join(
            json.dumps(row)
            for row in [
                {"fetched_at": "2026-06-04T01:00:00+00:00", "session_used_pct": 0.0, "weekly_used_pct": 0.0},
                {"fetched_at": "2026-06-04T02:00:00+00:00", "session_used_pct": 1.0, "weekly_used_pct": 0.0},
            ]
        )
        + "\n",
        encoding="utf-8",
    )


def test_cmd_write_public_projection_uses_default_neighbor_path(tmp_path, monkeypatch, capsys):
    ledger = tmp_path / "codex_usage_ledger.jsonl"
    state_db = tmp_path / "state.db"
    _write_usage_ledger(ledger)
    _make_state_db(state_db)
    monkeypatch.setenv("HERMES_STATE_DB_PATH", str(state_db))

    args = argparse.Namespace(
        atrium_root=str(tmp_path),
        ledger=str(ledger),
        public_projection=None,
        public_projection_source="test-public",
        public_projection_limit=168,
    )

    assert cli.cmd_write_public_projection(args) == 0

    output = ledger.with_name("codex_token_chart_public.json")
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["source"] == "test-public"
    assert payload["rows"][0]["total_tokens"] == 1060
    assert str(output) in capsys.readouterr().out


def test_cmd_fetch_does_not_write_public_projection_without_explicit_path(tmp_path, monkeypatch):
    ledger = tmp_path / "codex_usage_ledger.jsonl"
    state_db = tmp_path / "state.db"
    _write_usage_ledger(ledger)
    _make_state_db(state_db)
    monkeypatch.setenv("HERMES_STATE_DB_PATH", str(state_db))
    monkeypatch.setattr(
        cli,
        "fetch_usage",
        lambda: {
            "plan_type": "plus",
            "rate_limit": {
                "primary_window": {"used_percent": 2.0},
                "secondary_window": {"used_percent": 1.0},
            },
        },
    )
    args = argparse.Namespace(
        atrium_root=str(tmp_path),
        ledger=str(ledger),
        public_projection=None,
        public_projection_source="test-public",
        public_projection_limit=168,
        no_public_projection=False,
    )

    assert cli.cmd_fetch(args) == 0

    assert not ledger.with_name("codex_token_chart_public.json").exists()


def test_cmd_fetch_writes_public_projection_after_private_ledger_append_when_configured(tmp_path, monkeypatch):
    ledger = tmp_path / "codex_usage_ledger.jsonl"
    projection = tmp_path / "public.json"
    state_db = tmp_path / "state.db"
    _write_usage_ledger(ledger)
    _make_state_db(state_db)
    monkeypatch.setenv("HERMES_STATE_DB_PATH", str(state_db))
    monkeypatch.setattr(
        cli,
        "fetch_usage",
        lambda: {
            "plan_type": "plus",
            "rate_limit": {
                "primary_window": {"used_percent": 2.0},
                "secondary_window": {"used_percent": 1.0},
            },
        },
    )
    args = argparse.Namespace(
        atrium_root=str(tmp_path),
        ledger=str(ledger),
        public_projection=str(projection),
        public_projection_source="test-public",
        public_projection_limit=168,
        no_public_projection=False,
    )

    assert cli.cmd_fetch(args) == 0

    payload = json.loads(projection.read_text(encoding="utf-8"))
    assert payload["source"] == "test-public"
    assert "raw_payload" not in json.dumps(payload)
    assert len(payload["rows"]) >= 1
