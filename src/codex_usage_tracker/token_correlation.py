"""Correlate Codex usage snapshots with Hermes token accounting."""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
import os
import sqlite3
from typing import Any, Dict, Iterable, List, Optional


DEFAULT_HERMES_STATE_DB = "~/.hermes/state.db"


def resolve_state_db_path(cli_value: Optional[str] = None) -> str:
    """Resolve the Hermes session database path used for token correlation."""
    if cli_value:
        return cli_value
    env_value = os.environ.get("HERMES_STATE_DB_PATH")
    if env_value:
        return env_value
    return DEFAULT_HERMES_STATE_DB


def _parse_ts(raw: Any) -> Optional[datetime]:
    if not isinstance(raw, str) or not raw.strip():
        return None
    value = raw.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _to_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    if numeric != numeric:  # NaN
        return None
    return numeric


def _round(value: Optional[float], digits: int = 1) -> Optional[float]:
    if value is None:
        return None
    return round(value, digits)


def _delta(start: Any, end: Any) -> Optional[float]:
    left = _to_float(start)
    right = _to_float(end)
    if left is None or right is None:
        return None
    return round(right - left, 3)


def _positive_ratio(numerator: int, denominator: Optional[float]) -> Optional[float]:
    if denominator is None or denominator <= 0:
        return None
    return round(float(numerator) / denominator, 1)


def _percent(numerator: int, denominator: int) -> Optional[float]:
    if denominator <= 0:
        return None
    return round(100.0 * float(numerator) / float(denominator), 1)


