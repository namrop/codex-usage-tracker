"""The AI usage dashboard's Redis copy of what its panels read.

Keeper, Discord #gateway unified usage ledger thread, msg
1552951677286355045: "I would expect the loads to be much faster with only
the last week of ledger rows in memory. I was expecting the service to pull
from there instead of from disk. the writer can write to the DB as well as
disk, or the DB service can refresh from disk periodically. whatever makes
more sense". Then msg 1552965574315868321, for the panels the first copy did
not cover (weekly utilization and OpenCode Go from the quota ledger, the
ruling counts, rulings per dollar): "yes I want those on redis as well".

Chosen: refresh from disk. The SQLite ledgers and the Atrium stay the only
records; Redis holds derived copies that can be dropped and rebuilt at any
time, so it needs no persistence and cannot drift from its sources.
Write-through was rejected for three reasons:

* the collector commits once per run, and the dashboard's disk view only
  moves when that run checkpoints, so writing through gains no freshness;
* which observation represents a request depends on every copy in the window
  (a Hermes row arriving after its SDK receipt takes over the election), so
  a writer feeding Redis row by row would have to re-implement the election;
* the collector would gain a second store that can fail independently of the
  ledger it exists to fill.

Four copies, all under ``ai-usage:v1``:

* **Usage rows** (``meta``, ``rows:<generation>``): one elected row per
  physical request (usage contract v2) for the last eight days, each with its
  request's identity figures, so a window read rebuilds both the rows and the
  ``identity`` stats without touching SQLite. Election runs once over the
  whole cached span; a window read keeps the elected rows whose timestamp
  falls inside it. The SQL path elects inside each window instead. The two
  agree except when a request's copies sit on both sides of a window edge,
  which for a fork copy is the ``timestamp_disagreements`` alarm and for a
  relayed Hermes/SDK pair means the pair lands at the Hermes row's time.
* **Weekly price buckets** (``weekprice:<generation>``, same generation as
  the rows): the pre-pricing token sums ``rulings-per-dollar`` prices, one
  set per local ISO week for the last 26 weeks. The current week comes from
  the elected rows above; each closed week from its own SQLite election
  (``rulings.week_price_buckets_from_ledger``), computed once and carried
  forward, and recomputed when rows ingested since the last check land in
  it. Buckets carry a fingerprint of the code that shapes them (election,
  bucketing), so a dashboard running different code reads SQLite instead.
* **Quota observations** (``quota:meta``, ``quota:rows:<generation>``): every
  ``quota_observation_v1`` row of the last 36 days (weekly utilization's
  default four cycles need 35), in the ledger's own order.
* **Ruling scan** (``rulings``): the canon walk's result. It is refreshed by
  its own unit on every canon commit and on a timer, so it is not tied to the
  ledgers' generations.

A read returns ``None`` whenever the copy cannot answer exactly: not
configured, Redis unreachable, no meta, a different ledger file (or Atrium
root), a window starting before the cached span, a source that has moved on
while the copy aged past the allowed age, code that shapes the copy
differently, or a generation already gone. The dashboard then reads the
source as before. Answers from Redis say so (``read_path``).
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import hashlib
import json
import logging
import os
from pathlib import Path
import threading
import time
from typing import Any, Callable, Iterable

from . import canonical_ledger, list_pricing, rulings as rulings_module
from .canonical_ledger import (
    _SQLITE_QUERY_FILTER_COLUMNS,
    _timestamp,
    identity_stats_from_snapshot,
    snapshot_sqlite_facts,
    snapshot_sqlite_physical_requests,
    sqlite_ingestion_state,
)
from .rulings import (
    MAX_WEEKS as MAX_RULING_WEEKS,
    RulingScan,
    accumulate_weekly_price_buckets,
    price_buckets_as_list,
    price_buckets_from_list,
    recent_week_start_dates,
    scan_as_dict,
    scan_canon_rulings,
    scan_from_dict,
    week_bounds,
    week_price_buckets_from_ledger,
)

logger = logging.getLogger(__name__)

SOCKET_ENV = "AI_USAGE_REDIS_SOCKET"
MAX_AGE_ENV = "AI_USAGE_REDIS_MAX_AGE_SECONDS"
RULINGS_MAX_AGE_ENV = "AI_USAGE_REDIS_RULINGS_MAX_AGE_SECONDS"
DEFAULT_SOCKET = "/run/redis-ai-usage/redis.sock"
DEFAULT_SPAN_HOURS = 192  # the page's 168-hour view plus a day of margin
DEFAULT_QUOTA_SPAN_DAYS = 36  # weekly utilization's default read (35 days) plus one
DEFAULT_PRICE_WEEKS = 26
DEFAULT_MAX_AGE_SECONDS = 45 * 60  # a collector cycle is ~20-26 minutes
# The rulings unit runs on every canon commit and every five minutes.
DEFAULT_RULINGS_MAX_AGE_SECONDS = 15 * 60
KEY_PREFIX = "ai-usage:v1"
META_KEY = f"{KEY_PREFIX}:meta"
QUOTA_META_KEY = f"{KEY_PREFIX}:quota:meta"
RULINGS_KEY = f"{KEY_PREFIX}:rulings"
OLD_GENERATION_TTL_SECONDS = 600
ZADD_BATCH = 5_000
# 2: identity rule v2.1 (unkeyed relay observations ride the copy, never
# served as rows). A copy built by other code is never read.
SCHEMA = 2


def _fingerprint(*modules: Any) -> str:
    digest = hashlib.sha256()
    for module in modules:
        digest.update(Path(module.__file__).read_bytes())
    return digest.hexdigest()[:16]


# The code that decides which rows are elected and how they are bucketed.
# Both the refresher and the dashboard compute it from the modules they have
# loaded, so buckets built by other code are never priced as if they matched.
PRICE_FINGERPRINT = _fingerprint(canonical_ledger, list_pricing, rulings_module)
RULINGS_FINGERPRINT = _fingerprint(rulings_module)

_clients: dict[str, Any] = {}
_clients_lock = threading.Lock()


def _client(socket_path: str):
    import redis  # optional dependency; absent means no cache

    with _clients_lock:
        client = _clients.get(socket_path)
        if client is None:
            client = redis.Redis(
                unix_socket_path=socket_path,
                socket_timeout=10,
                socket_connect_timeout=2,
                decode_responses=True,
            )
            _clients[socket_path] = client
        return client


def _epoch(at: str) -> float:
    """Epoch seconds of a canonical UTC timestamp key (no zone, any fraction length)."""
    whole, _, fraction = at.partition(".")
    base = datetime.fromisoformat(whole).replace(tzinfo=timezone.utc).timestamp()
    return base + (float(f"0.{fraction}") if fraction else 0.0)


def _ledger_identity(path: Path) -> dict[str, int]:
    status = path.stat()
    return {"dev": status.st_dev, "ino": status.st_ino, "size": status.st_size, "mtime_ns": status.st_mtime_ns}


def _usage_member(entry: dict[str, Any]) -> str:
    head = json.dumps(
        {
            "k": entry["key"], "t": entry["at"], "i": entry["seq"], "c": entry["columns"],
            "n": entry["n"], "s": entry["stamp_split"], "x": entry["conflict"], "h": entry["harnesses"],
        },
        separators=(",", ":"),
        ensure_ascii=False,
    )
    # The stored canonical JSON goes in verbatim, not re-encoded.
    return head[:-1] + ',"f":' + entry["payload"] + "}"


_member = _usage_member  # historical name


def _fact_member(entry: dict[str, Any]) -> str:
    head = json.dumps(
        {"t": entry["at"], "i": entry["seq"], "c": entry["columns"]},
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return head[:-1] + ',"f":' + entry["payload"] + "}"


def _write_rows(client, rows_key: str, entries: list[dict[str, Any]], member: Callable[[dict[str, Any]], str]) -> None:
    client.delete(rows_key)
    pipe = client.pipeline(transaction=False)
    for start in range(0, len(entries), ZADD_BATCH):
        batch = entries[start:start + ZADD_BATCH]
        pipe.zadd(rows_key, {member(entry): _epoch(entry["at"]) for entry in batch})
        pipe.execute()
    if client.zcard(rows_key) != len(entries):
        client.delete(rows_key)
        raise RuntimeError("Redis row count does not match the snapshot; cache left on the previous generation")


def _unchanged(client, previous_meta: dict[str, Any] | None, identity: dict[str, int], now: datetime, skip_seconds: int) -> bool:
    return (
        previous_meta is not None
        and previous_meta.get("schema") == SCHEMA
        and previous_meta.get("ledger") == identity
        and client.exists(f"{previous_meta['rows_key']}")
        and now.timestamp() - previous_meta["refreshed_at_epoch"] < skip_seconds
    )


def _meta_usable(
    client, meta_key: str, ledger_path: str | Path, max_age_seconds: float, clock: Callable[[], float]
) -> tuple[dict[str, Any], float, bool] | None:
    """The copy's meta when it may answer: same ledger file, and either
    unchanged since the copy (exact however old) or moved on less than
    ``max_age_seconds`` ago. A ledger that moved while the copy aged past a
    collector cycle means the refresher is not keeping up."""
    raw_meta = client.get(meta_key)
    if not raw_meta:
        return None
    meta = json.loads(raw_meta)
    if meta.get("schema") != SCHEMA:
        return None
    identity = _ledger_identity(Path(ledger_path).expanduser())
    if (identity["dev"], identity["ino"]) != (meta["ledger"]["dev"], meta["ledger"]["ino"]):
        return None
    age = clock() - meta["refreshed_at_epoch"]
    matches = identity == meta["ledger"]
    if not matches and age > max_age_seconds:
        return None
    return meta, age, matches


# ── Usage rows and weekly price buckets ─────────────────────────────────────


def _week_start_key(start_day: date) -> str:
    return start_day.isoformat()


def _refresh_week_prices(
    client,
    ledger: Path,
    entries: list[dict[str, Any]],
    previous_meta: dict[str, Any] | None,
    *,
    now: datetime,
    price_weeks: int,
) -> tuple[dict[str, str], dict[str, Any]]:
    """Build this generation's weekly price buckets; returns (hash fields, meta)."""
    start_days = recent_week_start_dates(price_weeks, now)
    current, closed = start_days[-1], start_days[:-1]
    # Read the high-water mark BEFORE any closed-week read: every row at or
    # under it is inside the reads below, and any row above it is found as a
    # late arrival next time.
    previous_prices = (previous_meta or {}).get("prices") or {}
    carried: dict[str, str] = {}
    earliest_late: str | None = None
    reuse = (
        previous_prices.get("fingerprint") == PRICE_FINGERPRINT
        and previous_prices.get("high_water") is not None
        and previous_meta is not None
    )
    state = sqlite_ingestion_state(
        ledger, fact_type="usage_event_v1",
        ingested_after=previous_prices.get("high_water") if reuse else None,
    )
    if reuse:
        stored = client.hgetall(f"{KEY_PREFIX}:weekprice:{previous_meta['generation']}")
        closed_before = set(previous_prices.get("closed") or [])
        carried = {week: value for week, value in stored.items() if week in closed_before}
        earliest_late = state["earliest_after"]
    fields: dict[str, str] = {}
    recomputed: list[str] = []
    for start_day in closed:
        week = _week_start_key(start_day)
        week_end_key = _timestamp(week_bounds(start_day)[1].astimezone(timezone.utc).isoformat(), "week_end")[:-1]
        stale = earliest_late is not None and earliest_late < week_end_key
        if week in carried and not stale:
            fields[week] = carried[week]
            continue
        buckets, rows = week_price_buckets_from_ledger(ledger, start_day)
        fields[week] = json.dumps({"buckets": price_buckets_as_list(buckets), "rows": rows, "source": "week"}, separators=(",", ":"))
        recomputed.append(week)
    current_buckets = accumulate_weekly_price_buckets(
        (entry["fact"] for entry in entries if not entry["key"].startswith("relay|")), [current]
    )[0]
    fields[_week_start_key(current)] = json.dumps(
        {"buckets": price_buckets_as_list(current_buckets), "source": "span"}, separators=(",", ":")
    )
    meta = {
        "fingerprint": PRICE_FINGERPRINT,
        "high_water": state["ingestion_high_water"],
        "closed": [_week_start_key(day) for day in closed],
        "current": _week_start_key(current),
    }
    return fields, {**meta, "recomputed": recomputed}


