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


def test_hermes_sidecar_layout_and_cost_micro_mapping(tmp_path):
    db = tmp_path / "usage_events.db"
    con = sqlite3.connect(db)
    con.execute("""CREATE TABLE usage_events (
      id INTEGER PRIMARY KEY, event_uid TEXT, timestamp REAL, harness TEXT, session_id TEXT, run_id TEXT,
      source TEXT, purpose TEXT, record_kind TEXT, usage_source TEXT, measurement_confidence TEXT,
      provider TEXT, model TEXT, api_mode TEXT, billing_base_url TEXT, billing_mode TEXT,
      input_tokens INTEGER, output_tokens INTEGER, cache_read_tokens INTEGER, cache_write_tokens INTEGER,
      reasoning_tokens INTEGER, cost_usd_micro INTEGER, cost_status TEXT, cost_source TEXT,
      pricing_version TEXT, latency_ms INTEGER, request_status TEXT, error_class TEXT, api_call_index INTEGER,
      created_at REAL)""")
    con.executemany("INSERT INTO usage_events VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", [
      (1,"u1",1783857600,"hermes","s1","r","discord","main","api_attempt","provider_reported","exact","zai","glm-5.3",None,None,"",10,20,0,0,5,483061,"estimated","provider_models_api",None,None,"ok",None,1,1783857600),
      (2,"u2",1783857601,"hermes","s2","r","discord","main","api_attempt","provider_reported","exact","anthropic","claude-opus-5",None,None,"subscription_included",10,20,0,0,5,0,"included","none",None,None,"ok",None,1,1783857601),
      (3,"u3",1783857602,"hermes","s3","r","discord","aux:approval","api_attempt","provider_reported","exact","zai","glm-5.3",None,None,"",10,20,0,0,5,None,"unknown","none",None,None,"ok",None,1,1783857602),
    ])
    con.commit(); con.close()
    rows = list(collect_hermes_usage(db, "ns:hermes"))
    by = {r["source_event_id"]: r for r in rows}
    assert by["u1"]["estimated_cost_usd"] == "0.483061" and by["u1"]["cost_status"] == "estimated"
    assert by["u2"]["cost_status"] == "included" and by["u2"]["estimated_cost_usd"] is None
    assert by["u3"]["cost_status"] == "unknown" and by["u3"]["estimated_cost_usd"] is None
    assert by["u3"]["purpose"] == "x_aux_approval"


def test_hermes_sidecar_model_reported_passes_through_and_gap_fires_only_when_absent(tmp_path):
    db = tmp_path / "usage_events.db"
    con = sqlite3.connect(db)
    con.execute("""CREATE TABLE usage_events (
      id INTEGER PRIMARY KEY, event_uid TEXT, timestamp REAL, harness TEXT, session_id TEXT, run_id TEXT,
      source TEXT, purpose TEXT, record_kind TEXT, usage_source TEXT, measurement_confidence TEXT,
      provider TEXT, model TEXT, model_reported TEXT, api_mode TEXT, billing_base_url TEXT, billing_mode TEXT,
      input_tokens INTEGER, output_tokens INTEGER, cache_read_tokens INTEGER, cache_write_tokens INTEGER,
      reasoning_tokens INTEGER, cost_usd_micro INTEGER, cost_status TEXT, cost_source TEXT,
      pricing_version TEXT, latency_ms INTEGER, request_status TEXT, error_class TEXT, api_call_index INTEGER,
      created_at REAL)""")
    con.executemany("INSERT INTO usage_events VALUES (" + ",".join("?" * 31) + ")", [
      (1,"u1",1783857600,"hermes","s1","r","discord","main","api_attempt","provider_reported","exact","zai","glm-5.3","glm-5.3-air",None,None,"",10,20,0,0,5,None,"unknown","none",None,None,"ok",None,1,1783857600),
      (2,"u2",1783857601,"hermes","s2","r","discord","main","api_attempt","provider_reported","exact","zai","glm-5.3",None,None,None,"",10,20,0,0,5,None,"unknown","none",None,None,"ok",None,1,1783857601),
      (3,"u3",1783857602,"hermes","s3","r","discord","main","api_attempt","provider_reported","exact","zai","glm-5.3","   ",None,None,"",10,20,0,0,5,None,"unknown","none",None,None,"ok",None,1,1783857602),
    ])
    con.commit(); con.close()
    by = {r["source_event_id"]: r for r in collect_hermes_usage(db, "ns:hermes")}
    assert by["u1"]["model_reported"] == "glm-5.3-air"
    assert "model_reported" not in by["u1"]["attribution_gaps"]
    # NULL and whitespace-only values are genuinely absent, so only they flag.
    assert by["u2"]["model_reported"] is None and "model_reported" in by["u2"]["attribution_gaps"]
    assert by["u3"]["model_reported"] is None and "model_reported" in by["u3"]["attribution_gaps"]