def _sorted_usage_samples(usage_rows: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    samples: List[Dict[str, Any]] = []
    for row in usage_rows:
        fetched_at = _parse_ts(row.get("fetched_at"))
        if fetched_at is None:
            continue
        clone = dict(row)
        clone["_fetched_dt"] = fetched_at
        samples.append(clone)
    samples.sort(key=lambda row: row["_fetched_dt"])
    return samples


def _empty_token_totals() -> Dict[str, Any]:
    return {
        "codex_sessions": 0,
        "api_calls": 0,
        "input_tokens": 0,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
        "output_tokens": 0,
        "reasoning_tokens": 0,
        "models": [],
    }


def _load_token_windows(
    state_db_path: str,
    windows: List[tuple[datetime, datetime]],
) -> Dict[tuple[datetime, datetime], Dict[str, Any]]:
    """Aggregate openai-codex session tokens inside usage-sample windows."""
    if not windows:
        return {}
    path = Path(state_db_path).expanduser()
    totals = {window: _empty_token_totals() for window in windows}
    if not path.exists():
        return totals

    start_epoch = min(start.timestamp() for start, _ in windows)
    end_epoch = max(end.timestamp() for _, end in windows)
    try:
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT
                started_at,
                model,
                COALESCE(api_call_count, 0) AS api_call_count,
                COALESCE(input_tokens, 0) AS input_tokens,
                COALESCE(cache_read_tokens, 0) AS cache_read_tokens,
                COALESCE(cache_write_tokens, 0) AS cache_write_tokens,
                COALESCE(output_tokens, 0) AS output_tokens,
                COALESCE(reasoning_tokens, 0) AS reasoning_tokens
            FROM sessions
            WHERE billing_provider = 'openai-codex'
              AND started_at >= ?
              AND started_at < ?
            ORDER BY started_at ASC
            """,
            (start_epoch, end_epoch),
        ).fetchall()
    except sqlite3.Error:
        return totals
    finally:
        try:
            conn.close()  # type: ignore[name-defined]
        except Exception:
            pass

    # Window count is tiny (<=168 in the dashboard), so a linear scan is clearer
    # than trying to align SQLite date buckets with irregular usage samples.
    for row in rows:
        started_at = datetime.fromtimestamp(float(row["started_at"]), timezone.utc)
        for window in windows:
            start, end = window
            if start <= started_at < end:
                bucket = totals[window]
                bucket["codex_sessions"] += 1
                bucket["api_calls"] += int(row["api_call_count"] or 0)
                bucket["input_tokens"] += int(row["input_tokens"] or 0)
                bucket["cache_read_tokens"] += int(row["cache_read_tokens"] or 0)
                bucket["cache_write_tokens"] += int(row["cache_write_tokens"] or 0)
                bucket["output_tokens"] += int(row["output_tokens"] or 0)
                bucket["reasoning_tokens"] += int(row["reasoning_tokens"] or 0)
                model = row["model"]
                if model:
                    bucket.setdefault("_model_counts", Counter())[str(model)] += 1
                break

    for bucket in totals.values():
        model_counts = bucket.pop("_model_counts", Counter())
        bucket["models"] = [model for model, _ in model_counts.most_common(4)]
    return totals


def build_token_correlation_rows(
    usage_rows: Iterable[Dict[str, Any]],
    *,
    state_db_path: Optional[str] = None,
    limit: int = 168,
) -> List[Dict[str, Any]]:
    """Build an hourly-ish ledger from adjacent usage samples and Hermes tokens.

    Each returned row represents the activity window between two usage samples.
    Tokens are summed for Hermes sessions that started in that window. Usage
    deltas are computed from the first sample to the second sample, so a sample
    taken at 02:00 is compared against the prior sample at 01:00 and receives
    the Hermes token traffic from [01:00, 02:00).
    """
    samples = _sorted_usage_samples(usage_rows)
    if len(samples) < 2:
        return []

    pairs = [(samples[i], samples[i + 1]) for i in range(len(samples) - 1)]
    if limit > 0:
        pairs = pairs[-limit:]
    windows = [(left["_fetched_dt"], right["_fetched_dt"]) for left, right in pairs]
    token_windows = _load_token_windows(resolve_state_db_path(state_db_path), windows)

    rows: List[Dict[str, Any]] = []
    for left, right in pairs:
        start = left["_fetched_dt"]
        end = right["_fetched_dt"]
        totals = token_windows.get((start, end), _empty_token_totals())
        input_tokens = int(totals["input_tokens"])
        cache_read_tokens = int(totals["cache_read_tokens"])
        cache_write_tokens = int(totals["cache_write_tokens"])
        output_tokens = int(totals["output_tokens"])
        reasoning_tokens = int(totals["reasoning_tokens"])
        prompt_tokens = input_tokens + cache_read_tokens + cache_write_tokens
        total_tokens = prompt_tokens + output_tokens + reasoning_tokens
        session_delta = _delta(left.get("session_used_pct"), right.get("session_used_pct"))
        weekly_delta = _delta(left.get("weekly_used_pct"), right.get("weekly_used_pct"))
        spark_session_delta = _delta(left.get("spark_session_used_pct"), right.get("spark_session_used_pct"))
        spark_weekly_delta = _delta(left.get("spark_weekly_used_pct"), right.get("spark_weekly_used_pct"))
        reset_or_drop = any(
            value is not None and value < 0
            for value in (session_delta, weekly_delta, spark_session_delta, spark_weekly_delta)
        )
        span_hours = max((end - start).total_seconds() / 3600.0, 0.0)
        rows.append(
            {
                "window_start": start.isoformat(),
                "window_end": end.isoformat(),
                "span_hours": round(span_hours, 2),
                "session_used_pct_start": _to_float(left.get("session_used_pct")),
                "session_used_pct_end": _to_float(right.get("session_used_pct")),
                "session_delta_pct": session_delta,
                "weekly_used_pct_start": _to_float(left.get("weekly_used_pct")),
                "weekly_used_pct_end": _to_float(right.get("weekly_used_pct")),
                "weekly_delta_pct": weekly_delta,
                "spark_session_delta_pct": spark_session_delta,
                "spark_weekly_delta_pct": spark_weekly_delta,
                "reset_or_drop": reset_or_drop,
                "codex_sessions": int(totals["codex_sessions"]),
                "api_calls": int(totals["api_calls"]),
                "input_tokens": input_tokens,
                "cache_read_tokens": cache_read_tokens,
                "cache_write_tokens": cache_write_tokens,
                "output_tokens": output_tokens,
                "reasoning_tokens": reasoning_tokens,
                "prompt_tokens": prompt_tokens,
                "total_tokens": total_tokens,
                "cache_hit_pct": _percent(cache_read_tokens, prompt_tokens),
                "noncached_prompt_pct": _percent(input_tokens, prompt_tokens),
                "tokens_per_session_pct": None if reset_or_drop else _positive_ratio(total_tokens, session_delta),
                "tokens_per_weekly_pct": None if reset_or_drop else _positive_ratio(total_tokens, weekly_delta),
                "models": totals.get("models", []),
            }
        )
    return rows
