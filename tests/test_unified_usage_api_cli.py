from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone

import pytest

from codex_usage_tracker.canonical_ledger import (
    append_sqlite_facts,
    canonical_json,
    read_facts,
    read_sqlite_facts,
)
import codex_usage_tracker.dashboard as dashboard_module
from codex_usage_tracker.cli import main
from codex_usage_tracker.dashboard import create_app
from codex_usage_tracker.provider_spend import latest_budget_state
from codex_usage_tracker.quota import (
    capture_claude_code_usage_screen,
    collect_claude_code_quota,
    collect_deepseek_quota,
    collect_openrouter_quota,
)
from codex_usage_tracker.collector import collect_all


def _usage_fact(source_event_id="u-1"):
    return {
        "fact_type": "usage_event_v1",
        "schema_version": 1,
        "source_namespace": "test:collect-all",
        "source_event_id": source_event_id,
        "harness": "test",
        "purpose": "main",
        "record_kind": "api_attempt",
        "occurred_at": "2026-07-12T12:00:00Z",
        "recorded_at": "2026-07-12T12:00:01Z",
        "usage_source": "provider_reported",
        "usage_completeness": "complete",
        "measurement_confidence": "exact",
        "cost_status": "included",
    }


def test_live_provider_fetches_are_structured_and_secret_free(monkeypatch):
    class Response:
        def __init__(self, data): self.data=data
        def raise_for_status(self): pass
        def json(self): return self.data
    class Client:
        def __init__(self, *a, **kw): pass
        def __enter__(self): return self
        def __exit__(self,*a): pass
        def get(self,url,headers):
            if url.endswith("credits"):
                return Response({"data":{"total_credits":100,"total_usage":25}})
            if url.endswith("key"):
                return Response({"data":{"limit":50,"limit_remaining":40,"usage":10}})
            return Response({"balance_infos":[{"currency":"USD","total_balance":"8.25","granted_balance":"1"}],"is_available":True})
    monkeypatch.setattr("codex_usage_tracker.quota.httpx.Client",Client)
    opened=collect_openrouter_quota("key", "ns:or", observed_at="2026-07-12T12:00:00Z")
    deep=collect_deepseek_quota("key", "ns:ds", observed_at="2026-07-12T12:00:00Z")
    assert {r["quota_name"] for r in opened} >= {"credit_balance","api_key_quota"}
    assert deep[0]["remaining_value"]=="8.25"
    assert '"key"' not in json.dumps(opened+deep).lower()


def test_claude_quota_comes_from_claude_code_usage_screen(tmp_path, monkeypatch):
    screen = """
   Current session
   █████████████████                                  34% used
   Resets 3:40pm (America/New_York)

   Current week (all models)
   ███████████████████████▌                           47% used
   Resets Jul 16, 8pm (America/New_York)

   Current week (Fable)
   ████████████████████████████████████████▌          81% used
   Resets Jul 16, 8pm (America/New_York)
"""
    captured = {}

    def fake_capture(**kwargs):
        captured.update(kwargs)
        return screen

    monkeypatch.setattr("codex_usage_tracker.quota.capture_claude_code_usage_screen", fake_capture)
    rows = collect_claude_code_quota(
        "ns:claude-code",
        claude_command="/run/current-system/sw/bin/claude",
        probe_dir=tmp_path,
        observed_at="2026-07-12T12:00:00Z",
    )
    assert captured["claude_command"] == "/run/current-system/sw/bin/claude"
    assert captured["probe_dir"] == tmp_path
    assert {row["quota_name"] for row in rows} == {"five_hour", "seven_day", "seven_day_fable"}
    session = next(row for row in rows if row["quota_name"] == "five_hour")
    weekly = next(row for row in rows if row["quota_name"] == "seven_day")
    assert session["used_value"] == "34" and session["remaining_value"] == "66"
    assert session["resets_at"] == "2026-07-12T19:40:00Z"
    assert weekly["used_value"] == "47" and weekly["resets_at"] == "2026-07-17T00:00:00Z"
    assert all(row["harness"] == "claude_code" and row["measurement_confidence"] == "exact" for row in rows)