def refresh(
    ledger_path: str | Path,
    socket_path: str = DEFAULT_SOCKET,
    *,
    span_hours: int = DEFAULT_SPAN_HOURS,
    force: bool = False,
    now: datetime | None = None,
    skip_if_unchanged_seconds: int = 1800,
    price_weeks: int = DEFAULT_PRICE_WEEKS,
) -> dict[str, Any]:
    """Rebuild the usage copy (rows and weekly prices); returns a journal summary."""
    ledger = Path(ledger_path).expanduser()
    now = now or datetime.now(timezone.utc)
    if not 1 <= price_weeks <= MAX_RULING_WEEKS:
        raise ValueError(f"price_weeks must be between 1 and {MAX_RULING_WEEKS}")
    client = _client(socket_path)
    identity = _ledger_identity(ledger)
    previous = client.get(META_KEY)
    previous_meta = json.loads(previous) if previous else None
    if previous_meta is not None:
        previous_meta.setdefault("rows_key", f"{KEY_PREFIX}:rows:{previous_meta['generation']}")
    if (
        not force
        and _unchanged(client, previous_meta, identity, now, skip_if_unchanged_seconds)
        and (previous_meta.get("prices") or {}).get("fingerprint") == PRICE_FINGERPRINT
    ):
        return {"status": "unchanged", "generation": previous_meta["generation"], "rows": previous_meta["rows"]}

    started = time.monotonic()
    since = (now - timedelta(hours=span_hours)).replace(minute=0, second=0, microsecond=0)
    entries, snapshot_meta = snapshot_sqlite_physical_requests(
        ledger, occurred_or_observed_at_gte=since.isoformat()
    )
    read_seconds = time.monotonic() - started
    price_fields, price_meta = _refresh_week_prices(
        client, ledger, entries, previous_meta, now=now, price_weeks=price_weeks
    )
    price_seconds = time.monotonic() - started - read_seconds
    generation = f"{int(now.timestamp() * 1000)}"
    rows_key = f"{KEY_PREFIX}:rows:{generation}"
    prices_key = f"{KEY_PREFIX}:weekprice:{generation}"
    _write_rows(client, rows_key, entries, _usage_member)
    client.delete(prices_key)
    client.hset(prices_key, mapping=price_fields)
    meta = {
        "schema": SCHEMA,
        "generation": generation,
        "rows_key": rows_key,
        "since": snapshot_meta.get("lower_bound") or _timestamp(since.isoformat(), "since"),
        "refreshed_at": now.isoformat(),
        "refreshed_at_epoch": now.timestamp(),
        "ledger": identity,
        "ingestion_high_water": snapshot_meta.get("ingestion_high_water"),
        "rows": len(entries),
        "observations": sum(entry["n"] for entry in entries),
        "relay_rows": sum(1 for entry in entries if entry["key"].startswith("relay|")),
        "prices": {key: value for key, value in price_meta.items() if key != "recomputed"},
    }
    transaction = client.pipeline(transaction=True)
    transaction.set(META_KEY, json.dumps(meta, separators=(",", ":")))
    if previous_meta is not None and previous_meta.get("generation") != generation:
        for key in (f"{KEY_PREFIX}:rows:{previous_meta['generation']}", f"{KEY_PREFIX}:weekprice:{previous_meta['generation']}"):
            transaction.expire(key, OLD_GENERATION_TTL_SECONDS)
    transaction.execute()
    return {
        "status": "refreshed",
        "generation": generation,
        "since": meta["since"],
        "rows": meta["rows"],
        "observations": meta["observations"],
        "relay_rows": meta["relay_rows"],
        "ingestion_high_water": meta["ingestion_high_water"],
        "price_weeks": len(price_fields),
        "price_weeks_recomputed": len(price_meta["recomputed"]),
        "read_seconds": round(read_seconds, 1),
        "price_seconds": round(price_seconds, 1),
        "total_seconds": round(time.monotonic() - started, 1),
        "used_memory": client.info("memory").get("used_memory_human"),
    }