def test_hermes_database_without_event_tables_yields_zero_rows(tmp_path):
    db = tmp_path / "state.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE sessions (id INTEGER PRIMARY KEY)")
    con.commit(); con.close()
    assert list(collect_hermes_usage(db, "ns:hermes")) == []


def test_claude_stream_dedupe_subagent_synthetic_and_trailing_line(tmp_path):
    root = tmp_path / "projects" / "p"; root.mkdir(parents=True)
    rows = [
      {"type":"assistant","timestamp":"2026-07-12T12:00:00Z","sessionId":"s","message":{"id":"m1","model":"claude-sonnet","usage":{"input_tokens":1,"cache_creation_input_tokens":2,"cache_read_input_tokens":3,"output_tokens":4},"stop_reason":None}},
      {"type":"assistant","timestamp":"2026-07-12T12:00:01Z","sessionId":"s","message":{"id":"m1","model":"claude-sonnet","usage":{"input_tokens":1,"cache_creation_input_tokens":2,"cache_read_input_tokens":3,"output_tokens":8,"output_tokens_details":{"thinking_tokens":5}},"stop_reason":"end_turn"}},
      {"type":"assistant","timestamp":"2026-07-12T12:00:02Z","sessionId":"s","agentId":"a","isSidechain":True,"message":{"id":"m2","model":"claude-opus","usage":{"input_tokens":2,"output_tokens":3}}},
      {"type":"assistant","timestamp":"2026-07-12T12:00:03Z","sessionId":"s","message":{"id":"m3","model":"<synthetic>","usage":{"input_tokens":99}}},
      {"type":"user","timestamp":"2026-07-12T12:00:04Z","sessionId":"s","message":{"id":"prompt","content":"never read"}},
    ]
    transcript = root / "session.jsonl"
    transcript.write_text("\n".join(json.dumps(r) for r in rows) + "\n{active", encoding="utf-8")
    os.utime(transcript, (1_783_857_600, 1_783_857_600))
    facts = list(collect_claude_usage(tmp_path / "projects", "ns:claude"))
    assert len(facts) == 2
    assert facts[0]["output_tokens"] == 8 and facts[0]["reasoning_tokens"] == 5
    assert facts[0]["occurred_at"] == "2026-07-12T12:00:01Z"
    assert facts[1]["purpose"] == "subagent"
    assert all(f["provider"] == "anthropic" and f["cost_status"] == "included" for f in facts)


def test_claude_active_single_receipt_is_deferred_then_same_usage_replay_is_stable(tmp_path):
    root = tmp_path / "projects" / "p"
    root.mkdir(parents=True)
    transcript = root / "session.jsonl"
    first = {
        "type": "assistant",
        "timestamp": "2026-07-21T19:10:39.593Z",
        "sessionId": "session",
        "message": {
            "id": "message",
            "model": "claude-opus",
            "usage": {"input_tokens": 10, "output_tokens": 4},
            "stop_reason": None,
        },
    }
    later = {
        **first,
        "timestamp": "2026-07-21T19:11:26.082Z",
    }
    transcript.write_text(json.dumps(first) + "\n", encoding="utf-8")

    # A lone non-terminal receipt in a recently modified transcript is provisional.
    assert list(collect_claude_usage(tmp_path / "projects", "ns:claude")) == []

    transcript.write_text(json.dumps(first) + "\n" + json.dumps(later) + "\n", encoding="utf-8")
    facts = list(collect_claude_usage(tmp_path / "projects", "ns:claude"))
    assert len(facts) == 1
    assert facts[0]["occurred_at"] == "2026-07-21T19:11:26.082Z"
    assert facts[0]["recorded_at"] == "2026-07-21T19:11:26.082Z"
    assert facts[0]["output_tokens"] == 4


