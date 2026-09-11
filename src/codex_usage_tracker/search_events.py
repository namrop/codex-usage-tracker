"""Web search/extract call accounting: the Hermes search audit feed → canon.

Keeper ruling 2026-09-10 ("the ai usage ledger should start tracking searches
as well in a new table"): on the night of the ruling the composer run found
Z.ai's web search/reader weekly limit exhausted (reset 2026-09-24) and nobody
could see it coming because searches were not tracked anywhere.

The feed is ``$HERMES_HOME/logs/search_audit.jsonl`` (written by Hermes, one
JSON object per web call, ``schema: search_event.v1``). It carries a query
hash, counts, status, and latency — never query text or URLs — and this
module never adds either. Reads are incremental on a byte-offset watermark
kept beside the canonical ledger; the watermark is an optimization only, the
canonical identity makes a full re-read replay-safe.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
from typing import Any, Iterable, Iterator, Mapping

from .canonical_ledger import (
    SEARCH_STATUSES,
    _jcs_dumps,
    canonical_timestamp,
    normalize_fact,
)

AUDIT_SCHEMA = "search_event.v1"
DEFAULT_SEARCH_AUDIT_RELATIVE_PATH = "logs/search_audit.jsonl"
DEFAULT_SEARCH_LEDGER = "~/.local/state/codex-usage-tracker/search_events.sqlite3"
SEARCH_LANE = "hermes_search"
SEARCH_QUOTA_LANE = "hermes_search_quota"
# Search backends as Hermes names them → provider as the quota ledger already
# names them, so a derived "exhausted" observation lands in the same series
# the live provider probe writes (collect_z_ai_quota uses "z-ai").
_BACKEND_PROVIDER_ALIASES = {"zai": "z-ai", "z_ai": "z-ai"}
_ISO_INSTANT_RE = re.compile(
    r"(?P<date>[0-9]{4}-[0-9]{2}-[0-9]{2})"
    r"(?:[T ](?P<time>[0-9]{2}:[0-9]{2}(?::[0-9]{2})?)(?P<zone>Z|[+-][0-9]{2}:?[0-9]{2})?)?",
    re.ASCII,
)
_WATERMARK_SCHEMA_VERSION = 1


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _optional_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return None


def search_event_identity(
    observed_at: str, session_id: str | None, query_sha256: str | None, tool: str
) -> str:
    """Stable identity: sha256 over the JCS array of the four naming fields.

    ``observed_at`` is the canonical (UTC) rendering so a feed that later
    re-emits the same instant in another offset still replays instead of
    minting a second fact.
    """
    payload = _jcs_dumps([observed_at, session_id, query_sha256, tool])
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def project_search_event(raw: Mapping[str, Any], source_namespace: str) -> dict[str, Any]:
    """Project one audit object onto the ``search_event_v1`` contract.

    Raises ``ValueError``/``ValidationError`` for a row that cannot be a
    search event; the incremental reader counts those and moves on rather
    than ceding the lane for one bad line.
    """
    if raw.get("schema") != AUDIT_SCHEMA:
        raise ValueError("unsupported search audit schema")
    tool = _optional_text(raw.get("tool"))
    backend = _optional_text(raw.get("backend"))
    status = _optional_text(raw.get("status"))
    if tool is None or backend is None or status is None:
        raise ValueError("search audit row is missing tool, backend, or status")
    if status not in SEARCH_STATUSES:
        raise ValueError("unsupported search audit status")
    observed_at = canonical_timestamp(raw.get("observed_at"))
    session_id = _optional_text(raw.get("session_id"))
    query_sha256 = _optional_text(raw.get("query_sha256"))
    if query_sha256 is not None:
        query_sha256 = query_sha256.casefold()
    row: dict[str, Any] = {
        "fact_type": "search_event_v1",
        "schema_version": 1,
        "source_namespace": source_namespace,
        "source_search_event_id": search_event_identity(observed_at, session_id, query_sha256, tool),
        "harness": _optional_text(raw.get("harness")) or "hermes",
        "observed_at": observed_at,
        "provider": backend,
        "purpose": tool,
        "status": status,
        "session_id": session_id,
        "job_id": _optional_text(raw.get("job_id")),
        "query_sha256": query_sha256,
        "url_count": _optional_int(raw.get("url_count")),
        "result_count": _optional_int(raw.get("result_count")),
        "error_code": _optional_text(raw.get("error_code")),
        "latency_ms": _optional_int(raw.get("latency_ms")),
        "fallback_from": _optional_text(raw.get("fallback_from")),
    }
    return normalize_fact(row)


def _watermark_path(ledger: str | Path, audit: Path) -> Path:
    """One watermark per (ledger, audit file) beside the canonical ledger."""
    target = Path(ledger).expanduser()
    tag = hashlib.sha256(str(audit).encode("utf-8")).hexdigest()[:12]
    return target.with_name(f"{target.name}.search-audit-{tag}.watermark.json")


def read_watermark(path: Path) -> dict[str, Any]:
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(loaded, dict) or loaded.get("schema_version") != _WATERMARK_SCHEMA_VERSION:
        return {}
    return loaded


def write_watermark(path: Path, payload: Mapping[str, Any]) -> Path:
    """Private atomic replace, the same shape the collector uses for health files."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(
        json.dumps({"schema_version": _WATERMARK_SCHEMA_VERSION, **payload}, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)
    return path