def physical_requests(
    ledger_path: str | Path,
    *,
    cutoff: datetime | None,
    before: datetime | None,
    filters: dict[str, str],
    order: str = "asc",
    socket_path: str | None = None,
    max_age_seconds: float | None = None,
    clock: Callable[[], float] = time.time,
) -> tuple[list[dict[str, Any]], dict[str, Any]] | None:
    """The dashboard's usage read from Redis, or ``None`` to read SQLite instead.

    Same contract as ``query_sqlite_physical_requests``: rows elected before
    typed filters, identity stats for the whole window, rows in timestamp then
    ingestion order.
    """
    socket_path = socket_path or os.environ.get(SOCKET_ENV)
    if not socket_path or cutoff is None:
        return None
    if any(name not in _SQLITE_QUERY_FILTER_COLUMNS for name in filters):
        return None  # let the SQL path raise its own validation error
    if max_age_seconds is None:
        max_age_seconds = float(os.environ.get(MAX_AGE_ENV, DEFAULT_MAX_AGE_SECONDS))
    try:
        client = _client(socket_path)
        usable = _meta_usable(client, META_KEY, ledger_path, max_age_seconds, clock)
        if usable is None:
            return None
        meta, age, matches = usable
        lower = _timestamp(cutoff.isoformat(), "cutoff")[:-1]
        if lower < meta["since"][:-1]:
            return None
        upper = _timestamp(before.isoformat(), "before")[:-1] if before is not None else None
        rows_key = f"{KEY_PREFIX}:rows:{meta['generation']}"
        members = client.zrangebyscore(
            rows_key,
            _epoch(lower) - 1,
            "+inf" if upper is None else _epoch(upper) + 1,
        )
        if not members and not client.exists(rows_key):
            return None
    except Exception as exc:  # noqa: BLE001 - any cache fault means read the ledger
        logger.warning("usage cache unavailable, reading SQLite: %s: %s", type(exc).__name__, exc)
        return None

    window = []
    for member in members:
        entry = json.loads(member)
        if entry["t"] < lower or (upper is not None and entry["t"] >= upper):
            continue
        window.append(entry)
    window.sort(key=lambda entry: (entry["t"], entry["i"]), reverse=order == "desc")
    stats = identity_stats_from_snapshot(
        {"key": entry["k"], "n": entry["n"], "stamp_split": entry["s"], "conflict": entry["x"], "harnesses": entry["h"]}
        for entry in window
    )
    stats.update({
        "read_path": "redis",
        "cache_refreshed_at": meta["refreshed_at"],
        "cache_age_seconds": round(age),
        "cache_ingestion_high_water": meta["ingestion_high_water"],
        "cache_matches_ledger": matches,
    })
    rows = [
        entry["f"]
        for entry in window
        if not entry["k"].startswith("relay|")
        and all(entry["c"].get(name) == expected for name, expected in filters.items())
    ]
    return rows, stats


