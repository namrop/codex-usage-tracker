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
    # Unkeyed relay observations (the id-less Meridian Hermes rows) ride the
    # copy for the stats and are never rows (identity rule v2.1).
    assert summary["relay_rows"] == sql_stats["relay_observations_without_id"] == 3
    assert summary["rows"] - summary["relay_rows"] == len(sql_rows) == sql_stats["physical_requests"]
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


# ── Keeper, msg 1552965574315868321: "yes I want those on redis as well" ──
# The quota ledger, the weekly price buckets rulings-per-dollar prices, and
# the canon ruling scan. Same claim as above: from Redis, exactly what the
# source answers; otherwise None.

from argparse import Namespace

from codex_usage_tracker import cli
from codex_usage_tracker.canonical_ledger import query_sqlite_facts
from codex_usage_tracker.rulings import (
    accumulate_weekly_price_buckets,
    recent_week_start_dates,
    scan_canon_rulings,
    week_bounds,
    week_price_buckets_from_ledger,
)


def _quota(source_id, observed_at, used, **updates):
    row = {
        "fact_type": "quota_observation_v1", "schema_version": 1,
        "source_namespace": "private:test-quota", "source_observation_id": source_id,
        "harness": "claude_code", "observed_at": observed_at, "provider": "anthropic",
        "quota_name": "seven_day", "quota_scope": "account", "window_kind": "rolling",
        "unit": "percent", "measurement_confidence": "exact", "limit_value": "100",
        "used_value": str(used), "remaining_value": str(100 - used),
        "resets_at": _at(-48), "account_ref": None, "provider_payload_ref": "/private/provider/payload",
    }
    row.update(updates)
    return row


@pytest.fixture()
def quota_ledger(tmp_path):
    path = tmp_path / "quota.sqlite3"
    rows = []
    for index, hours in enumerate((0.2, 1, 3, 30, 100, 400, 800, 900)):  # 900 h is past the 36-day span
        rows.append(_quota(f"week-{index}", _at(hours), 10 + index))
        rows.append(_quota(f"five-{index}", _at(hours), 20 + index, quota_name="five_hour", resets_at=_at(-2)))
    rows.append(_quota("codex-0", _at(3), 7, provider="openai-codex", harness="codex", quota_name="primary_window"))
    append_sqlite_facts(path, rows, fact_type="quota_observation_v1")
    return path


QUOTA_WINDOWS = [(5, None), (24, None), (168, None), (35 * 24, None), (168, 2)]
QUOTA_FILTERS = [{}, {"provider": "anthropic"}, {"quota_name": "five_hour"}, {"harness": "codex"}]


@pytest.mark.parametrize("hours,upper", QUOTA_WINDOWS)
@pytest.mark.parametrize("filters", QUOTA_FILTERS)
def test_quota_copy_answers_exactly_what_sqlite_answers(quota_ledger, redis_socket, hours, upper, filters):
    assert usage_cache.refresh_quota(quota_ledger, redis_socket, now=NOW)["status"] == "refreshed"
    cutoff = NOW - timedelta(hours=hours)
    before = NOW - timedelta(hours=upper) if upper is not None else None
    for order in ("asc", "desc"):
        cached = usage_cache.quota_facts(
            quota_ledger, cutoff=cutoff, before=before, filters=filters, order=order, socket_path=redis_socket
        )
        sql = query_sqlite_facts(
            quota_ledger, fact_type="quota_observation_v1", filters=filters,
            occurred_or_observed_at_gte=cutoff.isoformat(),
            occurred_or_observed_at_lt=before.isoformat() if before else None,
            order=order, contract_validation=False,
        )
        assert cached == sql


def test_quota_windows_the_copy_cannot_answer_fall_back(quota_ledger, redis_socket, tmp_path):
    usage_cache.refresh_quota(quota_ledger, redis_socket, now=NOW)
    ask = lambda **kw: usage_cache.quota_facts(
        kw.pop("path", quota_ledger),
        **{"cutoff": NOW - timedelta(hours=5), "before": None, "filters": {}, "socket_path": redis_socket, **kw},
    )
    assert ask() is not None
    assert ask(cutoff=NOW - timedelta(days=40)) is None  # before the copied span
    assert ask(cutoff=None) is None
    assert ask(filters={"model": "x"}) is None
    assert ask(socket_path=str(tmp_path / "absent.sock")) is None
    append_sqlite_facts(quota_ledger, [_quota("late", _at(0.1), 50)], fact_type="quota_observation_v1")
    assert ask() is not None  # moved, copy recent: served
    assert ask(clock=lambda: time.time() + 3 * 3600) is None  # moved and the copy is old