def collect_hermes_search_events(
    audit_path: str | Path,
    source_namespace: str,
    *,
    watermark: Mapping[str, Any] | None = None,
) -> Iterator[dict[str, Any]]:
    """Stream new audit rows as canonical facts; a missing feed is an empty lane.

    Resumes at the watermark's byte offset when the file is the same inode
    and has not shrunk (rotation or truncation restarts from zero; the
    canonical identity makes the replay idempotent). A trailing line with no
    newline is an in-progress write: it is deferred and the returned offset
    stops before it. Malformed interior lines are counted, not fatal.

    The generator's return value carries ``{"next_offset", "inode",
    "size", "malformed_lines", "last_observed_at"}`` so the collector can
    advance the watermark only after the canonical append committed.
    """
    path = Path(audit_path).expanduser()
    diagnostics: dict[str, Any] = {
        "next_offset": 0, "inode": None, "size": 0, "malformed_lines": 0, "last_observed_at": None,
    }
    if not path.exists():
        return diagnostics
    if not path.is_file():
        raise ValueError(f"search audit feed is not a regular file: {path}")
    metadata = path.stat()
    diagnostics["inode"] = metadata.st_ino
    diagnostics["size"] = metadata.st_size
    start = 0
    if watermark:
        previous_offset = watermark.get("next_offset")
        same_file = watermark.get("inode") == metadata.st_ino
        if (
            same_file
            and isinstance(previous_offset, int)
            and not isinstance(previous_offset, bool)
            and 0 <= previous_offset <= metadata.st_size
        ):
            start = previous_offset
    with path.open("rb") as stream:
        stream.seek(start)
        offset = start
        while True:
            line = stream.readline()
            if not line:
                break
            if not line.endswith(b"\n"):
                break  # torn tail: deferred to the next pass
            offset += len(line)
            text = line.strip()
            if not text:
                continue
            try:
                raw = json.loads(text.decode("utf-8"))
                if not isinstance(raw, dict):
                    raise ValueError("search audit line is not an object")
                fact = project_search_event(raw, source_namespace)
            except (ValueError, UnicodeDecodeError):
                diagnostics["malformed_lines"] += 1
                continue
            diagnostics["last_observed_at"] = fact["observed_at"]
            yield fact
    diagnostics["next_offset"] = offset
    return diagnostics


def _reset_instant(row: Mapping[str, Any]) -> str | None:
    """A reset instant the rate-limited event names, or None.

    The audit contract has no reset field; a backend that reports one puts
    it in ``error_code`` (e.g. ``weekly_limit_exhausted;reset=2026-09-24``)
    or a future feed may add ``resets_at``. A bare date is read as that
    day's 00:00 UTC.
    """
    for key in ("resets_at", "reset_at", "x_resets_at"):
        candidate = _optional_text(row.get(key))
        if candidate is not None:
            try:
                return canonical_timestamp(candidate)
            except Exception:
                pass
    error_code = _optional_text(row.get("error_code"))
    if error_code is None:
        return None
    match = _ISO_INSTANT_RE.search(error_code)
    if match is None:
        return None
    time_part = match.group("time") or "00:00:00"
    if time_part.count(":") == 1:
        time_part += ":00"
    zone = match.group("zone") or "Z"
    if zone not in {"Z"} and ":" not in zone:
        zone = f"{zone[:3]}:{zone[3:]}"
    try:
        return canonical_timestamp(f"{match.group('date')}T{time_part}{zone}")
    except Exception:
        return None