def test_claude_usage_parser_rejects_partial_cross_section_render(tmp_path, monkeypatch):
    partial = """
   Current session
   Loading limits...

   Current week (all models)
   ███████████████████████▌                           47% used
   Resets Jul 16, 8pm (America/New_York)

   Current week (Fable)
   ████████████████████████████████████████▌          81% used
   Resets Jul 16, 8pm (America/New_York)
"""
    monkeypatch.setattr(
        "codex_usage_tracker.quota.capture_claude_code_usage_screen",
        lambda **kwargs: partial,
    )
    with pytest.raises(ValueError, match="Current session"):
        collect_claude_code_quota(
            "ns:claude-code",
            probe_dir=tmp_path,
            observed_at="2026-07-12T12:00:00Z",
        )


def test_claude_usage_probe_rejects_symlinked_lock(tmp_path):
    probe = tmp_path / "probe"
    probe.mkdir()
    victim = tmp_path / "victim"
    victim.write_text("do not chmod", encoding="utf-8")
    lock = tmp_path / ".probe.lock"
    lock.symlink_to(victim)
    with pytest.raises(ValueError, match="lock must not be a symlink"):
        capture_claude_code_usage_screen(probe_dir=probe, timeout=0.1)


def test_claude_usage_probe_refuses_nonempty_working_directory(tmp_path):
    (tmp_path / "untrusted.txt").write_text("data", encoding="utf-8")
    with pytest.raises(ValueError, match="must be empty"):
        capture_claude_code_usage_screen(probe_dir=tmp_path, timeout=0.1)


def test_unified_private_aggregate_endpoints_and_filters(tmp_path):
    usage=tmp_path/"usage.jsonl"; quotas=tmp_path/"quota.jsonl"
    base={"fact_type":"usage_event_v1","schema_version":1,"source_namespace":"n","harness":"hermes","surface":None,"purpose":"main","record_kind":"api_attempt","occurred_at":"2026-07-12T12:00:00Z","recorded_at":"2026-07-12T12:00:00Z","session_id":None,"logical_call_id":None,"attempt_no":None,"provider_request_id":None,"upstream_provider":None,"model_reported":None,"api_mode":None,"billing_mode":None,"request_status":"ok","error_class":None,"latency_ms":None,"cache_write_tokens":0,"reasoning_tokens":2,"usage_source":"provider_reported","usage_completeness":"complete","measurement_confidence":"exact","missing_fields":[],"attribution_gaps":[],"estimated_cost_usd":None,"actual_cost_usd":None,"cost_status":"included","cost_source":None,"pricing_version":None,"reconstructed_call_count":None,"corrects_source_namespace":None,"corrects_source_event_id":None}
    rows=[dict(base,source_event_id="1",provider="anthropic",model_requested="claude",input_tokens=10,cache_read_tokens=5,output_tokens=4),dict(base,source_event_id="2",provider="openrouter",model_requested="m",harness="opencode",purpose="subagent",record_kind="historical_aggregate",measurement_confidence="reconstructed",input_tokens=20,cache_read_tokens=0,output_tokens=8,reconstructed_call_count=3)]
    usage.write_text("\n".join(json.dumps(x) for x in rows)+"\n")
    q={"fact_type":"quota_observation_v1","schema_version":1,"source_namespace":"q","source_observation_id":"1","harness":"claude_code","observed_at":"2026-07-12T12:00:00Z","provider":"anthropic","account_ref":"private-account","quota_name":"five_hour","quota_scope":"account","window_kind":"rolling","window_started_at":None,"window_ends_at":None,"resets_at":None,"limit_value":"100","remaining_value":"80","used_value":"20","unit":"percent","measurement_confidence":"exact","provider_payload_ref":"private-ref","x_private":"do-not-project"}
    newer=dict(q,source_observation_id="2",observed_at="2026-07-12T12:00:00.1Z",remaining_value="79",used_value="21")
    quotas.write_text(json.dumps(q)+"\n"+json.dumps(newer)+"\n")
    client=create_app(atrium_root=str(tmp_path),ledger=str(tmp_path/"codex.jsonl"),unified_usage_ledger=str(usage),quota_ledger=str(quotas)).test_client()
    data=client.get("/api/unified-usage?provider=anthropic&harness=hermes&days=30").get_json()
    assert data["totals"]["input_tokens"]==10 and data["totals"]["total_tokens"]==19
    assert data["totals"]["estimated_cost_usd"] == "0"
    assert data["coverage"]=={"exact_events":1,"reconstructed_events":0,"reconstructed_calls":0}
    assert list(data["by_provider_model"])[0]["provider"]=="anthropic"
    subscriptions=client.get("/api/subscriptions").get_json()
    assert subscriptions["observations"][0]["used_value"]=="21"
    assert subscriptions["latest"][0]["used_value"] == "21"
    assert "source_namespace" not in subscriptions["latest"][0]
    assert "account_ref" not in subscriptions["latest"][0]
    assert "provider_payload_ref" not in subscriptions["latest"][0]
    assert "x_private" not in subscriptions["latest"][0]
    assert client.get("/api/unified-usage?days=-1").status_code == 400