def week_price_buckets(
    ledger_path: str | Path,
    start_days: Iterable[date],
    *,
    socket_path: str | None = None,
    max_age_seconds: float | None = None,
    clock: Callable[[], float] = time.time,
) -> tuple[dict[date, dict[tuple[str, str, str], dict[str, Any]]], dict[str, Any]] | None:
    """Each requested week's price buckets from Redis, or ``None`` when any
    week is missing or the copy cannot answer (same rules as the rows)."""
    socket_path = socket_path or os.environ.get(SOCKET_ENV)
    if not socket_path:
        return None
    if max_age_seconds is None:
        max_age_seconds = float(os.environ.get(MAX_AGE_ENV, DEFAULT_MAX_AGE_SECONDS))
    days = list(start_days)
    try:
        client = _client(socket_path)
        usable = _meta_usable(client, META_KEY, ledger_path, max_age_seconds, clock)
        if usable is None:
            return None
        meta, age, matches = usable
        if (meta.get("prices") or {}).get("fingerprint") != PRICE_FINGERPRINT:
            return None
        values = client.hmget(f"{KEY_PREFIX}:weekprice:{meta['generation']}", [_week_start_key(day) for day in days])
    except Exception as exc:  # noqa: BLE001
        logger.warning("usage cache unavailable, reading SQLite: %s: %s", type(exc).__name__, exc)
        return None
    if any(value is None for value in values):
        return None
    buckets = {day: price_buckets_from_list(json.loads(value)["buckets"]) for day, value in zip(days, values)}
    return buckets, {
        "read_path": "redis",
        "cache_refreshed_at": meta["refreshed_at"],
        "cache_age_seconds": round(age),
        "cache_matches_ledger": matches,
    }


