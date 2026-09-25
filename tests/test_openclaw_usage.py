"""OpenClaw model calls and web searches (keeper, 2026-09-25, Discord unified
usage ledger thread msg 1553067845766742228: "brave needs usage tracking too.
and I completely forgot about openclaw. that's not tracked at all now right?").

The fixture store keeps the shape of the live one
(~/.openclaw/agents/<agent>/agent/openclaw-agent.sqlite, table
transcript_events), with made-up ids and counts."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

from codex_usage_tracker.collector import collect_all
from codex_usage_tracker.openclaw_usage import collect_openclaw_searches, collect_openclaw_usage, openclaw_stores


def _assistant(provider, model, usage, *, api="openai-completions", response_id=None, stop="stop", content=None):
    message = {
        "role": "assistant", "content": content or [{"type": "text", "text": "hi"}], "api": api,
        "provider": provider, "model": model, "usage": usage, "stopReason": stop, "timestamp": 1790348862342,
    }
    if response_id:
        message["responseId"] = response_id
    return message


def _usage(inp, out, read=0, write=0, cost=0):
    return {"input": inp, "output": out, "cacheRead": read, "cacheWrite": write, "reasoningTokens": 0,
            "totalTokens": inp + out + read + write, "cost": {"total": cost}}


def _store(home: Path, agent: str, events: list[tuple[str, str, dict]], sessions: dict[str, str]) -> Path:
    path = home / "agents" / agent / "agent" / "openclaw-agent.sqlite"
    path.parent.mkdir(parents=True)
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE session_windows (session_id TEXT PRIMARY KEY, session_key TEXT NOT NULL)")
    con.execute("CREATE TABLE transcript_events (session_id TEXT NOT NULL, seq INTEGER NOT NULL, event_json TEXT NOT NULL,"
                " created_at INTEGER NOT NULL, PRIMARY KEY (session_id, seq))")
    con.executemany("INSERT INTO session_windows VALUES (?, ?)", sessions.items())
    for seq, (session, event_id, message) in enumerate(events):
        stamp = f"2026-09-25T15:{seq:02d}:00.000Z"
        event = {"type": "message", "id": event_id, "timestamp": stamp, "message": message}
        con.execute("INSERT INTO transcript_events VALUES (?, ?, ?, ?)",
                    (session, seq, json.dumps(event), 1790348800000 + seq * 1000))
    con.commit()
    con.close()
    return path


def _home(tmp_path: Path) -> Path:
    home = tmp_path / "openclaw"
    _store(home, "main", [
        ("s1", "a1", _assistant("zai", "glm-5.3", _usage(769, 69, 112384), response_id="20260925230742c7474aae5e5b4768")),
        ("s1", "a2", _assistant("kimi", "k3", _usage(10, 20, 30), api="anthropic-messages", response_id="msg_abcdefghijklmnopqrstuvwx")),
        # Codex runtime: tool steps carry zero usage, the turn's end carries the whole turn.
        ("s2", "a3", _assistant("openai", "gpt-6-astra", _usage(0, 0), api="openai-chatgpt-responses", stop="toolUse")),
        ("s2", "a4", _assistant("openai", "gpt-6-astra", _usage(114417, 7860, 1888384), api="openai-chatgpt-responses")),
        ("s2", "a5", _assistant("meridian-primary", "claude-sonnet-5", _usage(2, 300, 50000), api="anthropic-messages")),
        ("s2", "a6", _assistant("openclaw", "delivery-mirror", _usage(1, 1))),
        ("s3", "a7", _assistant("venice", "kimi-k3", _usage(100, 50, cost=0.0123))),
        ("s1", "a8", {"role": "assistant", "content": [{"type": "toolCall", "name": "web_search", "id": "t1",
                                                        "arguments": {"query": "vikunja sqlite"}}],
                      "provider": "zai", "model": "glm-5.3", "usage": _usage(5, 5)}),
        ("s1", "r1", {"role": "toolResult", "toolName": "web_search", "toolCallId": "t1", "isError": False,
                      "details": {"kind": "results", "provider": "brave", "query": "vikunja sqlite", "count": 5, "tookMs": 677}}),
        ("s1", "r2", {"role": "toolResult", "toolName": "web_search", "toolCallId": "t2", "isError": True, "details": {}}),
        ("s1", "r3", {"role": "toolResult", "toolName": "read", "toolCallId": "t3", "isError": False, "details": {}}),
        ("s1", "u1", {"role": "user", "content": "hello"}),
    ], {"s1": "agent:main:discord:channel:1480944309577580546", "s2": "agent:main:cron:b026368b",
        "s3": "agent:main:subagent:x1"})
    _store(home, "wrist", [
        ("w1", "b1", _assistant("zai", "glm-5.3-flash", _usage(40, 4, 400))),
    ], {"w1": "agent:wrist:openresponses-user:pebble-pin-test"})
    return home


def _drain(generator):
    rows = []
    try:
        while True:
            rows.append(next(generator))
    except StopIteration as stop:
        return rows, stop.value


def test_model_calls_become_usage_facts_with_ledger_provider_names(tmp_path):
    home = _home(tmp_path)
    assert [agent for agent, _path in openclaw_stores(home)] == ["main", "wrist"]
    rows, diagnostics = _drain(collect_openclaw_usage(home, "sol:openclaw"))
    by_id = {row["source_event_id"]: row for row in rows}
    assert sorted(by_id) == ["s1:a1", "s1:a2", "s1:a8", "s2:a4", "s3:a7", "w1:b1"]
    assert diagnostics["skipped"] == {"zero_usage": 1, "meridian_relay": 1, "not_model_call": 1}

    zai = by_id["s1:a1"]
    assert (zai["harness"], zai["provider"], zai["model_reported"], zai["record_kind"]) == ("openclaw", "zai", "glm-5.3", "api_attempt")
    assert (zai["input_tokens"], zai["output_tokens"], zai["cache_read_tokens"], zai["cache_write_tokens"]) == (769, 69, 112384, 0)
    assert (zai["cost_status"], zai["billing_mode"], zai["surface"], zai["purpose"]) == ("included", "subscription_included", "discord", "main")
    assert zai["source_namespace"] == "sol:openclaw:main"
    assert zai["occurred_at"] == "2026-09-25T15:00:00Z"
    # The response id is kept visible and never joins anything.
    assert zai["x_openclaw_response_id"] == "20260925230742c7474aae5e5b4768" and zai.get("provider_request_id") is None

    kimi = by_id["s1:a2"]
    assert (kimi["provider"], kimi["x_openclaw_provider"]) == ("kimi-coding", "kimi")
    # Kimi's msg_ ids are Kimi's, not Anthropic's: never a physical-request key.
    assert kimi.get("provider_request_id") is None and kimi.get("upstream_provider") is None

    codex = by_id["s2:a4"]
    assert (codex["provider"], codex["record_kind"], codex["surface"]) == ("openai-codex", "historical_aggregate", "cron")
    assert codex["latency_ms"] is None

    venice = by_id["s3:a7"]
    assert (venice["provider"], venice["cost_status"], venice["purpose"]) == ("venice", "unknown", "subagent")
    assert venice["x_openclaw_cost_usd"] == "0.0123"

    wrist = by_id["w1:b1"]
    assert (wrist["source_namespace"], wrist["surface"]) == ("sol:openclaw:wrist", "openresponses-user")


def test_web_searches_become_search_facts_without_query_text(tmp_path):
    rows = list(collect_openclaw_searches(_home(tmp_path), "sol:openclaw-search"))
    assert [(row["provider"], row["status"], row["result_count"], row["latency_ms"]) for row in rows] == [
        ("brave", "ok", 5, 677), ("unknown", "error", None, None),
    ]
    first = rows[0]
    assert first["harness"] == "openclaw" and first["purpose"] == "web_search"
    assert first["query_sha256"] == hashlib.sha256(b"vikunja sqlite").hexdigest()
    assert "vikunja" not in json.dumps(rows)


def test_missing_home_is_an_empty_lane(tmp_path):
    assert openclaw_stores(tmp_path / "absent") == []
    assert _drain(collect_openclaw_usage(tmp_path / "absent"))[0] == []


def _collect(tmp_path, scope, home):
    return collect_all(
        state_db=tmp_path / "state.db", claude_root=tmp_path / "claude", opencode_dbs=[],
        codex_ledger=tmp_path / "codex.jsonl", usage_ledger=tmp_path / "usage.sqlite3",
        quota_ledger=tmp_path / "quota.sqlite3", search_ledger=tmp_path / "search.sqlite3",
        search_audit=tmp_path / "no-audit.jsonl", live_quota=False, scope=scope, strict_sources=True,
        openclaw_home=home,
    )


def test_collector_lanes_append_once_and_replay(tmp_path):
    home = _home(tmp_path)
    first = _collect(tmp_path, "usage", home)
    assert first["lanes"]["openclaw"]["succeeded"] is True
    assert first["usage"]["appended"] == 6
    again = _collect(tmp_path, "usage", home)
    assert (again["usage"]["appended"], again["usage"]["replayed"]) == (0, 6)
    searches = _collect(tmp_path, "searches", home)
    assert searches["lanes"]["openclaw_search"]["succeeded"] is True
    assert searches["searches"]["appended"] == 2
    # Without a home the lanes do not run at all.
    off = _collect(tmp_path, "usage", None)
    assert "openclaw" not in off["lanes"]
