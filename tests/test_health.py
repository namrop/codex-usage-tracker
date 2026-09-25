"""The ledger health endpoint (keeper, 2026-09-25, unified usage ledger thread:
"the AI usage ledger should have a health endpoint itself ... is the service
up? Are the ledgers, the watchers, running? Is that working?")."""
from __future__ import annotations

import json
import shutil
import subprocess
import time
from datetime import datetime, timedelta, timezone

import pytest

from codex_usage_tracker import health
from codex_usage_tracker.canonical_ledger import append_sqlite_facts
from codex_usage_tracker.dashboard import create_app

NOW = time.time()


def _systemctl_output(overrides: dict[str, dict[str, str]] | None = None):
    overrides = overrides or {}

    def show(units):
        blocks = []
        for unit in units:
            fields = {"Id": unit, "LoadState": "loaded"}
            if unit.endswith(".timer"):
                fields.update(ActiveState="active", SubState="waiting",
                              LastTriggerUSec=f"@{int(NOW) - 300}", NextElapseUSecRealtime=f"@{int(NOW) + 600}")
            elif unit in health.RUNNING_SERVICES:
                fields.update(ActiveState="active", SubState="running", Result="success",
                              ActiveEnterTimestamp=f"@{int(NOW) - 3600}")
            else:
                fields.update(ActiveState="inactive", SubState="dead", Result="success",
                              ExecMainExitTimestamp=f"@{int(NOW) - 290}")
            fields.update(overrides.get(unit, {}))
            blocks.append("\n".join(f"{key}={value}" for key, value in fields.items()))
        return "\n\n".join(blocks) + "\n"

    return show


def _jobs(tmp_path, *, status="ok", minutes_ago=10, enabled=True):
    last = (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).isoformat()
    path = tmp_path / "jobs.json"
    path.write_text(json.dumps({"jobs": [
        {"id": "67e882e9032a", "name": "Unified AI usage ledger silent watchdog", "script": "ai_usage_watchdog.py",
         "enabled": enabled, "schedule": {"kind": "interval", "minutes": 30}, "last_run_at": last,
         "last_status": status, "failure_streak": 0, "next_run_at": last},
        {"id": "other", "name": "Unrelated", "script": "masthead.py", "enabled": True,
         "schedule": {"kind": "interval", "minutes": 30}, "last_run_at": last, "last_status": "error"},
    ]}))
    return str(path)


def _ledger(tmp_path):
    path = tmp_path / "usage.sqlite3"
    append_sqlite_facts(path, [{
        "fact_type": "usage_event_v1", "schema_version": 1, "source_namespace": "t", "source_event_id": "e1",
        "harness": "hermes", "purpose": "main", "record_kind": "api_attempt",
        "occurred_at": "2026-09-25T12:00:00Z", "recorded_at": "2026-09-25T12:00:00Z",
        "usage_source": "provider_reported", "usage_completeness": "complete", "measurement_confidence": "exact",
        "cost_status": "included", "provider": "zai", "model_requested": "glm-5.3", "input_tokens": 1,
    }], fact_type="usage_event_v1")
    return str(path)


FRESH = {"status": "fresh", "heartbeat_status": "fresh", "latest_at": "2026-09-25T12:00:00Z", "stale_lanes": []}


def test_all_green_reads_ok(tmp_path):
    payload, code = health.build_health(
        ledgers={"usage": (_ledger(tmp_path), FRESH), "billing": (None, None)},
        redis_socket=None, systemctl=_systemctl_output(), cron_jobs_path=_jobs(tmp_path), clock=lambda: NOW,
    )
    assert (payload["status"], code) == ("ok", 200), payload["problems"]
    usage = payload["ledgers"][0]
    assert usage["readable"] and usage["rows_high_water"] == 1 and usage["collector"]["status"] == "fresh"
    assert payload["ledgers"][1]["status"] == "not_configured"
    assert [row["script"] for row in payload["watchers"]["jobs"]] == ["ai_usage_watchdog.py"]
    assert {row["timer"] for row in payload["units"]["timers"]} == set(health.TIMERS)
    assert payload["service"]["status"] == "up"


def test_each_failure_is_named_and_degrades(tmp_path):
    payload, code = health.build_health(
        ledgers={"usage": (_ledger(tmp_path), {**FRESH, "status": "stale", "stale_lanes": ["hermes"]})},
        redis_socket=None,
        systemctl=_systemctl_output({
            "ai-usage-view-render.service": {"ActiveState": "failed"},
            "ai-quota-collect.service": {"Result": "exit-code"},
            "ai-usage-collect.timer": {"ActiveState": "inactive"},
        }),
        cron_jobs_path=_jobs(tmp_path, minutes_ago=90),
        clock=lambda: NOW,
    )
    assert (payload["status"], code) == ("degraded", 200)
    problems = " | ".join(payload["problems"])
    for fragment in ("usage collector is stale", "usage lane hermes is stale", "ai-usage-view-render.service is failed",
                     "ai-quota-collect.service last run: exit-code", "ai-usage-collect.timer is inactive",
                     "watchdog is overdue"):
        assert fragment in problems, fragment
    assert "Unrelated" not in problems