# ── Quota observations ─────────────────────────────────────────────────────


def refresh_quota(
    ledger_path: str | Path,
    socket_path: str = DEFAULT_SOCKET,
    *,
    span_days: int = DEFAULT_QUOTA_SPAN_DAYS,
    force: bool = False,
    now: datetime | None = None,
    skip_if_unchanged_seconds: int = 1800,
) -> dict[str, Any]:
    """Rebuild the quota copy from the quota ledger; returns a journal summary."""
    ledger = Path(ledger_path).expanduser()
    now = now or datetime.now(timezone.utc)
    client = _client(socket_path)
    identity = _ledger_identity(ledger)
    previous = client.get(QUOTA_META_KEY)
    previous_meta = json.loads(previous) if previous else None
    if not force and _unchanged(client, previous_meta, identity, now, skip_if_unchanged_seconds):
        return {"status": "unchanged", "generation": previous_meta["generation"], "rows": previous_meta["rows"]}
    started = time.monotonic()
    since = (now - timedelta(days=span_days)).replace(minute=0, second=0, microsecond=0)
    entries, snapshot_meta = snapshot_sqlite_facts(
        ledger, fact_type="quota_observation_v1", occurred_or_observed_at_gte=since.isoformat()
    )
    read_seconds = time.monotonic() - started
    generation = f"{int(now.timestamp() * 1000)}"
    rows_key = f"{KEY_PREFIX}:quota:rows:{generation}"
    _write_rows(client, rows_key, entries, _fact_member)
    meta = {
        "schema": SCHEMA,
        "generation": generation,
        "rows_key": rows_key,
        "since": snapshot_meta["lower_bound"],
        "refreshed_at": now.isoformat(),
        "refreshed_at_epoch": now.timestamp(),
        "ledger": identity,
        "ingestion_high_water": snapshot_meta.get("ingestion_high_water"),
        "rows": len(entries),
    }
    transaction = client.pipeline(transaction=True)
    transaction.set(QUOTA_META_KEY, json.dumps(meta, separators=(",", ":")))
    if previous_meta is not None and previous_meta.get("generation") != generation:
        transaction.expire(previous_meta["rows_key"], OLD_GENERATION_TTL_SECONDS)
    transaction.execute()
    return {
        "status": "refreshed",
        "generation": generation,
        "since": meta["since"],
        "rows": meta["rows"],
        "ingestion_high_water": meta["ingestion_high_water"],
        "read_seconds": round(read_seconds, 1),
        "total_seconds": round(time.monotonic() - started, 1),
    }