def test_private_aggregate_endpoints_require_explicit_configuration(tmp_path, monkeypatch):
    monkeypatch.delenv("UNIFIED_USAGE_LEDGER_PATH", raising=False)
    monkeypatch.delenv("QUOTA_LEDGER_PATH", raising=False)
    monkeypatch.delenv("BILLING_LEDGER_PATH", raising=False)
    client = create_app(atrium_root=str(tmp_path), ledger=str(tmp_path / "codex.jsonl")).test_client()
    assert client.get("/api/unified-usage").status_code == 404
    assert client.get("/api/subscriptions").status_code == 404
    assert client.get("/api/billing").status_code == 404


def _billing_fact(source_id, kind, amount, currency="USD", **updates):
    row = {
        "fact_type": "billing_fact_v1",
        "schema_version": 1,
        "source_namespace": "private:test-billing",
        "source_billing_fact_id": source_id,
        "provider": "openrouter",
        "account_ref": "private-account",
        "occurred_at": "2026-07-12T12:00:00Z",
        "billing_period_start": "2026-07-01T00:00:00Z",
        "billing_period_end": "2026-08-01T00:00:00Z",
        "invoice_id": "private-invoice",
        "line_item_id": "private-line",
        "transaction_kind": kind,
        "status": "posted",
        "amount": amount,
        "currency": currency,
        "usage_event_refs": [{"source_namespace": "usage", "source_event_id": "u-1"}],
        "description_code": "api_usage",
        "provider_receipt_id": "private-receipt",
        "x_private": "never-return",
    }
    row.update(updates)
    return row


def test_private_sqlite_endpoints_match_jsonl_and_unknown_suffix_fails_closed(tmp_path):
    usage_rows = [
        dict(_usage_fact("u-1"), provider="openrouter", model_requested="requested", model_reported="reported", input_tokens=3),
        dict(_usage_fact("u-2"), provider="other", harness="other", purpose="subagent", model_requested="other", input_tokens=7),
    ]
    quota_rows = [{
        "fact_type": "quota_observation_v1", "schema_version": 1,
        "source_namespace": "quota", "source_observation_id": "q-1", "harness": "test",
        "observed_at": "2026-07-12T12:00:00Z", "provider": "openrouter", "quota_name": "weekly",
        "quota_scope": "account", "window_kind": "rolling", "unit": "percent",
        "measurement_confidence": "exact", "used_value": "25",
    }]
    usage_jsonl = tmp_path / "usage.jsonl"
    quota_jsonl = tmp_path / "quota.jsonl"
    usage_jsonl.write_text("\n".join(canonical_json(row) for row in usage_rows) + "\n", encoding="utf-8")
    quota_jsonl.write_text("\n".join(canonical_json(row) for row in quota_rows) + "\n", encoding="utf-8")
    usage_db = tmp_path / "usage.sqlite3"
    quota_db = tmp_path / "quota.db"
    append_sqlite_facts(usage_db, usage_rows, fact_type="usage_event_v1")
    append_sqlite_facts(quota_db, quota_rows, fact_type="quota_observation_v1")

    query = "?provider=openrouter&harness=test&purpose=main&model_requested=requested"
    jsonl_client = create_app(unified_usage_ledger=str(usage_jsonl), quota_ledger=str(quota_jsonl)).test_client()
    sqlite_client = create_app(unified_usage_ledger=str(usage_db), quota_ledger=str(quota_db)).test_client()
    assert sqlite_client.get("/api/unified-usage" + query).get_json() == jsonl_client.get("/api/unified-usage" + query).get_json()
    assert sqlite_client.get("/api/subscriptions?provider=openrouter&quota_name=weekly").get_json() == jsonl_client.get("/api/subscriptions?provider=openrouter&quota_name=weekly").get_json()

    bad = create_app(unified_usage_ledger=str(tmp_path / "usage.txt")).test_client()
    response = bad.get("/api/unified-usage")
    assert response.status_code == 400
    assert response.get_json() == {"error": "unsupported ledger suffix"}