def test_claude_quiescent_single_receipt_is_collected(tmp_path):
    root = tmp_path / "projects" / "p"
    root.mkdir(parents=True)
    transcript = root / "session.jsonl"
    transcript.write_text(json.dumps({
        "type": "assistant",
        "timestamp": "2026-07-12T12:00:00Z",
        "sessionId": "session",
        "message": {
            "id": "message",
            "model": "claude-opus",
            "usage": {"input_tokens": 10, "output_tokens": 4},
            "stop_reason": None,
        },
    }) + "\n", encoding="utf-8")
    os.utime(transcript, (1_783_857_600, 1_783_857_600))

    facts = list(collect_claude_usage(tmp_path / "projects", "ns:claude"))
    assert len(facts) == 1


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


def _make_opencode_inflight(path, *, now_ms, completed=None, error=None):
    """One assistant row shaped like an OpenCode generation in progress."""
    con = sqlite3.connect(path)
    con.execute(
        "CREATE TABLE session (id TEXT PRIMARY KEY, parent_id TEXT, agent TEXT,"
        " time_created INTEGER, time_updated INTEGER)"
    )
    con.execute("INSERT INTO session VALUES (?,?,?,?,?)", ("root", None, "build", now_ms, now_ms))
    con.execute(
        "CREATE TABLE message (id TEXT, session_id TEXT, time_created INTEGER,"
        " time_updated INTEGER, data TEXT)"
    )
    timing = {"created": now_ms}
    if completed is not None:
        timing["completed"] = completed
    data = {
        "role": "assistant",
        "providerID": "anthropic",
        "modelID": "claude",
        "time": timing,
        "tokens": {"input": 0, "output": 0, "reasoning": 0, "cache": {"read": 0, "write": 0}},
    }
    if error is not None:
        data["error"] = error
    con.execute(
        "INSERT INTO message VALUES (?,?,?,?,?)",
        ("live", "root", now_ms, completed or now_ms, json.dumps(data)),
    )
    con.commit()
    con.close()


def test_opencode_defers_in_flight_rows_until_the_settle_window_elapses(tmp_path):
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    database = tmp_path / "stable.db"
    _make_opencode_inflight(database, now_ms=now_ms)

    assert list(collect_opencode_usage([database], "ns:oc")) == []
    # The escape hatch: a row that never completes is still eventually recorded.
    settled = list(collect_opencode_usage([database], "ns:oc", settle_seconds=0))
    assert len(settled) == 1
    assert settled[0]["source_event_id"] == "live"


def test_opencode_projects_a_completed_row_immediately(tmp_path):
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    database = tmp_path / "stable.db"
    _make_opencode_inflight(database, now_ms=now_ms, completed=now_ms + 1000)

    facts = list(collect_opencode_usage([database], "ns:oc"))
    assert len(facts) == 1
    assert facts[0]["request_status"] == "ok"


def test_opencode_settle_window_withholds_the_status_that_later_flips_to_error(tmp_path):
    """The mid-generation snapshot is the one that poisons an identity forever."""
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    database = tmp_path / "stable.db"
    _make_opencode_inflight(database, now_ms=now_ms)
    assert list(collect_opencode_usage([database], "ns:oc")) == []

    _make_opencode_inflight(
        tmp_path / "settled.db",
        now_ms=now_ms,
        completed=now_ms + 44_000,
        error={"name": "UnknownError"},
    )
    facts = list(collect_opencode_usage([tmp_path / "settled.db"], "ns:oc"))
    assert [fact["request_status"] for fact in facts] == ["error"]


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
    # The retired session window emits NO row rather than an all-None one
    # (keeper ruling 2026-08-30: null rows rendered as "Unavailable" cards).
    assert "five_hour" not in facts
    assert facts["week"]["used_value"] == "15"
    assert facts["week"]["remaining_value"] == "85"
    assert facts["week"]["resets_at"] == "2026-07-19T19:39:02Z"


