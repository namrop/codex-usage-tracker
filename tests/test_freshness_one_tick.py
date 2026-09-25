"""Staleness is one ledger tick (keeper, 2026-09-25, Discord unified usage
ledger thread: "also why does it always say opencode is stale? I think
staleness should only be reported if it is over 15 minutes, or one ledger
tick").

"Always stale" was a lane the quota collector stopped running on 2026-09-15
(aa583c3, OpenCode Go quota moved to the native lane): lane health keeps every
lane it has seen, so it read stale forever. It is now recorded as retired.
The write threshold is one collector tick, and a run in progress is a tick
in progress, not a missed one.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
import os

import codex_usage_tracker.dashboard as dashboard_module
from codex_usage_tracker import collector
from codex_usage_tracker.dashboard import create_app

NOW = datetime(2026, 7, 14, 12, 0, 0, tzinfo=timezone.utc)


class _Frozen(datetime):
    @classmethod
    def now(cls, tz=None):
        return cls(2026, 7, 14, 12, 0, 0, tzinfo=tz)


def _stamp(path, hour, minute):
    moment = datetime(2026, 7, 14, hour, minute, 0, tzinfo=timezone.utc).timestamp()
    path.touch()
    os.utime(path, (moment, moment))


def _client(tmp_path, monkeypatch, *, heartbeat, started=None, lanes=None):
    monkeypatch.setattr(dashboard_module, "datetime", _Frozen)
    usage = tmp_path / "usage.jsonl"
    usage.write_text("", encoding="utf-8")
    health = tmp_path / "health"
    health.mkdir(exist_ok=True)
    _stamp(health / "usage-collect.ok", *heartbeat)
    if started is not None:
        _stamp(health / "usage-collect.started", *started)
    if lanes is not None:
        (health / "lanes-usage.json").write_text(json.dumps(lanes), encoding="utf-8")
    return create_app(unified_usage_ledger=str(usage)).test_client()


def _freshness(client):
    return client.get("/api/unified-usage?hours=3").get_json()["freshness"]


def test_a_write_within_one_tick_is_fresh_and_past_it_stale(tmp_path, monkeypatch):
    assert _freshness(_client(tmp_path, monkeypatch, heartbeat=(11, 44)))["heartbeat_status"] == "fresh"
    stale = _freshness(_client(tmp_path, monkeypatch, heartbeat=(11, 42)))
    assert stale["heartbeat_status"] == "stale" and stale["status"] == "stale"
    assert stale["max_age_seconds"] == 17 * 60


def test_a_run_in_progress_is_not_a_missed_tick_unless_it_hangs(tmp_path, monkeypatch):
    # A usage run takes 8-25 minutes: the last write is 30 minutes old, but a
    # run started 12 minutes ago.
    running = _freshness(_client(tmp_path, monkeypatch, heartbeat=(11, 30), started=(11, 48)))
    assert running["heartbeat_status"] == "fresh"
    assert running["run_in_progress_since"] == "2026-07-14T11:48:00Z"
    # A marker older than the last write is the previous run's: not in progress.
    done = _freshness(_client(tmp_path, monkeypatch, heartbeat=(11, 30), started=(11, 20)))
    assert done["heartbeat_status"] == "stale" and done["run_in_progress_since"] is None
    hung = _freshness(_client(tmp_path, monkeypatch, heartbeat=(10, 30), started=(10, 50)))
    assert hung["heartbeat_status"] == "stale"


def _lanes(updated_at, **lanes):
    return {"schema_version": 1, "scope": "usage", "updated_at": updated_at, "lanes": lanes}


def _lane(success, **extra):
    return {"last_run_at": "2026-07-14T11:50:00Z", "last_success_at": success, "rows_written": 1, **extra}


def test_a_lane_is_stale_when_it_missed_the_latest_run(tmp_path, monkeypatch):
    freshness = _freshness(_client(
        tmp_path, monkeypatch, heartbeat=(11, 50),
        lanes=_lanes(
            "2026-07-14T11:50:00Z",
            hermes=_lane("2026-07-14T11:50:00Z"),
            claude_code=_lane("2026-07-14T11:35:00Z", last_error_class="TimeoutError"),
        ),
    ))
    assert freshness["heartbeat_status"] == "fresh"
    assert freshness["stale_lanes"] == ["claude_code"]
    assert freshness["status"] == "stale"


def test_a_retired_lane_is_listed_with_its_reason_and_never_stale(tmp_path, monkeypatch):
    freshness = _freshness(_client(
        tmp_path, monkeypatch, heartbeat=(11, 50),
        lanes=_lanes(
            "2026-07-14T11:50:00Z",
            hermes=_lane("2026-07-14T11:50:00Z"),
            opencode=_lane("2026-07-04T04:35:00Z", retired="retired 2026-07-04: moved"),
        ),
    ))
    assert freshness["status"] == "fresh"
    assert freshness["stale_lanes"] == []
    assert freshness["retired_lanes"] == ["opencode"]
    by_lane = {lane["lane"]: lane for lane in freshness["lanes"]}
    assert by_lane["opencode"]["status"] == "retired"
    assert by_lane["opencode"]["retired"] == "retired 2026-07-04: moved"


def test_the_collector_records_the_retired_quota_lane(tmp_path):
    health = tmp_path / "health"
    health.mkdir()
    (health / "lanes-quota.json").write_text(json.dumps({
        "schema_version": 1, "scope": "quota", "updated_at": "2026-09-15T04:35:07Z",
        "lanes": {"opencode": {"last_run_at": "2026-09-15T04:35:07Z", "last_success_at": "2026-09-15T04:35:07Z"}},
    }), encoding="utf-8")
    collector.update_lane_health(health, "quota", {"opencode_go_quota": {"succeeded": True, "rows_written": 3}},
                                 now="2026-09-25T09:50:03Z")
    lanes = json.loads((health / "lanes-quota.json").read_text(encoding="utf-8"))["lanes"]
    assert lanes["opencode"]["retired"].startswith("retired 2026-09-15")
    assert lanes["opencode"]["last_success_at"] == "2026-09-15T04:35:07Z"  # history kept
    assert "retired" not in lanes["opencode_go_quota"]
    # A lane not on the retired list that stops running still goes stale.
    collector.update_lane_health(health, "quota", {"opencode_go_quota": {"succeeded": True}}, now="2026-09-25T10:05:03Z")
    assert "retired" not in json.loads((health / "lanes-quota.json").read_text(encoding="utf-8"))["lanes"]["opencode_go_quota"]


def test_the_collector_marks_a_run_started(tmp_path):
    touched = collector.mark_run_started(tmp_path, "usage")
    assert [path.name for path in touched] == ["usage-collect.started"]
    assert (tmp_path / "usage-collect.started").exists()