def test_billing_api_signed_totals_safe_allowlist_filters_and_separate_costs(tmp_path):
    billing_db = tmp_path / "billing.sqlite"
    append_sqlite_facts(billing_db, [
        _billing_fact("charge", "charge", "12.50"),
        _billing_fact("refund", "refund", "-2.50"),
        _billing_fact("euro", "charge", "4", currency="EUR"),
    ], fact_type="billing_fact_v1")
    usage_db = tmp_path / "usage.sqlite3"
    append_sqlite_facts(usage_db, [dict(
        _usage_fact(), provider="openrouter", input_tokens=1,
        cost_status="actual", actual_cost_usd="99", billing_mode="payg",
    )], fact_type="usage_event_v1")
    client = create_app(unified_usage_ledger=str(usage_db), billing_ledger=str(billing_db)).test_client()

    result = client.get("/api/billing?provider=openrouter&status=posted").get_json()
    assert result["totals_by_currency"] == [
        {"amount": "4", "currency": "EUR"},
        {"amount": "10", "currency": "USD"},
    ]
    assert result["totals_by_currency_and_transaction_kind"] == [
        {"amount": "4", "currency": "EUR", "transaction_kind": "charge"},
        {"amount": "12.5", "currency": "USD", "transaction_kind": "charge"},
        {"amount": "-2.5", "currency": "USD", "transaction_kind": "refund"},
    ]
    safe = {"provider", "occurred_at", "billing_period_start", "billing_period_end", "transaction_kind", "status", "amount", "currency", "description_code"}
    assert result["transactions"] and all(set(row) == safe for row in result["transactions"])
    assert "99" not in json.dumps(result)
    assert len(client.get("/api/billing?currency=EUR&transaction_kind=charge").get_json()["transactions"]) == 1


@pytest.mark.parametrize("query", ["days=nope", "days=-1"])
def test_billing_api_rejects_malformed_days(tmp_path, query):
    ledger = tmp_path / "billing.jsonl"
    ledger.write_text(canonical_json(_billing_fact("charge", "charge", "1")) + "\n", encoding="utf-8")
    response = create_app(billing_ledger=str(ledger)).test_client().get("/api/billing?" + query)
    assert response.status_code == 400


def test_collect_all_dry_run_compact_summary(tmp_path, monkeypatch, capsys):
    state=tmp_path/"missing.db"; claude=tmp_path/"claude"; claude.mkdir()
    usage=tmp_path/"usage.jsonl"; quota=tmp_path/"quota.jsonl"; billing=tmp_path/"billing.jsonl"
    monkeypatch.setattr("sys.argv",["tracker","collect-all","--state-db",str(state),"--claude-root",str(claude),"--usage-ledger",str(usage),"--quota-ledger",str(quota),"--billing-ledger",str(billing),"--no-live-quota","--dry-run"])
    assert main()==0
    result=json.loads(capsys.readouterr().out)
    assert result["dry_run"] is True and result["paths"]["usage_ledger"]==str(usage)
    assert result["paths"]["billing_ledger"] == str(billing)
    assert result["billing"] == {"appended": 0, "discovered": 0, "replayed": 0}
    assert not usage.exists()
    assert not billing.exists()
    assert not usage.with_name(f"{usage.name}.lock").exists()
    assert set(result["sources"]) >= {"hermes","claude_code","opencode","codex_quota"}


def test_latest_budget_state_filters_to_current_week(monkeypatch):
    class Frozen(datetime):
        @classmethod
        def now(cls,tz=None): return cls(2026,7,15,tzinfo=timezone.utc)
    monkeypatch.setattr("codex_usage_tracker.provider_spend.datetime",Frozen)
    result=latest_budget_state([
      {"billed_usd":99,"started_at":"2026-07-05T12:00:00Z"},
      {"billed_usd":2,"started_at":"2026-07-14T12:00:00Z"},
      {"billed_usd":3,"occurred_at":"2026-07-15T12:00:00+00:00"},
      {"billed_usd":4,"occurred_at":"not-a-time","started_at":"2026-07-15T13:00:00Z"},
    ])
    assert result["direct_provider_spend_usd"]==9


