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
    conn.execute(
        """
        INSERT INTO llm_usage_events (
            timestamp, session_id, provider, model, api_call_index, record_kind,
            usage_source, measurement_confidence, input_tokens, cache_read_tokens,
            cache_write_tokens, output_tokens, reasoning_tokens
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (_epoch("2026-06-04T01:15:00Z"), "codex-a", "openai-codex", "gpt-5.5", 1, "api_attempt", "provider_reported", "exact", 100, 900, 0, 50, 10),
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
    assert payload["rows"][0]["total_tokens"] == 1050
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


def _fetch_args(tmp_path, ledger):
    return argparse.Namespace(
        atrium_root=str(tmp_path),
        ledger=str(ledger),
        public_projection=None,
        public_projection_source="test-public",
        public_projection_limit=168,
        no_public_projection=False,
    )


def test_cmd_fetch_renders_unknown_weekly_usage_when_secondary_window_is_null(tmp_path, monkeypatch, capsys):
    ledger = tmp_path / "usage.jsonl"
    monkeypatch.setattr(
        cli,
        "fetch_usage",
        lambda: {
            "plan_type": "pro",
            "rate_limit": {
                "primary_window": {"used_percent": 12.0},
                "secondary_window": None,
            },
        },
    )

    assert cli.cmd_fetch(_fetch_args(tmp_path, ledger)) == 0

    row = json.loads(ledger.read_text(encoding="utf-8"))
    assert row["weekly_used_pct"] is None
    assert "weekly_used_pct: None" in capsys.readouterr().out


def test_cmd_fetch_renders_duration_annotated_primary_as_weekly(tmp_path, monkeypatch, capsys):
    ledger = tmp_path / "usage.jsonl"
    monkeypatch.setattr(
        cli,
        "fetch_usage",
        lambda: {
            "plan_type": "pro",
            "rate_limit": {
                "primary_window": {"used_percent": 15.0, "limit_window_seconds": 604800},
                "secondary_window": None,
            },
        },
    )

    assert cli.cmd_fetch(_fetch_args(tmp_path, ledger)) == 0

    row = json.loads(ledger.read_text(encoding="utf-8"))
    assert row["session_used_pct"] is None
    assert row["weekly_used_pct"] == 15.0
    output = capsys.readouterr().out
    assert "session_used_pct: None" in output
    assert "weekly_used_pct: 15.0" in output


def test_cmd_fetch_renders_unknown_usage_when_rate_limit_is_null(tmp_path, monkeypatch, capsys):
    ledger = tmp_path / "usage.jsonl"
    monkeypatch.setattr(cli, "fetch_usage", lambda: {"plan_type": "pro", "rate_limit": None})

    assert cli.cmd_fetch(_fetch_args(tmp_path, ledger)) == 0

    row = json.loads(ledger.read_text(encoding="utf-8"))
    assert row["session_used_pct"] is None
    assert row["weekly_used_pct"] is None
    output = capsys.readouterr().out
    assert "session_used_pct: None" in output
    assert "weekly_used_pct: None" in output


def test_cmd_fetch_reports_summary_failure_after_successful_append_separately(tmp_path, monkeypatch, capsys):
    ledger = tmp_path / "usage.jsonl"
    monkeypatch.setattr(cli, "fetch_usage", lambda: {"plan_type": "pro", "rate_limit": None})
    monkeypatch.setattr(cli, "_print_summary", lambda payload: (_ for _ in ()).throw(RuntimeError("summary boom")))

    assert cli.cmd_fetch(_fetch_args(tmp_path, ledger)) == 1

    assert len(ledger.read_text(encoding="utf-8").splitlines()) == 1
    stderr = capsys.readouterr().err
    assert "Failed to render usage summary: summary boom" in stderr
    assert "append" not in stderr.lower()


def test_cmd_fetch_stops_after_append_failure_and_reports_that_phase(tmp_path, monkeypatch, capsys):
    ledger = tmp_path / "usage.jsonl"
    monkeypatch.setattr(cli, "fetch_usage", lambda: {"plan_type": "pro", "rate_limit": None})
    monkeypatch.setattr(cli, "append_row", lambda payload, path: (_ for _ in ()).throw(OSError("disk boom")))
    monkeypatch.setattr(
        cli,
        "_write_public_projection_for_args",
        lambda args, path: (_ for _ in ()).throw(AssertionError("projection must not run")),
    )

    assert cli.cmd_fetch(_fetch_args(tmp_path, ledger)) == 1

    stderr = capsys.readouterr().err
    assert "Failed to append ledger row: disk boom" in stderr
    assert "projection" not in stderr.lower()


def test_cmd_fetch_reports_projection_failure_without_relabeling_the_append(tmp_path, monkeypatch, capsys):
    ledger = tmp_path / "usage.jsonl"
    summary_calls = []
    monkeypatch.setattr(cli, "fetch_usage", lambda: {"plan_type": "pro", "rate_limit": None})
    monkeypatch.setattr(
        cli,
        "_write_public_projection_for_args",
        lambda args, path: (_ for _ in ()).throw(RuntimeError("projection boom")),
    )
    monkeypatch.setattr(cli, "_print_summary", lambda payload: summary_calls.append(payload))

    assert cli.cmd_fetch(_fetch_args(tmp_path, ledger)) == 1

    assert len(ledger.read_text(encoding="utf-8").splitlines()) == 1
    assert len(summary_calls) == 1
    stderr = capsys.readouterr().err
    assert "Failed to write public projection: projection boom" in stderr
    assert "append" not in stderr.lower()
