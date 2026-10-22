"""OpenClaw model calls and web searches (keeper, 2026-09-25, Discord unified
usage ledger thread msg 1553067845766742228: "brave needs usage tracking too.
and I completely forgot about openclaw. that's not tracked at all now right?").

The fixture store keeps the shape of the live one
(~/.openclaw/agents/<agent>/agent/openclaw-agent.sqlite, table
transcript_events), with made-up ids and counts."""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from codex_usage_tracker.collector import collect_all
from codex_usage_tracker.openclaw_usage import (
    collect_openclaw_searches,
    collect_openclaw_usage,
    openclaw_stores,
    uuid7_ms,
)


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
        # The Claude CLI runtime: its own transcript under ~/.claude is the receipt.
        ("s3", "a9", _assistant("claude-cli", "claude-sonnet-5", _usage(2, 4, 0, 99718), api="cli")),
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
    assert diagnostics["skipped"] == {
        "zero_usage": 1, "meridian_relay": 1, "not_model_call": 1, "codex_rollout": 0, "claude_cli_receipt": 1,
    }

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


# --- Codex runtime turns: counted from the Codex rollout after the cutover (SIL-77) ---

CUTOVER = "2026-10-22T20:00:00Z"
_CUT_MS = int(datetime(2026, 10, 22, 20, tzinfo=timezone.utc).timestamp() * 1000)


def _uuid7(ms: int, tail: int) -> str:
    digits = f"{ms:012x}7{tail:03x}8{tail:015x}"
    return f"{digits[:8]}-{digits[8:12]}-{digits[12:16]}-{digits[16:20]}-{digits[20:]}"


THREAD = _uuid7(_CUT_MS - 3_600_000, 1)
OLD_TURN = _uuid7(_CUT_MS - 3_600_000, 2)   # started an hour before the cutover
NEW_TURN = _uuid7(_CUT_MS + 60_000, 3)      # started a minute after it
GUARD_THREAD = _uuid7(_CUT_MS + 61_000, 4)
GUARD_TURN = _uuid7(_CUT_MS + 61_000, 5)


def _codex_usage(inp, cached, out, reasoning=0):
    return {"input_tokens": inp, "cached_input_tokens": cached, "cache_write_input_tokens": 0,
            "output_tokens": out, "reasoning_output_tokens": reasoning, "total_tokens": inp + out}


def _rollout(home: Path, name: str, thread: str, source, turns: list[tuple[str, str, list[tuple[str, dict]]]]) -> Path:
    path = home / "agents" / "main" / "agent" / "codex-home" / "sessions" / "2026" / "10" / "22" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [{"timestamp": "2026-10-22T19:00:00.000Z", "type": "session_meta",
              "payload": {"id": thread, "originator": "openclaw", "source": source, "model_provider": "openai"}}]
    for turn, model, calls in turns:
        lines.append({"timestamp": "2026-10-22T20:01:00.000Z", "type": "turn_context",
                      "payload": {"turn_id": turn, "model": model}})
        for index, (response_id, usage) in enumerate(calls):
            lines.append({"timestamp": f"2026-10-22T20:01:{10 + index:02d}.000Z", "type": "token_usage_record",
                          "payload": {"thread_id": thread, "turn_id": turn, "response_id": response_id, "usage": usage}})
            lines.append({"timestamp": f"2026-10-22T20:01:{10 + index:02d}.001Z", "type": "event_msg",
                          "payload": {"type": "token_count", "info": None}})
    path.write_text("\n".join(json.dumps(line) for line in lines) + "\n{torn line")
    # Written during the turn, after the cutover (the real clock may be earlier).
    os.utime(path, (_CUT_MS / 1000 + 120, _CUT_MS / 1000 + 120))
    return path


def _codex_home(tmp_path: Path) -> Path:
    home = tmp_path / "openclaw"
    key = lambda turn: f"codex-app-server:{THREAD}:{turn}:assistant"  # noqa: E731
    old = _assistant("openai", "gpt-6.1-sol", _usage(100, 10, 900), api="openai-responses", response_id="oc-1")
    old["idempotencyKey"] = key(OLD_TURN)
    new = _assistant("openai", "gpt-6.1-sol", _usage(200, 20, 1800), api="openai-responses", response_id="oc-2")
    new["idempotencyKey"] = key(NEW_TURN)
    step = _assistant("openai", "gpt-6.1-sol", _usage(0, 0), api="openai-responses", stop="toolUse")
    step["idempotencyKey"] = f"codex-app-server:{THREAD}:{NEW_TURN}:tool-1"
    _store(home, "main", [("s9", "c1", old), ("s9", "c2", step), ("s9", "c3", new)],
           {"s9": "agent:main:discord:channel:1"})
    _rollout(home, "rollout-main.jsonl", THREAD, "vscode", [
        (OLD_TURN, "gpt-6.1-sol", [("resp_old1", _codex_usage(500, 400, 5))]),
        (NEW_TURN, "gpt-6.1-sol", [("resp_new1", _codex_usage(1000, 800, 50, 7)),
                                  ("resp_new2", _codex_usage(3000, 2900, 60))]),
    ])
    _rollout(home, "rollout-guardian.jsonl", GUARD_THREAD, {"subagent": {"other": "guardian"}}, [
        (GUARD_TURN, "codex-auto-review", [("resp_g1", _codex_usage(700, 0, 9))]),
    ])
    # A fork copy repeating a receipt it inherited: one physical call.
    _rollout(home, "rollout-fork.jsonl", THREAD, "vscode", [
        (NEW_TURN, "gpt-6.1-sol", [("resp_new1", _codex_usage(1000, 800, 50, 7))]),
    ])
    return home


