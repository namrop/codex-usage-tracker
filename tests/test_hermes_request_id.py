"""Hermes sidecar response ids -> canonical physical-request identity.

Usage contract v2; the Hermes<->SDK double count fix (keeper, Discord
1552924591557574687). Hermes records each call's raw response id. The
collector keys it as a physical request only on the explicitly listed relay
routes (the two Meridian lanes) and only when it has Anthropic's id shape;
everything else keeps the id as a visible, never-joined extension.
"""
from __future__ import annotations

import json
import sqlite3

from codex_usage_tracker.canonical_ledger import append_sqlite_facts, query_sqlite_physical_requests
from codex_usage_tracker.usage_adapters import collect_claude_usage, collect_hermes_usage

REAL_ID = "msg_011CfPfSeZnPyZipskzk2go5"  # verified in both surfaces 2026-09-25


def _sidecar(tmp_path, rows, *, with_column=True):
    db = tmp_path / "usage_events.db"
    con = sqlite3.connect(db)
    extra = ", provider_request_id TEXT" if with_column else ""
    con.execute(f"""CREATE TABLE usage_events (
      id INTEGER PRIMARY KEY, event_uid TEXT, timestamp REAL, harness TEXT, session_id TEXT,
      source TEXT, purpose TEXT, record_kind TEXT, usage_source TEXT, measurement_confidence TEXT,
      provider TEXT, model TEXT, model_reported TEXT, api_mode TEXT, billing_base_url TEXT, billing_mode TEXT,
      input_tokens INTEGER, output_tokens INTEGER, cache_read_tokens INTEGER, cache_write_tokens INTEGER,
      reasoning_tokens INTEGER, cost_usd_micro INTEGER, cost_status TEXT, cost_source TEXT,
      pricing_version TEXT, latency_ms INTEGER, request_status TEXT, error_class TEXT, api_call_index INTEGER,
      created_at REAL{extra})""")
    width = 31 if with_column else 30
    for index, (uid, provider, base_url, response_id) in enumerate(rows, start=1):
        values = [index, uid, 1790316757.5, "hermes", "sess", "discord", "main", "api_attempt",
                  "provider_reported", "exact", provider, "claude-haiku-4-5", "claude-haiku-4-5",
                  "anthropic_messages", base_url, "subscription_included", 546, 37, 0, 0, 0, None,
                  "included", "subscription", None, None, "ok", None, 1, 1790316757.5]
        if with_column:
            values.append(response_id)
        con.execute(f"INSERT INTO usage_events VALUES ({','.join('?' * width)})", values)
    con.commit()
    con.close()
    return db


def test_meridian_real_id_becomes_physical_request(tmp_path):
    db = _sidecar(tmp_path, [
        ("u1", "custom:meridian-primary", "http://127.0.0.1:3458", REAL_ID),
        ("u2", "custom", "http://127.0.0.1:3457/", "msg_011CfPfZZZZZZZZZZZZZZZZZ"),
    ])
    by = {row["source_event_id"]: row for row in collect_hermes_usage(db, "sol:hermes")}
    assert by["u1"]["provider_request_id"] == REAL_ID and by["u1"]["upstream_provider"] == "anthropic"
    # Bare "custom" label, Meridian base URL: the route table still recognizes it.
    assert by["u2"]["upstream_provider"] == "anthropic"
    assert "x_hermes_response_id" not in by["u1"]


def test_meridian_synthetic_id_and_unlisted_routes_are_never_keyed(tmp_path):
    db = _sidecar(tmp_path, [
        ("s1", "custom:meridian-yugen", "http://127.0.0.1:3457", "msg_1790316687652"),
        ("s2", "kimi-coding", "https://api.kimi.com/coding", "msg_01KimiLooksAnthropicShaped00"),
        ("s3", "openai-codex", "https://chatgpt.com/backend-api/codex", "resp_abc"),
    ])
    for row in collect_hermes_usage(db, "sol:hermes"):
        assert row["provider_request_id"] is None and row["upstream_provider"] is None
        assert row["x_hermes_response_id"]


def test_rows_without_an_id_and_old_sidecars_are_unchanged(tmp_path):
    new = _sidecar(tmp_path, [("n1", "custom:meridian-primary", "http://127.0.0.1:3458", None)])
    old_dir = tmp_path / "old"
    old_dir.mkdir()
    old = _sidecar(old_dir, [("n1", "custom:meridian-primary", "http://127.0.0.1:3458", None)], with_column=False)
    (new_row,) = collect_hermes_usage(new, "sol:hermes")
    (old_row,) = collect_hermes_usage(old, "sol:hermes")
    # Byte-identical canonical facts: the collector's append-time identity
    # check sees no change for every row written before the Hermes switch.
    assert json.dumps(new_row, sort_keys=True) == json.dumps(old_row, sort_keys=True)
    assert "x_hermes_response_id" not in new_row and new_row["provider_request_id"] is None