def quota_facts(
    ledger_path: str | Path,
    *,
    cutoff: datetime | None,
    before: datetime | None,
    filters: dict[str, str],
    order: str = "asc",
    socket_path: str | None = None,
    max_age_seconds: float | None = None,
    clock: Callable[[], float] = time.time,
) -> list[dict[str, Any]] | None:
    """The dashboard's quota read from Redis, or ``None`` to read SQLite.

    Same contract as ``query_sqlite_facts`` for ``quota_observation_v1``:
    ``[cutoff, before)`` on the stored timestamp key, typed-column filters,
    rows in timestamp then ingestion order.
    """
    socket_path = socket_path or os.environ.get(SOCKET_ENV)
    if not socket_path or cutoff is None:
        return None
    if any(name not in _SQLITE_QUERY_FILTER_COLUMNS for name in filters):
        return None
    if max_age_seconds is None:
        max_age_seconds = float(os.environ.get(MAX_AGE_ENV, DEFAULT_MAX_AGE_SECONDS))
    try:
        client = _client(socket_path)
        usable = _meta_usable(client, QUOTA_META_KEY, ledger_path, max_age_seconds, clock)
        if usable is None:
            return None
        meta, _age, _matches = usable
        lower = _timestamp(cutoff.isoformat(), "cutoff")[:-1]
        if lower < meta["since"][:-1]:
            return None
        upper = _timestamp(before.isoformat(), "before")[:-1] if before is not None else None
        members = client.zrangebyscore(
            meta["rows_key"], _epoch(lower) - 1, "+inf" if upper is None else _epoch(upper) + 1
        )
        if not members and not client.exists(meta["rows_key"]):
            return None
    except Exception as exc:  # noqa: BLE001
        logger.warning("quota cache unavailable, reading SQLite: %s: %s", type(exc).__name__, exc)
        return None
    window = []
    for member in members:
        entry = json.loads(member)
        if entry["t"] < lower or (upper is not None and entry["t"] >= upper):
            continue
        if all(entry["c"].get(name) == expected for name, expected in filters.items()):
            window.append(entry)
    window.sort(key=lambda entry: (entry["t"], entry["i"]), reverse=order == "desc")
    return [entry["f"] for entry in window]