def test_uuid7_turn_ids_carry_their_start():
    assert uuid7_ms(NEW_TURN) == _CUT_MS + 60_000
    # The live id from 2026-10-08 decodes to its task_started (1791439607 s).
    assert uuid7_ms("01a11a1f-15c2-7d93-bb22-f77bb3c8e06e") // 1000 == 1791439607
    assert uuid7_ms("38059e1f-7d77-491c-a320-22606efcfe73") is None  # v4
    assert uuid7_ms(None) is None and uuid7_ms("not-a-uuid") is None


def test_codex_turns_after_cutover_count_from_rollout_never_twice(tmp_path):
    rows, diagnostics = _drain(collect_openclaw_usage(_codex_home(tmp_path), "sol:openclaw", codex_rollout_cutover=CUTOVER))
    by_id = {row["source_event_id"]: row for row in rows}
    # Before the cutover: the transcript row, exactly as before, and not its rollout calls.
    assert "s9:c1" in by_id and by_id["s9:c1"]["provider"] == "openai"
    assert f"{THREAD}:resp_old1" not in by_id
    # After it: the rollout calls, and not the transcript row for that turn.
    assert "s9:c3" not in by_id
    assert diagnostics["skipped"]["codex_rollout"] == 1
    calls = sorted(key for key in by_id if key.endswith(("resp_new1", "resp_new2", "resp_g1")))
    assert calls == sorted([f"{GUARD_THREAD}:resp_g1", f"{THREAD}:resp_new1", f"{THREAD}:resp_new2"])
    assert diagnostics["codex_rollouts"] == {
        "files_read": 3, "calls": 3, "before_cutover": 1, "duplicate_response": 1, "malformed": 3,
    }

    first = by_id[f"{THREAD}:resp_new1"]
    assert first["source_namespace"] == "sol:openclaw:main:codex-rollout"
    assert (first["provider"], first["record_kind"], first["billing_mode"], first["cost_status"]) == (
        "openai-codex", "api_attempt", "subscription_included", "included")
    # Codex input includes the cached part; the ledger's fields are additive.
    assert (first["input_tokens"], first["cache_read_tokens"], first["output_tokens"], first["reasoning_tokens"]) == (200, 800, 50, 7)
    assert first["provider_request_id"] == "resp_new1"
    assert (first["model_reported"], first["session_id"], first["surface"], first["purpose"]) == (
        "gpt-6.1-sol", "s9", "discord", "main")
    assert (first["x_codex_thread_id"], first["x_codex_turn_id"]) == (THREAD, NEW_TURN)

    guard = by_id[f"{GUARD_THREAD}:resp_g1"]
    assert (guard["model_reported"], guard["purpose"], guard["session_id"]) == ("codex-auto-review", "subagent", None)


def test_rollouts_last_written_before_the_cutover_are_not_read(tmp_path):

    home = _codex_home(tmp_path)
    for path in home.glob("agents/*/agent/codex-home/sessions/**/*.jsonl"):
        os.utime(path, (_CUT_MS / 1000 - 3600, _CUT_MS / 1000 - 3600))
    rows, diagnostics = _drain(collect_openclaw_usage(home, "sol:openclaw", codex_rollout_cutover=CUTOVER))
    assert diagnostics["codex_rollouts"]["files_read"] == 0
    assert not [row for row in rows if row["source_namespace"].endswith(":codex-rollout")]


def test_codex_rollout_rows_append_once_and_replay(tmp_path, monkeypatch):
    import codex_usage_tracker.openclaw_usage as openclaw_usage

    monkeypatch.setattr(openclaw_usage, "CODEX_ROLLOUT_CUTOVER", CUTOVER)
    monkeypatch.setattr(
        openclaw_usage.collect_openclaw_usage, "__kwdefaults__", {"codex_rollout_cutover": CUTOVER}
    )
    home = _codex_home(tmp_path)
    first = _collect(tmp_path, "usage", home)
    assert first["lanes"]["openclaw"]["succeeded"] is True
    assert first["usage"]["appended"] == 4  # s9:c1 + three rollout calls
    again = _collect(tmp_path, "usage", home)
    assert (again["usage"]["appended"], again["usage"]["replayed"]) == (0, 4)
