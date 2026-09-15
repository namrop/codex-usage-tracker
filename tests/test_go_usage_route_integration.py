"""Real-store endpoint proof for the native Go explanation integration."""
from datetime import datetime, timezone
import json

import pytest
import codex_usage_tracker.dashboard as dashboard
from codex_usage_tracker.canonical_ledger import append_sqlite_facts, canonical_json, normalize_fact
from codex_usage_tracker.quota import _quota

class Clock(datetime):
    @classmethod
    def now(cls, tz=None):
        return cls(2026, 9, 15, 3, 40, 0, tzinfo=tz or timezone.utc)


def event(identity, when, provider="opencode-go"):
    return normalize_fact({
        "fact_type": "usage_event_v1", "schema_version": 1,
        "source_namespace": "private:test", "source_event_id": identity,
        "harness": "hermes", "purpose": "main", "record_kind": "api_attempt",
        "occurred_at": when, "recorded_at": when,
        "usage_source": "provider_reported", "usage_completeness": "complete",
        "measurement_confidence": "exact", "cost_status": "included",
        "provider": provider, "model_requested": "glm-5.3", "model_reported": "glm-5.3",
        "input_tokens": 1000000, "cache_read_tokens": 0, "cache_write_tokens": 0,
        "output_tokens": 0, "reasoning_tokens": 0,
    })


def quota():
    return _quota(namespace="private:go", observation_id="native-1", harness="opencode_go_api",
        observed_at="2026-09-15T03:35:00Z", provider="opencode-go", quota_name="week",
        window_kind="fixed", unit="percent", limit=100, used=100, remaining=0,
        resets_at="2026-09-21T00:00:00Z", x_source_surface="opencode_go_usage_api",
        x_percent_resolution=1, x_provider_state="rate_limited")


def store(path, rows):
    if path.suffix == ".jsonl":
        path.write_text("".join(canonical_json(row) + "\n" for row in rows))
    else:
        append_sqlite_facts(path, rows)


@pytest.mark.parametrize("suffix", [".jsonl", ".sqlite3"])
def test_go_route_reads_only_current_week_provider_and_serializes_safe_payload(tmp_path, monkeypatch, suffix):
    monkeypatch.setattr(dashboard, "datetime", Clock)
    usage, quotas = tmp_path / ("usage" + suffix), tmp_path / ("quota" + suffix)
    store(usage, [event("in", "2026-09-14T00:00:00Z"),
                  event("prior", "2026-09-13T23:59:59Z"),
                  event("future", "2026-09-15T03:40:01Z"),
                  event("other", "2026-09-14T12:00:00Z", "anthropic")])
    store(quotas, [quota()])
    app = dashboard.create_app(ledger=str(tmp_path / "unused.jsonl"),
        unified_usage_ledger=str(usage), quota_ledger=str(quotas))
    response = app.test_client().get("/api/opencode-go-usage")
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["native"]["percent"] == 100
    assert len(payload["models"]) == 1
    assert payload["models"][0]["event_count"] == 1
    assert payload["models"][0]["total_tokens"] == 1000000
    text = json.dumps(payload)
    assert "private:test" not in text and "private:go" not in text
    assert str(tmp_path) not in text


def test_go_route_reports_missing_config_without_invented_usage(tmp_path, monkeypatch):
    monkeypatch.delenv("UNIFIED_USAGE_LEDGER_PATH", raising=False)
    monkeypatch.delenv("QUOTA_LEDGER_PATH", raising=False)
    app = dashboard.create_app(ledger=str(tmp_path / "unused.jsonl"))
    response = app.test_client().get("/api/opencode-go-usage")
    assert response.status_code == 404
    assert response.is_json


def test_go_route_preserves_row_limit_error(tmp_path, monkeypatch):
    monkeypatch.setattr(dashboard, "datetime", Clock)
    usage, quotas = tmp_path / "usage.jsonl", tmp_path / "quota.jsonl"
    store(usage, [event("a", "2026-09-14T00:00:00Z"), event("b", "2026-09-14T00:00:01Z")])
    store(quotas, [quota()])
    monkeypatch.setattr(dashboard, "_PRIVATE_API_MAX_ROWS", 1)
    app = dashboard.create_app(ledger=str(tmp_path / "unused.jsonl"),
        unified_usage_ledger=str(usage), quota_ledger=str(quotas))
    response = app.test_client().get("/api/opencode-go-usage")
    assert response.status_code == 413
    assert response.get_json()["error"] == "ledger query exceeds private API row limit"