def derive_search_quota_observations(
    facts: Iterable[Mapping[str, Any]],
    source_namespace: str,
    *,
    raw_rows: Iterable[Mapping[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Rate-limited events that name a reset → ``quota_observation_v1`` rows.

    Records "<backend> <tool> limit exhausted until <reset>" as a point-in-time
    observation (remaining 0, limit unknown, window unknown) in the provider's
    existing quota series, so the burn/quota surfaces can show the exhaustion
    without a live provider probe. Events with no reset time produce nothing:
    the search ledger already carries the rate_limited count.
    """
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    extras = {}
    for raw in raw_rows or ():
        try:
            key = search_event_identity(
                canonical_timestamp(raw.get("observed_at")),
                _optional_text(raw.get("session_id")),
                (_optional_text(raw.get("query_sha256")) or "").casefold() or None,
                _optional_text(raw.get("tool")) or "",
            )
        except Exception:
            continue
        extras[key] = raw
    for fact in facts:
        if fact.get("status") != "rate_limited":
            continue
        merged: dict[str, Any] = dict(extras.get(str(fact.get("source_search_event_id")), {}))
        merged.update({k: v for k, v in fact.items() if v is not None})
        resets_at = _reset_instant(merged)
        if resets_at is None:
            continue
        backend = str(fact["provider"])
        provider = _BACKEND_PROVIDER_ALIASES.get(backend.casefold(), backend)
        tool = str(fact["purpose"])
        observed_at = str(fact["observed_at"])
        observation_id = f"{observed_at}:{backend}:{tool}:rate_limited"
        if observation_id in seen:
            continue
        seen.add(observation_id)
        rows.append(
            normalize_fact(
                {
                    "fact_type": "quota_observation_v1",
                    "schema_version": 1,
                    "source_namespace": source_namespace,
                    "source_observation_id": observation_id,
                    "harness": str(fact.get("harness") or "hermes"),
                    "observed_at": observed_at,
                    "provider": provider,
                    "quota_name": f"{tool}_limit",
                    "quota_scope": "account",
                    "window_kind": "unknown",
                    "unit": "requests",
                    "measurement_confidence": "estimated",
                    "remaining_value": "0",
                    "resets_at": resets_at,
                    "x_source_surface": "hermes_search_audit",
                    "x_search_backend": backend,
                    "x_search_status": "rate_limited",
                    "x_search_error_code": fact.get("error_code"),
                }
            )
        )
    return rows


def _day(observed_at: str) -> str:
    return observed_at[:10]


def build_search_summary(
    facts: Iterable[Mapping[str, Any]],
    *,
    days: int = 7,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Calls per backend per UTC day, rate-limited and fallback counts.

    The window is the last ``days`` UTC calendar days including today; a fact
    outside it is ignored so a caller may hand over an unbounded read.
    """
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    if days < 1:
        raise ValueError("days must be positive")
    first_day = (current - timedelta(days=days - 1)).date()
    day_labels = [(first_day + timedelta(days=index)).isoformat() for index in range(days)]
    per_backend_day: dict[tuple[str, str, str], dict[str, int]] = {}
    per_backend: dict[str, dict[str, Any]] = {}
    total = rate_limited = fallbacks = errors = 0
    for fact in facts:
        observed = str(fact.get("observed_at") or "")
        day = _day(observed)
        if day not in day_labels:
            continue
        backend = str(fact.get("provider") or "unknown")
        tool = str(fact.get("purpose") or "unknown")
        status = str(fact.get("status") or "unknown")
        cell = per_backend_day.setdefault(
            (day, backend, tool), {"calls": 0, "ok": 0, "rate_limited": 0, "error": 0, "fallbacks": 0}
        )
        agg = per_backend.setdefault(
            backend,
            {
                "backend": backend, "calls": 0, "ok": 0, "rate_limited": 0, "error": 0,
                "fallbacks": 0, "fallback_from": {}, "last_rate_limited_at": None,
                "last_error_code": None,
            },
        )
        total += 1
        cell["calls"] += 1
        agg["calls"] += 1
        if status in cell:
            cell[status] += 1
            agg[status] += 1
        if status == "rate_limited":
            rate_limited += 1
            if agg["last_rate_limited_at"] is None or observed > agg["last_rate_limited_at"]:
                agg["last_rate_limited_at"] = observed
                agg["last_error_code"] = fact.get("error_code")
        elif status == "error":
            errors += 1
        origin = fact.get("fallback_from")
        if origin:
            fallbacks += 1
            cell["fallbacks"] += 1
            agg["fallbacks"] += 1
            agg["fallback_from"][str(origin)] = agg["fallback_from"].get(str(origin), 0) + 1
    daily = [
        {"day": day, "backend": backend, "tool": tool, **counts}
        for (day, backend, tool), counts in sorted(per_backend_day.items())
    ]
    return {
        "days": days,
        "window": {"start_day": day_labels[0], "end_day": day_labels[-1]},
        "totals": {
            "calls": total,
            "rate_limited": rate_limited,
            "errors": errors,
            "fallbacks": fallbacks,
        },
        "backends": [per_backend[name] for name in sorted(per_backend)],
        "daily": daily,
    }


__all__ = [
    "AUDIT_SCHEMA",
    "DEFAULT_SEARCH_AUDIT_RELATIVE_PATH",
    "DEFAULT_SEARCH_LEDGER",
    "SEARCH_LANE",
    "SEARCH_QUOTA_LANE",
    "build_search_summary",
    "collect_hermes_search_events",
    "derive_search_quota_observations",
    "project_search_event",
    "read_watermark",
    "search_event_identity",
    "write_watermark",
    "_watermark_path",
]