def test_live_quota_failures_are_isolated_and_reported(tmp_path, monkeypatch):
    state=tmp_path/"missing.db"; claude=tmp_path/"claude"; claude.mkdir()
    monkeypatch.setattr(
        "codex_usage_tracker.collector.collect_hermes_usage",
        lambda *args, **kwargs: (_ for _ in ()).throw(sqlite3.DatabaseError("bad source")),
    )
    monkeypatch.setattr(
        "codex_usage_tracker.collector.collect_openrouter_quota",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("provider unavailable")),
    )
    monkeypatch.setattr(
        "codex_usage_tracker.collector.collect_deepseek_quota",
        lambda *args, **kwargs: [],
    )
    result = collect_all(
        state_db=state,
        claude_root=claude,
        opencode_dbs=[],
        codex_ledger=tmp_path/"codex.jsonl",
        usage_ledger=tmp_path/"usage.jsonl",
        quota_ledger=tmp_path/"quota.jsonl",
        live_quota=True,
        dry_run=True,
        environment={"OPENROUTER_API_KEY":"secret", "DEEPSEEK_API_KEY":"secret"},
    )
    assert result["sources"]["hermes"]["discovered"] == 0
    assert result["sources"]["deepseek_quota"]["discovered"] == 0
    assert result["warnings"] == [
        {"source":"hermes", "error":"DatabaseError"},
        {"source":"openrouter_quota", "error":"RuntimeError"},
    ]
    assert "secret" not in json.dumps(result)


def test_collect_all_cli_uses_real_sol_defaults(monkeypatch, capsys):
    captured = {}
    monkeypatch.delenv("UNIFIED_USAGE_LEDGER_PATH", raising=False)
    monkeypatch.delenv("QUOTA_LEDGER_PATH", raising=False)
    monkeypatch.delenv("BILLING_LEDGER_PATH", raising=False)

    def fake_collect_all(**kwargs):
        captured.update(kwargs)
        return {"ok": True}

    monkeypatch.setattr("codex_usage_tracker.collector.collect_all", fake_collect_all)
    monkeypatch.setattr("sys.argv", ["tracker", "collect-all", "--dry-run", "--no-live-quota"])
    assert main() == 0
    assert captured["state_db"] == "/var/lib/hermes/primary/state.db"
    assert captured["dotenv"] == "/var/lib/hermes/primary/.env"
    assert captured["claude_quota_command"] == "claude"
    assert captured["claude_probe_dir"] == "~/.local/state/codex-usage-tracker/claude-probe"
    assert captured["claude_quota_timeout"] == 25.0
    assert captured["usage_ledger"] == "~/.local/state/codex-usage-tracker/usage_events.sqlite3"
    assert captured["quota_ledger"] == "~/.local/state/codex-usage-tracker/quota_observations.sqlite3"
    assert captured["billing_ledger"] == "~/.local/state/codex-usage-tracker/billing_facts.sqlite3"
    assert captured["opencode_dbs"] == [
        "~/.local/share/opencode/opencode-stable.db",
        "~/.local/share/opencode/opencode-local.db",
    ]
    assert json.loads(capsys.readouterr().out) == {"ok": True}


def test_collect_all_cli_reports_sanitized_failure(monkeypatch, capsys):
    def fail(**kwargs):
        raise RuntimeError("credential and private path details")

    monkeypatch.setattr("codex_usage_tracker.collector.collect_all", fail)
    monkeypatch.setattr("sys.argv", ["tracker", "collect-all", "--dry-run", "--no-live-quota"])
    assert main() == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.strip() == "collect-all failed: RuntimeError"


def _stub_collectors(monkeypatch, usage_rows):
    monkeypatch.setattr("codex_usage_tracker.collector.collect_hermes_usage", lambda *args: list(usage_rows))
    monkeypatch.setattr("codex_usage_tracker.collector.collect_claude_usage", lambda *args: [])
    monkeypatch.setattr("codex_usage_tracker.collector.collect_opencode_usage", lambda *args: [])
    monkeypatch.setattr("codex_usage_tracker.collector.codex_quota_observations", lambda *args: [])


