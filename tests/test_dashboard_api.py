from __future__ import annotations

import json

from codex_usage_tracker.dashboard import create_app


def _write_usage_ledger(path, rows):
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")


def test_dashboard_exposes_policy_budget_capability_and_scaffold_routes(tmp_path, monkeypatch):
    ledger = tmp_path / "usage.jsonl"
    atrium_root = tmp_path / "atrium"
    _write_usage_ledger(
        ledger,
        [
            {
                "fetched_at": "2026-06-04T01:00:00+00:00",
                "weekly_used_pct": 72.0,
                "session_used_pct": 5.0,
                "weekly_reset_at": 1781137839,
                "allowed": True,
                "limit_reached": False,
            },
            {
                "fetched_at": "2026-06-04T02:00:00+00:00",
                "weekly_used_pct": 74.0,
                "session_used_pct": 7.0,
                "weekly_reset_at": 1781137839,
                "allowed": True,
                "limit_reached": False,
            },
        ],
    )
    spend_ledger = atrium_root / "12_runtime/ledgers/model_routing/direct_provider_spend_ledger.jsonl"
    spend_ledger.parent.mkdir(parents=True)
    spend_ledger.write_text(
        json.dumps({"provider": "deepseek", "model": "deepseek-v4-pro", "billed_usd": 1.25, "started_at": "2026-06-04T02:30:00+00:00"}) + "\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_STATE_DB_PATH", str(tmp_path / "missing.db"))

    client = create_app(atrium_root=str(atrium_root), ledger=str(ledger)).test_client()

    policy = client.get("/api/policy-state")
    assert policy.status_code == 200
    assert policy.get_json()["policy_mode"] == "caution"

    budget = client.get("/api/budget-state")
    assert budget.status_code == 200
    assert budget.get_json()["direct_provider_spend_usd"] == 1.25

    capability = client.get("/api/capability-matrix")
    assert capability.status_code == 200
    assert any(row["provider"] == "deepseek" for row in capability.get_json())

    provider_spend = client.get("/api/provider-spend")
    assert provider_spend.status_code == 200
    assert provider_spend.get_json()["total_billed_usd"] == 1.25

    accounting = client.get("/api/codex-call-accounting")
    assert accounting.status_code == 200
    assert accounting.get_json()[0]["source"] == "hourly_correlation"

    assert client.get("/api/routing-decisions").status_code == 200
    assert client.get("/api/task-outcomes").status_code == 200
    assert client.get("/api/backtests/latest").status_code == 200


def test_dashboard_summary_exposes_banked_reset_credits(tmp_path, monkeypatch):
    ledger = tmp_path / "usage.jsonl"
    _write_usage_ledger(
        ledger,
        [
            {
                "fetched_at": "2026-06-04T02:00:00+00:00",
                "weekly_used_pct": 74.0,
                "session_used_pct": 7.0,
                "weekly_reset_at": 1781137839,
                "allowed": True,
                "limit_reached": False,
                "raw_payload": {"rate_limit_reset_credits": {"available_count": 3}},
            },
        ],
    )
    monkeypatch.setenv("HERMES_STATE_DB_PATH", str(tmp_path / "missing.db"))

    client = create_app(atrium_root=str(tmp_path / "atrium"), ledger=str(ledger)).test_client()

    summary = client.get("/api/summary")
    assert summary.status_code == 200
    assert summary.get_json()["rate_limit_reset_credits_available"] == 3

    data = client.get("/api/data")
    assert data.status_code == 200
    assert data.get_json()[0]["rate_limit_reset_credits_available"] == 3

    page = client.get("/")
    assert page.status_code == 200
    assert b"Banked resets" in page.data
    assert b"manual" in page.data