def test_unreadable_usage_ledger_is_down_with_503(tmp_path):
    payload, code = health.build_health(
        ledgers={"usage": (str(tmp_path / "missing.sqlite3"), None)},
        redis_socket=None, systemctl=_systemctl_output(), cron_jobs_path=_jobs(tmp_path), clock=lambda: NOW,
    )
    assert (payload["status"], code) == ("down", 503)


def test_systemd_and_cron_that_cannot_be_read_say_so(tmp_path):
    def broken(_units):
        raise RuntimeError("no bus")

    payload, _code = health.build_health(
        ledgers={"usage": (_ledger(tmp_path), FRESH)}, redis_socket=None, systemctl=broken,
        cron_jobs_path=str(tmp_path / "nope.json"), clock=lambda: NOW,
    )
    assert payload["status"] == "degraded"
    assert payload["units"]["status"] == "unknown" and payload["watchers"]["status"] == "unknown"


def test_failed_watcher_and_disabled_watcher(tmp_path):
    failed = health.watchers_health(_jobs(tmp_path, status="error"), now=NOW)
    assert failed["problems"] == ["watcher Unified AI usage ledger silent watchdog last run: error"]
    disabled = health.watchers_health(_jobs(tmp_path, enabled=False), now=NOW)
    assert "is disabled" in disabled["problems"][0]


def test_a_cron_expression_watcher_is_overdue_when_its_next_run_passed(tmp_path):
    """The monthly receipts job (keeper msg 1553058626350874726) runs on a cron
    expression, so there is no interval to double; its next run is the clock."""
    def jobs(next_run):
        path = tmp_path / "cron.json"
        path.write_text(json.dumps({"jobs": [
            {"id": "m1", "name": "AI subscription receipts monthly", "script": "ai_usage_receipts_monthly.py",
             "enabled": True, "schedule": {"kind": "cron", "expr": "0 9 10 * *"}, "last_run_at": None,
             "last_status": None, "next_run_at": next_run.isoformat()},
        ]}))
        return str(path)
    now = datetime.now(timezone.utc)
    assert health.watchers_health(jobs(now + timedelta(days=12)), now=NOW)["status"] == "ok"
    late = health.watchers_health(jobs(now - timedelta(hours=3)), now=NOW)
    assert late["problems"] == ["watcher AI subscription receipts monthly is overdue (last run None)"]


def test_endpoint_is_wired_and_uncached(tmp_path, monkeypatch):
    monkeypatch.setattr(health, "run_systemctl", _systemctl_output())
    monkeypatch.setenv("AI_USAGE_HERMES_CRON_JOBS", _jobs(tmp_path))
    monkeypatch.delenv("AI_USAGE_REDIS_SOCKET", raising=False)
    client = create_app(unified_usage_ledger=_ledger(tmp_path)).test_client()
    response = client.get("/api/health")
    data = response.get_json()
    assert response.headers["Cache-Control"] == "no-store"
    assert {row["name"] for row in data["ledgers"]} == {"usage", "quota", "billing", "search"}
    assert data["redis"]["status"] == "not_configured"
    assert data["watchers"]["status"] == "ok"


REDIS_SERVER = shutil.which("redis-server")


@pytest.mark.skipif(REDIS_SERVER is None, reason="redis-server not on PATH")
def test_redis_section_names_missing_copies_and_views(tmp_path):
    redis = pytest.importorskip("redis")
    from codex_usage_tracker import usage_cache

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
        usage_cache._clients.clear()
        client = redis.Redis(unix_socket_path=str(socket_path))
        client.set(usage_cache.META_KEY, json.dumps({"refreshed_at_epoch": NOW - 60}))
        section = health.redis_health(str(socket_path), now=NOW)
        assert section["copies"]["usage_rows"]["age_seconds"] == 60
        assert "redis copy quota_rows is missing" in section["problems"]
        assert any(problem.startswith("pre-built /api/unified-usage") for problem in section["problems"])
        assert section["status"] == "stale"
    finally:
        usage_cache._clients.clear()
        process.terminate()
        process.wait(timeout=10)
    assert health.redis_health(str(tmp_path / "gone.sock"), now=NOW)["status"] == "down"