def test_quota_panels_serve_the_same_answer_from_redis(quota_ledger, redis_socket, monkeypatch):
    import codex_usage_tracker.dashboard as dashboard_module

    usage_cache.refresh_quota(quota_ledger, redis_socket)
    client = create_app(quota_ledger=str(quota_ledger)).test_client()
    real_query = dashboard_module.query_sqlite_facts

    def no_sqlite(*args, **kwargs):
        raise AssertionError("served from SQLite while the Redis copy could answer")

    for path in ("/api/weekly-utilization?weeks=4", "/api/subscriptions?hours=168&history=0", "/api/quota-burn?hours=6"):
        monkeypatch.delenv(usage_cache.SOCKET_ENV, raising=False)
        monkeypatch.setattr(dashboard_module, "query_sqlite_facts", real_query)
        from_disk = client.get(path)
        monkeypatch.setenv(usage_cache.SOCKET_ENV, redis_socket)
        monkeypatch.setattr(dashboard_module, "query_sqlite_facts", no_sqlite)
        from_redis = client.get(path)
        assert from_disk.status_code == from_redis.status_code == 200, path
        disk, cached = from_disk.get_json(), from_redis.get_json()
        for payload in (disk, cached):
            payload.pop("generated_at", None)
            (payload.get("freshness") or {}).pop("age_seconds", None)
        assert cached == disk, path


def test_week_prices_match_per_week_sqlite(ledger, redis_socket):
    summary = usage_cache.refresh(ledger, redis_socket, now=NOW, price_weeks=4)
    assert summary["price_weeks"] == 4 and summary["price_weeks_recomputed"] == 3
    start_days = recent_week_start_dates(4, NOW)
    cached, info = usage_cache.week_price_buckets(ledger, start_days, socket_path=redis_socket)
    assert info["read_path"] == "redis"
    for day in start_days[:-1]:
        assert cached[day] == week_price_buckets_from_ledger(ledger, day)[0]
    # The current week comes from the elected span rows; here no request
    # straddles its start, so it equals the week's own election.
    assert cached[start_days[-1]] == week_price_buckets_from_ledger(ledger, start_days[-1])[0]
    assert usage_cache.week_price_buckets(ledger, recent_week_start_dates(5, NOW), socket_path=redis_socket) is None


def test_a_late_row_in_a_closed_week_is_recomputed(ledger, redis_socket):
    usage_cache.refresh(ledger, redis_socket, now=NOW, price_weeks=4)
    closed = recent_week_start_dates(4, NOW)[1]
    late_at = (week_bounds(closed)[0] + timedelta(hours=1)).astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    append_sqlite_facts(ledger, [_hermes("evt-late-week", late_at, provider="zai", model_requested="glm-5.3")], fact_type="usage_event_v1")
    summary = usage_cache.refresh(ledger, redis_socket, now=NOW + timedelta(seconds=5), price_weeks=4)
    assert summary["price_weeks_recomputed"] >= 1
    cached, _ = usage_cache.week_price_buckets(ledger, [closed], socket_path=redis_socket)
    expected = week_price_buckets_from_ledger(ledger, closed)[0]
    assert cached[closed] == expected
    assert any(key[0] == "zai" for key in expected)
    # Nothing new: the next refresh carries every closed week forward.
    append_sqlite_facts(ledger, [_hermes("evt-now", _at(0.05))], fact_type="usage_event_v1")
    assert usage_cache.refresh(ledger, redis_socket, now=NOW + timedelta(seconds=10), price_weeks=4)["price_weeks_recomputed"] == 0


def test_week_prices_built_by_other_code_are_not_used(ledger, redis_socket, monkeypatch):
    usage_cache.refresh(ledger, redis_socket, now=NOW, price_weeks=4)
    days = recent_week_start_dates(2, NOW)
    assert usage_cache.week_price_buckets(ledger, days, socket_path=redis_socket) is not None
    monkeypatch.setattr(usage_cache, "PRICE_FINGERPRINT", "other-code")
    assert usage_cache.week_price_buckets(ledger, days, socket_path=redis_socket) is None


def test_rulings_per_dollar_same_from_redis_and_per_week_sqlite(ledger, redis_socket, monkeypatch, tmp_path):
    usage_cache.refresh(ledger, redis_socket)
    client = create_app(atrium_root=str(tmp_path), unified_usage_ledger=str(ledger)).test_client()
    monkeypatch.delenv(usage_cache.SOCKET_ENV, raising=False)
    from_disk = client.get("/api/rulings-per-dollar?weeks=8")
    monkeypatch.setenv(usage_cache.SOCKET_ENV, redis_socket)
    from_redis = client.get("/api/rulings-per-dollar?weeks=8")
    assert from_disk.status_code == from_redis.status_code == 200
    disk, cached = from_disk.get_json(), from_redis.get_json()
    assert disk["coverage"]["price_read"]["read_path"] == "sqlite_per_week"
    assert cached["coverage"]["price_read"]["read_path"] == "redis"
    assert sum(entry["total_tokens"] for entry in disk["series"]) > 0
    for payload in (disk, cached):
        payload.pop("generated_at")
        payload["coverage"].pop("price_read")
    assert cached == disk


