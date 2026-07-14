from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone

import pytest

from codex_usage_tracker.canonical_ledger import (
    IdentityConflictError,
    MalformedLedgerError,
    ValidationError,
    append_facts,
    normalize_fact,
    read_facts,
)
from codex_usage_tracker.usage_adapters import collect_claude_usage, collect_hermes_usage, collect_opencode_usage
from codex_usage_tracker.quota import codex_quota_observations, derive_opencode_go_quotas


def event(**updates):
    row = {
        "fact_type": "usage_event_v1", "schema_version": 1,
        "source_namespace": "test:install", "source_event_id": "one",
        "harness": "test", "purpose": "main", "record_kind": "api_attempt",
        "occurred_at": "2026-07-12T12:00:00.100+00:00", "recorded_at": "2026-07-12T12:00:00Z",
        "usage_source": "provider_reported", "usage_completeness": "complete",
        "measurement_confidence": "exact", "cost_status": "included",
        "input_tokens": 1, "cache_read_tokens": 2, "cache_write_tokens": 0,
        "output_tokens": 4, "reasoning_tokens": 3,
    }
    row.update(updates)
    return row


def test_canonical_ledger_materializes_fields_secret_policy_and_replay(tmp_path):
    normalized = normalize_fact(event(x_token_count=9))
    assert normalized["occurred_at"] == "2026-07-12T12:00:00.1Z"
    assert normalized["actual_cost_usd"] is None
    assert normalized["model_reported"] is None
    assert normalized["x_token_count"] == 9
    with pytest.raises(ValidationError, match="api_key"):
        normalize_fact(event(x_meta={"api_key": "secret"}))
    for secret_key in (
        "apiKey", "x-api-key", "Proxy-Authorization", "Cookie", "credentials",
        "x_access_token", "x_password", "x_secret", "x_token", "x_x_api_key",
        "api_keys", "secret_key", "client_secrets", "private_key",
    ):
        with pytest.raises(ValidationError, match="credential-bearing"):
            normalize_fact(event(x_meta={secret_key: "secret"}))
    assert normalize_fact(event(x_api_key_quota=10))["x_api_key_quota"] == 10

    path = tmp_path / "usage.jsonl"
    first = append_facts(path, [event(estimated_cost_usd=None)])
    replay = append_facts(path, [event(estimated_cost_usd=None)])
    assert (first.appended, first.replayed) == (1, 0)
    assert (os.stat(path).st_mode & 0o777) == 0o600
    assert (replay.appended, replay.replayed) == (0, 1)
    with pytest.raises(IdentityConflictError):
        append_facts(path, [event(input_tokens=99)])


def test_malformed_existing_jsonl_is_never_silently_skipped(tmp_path):
    path = tmp_path / "bad.jsonl"
    path.write_text('{"fact_type": "usage_event_v1"}\nnot json\n', encoding="utf-8")
    with pytest.raises(MalformedLedgerError, match=r"bad.jsonl:2"):
        read_facts(path)
    with pytest.raises(MalformedLedgerError):
        append_facts(path, [event()])


