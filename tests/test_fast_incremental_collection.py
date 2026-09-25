"""Fast incremental collection (keeper msg 1553090125062479884: "can we split
it up so that the core stuff is quick and the longer stuff happens less
frequently? or do the requests in parallel?").

Covers the two speed cuts that made the 15-minute tick quick:
- the Claude reconcile loads only the canonical rows matching incoming
  identities instead of the whole ledger (the whole-ledger load was ~10 min of
  an ~18 min run);
- ``--usage-since`` bounds the Hermes sidecar scan and skips Claude
  transcripts untouched since the epoch.
"""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

import pytest

from codex_usage_tracker.canonical_ledger import append_sqlite_facts
from codex_usage_tracker.collector import _load_reconcile_window, collect_all
from codex_usage_tracker.usage_adapters import collect_claude_usage, collect_hermes_usage

from test_collector_convergence import _claude_fact, _kwargs, _stub_claude


def _sidecar(path: Path, rows: list[tuple]) -> None:
    con = sqlite3.connect(path)
    con.execute(
        """CREATE TABLE usage_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT, event_uid TEXT, timestamp REAL NOT NULL,
            harness TEXT NOT NULL, session_id TEXT, source TEXT, purpose TEXT NOT NULL,
            record_kind TEXT NOT NULL, usage_source TEXT NOT NULL,
            measurement_confidence TEXT NOT NULL, provider TEXT, model TEXT,
            input_tokens INTEGER, output_tokens INTEGER, created_at REAL NOT NULL
        )"""
    )
    con.executemany(
        "INSERT INTO usage_events(event_uid, timestamp, harness, session_id, purpose, "
        "record_kind, usage_source, measurement_confidence, provider, model, "
        "input_tokens, output_tokens, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        rows,
    )
    con.commit()
    con.close()


def test_reconcile_window_loads_only_incoming_identities(tmp_path):
    """The targeted loader answers the reconcile from a handful of rows."""
    usage = tmp_path / "usage.sqlite3"
    claude = _claude_fact(request_status="unknown", output_tokens=5)
    hermes_row = dict(claude, source_namespace="sol:hermes", source_event_id="hermes:1")
    append_sqlite_facts(usage, [claude, hermes_row], fact_type="usage_event_v1")

    connection = sqlite3.connect(str(usage))
    try:
        window = _load_reconcile_window(connection, [claude])
    finally:
        connection.close()
    assert [row["source_event_id"] for row in window] == [claude["source_event_id"]]
    # The loaded row is the canonical payload itself, not a re-normalized copy.
    assert window[0]["output_tokens"] == claude["output_tokens"]


def test_reconcile_window_handles_empty_batch():
    assert _load_reconcile_window(sqlite3.connect(":memory:"), []) == []


def test_targeted_reconcile_still_finalizes_and_adjudicates(tmp_path, monkeypatch):
    """Same convergence semantics as the whole-ledger path, on the fast path."""
    usage = tmp_path / "usage.sqlite3"
    original = _claude_fact(request_status="unknown", output_tokens=5, input_tokens=10)
    append_sqlite_facts(usage, [original], fact_type="usage_event_v1")
    kwargs = _kwargs(tmp_path, usage)

    _stub_claude(monkeypatch, [dict(original, request_status="ok", output_tokens=1028)])
    first = collect_all(**kwargs)
    assert first["generated_corrections"] == 1
    assert first["conflicts"] == []

    _stub_claude(monkeypatch, [dict(original, request_status="ok", output_tokens=2000)])
    second = collect_all(**kwargs)
    third = collect_all(**kwargs)
    assert second["generated_corrections"] == 1
    assert third["generated_corrections"] == 0
    rows = [r for r in json_rows(usage) if r.get("record_kind") == "correction"]
    assert len(rows) == 2
    assert sorted(int(row["output_tokens"]) for row in rows) == [972, 1023]


def test_usage_since_skips_untouched_transcripts(tmp_path, monkeypatch):
    """A transcript untouched since the cutoff yields no rows on the fast tick."""
    transcript = tmp_path / "projects" / "s" / "a.jsonl"
    transcript.parent.mkdir(parents=True)
    message = {
        "type": "assistant",
        "sessionId": "s",
        "timestamp": "2026-09-25T12:00:00Z",
        "message": {
            "id": "m1",
            "model": "claude-opus",
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 10, "output_tokens": 10},
        },
    }
    transcript.write_text(json.dumps(message) + "\n", encoding="utf-8")
    past = time.time() - 3600

    fresh = list(collect_claude_usage(tmp_path / "projects", "sol:claude-code"))
    assert len(fresh) == 1
    # mtime older than the cutoff: skipped entirely.
    import os

    os.utime(transcript, (past - 60, past - 60))
    skipped = list(
        collect_claude_usage(tmp_path / "projects", "sol:claude-code", since=past)
    )
    assert skipped == []
    # A cutoff before the file's mtime keeps it.
    kept = list(
        collect_claude_usage(tmp_path / "projects", "sol:claude-code", since=past - 120)
    )
    assert len(kept) == 1