def test_hermes_and_sdk_observations_of_one_meridian_call_count_once(tmp_path):
    db = _sidecar(tmp_path, [("h1", "custom:meridian-primary", "http://127.0.0.1:3458", REAL_ID)])
    projects = tmp_path / "claude" / "-var-lib-pharos-services-meridian-primary"
    projects.mkdir(parents=True)
    receipt = {
        "type": "assistant", "sessionId": "0732e1f5-sdk", "timestamp": "2026-09-25T06:12:37.397Z",
        "message": {"id": REAL_ID, "model": "claude-haiku-4-5-20251001", "stop_reason": "end_turn",
                    "usage": {"input_tokens": 546, "output_tokens": 37, "cache_read_input_tokens": 0,
                              "cache_creation_input_tokens": 0,
                              "output_tokens_details": {"thinking_tokens": 30}}},
    }
    (projects / "0732e1f5-sdk.jsonl").write_text(json.dumps(receipt) + "\n")
    facts = list(collect_hermes_usage(db, "sol:hermes")) + list(
        collect_claude_usage(tmp_path / "claude", "sol:claude-code", settle_seconds=0)
    )
    ledger = tmp_path / "unified.sqlite3"
    append_sqlite_facts(ledger, facts, fact_type="usage_event_v1")
    rows, stats = query_sqlite_physical_requests(
        ledger, occurred_or_observed_at_gte="2026-09-25T00:00:00Z", occurred_or_observed_at_lt="2026-09-26T00:00:00Z"
    )
    assert stats["observations"] == 2 and stats["physical_requests"] == 1
    assert stats["cross_harness_requests"] == 1 and stats["payload_conflicts"] == 0
    assert [row["harness"] for row in rows] == ["hermes"]  # the caller keeps the attribution


# Keeper, msg 1552986099343818813: "meridian primary and meridian yugen are
# really anthropic 1 and 2". From RELAY_ROUTE_RECORDED_FROM the collector
# records the Meridian lane a Hermes call went through; before it, rows keep
# the exact shape already stored (the adapter re-reads the whole sidecar).

from datetime import datetime, timezone

from codex_usage_tracker.usage_adapters import RELAY_ROUTE_RECORDED_FROM


def _restamp(db, epoch):
    con = sqlite3.connect(db)
    con.execute("UPDATE usage_events SET timestamp = ?, created_at = ?", (epoch, epoch))
    con.commit()
    con.close()


_CUTOVER = datetime.fromisoformat(RELAY_ROUTE_RECORDED_FROM).replace(tzinfo=timezone.utc).timestamp()
ROUTES = [
    ("p1", "custom:meridian-primary", "http://127.0.0.1:3458", None),
    ("p2", "custom", "http://127.0.0.1:3458/", "msg_1790316687652"),
    ("y1", "custom", "http://127.0.0.1:3457", REAL_ID),
    ("y2", "custom:meridian-yugen", None, None),
    ("k1", "kimi-coding", "https://api.kimi.com/coding", None),
    ("c1", "custom", "http://127.0.0.1:8017/v1", None),
]


def test_relay_route_is_recorded_from_the_cutover(tmp_path):
    db = _sidecar(tmp_path, ROUTES)
    _restamp(db, _CUTOVER)
    by = {row["source_event_id"]: row for row in collect_hermes_usage(db, "sol:hermes")}
    assert by["p1"]["x_relay_route"] == by["p2"]["x_relay_route"] == "meridian-primary"
    assert by["y1"]["x_relay_route"] == by["y2"]["x_relay_route"] == "meridian-yugen"
    assert "x_relay_route" not in by["k1"] and "x_relay_route" not in by["c1"]
    # The route is recorded alongside, never instead of, the id rules.
    assert by["y1"]["provider_request_id"] == REAL_ID and by["p2"]["provider_request_id"] is None


def test_rows_before_the_cutover_keep_their_stored_shape(tmp_path):
    db = _sidecar(tmp_path, ROUTES)
    _restamp(db, _CUTOVER - 0.001)
    for row in collect_hermes_usage(db, "sol:hermes"):
        assert "x_relay_route" not in row