def test_hermes_mixed_provider_history_and_included_cost(tmp_path):
    db = tmp_path / "state.db"
    con = sqlite3.connect(db)
    con.execute("""CREATE TABLE llm_usage_events (
      id INTEGER PRIMARY KEY, timestamp REAL, session_id TEXT, source TEXT, provider TEXT, model TEXT,
      api_mode TEXT, billing_base_url TEXT, billing_mode TEXT, input_tokens INTEGER, output_tokens INTEGER,
      cache_read_tokens INTEGER, cache_write_tokens INTEGER, reasoning_tokens INTEGER,
      estimated_cost_usd NUMERIC, actual_cost_usd NUMERIC, cost_status TEXT, cost_source TEXT,
      pricing_version TEXT, latency_ms INTEGER, request_status TEXT, error_class TEXT, api_call_index INTEGER,
      created_at TEXT, event_uid TEXT, purpose TEXT, record_kind TEXT, usage_source TEXT,
      measurement_confidence TEXT)""")
    con.executemany("INSERT INTO llm_usage_events VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", [
        (1, 1783857600, "s1", "discord", "openrouter", "", "chat", "SECRET", None, 1,2,3,4,1,".1",None,"estimated","catalog",None,10,"ok",None,1,"2026-07-12T12:00:00Z","uid-1","main","api_attempt","provider_reported","exact"),
        (2, 1783857601, "s2", "cli", "anthropic", "claude", "messages", "SECRET", "subscription_included", 5,6,0,0,2,0,0,"included","subscription",None,None,None,None,7,"2026-07-12T12:00:01Z",None,"historical_backfill","historical_aggregate","reconstructed","reconstructed"),
    ])
    con.commit(); con.close()
    rows = list(collect_hermes_usage(db, "ns:hermes", batch_size=1))
    assert [r["provider"] for r in rows] == ["openrouter", "anthropic"]
    assert rows[0]["usage_completeness"] == "unknown" and "billing_base_url" not in rows[0]
    assert rows[0]["model_requested"] is None
    assert rows[0]["model_reported"] is None and "model_reported" in rows[0]["attribution_gaps"]
    assert rows[1]["source_event_id"] == "sqlite-row:2"
    assert rows[1]["reconstructed_call_count"] == 7
    assert rows[1]["estimated_cost_usd"] is None and rows[1]["actual_cost_usd"] is None


def test_claude_stream_dedupe_subagent_synthetic_and_trailing_line(tmp_path):
    root = tmp_path / "projects" / "p"; root.mkdir(parents=True)
    rows = [
      {"type":"assistant","sessionId":"s","message":{"id":"m1","model":"claude-sonnet","usage":{"input_tokens":1,"cache_creation_input_tokens":2,"cache_read_input_tokens":3,"output_tokens":4},"stop_reason":None}},
      {"type":"assistant","sessionId":"s","message":{"id":"m1","model":"claude-sonnet","usage":{"input_tokens":1,"cache_creation_input_tokens":2,"cache_read_input_tokens":3,"output_tokens":8,"output_tokens_details":{"thinking_tokens":5}},"stop_reason":"end_turn"}},
      {"type":"assistant","sessionId":"s","agentId":"a","isSidechain":True,"message":{"id":"m2","model":"claude-opus","usage":{"input_tokens":2,"output_tokens":3}}},
      {"type":"assistant","sessionId":"s","message":{"id":"m3","model":"<synthetic>","usage":{"input_tokens":99}}},
      {"type":"user","sessionId":"s","message":{"id":"prompt","content":"never read"}},
    ]
    (root / "session.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n{active", encoding="utf-8")
    facts = list(collect_claude_usage(tmp_path / "projects", "ns:claude"))
    assert len(facts) == 2
    assert facts[0]["output_tokens"] == 8 and facts[0]["reasoning_tokens"] == 5
    assert facts[1]["purpose"] == "subagent"
    assert all(f["provider"] == "anthropic" and f["cost_status"] == "included" for f in facts)


def _make_opencode(path, *, legacy=False):
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE session (id TEXT PRIMARY KEY, parent_id TEXT, agent TEXT, time_created INTEGER, time_updated INTEGER)")
    con.executemany("INSERT INTO session VALUES (?,?,?,?,?)", [("root",None,"build",1000,2000),("child","root","task",1000,2000)])
    if legacy:
        con.execute("CREATE TABLE session_message (id TEXT, session_id TEXT, type TEXT, seq INTEGER, time_created INTEGER, time_updated INTEGER, data TEXT)")
        con.execute("INSERT INTO session_message VALUES (?,?,?,?,?,?,?)",("old","root","assistant",1,1100,1200,json.dumps({"role":"assistant","model":{"providerID":"deepseek","id":"v3"},"tokens":{"input":1,"output":2,"reasoning":3,"cache":{"read":4,"write":5}},"cost":0.25})))
    else:
        con.execute("CREATE TABLE message (id TEXT, session_id TEXT, time_created INTEGER, time_updated INTEGER, data TEXT)")
        con.execute("CREATE TABLE session_message (id TEXT, session_id TEXT, type TEXT, seq INTEGER, time_created INTEGER, time_updated INTEGER, data TEXT)")
        data={"role":"assistant","providerID":"opencode-go","modelID":"kimi","tokens":{"input":1,"output":2,"reasoning":3,"cache":{"read":4,"write":5}},"cost":0.5}
        con.execute("INSERT INTO message VALUES (?,?,?,?,?)",("new","child",1100,1200,json.dumps(data)))
        con.execute("INSERT INTO session_message VALUES (?,?,?,?,?,?,?)",("new","child","assistant",1,1100,1200,json.dumps(data)))
    con.commit(); con.close()