def test_hermes_since_bounds_the_sidecar_scan(tmp_path):
    """Only sidecar rows recorded at/after the epoch are read on the fast tick."""
    sidecar = tmp_path / "usage_events.db"
    now = time.time()
    _sidecar(
        sidecar,
        [
            ("uid-1", now - 7200, "hermes", "s1", "main", "api_attempt",
             "provider_reported", "exact", "zai", "glm-5.3", 100, 10, now - 7200),
            ("uid-2", now - 60, "hermes", "s2", "main", "api_attempt",
             "provider_reported", "exact", "zai", "glm-5.3", 200, 20, now - 60),
        ],
    )
    all_rows = list(collect_hermes_usage(sidecar, "sol:hermes"))
    assert len(all_rows) == 2
    recent = list(collect_hermes_usage(sidecar, "sol:hermes", since=now - 300))
    assert [row["event_uid"] for row in recent] == ["uid-2"]


def test_collect_all_reports_the_cutoff_it_ran_with(tmp_path, monkeypatch):
    usage = tmp_path / "usage.sqlite3"
    for adapter in ("collect_hermes_usage", "collect_claude_usage", "collect_opencode_usage"):
        monkeypatch.setattr("codex_usage_tracker.collector."+adapter, lambda *args, **kwargs: [])
    kwargs = _kwargs(tmp_path, usage)
    cutoff = time.time() - 900
    result = collect_all(**kwargs, usage_since=cutoff)
    assert result["usage"]["usage_since"] == cutoff
    deep = collect_all(**kwargs)
    assert "usage_since" not in deep["usage"]


def test_reconcile_window_scans_corrections_once_not_per_chunk(tmp_path):
    usage = tmp_path / "usage.sqlite3"
    fact = _claude_fact()
    append_sqlite_facts(usage, [fact], fact_type="usage_event_v1")
    incoming = [dict(fact, source_event_id=f"event:{i}") for i in range(1001)]
    con = sqlite3.connect(f"file:{usage}?mode=ro", uri=True)
    statements = []
    con.set_trace_callback(statements.append)
    try:
        _load_reconcile_window(con, incoming)
        con.set_trace_callback(None)
        correction_queries = [q for q in statements if "json_extract" in q]
        assert len(correction_queries) == 1
        for q in statements:
            if "SELECT" not in q.upper() or "json_extract" in q:
                continue
            plan = [r[3] for r in con.execute("EXPLAIN QUERY PLAN " + q)]
            assert not any("SCAN facts" in step or "SCAN f " in step for step in plan), plan
    finally:
        con.close()


def test_reconcile_window_checks_selected_payload_hash(tmp_path):
    from codex_usage_tracker.canonical_ledger import MalformedLedgerError

    usage = tmp_path / "usage.sqlite3"
    fact = _claude_fact()
    append_sqlite_facts(usage, [fact], fact_type="usage_event_v1")
    con = sqlite3.connect(usage)
    con.execute("DROP TRIGGER facts_no_update")
    con.execute("UPDATE facts SET canonical_sha256 = ?", ("0" * 64,))
    con.commit()
    try:
        with pytest.raises(MalformedLedgerError, match="hash mismatch"):
            _load_reconcile_window(con, [fact])
    finally:
        con.close()


def test_targeted_writer_validates_reconcile_output(tmp_path):
    from codex_usage_tracker.canonical_ledger import (
        append_sqlite_facts_quarantined_reconciled, ValidationError,
    )

    fact = _claude_fact()
    def invalid(existing, incoming):
        return [dict(incoming[0], input_tokens=-1)], None

    with pytest.raises(ValidationError):
        append_sqlite_facts_quarantined_reconciled(
            tmp_path / "usage.sqlite3", [fact], fact_type="usage_event_v1",
            reconcile=invalid, reconcile_existing=lambda con, rows: [],
        )



def test_hermes_legacy_without_created_at_falls_back_to_full(tmp_path):
    sidecar=tmp_path/'legacy.db'
    now=time.time()
    _sidecar(sidecar, [('uid-1',now-7200,'hermes','s','main','api_attempt',
                       'provider_reported','exact','zai','glm-5.3',1,1,now-7200)])
    with sqlite3.connect(sidecar) as con:
        con.execute('ALTER TABLE usage_events DROP COLUMN created_at')
        con.execute('ALTER TABLE usage_events RENAME TO llm_usage_events')
    assert len(list(collect_hermes_usage(sidecar,'sol:hermes',since=now-60)))==1


def test_old_transcript_on_retry_list_bypasses_mtime_cutoff(tmp_path):
    import os
    p=tmp_path/'old.jsonl'
    p.write_text(json.dumps({'type':'assistant','sessionId':'s','timestamp':'2026-09-25T12:00:00Z',
        'message':{'id':'m1','model':'claude-opus','stop_reason':'end_turn',
        'usage':{'input_tokens':1,'output_tokens':2}}})+'\n')
    os.utime(p,(100,100))
    assert list(collect_claude_usage(tmp_path,'test:claude',since=1000))==[]
    assert len(list(collect_claude_usage(tmp_path,'test:claude',since=1000,retry_files=[str(p)])))==1


def json_rows(path: Path) -> list[dict]:
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return [json.loads(r[0]) for r in con.execute("SELECT canonical_json FROM facts")]
    finally:
        con.close()