# ── Ruling scan ────────────────────────────────────────────────────────────


def refresh_rulings(
    atrium_root: str | Path, socket_path: str = DEFAULT_SOCKET, *, now: datetime | None = None
) -> dict[str, Any]:
    """Walk the canon for ruling ledgers and store the scan."""
    now = now or datetime.now(timezone.utc)
    root = str(Path(atrium_root).expanduser())
    started = time.monotonic()
    scan = scan_canon_rulings(root)
    seconds = time.monotonic() - started
    record = {
        "schema": SCHEMA,
        "fingerprint": RULINGS_FINGERPRINT,
        "atrium_root": root,
        "refreshed_at": now.isoformat(),
        "refreshed_at_epoch": now.timestamp(),
        "scan_seconds": round(seconds, 2),
        "scan": scan_as_dict(scan),
    }
    _client(socket_path).set(RULINGS_KEY, json.dumps(record, separators=(",", ":"), ensure_ascii=False))
    return {
        "status": "refreshed",
        "rulings": len(scan.rulings),
        "ledgers_scanned": scan.ledgers_scanned,
        "parse_errors": scan.parse_error_count,
        "scan_seconds": round(seconds, 2),
    }


def rulings_scan(
    atrium_root: str | Path,
    *,
    socket_path: str | None = None,
    max_age_seconds: float | None = None,
    clock: Callable[[], float] = time.time,
) -> tuple[RulingScan, dict[str, Any]] | None:
    """The stored canon scan, or ``None`` to walk the canon instead."""
    socket_path = socket_path or os.environ.get(SOCKET_ENV)
    if not socket_path:
        return None
    if max_age_seconds is None:
        max_age_seconds = float(os.environ.get(RULINGS_MAX_AGE_ENV, DEFAULT_RULINGS_MAX_AGE_SECONDS))
    try:
        raw = _client(socket_path).get(RULINGS_KEY)
        if not raw:
            return None
        record = json.loads(raw)
        if (
            record.get("schema") != SCHEMA
            or record.get("fingerprint") != RULINGS_FINGERPRINT
            or record.get("atrium_root") != str(Path(atrium_root).expanduser())
        ):
            return None
        age = clock() - record["refreshed_at_epoch"]
        if age > max_age_seconds:
            return None
        scan = scan_from_dict(record["scan"])
    except Exception as exc:  # noqa: BLE001
        logger.warning("rulings cache unavailable, walking the canon: %s: %s", type(exc).__name__, exc)
        return None
    return scan, {"read_path": "redis", "scanned_at": record["refreshed_at"], "scan_age_seconds": round(age)}
