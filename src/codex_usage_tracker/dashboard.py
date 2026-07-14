"""Flask dashboard for Codex usage ledger inspection."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
import json
import os
from typing import Any, Dict, List, Optional

from flask import Flask, jsonify, render_template, request

from .capability_matrix import get_capability_matrix
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
_MAX_UNIFIED_SERIES_HOURS = 168
_UNIFIED_MODEL_SERIES_LIMIT = 5
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
        return datetime.fromisoformat(value).timestamp()
    except (TypeError, ValueError):
        return 0.0


def _fact_datetime(raw_timestamp: Any) -> datetime:
    if not isinstance(raw_timestamp, str):
        return datetime.min.replace(tzinfo=timezone.utc)
    try:
        parsed = datetime.fromisoformat(raw_timestamp.replace("Z", "+00:00"))
    except ValueError:
        return datetime.min.replace(tzinfo=timezone.utc)
    if parsed.tzinfo is None:
        return datetime.min.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


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


def _days_argument() -> tuple[datetime | None, tuple[Any, int] | None]:
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
    try:
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    except OverflowError:
        return None, (jsonify({"error": "days is outside the supported range"}), 400)
    return cutoff, None


def _hours_argument() -> tuple[int | None, tuple[Any, int] | None]:
    hours_text = request.args.get("hours")
    if hours_text is None:
        return None, None
    if request.args.get("days") is not None:
        return None, (jsonify({"error": "days and hours are mutually exclusive"}), 400)
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


def _iso_z(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _accounted_tokens(row: dict[str, Any]) -> int:
    return sum(int(row.get(field) or 0) for field in _ADDITIVE_TOKEN_FIELDS)


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
    comparison = {
        "codex": [0] * bucket_count,
        "claude_code": [0] * bucket_count,
    }
    excluded_tokens = 0

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

        is_codex = provider == "openai-codex"
        is_claude_code = row.get("harness") == "claude_code"
        if is_codex:
            comparison["codex"][bucket_index] += total
        if is_claude_code:
            comparison["claude_code"][bucket_index] += total
        if not is_codex and not is_claude_code:
            excluded_tokens += total

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

    comparison_series = [
        {
            "key": "codex",
            "label": "OpenAI Codex subscription",
            "harness": None,
            "provider": "openai-codex",
            "total_tokens": sum(comparison["codex"]),
            "values": comparison["codex"],
        },
        {
            "key": "claude_code",
            "label": "Claude Code",
            "harness": "claude_code",
            "provider": None,
            "total_tokens": sum(comparison["claude_code"]),
            "values": comparison["claude_code"],
        },
    ]
    return {
        "generated_at": _iso_z(generated_at),
        "bucket_minutes": 60,
        "bucket_count": bucket_count,
        "window_start": _iso_z(window_start),
        "window_end": _iso_z(window_end),
        "bucket_starts": [_iso_z(value) for value in bucket_starts],
        "model_series": model_series,
        "other_model_series": other_model_series,
        "comparison_series": comparison_series,
        "comparison_excluded_tokens": excluded_tokens,
    }


def _load_rows(ledger_path: str) -> List[Dict[str, Any]]:
    path = Path(ledger_path).expanduser()
    if not path.exists():
        return []

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
        rows = _load_ledger_rows()
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
        if hours is not None:
            series_end = generated_at.replace(minute=0, second=0, microsecond=0)
            series_start = series_end - timedelta(hours=hours)
            cutoff = series_start
            days_error = None
        else:
            cutoff, days_error = _days_argument()
        if days_error is not None:
            return days_error
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
                before=series_end,
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
        grouped: Dict[tuple[str, str], Dict[str, Any]] = {}
        by_harness: Dict[str, Dict[str, Any]] = {}
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
                    **{field: 0 for field in token_fields},
                    **{field: "0" for field in cost_fields},
                },
            )
            bucket["events"] += 1
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
        for bucket in grouped.values():
            bucket["total_tokens"] = sum(bucket[field] for field in token_fields if field != "reasoning_tokens")
        for bucket in by_harness.values():
            bucket["total_tokens"] = sum(bucket[field] for field in token_fields if field != "reasoning_tokens")
        coverage = {
            "exact_events": sum(row.get("measurement_confidence") == "exact" for row in rows),
            "reconstructed_events": sum(row.get("measurement_confidence") == "reconstructed" for row in rows),
            "reconstructed_calls": sum(int(row.get("reconstructed_call_count") or 0) for row in rows),
        }
        window_payload: dict[str, Any] = {
            "first_occurred_at": rows[0].get("occurred_at") if rows else None,
            "last_occurred_at": rows[-1].get("occurred_at") if rows else None,
            "event_count": len(rows),
            "days": int(request.args["days"]) if "days" in request.args else None,
        }
        if hours is not None:
            window_payload["hours"] = hours
        payload: dict[str, Any] = {
            "totals": totals,
            "coverage": coverage,
            "window": window_payload,
            "by_harness": sorted(by_harness.values(), key=lambda item: item["harness"]),
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

    @app.route("/api/subscriptions")
    def api_subscriptions():
        if not resolved_quota_ledger:
            return jsonify({"error": "quota ledger is not configured"}), 404
        cutoff, days_error = _days_argument()
        if days_error is not None:
            return days_error
        history_text = request.args.get("history")
        if history_text not in (None, "0", "1"):
            return jsonify({"error": "history must be 0 or 1"}), 400
        include_history = history_text != "0"
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
                order="desc",
            )
        except UnsupportedLedgerSuffix:
            return jsonify({"error": "unsupported ledger suffix"}), 400
        except PrivateLedgerQueryTooLarge:
            return jsonify({"error": "ledger query exceeds private API row limit"}), 413
        except (MalformedLedgerError, ValidationError, IdentityConflictError):
            return jsonify({"error": "configured private ledger is unavailable"}), 503
        latest: list[dict[str, Any]] = []
        seen: set[tuple[str, str, str, str]] = set()
        for row in rows:
            identity = (
                str(row.get("provider") or "unknown"),
                str(row.get("harness") or "unknown"),
                str(row.get("quota_name") or "unknown"),
                str(row.get("account_ref") or "default"),
            )
            if identity in seen:
                continue
            seen.add(identity)
            latest.append(row)
        def project(row: dict[str, Any]) -> dict[str, Any]:
            return {field: row.get(field) for field in QUOTA_RESPONSE_FIELDS}

        return jsonify(
            {
                "latest": [project(row) for row in latest],
                "observations": [project(row) for row in rows] if include_history else [],
            }
        )

    @app.route("/api/billing")
    def api_billing():
        if not resolved_billing_ledger:
            return jsonify({"error": "billing ledger is not configured"}), 404
        cutoff, days_error = _days_argument()
        if days_error is not None:
            return days_error
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

