"""Flask dashboard for Codex usage ledger inspection."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
import json
import os
import threading
from typing import Any, Dict, List, Optional, TypeAlias

from flask import Flask, jsonify, render_template, request

from .capability_matrix import get_capability_matrix
from .list_pricing import estimate_list_price
from .codex_call_accounting import build_codex_accounting_rows
from .jsonl_store import newest_first, read_jsonl
from .ledger import reconcile_snapshot_windows
from .policy_state import build_burn_projection_rows, latest_policy_state
from .provider_spend import (
    DIRECT_PROVIDER_SPEND_LEDGER,
    ROUTING_DECISION_LEDGER,
    TASK_OUTCOME_LEDGER,
    latest_budget_state,
    model_routing_ledger_path,
    read_provider_spend_rows,
    summarize_provider_spend,
)
from .token_correlation import build_token_correlation_rows, resolve_state_db_path
from .canonical_ledger import (
    IdentityConflictError,
    MalformedLedgerError,
    ValidationError,
    canonical_timestamp,
    query_sqlite_facts,
    read_facts,
)


DEFAULT_ATRIUM_ROOT = "/Users/luisramirez/Digital_Workspace"
DEFAULT_LEDGER_RELATIVE_PATH = "12_runtime/ledgers/codex_usage/codex_usage_ledger.jsonl"
QUOTA_RESPONSE_FIELDS = (
    "harness",
    "observed_at",
    "provider",
    "quota_name",
    "quota_scope",
    "window_kind",
    "window_started_at",
    "window_ends_at",
    "resets_at",
    "limit_value",
    "remaining_value",
    "used_value",
    "unit",
    "measurement_confidence",
)
BILLING_RESPONSE_FIELDS = (
    "provider",
    "occurred_at",
    "billing_period_start",
    "billing_period_end",
    "transaction_kind",
    "status",
    "amount",
    "currency",
    "description_code",
)
_SQLITE_LEDGER_SUFFIXES = frozenset({".sqlite3", ".sqlite", ".db"})
_PRIVATE_API_SQLITE_LIMIT = 10_000
_PRIVATE_API_MAX_ROWS = 100_000
_PRIVATE_API_MAX_JSONL_BYTES = 128 * 1024 * 1024
_MAX_PRIVATE_API_DAYS = 36_500
# Keeper ruling 2026-08-29: unbounded private reads return the latest rows an
# 8-day reconstruction needs; older history is reached with `before` paging.
_DEFAULT_PRIVATE_API_WINDOW_DAYS = 8
_MAX_UNIFIED_SERIES_HOURS = 168
# /api/data feeds a 24-row table; returning the whole ledger cost ~4MB per refresh.
_DEFAULT_DATA_ROW_LIMIT = 200
_LEDGER_CACHE_MAX_ENTRIES = 8
_LEDGER_CACHE_LOCK = threading.Lock()
_LEDGER_CACHE: Dict[str, tuple[tuple[int, int], List[Dict[str, Any]]]] = {}
_UNIFIED_MODEL_SERIES_LIMIT = 5
# The status strip is one row per provider; provider cardinality is small and
# bounded by the ledger, but the payload stays bounded on principle.
_UNIFIED_STATUS_PROVIDER_LIMIT = 12
_SUBSCRIPTION_CHART_PROVIDER_LIMIT = 32
_SUBSCRIPTION_CHART_SERIES_PER_PROVIDER_LIMIT = 16
_PRIVATE_LEDGER_MAX_AGE_SECONDS = 3 * 60 * 60
_QUOTA_PROVIDER_STATES = frozenset({"inactive_or_not_reported"})
_QUOTA_COVERAGE_STATUSES = frozenset({"partial"})
_QUOTA_PROVIDER_ORDER = (
    "openai-codex",
    "anthropic",
    "opencode-go",
    "z-ai-glm",
    "kimi-k3-coding",
)
_QUOTA_PROVIDER_LABELS = {
    "openai-codex": "OpenAI / Codex",
    "anthropic": "Anthropic / Claude",
    "opencode-go": "OpenCode Go",
    "z-ai-glm": "Z.AI / GLM",
    "kimi-k3-coding": "Kimi K3 Coding",
}
_PRIMARY_WEEKLY_QUOTAS = {
    "openai-codex": "week",
    "anthropic": "seven_day",
    "opencode-go": "week",
    "z-ai-glm": "week",
    "kimi-k3-coding": "week",
}
_PRIMARY_FIVE_HOUR_QUOTAS = {
    "anthropic": "five_hour",
    "opencode-go": "five_hour",
    "z-ai-glm": "five_hour",
    "kimi-k3-coding": "five_hour",
}
_QUOTA_PROVIDER_ALIASES = {
    "openai": "openai-codex",
    "openai-codex": "openai-codex",
    "codex": "openai-codex",
    "anthropic": "anthropic",
    "claude": "anthropic",
    "claude-code": "anthropic",
    "opencode-go": "opencode-go",
    "z.ai": "z-ai-glm",
    "z-ai": "z-ai-glm",
    "zai": "z-ai-glm",
    "z-ai-glm": "z-ai-glm",
    "zhipu": "z-ai-glm",
    "zhipuai": "z-ai-glm",
    "glm": "z-ai-glm",
    "kimi": "kimi-k3-coding",
    "kimi-code": "kimi-k3-coding",
    "kimi-coding": "kimi-k3-coding",
    "kimi-k3-coding": "kimi-k3-coding",
    "moonshot": "kimi-k3-coding",
    "moonshot-ai": "kimi-k3-coding",
}
_QuotaAccountIdentity: TypeAlias = tuple[bool, str]
_QuotaSeriesIdentity: TypeAlias = tuple[
    str,
    str,
    str,
    str,
    str,
    str,
    _QuotaAccountIdentity,
]
_ADDITIVE_TOKEN_FIELDS = (
    "input_tokens", "cache_read_tokens", "cache_write_tokens", "output_tokens",
)


class UnsupportedLedgerSuffix(ValueError):
    pass


class PrivateLedgerQueryTooLarge(ValueError):
    pass


def _resolve_ledger_path(atrium_root: str, cli_value: Optional[str]) -> str:
    if cli_value:
        return cli_value
    env_value = os.environ.get("CODEX_USAGE_LEDGER_PATH")
    if env_value:
        return env_value
    return f"{atrium_root.rstrip('/')}/{DEFAULT_LEDGER_RELATIVE_PATH}"


def _normalize_timestamp(raw_timestamp: Any) -> float:
    if not isinstance(raw_timestamp, str):
        return 0.0
    value = raw_timestamp.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            return 0.0
        return parsed.astimezone(timezone.utc).timestamp()
    except (TypeError, ValueError, OverflowError, OSError):
        return 0.0


def _fact_datetime(raw_timestamp: Any) -> datetime:
    if not isinstance(raw_timestamp, str):
        return datetime.min.replace(tzinfo=timezone.utc)
    try:
        parsed = datetime.fromisoformat(raw_timestamp.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            return datetime.min.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except (ValueError, OverflowError):
        return datetime.min.replace(tzinfo=timezone.utc)


def _rfc3339_order_key(raw_timestamp: Any) -> tuple[datetime, Decimal] | None:
    """Return an exact UTC timestamp key, or fail closed for invalid input."""
    if not isinstance(raw_timestamp, str):
        return None
    try:
        canonical = canonical_timestamp(raw_timestamp)
        timestamp_text = canonical.removesuffix("Z")
        whole_text, separator, fraction_text = timestamp_text.partition(".")
        # canonical_timestamp validates and normalizes the UTC whole second.
        # Pad years because strftime may omit leading zeroes on some platforms.
        year, remainder = whole_text.split("-", 1)
        whole_second = datetime.fromisoformat(f"{int(year):04d}-{remainder}+00:00")
        fraction = Decimal(f"0.{fraction_text}") if separator else Decimal(0)
    except (
        InvalidOperation,
        OSError,
        OverflowError,
        TypeError,
        ValidationError,
        ValueError,
    ):
        return None
    return whole_second, fraction


def _datetime_rfc3339_order_key(value: datetime) -> tuple[datetime, Decimal] | None:
    """Return the exact RFC 3339 key corresponding to an aware datetime."""
    try:
        if value.tzinfo is None or value.utcoffset() is None:
            return None
        rendered = value.astimezone(timezone.utc).isoformat()
    except (AttributeError, OSError, OverflowError, TypeError, ValueError):
        return None
    return _rfc3339_order_key(rendered)


def _lane_freshness(
    lane_health_path: Path,
    *,
    now: datetime,
) -> list[dict[str, Any]]:
    """Project the collector's per-lane health file, or nothing if absent.

    A single heartbeat answers "did canonical writes land"; it cannot answer
    "which source stopped reporting". When the collector's lane health file is
    reachable each lane is projected with its own age so the UI can name the
    stale lane instead of condemning the whole ledger.
    """
    try:
        loaded = json.loads(lane_health_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if not isinstance(loaded, dict) or loaded.get("schema_version") != 1:
        return []
    lanes = loaded.get("lanes")
    if not isinstance(lanes, dict):
        return []
    projected: list[dict[str, Any]] = []
    for name in sorted(str(key) for key in lanes):
        lane = lanes.get(name)
        if not isinstance(lane, dict):
            continue
        last_success_at = lane.get("last_success_at")
        parsed = (
            _fact_datetime(last_success_at) if isinstance(last_success_at, str) else None
        )
        if parsed is None or parsed == datetime.min.replace(tzinfo=timezone.utc):
            status = "never"
            age_seconds = None
        else:
            age_seconds = int((now - parsed).total_seconds())
            status = "stale" if age_seconds > _PRIVATE_LEDGER_MAX_AGE_SECONDS else "fresh"
        projected.append(
            {
                "lane": name,
                "status": status,
                "last_success_at": last_success_at if isinstance(last_success_at, str) else None,
                "last_run_at": lane.get("last_run_at") if isinstance(lane.get("last_run_at"), str) else None,
                "last_error_class": (
                    lane.get("last_error_class")
                    if isinstance(lane.get("last_error_class"), str)
                    else None
                ),
                "rows_written": int(lane.get("rows_written") or 0),
                "quarantined_count": int(lane.get("quarantined_count") or 0),
                "age_seconds": age_seconds,
                # Graceful cede (keeper-ruled 2026-08-29): a lane that
                # exhausted its retries this run was skipped so the rest of
                # the run could land. The honest state must reach the screen.
                "ceded": bool(lane.get("ceded")),
            }
        )
    return projected


def _ledger_freshness(
    ledger_path: str,
    *,
    now: datetime,
    heartbeat_name: str,
    lane_health_name: str | None = None,
) -> dict[str, Any]:
    path = Path(ledger_path).expanduser()
    health_dir = path.parent / "health"
    heartbeat = health_dir / heartbeat_name
    candidates = [heartbeat] if heartbeat.exists() else [path]
    wal = Path(f"{path}-wal")
    try:
        if not heartbeat.exists() and wal.exists() and wal.stat().st_size > 0:
            candidates.append(wal)
    except OSError:
        pass
    lanes = (
        _lane_freshness(health_dir / lane_health_name, now=now)
        if lane_health_name
        else []
    )
    stale_lanes = [lane["lane"] for lane in lanes if lane["status"] != "fresh"]
    try:
        latest = max(
            datetime.fromtimestamp(candidate.stat().st_mtime, timezone.utc).replace(microsecond=0)
            for candidate in candidates
        )
    except (OSError, ValueError, OverflowError):
        payload: dict[str, Any] = {
            "status": "empty",
            "latest_at": None,
            "max_age_seconds": _PRIVATE_LEDGER_MAX_AGE_SECONDS,
        }
        if lanes:
            payload["lanes"] = lanes
            payload["stale_lanes"] = stale_lanes
        return payload
    age_seconds = (now - latest).total_seconds()
    status = "stale" if age_seconds > _PRIVATE_LEDGER_MAX_AGE_SECONDS else "fresh"
    payload = {
        # Writes can land on schedule while one lane has been silently dead for
        # days, so a stale lane makes the whole surface stale.
        "status": "stale" if (status == "stale" or stale_lanes) else "fresh",
        "latest_at": latest.isoformat().replace("+00:00", "Z"),
        "max_age_seconds": _PRIVATE_LEDGER_MAX_AGE_SECONDS,
    }
    if lanes:
        payload["lanes"] = lanes
        payload["stale_lanes"] = stale_lanes
    return payload


def _private_facts(
    ledger_path: str,
    *,
    fact_type: str,
    filters: dict[str, str],
    cutoff: datetime | None = None,
    before: datetime | None = None,
    order: str = "asc",
) -> list[dict[str, Any]]:
    """Dispatch canonical private reads by explicit suffix and preserve JSONL."""
    path = Path(ledger_path).expanduser()
    suffix = path.suffix.casefold()
    if suffix == ".jsonl":
        if path.exists() and path.stat().st_size > _PRIVATE_API_MAX_JSONL_BYTES:
            raise PrivateLedgerQueryTooLarge("JSONL ledger exceeds private API byte limit")
        rows = [row for row in read_facts(path) if row.get("fact_type") == fact_type]
        for field, expected in filters.items():
            rows = [row for row in rows if row.get(field) == expected]
        timestamp_field = "observed_at" if fact_type == "quota_observation_v1" else "occurred_at"
        if cutoff is not None:
            rows = [row for row in rows if _fact_datetime(row.get(timestamp_field)) >= cutoff]
        if before is not None:
            rows = [row for row in rows if _fact_datetime(row.get(timestamp_field)) < before]
        if len(rows) > _PRIVATE_API_MAX_ROWS:
            raise PrivateLedgerQueryTooLarge("ledger query exceeds private API row limit")
        rows.sort(
            key=lambda row: _fact_datetime(row.get(timestamp_field)),
            reverse=order == "desc",
        )
        return rows
    if suffix in _SQLITE_LEDGER_SUFFIXES:
        lower_bound = cutoff.isoformat() if cutoff is not None else None
        upper_bound = before.isoformat() if before is not None else None
        # One SELECT/fetchall call observes one SQLite read snapshot. Reopening
        # connections between OFFSET pages would permit concurrent appends to
        # duplicate or omit facts in an aggregate response.
        rows = query_sqlite_facts(
            path,
            fact_type=fact_type,
            filters=filters,
            occurred_or_observed_at_gte=lower_bound,
            occurred_or_observed_at_lt=upper_bound,
            order=order,
            limit=_PRIVATE_API_MAX_ROWS + 1,
            contract_validation=fact_type != "usage_event_v1",
        )
        if len(rows) > _PRIVATE_API_MAX_ROWS:
            raise PrivateLedgerQueryTooLarge("ledger query exceeds private API row limit")
        return rows
    raise UnsupportedLedgerSuffix(f"unsupported ledger suffix: {suffix or '<none>'}")


def _days_argument() -> tuple[int | None, tuple[Any, int] | None]:
    """Parse and validate ``?days=`` without anchoring it to a window end."""
    days_text = request.args.get("days")
    if days_text is None:
        return None, None
    try:
        days = int(days_text)
    except ValueError:
        return None, (jsonify({"error": "days must be an integer"}), 400)
    if days < 0:
        return None, (jsonify({"error": "days must be nonnegative"}), 400)
    if days > _MAX_PRIVATE_API_DAYS:
        return None, (jsonify({"error": f"days must be no greater than {_MAX_PRIVATE_API_DAYS}"}), 400)
    return days, None


def _before_argument() -> tuple[datetime | None, tuple[Any, int] | None]:
    """Parse ``?before=``, the exclusive upper timestamp bound and paging cursor."""
    before_text = request.args.get("before")
    if before_text is None:
        return None, None
    try:
        canonical = canonical_timestamp(before_text)
    except ValidationError:
        return None, (jsonify({"error": "before must be an RFC 3339 timestamp"}), 400)
    return _fact_datetime(canonical), None


def _window_timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _window_arguments(
    now: datetime,
) -> tuple[datetime | None, datetime | None, dict[str, Any], tuple[Any, int] | None]:
    """Resolve the ``[cutoff, before)`` fact window for a private canonical read.

    Keeper ruling 2026-08-29: an unbounded private read returns at most the
    latest ``_DEFAULT_PRIVATE_API_WINDOW_DAYS`` days — enough for an 8-day
    reconstruction — and anything older is fetched in pages. ``before`` is the
    paging cursor: each page is the disjoint half-open window
    ``[before - days, before)``, so consecutive pages meet at an exact
    timestamp boundary and can neither overlap nor skip rows even while
    writers append (fact timestamps are immutable, unlike OFFSET positions
    across reopened connections — see ``_private_facts``). An explicit
    ``days`` without ``before`` keeps its historical now-anchored,
    upper-unbounded semantics, with ``_PRIVATE_API_MAX_ROWS`` as the backstop
    for pathological explicit windows.

    Returns ``(cutoff, before, window_metadata, error_response)``.
    """
    before, before_error = _before_argument()
    if before_error is not None:
        return None, None, {}, before_error
    days, days_error = _days_argument()
    if days_error is not None:
        return None, None, {}, days_error
    window: dict[str, Any] = {"days": days}
    if days is not None and before is None:
        try:
            cutoff = now - timedelta(days=days)
        except OverflowError:
            return None, None, {}, (jsonify({"error": "days is outside the supported range"}), 400)
        return cutoff, None, window, None
    page_days = days if days is not None else _DEFAULT_PRIVATE_API_WINDOW_DAYS
    if days is None:
        window["default_days"] = _DEFAULT_PRIVATE_API_WINDOW_DAYS
    anchor = now if before is None else before
    try:
        cutoff = anchor - timedelta(days=page_days)
    except OverflowError:
        return None, None, {}, (
            jsonify({"error": "the requested window is outside the supported range"}),
            400,
        )
    if before is not None:
        window["before"] = _window_timestamp(before)
    window["next_before"] = _window_timestamp(cutoff)
    return cutoff, before, window, None


def _hours_argument() -> tuple[int | None, tuple[Any, int] | None]:
    hours_text = request.args.get("hours")
    if hours_text is None:
        return None, None
    if request.args.get("days") is not None:
        return None, (jsonify({"error": "days and hours are mutually exclusive"}), 400)
    if request.args.get("before") is not None:
        return None, (jsonify({"error": "before and hours are mutually exclusive"}), 400)
    try:
        hours = int(hours_text)
    except ValueError:
        return None, (jsonify({"error": "hours must be an integer"}), 400)
    if not 1 <= hours <= _MAX_UNIFIED_SERIES_HOURS:
        return None, (
            jsonify({"error": f"hours must be between 1 and {_MAX_UNIFIED_SERIES_HOURS}"}),
            400,
        )
    return hours, None


def _limit_argument(default: int = _DEFAULT_DATA_ROW_LIMIT) -> tuple[int | None, tuple[Any, int] | None]:
    """Parse ``?limit=``; ``None`` means "every row" and ``0`` requests it explicitly."""

    limit_text = request.args.get("limit")
    if limit_text is None:
        return default, None
    try:
        limit = int(limit_text)
    except ValueError:
        return None, (jsonify({"error": "limit must be an integer"}), 400)
    if limit < 0:
        return None, (jsonify({"error": "limit must be nonnegative"}), 400)
    return (None if limit == 0 else limit), None


def _iso_z(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _accounted_tokens(row: dict[str, Any]) -> int:
    return sum(int(row.get(field) or 0) for field in _ADDITIVE_TOKEN_FIELDS)


def _is_substituted_event(row: dict[str, Any]) -> bool:
    """Served-model substitution: the provider named a model, and it is not the
    one the caller asked for.

    NULL ``model_reported`` is the source declining to claim a served model, not
    a claim that the served model matched — so a NULL never counts as a
    substitution and never counts as a non-substitution either. It is simply not
    evidence. (Reported-model capture went live 2026-08-29; every older row is
    NULL forever by decision, so this count is only meaningful forward.)
    """
    reported = row.get("model_reported")
    if reported is None:
        return False
    return reported != row.get("model_requested")


def _build_provider_status_series(
    rows: list[dict[str, Any]],
    *,
    window_start: datetime,
    bucket_count: int,
) -> list[dict[str, Any]]:
    """Per provider per complete UTC hour: traffic, failures, dominant class.

    ``request_status`` and ``error_class`` ride every canonical usage event, so
    an uptime read needs no new collection — only this projection. Only the
    explicit ``error`` status counts as a failure: ``unknown`` is a source that
    does not report status, and ``cancelled`` is the operator stopping a call,
    neither of which is the provider failing.
    """
    events: dict[str, list[int]] = {}
    errors: dict[str, list[int]] = {}
    classes: dict[str, list[dict[str, int]]] = {}

    for row in rows:
        occurred_at = _fact_datetime(row.get("occurred_at"))
        bucket_index = int((occurred_at - window_start).total_seconds() // 3600)
        if bucket_index < 0 or bucket_index >= bucket_count:
            continue
        provider = str(row.get("provider") or "unknown")
        if provider not in events:
            events[provider] = [0] * bucket_count
            errors[provider] = [0] * bucket_count
            classes[provider] = [{} for _index in range(bucket_count)]
        events[provider][bucket_index] += 1
        if row.get("request_status") != "error":
            continue
        errors[provider][bucket_index] += 1
        error_class = row.get("error_class")
        if error_class is None:
            continue
        counts = classes[provider][bucket_index]
        name = str(error_class)
        counts[name] = counts.get(name, 0) + 1

    def dominant(counts: dict[str, int]) -> str | None:
        if not counts:
            return None
        # Deterministic ties: highest count, then lexicographic name.
        return sorted(counts.items(), key=lambda item: (-item[1], item[0]))[0][0]

    series = []
    for provider in events:
        window_classes: dict[str, int] = {}
        for counts in classes[provider]:
            for name, total in counts.items():
                window_classes[name] = window_classes.get(name, 0) + total
        series.append(
            {
                "provider": provider,
                "provider_canonical": _canonical_quota_provider(provider),
                "events": events[provider],
                "errors": errors[provider],
                "top_error_classes": [dominant(counts) for counts in classes[provider]],
                "total_events": sum(events[provider]),
                "total_errors": sum(errors[provider]),
                "dominant_error_class": dominant(window_classes),
            }
        )
    series.sort(key=lambda item: (-item["total_events"], item["provider"]))
    return series[:_UNIFIED_STATUS_PROVIDER_LIMIT]


def _build_unified_time_series(
    rows: list[dict[str, Any]],
    *,
    window_start: datetime,
    window_end: datetime,
    generated_at: datetime,
) -> dict[str, Any]:
    bucket_count = int((window_end - window_start).total_seconds() // 3600)
    bucket_starts = [window_start + timedelta(hours=index) for index in range(bucket_count)]
    model_buckets: dict[tuple[str, str], list[int]] = {}

    for row in rows:
        occurred_at = _fact_datetime(row.get("occurred_at"))
        bucket_index = int((occurred_at - window_start).total_seconds() // 3600)
        if bucket_index < 0 or bucket_index >= bucket_count:
            continue
        total = _accounted_tokens(row)
        provider = str(row.get("provider") or "unknown")
        model = str(row.get("model_reported") or row.get("model_requested") or "unknown")
        values = model_buckets.setdefault((provider, model), [0] * bucket_count)
        values[bucket_index] += total

    ranked_models = sorted(
        model_buckets.items(),
        key=lambda item: (-sum(item[1]), item[0][0], item[0][1]),
    )
    named_models = ranked_models[:_UNIFIED_MODEL_SERIES_LIMIT]
    remaining_models = ranked_models[_UNIFIED_MODEL_SERIES_LIMIT:]
    model_series = [
        {
            "provider": provider,
            "model": model,
            "label": f"{model} · {provider}",
            "total_tokens": sum(values),
            "values": values,
        }
        for (provider, model), values in named_models
    ]
    other_model_series = None
    if remaining_models:
        other_values = [
            sum(values[index] for _key, values in remaining_models)
            for index in range(bucket_count)
        ]
        other_model_series = {
            "label": "Other models",
            "member_count": len(remaining_models),
            "total_tokens": sum(other_values),
            "values": other_values,
        }

    return {
        "generated_at": _iso_z(generated_at),
        "bucket_minutes": 60,
        "bucket_count": bucket_count,
        "window_start": _iso_z(window_start),
        "window_end": _iso_z(window_end),
        "bucket_starts": [_iso_z(value) for value in bucket_starts],
        "model_series": model_series,
        "other_model_series": other_model_series,
        # The Codex-vs-Claude two-series comparison was retired with its card
        # (keeper ruling, 2026-08-30): the provider donuts carry share now, and
        # this projection existed only to feed that one chart.
        "provider_status_series": _build_provider_status_series(
            rows, window_start=window_start, bucket_count=bucket_count
        ),
    }


# ── Provider status feeds (keeper-ruled 2026-08-30: API liveness comes from
# the provider's own status API, not inferred from the ledger; the observed
# cells remain as a secondary "what our calls saw" line). Statuspage v2
# summary endpoints, verified live 2026-08-30; anthropic redirects to
# status.claude.com. Providers without a public feed are honestly feedless.
_PROVIDER_STATUS_FEEDS: dict[str, str] = {
    "anthropic": "https://status.claude.com/api/v2/summary.json",
    "openai": "https://status.openai.com/api/v2/summary.json",
    "openai-codex": "https://status.openai.com/api/v2/summary.json",
    "kimi-coding": "https://status.moonshot.cn/api/v2/summary.json",
}
_PROVIDER_STATUS_TTL_SECONDS = 120.0
_provider_status_cache: dict[str, tuple[float, dict[str, Any]]] = {}
_provider_status_lock = threading.Lock()


def _fetch_status_feed(url: str, *, timeout: float = 6.0) -> dict[str, Any]:
    """One GET against a public statuspage summary endpoint. No credentials
    leave the box; failures degrade to an explicit unreachable marker rather
    than an inferred status."""
    import urllib.request

    request_obj = urllib.request.Request(
        url, headers={"User-Agent": "codex-usage-tracker/private-dashboard"}
    )
    with urllib.request.urlopen(request_obj, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def _provider_status_snapshot(
    now: float,
    fetcher=None,
) -> dict[str, dict[str, Any]]:
    """Per-URL cached (TTL 120s) statuspage reads, projected per provider."""
    if fetcher is None:
        fetcher = _fetch_status_feed
    by_url: dict[str, dict[str, Any]] = {}
    for url in set(_PROVIDER_STATUS_FEEDS.values()):
        with _provider_status_lock:
            cached = _provider_status_cache.get(url)
        if cached is not None and now - cached[0] < _PROVIDER_STATUS_TTL_SECONDS:
            by_url[url] = cached[1]
            continue
        try:
            raw = fetcher(url)
            status = raw.get("status") or {}
            incidents = [
                str(item.get("name"))
                for item in (raw.get("incidents") or [])
                if item.get("name")
            ]
            entry = {
                "indicator": str(status.get("indicator") or "unknown"),
                "description": str(status.get("description") or ""),
                "incidents": incidents[:5],
                "unreachable": False,
            }
        except Exception as exc:  # noqa: BLE001 — any transport/parse failure
            entry = {
                "indicator": "unknown",
                "description": f"status feed unreachable ({type(exc).__name__})",
                "incidents": [],
                "unreachable": True,
            }
        with _provider_status_lock:
            _provider_status_cache[url] = (now, entry)
        by_url[url] = entry
    return {
        provider: {**by_url[url], "feed_url": url}
        for provider, url in _PROVIDER_STATUS_FEEDS.items()
    }


def _canonical_quota_provider(value: Any) -> str:
    raw = "unknown" if value is None else str(value).strip()
    if not raw:
        raw = "unknown"
    return _QUOTA_PROVIDER_ALIASES.get(raw.casefold(), raw)


def _quota_provider_label(provider: str) -> str:
    return _QUOTA_PROVIDER_LABELS.get(provider, provider)


def _quota_utilization_pct(row: dict[str, Any]) -> float | None:
    """Return a bounded, deterministic ratio without inferring missing values."""
    used_raw = row.get("used_value")
    limit_raw = row.get("limit_value")
    if used_raw is None or limit_raw is None or isinstance(used_raw, bool) or isinstance(limit_raw, bool):
        return None
    try:
        used = Decimal(str(used_raw))
        limit = Decimal(str(limit_raw))
        if not used.is_finite() or not limit.is_finite():
            return None
        if used < 0 or limit <= 0:
            return None
        ratio = used / limit * Decimal("100")
        if not ratio.is_finite() or ratio < 0 or ratio > 100:
            return None
        return float(ratio.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP))
    except (ArithmeticError, InvalidOperation, TypeError, ValueError):
        return None


def _quota_row_confidence(row: dict[str, Any]) -> str:
    coverage = row.get("x_coverage")
    if isinstance(coverage, dict) and coverage.get("status") == "partial":
        return "partial"
    confidence = str(row.get("measurement_confidence") or "unknown").casefold()
    if confidence in {"exact", "estimated", "reconstructed", "partial"}:
        return confidence
    return "unknown"


def _quota_series_confidence(rows: list[dict[str, Any]]) -> str:
    statuses = {_quota_row_confidence(row) for row in rows}
    if len(statuses) == 1:
        return next(iter(statuses))
    return "mixed" if statuses else "unknown"


def _quota_observation_order_key(
    row: dict[str, Any],
) -> tuple[int, datetime, Decimal, str, str, str]:
    """Order observations independent of backend; the lexicographically greatest tie wins."""
    canonical_row = json.dumps(
        row,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        default=str,
    )
    timestamp_key = _rfc3339_order_key(row.get("observed_at"))
    if timestamp_key is None:
        valid_timestamp = 0
        whole_second = datetime.min.replace(tzinfo=timezone.utc)
        fraction = Decimal(0)
    else:
        valid_timestamp = 1
        whole_second, fraction = timestamp_key
    return (
        valid_timestamp,
        whole_second,
        fraction,
        str(row.get("source_namespace") or ""),
        str(row.get("source_observation_id") or ""),
        canonical_row,
    )


def _quota_series_identity(row: dict[str, Any]) -> _QuotaSeriesIdentity:
    """Return the complete private identity used to prevent cross-account merging."""
    return (
        _canonical_quota_provider(row.get("provider")),
        str(row.get("quota_name") or "unknown"),
        str(row.get("harness") or "unknown"),
        str(row.get("quota_scope") or "unknown"),
        str(row.get("window_kind") or "unknown"),
        str(row.get("unit") or "unknown"),
        _quota_account_identity(row),
    )


def _quota_account_identity(row: dict[str, Any]) -> _QuotaAccountIdentity:
    """Keep the complete account reference private while distinguishing null from text."""
    account_ref = row.get("account_ref")
    return account_ref is not None, str(account_ref or "")


def _quota_account_sort_key(
    identity: _QuotaAccountIdentity,
) -> tuple[bool, str, str]:
    account_is_present, account_ref = identity
    return account_is_present, account_ref.casefold(), account_ref


def _quota_identity_sort_key(
    identity: _QuotaSeriesIdentity,
) -> tuple[tuple[tuple[str, str], ...], tuple[bool, str, str]]:
    account_is_present, account_ref = identity[6]
    return (
        tuple((value.casefold(), value) for value in identity[:6]),
        _quota_account_sort_key((account_is_present, account_ref)),
    )


def _build_primary_quota_projection(
    *,
    primary_quotas: dict[str, str],
    period_name: str,
    label: str,
    description: str,
    identities_by_provider_quota: dict[tuple[str, str], list[_QuotaSeriesIdentity]],
    provider_account_indexes: dict[str, dict[_QuotaAccountIdentity, int]],
    entries: dict[_QuotaSeriesIdentity, dict[str, Any]],
    unmapped_status: str | None = None,
) -> dict[str, Any]:
    """Select one explicitly mapped quota family without merging account series."""
    unified_series: list[dict[str, Any]] = []
    no_data_providers: list[dict[str, Any]] = []
    for provider in _QUOTA_PROVIDER_ORDER:
        quota_name = primary_quotas.get(provider)
        if quota_name is None:
            no_data_providers.append(
                {
                    "provider": provider,
                    "label": _QUOTA_PROVIDER_LABELS[provider],
                    "quota_name": None,
                    "status": unmapped_status or f"primary_{period_name}_quota_not_mapped",
                }
            )
            continue

        primary_identities = sorted(
            identities_by_provider_quota.get((provider, quota_name), []),
            key=lambda identity: (
                provider_account_indexes[provider][identity[6]],
                _quota_identity_sort_key(identity),
            ),
        )
        chartable_identities = [
            identity for identity in primary_identities if entries[identity]["chartable"]
        ]
        if chartable_identities:
            account_count = len(provider_account_indexes[provider])
            for identity in chartable_identities:
                account_index = provider_account_indexes[provider][identity[6]]
                provider_label = _QUOTA_PROVIDER_LABELS[provider]
                series_payload = {
                    **entries[identity]["payload"],
                    "label": provider_label,
                }
                if account_count > 1:
                    series_payload["label"] = (
                        f"{provider_label} · account {account_index}"
                    )
                unified_series.append(series_payload)
            continue

        status = (
            f"no_comparable_{period_name}_utilization"
            if primary_identities
            else f"primary_{period_name}_quota_not_reported"
        )
        no_data_providers.append(
            {
                "provider": provider,
                "label": _QUOTA_PROVIDER_LABELS[provider],
                "quota_name": quota_name,
                "status": status,
            }
        )

    return {
        "label": label,
        "description": description,
        "series": unified_series,
        "no_data_providers": no_data_providers,
    }


def _build_subscription_time_series(
    rows: list[dict[str, Any]],
    *,
    window_start: datetime,
    window_end: datetime,
    generated_at: datetime,
) -> dict[str, Any]:
    """Project canonical quota observations into complete UTC hourly buckets."""
    bucket_count = int((window_end - window_start).total_seconds() // 3600)
    bucket_starts = [window_start + timedelta(hours=index) for index in range(bucket_count)]
    grouped: dict[_QuotaSeriesIdentity, list[dict[str, Any]]] = {}
    provider_rows: dict[str, list[dict[str, Any]]] = {}
    provider_raw_ids: dict[str, set[str]] = {}

    for row in rows:
        observed_at = _fact_datetime(row.get("observed_at"))
        if observed_at < window_start or observed_at >= window_end:
            continue
        identity = _quota_series_identity(row)
        provider = identity[0]
        grouped.setdefault(identity, []).append(row)
        provider_rows.setdefault(provider, []).append(row)
        raw_provider = str(row.get("provider") or "unknown").strip() or "unknown"
        provider_raw_ids.setdefault(provider, set()).add(raw_provider)

    identities_by_provider_quota: dict[
        tuple[str, str], list[_QuotaSeriesIdentity]
    ] = {}
    for identity in grouped:
        identities_by_provider_quota.setdefault(identity[:2], []).append(identity)

    provider_account_indexes: dict[
        str, dict[_QuotaAccountIdentity, int]
    ] = {}
    for provider in {identity[0] for identity in grouped}:
        ordered_accounts = sorted(
            {identity[6] for identity in grouped if identity[0] == provider},
            key=_quota_account_sort_key,
        )
        provider_account_indexes[provider] = {
            account_identity: index
            for index, account_identity in enumerate(ordered_accounts, start=1)
        }

    labels: dict[_QuotaSeriesIdentity, str] = {}
    for identities in identities_by_provider_quota.values():
        ordered = sorted(identities, key=_quota_identity_sort_key)
        preliminary: dict[_QuotaSeriesIdentity, str] = {}
        for identity in ordered:
            quota_name = identity[1].replace("_", " ")
            account_index = provider_account_indexes[identity[0]][identity[6]]
            preliminary[identity] = (
                quota_name
                if len(ordered) == 1
                else f"{quota_name} · {identity[2]} · account {account_index}"
            )
        duplicate_counts = {
            label: sum(candidate == label for candidate in preliminary.values())
            for label in preliminary.values()
        }
        for identity, label in preliminary.items():
            if duplicate_counts[label] > 1:
                label = f"{label} · {identity[3]} · {identity[4]} · {identity[5]}"
            labels[identity] = label

    entries: dict[_QuotaSeriesIdentity, dict[str, Any]] = {}
    for identity in sorted(grouped, key=_quota_identity_sort_key):
        provider, quota_name, harness, _quota_scope, _window_kind, unit, _account_ref = identity
        quota_rows = grouped[identity]
        provider_account_index = provider_account_indexes[provider][_account_ref]
        provider_account_count = len(provider_account_indexes[provider])
        bucket_rows: list[dict[str, Any] | None] = [None] * bucket_count
        for row in sorted(quota_rows, key=_quota_observation_order_key):
            observed_at = _fact_datetime(row.get("observed_at"))
            bucket_index = int((observed_at - window_start).total_seconds() // 3600)
            if 0 <= bucket_index < bucket_count:
                # The greatest deterministic key is the last observation in a tied hour.
                bucket_rows[bucket_index] = row
        values = [
            _quota_utilization_pct(row) if row is not None else None
            for row in bucket_rows
        ]
        selected_rows = [row for row in bucket_rows if row is not None]
        valid_rows = [
            row
            for row, value in zip(bucket_rows, values)
            if row is not None and value is not None
        ]
        if valid_rows:
            latest = max(valid_rows, key=_quota_observation_order_key)
            payload = {
                "provider": provider,
                "label": labels[identity],
                "quota_name": quota_name,
                "raw_provider_ids": sorted(
                    {str(row.get("provider") or "unknown") for row in quota_rows}
                ),
                "harness": harness,
                "unit": unit,
                "used_value": latest.get("used_value"),
                "limit_value": latest.get("limit_value"),
                "remaining_value": latest.get("remaining_value"),
                "latest_observed_at": latest.get("observed_at"),
                "measurement_confidence": latest.get("measurement_confidence"),
                "confidence_status": _quota_series_confidence(valid_rows),
                "sample_count": len(valid_rows),
                "observation_count": len(quota_rows),
                "provider_account_index": provider_account_index,
                "provider_account_count": provider_account_count,
                "values": values,
            }
            entries[identity] = {"chartable": True, "payload": payload}
        else:
            latest = max(selected_rows, key=_quota_observation_order_key)
            payload = {
                "label": labels[identity],
                "quota_name": quota_name,
                "harness": harness,
                "unit": unit,
                "measurement_confidence": latest.get("measurement_confidence"),
                "confidence_status": _quota_series_confidence(selected_rows),
                "sample_count": 0,
                "observation_count": len(quota_rows),
                "provider_account_index": provider_account_index,
                "provider_account_count": provider_account_count,
                "reason": "no_comparable_utilization",
            }
            entries[identity] = {"chartable": False, "payload": payload}

    def provider_rank(provider: str) -> tuple[int, str, str]:
        try:
            return _QUOTA_PROVIDER_ORDER.index(provider), "", ""
        except ValueError:
            return len(_QUOTA_PROVIDER_ORDER), provider.casefold(), provider

    all_providers = sorted(
        set(_QUOTA_PROVIDER_ORDER).union(provider_rows),
        key=provider_rank,
    )
    included_providers = all_providers[:_SUBSCRIPTION_CHART_PROVIDER_LIMIT]
    providers: list[dict[str, Any]] = []
    included_series_count = 0
    for provider in included_providers:
        provider_identities = sorted(
            (identity for identity in grouped if identity[0] == provider),
            key=lambda identity: (
                0 if entries[identity]["chartable"] else 1,
                _quota_identity_sort_key(identity),
            ),
        )
        selected_identities = provider_identities[
            :_SUBSCRIPTION_CHART_SERIES_PER_PROVIDER_LIMIT
        ]
        included_series_count += len(selected_identities)
        chart_series = [
            entries[identity]["payload"]
            for identity in selected_identities
            if entries[identity]["chartable"]
        ]
        unavailable_series = [
            entries[identity]["payload"]
            for identity in selected_identities
            if not entries[identity]["chartable"]
        ]
        if not provider_identities:
            status = "no_observations"
        elif any(entries[identity]["chartable"] for identity in provider_identities):
            status = "available"
        else:
            status = "no_comparable_utilization"
        providers.append(
            {
                "provider": provider,
                "label": _quota_provider_label(provider),
                "raw_provider_ids": sorted(provider_raw_ids.get(provider, set())),
                "observation_count": len(provider_rows.get(provider, [])),
                "status": status,
                "series": chart_series,
                "unavailable_series": unavailable_series,
            }
        )

    unified_weekly = _build_primary_quota_projection(
        primary_quotas=_PRIMARY_WEEKLY_QUOTAS,
        period_name="weekly",
        label="Unified weekly subscription utilization",
        description=(
            "Each named provider's explicitly mapped primary weekly-equivalent quota "
            "on a normalized 0–100% scale; this does not compare tokens or dollars."
        ),
        identities_by_provider_quota=identities_by_provider_quota,
        provider_account_indexes=provider_account_indexes,
        entries=entries,
    )
    unified_five_hour = _build_primary_quota_projection(
        primary_quotas=_PRIMARY_FIVE_HOUR_QUOTAS,
        period_name="five_hour",
        label="Unified five-hour subscription utilization",
        description=(
            "Each named provider's explicitly mapped comparable five-hour quota on a "
            "normalized 0–100% scale; this does not compare tokens or dollars. "
            "OpenAI / Codex reports no comparable five-hour quota in the canonical "
            "ledger, and weekly data is never substituted."
        ),
        identities_by_provider_quota=identities_by_provider_quota,
        provider_account_indexes=provider_account_indexes,
        entries=entries,
        unmapped_status="comparable_five_hour_quota_not_reported",
    )

    total_series_count = len(grouped)
    truncation = {
        "provider_limit": _SUBSCRIPTION_CHART_PROVIDER_LIMIT,
        "providers_included": len(included_providers),
        "providers_omitted": len(all_providers) - len(included_providers),
        "providers_total": len(all_providers),
        "providers_truncated": len(included_providers) < len(all_providers),
        "series_included": included_series_count,
        "series_limit_per_provider": _SUBSCRIPTION_CHART_SERIES_PER_PROVIDER_LIMIT,
        "series_omitted": total_series_count - included_series_count,
        "series_total": total_series_count,
        "series_truncated": included_series_count < total_series_count,
    }
    return {
        "generated_at": _iso_z(generated_at),
        "bucket_minutes": 60,
        "bucket_count": bucket_count,
        "window_start": _iso_z(window_start),
        "window_end": _iso_z(window_end),
        "bucket_starts": [_iso_z(value) for value in bucket_starts],
        "providers": providers,
        "truncation": truncation,
        "unified_weekly": unified_weekly,
        "unified_five_hour": unified_five_hour,
    }


def _parse_rows(path: Path) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as fp:
        for line in fp:
            text = line.strip()
            if not text:
                continue
            try:
                payload = json.loads(text)
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict):
                payload = reconcile_snapshot_windows(payload)
                if "rate_limit_reset_credits_available" not in payload:
                    payload["rate_limit_reset_credits_available"] = _reset_credits_available(payload)
                rows.append(payload)

    rows.sort(key=lambda row: _normalize_timestamp(row.get("fetched_at")), reverse=True)
    return rows


def _load_rows(ledger_path: str) -> List[Dict[str, Any]]:
    """Return newest-first ledger rows, reparsing only when the file changes.

    The JSONL ledger is multi-megabyte and five endpoints read it per dashboard
    refresh, so one parse is cached per (path, mtime, size) and shared. Callers
    get their own list; the row dicts are shared and must be treated read-only
    (every current consumer clones rows before annotating them).
    """

    path = Path(ledger_path).expanduser()
    try:
        stat = path.stat()
    except OSError:
        return []
    signature = (stat.st_mtime_ns, stat.st_size)
    key = str(path)
    with _LEDGER_CACHE_LOCK:
        cached = _LEDGER_CACHE.get(key)
        if cached is not None and cached[0] == signature:
            return list(cached[1])
        rows = _parse_rows(path)
        if key not in _LEDGER_CACHE and len(_LEDGER_CACHE) >= _LEDGER_CACHE_MAX_ENTRIES:
            _LEDGER_CACHE.clear()
        _LEDGER_CACHE[key] = (signature, rows)
        return list(rows)


def _latest_row(rows: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    return rows[0] if rows else None


def _to_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _decimal_text(value: Decimal) -> str:
    if value == 0:
        return "0"
    rendered = format(value, "f")
    return rendered.rstrip("0").rstrip(".") if "." in rendered else rendered


def _to_int(value: Any) -> Optional[int]:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _reset_credits_available(row: Dict[str, Any]) -> Optional[int]:
    explicit = _to_int(row.get("rate_limit_reset_credits_available"))
    if explicit is not None:
        return explicit
    raw_payload = row.get("raw_payload")
    if not isinstance(raw_payload, dict):
        return None
    reset_credits = raw_payload.get("rate_limit_reset_credits")
    if not isinstance(reset_credits, dict):
        return None
    return _to_int(reset_credits.get("available_count"))


def create_app(
    atrium_root: str = DEFAULT_ATRIUM_ROOT,
    ledger: Optional[str] = None,
    unified_usage_ledger: Optional[str] = None,
    quota_ledger: Optional[str] = None,
    billing_ledger: Optional[str] = None,
) -> Flask:
    resolved_ledger_path = _resolve_ledger_path(atrium_root, ledger)
    resolved_unified_usage = unified_usage_ledger or os.environ.get("UNIFIED_USAGE_LEDGER_PATH")
    resolved_quota_ledger = quota_ledger or os.environ.get("QUOTA_LEDGER_PATH")
    resolved_billing_ledger = billing_ledger or os.environ.get("BILLING_LEDGER_PATH")
    app = Flask(__name__)

    def _load_ledger_rows() -> List[Dict[str, Any]]:
        return _load_rows(resolved_ledger_path)

    @app.route("/")
    def dashboard_page():
        return render_template("index.html")

    @app.route("/api/data")
    def api_data():
        limit, limit_error = _limit_argument()
        if limit_error is not None:
            return limit_error
        rows = _load_ledger_rows()
        if limit is not None:
            rows = rows[:limit]
        return jsonify(rows)

    @app.route("/api/summary")
    def api_summary():
        rows = _load_ledger_rows()
        current = _latest_row(rows)
        if current is None:
            return jsonify(
                {
                    "total_rows": 0,
                    "first_fetched_at": None,
                    "last_fetched_at": None,
                    "current_session_used_pct": None,
                    "current_weekly_used_pct": None,
                    "current_spark_session_used_pct": None,
                    "current_spark_weekly_used_pct": None,
                    "session_reset_at": None,
                    "weekly_reset_at": None,
                    "spark_session_reset_at": None,
                    "spark_weekly_reset_at": None,
                    "plan_type": None,
                    "allowed": None,
                    "limit_reached": None,
                    "rate_limit_reset_credits_available": None,
                    "ledger_path": resolved_ledger_path,
                }
            )

        first = rows[-1].get("fetched_at")
        last = current.get("fetched_at")
        rate_limit = current.get("raw_payload", {}).get("rate_limit", {})
        allowed = rate_limit.get("allowed", True) if isinstance(rate_limit, dict) else True
        limit_reached = rate_limit.get("limit_reached", False) if isinstance(rate_limit, dict) else False

        return jsonify(
            {
                "total_rows": len(rows),
                "first_fetched_at": first,
                "last_fetched_at": last,
                "current_session_used_pct": _to_float(current.get("session_used_pct")),
                "current_weekly_used_pct": _to_float(current.get("weekly_used_pct")),
                "current_spark_session_used_pct": _to_float(current.get("spark_session_used_pct")),
                "current_spark_weekly_used_pct": _to_float(current.get("spark_weekly_used_pct")),
                "session_reset_at": current.get("session_reset_at"),
                "weekly_reset_at": current.get("weekly_reset_at"),
                "spark_session_reset_at": current.get("spark_session_reset_at"),
                "spark_weekly_reset_at": current.get("spark_weekly_reset_at"),
                "plan_type": current.get("plan_type"),
                "allowed": allowed,
                "limit_reached": limit_reached,
                "rate_limit_reset_credits_available": _reset_credits_available(current),
                "ledger_path": resolved_ledger_path,
            }
        )

    @app.route("/api/trend")
    def api_trend():
        rows = _load_ledger_rows()[:168]
        rows.reverse()
        trend_rows = [
            {
                "fetched_at": row.get("fetched_at"),
                "session_used_pct": row.get("session_used_pct"),
                "weekly_used_pct": row.get("weekly_used_pct"),
                "spark_session_used_pct": row.get("spark_session_used_pct"),
                "spark_weekly_used_pct": row.get("spark_weekly_used_pct"),
            }
            for row in rows
        ]
        return jsonify(trend_rows)

    def _build_token_rows() -> List[Dict[str, Any]]:
        return build_token_correlation_rows(
            _load_ledger_rows(),
            state_db_path=resolve_state_db_path(),
            limit=168,
        )

    @app.route("/api/token-ledger")
    def api_token_ledger():
        token_rows = _build_token_rows()
        token_rows.reverse()
        return jsonify(token_rows)

    @app.route("/api/token-chart")
    def api_token_chart():
        chart_rows = []
        for row in _build_token_rows():
            chart_rows.append(
                {
                    "window_start": row.get("window_start"),
                    "window_end": row.get("window_end"),
                    "session_used_pct": row.get("session_used_pct_end"),
                    "weekly_used_pct": row.get("weekly_used_pct_end"),
                    "session_delta_pct": row.get("session_delta_pct"),
                    "weekly_delta_pct": row.get("weekly_delta_pct"),
                    "api_calls": row.get("api_calls"),
                    "input_tokens": row.get("input_tokens"),
                    "cache_read_tokens": row.get("cache_read_tokens"),
                    "cache_write_tokens": row.get("cache_write_tokens"),
                    "noncached_prompt_tokens": int(row.get("input_tokens") or 0) + int(row.get("cache_write_tokens") or 0),
                    "output_tokens": row.get("output_tokens"),
                    "reasoning_tokens": row.get("reasoning_tokens"),
                    "prompt_tokens": row.get("prompt_tokens"),
                    "total_tokens": row.get("total_tokens"),
                    "cache_hit_pct": row.get("cache_hit_pct"),
                    "reset_or_drop": row.get("reset_or_drop"),
                }
            )
        return jsonify(chart_rows)

    @app.route("/api/codex-call-accounting")
    def api_codex_call_accounting():
        return jsonify(
            build_codex_accounting_rows(
                _load_ledger_rows(),
                state_db_path=resolve_state_db_path(),
                limit=168,
            )
        )

    @app.route("/api/burn-projection")
    def api_burn_projection():
        return jsonify(build_burn_projection_rows(_load_ledger_rows()))

    @app.route("/api/policy-state")
    def api_policy_state():
        return jsonify(latest_policy_state(_load_ledger_rows()))

    @app.route("/api/budget-state")
    def api_budget_state():
        # Keep the budget view coherent with the dashboard's latest Codex
        # snapshot. On a healthy deployment that snapshot is hourly; when it is
        # stale, the response's week bounds make the historical anchor visible.
        latest = _latest_row(_load_ledger_rows())
        as_of = _fact_datetime(latest.get("fetched_at")) if latest else None
        return jsonify(latest_budget_state(read_provider_spend_rows(atrium_root), as_of=as_of))

    @app.route("/api/capability-matrix")
    def api_capability_matrix():
        return jsonify(get_capability_matrix())

    @app.route("/api/provider-spend")
    def api_provider_spend():
        return jsonify(summarize_provider_spend(read_provider_spend_rows(atrium_root)))

    @app.route("/api/unified-usage")
    def api_unified_usage():
        if not resolved_unified_usage:
            return jsonify({"error": "unified usage ledger is not configured"}), 404
        hours, hours_error = _hours_argument()
        if hours_error is not None:
            return hours_error
        generated_at = datetime.now(timezone.utc)
        series_start = None
        series_end = None
        query_before = None
        window_meta: dict[str, Any] = {}
        if hours is not None:
            series_end = generated_at.replace(minute=0, second=0, microsecond=0)
            series_start = series_end - timedelta(hours=hours)
            cutoff = series_start
            query_before = series_end
        else:
            cutoff, query_before, window_meta, window_error = _window_arguments(generated_at)
            if window_error is not None:
                return window_error
        typed_filters = {
            field: requested
            for field in ("provider", "harness", "purpose")
            if (requested := request.args.get(field)) is not None
        }
        try:
            rows = _private_facts(
                resolved_unified_usage,
                fact_type="usage_event_v1",
                filters=typed_filters,
                cutoff=cutoff,
                before=query_before,
            )
        except UnsupportedLedgerSuffix:
            return jsonify({"error": "unsupported ledger suffix"}), 400
        except PrivateLedgerQueryTooLarge:
            return jsonify({"error": "ledger query exceeds private API row limit"}), 413
        except (MalformedLedgerError, ValidationError, IdentityConflictError):
            return jsonify({"error": "configured private ledger is unavailable"}), 503
        # The SQLite ``model`` extraction prefers model_reported, so retain the
        # endpoint's exact model_requested semantics on canonical payloads.
        requested_model = request.args.get("model_requested")
        if requested_model is not None:
            rows = [row for row in rows if row.get("model_requested") == requested_model]

        token_fields = ("input_tokens", "cache_read_tokens", "cache_write_tokens", "output_tokens", "reasoning_tokens")
        cost_fields = ("estimated_cost_usd", "actual_cost_usd")
        totals: Dict[str, Any] = {field: sum(int(row.get(field) or 0) for row in rows) for field in token_fields}
        for field in cost_fields:
            totals[field] = str(sum((Decimal(str(row.get(field))) for row in rows if row.get(field) is not None), Decimal("0")))
        # Canonical output includes reasoning when a harness reports both, so do
        # not count the diagnostic reasoning bucket a second time.
        totals["total_tokens"] = sum(totals[field] for field in token_fields if field != "reasoning_tokens")
        # Most rows are subscription-included and carry no price at all; count
        # them so the UI can say "$X across N priced events" instead of "$0".
        priced_events = sum(
            1 for row in rows if any(row.get(field) is not None for field in cost_fields)
        )
        totals["priced_events"] = priced_events
        totals["unpriced_events"] = len(rows) - priced_events
        # Served-model substitution watch: a NULL model_reported is not a claim
        # that the served model matched, so it never counts here.
        totals["substituted_events"] = sum(1 for row in rows if _is_substituted_event(row))
        grouped: Dict[tuple[str, str], Dict[str, Any]] = {}
        by_harness: Dict[str, Dict[str, Any]] = {}
        by_provider: Dict[str, Dict[str, Any]] = {}
        for row in rows:
            key = (
                str(row.get("provider") or "unknown"),
                str(row.get("model_reported") or row.get("model_requested") or "unknown"),
            )
            bucket = grouped.setdefault(
                key,
                {
                    "provider": key[0],
                    "model": key[1],
                    "events": 0,
                    "api_modes": {},
                    **{field: 0 for field in token_fields},
                    **{field: "0" for field in cost_fields},
                },
            )
            bucket["events"] += 1
            # Endpoint/protocol attribution — restored upstream for Hermes on
            # 2026-08-29; NULL means the source genuinely does not know.
            mode_key = str(row.get("api_mode") or "unknown")
            bucket["api_modes"][mode_key] = bucket["api_modes"].get(mode_key, 0) + 1
            for field in token_fields:
                bucket[field] += int(row.get(field) or 0)
            for field in cost_fields:
                if row.get(field) is not None:
                    bucket[field] = str(Decimal(bucket[field]) + Decimal(str(row[field])))
            harness = str(row.get("harness") or "unknown")
            harness_bucket = by_harness.setdefault(
                harness,
                {
                    "harness": harness,
                    "events": 0,
                    **{field: 0 for field in token_fields},
                    **{field: "0" for field in cost_fields},
                },
            )
            harness_bucket["events"] += 1
            for field in token_fields:
                harness_bucket[field] += int(row.get(field) or 0)
            for field in cost_fields:
                if row.get(field) is not None:
                    harness_bucket[field] = str(
                        Decimal(harness_bucket[field]) + Decimal(str(row[field]))
                    )
            # Provider-level throughput for the window donuts. The canonical key
            # is resolved here rather than in the browser so the donut legend
            # can join to a quota row without a second copy of the alias table.
            provider_bucket = by_provider.setdefault(
                key[0],
                {
                    "provider": key[0],
                    "provider_canonical": _canonical_quota_provider(key[0]),
                    "events": 0,
                    "substituted_events": 0,
                    **{field: 0 for field in token_fields},
                },
            )
            provider_bucket["events"] += 1
            if _is_substituted_event(row):
                provider_bucket["substituted_events"] += 1
            for field in token_fields:
                provider_bucket[field] += int(row.get(field) or 0)
        for bucket in by_provider.values():
            bucket["total_tokens"] = sum(
                bucket[field] for field in token_fields if field != "reasoning_tokens"
            )
            # Cached vs fresh throughput is the distinction the donut draws: a
            # cache read is billed and served differently from fresh input.
            bucket["cached_tokens"] = bucket["cache_read_tokens"]
            bucket["fresh_tokens"] = bucket["total_tokens"] - bucket["cache_read_tokens"]
        for bucket in grouped.values():
            bucket["total_tokens"] = sum(bucket[field] for field in token_fields if field != "reasoning_tokens")
        for bucket in by_harness.values():
            bucket["total_tokens"] = sum(bucket[field] for field in token_fields if field != "reasoning_tokens")
        totals["list_price"] = estimate_list_price(grouped.values())
        coverage = {
            "exact_events": sum(row.get("measurement_confidence") == "exact" for row in rows),
            "reconstructed_events": sum(row.get("measurement_confidence") == "reconstructed" for row in rows),
            "reconstructed_calls": sum(int(row.get("reconstructed_call_count") or 0) for row in rows),
        }
        window_payload: dict[str, Any] = {
            "first_occurred_at": rows[0].get("occurred_at") if rows else None,
            "last_occurred_at": rows[-1].get("occurred_at") if rows else None,
            "event_count": len(rows),
            "days": None,
            **window_meta,
        }
        if hours is not None:
            window_payload["hours"] = hours
        payload: dict[str, Any] = {
            "freshness": _ledger_freshness(
                resolved_unified_usage,
                now=generated_at,
                heartbeat_name="usage-collect.ok",
                lane_health_name="lanes-usage.json",
            ),
            "totals": totals,
            "coverage": coverage,
            "window": window_payload,
            "by_harness": sorted(by_harness.values(), key=lambda item: item["harness"]),
            "by_provider": sorted(
                by_provider.values(),
                key=lambda item: (-item["total_tokens"], item["provider"]),
            ),
            "by_provider_model": sorted(grouped.values(), key=lambda item: (item["provider"], item["model"])),
        }
        if series_start is not None and series_end is not None:
            payload["time_series"] = _build_unified_time_series(
                rows,
                window_start=series_start,
                window_end=series_end,
                generated_at=generated_at,
            )
        return jsonify(payload)

    @app.route("/api/provider-status")
    def api_provider_status():
        import time as _time

        return jsonify(
            {
                "providers": _provider_status_snapshot(_time.time()),
                "feedless": sorted(
                    provider
                    for provider in (
                        "zai", "deepseek", "openrouter", "opencode-go",
                    )
                    if provider not in _PROVIDER_STATUS_FEEDS
                ),
            }
        )

    @app.route("/api/subscriptions")
    def api_subscriptions():
        if not resolved_quota_ledger:
            return jsonify({"error": "quota ledger is not configured"}), 404
        hours, hours_error = _hours_argument()
        if hours_error is not None:
            return hours_error
        generated_at = datetime.now(timezone.utc)
        series_start = None
        series_end = None
        query_before = None
        window_meta: dict[str, Any] = {}
        if hours is not None:
            series_end = generated_at.replace(minute=0, second=0, microsecond=0)
            series_start = series_end - timedelta(hours=hours)
            cutoff = series_start
            try:
                # Private ledger bounds are strict, so use the smallest
                # representable successor and then enforce inclusive-now below.
                query_before = generated_at + timedelta(microseconds=1)
            except OverflowError:
                # Fail closed at datetime.max rather than dropping the bound.
                query_before = generated_at
        else:
            cutoff, query_before, window_meta, window_error = _window_arguments(generated_at)
            if window_error is not None:
                return window_error
        history_text = request.args.get("history")
        if history_text not in (None, "0", "1"):
            return jsonify({"error": "history must be 0 or 1"}), 400
        if hours is not None and history_text == "1":
            return jsonify(
                {"error": "history=1 is not supported with hours chart mode"}
            ), 400
        include_history = history_text != "0" if hours is None else False
        typed_filters = {
            field: requested
            for field in ("provider", "harness", "quota_name")
            if (requested := request.args.get(field)) is not None
        }
        try:
            rows = _private_facts(
                resolved_quota_ledger,
                fact_type="quota_observation_v1",
                filters=typed_filters,
                cutoff=cutoff,
                before=query_before,
                order="desc",
            )
        except UnsupportedLedgerSuffix:
            return jsonify({"error": "unsupported ledger suffix"}), 400
        except PrivateLedgerQueryTooLarge:
            return jsonify({"error": "ledger query exceeds private API row limit"}), 413
        except (MalformedLedgerError, ValidationError, IdentityConflictError):
            return jsonify({"error": "configured private ledger is unavailable"}), 503
        if hours is not None:
            # Backends use a strict successor only to admit the exact request
            # instant; this exact comparison prevents any future fact from
            # reaching latest/history/chart projections.
            generated_at_key = _datetime_rfc3339_order_key(generated_at)
            if generated_at_key is None:
                rows = []
            else:
                rows = [
                    row
                    for row in rows
                    if (
                        (observed_at_key := _rfc3339_order_key(row.get("observed_at")))
                        is not None
                        and observed_at_key <= generated_at_key
                    )
                ]
        rows.sort(key=_quota_observation_order_key, reverse=True)
        latest: list[dict[str, Any]] = []
        seen: set[_QuotaSeriesIdentity] = set()
        for row in rows:
            identity = _quota_series_identity(row)
            if identity in seen:
                continue
            seen.add(identity)
            latest.append(row)

        # Account ordinals for the reset rail. account_ref itself stays private
        # (it is deliberately absent from QUOTA_RESPONSE_FIELDS), but two
        # Anthropic subscriptions have to read as two chips, not one merged
        # number. `latest` holds exactly one row per series identity and that
        # identity contains the account, so the account set here is identical to
        # the one the quota charts index, and the shared sort key makes the
        # ordinals identical too.
        latest_account_indexes: dict[str, dict[_QuotaAccountIdentity, int]] = {}
        for row in latest:
            provider = _canonical_quota_provider(row.get("provider"))
            latest_account_indexes.setdefault(provider, {})[
                _quota_account_identity(row)
            ] = 0
        for provider, accounts in latest_account_indexes.items():
            for index, account in enumerate(
                sorted(accounts, key=_quota_account_sort_key), start=1
            ):
                accounts[account] = index

        def project(row: dict[str, Any]) -> dict[str, Any]:
            projected = {field: row.get(field) for field in QUOTA_RESPONSE_FIELDS}
            canonical = _canonical_quota_provider(row.get("provider"))
            projected["provider_canonical"] = canonical
            accounts = latest_account_indexes.get(canonical)
            account_index = (
                accounts.get(_quota_account_identity(row)) if accounts else None
            )
            projected["provider_account_index"] = account_index
            projected["provider_account_count"] = len(accounts) if accounts else 0
            provider_state = row.get("x_provider_state")
            projected["provider_state"] = (
                provider_state
                if isinstance(provider_state, str) and provider_state in _QUOTA_PROVIDER_STATES
                else None
            )
            coverage = row.get("x_coverage")
            coverage_status = coverage.get("status") if isinstance(coverage, dict) else None
            projected["coverage_status"] = (
                coverage_status
                if isinstance(coverage_status, str)
                and coverage_status in _QUOTA_COVERAGE_STATUSES
                else None
            )
            return projected

        payload: dict[str, Any] = {
            "freshness": _ledger_freshness(
                resolved_quota_ledger,
                now=generated_at,
                heartbeat_name="quota-collect.ok",
                lane_health_name="lanes-quota.json",
            ),
            # A latest observation with neither used nor remaining NOR an
            # explicit provider_state is a historical null (e.g. the
            # upstream-retired codex session window, whose old all-None rows
            # stay newest forever because collection stopped emitting that
            # window) — it rendered as a permanent "Unavailable" card. Empty
            # observation = no card. A null row that CARRIES provider_state
            # ("inactive_or_not_reported") is an honest signal and stays.
            "latest": [
                project(row)
                for row in latest
                if row.get("used_value") is not None
                or row.get("remaining_value") is not None
                or row.get("x_provider_state") is not None
                or row.get("x_coverage") is not None
            ],
            "observations": [project(row) for row in rows] if include_history else [],
        }
        if window_meta:
            payload["window"] = window_meta
        if series_start is not None and series_end is not None:
            payload["time_series"] = _build_subscription_time_series(
                rows,
                window_start=series_start,
                window_end=series_end,
                generated_at=generated_at,
            )
        return jsonify(payload)

    @app.route("/api/billing")
    def api_billing():
        if not resolved_billing_ledger:
            return jsonify({"error": "billing ledger is not configured"}), 404
        cutoff, query_before, window_meta, window_error = _window_arguments(
            datetime.now(timezone.utc)
        )
        if window_error is not None:
            return window_error
        typed_filters = {
            field: requested
            for field in ("provider", "transaction_kind", "status")
            if (requested := request.args.get(field)) is not None
        }
        try:
            rows = _private_facts(
                resolved_billing_ledger,
                fact_type="billing_fact_v1",
                filters=typed_filters,
                cutoff=cutoff,
                before=query_before,
                order="desc",
            )
        except UnsupportedLedgerSuffix:
            return jsonify({"error": "unsupported ledger suffix"}), 400
        except PrivateLedgerQueryTooLarge:
            return jsonify({"error": "ledger query exceeds private API row limit"}), 413
        except (MalformedLedgerError, ValidationError, IdentityConflictError):
            return jsonify({"error": "configured private ledger is unavailable"}), 503
        # Currency is canonical payload data but is intentionally not a SQLite
        # typed query column in schema v1, so preserve exact filtering here.
        requested_currency = request.args.get("currency")
        if requested_currency is not None:
            rows = [row for row in rows if row.get("currency") == requested_currency]

        currency_totals: dict[str, Decimal] = {}
        kind_totals: dict[tuple[str, str], Decimal] = {}
        for row in rows:
            currency = str(row["currency"])
            kind = str(row["transaction_kind"])
            amount = Decimal(str(row["amount"]))
            currency_totals[currency] = currency_totals.get(currency, Decimal("0")) + amount
            key = (currency, kind)
            kind_totals[key] = kind_totals.get(key, Decimal("0")) + amount
        return jsonify(
            {
                "totals_by_currency": [
                    {"currency": currency, "amount": _decimal_text(amount)}
                    for currency, amount in sorted(currency_totals.items())
                ],
                "totals_by_currency_and_transaction_kind": [
                    {"currency": currency, "transaction_kind": kind, "amount": _decimal_text(amount)}
                    for (currency, kind), amount in sorted(kind_totals.items())
                ],
                "transactions": [
                    {field: row.get(field) for field in BILLING_RESPONSE_FIELDS}
                    for row in rows
                ],
                "window": window_meta,
            }
        )

    @app.route("/api/routing-decisions")
    def api_routing_decisions():
        path = model_routing_ledger_path(atrium_root, ROUTING_DECISION_LEDGER)
        return jsonify({"rows": newest_first(read_jsonl(path), timestamp_key="decided_at")})

    @app.route("/api/task-outcomes")
    def api_task_outcomes():
        path = model_routing_ledger_path(atrium_root, TASK_OUTCOME_LEDGER)
        return jsonify({"rows": newest_first(read_jsonl(path), timestamp_key="completed_at")})

    @app.route("/api/backtests/latest")
    def api_backtests_latest():
        backtests_dir = Path(model_routing_ledger_path(atrium_root, "12_runtime/ledgers/model_routing/backtests"))
        if not backtests_dir.exists():
            return jsonify({"latest": None, "rows": []})
        files = sorted(backtests_dir.glob("*.json"), key=lambda item: item.stat().st_mtime, reverse=True)
        if not files:
            return jsonify({"latest": None, "rows": []})
        try:
            payload = json.loads(files[0].read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            payload = None
        return jsonify({"latest": payload, "path": str(files[0])})

    @app.after_request
    def _cache_policy(response):
        """The page and APIs must never be browser-cached: the operator was
        served a stale page for hours because Flask emits no cache headers at
        all and browsers cache heuristically (keeper report 2026-08-30, "is
        this live? I don't see it"). Vendored static assets may cache briefly.
        """
        if request.path.startswith("/static/"):
            response.headers.setdefault("Cache-Control", "max-age=3600")
        else:
            response.headers["Cache-Control"] = "no-store"
        return response

    return app


def run_dashboard(
    atrium_root: str,
    ledger: Optional[str],
    host: str,
    port: int,
    unified_usage_ledger: Optional[str] = None,
    quota_ledger: Optional[str] = None,
    billing_ledger: Optional[str] = None,
) -> None:
    app = create_app(
        atrium_root=atrium_root,
        ledger=ledger,
        unified_usage_ledger=unified_usage_ledger,
        quota_ledger=quota_ledger,
        billing_ledger=billing_ledger,
    )
    app.run(host=host, port=port)