def test_codex_quota_history_mode_projects_every_retained_snapshot(tmp_path):
    ledger = tmp_path / "codex.jsonl"
    ledger.write_text(
        "\n".join(
            json.dumps(
                {
                    "id": snapshot_id,
                    "fetched_at": fetched_at,
                    "session_used_pct": used,
                    "weekly_used_pct": used + 10,
                }
            )
            for snapshot_id, fetched_at, used in (
                ("older", "2026-07-11T12:00:00Z", 10),
                ("newer", "2026-07-12T12:00:00Z", 20),
            )
        )
        + "\n",
        encoding="utf-8",
    )

    newest = codex_quota_observations(ledger, "ns:codex")
    history = codex_quota_observations(
        ledger,
        "ns:codex",
        include_history=True,
        history_since="2026-07-10T00:00:00Z",
    )
    bounded = codex_quota_observations(
        ledger,
        "ns:codex",
        include_history=True,
        history_since="2026-07-11T18:00:00Z",
    )

    assert len(newest) == 2
    assert {fact["source_observation_id"].split(":", 1)[0] for fact in newest} == {"newer"}
    assert len(history) == 4
    assert {fact["source_observation_id"].split(":", 1)[0] for fact in history} == {"older", "newer"}
    assert len(bounded) == 2
    assert {fact["source_observation_id"].split(":", 1)[0] for fact in bounded} == {"newer"}


def test_codex_quota_nulls_and_opencode_partial_coverage(tmp_path):
    ledger=tmp_path/"codex.jsonl"
    ledger.write_text(
        json.dumps({"id":"older-offset","fetched_at":"2026-07-12T13:00:00+02:00","session_used_pct":99,"weekly_used_pct":99})+"\n"+
        json.dumps({"id":"snap","fetched_at":"2026-07-12T12:00:00Z","session_used_pct":20,"weekly_used_pct":None,"session_reset_at":None,"weekly_reset_at":1783861200,"raw_payload":{"authorization":"secret"}})+"\n"
    )
    facts=codex_quota_observations(ledger,"ns:codex")
    # Null weekly emits no row (keeper ruling 2026-08-30); the live window survives.
    assert len(facts)==1 and facts[0]["quota_name"]=="five_hour" and facts[0]["used_value"] == "20"
    assert all("raw_payload" not in f for f in facts)
    # The window is anchored at first use, so ``now`` sits inside the window
    # the 12:00 event opened.
    quotas=derive_opencode_go_quotas([event(provider="opencode-go", harness="opencode", x_opencode_quota_cost_usd="3")],"ns:go",now=datetime(2026,7,12,13,tzinfo=timezone.utc))
    assert {q["quota_name"] for q in quotas}=={"five_hour","week","month"}
    assert all(q["measurement_confidence"]=="estimated" and q["x_coverage"]["harnesses_with_values"]==["opencode"] for q in quotas)
    assert all(q["used_value"]=="3" and q["x_coverage"]["anchor"]=="first_use_estimate" for q in quotas)


# --- OpenCode Go estimate: valuation and first-use anchored windows -------------


def _go_event(event_id, occurred, **updates):
    return event(
        source_event_id=event_id,
        provider="opencode-go",
        occurred_at=occurred,
        recorded_at=occurred,
        **updates,
    )