def test_opencode_current_legacy_reasoning_and_no_overlap(tmp_path):
    current, legacy = tmp_path/"stable.db", tmp_path/"local.db"
    _make_opencode(current); _make_opencode(legacy, legacy=True)
    facts=list(collect_opencode_usage([current,legacy], "ns:oc"))
    assert len(facts)==2
    go=next(f for f in facts if f["provider"]=="opencode-go")
    assert go["purpose"]=="subagent" and go["output_tokens"]==5 and go["reasoning_tokens"]==3
    assert go["estimated_cost_usd"] is None and go["x_opencode_quota_cost_usd"]=="0.5"
    paid=next(f for f in facts if f["provider"]=="deepseek")
    assert paid["model_requested"] == "v3"
    assert paid["estimated_cost_usd"]=="0.25" and paid["cost_source"]=="opencode-local-calculation"


def test_negative_correction_decimal_keeps_leading_zero():
    corrected = normalize_fact(
        event(
            source_event_id="correction",
            record_kind="correction",
            input_tokens=-1,
            estimated_cost_usd="-0.5",
            cost_status="estimated",
        )
    )
    assert corrected["estimated_cost_usd"] == "-0.5"
    with pytest.raises(ValidationError, match="only valid on corrections"):
        normalize_fact(event(corrects_source_namespace="n", corrects_source_event_id="e"))


def test_codex_quota_reconciles_retained_weekly_only_raw_payload(tmp_path):
    ledger = tmp_path / "codex.jsonl"
    ledger.write_text(
        json.dumps(
            {
                "id": "snap",
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
        )
        + "\n",
        encoding="utf-8",
    )

    facts = {fact["quota_name"]: fact for fact in codex_quota_observations(ledger, "ns:codex")}
    assert facts["five_hour"]["used_value"] is None
    assert facts["five_hour"]["resets_at"] is None
    assert facts["week"]["used_value"] == "15"
    assert facts["week"]["remaining_value"] == "85"
    assert facts["week"]["resets_at"] == "2026-07-19T19:39:02Z"


def test_codex_quota_nulls_and_opencode_partial_coverage(tmp_path):
    ledger=tmp_path/"codex.jsonl"
    ledger.write_text(
        json.dumps({"id":"older-offset","fetched_at":"2026-07-12T13:00:00+02:00","session_used_pct":99,"weekly_used_pct":99})+"\n"+
        json.dumps({"id":"snap","fetched_at":"2026-07-12T12:00:00Z","session_used_pct":20,"weekly_used_pct":None,"session_reset_at":None,"weekly_reset_at":1783861200,"raw_payload":{"authorization":"secret"}})+"\n"
    )
    facts=codex_quota_observations(ledger,"ns:codex")
    assert len(facts)==2 and facts[0]["used_value"] == "20" and facts[1]["used_value"] is None
    assert all("raw_payload" not in f for f in facts)
    quotas=derive_opencode_go_quotas([event(provider="opencode-go", harness="opencode", x_opencode_quota_cost_usd="3")],"ns:go",now=datetime(2026,7,12,tzinfo=timezone.utc))
    assert {q["quota_name"] for q in quotas}=={"five_hour","week","month"}
    assert all(q["measurement_confidence"]=="estimated" and q["x_coverage"]["harnesses_with_values"]==["opencode"] for q in quotas)
