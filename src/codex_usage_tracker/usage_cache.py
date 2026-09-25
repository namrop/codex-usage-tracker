"""Redis cache of the last eight days of elected usage rows, for the dashboard.

Keeper, Discord #gateway unified usage ledger thread, msg
1552951677286355045: "I would expect the loads to be much faster with only
the last week of ledger rows in memory. I was expecting the service to pull
from there instead of from disk. the writer can write to the DB as well as
disk, or the DB service can refresh from disk periodically. whatever makes
more sense".

Chosen: refresh from disk. SQLite stays the only record; Redis holds a
derived copy that can be dropped and rebuilt at any time, so it needs no
persistence and cannot drift from the ledger. Write-through was rejected for
three reasons:

* the collector commits once per run, and the dashboard's disk view only
  moves when that run checkpoints, so writing through gains no freshness;
* which observation represents a request depends on every copy in the window
  (a Hermes row arriving after its SDK receipt takes over the election), so
  a writer feeding Redis row by row would have to re-implement the election;
* the collector would gain a second store that can fail independently of the
  ledger it exists to fill.

What the cache holds is what the dashboard reads: one elected row per
physical request (usage contract v2), already integrity-checked, each with
its request's identity figures, so a window read rebuilds both the rows and
the ``identity`` stats without touching SQLite. Election runs once over the
whole cached span; a window read then keeps the elected rows whose timestamp
falls inside it. The SQL path elects inside each window instead. The two
agree except when a request's copies sit on both sides of a window edge,
which for a fork copy is the ``timestamp_disagreements`` alarm and for a
relayed Hermes/SDK pair means the pair lands at the Hermes row's time.

Keys, all under ``ai-usage:v1``:

* ``meta``: JSON naming the live generation, the span's lower bound, when it
  was built, and the ledger file it was built from (device, inode, size,
  mtime) with its ingestion high-water mark;
* ``rows:<generation>``: a sorted set; score is the row's epoch second,
  member a compact JSON entry. A new generation is written in full before
  ``meta`` names it; the previous one expires ten minutes later so readers
  mid-read never lose it.

A read returns ``None`` whenever the cache cannot answer exactly: not
configured, Redis unreachable, no meta, a different ledger file, a window
starting before the cached span, a ledger that has moved on while the copy
aged past the allowed age (45 minutes, about two collector cycles), or a
generation already gone. The dashboard then reads SQLite as before. Every
answer from Redis says so in the ``identity`` stats (``read_path``,
``cache_refreshed_at``, ``cache_matches_ledger``).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import logging
import os
from pathlib import Path
import threading
import time
from typing import Any, Callable

from .canonical_ledger import (
    _SQLITE_QUERY_FILTER_COLUMNS,
    _timestamp,
    identity_stats_from_snapshot,
    snapshot_sqlite_physical_requests,
)

logger = logging.getLogger(__name__)

SOCKET_ENV = "AI_USAGE_REDIS_SOCKET"
MAX_AGE_ENV = "AI_USAGE_REDIS_MAX_AGE_SECONDS"
DEFAULT_SOCKET = "/run/redis-ai-usage/redis.sock"
DEFAULT_SPAN_HOURS = 192  # the page's 168-hour view plus a day of margin
DEFAULT_MAX_AGE_SECONDS = 45 * 60  # a collector cycle is ~20-26 minutes
KEY_PREFIX = "ai-usage:v1"
META_KEY = f"{KEY_PREFIX}:meta"
OLD_GENERATION_TTL_SECONDS = 600
ZADD_BATCH = 5_000
SCHEMA = 1

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


def _member(entry: dict[str, Any]) -> str:
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


def refresh(
    ledger_path: str | Path,
    socket_path: str = DEFAULT_SOCKET,
    *,
    span_hours: int = DEFAULT_SPAN_HOURS,
    force: bool = False,
    now: datetime | None = None,
    skip_if_unchanged_seconds: int = 1800,
) -> dict[str, Any]:
    """Rebuild the cache from the ledger; returns a summary for the journal."""
    ledger = Path(ledger_path).expanduser()
    now = now or datetime.now(timezone.utc)
    client = _client(socket_path)
    identity = _ledger_identity(ledger)
    previous = client.get(META_KEY)
    previous_meta = json.loads(previous) if previous else None
    if (
        not force
        and previous_meta is not None
        and previous_meta.get("schema") == SCHEMA
        and previous_meta.get("ledger") == identity
        and client.exists(f"{KEY_PREFIX}:rows:{previous_meta['generation']}")
        and now.timestamp() - previous_meta["refreshed_at_epoch"] < skip_if_unchanged_seconds
    ):
        return {"status": "unchanged", "generation": previous_meta["generation"], "rows": previous_meta["rows"]}

    started = time.monotonic()
    since = (now - timedelta(hours=span_hours)).replace(minute=0, second=0, microsecond=0)
    entries, snapshot_meta = snapshot_sqlite_physical_requests(
        ledger, occurred_or_observed_at_gte=since.isoformat()
    )
    read_seconds = time.monotonic() - started
    generation = f"{int(now.timestamp() * 1000)}"
    rows_key = f"{KEY_PREFIX}:rows:{generation}"
    client.delete(rows_key)
    pipe = client.pipeline(transaction=False)
    for start in range(0, len(entries), ZADD_BATCH):
        batch = entries[start:start + ZADD_BATCH]
        pipe.zadd(rows_key, {_member(entry): _epoch(entry["at"]) for entry in batch})
        pipe.execute()
    if client.zcard(rows_key) != len(entries):
        client.delete(rows_key)
        raise RuntimeError("Redis row count does not match the snapshot; cache left on the previous generation")
    meta = {
        "schema": SCHEMA,
        "generation": generation,
        "since": snapshot_meta.get("lower_bound") or _timestamp(since.isoformat(), "since"),
        "refreshed_at": now.isoformat(),
        "refreshed_at_epoch": now.timestamp(),
        "ledger": identity,
        "ingestion_high_water": snapshot_meta.get("ingestion_high_water"),
        "rows": len(entries),
        "observations": sum(entry["n"] for entry in entries),
    }
    transaction = client.pipeline(transaction=True)
    transaction.set(META_KEY, json.dumps(meta, separators=(",", ":")))
    if previous_meta is not None and previous_meta.get("generation") != generation:
        transaction.expire(f"{KEY_PREFIX}:rows:{previous_meta['generation']}", OLD_GENERATION_TTL_SECONDS)
    transaction.execute()
    return {
        "status": "refreshed",
        "generation": generation,
        "since": meta["since"],
        "rows": meta["rows"],
        "observations": meta["observations"],
        "ingestion_high_water": meta["ingestion_high_water"],
        "read_seconds": round(read_seconds, 1),
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
        raw_meta = client.get(META_KEY)
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
        # An unchanged ledger means the copy is exact however old it is. A
        # ledger that moved on while the copy aged past a collector cycle
        # means the refresher is not keeping up: read the ledger.
        if not matches and age > max_age_seconds:
            return None
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
        if all(entry["c"].get(name) == expected for name, expected in filters.items())
    ]
    return rows, stats