def test_opencode_go_estimate_values_hermes_rows_at_list_price_and_counts_unpriced():
    now = datetime(2026, 9, 2, 12, tzinfo=timezone.utc)
    rows = [
        # OpenCode harness: the provider's own quota cost wins.
        _go_event("oc", "2026-09-02T10:00:00Z", harness="opencode", x_opencode_quota_cost_usd="0.5"),
        # Hermes routes through opencode-go with no quota cost: list price
        # (glm-5.3: 1.40 in / 4.40 out / 0.26 cache read per MTok). Canonical
        # output_tokens already includes reasoning, so reasoning is NOT added.
        _go_event(
            "hermes-priced", "2026-09-02T10:30:00Z", harness="hermes",
            model_requested="glm-5.3", model_reported="glm-5.3",
            input_tokens=1_000_000, cache_read_tokens=1_000_000, cache_write_tokens=0,
            output_tokens=1_000_000, reasoning_tokens=500_000,
        ),
        # A model with no rate is coverage, never silently priced.
        _go_event(
            "hermes-unpriced", "2026-09-02T11:00:00Z", harness="hermes",
            model_requested="qwen-unpriced-model", model_reported=None,
            input_tokens=10, output_tokens=10,
        ),
        # Another provider's row never counts.
        event(source_event_id="zai", provider="zai", harness="hermes", occurred_at="2026-09-02T11:00:00Z"),
    ]
    quotas = {q["quota_name"]: q for q in derive_opencode_go_quotas(rows, "ns:go", now=now)}
    five = quotas["five_hour"]
    assert five["used_value"] == "6.56"  # 0.5 + (1.40 + 0.26 + 4.40)
    assert five["remaining_value"] == "5.44"
    assert five["unit"] == "usd" and five["measurement_confidence"] == "estimated"
    assert five["window_kind"] == "rolling"
    assert five["x_coverage"] == {
        "status": "partial",
        "anchor": "first_use_estimate",
        "harnesses_with_values": ["hermes", "opencode"],
        "valued_events": 2,
        "unvalued_events": 1,
        "list_priced_events": 1,
    }


def test_opencode_go_estimate_windows_anchor_at_first_use():
    rows = [
        _go_event("a", "2026-09-02T00:00:00Z", harness="opencode", x_opencode_quota_cost_usd="1"),
        _go_event("b", "2026-09-02T04:59:00Z", harness="opencode", x_opencode_quota_cost_usd="2"),
        # 05:30 is after the first five-hour window closed (05:00): it opens
        # a new one, so a trailing slice would have counted b but the
        # anchored window does not.
        _go_event("c", "2026-09-02T05:30:00Z", harness="opencode", x_opencode_quota_cost_usd="4"),
    ]
    now = datetime(2026, 9, 2, 6, tzinfo=timezone.utc)
    five = next(q for q in derive_opencode_go_quotas(rows, "ns:go", now=now) if q["quota_name"] == "five_hour")
    assert five["used_value"] == "4"
    assert five["window_started_at"] == "2026-09-02T05:30:00Z"
    assert five["window_ends_at"] == five["resets_at"] == "2026-09-02T10:30:00Z"
    assert five["x_coverage"]["valued_events"] == 1
    # The week window opened with the first event and still holds all three.
    week = next(q for q in derive_opencode_go_quotas(rows, "ns:go", now=now) if q["quota_name"] == "week")
    assert week["used_value"] == "7"
    assert week["window_started_at"] == "2026-09-02T00:00:00Z"
    assert week["resets_at"] == "2026-09-09T00:00:00Z"


def test_opencode_go_estimate_reports_no_open_window_without_fabricating_bounds():
    rows = [_go_event("old", "2026-09-01T00:00:00Z", harness="opencode", x_opencode_quota_cost_usd="9")]
    now = datetime(2026, 9, 2, 12, tzinfo=timezone.utc)
    quotas = {q["quota_name"]: q for q in derive_opencode_go_quotas(rows, "ns:go", now=now)}
    five = quotas["five_hour"]
    assert five["used_value"] == "0" and five["remaining_value"] == "12"
    assert five["window_started_at"] is None and five["window_ends_at"] is None and five["resets_at"] is None
    assert five["x_coverage"]["valued_events"] == 0
    # The week window is still open from that event.
    assert quotas["week"]["used_value"] == "9"
    # Future events (clock skew) and duplicate identities never count twice.
    duplicated = rows + [dict(rows[0]), _go_event("future", "2026-09-03T00:00:00Z", harness="opencode", x_opencode_quota_cost_usd="1")]
    again = {q["quota_name"]: q for q in derive_opencode_go_quotas(duplicated, "ns:go", now=now)}
    assert again["week"]["used_value"] == "9"


