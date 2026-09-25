"""Redis cache of recent elected usage rows (keeper, 2026-09-25, msg 1552951677286355045).

Runs a real redis-server on a private unix socket; no mocks. The core claim:
for any window inside the cached span, the cache answers exactly what the
SQLite election answers, rows and identity stats. Every way the cache cannot
answer must fall back (``None``), never answer wrong.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import os
import shutil
import subprocess
import time

import pytest

from codex_usage_tracker import usage_cache
from codex_usage_tracker.canonical_ledger import append_sqlite_facts, query_sqlite_physical_requests
from codex_usage_tracker.dashboard import create_app

redis = pytest.importorskip("redis")
REDIS_SERVER = shutil.which("redis-server")
pytestmark = pytest.mark.skipif(REDIS_SERVER is None, reason="redis-server not on PATH")

NOW = datetime.now(timezone.utc).replace(microsecond=0)


def _at(hours_ago: float, seconds: int = 0) -> str:
    return (NOW - timedelta(hours=hours_ago) + timedelta(seconds=seconds)).isoformat().replace("+00:00", "Z")


def _claude(session, msg, occurred_at, *, namespace="sol:claude-code", **updates):
    row = {
        "fact_type": "usage_event_v1", "schema_version": 1,
        "source_namespace": namespace, "source_event_id": f"{session}:{msg}",
        "harness": "claude_code", "purpose": "main", "record_kind": "api_attempt",
        "occurred_at": occurred_at, "recorded_at": occurred_at,
        "usage_source": "provider_reported", "usage_completeness": "complete",
        "measurement_confidence": "exact", "cost_status": "included",
        "provider": "anthropic", "model_requested": "claude-opus-5-5", "model_reported": "claude-opus-5-5",
        "session_id": session,
        "input_tokens": 2, "cache_read_tokens": 1000, "cache_write_tokens": 0,
        "output_tokens": 50, "reasoning_tokens": 0,
    }
    row.update(updates)
    return row


def _hermes(event, occurred_at, **updates):
    row = {
        "fact_type": "usage_event_v1", "schema_version": 1,
        "source_namespace": "sol:hermes", "source_event_id": event,
        "harness": "hermes", "purpose": "main", "record_kind": "api_attempt",
        "occurred_at": occurred_at, "recorded_at": occurred_at,
        "usage_source": "provider_reported", "usage_completeness": "unknown",
        "measurement_confidence": "exact", "cost_status": "included",
        "provider": "custom:meridian-yugen", "model_requested": "claude-opus-5-5",
        "input_tokens": 2, "cache_read_tokens": 1000, "cache_write_tokens": 0,
        "output_tokens": 50, "reasoning_tokens": 0,
    }
    row.update(updates)
    return row


def _facts():
    facts = []
    # Fork copies: one request seen in five transcripts, same timestamp.
    facts += [_claude(f"fork-{index}", "msg_01AAAAAAAAAAAAAAAAAAAAAA", _at(30)) for index in range(5)]
    # Distinct requests across the span, both accounts.
    facts += [_claude("s1", f"msg_01D{index:021d}", _at(hours)) for index, hours in enumerate((2, 5, 20, 26, 100, 150))]
    facts += [_claude("s2", "msg_01S0000000000000000000", _at(3), namespace="sol:claude-code-secondary")]
    # A relayed Hermes call and its SDK receipt, one second apart, well inside windows.
    facts += [
        _claude("sdk", "msg_01GGGGGGGGGGGGGGGGGGGGGG", _at(10)),
        _hermes("evt-g", _at(10, 1), provider_request_id="msg_01GGGGGGGGGGGGGGGGGGGGGG", upstream_provider="anthropic"),
    ]
    # Id-less Hermes rows never collapse; one on another provider.
    facts += [_hermes(f"evt-{index}", _at(hours)) for index, hours in enumerate((1, 4, 40))]
    facts += [_hermes("evt-zai", _at(6), provider="zai", model_requested="glm-5.3")]
    # A payload conflict and a timestamp split inside one source.
    facts += [
        _claude("c1", "msg_01CCCCCCCCCCCCCCCCCCCCCC", _at(12), output_tokens=50),
        _claude("c2", "msg_01CCCCCCCCCCCCCCCCCCCCCC", _at(12), output_tokens=51),
        _claude("t1", "msg_01TTTTTTTTTTTTTTTTTTTTTT", _at(50)),
        _claude("t2", "msg_01TTTTTTTTTTTTTTTTTTTTTT", _at(50, 5)),
    ]
    # Older than the cached span.
    facts += [_claude("old", "msg_01OOOOOOOOOOOOOOOOOOOOOO", _at(250))]
    return facts


@pytest.fixture()
def redis_socket(tmp_path):
    socket_path = tmp_path / "redis.sock"
    process = subprocess.Popen(
        [REDIS_SERVER, "--port", "0", "--unixsocket", str(socket_path), "--unixsocketperm", "700",
         "--save", "", "--appendonly", "no", "--dir", str(tmp_path)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        for _ in range(100):
            if socket_path.exists():
                break
            time.sleep(0.05)
        client = redis.Redis(unix_socket_path=str(socket_path))
        client.ping()
        usage_cache._clients.clear()
        yield str(socket_path)
    finally:
        usage_cache._clients.clear()
        process.terminate()
        process.wait(timeout=10)


@pytest.fixture()
def ledger(tmp_path):
    path = tmp_path / "usage.sqlite3"
    append_sqlite_facts(path, _facts(), fact_type="usage_event_v1")
    return path


def _sql(ledger, cutoff, before, filters, order="asc"):
    return query_sqlite_physical_requests(
        ledger,
        filters=filters,
        occurred_or_observed_at_gte=cutoff.isoformat(),
        occurred_or_observed_at_lt=before.isoformat() if before else None,
        order=order,
        contract_validation=False,
    )


WINDOWS = [(5, None), (24, None), (168, None), (168, 24), (48, 12), (0.5, None)]
FILTERS = [{}, {"harness": "hermes"}, {"harness": "claude_code"}, {"provider": "anthropic"}, {"purpose": "main"}]


@pytest.mark.parametrize("hours,upper", WINDOWS)
@pytest.mark.parametrize("filters", FILTERS)
def test_cache_answers_exactly_what_sqlite_answers(ledger, redis_socket, hours, upper, filters):
    summary = usage_cache.refresh(ledger, redis_socket, span_hours=192)
    assert summary["status"] == "refreshed"
    cutoff = NOW - timedelta(hours=hours)
    before = NOW - timedelta(hours=upper) if upper is not None else None
    for order in ("asc", "desc"):
        cached = usage_cache.physical_requests(
            ledger, cutoff=cutoff, before=before, filters=filters, order=order, socket_path=redis_socket
        )
        assert cached is not None
        rows, stats = cached
        sql_rows, sql_stats = _sql(ledger, cutoff, before, filters, order)
        assert rows == sql_rows
        assert {key: stats[key] for key in sql_stats} == sql_stats
        assert stats["read_path"] == "redis" and stats["cache_matches_ledger"] is True


def test_refresh_counts_requests_not_copies(ledger, redis_socket):
    summary = usage_cache.refresh(ledger, redis_socket, span_hours=192)
    sql_rows, sql_stats = _sql(ledger, NOW - timedelta(hours=192), None, {})
    assert summary["rows"] == len(sql_rows) == sql_stats["physical_requests"]
    assert summary["observations"] == sql_stats["observations"]
    assert sql_stats["copies_suppressed"] >= 4 + 1 + 1 + 1  # forks, relay, conflict, split


def test_windows_the_cache_cannot_answer_fall_back(ledger, redis_socket, tmp_path, monkeypatch):
    usage_cache.refresh(ledger, redis_socket, span_hours=48)
    ask = lambda **kw: usage_cache.physical_requests(
        kw.pop("path", ledger), **{"cutoff": NOW - timedelta(hours=5), "before": None, "filters": {},
                                   "socket_path": redis_socket, **kw}
    )
    assert ask() is not None
    assert ask(cutoff=NOW - timedelta(hours=72)) is None  # starts before the cached span
    assert ask(cutoff=None) is None  # unbounded read
    assert ask(filters={"model": "x"}) is None  # not a typed filter: SQL raises its own error
    later = lambda: time.time() + 3 * 3600
    assert ask(clock=later) is not None  # old but the ledger has not moved: still exact
    other = tmp_path / "other.sqlite3"
    append_sqlite_facts(other, _facts()[:3], fact_type="usage_event_v1")
    assert ask(path=other) is None  # built from a different ledger file
    assert ask(socket_path=str(tmp_path / "absent.sock")) is None  # Redis unreachable
    monkeypatch.delenv(usage_cache.SOCKET_ENV, raising=False)
    assert usage_cache.physical_requests(ledger, cutoff=NOW - timedelta(hours=5), before=None, filters={}) is None
    append_sqlite_facts(ledger, [_hermes("evt-moved", _at(0.1))], fact_type="usage_event_v1")
    assert ask() is not None  # moved, but the copy is recent: served, flagged
    assert ask(clock=later) is None  # moved and the copy is past a collector cycle: refresher is behind


def test_refresh_skips_an_unchanged_ledger_and_retires_the_old_generation(ledger, redis_socket):
    first = usage_cache.refresh(ledger, redis_socket)
    assert usage_cache.refresh(ledger, redis_socket)["status"] == "unchanged"
    append_sqlite_facts(ledger, [_hermes("evt-new", _at(0.2))], fact_type="usage_event_v1")
    second = usage_cache.refresh(ledger, redis_socket, now=NOW + timedelta(seconds=5))
    assert second["status"] == "refreshed" and second["rows"] == first["rows"] + 1
    client = redis.Redis(unix_socket_path=redis_socket)
    old_ttl = client.ttl(f"{usage_cache.KEY_PREFIX}:rows:{first['generation']}")
    assert 0 < old_ttl <= usage_cache.OLD_GENERATION_TTL_SECONDS
    assert client.ttl(f"{usage_cache.KEY_PREFIX}:rows:{second['generation']}") == -1
    meta = json.loads(client.get(usage_cache.META_KEY))
    assert meta["generation"] == second["generation"]


def test_a_growing_ledger_is_flagged_until_the_next_refresh(ledger, redis_socket):
    usage_cache.refresh(ledger, redis_socket)
    append_sqlite_facts(ledger, [_hermes("evt-late", _at(0.1))], fact_type="usage_event_v1")
    rows, stats = usage_cache.physical_requests(
        ledger, cutoff=NOW - timedelta(hours=5), before=None, filters={}, socket_path=redis_socket
    )
    assert stats["cache_matches_ledger"] is False
    assert all(row["source_event_id"] != "evt-late" for row in rows)


def test_dashboard_serves_the_same_answer_from_redis(ledger, redis_socket, monkeypatch):
    usage_cache.refresh(ledger, redis_socket)
    client = create_app(unified_usage_ledger=str(ledger)).test_client()
    monkeypatch.delenv(usage_cache.SOCKET_ENV, raising=False)
    from_disk = client.get("/api/unified-usage?hours=168").get_json()
    monkeypatch.setenv(usage_cache.SOCKET_ENV, redis_socket)
    from_redis = client.get("/api/unified-usage?hours=168").get_json()
    assert from_redis["identity"]["read_path"] == "redis"
    assert "read_path" not in from_disk["identity"]
    for payload in (from_disk, from_redis):
        payload.pop("generated_at", None)
        for key in [k for k in payload["identity"] if k.startswith("cache_") or k == "read_path"]:
            payload["identity"].pop(key)
    assert from_redis == from_disk