def test_collect_all_sqlite_append_replay_and_three_fact_bindings(tmp_path, monkeypatch):
    _stub_collectors(monkeypatch, [_usage_fact()])
    paths = {
        "usage_ledger": tmp_path / "usage.sqlite3",
        "quota_ledger": tmp_path / "quota.db",
        "billing_ledger": tmp_path / "billing.sqlite",
    }
    kwargs = dict(
        state_db=tmp_path / "state.db",
        claude_root=tmp_path / "claude",
        opencode_dbs=[],
        codex_ledger=tmp_path / "codex.jsonl",
        live_quota=False,
        **paths,
    )
    first = collect_all(**kwargs)
    second = collect_all(**kwargs)

    assert first["usage"] == {"discovered": 1, "appended": 1, "replayed": 0}
    assert second["usage"] == {"discovered": 1, "appended": 0, "replayed": 1}
    assert first["quotas"]["appended"] == 3
    assert first["billing"] == {"discovered": 0, "appended": 0, "replayed": 0}
    assert read_sqlite_facts(paths["usage_ledger"], fact_type="usage_event_v1")[0]["source_event_id"] == "u-1"
    for name, expected in (
        ("usage_ledger", "usage_event_v1"),
        ("quota_ledger", "quota_observation_v1"),
        ("billing_ledger", "billing_fact_v1"),
    ):
        with sqlite3.connect(paths[name]) as connection:
            assert connection.execute(
                "SELECT value FROM ledger_metadata WHERE key='fact_type'"
            ).fetchone()[0] == expected
        assert first["paths"][name] == str(paths[name])


def test_collect_all_preserves_jsonl_backend_compatibility(tmp_path, monkeypatch):
    _stub_collectors(monkeypatch, [_usage_fact()])
    usage = tmp_path / "usage.jsonl"
    quota = tmp_path / "quota.jsonl"
    billing = tmp_path / "billing.jsonl"
    result = collect_all(
        state_db=tmp_path / "state.db",
        claude_root=tmp_path / "claude",
        opencode_dbs=[],
        codex_ledger=tmp_path / "codex.jsonl",
        usage_ledger=usage,
        quota_ledger=quota,
        billing_ledger=billing,
        live_quota=False,
    )
    assert result["usage"]["appended"] == 1
    assert read_facts(usage)[0]["source_event_id"] == "u-1"
    assert {row["fact_type"] for row in read_facts(quota)} == {"quota_observation_v1"}
    assert read_facts(billing) == []


def test_collect_all_rejects_unknown_ledger_suffix(tmp_path, monkeypatch):
    _stub_collectors(monkeypatch, [])
    with pytest.raises(ValueError, match="suffix"):
        collect_all(
            state_db=tmp_path / "state.db",
            claude_root=tmp_path / "claude",
            opencode_dbs=[],
            codex_ledger=tmp_path / "codex.jsonl",
            usage_ledger=tmp_path / "usage.txt",
            quota_ledger=tmp_path / "quota.jsonl",
            live_quota=False,
            dry_run=True,
        )


def test_operator_cli_migrate_export_audit_round_trip(tmp_path, monkeypatch, capsys):
    source = tmp_path / "usage.jsonl"
    source.write_text(canonical_json(_usage_fact()) + "\n", encoding="utf-8")
    database = tmp_path / "usage.sqlite3"
    exported = tmp_path / "exported.jsonl"

    monkeypatch.setattr("sys.argv", [
        "tracker", "migrate-ledger", "--source-jsonl", str(source),
        "--destination-sqlite", str(database), "--fact-type", "usage_event_v1",
    ])
    assert main() == 0
    migrated = json.loads(capsys.readouterr().out)
    assert migrated["counts"] == {"appended": 1, "discovered": 1, "replayed": 0}
    assert migrated["paths"] == {"destination_sqlite": str(database), "source_jsonl": str(source)}

    monkeypatch.setattr("sys.argv", [
        "tracker", "export-ledger", "--source-sqlite", str(database),
        "--destination-jsonl", str(exported), "--fact-type", "usage_event_v1",
    ])
    assert main() == 0
    exported_result = json.loads(capsys.readouterr().out)
    assert exported_result["counts"] == {"exported": 1}
    assert read_facts(exported) == read_sqlite_facts(database)

    monkeypatch.setattr("sys.argv", [
        "tracker", "audit-ledger", "--sqlite", str(database),
        "--fact-type", "usage_event_v1",
    ])
    assert main() == 0
    audited = json.loads(capsys.readouterr().out)
    assert audited == {
        "counts": {"audited": 1},
        "fact_type": "usage_event_v1",
        "paths": {"sqlite": str(database)},
    }