# --- Hermes sidecar: purpose translation and cost filing --------------------------


def _sidecar_db(path, rows):
    con = sqlite3.connect(path)
    con.execute("""CREATE TABLE usage_events (
      id INTEGER PRIMARY KEY, event_uid TEXT, timestamp REAL, harness TEXT, session_id TEXT, run_id TEXT,
      source TEXT, purpose TEXT, record_kind TEXT, usage_source TEXT, measurement_confidence TEXT,
      provider TEXT, model TEXT, api_mode TEXT, billing_base_url TEXT, billing_mode TEXT,
      input_tokens INTEGER, output_tokens INTEGER, cache_read_tokens INTEGER, cache_write_tokens INTEGER,
      reasoning_tokens INTEGER, cost_usd_micro INTEGER, cost_status TEXT, cost_source TEXT,
      pricing_version TEXT, latency_ms INTEGER, request_status TEXT, error_class TEXT, api_call_index INTEGER,
      created_at REAL)""")
    con.executemany("INSERT INTO usage_events VALUES (" + ",".join("?" * 30) + ")", rows)
    con.commit(); con.close()


def _sidecar_row(n, uid, *, purpose="main", micro=None, status="unknown"):
    return (n, uid, 1783857600 + n, "hermes", f"s{n}", "r", "discord", purpose, "api_attempt", "provider_reported", "exact",
            "zai", "glm-5.3", None, None, "", 10, 20, 0, 0, 5, micro, status, "none", None, None, "ok", None, 1, 1783857600 + n)


def test_hermes_unmapped_aux_purpose_becomes_named_extension_and_keeps_raw_value(tmp_path):
    db = tmp_path / "usage_events.db"
    _sidecar_db(db, [
        _sidecar_row(1, "u1", purpose="aux:summarization"),
        _sidecar_row(2, "u2", purpose="aux:Odd Name/2"),
        _sidecar_row(3, "u3", purpose="totally-unknown"),
        _sidecar_row(4, "u4", purpose="aux:approval"),
        _sidecar_row(5, "u5", purpose="background_review"),
    ])
    by = {r["source_event_id"]: r for r in collect_hermes_usage(db, "ns:hermes")}
    # Every row validated (the lane no longer cedes on one raw aux value).
    assert set(by) == {"u1", "u2", "u3", "u4", "u5"}
    assert by["u1"]["purpose"] == "x_aux_summarization" and by["u1"]["x_aux_purpose"] == "aux:summarization"
    assert by["u2"]["purpose"] == "x_aux_odd_name_2" and by["u2"]["x_aux_purpose"] == "aux:Odd Name/2"
    assert by["u3"]["purpose"] == "other" and by["u3"]["x_aux_purpose"] == "totally-unknown"
    # Exact matches and canonical values are untouched (and carry no extension:
    # the live ledger already holds them without one).
    assert by["u4"]["purpose"] == "x_aux_approval" and "x_aux_purpose" not in by["u4"]
    assert by["u5"]["purpose"] == "background_review" and "x_aux_purpose" not in by["u5"]


def test_hermes_sidecar_cost_is_filed_under_its_cost_status(tmp_path):
    db = tmp_path / "usage_events.db"
    _sidecar_db(db, [
        _sidecar_row(1, "actual", micro=1_500_000, status="actual"),
        _sidecar_row(2, "estimated", micro=250_000, status="estimated"),
        _sidecar_row(3, "unknown", micro=750_000, status="unknown"),
        _sidecar_row(4, "included", micro=750_000, status="included"),
        _sidecar_row(5, "reconstructed", micro=10_000, status="reconstructed"),
    ])
    by = {r["source_event_id"]: r for r in collect_hermes_usage(db, "ns:hermes")}
    assert by["actual"]["actual_cost_usd"] == "1.5" and by["actual"]["estimated_cost_usd"] is None
    assert by["actual"]["cost_status"] == "actual" and "x_hermes_cost_usd" not in by["actual"]
    assert by["estimated"]["estimated_cost_usd"] == "0.25" and by["estimated"]["actual_cost_usd"] is None
    assert by["unknown"]["estimated_cost_usd"] is None and by["unknown"]["actual_cost_usd"] is None
    assert by["unknown"]["cost_status"] == "unknown" and by["unknown"]["x_hermes_cost_usd"] == "0.75"
    assert by["included"]["estimated_cost_usd"] is None and "x_hermes_cost_usd" not in by["included"]
    assert by["reconstructed"]["cost_status"] == "estimated" and by["reconstructed"]["estimated_cost_usd"] == "0.01"