RULING_UIDS = ("019938a5-1b2c-7def-8000-00000000000a", "019938a5-1b2c-7def-8000-00000000000b")


def _canon(root):
    path = root / "09_authors_notes" / "ruling_ledger.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    blocks = [
        f"uid: {RULING_UIDS[0]}\nruled_at: {_at(30)}\nclass: adoption\ngrade: ratified\nverbatim: \"yes\"",
        f"uid: {RULING_UIDS[1]}\nruled_at: {_at(200)}\nclass: adoption\napplies_to: [a, b]",
    ]
    body = "\n\n".join(f"```ruling\n{block}\n```" for block in blocks)
    path.write_text(f"---\nuid: 019938a5-1b2c-7def-8000-0000000000ff\nkind: note\nruling_ledger: v1\n---\n\n{body}\n", encoding="utf-8")
    return root


def test_ruling_scan_round_trips_through_redis(tmp_path, redis_socket, monkeypatch):
    root = _canon(tmp_path / "canon")
    summary = usage_cache.refresh_rulings(root, redis_socket)
    assert summary["rulings"] == 2 and summary["ledgers_scanned"] == 1
    scan, info = usage_cache.rulings_scan(root, socket_path=redis_socket)
    assert scan == scan_canon_rulings(root)
    assert info["read_path"] == "redis"
    assert usage_cache.rulings_scan(tmp_path / "elsewhere", socket_path=redis_socket) is None
    assert usage_cache.rulings_scan(root, socket_path=redis_socket, clock=lambda: time.time() + 3600) is None
    monkeypatch.setattr(usage_cache, "RULINGS_FINGERPRINT", "other-code")
    assert usage_cache.rulings_scan(root, socket_path=redis_socket) is None


def test_rulings_endpoint_same_from_redis_and_walk(tmp_path, redis_socket, monkeypatch):
    import codex_usage_tracker.dashboard as dashboard_module

    root = _canon(tmp_path / "canon")
    usage_cache.refresh_rulings(root, redis_socket)
    client = create_app(atrium_root=str(root)).test_client()
    monkeypatch.delenv(usage_cache.SOCKET_ENV, raising=False)
    dashboard_module._rulings_scan_cache.clear()
    walked = client.get("/api/rulings?weeks=8").get_json()
    monkeypatch.setenv(usage_cache.SOCKET_ENV, redis_socket)
    dashboard_module._rulings_scan_cache.clear()
    cached = client.get("/api/rulings?weeks=8").get_json()
    assert walked["coverage"].pop("scan_read")["read_path"] == "canon"
    assert cached["coverage"].pop("scan_read")["read_path"] == "redis"
    walked.pop("generated_at"), cached.pop("generated_at")
    assert cached == walked
    assert sum(entry["rulings_total"] for entry in cached["series"]) == 2


def test_cli_refreshes_each_copy_on_its_own(ledger, redis_socket, tmp_path, capsys):
    args = Namespace(ledger=str(ledger), socket=redis_socket, hours=192, force=False,
                     quota_ledger=str(tmp_path / "missing.sqlite3"))
    assert cli.cmd_usage_cache_refresh(args) == 1
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert [(line["copy"], line["status"]) for line in lines] == [("usage", "refreshed"), ("quota", "failed")]


# ── Keeper, msg 1552976803495280683: "option A is the better option. lets do
# it please" — pre-built answers for the page's fixed requests. ──

from codex_usage_tracker import prebuilt_views


def _apps(ledger, quota_ledger, monkeypatch, redis_socket):
    monkeypatch.setenv(usage_cache.SOCKET_ENV, redis_socket)
    monkeypatch.delenv(prebuilt_views.SERVE_ENV, raising=False)
    renderer = create_app(unified_usage_ledger=str(ledger), quota_ledger=str(quota_ledger))
    monkeypatch.setenv(prebuilt_views.SERVE_ENV, "serve")
    serving = create_app(unified_usage_ledger=str(ledger), quota_ledger=str(quota_ledger))
    monkeypatch.delenv(prebuilt_views.SERVE_ENV)
    return renderer, serving