def test_migrate_ledger_cli_dry_run_has_no_artifacts(tmp_path, monkeypatch, capsys):
    source = tmp_path / "usage.jsonl"
    source.write_text(canonical_json(_usage_fact()) + "\n", encoding="utf-8")
    destination = tmp_path / "absent" / "usage.sqlite3"
    monkeypatch.setattr("sys.argv", [
        "tracker", "migrate-ledger", "--source-jsonl", str(source),
        "--destination-sqlite", str(destination), "--fact-type", "usage_event_v1", "--dry-run",
    ])
    assert main() == 0
    result = json.loads(capsys.readouterr().out)
    assert result["dry_run"] is True and result["counts"]["appended"] == 1
    assert not destination.parent.exists()
    assert not source.with_name(f"{source.name}.lock").exists()


def test_operator_cli_sanitizes_fatal_errors(tmp_path, monkeypatch, capsys):
    private = tmp_path / "secret-source.jsonl"
    monkeypatch.setattr("sys.argv", [
        "tracker", "migrate-ledger", "--source-jsonl", str(private),
        "--destination-sqlite", str(tmp_path / "out.sqlite3"),
        "--fact-type", "usage_event_v1",
    ])
    assert main() == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.strip() == "migrate-ledger failed: MalformedLedgerError"
    assert str(private) not in captured.err


def test_collect_all_rejects_reused_or_aliased_destination_paths_before_writes(tmp_path, monkeypatch):
    _stub_collectors(monkeypatch, [_usage_fact()])
    shared = tmp_path / "shared.jsonl"
    with pytest.raises(ValueError, match="distinct"):
        collect_all(
            state_db=tmp_path / "state.db",
            claude_root=tmp_path / "claude",
            opencode_dbs=[],
            codex_ledger=tmp_path / "codex.jsonl",
            usage_ledger=shared,
            quota_ledger=shared,
            billing_ledger=shared,
            live_quota=False,
        )
    assert not shared.exists()


def test_collect_all_jsonl_dry_run_does_not_lock_or_chmod_existing_ledgers(tmp_path, monkeypatch):
    _stub_collectors(monkeypatch, [_usage_fact()])
    usage = tmp_path / "usage.jsonl"
    quota = tmp_path / "quota.jsonl"
    usage.write_text(canonical_json(_usage_fact("existing")) + "\n", encoding="utf-8")
    quota.write_text("", encoding="utf-8")
    os.chmod(usage, 0o644)
    os.chmod(quota, 0o644)
    before = {path: path.stat() for path in (usage, quota)}
    collect_all(
        state_db=tmp_path / "state.db",
        claude_root=tmp_path / "claude",
        opencode_dbs=[],
        codex_ledger=tmp_path / "codex.jsonl",
        usage_ledger=usage,
        quota_ledger=quota,
        live_quota=False,
        dry_run=True,
    )
    for path in (usage, quota):
        after = path.stat()
        prior = before[path]
        assert (after.st_mode, after.st_mtime_ns, after.st_size) == (prior.st_mode, prior.st_mtime_ns, prior.st_size)
        assert not path.with_name(path.name + ".lock").exists()


@pytest.mark.parametrize("endpoint", ["/api/unified-usage", "/api/billing"])
def test_private_api_rejects_overflowing_days_with_400(tmp_path, endpoint):
    usage = tmp_path / "usage.jsonl"
    billing = tmp_path / "billing.jsonl"
    usage.write_text(canonical_json(_usage_fact()) + "\n", encoding="utf-8")
    billing.write_text(canonical_json(_billing_fact("b", "charge", "1")) + "\n", encoding="utf-8")
    client = create_app(unified_usage_ledger=str(usage), billing_ledger=str(billing)).test_client()
    response = client.get(endpoint + "?days=999999999999999999999")
    assert response.status_code == 400