# --- Claude transcripts: a malformed interior line skips only that file -----------


def _write_transcript(path, lines):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.utime(path, (1_783_857_600, 1_783_857_600))


def test_claude_malformed_interior_line_skips_that_file_only_and_reports_it(tmp_path):
    root = tmp_path / "projects"
    good = {"type": "assistant", "timestamp": "2026-07-12T12:00:00Z", "sessionId": "s", "message": {"id": "m1", "model": "claude-opus", "usage": {"input_tokens": 1, "output_tokens": 2}, "stop_reason": "end_turn"}}
    _write_transcript(root / "a" / "good.jsonl", [json.dumps(good)])
    _write_transcript(root / "b" / "torn.jsonl", [json.dumps(dict(good, sessionId="t")), "{torn interior", json.dumps(dict(good, sessionId="t2"))])
    skipped = []
    facts = list(collect_claude_usage(root, "ns:claude", skipped_files=skipped))
    assert [f["session_id"] for f in facts] == ["s"]
    assert skipped == [{"path": str(root / "b" / "torn.jsonl"), "line": 2, "error": skipped[0]["error"]}]
    # The generator's return value carries the same diagnostics for callers
    # that only materialize the stream.
    generator = collect_claude_usage(root, "ns:claude")
    while True:
        try:
            next(generator)
        except StopIteration as stop:
            assert [entry["line"] for entry in stop.value["skipped_files"]] == [2]
            break


# --- Codex spark windows are retired outright (keeper ruling 2026-09-06) ------


def test_codex_spark_windows_emit_no_rows(tmp_path):
    ledger = tmp_path / "codex.jsonl"
    ledger.write_text(json.dumps({
        "id": "snap", "fetched_at": "2026-07-12T12:00:00Z",
        "session_used_pct": 20, "weekly_used_pct": 30,
        "spark_session_used_pct": None, "spark_weekly_used_pct": 5,
    }) + "\n")
    facts = {f["quota_name"]: f for f in codex_quota_observations(ledger, "ns:codex")}
    assert set(facts) == {"five_hour", "week"}


# --- OpenRouter: an overdrawn account is an observation, not a ceded lane --------------


def test_openrouter_overdraft_clamps_canonical_remaining_and_keeps_the_signed_figure(monkeypatch):
    from codex_usage_tracker import quota as quota_module

    class Response:
        def __init__(self, data): self._data = data
        def raise_for_status(self): pass
        def json(self): return self._data

    class Client:
        def __init__(self, *a, **k): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def get(self, url, headers):
            if url == quota_module.OPENROUTER_CREDITS_URL:
                return Response({"data": {"total_credits": 10, "total_usage": 12.5}})
            return Response({"data": {"limit": 5, "limit_remaining": -3.5, "usage": 8.5}})

    monkeypatch.setattr(quota_module.httpx, "Client", Client)
    rows = {r["quota_name"]: r for r in quota_module.collect_openrouter_quota("sk-private-secret", "ns:or", observed_at="2026-09-02T12:00:00Z")}
    credit = rows["credit_balance"]
    assert credit["remaining_value"] == "0" and credit["used_value"] == "12.5"
    assert credit["x_provider_balance"] == "-2.5" and credit["x_overdrawn"] is True
    key = rows["api_key_quota"]
    assert key["remaining_value"] == "0" and key["x_provider_limit_remaining"] == "-3.5"
    assert key["x_overdrawn"] is True
    assert "sk-private-secret" not in json.dumps(rows)
