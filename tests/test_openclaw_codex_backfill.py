"""SIL-77 backfill (keeper decision 2026-10-22, option B): reverse the
ledger's OpenClaw transcript row for each pre-cutover Codex turn and import the
turn's rollout calls. Fixtures are shared with test_openclaw_usage."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import sqlite3

import pytest

from codex_usage_tracker.canonical_ledger import append_sqlite_facts
from codex_usage_tracker.list_pricing import request_context_basis
from codex_usage_tracker.openclaw_codex_backfill import BackfillRefused, plan_backfill
from test_openclaw_usage import (
    CUTOVER, OLD_TURN, THREAD, _assistant, _codex_home, _codex_usage, _collect, _rollout, _usage,
)

LATER = datetime(2026, 10, 23, 12, tzinfo=timezone.utc)
TOKENS = ("input_tokens", "cache_read_tokens", "cache_write_tokens", "output_tokens")


def _ledger_with_lane(tmp_path):
    home = _codex_home(tmp_path)
    _collect(tmp_path, "usage", home)
    return home, tmp_path / "usage.sqlite3"


def _sums(ledger):
    con = sqlite3.connect(ledger)
    try:
        rows = [json.loads(r[0]) for r in con.execute("SELECT canonical_json FROM facts WHERE harness = 'openclaw'")]
    finally:
        con.close()
    return rows, sum(sum(int(r.get(f) or 0) for f in TOKENS) for r in rows)


def test_backfill_reverses_the_transcript_row_and_imports_the_turns_calls(tmp_path):
    home, ledger = _ledger_with_lane(tmp_path)
    plan = plan_backfill(home, ledger, cutover=CUTOVER, now=LATER)
    by_kind = {}
    for fact in plan["facts"]:
        by_kind.setdefault(fact["record_kind"], []).append(fact)
    (reversal,) = by_kind["correction"]
    assert (reversal["corrects_source_namespace"], reversal["corrects_source_event_id"]) == ("sol:openclaw:main", "s9:c1")
    assert reversal["source_namespace"] == "sol:openclaw:main:codex-backfill"
    assert (reversal["input_tokens"], reversal["output_tokens"], reversal["cache_read_tokens"]) == (-100, -10, -900)
    assert reversal["x_corrected_record_kind"] == "api_attempt" and "x_openclaw_response_id" not in reversal
    assert reversal["occurred_at"] == "2026-09-25T15:00:00Z"  # the reversed row's own instant
    # Only the pre-cutover turn's call; post-cutover calls belong to the lane.
    assert [c["source_event_id"] for c in by_kind["api_attempt"]] == [f"{THREAD}:resp_old1"]
    assert plan["summary"]["net_added_tokens"] == 505 - 1010

    _rows, before = _sums(ledger)
    result = append_sqlite_facts(ledger, plan["facts"], fact_type="usage_event_v1")
    assert (result.appended, result.replayed) == (2, 0)
    _rows, after = _sums(ledger)
    assert after - before == 505 - 1010
    # Re-running replays; the lane keeps replaying its rows and adds nothing.
    again = plan_backfill(home, ledger, cutover=CUTOVER, now=LATER)
    assert append_sqlite_facts(ledger, again["facts"], fact_type="usage_event_v1").replayed == 2
    lane = _collect(tmp_path, "usage", home)
    assert lane["usage"]["appended"] == 0


def test_backfill_refuses_while_a_covered_transcript_row_is_not_yet_in_the_ledger(tmp_path):
    home, ledger = _ledger_with_lane(tmp_path)
    con = sqlite3.connect(home / "agents" / "main" / "agent" / "openclaw-agent.sqlite")
    late = _assistant("openai", "gpt-6.1-sol", _usage(1, 1, 1), api="openai-responses")
    late["idempotencyKey"] = f"codex-app-server:{THREAD}:{OLD_TURN}:assistant-2"
    con.execute("INSERT INTO transcript_events VALUES (?, ?, ?, ?)",
                ("s9", 99, json.dumps({"type": "message", "id": "c9", "timestamp": "2026-09-25T16:00:00.000Z", "message": late}), 1790352400000))
    con.commit()
    con.close()
    with pytest.raises(BackfillRefused, match="not in the ledger"):
        plan_backfill(home, ledger, cutover=CUTOVER, now=LATER)


def test_backfill_refuses_while_a_pre_cutover_turn_may_still_be_running(tmp_path):
    home, ledger = _ledger_with_lane(tmp_path)
    with pytest.raises(BackfillRefused, match="may still be running"):
        plan_backfill(home, ledger, cutover=CUTOVER, now=datetime(2026, 10, 22, 20, 30, tzinfo=timezone.utc))


def test_a_reversal_prices_on_the_reversed_requests_own_basis():
    long_prompt = {"record_kind": "api_attempt", "input_tokens": 156547, "cache_read_tokens": 2468480}
    reversal = {"record_kind": "correction", "x_corrected_record_kind": "api_attempt",
                "input_tokens": -156547, "cache_read_tokens": -2468480}
    assert request_context_basis(long_prompt) == request_context_basis(reversal) == "long"
    aggregate_reversal = {**reversal, "x_corrected_record_kind": "historical_aggregate"}
    assert request_context_basis(aggregate_reversal) == "aggregate"
    # Ordinary corrections keep their old treatment.
    assert request_context_basis({"record_kind": "correction", "input_tokens": 400000}) == "aggregate"