def test_prebuilt_answers_are_the_routes_own_bytes(ledger, quota_ledger, redis_socket, monkeypatch):
    usage_cache.refresh(ledger, redis_socket)
    usage_cache.refresh_quota(quota_ledger, redis_socket)
    renderer, serving = _apps(ledger, quota_ledger, monkeypatch, redis_socket)
    summary = prebuilt_views.render_once(renderer, redis_socket)
    assert {view: entry["status"] for view, entry in summary["views"].items()} == {view: 200 for view in prebuilt_views.VIEWS}
    client = serving.test_client()
    for view in prebuilt_views.VIEWS:
        stored = redis.Redis(unix_socket_path=redis_socket, decode_responses=True).hget(
            prebuilt_views.view_key(*view.split("?", 1)) if "?" in view else prebuilt_views.view_key(view, ""), "body"
        )
        response = client.get(view)
        assert response.status_code == 200
        assert "X-AI-Usage-Prebuilt" in response.headers, view
        assert response.get_data(as_text=True) == stored
        assert response.headers["Cache-Control"] == "no-store"
    # Anything else is computed live.
    assert "X-AI-Usage-Prebuilt" not in client.get("/api/unified-usage?hours=6").headers
    assert "X-AI-Usage-Prebuilt" not in client.get("/api/unified-usage?hours=5&provider=anthropic").headers


def test_prebuilt_answer_matches_the_live_route_but_its_clock(ledger, quota_ledger, redis_socket, monkeypatch):
    usage_cache.refresh(ledger, redis_socket)
    usage_cache.refresh_quota(quota_ledger, redis_socket)
    renderer, serving = _apps(ledger, quota_ledger, monkeypatch, redis_socket)
    prebuilt_views.render_once(renderer, redis_socket)
    def drop_clock(value):
        # Every field that is "now" or "seconds since" ticks between the two
        # reads; a second boundary made this test flaky (seen 2026-09-25 on
        # /api/unified-usage?hours=5). Everything else must match exactly.
        if isinstance(value, dict):
            return {
                key: drop_clock(item) for key, item in value.items()
                if key not in ("generated_at", "as_of") and not key.endswith("age_seconds")
            }
        if isinstance(value, list):
            return [drop_clock(item) for item in value]
        return value

    for view in prebuilt_views.VIEWS:
        built = serving.test_client().get(view).get_json()
        live = renderer.test_client().get(view).get_json()
        assert drop_clock(built) == drop_clock(live), view


def test_an_old_or_missing_answer_hands_back_to_the_route(ledger, quota_ledger, redis_socket, monkeypatch):
    usage_cache.refresh(ledger, redis_socket)
    renderer, serving = _apps(ledger, quota_ledger, monkeypatch, redis_socket)
    prebuilt_views.render_once(renderer, redis_socket, views=("/api/unified-usage?hours=5",))
    assert prebuilt_views.lookup("/api/unified-usage", "hours=5", socket_path=redis_socket) is not None
    later = lambda: time.time() + prebuilt_views.DEFAULT_MAX_AGE_SECONDS + 1
    assert prebuilt_views.lookup("/api/unified-usage", "hours=5", socket_path=redis_socket, clock=later) is None
    assert prebuilt_views.lookup("/api/unified-usage", "hours=24", socket_path=redis_socket) is None  # not rendered
    assert prebuilt_views.lookup("/api/unified-usage", "hours=5", socket_path=str(ledger.parent / "absent.sock")) is None
    assert "X-AI-Usage-Prebuilt" not in renderer.test_client().get("/api/unified-usage?hours=5").headers


def test_the_renderer_never_serves_its_own_answers(ledger, quota_ledger, redis_socket, monkeypatch):
    usage_cache.refresh(ledger, redis_socket)
    _renderer, serving = _apps(ledger, quota_ledger, monkeypatch, redis_socket)
    with pytest.raises(RuntimeError):
        prebuilt_views.render_once(serving, redis_socket)


def test_a_rerender_carries_new_data(ledger, quota_ledger, redis_socket, monkeypatch):
    usage_cache.refresh(ledger, redis_socket, now=NOW)
    renderer, serving = _apps(ledger, quota_ledger, monkeypatch, redis_socket)
    prebuilt_views.render_once(renderer, redis_socket, views=("/api/unified-usage?hours=5",))
    before = serving.test_client().get("/api/unified-usage?hours=5").get_json()["identity"]["physical_requests"]
    append_sqlite_facts(ledger, [_hermes("evt-rerender", _at(2.5), provider="zai", model_requested="glm-5.3")], fact_type="usage_event_v1")
    usage_cache.refresh(ledger, redis_socket, now=NOW + timedelta(seconds=5))
    prebuilt_views.render_once(renderer, redis_socket, views=("/api/unified-usage?hours=5",))
    after = serving.test_client().get("/api/unified-usage?hours=5").get_json()["identity"]["physical_requests"]
    assert after == before + 1
