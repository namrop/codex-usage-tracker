"""Health of the AI usage ledger system, in one answer.

Keeper, 2026-09-25 (Discord unified usage ledger thread, a message sent
during the turn for msg 1553037883747598450): "the AI usage ledger should have a
health endpoint itself. The health endpoint should basically just talk about,
like, you know, is the service up? Are the ledgers, the watchers, running? Is
that working?"

``build_health`` answers five questions and names every problem it finds:

- service: this dashboard process is answering (it is, if you got a reply).
- ledgers: each configured private ledger opens read-only and has rows; the
  usage and quota collectors' heartbeats and lanes are fresh.
- redis: the cache answers, and its copies (usage rows, quota rows, the
  canon rulings scan, the page's pre-built answers) are recent.
- units: the systemd services that must stay up are up; every timer is
  scheduled and its service's last run succeeded.
- watchers: the Hermes no-agent watchdog jobs over this ledger ran on time
  and reported ok.

Overall: "down" when the usage ledger cannot be read (the dashboard cannot
answer usage questions at all); "degraded" when anything else is wrong or
cannot be checked; "ok" otherwise. It reads; it never repairs.
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

STARTED_AT = time.time()

# Units the fleet defines for this system (pharos-nixos-fleet
# hosts/sol/configuration.nix). Running services must be active; timers must
# be active with a next run, and their service's last run must have succeeded.
RUNNING_SERVICES = (
    "codex-dashboard.service",
    "ai-usage-view-render.service",
    "redis-ai-usage.service",
    "codex-usage-tracker.service",
)
TIMERS = (
    "ai-usage-collect.timer",
    "ai-quota-collect.timer",
    "ai-quota-bench.timer",
    "ai-usage-redis-refresh.timer",
    "ai-usage-rulings-refresh.timer",
    "ai-usage-public-projection-rsync.timer",
    "codex-usage-public-projection-rsync.timer",
)
SYSTEMCTL_CANDIDATES = ("/run/current-system/sw/bin/systemctl", "/usr/bin/systemctl", "systemctl")
HERMES_CRON_JOBS = "/var/lib/hermes/primary/cron/jobs.json"
# Hermes jobs are watchers of this ledger when their script is one of these.
WATCHER_SCRIPT_PREFIX = "ai_usage_"

CACHE_MAX_AGE_SECONDS = 45 * 60
VIEW_MAX_AGE_SECONDS = 150
RULINGS_MAX_AGE_SECONDS = 2 * 3600


def _iso(epoch: float | None) -> str | None:
    if epoch is None:
        return None
    return datetime.fromtimestamp(epoch, timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def run_systemctl(units: Iterable[str]) -> str:
    properties = "Id,LoadState,ActiveState,SubState,Result,LastTriggerUSec,NextElapseUSecRealtime,ExecMainExitTimestamp,ActiveEnterTimestamp"
    last_error: Exception | None = None
    for binary in SYSTEMCTL_CANDIDATES:
        try:
            return subprocess.run(
                [binary, "show", *units, "-p", properties, "--timestamp=unix", "--no-pager"],
                capture_output=True, text=True, timeout=5, check=True,
            ).stdout
        except (OSError, subprocess.SubprocessError) as exc:
            last_error = exc
    raise RuntimeError(f"systemctl unavailable: {last_error}")


def parse_systemctl(output: str) -> dict[str, dict[str, str]]:
    units: dict[str, dict[str, str]] = {}
    for block in output.strip().split("\n\n"):
        fields = dict(line.split("=", 1) for line in block.splitlines() if "=" in line)
        if fields.get("Id"):
            units[fields["Id"]] = fields
    return units


def _unix(value: str | None) -> float | None:
    text = (value or "").strip()
    if not text.startswith("@"):
        return None
    try:
        seconds = float(text[1:])
    except ValueError:
        return None
    return seconds if seconds > 0 else None


def _sqlite_ledger(path: Path) -> dict[str, Any]:
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=2)
    try:
        row = connection.execute("select max(ingestion_sequence), max(ingested_at) from facts").fetchone()
    finally:
        connection.close()
    return {"rows_high_water": row[0], "last_ingested_at": row[1]}


def ledger_health(
    name: str,
    path: str | None,
    *,
    freshness: Mapping[str, Any] | None,
) -> dict[str, Any]:
    entry: dict[str, Any] = {"name": name, "configured": bool(path)}
    problems: list[str] = []
    if not path:
        entry.update(status="not_configured")
        return entry
    target = Path(path).expanduser()
    entry["exists"] = target.exists()
    if not target.exists():
        entry.update(status="missing", problems=[f"{name} ledger file is missing"])
        return entry
    entry["size_bytes"] = target.stat().st_size
    if target.suffix in (".sqlite3", ".sqlite", ".db"):
        try:
            entry.update(_sqlite_ledger(target))
            entry["readable"] = True
        except sqlite3.Error as exc:
            entry.update(readable=False, status="unreadable", problems=[f"{name} ledger unreadable: {exc}"])
            return entry
    else:
        entry["readable"] = os.access(target, os.R_OK)
        if not entry["readable"]:
            entry.update(status="unreadable", problems=[f"{name} ledger unreadable"])
            return entry
    if freshness is not None:
        entry["collector"] = {
            key: freshness.get(key)
            for key in ("status", "heartbeat_status", "latest_at", "age_seconds", "run_started_at",
                        "stale_lanes", "never_lanes", "retired_lanes")
            if key in freshness
        }
        if freshness.get("status") != "fresh":
            problems.append(f"{name} collector is {freshness.get('status')} (last write {freshness.get('latest_at')})")
        for lane in freshness.get("stale_lanes") or []:
            problems.append(f"{name} lane {lane} is stale")
    entry["status"] = "ok" if not problems else "stale"
    entry["problems"] = problems
    return entry


def redis_health(socket_path: str | None, *, now: float) -> dict[str, Any]:
    from . import prebuilt_views, usage_cache

    if not socket_path:
        return {"status": "not_configured", "problems": []}
    problems: list[str] = []
    try:
        client = usage_cache._client(socket_path)
        client.ping()
    except Exception as exc:  # noqa: BLE001 - any failure is the answer
        return {"status": "down", "problems": [f"redis unreachable: {type(exc).__name__}: {exc}"]}

    def age_of(raw: str | None, epoch_key: str) -> tuple[float | None, str | None]:
        if not raw:
            return None, None
        try:
            record = json.loads(raw)
            epoch = float(record[epoch_key])
        except (ValueError, KeyError, TypeError):
            return None, None
        return now - epoch, _iso(epoch)

    copies: dict[str, Any] = {}
    for name, key, limit in (
        ("usage_rows", usage_cache.META_KEY, CACHE_MAX_AGE_SECONDS),
        ("quota_rows", usage_cache.QUOTA_META_KEY, CACHE_MAX_AGE_SECONDS),
        ("rulings_scan", usage_cache.RULINGS_KEY, RULINGS_MAX_AGE_SECONDS),
    ):
        age, at = age_of(client.get(key), "refreshed_at_epoch")
        copies[name] = {"refreshed_at": at, "age_seconds": None if age is None else round(age)}
        if age is None:
            problems.append(f"redis copy {name} is missing")
        elif age > limit:
            problems.append(f"redis copy {name} is {round(age / 60)} min old")
    views: dict[str, Any] = {}
    for view in prebuilt_views.VIEWS:
        path, _, query = view.partition("?")
        stored = client.hmget(prebuilt_views.view_key(path, query), "rendered_at_epoch")[0]
        age = None if stored is None else now - float(stored)
        views[view] = None if age is None else round(age)
        if age is None or age > VIEW_MAX_AGE_SECONDS:
            problems.append(f"pre-built {view} is {'missing' if age is None else f'{round(age)} s old'}")
    return {"status": "ok" if not problems else "stale", "copies": copies, "prebuilt_view_ages_seconds": views,
            "problems": problems}


def units_health(
    systemctl: Callable[[Iterable[str]], str],
    *,
    running: Iterable[str] = RUNNING_SERVICES,
    timers: Iterable[str] = TIMERS,
) -> dict[str, Any]:
    running, timers = tuple(running), tuple(timers)
    services_of_timers = tuple(timer.removesuffix(".timer") + ".service" for timer in timers)
    try:
        units = parse_systemctl(systemctl((*running, *timers, *services_of_timers)))
    except Exception as exc:  # noqa: BLE001
        return {"status": "unknown", "problems": [f"cannot read systemd state: {exc}"]}
    problems: list[str] = []
    services: list[dict[str, Any]] = []
    for name in running:
        unit = units.get(name, {})
        state = unit.get("ActiveState", "unknown")
        services.append({"unit": name, "active": state, "sub": unit.get("SubState"),
                         "since": _iso(_unix(unit.get("ActiveEnterTimestamp")))})
        if state != "active":
            problems.append(f"{name} is {state}")
    timer_rows: list[dict[str, Any]] = []
    for timer, service in zip(timers, services_of_timers):
        unit = units.get(timer, {})
        job = units.get(service, {})
        row = {
            "timer": timer,
            "active": unit.get("ActiveState", "unknown"),
            "last_trigger": _iso(_unix(unit.get("LastTriggerUSec"))),
            "next": _iso(_unix(unit.get("NextElapseUSecRealtime"))),
            "service": service,
            "service_state": job.get("ActiveState"),
            "last_result": job.get("Result"),
            "last_exit": _iso(_unix(job.get("ExecMainExitTimestamp"))),
        }
        timer_rows.append(row)
        if row["active"] != "active":
            problems.append(f"{timer} is {row['active']}")
        if job.get("Result") not in (None, "success"):
            problems.append(f"{service} last run: {job.get('Result')}")
    return {"status": "ok" if not problems else "degraded", "services": services, "timers": timer_rows,
            "problems": problems}


def watchers_health(jobs_path: str | None, *, now: float) -> dict[str, Any]:
    if not jobs_path:
        return {"status": "not_configured", "problems": []}
    try:
        raw = json.loads(Path(jobs_path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return {"status": "unknown", "problems": [f"cannot read Hermes cron jobs: {exc}"]}
    jobs = raw.get("jobs", raw) if isinstance(raw, dict) else raw
    if isinstance(jobs, dict):
        jobs = list(jobs.values())
    problems: list[str] = []
    rows: list[dict[str, Any]] = []
    for job in jobs if isinstance(jobs, list) else []:
        if not isinstance(job, dict) or not str(job.get("script") or "").startswith(WATCHER_SCRIPT_PREFIX):
            continue
        minutes = (job.get("schedule") or {}).get("minutes")
        last = job.get("last_run_at")
        try:
            last_epoch = datetime.fromisoformat(str(last)).timestamp() if last else None
        except ValueError:
            last_epoch = None
        overdue = bool(minutes and last_epoch is not None and now - last_epoch > 2 * 60 * float(minutes))
        row = {
            "id": job.get("id"), "name": job.get("name"), "script": job.get("script"),
            "enabled": job.get("enabled"), "every_minutes": minutes, "last_run_at": last,
            "last_status": job.get("last_status"), "failure_streak": job.get("failure_streak"),
            "next_run_at": job.get("next_run_at"), "overdue": overdue,
        }
        rows.append(row)
        label = job.get("name") or job.get("id")
        if not job.get("enabled"):
            problems.append(f"watcher {label} is disabled")
        elif job.get("last_status") not in (None, "ok"):
            problems.append(f"watcher {label} last run: {job.get('last_status')}")
        elif overdue:
            problems.append(f"watcher {label} is overdue (last run {last})")
    if not rows:
        problems.append("no ai_usage watcher jobs found in Hermes cron")
    return {"status": "ok" if not problems else "degraded", "jobs": rows, "problems": problems}


def build_health(
    *,
    ledgers: Mapping[str, tuple[str | None, Mapping[str, Any] | None]],
    redis_socket: str | None,
    systemctl: Callable[[Iterable[str]], str] | None = None,
    cron_jobs_path: str | None = HERMES_CRON_JOBS,
    clock: Callable[[], float] = time.time,
) -> tuple[dict[str, Any], int]:
    now = clock()
    systemctl = systemctl or run_systemctl
    ledger_rows = [ledger_health(name, path, freshness=freshness) for name, (path, freshness) in ledgers.items()]
    sections = {
        "redis": redis_health(redis_socket, now=now),
        "units": units_health(systemctl),
        "watchers": watchers_health(cron_jobs_path, now=now),
    }
    problems = [problem for row in ledger_rows for problem in row.get("problems", [])]
    for section in sections.values():
        problems.extend(section.get("problems", []))
    usage = next((row for row in ledger_rows if row["name"] == "usage"), None)
    if usage is not None and usage.get("status") in ("missing", "unreadable", "not_configured"):
        status = "down"
    elif problems:
        status = "degraded"
    else:
        status = "ok"
    payload = {
        "status": status,
        "checked_at": _iso(now),
        "service": {"status": "up", "pid": os.getpid(), "started_at": _iso(STARTED_AT),
                    "uptime_seconds": round(now - STARTED_AT)},
        "ledgers": ledger_rows,
        **sections,
        "problems": problems,
    }
    return payload, (503 if status == "down" else 200)
