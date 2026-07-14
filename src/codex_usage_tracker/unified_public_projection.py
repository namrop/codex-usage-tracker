"""Public-safe hourly projection of the private canonical usage ledger."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import math
import os
from pathlib import Path
import re
import tempfile
from typing import Any

from .canonical_ledger import MAX_SAFE_INTEGER, ValidationError, canonical_timestamp, query_sqlite_facts


PUBLIC_PROJECTION_KIND = "namrop_public_usage_projection.v1"
DEFAULT_PUBLIC_PROJECTION_SOURCE = "unified-usage-public-projection"
DEFAULT_PUBLIC_PROJECTION_HOURS = 168
MAX_PUBLIC_PROJECTION_HOURS = 168
_MAX_INPUT_EVENTS = 100_000
_SQLITE_SUFFIXES = frozenset({".sqlite3", ".sqlite", ".db"})
_TOKEN_FIELDS = (
    "input_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "output_tokens",
    "reasoning_tokens",
)
_TOP_LEVEL_KEYS = frozenset({
    "kind", "schema_version", "source", "generated_at", "bucket_minutes", "rows", "summary",
})
_ROW_KEYS = frozenset({
    "window_start", "window_end", "input_tokens", "cache_read_tokens", "cache_write_tokens",
    "output_tokens", "reasoning_tokens", "prompt_tokens", "total_tokens", "request_attempts",
    "cache_hit_pct", "measurement_confidence",
})
_SUMMARY_KEYS = frozenset({
    "updated_at", "window_count", "total_tokens", "request_attempts", "latest_total_tokens",
    "latest_cache_hit_pct",
})
_CONFIDENCE_VALUES = frozenset({"exact", "reconstructed", "mixed", "unknown"})
_PUBLIC_SOURCE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


def _utc_text(value: datetime) -> str:
    """Render a timezone-aware datetime using the canonical ledger timestamp form."""
    return canonical_timestamp(value.astimezone(timezone.utc).isoformat())


def _validated_hours(hours: int) -> int:
    if isinstance(hours, bool) or not isinstance(hours, int) or not 1 <= hours <= MAX_PUBLIC_PROJECTION_HOURS:
        raise ValueError(f"hours must be an integer from 1 through {MAX_PUBLIC_PROJECTION_HOURS}")
    return hours


def _validated_source(source: Any) -> str:
    """Accept an opaque public marker, never a path or free-form private text."""
    if not isinstance(source, str) or _PUBLIC_SOURCE_RE.fullmatch(source) is None:
        raise ValueError(
            "source must be a 1-128 character public marker containing only "
            "ASCII letters, digits, '.', '_', ':', or '-'"
        )
    return source


def _event_datetime(value: Any) -> datetime:
    if not isinstance(value, str):
        raise ValidationError("usage event occurred_at is not a timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValidationError("usage event occurred_at is not a timestamp") from exc
    if parsed.tzinfo is None:
        raise ValidationError("usage event occurred_at is not timezone-aware")
    return parsed.astimezone(timezone.utc)


def _bucket_confidence(values: set[str]) -> str:
    if values == {"exact"}:
        return "exact"
    if values == {"reconstructed"}:
        return "reconstructed"
    if not values or values <= {"estimated", "unknown"}:
        return "unknown"
    return "mixed"


def _request_attempts(row: dict[str, Any]) -> int:
    if row.get("record_kind") == "api_attempt":
        return 1
    if row.get("record_kind") == "historical_aggregate":
        return int(row.get("reconstructed_call_count") or 0)
    return 0


def build_unified_public_projection(
    ledger_path: str | Path,
    *,
    hours: int = DEFAULT_PUBLIC_PROJECTION_HOURS,
    source: str = DEFAULT_PUBLIC_PROJECTION_SOURCE,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Build complete UTC hourly buckets from a type-bound usage SQLite ledger."""
    count = _validated_hours(hours)
    public_source = _validated_source(source)
    target = Path(ledger_path).expanduser()
    if target.suffix.casefold() not in _SQLITE_SUFFIXES:
        raise ValueError("unified public projection input must be SQLite")
    if not target.exists() or not target.is_file():
        raise ValueError("unified public projection input must be an existing SQLite ledger")

    generated = datetime.now(timezone.utc) if now is None else now
    if generated.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    generated = generated.astimezone(timezone.utc)
    complete_end = generated.replace(minute=0, second=0, microsecond=0)
    projection_start = complete_end - timedelta(hours=count)
    facts = query_sqlite_facts(
        target,
        fact_type="usage_event_v1",
        occurred_or_observed_at_gte=_utc_text(projection_start),
        occurred_or_observed_at_lt=_utc_text(complete_end),
        order="asc",
        limit=_MAX_INPUT_EVENTS + 1,
    )
    if len(facts) > _MAX_INPUT_EVENTS:
        raise ValueError("usage query exceeds the bounded public projection event limit")

    buckets: list[dict[str, Any]] = []
    confidences: list[set[str]] = []
    for index in range(count):
        window_start = projection_start + timedelta(hours=index)
        window_end = window_start + timedelta(hours=1)
        buckets.append({
            "window_start": _utc_text(window_start),
            "window_end": _utc_text(window_end),
            "input_tokens": 0,
            "cache_read_tokens": 0,
            "cache_write_tokens": 0,
            "output_tokens": 0,
            "reasoning_tokens": 0,
            "prompt_tokens": 0,
            "total_tokens": 0,
            "request_attempts": 0,
            "cache_hit_pct": None,
            "measurement_confidence": "unknown",
        })
        confidences.append(set())

    for fact in facts:
        occurred_at = _event_datetime(fact.get("occurred_at"))
        if occurred_at < projection_start or occurred_at >= complete_end:
            continue
        bucket_index = int((occurred_at - projection_start).total_seconds() // 3600)
        bucket = buckets[bucket_index]
        for field in _TOKEN_FIELDS:
            bucket[field] += int(fact.get(field) or 0)
        bucket["request_attempts"] += _request_attempts(fact)
        confidences[bucket_index].add(str(fact.get("measurement_confidence") or "unknown"))

    for bucket, confidence_values in zip(buckets, confidences, strict=True):
        bucket["prompt_tokens"] = (
            bucket["input_tokens"] + bucket["cache_read_tokens"] + bucket["cache_write_tokens"]
        )
        # Canonical output already includes reported reasoning; reasoning remains diagnostic.
        bucket["total_tokens"] = bucket["prompt_tokens"] + bucket["output_tokens"]
        prompt_tokens = bucket["prompt_tokens"]
        bucket["cache_hit_pct"] = (
            round(bucket["cache_read_tokens"] * 100 / prompt_tokens, 1) if prompt_tokens > 0 else None
        )
        bucket["measurement_confidence"] = _bucket_confidence(confidence_values)

    latest = buckets[-1]
    generated_at = _utc_text(generated)
    summary = {
        "updated_at": generated_at,
        "window_count": len(buckets),
        "total_tokens": sum(row["total_tokens"] for row in buckets),
        "request_attempts": sum(row["request_attempts"] for row in buckets),
        "latest_total_tokens": latest["total_tokens"],
        "latest_cache_hit_pct": latest["cache_hit_pct"],
    }
    payload = {
        "kind": PUBLIC_PROJECTION_KIND,
        "schema_version": 1,
        "source": public_source,
        "generated_at": generated_at,
        "bucket_minutes": 60,
        "rows": buckets,
        "summary": summary,
    }
    validate_unified_public_projection(payload)
    return payload


def _require_exact_keys(value: Any, expected: frozenset[str], location: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != expected:
        raise ValueError(f"{location} must contain exactly the allowlisted keys")
    return value


def _require_safe_int(value: Any, location: str, *, nonnegative: bool = False) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or abs(value) > MAX_SAFE_INTEGER:
        raise ValueError(f"{location} must be an I-JSON safe integer")
    if nonnegative and value < 0:
        raise ValueError(f"{location} must be nonnegative")


def _require_percentage(value: Any, location: str) -> None:
    if value is None:
        return
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or not 0 <= value <= 100
    ):
        raise ValueError(f"{location} must be null or a percentage")


def _require_utc_timestamp(value: Any, location: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"{location} must be a canonical UTC timestamp")
    try:
        normalized = canonical_timestamp(value)
    except ValidationError as exc:
        raise ValueError(f"{location} must be a canonical UTC timestamp") from exc
    if normalized != value or not value.endswith("Z"):
        raise ValueError(f"{location} must be a canonical UTC timestamp")
    return _event_datetime(value)


def validate_unified_public_projection(payload: Any) -> None:
    """Reject any projection that is not the exact public allowlist schema."""
    top = _require_exact_keys(payload, _TOP_LEVEL_KEYS, "projection")
    if (
        top["kind"] != PUBLIC_PROJECTION_KIND
        or isinstance(top["schema_version"], bool)
        or top["schema_version"] != 1
        or isinstance(top["bucket_minutes"], bool)
        or top["bucket_minutes"] != 60
    ):
        raise ValueError("projection contract marker is invalid")
    _validated_source(top["source"])
    generated_at = _require_utc_timestamp(top["generated_at"], "generated_at")
    rows = top["rows"]
    if not isinstance(rows, list) or not 1 <= len(rows) <= MAX_PUBLIC_PROJECTION_HOURS:
        raise ValueError("projection rows must be an array of 1 through 168 buckets")
    previous_end: datetime | None = None
    for index, candidate in enumerate(rows):
        row = _require_exact_keys(candidate, _ROW_KEYS, f"rows[{index}]")
        window_start = _require_utc_timestamp(row["window_start"], f"rows[{index}].window_start")
        window_end = _require_utc_timestamp(row["window_end"], f"rows[{index}].window_end")
        if window_start.minute or window_start.second or window_start.microsecond:
            raise ValueError(f"rows[{index}].window_start must be on a UTC hour boundary")
        if window_end - window_start != timedelta(hours=1):
            raise ValueError(f"rows[{index}] must describe exactly one complete hour")
        if previous_end is not None and window_start != previous_end:
            raise ValueError("projection rows must be contiguous and chronological")
        previous_end = window_end
        for field in (*_TOKEN_FIELDS, "prompt_tokens", "total_tokens"):
            _require_safe_int(row[field], f"rows[{index}].{field}")
        _require_safe_int(row["request_attempts"], f"rows[{index}].request_attempts", nonnegative=True)
        _require_percentage(row["cache_hit_pct"], f"rows[{index}].cache_hit_pct")
        if row["measurement_confidence"] not in _CONFIDENCE_VALUES:
            raise ValueError(f"rows[{index}].measurement_confidence is invalid")
        expected_prompt = row["input_tokens"] + row["cache_read_tokens"] + row["cache_write_tokens"]
        if row["prompt_tokens"] != expected_prompt:
            raise ValueError(f"rows[{index}].prompt_tokens does not match its token buckets")
        if row["total_tokens"] != expected_prompt + row["output_tokens"]:
            raise ValueError(f"rows[{index}].total_tokens does not match prompt plus output tokens")
        expected_cache_hit = (
            round(row["cache_read_tokens"] * 100 / expected_prompt, 1)
            if expected_prompt > 0
            else None
        )
        if row["cache_hit_pct"] != expected_cache_hit:
            raise ValueError(f"rows[{index}].cache_hit_pct does not match its token buckets")
    complete_end = generated_at.replace(minute=0, second=0, microsecond=0)
    if previous_end != complete_end:
        raise ValueError("projection rows must end at the latest complete UTC hour")

    summary = _require_exact_keys(top["summary"], _SUMMARY_KEYS, "summary")
    _require_utc_timestamp(summary["updated_at"], "summary.updated_at")
    for field in ("window_count", "request_attempts"):
        _require_safe_int(summary[field], f"summary.{field}", nonnegative=True)
    for field in ("total_tokens", "latest_total_tokens"):
        _require_safe_int(summary[field], f"summary.{field}")
    _require_percentage(summary["latest_cache_hit_pct"], "summary.latest_cache_hit_pct")
    if summary["window_count"] != len(rows):
        raise ValueError("summary.window_count does not match rows")
    if summary["updated_at"] != top["generated_at"]:
        raise ValueError("summary.updated_at does not match generated_at")
    if summary["total_tokens"] != sum(row["total_tokens"] for row in rows):
        raise ValueError("summary.total_tokens does not match rows")
    if summary["request_attempts"] != sum(row["request_attempts"] for row in rows):
        raise ValueError("summary.request_attempts does not match rows")
    latest = rows[-1]
    if summary["latest_total_tokens"] != latest["total_tokens"]:
        raise ValueError("summary.latest_total_tokens does not match the latest row")
    if summary["latest_cache_hit_pct"] != latest["cache_hit_pct"]:
        raise ValueError("summary.latest_cache_hit_pct does not match the latest row")


def write_unified_public_projection(
    ledger_path: str | Path,
    projection_path: str | Path,
    *,
    hours: int = DEFAULT_PUBLIC_PROJECTION_HOURS,
    source: str = DEFAULT_PUBLIC_PROJECTION_SOURCE,
    now: datetime | None = None,
) -> Path:
    """Validate and atomically replace one same-directory public JSON artifact."""
    output = Path(projection_path).expanduser()
    private_input = Path(ledger_path).expanduser()
    try:
        aliases_input = output.resolve(strict=False) == private_input.resolve(strict=False)
        if output.exists() and private_input.exists():
            aliases_input = aliases_input or os.path.samefile(output, private_input)
    except OSError as exc:
        raise ValueError("could not establish that public output differs from private input") from exc
    if aliases_input:
        raise ValueError("public projection output must not be the same file as its private input")
    payload = build_unified_public_projection(ledger_path, hours=hours, source=source, now=now)
    validate_unified_public_projection(payload)
    serialized = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    output.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{output.name}.", suffix=".tmp", dir=output.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            descriptor = -1
            stream.write(serialized)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, output)
        directory_fd = os.open(output.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        return output
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