def test_private_api_enforces_request_wide_row_bound(tmp_path, monkeypatch):
    usage = tmp_path / "usage.sqlite3"
    append_sqlite_facts(usage, [_usage_fact(f"u-{index}") for index in range(3)], fact_type="usage_event_v1")
    monkeypatch.setattr(dashboard_module, "_PRIVATE_API_MAX_ROWS", 2)
    response = create_app(unified_usage_ledger=str(usage)).test_client().get("/api/unified-usage")
    assert response.status_code == 413
    assert response.get_json() == {"error": "ledger query exceeds private API row limit"}


def test_private_api_maps_non_utf8_jsonl_to_generic_503(tmp_path):
    usage = tmp_path / "usage.jsonl"
    usage.write_bytes(b"\xff\xfe\n")
    response = create_app(unified_usage_ledger=str(usage)).test_client().get("/api/unified-usage")
    assert response.status_code == 503
    assert response.get_json() == {"error": "configured private ledger is unavailable"}


def test_collect_all_dry_run_skips_mutating_claude_quota_probe(tmp_path, monkeypatch):
    _stub_collectors(monkeypatch, [])
    probe = tmp_path / "probe"

    def forbidden_probe(*args, **kwargs):
        raise AssertionError("Claude quota probe must not run during dry-run")

    monkeypatch.setattr("codex_usage_tracker.collector.collect_claude_code_quota", forbidden_probe)
    result = collect_all(
        state_db=tmp_path / "state.db",
        claude_root=tmp_path / "claude",
        opencode_dbs=[],
        codex_ledger=tmp_path / "codex.jsonl",
        usage_ledger=tmp_path / "usage.sqlite3",
        quota_ledger=tmp_path / "quota.sqlite3",
        billing_ledger=tmp_path / "billing.sqlite3",
        claude_quota_command="missing-claude",
        claude_probe_dir=probe,
        environment={},
        live_quota=True,
        dry_run=True,
    )
    assert result["sources"]["claude_code_quota"] == {"discovered": 0}
    assert not probe.exists()


def test_normal_collect_preflight_accepts_valid_active_wal(tmp_path, monkeypatch):
    _stub_collectors(monkeypatch, [_usage_fact()])
    usage = tmp_path / "usage.sqlite3"
    quota = tmp_path / "quota.sqlite3"
    billing = tmp_path / "billing.sqlite3"
    append_sqlite_facts(usage, [_usage_fact()], fact_type="usage_event_v1")
    append_sqlite_facts(quota, [], fact_type="quota_observation_v1")
    append_sqlite_facts(billing, [], fact_type="billing_fact_v1")
    reader = sqlite3.connect(usage)
    reader.execute("BEGIN")
    reader.execute("SELECT count(*) FROM facts").fetchone()
    try:
        result = collect_all(
            state_db=tmp_path / "state.db",
            claude_root=tmp_path / "claude",
            opencode_dbs=[],
            codex_ledger=tmp_path / "codex.jsonl",
            usage_ledger=usage,
            quota_ledger=quota,
            billing_ledger=billing,
            live_quota=False,
        )
    finally:
        reader.close()
    assert result["usage"]["replayed"] == 1


def test_sqlite_private_api_uses_one_stable_query_snapshot(tmp_path, monkeypatch):
    usage = tmp_path / "usage.sqlite3"
    append_sqlite_facts(
        usage,
        [_usage_fact(f"u-{index}") for index in range(3)],
        fact_type="usage_event_v1",
    )
    original = dashboard_module.query_sqlite_facts
    calls = 0

    def observed_query(*args, **kwargs):
        nonlocal calls
        calls += 1
        rows = original(*args, **kwargs)
        if calls == 1:
            append_sqlite_facts(usage, [_usage_fact("late")], fact_type="usage_event_v1")
        return rows

    monkeypatch.setattr(dashboard_module, "_PRIVATE_API_SQLITE_LIMIT", 2)
    monkeypatch.setattr(dashboard_module, "query_sqlite_facts", observed_query)
    response = create_app(unified_usage_ledger=str(usage)).test_client().get("/api/unified-usage")
    assert response.status_code == 200
    assert calls == 1
    assert sum(bucket["events"] for bucket in response.get_json()["by_provider_model"]) == 3
