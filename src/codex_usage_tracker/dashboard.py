"""Flask dashboard for Codex usage ledger inspection."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import functools
from pathlib import Path
import json
import os
import stat
import threading
from typing import Any, Dict, List, Optional, TypeAlias

from flask import Flask, jsonify, request

from .capability_matrix import get_capability_matrix
from .list_pricing import (
    LIST_PRICE_TOKEN_FIELDS,
    accumulate_price_bucket,
    estimate_list_price,
    finalize_price_buckets,
)
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
from .rulings import (
    DEFAULT_WEEKS as DEFAULT_RULING_WEEKS,
    MAX_WEEKS as MAX_RULING_WEEKS,
    RULING_SOURCES,
    RulingScan,
    RulingsSourceUnavailable,
    UsageWeekTooLarge,
    build_rulings_per_dollar,
    build_weekly_rulings,
    load_rulings,
    recent_week_start_dates,
    usage_window,
    week_price_buckets_from_ledger,
)
from .search_events import build_search_summary
from .token_correlation import build_token_correlation_rows, resolve_state_db_path
from .canonical_ledger import (
    _read_facts_without_side_effects,
    IdentityConflictError,
    MalformedLedgerError,
    ValidationError,
    canonical_timestamp,
    elect_physical_requests,
    query_sqlite_physical_requests,
    query_sqlite_facts,
    read_facts,
)
from . import prebuilt_views, usage_cache


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
# Chart bucket width for every hours= time series (keeper-ruled 2026-09-02:
# "15 minute buckets are better across all of the charts"). hours= windows
# end on the most recent complete 15-minute UTC boundary and always span
# whole hours, so bucket_count is exactly hours * 4. The public projection
# module keeps its own hourly grid and does not read this constant.
_BUCKET_MINUTES = 15
_BUCKET_SECONDS = _BUCKET_MINUTES * 60
# /api/data feeds a 24-row table; returning the whole ledger cost ~4MB per refresh.
_DEFAULT_DATA_ROW_LIMIT = 200
_LEDGER_CACHE_MAX_ENTRIES = 8
_LEDGER_CACHE_LOCK = threading.Lock()
_LEDGER_CACHE: Dict[str, tuple[tuple[int, int], List[Dict[str, Any]]]] = {}
# The model chart used to truncate at five series in the API itself, so a new
# or low-volume model (gpt-6-astra, glm-5.3-flash) could not be seen at all —
# it was folded into "Other models" before the browser ever saw it. The API
# now carries every model up to a bounded cap and tells the frontend how many
# to draw by default; the Top-models / All-models control owns the rest. This
# is the PRIVATE dashboard: the cap is a payload bound, not a disclosure rule.
_UNIFIED_MODEL_SERIES_LIMIT = 24
_UNIFIED_MODEL_SERIES_DEFAULT_VISIBLE = 5
# The status strip is one row per provider; provider cardinality is small and
# bounded by the ledger, but the payload stays bounded on principle.
_UNIFIED_STATUS_PROVIDER_LIMIT = 12
_SUBSCRIPTION_CHART_PROVIDER_LIMIT = 32
_SUBSCRIPTION_CHART_SERIES_PER_PROVIDER_LIMIT = 16
_PRIVATE_LEDGER_MAX_AGE_SECONDS = 3 * 60 * 60
# Keeper, 2026-09-25 (Discord unified usage ledger thread): "staleness should
# only be reported if it is over 15 minutes, or one ledger tick". Both
# collectors run on 15-minute timers (fleet ai-usage-collect /
# ai-quota-collect). A collector-written ledger has missed a tick when its
# last write is over a tick old and no run has started since: a usage run
# takes 8-25 minutes, so an older write while a run is going is a tick in
# progress. Two minutes cover timer slack. A run going past an hour is hung.
# A lane is stale when it did not succeed in the latest completed run.
# Ledgers without a collector heartbeat keep the three-hour threshold, and so
# do the provider cards' per-observation badges (``threshold_seconds``):
# some providers write a reading only when their numbers change (the Codex
# reading was 42 minutes old with its lane healthy), so a tick there would
# mark a healthy provider stale.
_COLLECTOR_TICK_SECONDS = 15 * 60
_COLLECTOR_TICK_GRACE_SECONDS = 2 * 60
_COLLECTOR_RUN_LIMIT_SECONDS = 60 * 60
# Provider-reported window states that may cross the API boundary:
# Kimi's inactive marker, and the OpenCode Go usage API's ok / rate-limited.
_QUOTA_PROVIDER_STATES = frozenset({"inactive_or_not_reported", "ok", "rate_limited"})
_QUOTA_COVERAGE_STATUSES = frozenset({"partial"})
# Reader surface tags that are safe to publish (no path, URL or credential).
_QUOTA_SOURCE_SURFACES = frozenset(
    {
        "opencode_go_usage_api",
        "opencode_go_server_function",
        "claude_oauth_usage_api",
        "claude_code_cli_usage",
        "z_ai_usage_quota",
    }
)
# Percent resolutions a reader may declare; 1 = whole points (OpenCode Go).
_QUOTA_PERCENT_RESOLUTIONS = frozenset({1})
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
    # The two Meridian lanes relay to the two Claude accounts (keeper,
    # 2026-09-25, msg 1552986099343818813); see _usage_provider_identity.
    "custom:meridian-primary": "anthropic",
    "custom:meridian-yugen": "anthropic",
    "custom:omlx": "local-acubens",
    "custom:gemma-4-moe-it": "local-acubens",
    "custom:minicpm5": "local-acubens",
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
    return _rfc3339_order_key_text(raw_timestamp)


# Pure on its string input and returns immutables, so memoized: the quota
# panels call it several times per row, and one collector run stamps many
# rows with the same instant. Profiled 2026-09-25: 169k calls were 3.8 s of
# a 7.8 s weekly-utilization request.
@functools.lru_cache(maxsize=131_072)
def _rfc3339_order_key_text(raw_timestamp: str) -> tuple[datetime, Decimal] | None:
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
    never = datetime.min.replace(tzinfo=timezone.utc)
    latest_run = (
        _fact_datetime(loaded["updated_at"]) if isinstance(loaded.get("updated_at"), str) else never
    )
    projected: list[dict[str, Any]] = []
    for name in sorted(str(key) for key in lanes):
        lane = lanes.get(name)
        if not isinstance(lane, dict):
            continue
        last_success_at = lane.get("last_success_at")
        parsed = (
            _fact_datetime(last_success_at) if isinstance(last_success_at, str) else None
        )
        if parsed is None or parsed == never:
            status = "never"
            age_seconds = None
        else:
            age_seconds = int((now - parsed).total_seconds())
            if latest_run != never:
                # One ledger tick: stale when it missed the latest completed run.
                status = "stale" if parsed < latest_run else "fresh"
            else:
                status = (
                    "stale"
                    if age_seconds > _COLLECTOR_TICK_SECONDS + _COLLECTOR_TICK_GRACE_SECONDS
                    else "fresh"
                )
        if isinstance(lane.get("retired"), str):
            # Deliberately no longer run (collector RETIRED_LANES): listed with
            # its reason, never stale.
            status = "retired"
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
                "retired": lane.get("retired") if isinstance(lane.get("retired"), str) else None,
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
    # A lane that has never succeeded is not "stale": nothing went quiet, it
    # simply has no history yet (typically a lane registered before its
    # credentials exist). It stays listed, under its own key, and never
    # flips the surface — only a lane that HAS succeeded and then went quiet
    # beyond the threshold does that.
    stale_lanes = [lane["lane"] for lane in lanes if lane["status"] == "stale"]
    never_lanes = [lane["lane"] for lane in lanes if lane["status"] == "never"]
    retired_lanes = [lane["lane"] for lane in lanes if lane["status"] == "retired"]
    collector_ticks = heartbeat.exists()
    max_age_seconds = (
        _COLLECTOR_TICK_SECONDS + _COLLECTOR_TICK_GRACE_SECONDS
        if collector_ticks
        else _PRIVATE_LEDGER_MAX_AGE_SECONDS
    )
    try:
        latest = max(
            datetime.fromtimestamp(candidate.stat().st_mtime, timezone.utc).replace(microsecond=0)
            for candidate in candidates
        )
    except (OSError, ValueError, OverflowError):
        payload: dict[str, Any] = {
            "status": "empty",
            "heartbeat_status": "empty",
            "latest_at": None,
            "max_age_seconds": max_age_seconds,
        }
        if collector_ticks:
            payload["threshold_seconds"] = _PRIVATE_LEDGER_MAX_AGE_SECONDS
        if lanes:
            payload["lanes"] = lanes
            payload["stale_lanes"] = stale_lanes
            payload["never_lanes"] = never_lanes
            payload["retired_lanes"] = retired_lanes
        return payload
    age_seconds = (now - latest).total_seconds()
    run_started: datetime | None = None
    if collector_ticks:
        started_marker = health_dir / f"{heartbeat_name.removesuffix('.ok')}.started"
        try:
            started = datetime.fromtimestamp(started_marker.stat().st_mtime, timezone.utc)
            if started > datetime.fromtimestamp(heartbeat.stat().st_mtime, timezone.utc):
                run_started = started
        except (OSError, ValueError, OverflowError):
            run_started = None
        if run_started is not None:
            # A tick is in progress; only a hung run is stale.
            status = "stale" if (now - run_started).total_seconds() > _COLLECTOR_RUN_LIMIT_SECONDS else "fresh"
        else:
            status = "stale" if age_seconds > max_age_seconds else "fresh"
    else:
        status = "stale" if age_seconds > max_age_seconds else "fresh"
    payload = {
        # Writes can land on schedule while one lane has been silently dead for
        # days, so a stale lane makes the whole surface stale.
        "status": "stale" if (status == "stale" or stale_lanes) else "fresh",
        # The write-level state on its own. `status` folds lane staleness in
        # so the badge and notices stay honest about a quiet source; the
        # charts gate on this field instead (2026-09-10: one ceded Z.AI lane
        # hid every provider's utilization chart for twelve hours while the
        # other eight lanes were writing on schedule).
        "heartbeat_status": status,
        "latest_at": latest.isoformat().replace("+00:00", "Z"),
        # The write-level threshold: one collector tick where a collector
        # heartbeat exists, else three hours.
        "max_age_seconds": max_age_seconds,
    }
    if collector_ticks:
        # The provider cards' per-observation threshold stays three hours
        # (see _COLLECTOR_TICK_SECONDS); the frontend reads it first.
        payload["threshold_seconds"] = _PRIVATE_LEDGER_MAX_AGE_SECONDS
        payload["run_in_progress_since"] = (
            run_started.isoformat().replace("+00:00", "Z") if run_started is not None else None
        )
    if lanes:
        payload["lanes"] = lanes
        payload["stale_lanes"] = stale_lanes
        payload["never_lanes"] = never_lanes
        payload["retired_lanes"] = retired_lanes
    return payload


def _private_facts(
    ledger_path: str,
    *,
    fact_type: str,
    filters: dict[str, str],
    cutoff: datetime | None = None,
    before: datetime | None = None,
    order: str = "asc",
    readonly_jsonl: bool = False,
    identity_stats: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Dispatch canonical private reads by explicit suffix and preserve JSONL.

    ``usage_event_v1`` reads count physical requests (contract v2, keeper
    ratified 2026-09-25): one elected observation per request, elected over
    the whole time window before typed filters. Pass ``identity_stats`` (a
    dict) to receive the window's election stats.
    """
    path = Path(ledger_path).expanduser()
    suffix = path.suffix.casefold()
    physical = fact_type == "usage_event_v1"
    if suffix == ".jsonl":
        if path.exists() and path.stat().st_size > _PRIVATE_API_MAX_JSONL_BYTES:
            raise PrivateLedgerQueryTooLarge("JSONL ledger exceeds private API byte limit")
        reader = _read_facts_without_side_effects if readonly_jsonl else read_facts
        rows = [row for row in reader(path) if row.get("fact_type") == fact_type]
        if not physical:
            for field, expected in filters.items():
                rows = [row for row in rows if row.get(field) == expected]
        timestamp_field = (
            "observed_at"
            if fact_type in {"quota_observation_v1", "search_event_v1"}
            else "occurred_at"
        )
        if cutoff is not None:
            rows = [row for row in rows if _fact_datetime(row.get(timestamp_field)) >= cutoff]
        if before is not None:
            rows = [row for row in rows if _fact_datetime(row.get(timestamp_field)) < before]
        if physical:
            rows, stats = elect_physical_requests(rows)
            if identity_stats is not None:
                identity_stats.update(stats)
            for field, expected in filters.items():
                rows = [row for row in rows if row.get(field) == expected]
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
        if physical:
            # Keeper, 2026-09-25 (msg 1552951677286355045): the page should pull
            # the last week of rows from Redis, not disk. usage_cache returns
            # None whenever it cannot answer this window exactly.
            cached = usage_cache.physical_requests(
                path, cutoff=cutoff, before=before, filters=filters, order=order
            )
            if cached is not None:
                rows, stats = cached
                if identity_stats is not None:
                    identity_stats.update(stats)
                if len(rows) > _PRIVATE_API_MAX_ROWS:
                    raise PrivateLedgerQueryTooLarge("ledger query exceeds private API row limit")
                return rows
            rows, stats = query_sqlite_physical_requests(
                path,
                filters=filters,
                occurred_or_observed_at_gte=lower_bound,
                occurred_or_observed_at_lt=upper_bound,
                order=order,
                limit=_PRIVATE_API_MAX_ROWS + 1,
                contract_validation=False,
            )
            if identity_stats is not None:
                identity_stats.update(stats)
            if len(rows) > _PRIVATE_API_MAX_ROWS:
                raise PrivateLedgerQueryTooLarge("ledger query exceeds private API row limit")
            return rows
        if fact_type == "quota_observation_v1":
            # Keeper, 2026-09-25 (msg 1552965574315868321): the quota panels
            # read Redis too. None means the copy cannot answer this window.
            cached_quota = usage_cache.quota_facts(
                path, cutoff=cutoff, before=before, filters=filters, order=order
            )
            if cached_quota is not None:
                if len(cached_quota) > _PRIVATE_API_MAX_ROWS:
                    raise PrivateLedgerQueryTooLarge("ledger query exceeds private API row limit")
                return cached_quota
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
            # Integrity check only (hash, identity, typed columns): facts were
            # contract-normalized before append and are immutable under the
            # ledger trigger, so re-running the full contract and secret
            # scanner per row is audit work, not read work. Measured
            # 2026-09-10 on the live quota ledger: a 168-hour subscriptions
            # read spent 46 of 50 profiled seconds in that re-validation and
            # sat at the frontend's 30-second abort, so the reset rail and
            # provider cards flapped between "Loading" and "unavailable".
            contract_validation=False,
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
    if days < 1:
        # days=0 used to be accepted and produced an empty window whose
        # ``next_before`` cursor equalled ``before`` — a page that never advances.
        return None, (jsonify({"error": "days must be at least 1"}), 400)
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


# Ruling-ledger reads are a filesystem walk of the whole Atrium (a couple of
# seconds over ~17k markdown files), so the scan is memoized briefly rather
# than repeated for every panel refresh. The TTL is short enough that a
# just-recorded ruling shows up within a minute.
_RULINGS_SCAN_TTL_SECONDS = 60.0
_rulings_scan_cache: dict[tuple[str, str], tuple[float, RulingScan]] = {}
_rulings_scan_lock = threading.Lock()


def _weeks_argument() -> tuple[int, tuple[Any, int] | None]:
    """Parse ``?weeks=`` for the ruling series; default eight, bounded."""
    weeks_text = request.args.get("weeks")
    if weeks_text is None:
        return DEFAULT_RULING_WEEKS, None
    try:
        weeks = int(weeks_text)
    except ValueError:
        return DEFAULT_RULING_WEEKS, (jsonify({"error": "weeks must be an integer"}), 400)
    if not 1 <= weeks <= MAX_RULING_WEEKS:
        return DEFAULT_RULING_WEEKS, (
            jsonify({"error": f"weeks must be between 1 and {MAX_RULING_WEEKS}"}),
            400,
        )
    return weeks, None


def _rulings_source_argument() -> tuple[str, tuple[Any, int] | None]:
    """Parse ``?source=canon|service``; the canon scan is the default because
    the Atrium Service rulings endpoint is not deployed yet."""
    source = request.args.get("source")
    if source is None:
        return "canon", None
    if source not in RULING_SOURCES:
        return "canon", (
            jsonify({"error": f"source must be one of: {', '.join(RULING_SOURCES)}"}),
            400,
        )
    return source, None


def _cached_rulings_scan(
    *, atrium_root: str, source: str, now: float | None = None
) -> RulingScan:
    return _rulings_scan_with_path(atrium_root=atrium_root, source=source, now=now)[0]


def _rulings_scan_with_path(
    *, atrium_root: str, source: str, now: float | None = None
) -> tuple[RulingScan, dict[str, Any]]:
    """The canon scan from Redis when the rulings unit has stored a fresh one
    (keeper, 2026-09-25, msg 1552965574315868321), else the walk, memoized
    for a minute. Returns the scan and where it came from."""
    import time as _time

    if source == "canon":
        cached_scan = usage_cache.rulings_scan(atrium_root)
        if cached_scan is not None:
            return cached_scan
    moment = _time.time() if now is None else now
    key = (source, atrium_root)
    with _rulings_scan_lock:
        cached = _rulings_scan_cache.get(key)
        if cached is not None and moment - cached[0] < _RULINGS_SCAN_TTL_SECONDS:
            return cached[1], {"read_path": source, "scan_age_seconds": round(moment - cached[0])}
    scan = load_rulings(atrium_root=atrium_root, source=source)
    with _rulings_scan_lock:
        _rulings_scan_cache[key] = (moment, scan)
    return scan, {"read_path": source, "scan_age_seconds": 0}


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
    # A Mixture-of-Agents request names a preset ("council"), not a model, and
    # its answer always comes from one of the preset's member models. Luis,
    # 2026-09-25 (unified usage ledger thread msg 1553037883747598450, approving
    # the UI review item that found all 143 flagged calls that week were
    # council runs): the watch counts real swaps only.
    if str(row.get("provider") or "").strip().lower() == "moa":
        return False
    return reported != row.get("model_requested")


def _floor_to_bucket(moment: datetime) -> datetime:
    """Floor ``moment`` to the most recent complete ``_BUCKET_MINUTES`` boundary.

    ``hours=`` series end here rather than on the hour, so a request at 12:34
    charts through 12:30 instead of discarding up to 59 minutes of traffic.
    """
    return moment.replace(
        minute=(moment.minute // _BUCKET_MINUTES) * _BUCKET_MINUTES,
        second=0,
        microsecond=0,
    )


def _bucket_index(moment: datetime, window_start: datetime) -> int:
    """Index of the ``_BUCKET_MINUTES`` bucket holding ``moment``; negative before the window."""
    return int((moment - window_start).total_seconds() // _BUCKET_SECONDS)


def _bucket_grid(window_start: datetime, window_end: datetime) -> tuple[int, list[datetime]]:
    """The shared ``(bucket_count, bucket_starts)`` grid every hours= series draws on."""
    bucket_count = int((window_end - window_start).total_seconds() // _BUCKET_SECONDS)
    bucket_starts = [
        window_start + timedelta(minutes=_BUCKET_MINUTES * index)
        for index in range(bucket_count)
    ]
    return bucket_count, bucket_starts


def _build_provider_status_series(
    rows: list[dict[str, Any]],
    *,
    window_start: datetime,
    bucket_count: int,
) -> list[dict[str, Any]]:
    """Per provider per complete UTC bucket (``_BUCKET_MINUTES``): traffic,
    failures, dominant class.

    ``request_status`` and ``error_class`` ride every canonical usage event, so
    an uptime read needs no new collection — only this projection. Only the
    explicit ``error`` status counts as a failure: ``unknown`` is a source that
    does not report status, and ``cancelled`` is the operator stopping a call,
    neither of which is the provider failing.

    Each entry also carries per-bucket ``input_tokens`` and
    ``cache_read_tokens`` sums — the two prompt-side buckets the estate-wide
    cache-hit-rate chart divides (cache read over input plus cache read), so
    the frontend needs no second projection to draw it.
    """
    events: dict[str, list[int]] = {}
    errors: dict[str, list[int]] = {}
    classes: dict[str, list[dict[str, int]]] = {}
    input_tokens: dict[str, list[int]] = {}
    cache_read_tokens: dict[str, list[int]] = {}
    raw_ids: dict[str, set[str]] = {}

    for row in rows:
        occurred_at = _fact_datetime(row.get("occurred_at"))
        bucket_index = _bucket_index(occurred_at, window_start)
        if bucket_index < 0 or bucket_index >= bucket_count:
            continue
        # One row per canonical provider: "openai" and "openai-codex" are the
        # same status feed and the same quota row, so they must not read as
        # two providers on the strip.
        raw_provider = str(row.get("provider") or "unknown")
        provider = _usage_canonical_provider(row)
        if provider not in events:
            events[provider] = [0] * bucket_count
            errors[provider] = [0] * bucket_count
            classes[provider] = [{} for _index in range(bucket_count)]
            input_tokens[provider] = [0] * bucket_count
            cache_read_tokens[provider] = [0] * bucket_count
            raw_ids[provider] = set()
        raw_ids[provider].add(raw_provider)
        events[provider][bucket_index] += 1
        input_tokens[provider][bucket_index] += int(row.get("input_tokens") or 0)
        cache_read_tokens[provider][bucket_index] += int(row.get("cache_read_tokens") or 0)
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
                "provider_canonical": provider,
                "provider_raw_ids": sorted(raw_ids[provider]),
                "events": events[provider],
                "errors": errors[provider],
                "input_tokens": input_tokens[provider],
                "cache_read_tokens": cache_read_tokens[provider],
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
    bucket_count, bucket_starts = _bucket_grid(window_start, window_end)
    model_buckets: dict[tuple[str, str], list[int]] = {}
    provider_of_label: dict[str, str] = {}
    bucket_series = {field: [0] * bucket_count for field in _LIST_PRICE_TOKEN_FIELDS}

    for row in rows:
        occurred_at = _fact_datetime(row.get("occurred_at"))
        bucket_index = _bucket_index(occurred_at, window_start)
        if bucket_index < 0 or bucket_index >= bucket_count:
            continue
        total = _accounted_tokens(row)
        provider, _account, provider_label = _usage_provider_identity(row)
        model = str(row.get("model_reported") or row.get("model_requested") or "unknown")
        values = model_buckets.setdefault((provider_label, model), [0] * bucket_count)
        provider_of_label[provider_label] = provider
        values[bucket_index] += total
        for field in _LIST_PRICE_TOKEN_FIELDS:
            bucket_series[field][bucket_index] += int(row.get(field) or 0)

    ranked_models = sorted(
        model_buckets.items(),
        key=lambda item: (-sum(item[1]), item[0][0], item[0][1]),
    )
    named_models = ranked_models[:_UNIFIED_MODEL_SERIES_LIMIT]
    remaining_models = ranked_models[_UNIFIED_MODEL_SERIES_LIMIT:]
    model_series = [
        {
            "provider": provider_of_label[provider_label],
            "provider_label": provider_label,
            "model": model,
            "label": f"{model} · {provider_label}",
            "total_tokens": sum(values),
            "values": values,
        }
        for (provider_label, model), values in named_models
    ]
    other_model_series = None
    if remaining_models:
        other_values = [
            sum(values[index] for _key, values in remaining_models)
            for index in range(bucket_count)
        ]
        other_model_series = {
            # Say what this bar IS: a charting decision to bound the payload,
            # not a bucket of models the tracker failed to identify. Every
            # member is a known (provider, model) pair.
            "label": f"Other models (chart grouping, {len(remaining_models)})",
            "member_count": len(remaining_models),
            "total_tokens": sum(other_values),
            "values": other_values,
        }

    return {
        "generated_at": _iso_z(generated_at),
        "bucket_minutes": _BUCKET_MINUTES,
        "bucket_count": bucket_count,
        "window_start": _iso_z(window_start),
        "window_end": _iso_z(window_end),
        "bucket_starts": [_iso_z(value) for value in bucket_starts],
        "model_series": model_series,
        "other_model_series": other_model_series,
        # How many series the chart draws before the reader expands it. The
        # rest are present above and are one click away, never dropped.
        "model_series_default_visible": _UNIFIED_MODEL_SERIES_DEFAULT_VISIBLE,
        # Estate-wide token composition (keeper-ruled 2026-08-30, replacing
        # the codex-only Token bucket mix): the five token buckets summed
        # across every provider, on the same 15-minute UTC grid. Reasoning stays
        # its own series here — the chart shows composition, not the
        # accounted total, and the caption owns that distinction.
        "bucket_series": bucket_series,
        # The Codex-vs-Claude two-series comparison was retired with its card
        # (keeper ruling, 2026-08-30): the provider donuts carry share now, and
        # this projection existed only to feed that one chart.
        "provider_status_series": _build_provider_status_series(
            rows, window_start=window_start, bucket_count=bucket_count
        ),
    }


# Price bucketing lives in list_pricing (moved 2026-09-10): the dashboard's
# window and daily estimates and the weekly rulings-per-dollar join must
# fold rows the same way against the same rate table. These aliases keep the
# module-private call sites below unchanged.
_LIST_PRICE_TOKEN_FIELDS = LIST_PRICE_TOKEN_FIELDS
_accumulate_price_bucket = accumulate_price_bucket
_finalize_price_buckets = finalize_price_buckets


def _build_list_price_daily(
    rows: list[dict[str, Any]],
    *,
    window_start: datetime,
    window_end: datetime,
) -> list[dict[str, Any]]:
    """Per-UTC-calendar-day list-price estimates over the bounded hour window.

    Buckets are UTC calendar days — from the UTC date of ``window_start``
    through the UTC date of ``window_end`` — because the UI labels each entry
    as a date. (The earlier 24-hour slices anchored at the sliding window
    start were labelled as dates too, so every "day" was really a 16:00Z to
    16:00Z span that straddled two calendar days.) ``day_start`` is that day's
    00:00Z; an entry the window covers only partially (the first and last day
    of a window that does not start and end at midnight) carries
    ``partial: true``. Each day reuses :func:`estimate_list_price` over that
    day's (provider, model) token aggregates; a day with no traffic prices
    honestly at $0.00 with full coverage rather than being dropped, so the
    sparkline keeps calendar shape. Oldest day first.
    """
    first_day = window_start.astimezone(timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    last_instant = window_end.astimezone(timezone.utc)
    last_day = last_instant.replace(hour=0, minute=0, second=0, microsecond=0)
    if last_day == last_instant and last_day > first_day:
        # A window ending exactly at midnight covers nothing of that day.
        last_day -= timedelta(days=1)
    day_count = (last_day - first_day).days + 1
    daily_grouped: list[dict[tuple[str, str, str], dict[str, Any]]] = [
        {} for _ in range(day_count)
    ]
    for row in rows:
        occurred_at = _fact_datetime(row.get("occurred_at"))
        if occurred_at < window_start or occurred_at >= window_end:
            continue
        day_index = (occurred_at - first_day).days
        if day_index < 0 or day_index >= day_count:
            continue
        _accumulate_price_bucket(daily_grouped[day_index], row)
    entries: list[dict[str, Any]] = []
    for index in range(day_count):
        day_start = first_day + timedelta(days=index)
        day_end = day_start + timedelta(days=1)
        entries.append(
            {
                "day_start": _iso_z(day_start),
                "partial": window_start > day_start or window_end < day_end,
                **estimate_list_price(_finalize_price_buckets(daily_grouped[index])),
            }
        )
    return entries


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
    projected = {
        provider: {**by_url[url], "feed_url": url}
        for provider, url in _PROVIDER_STATUS_FEEDS.items()
    }
    # The status strip and the quota cards look feeds up by canonical provider
    # ("openai-codex", "kimi-k3-coding"); the raw keys stay so nothing that
    # still reads them breaks.
    for provider, url in _PROVIDER_STATUS_FEEDS.items():
        canonical = _canonical_quota_provider(provider)
        if canonical not in projected:
            projected[canonical] = {**by_url[url], "feed_url": url}
    return projected


def _canonical_quota_provider(value: Any) -> str:
    raw = "unknown" if value is None else str(value).strip()
    if not raw:
        raw = "unknown"
    return _QUOTA_PROVIDER_ALIASES.get(raw.casefold(), raw)


# Keeper, 2026-09-25 (unified usage ledger thread msgs 1552986099343818813
# and 1552992232733413420): "meridian primary and meridian yugen are really
# anthropic 1 and 2". Meridian is the route, the provider is Anthropic. Usage
# on either account reads as Anthropic, pooled where the page draws shares
# (provider donuts, status strip) and named "Claude 1" / "Claude 2" where it
# lists rows (provider/model table, model series): the ordinals the quota
# surfaces already use, primary account first.
_ANTHROPIC_ACCOUNT_BY_ROUTE = {
    "custom:meridian-primary": 1,
    "meridian-primary": 1,
    "custom:meridian-yugen": 2,
    "meridian-yugen": 2,
}
_ANTHROPIC_ACCOUNT_BY_NAMESPACE_SUFFIX = ((":claude-code-secondary", 2), (":claude-code", 1))
_ANTHROPIC_ACCOUNT_COUNT = 2


def _usage_canonical_provider(row: dict[str, Any]) -> str:
    """The provider a usage row's call reached: a relayed call's upstream
    (a bare ``custom`` Hermes row through Meridian carries
    ``upstream_provider``), else its own label, folded by the alias table."""
    upstream = row.get("upstream_provider")
    if upstream not in (None, ""):
        return _canonical_quota_provider(upstream)
    return _canonical_quota_provider(row.get("provider"))


def _usage_account_index(row: dict[str, Any]) -> int | None:
    """Which Claude account an Anthropic usage row belongs to, when recorded."""
    for route in (row.get("x_relay_route"), row.get("provider")):
        index = _ANTHROPIC_ACCOUNT_BY_ROUTE.get(str(route or ""))
        if index is not None:
            return index
    if row.get("harness") == "claude_code":
        namespace = str(row.get("source_namespace") or "")
        for suffix, index in _ANTHROPIC_ACCOUNT_BY_NAMESPACE_SUFFIX:
            if namespace.endswith(suffix):
                return index
    return None


def _usage_provider_identity(row: dict[str, Any]) -> tuple[str, int | None, str]:
    """``(provider, account index, label)`` for listing a usage row.

    Anthropic rows list under ``anthropic`` and their account ("Claude 1");
    one whose account the ledger did not record says so rather than guessing.
    Every other provider keeps its raw label, as before.
    """
    canonical = _usage_canonical_provider(row)
    if canonical != "anthropic":
        raw = str(row.get("provider") or "unknown")
        return raw, None, raw
    index = _usage_account_index(row)
    return "anthropic", index, (f"Claude {index}" if index is not None else "Claude (account not recorded)")


# Burn-panel cadence contract: the hourly ai-quota-collect timer plus the
# five-minute active-window tick (both Claude accounts, collector
# ``--when-active`` gate) produce observations at least this dense while a
# window is actually burning. 90 minutes so the ledger's own hourly
# cadence — the baseline sampling, with its real probe drift — always
# yields honest rise measurements; anything wider is a multi-window
# conflation, not a burn sample.
_BURN_MAX_SAMPLE_GAP_SECONDS = 90 * 60

# A window whose resets_at is further away than this is not a session window
# for burn purposes — projecting exhaustion against it would be noise.
_BURN_MAX_WINDOW_SECONDS = 7 * 24 * 3600

# Lookback margin ahead of the requested burn window so the first in-window
# observation still has a predecessor to measure a rise from.
_BURN_BASELINE_MARGIN_SECONDS = 90 * 60

# Account ordinals ("Claude 1", "Claude 2") are public identity and must read
# the same on every surface. The subscriptions endpoint numbers the accounts
# it sees in the dashboard's 168-hour window; the burn endpoint numbers
# against that same roster rather than against whichever accounts happened
# to burn in the last few hours, so a quiet primary account never lets the
# secondary one become "1".
_ACCOUNT_ROSTER_HOURS = _MAX_UNIFIED_SERIES_HOURS

# Window kinds that are balances rather than windows: nothing resets, so a
# burn forecast against them would be noise. They keep their data and carry
# an explicit not-applicable projection status instead.
_BURN_BALANCE_WINDOW_KINDS = frozenset({"lifetime", "unknown"})

_BURN_PROJECTION_STATUSES = (
    "exhausted",
    "projected_before_reset",
    "resets_first",
    "insufficient_observations",
    "not_applicable",
    "stale",
)


def _latest_rows_per_identity(rows_newest_first: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One row per private series identity — the newest — from rows already
    sorted newest first (``_quota_observation_order_key`` descending)."""
    latest: list[dict[str, Any]] = []
    seen: set[_QuotaSeriesIdentity] = set()
    for row in rows_newest_first:
        identity = _quota_series_identity(row)
        if identity in seen:
            continue
        seen.add(identity)
        latest.append(row)
    return latest


def _quota_account_ordinals(
    latest_rows: list[dict[str, Any]],
) -> dict[str, dict[_QuotaAccountIdentity, int]]:
    """Number each canonical provider's accounts 1..n by the shared account
    sort key. The account reference itself stays private; only the ordinal
    and the count cross the API, and every endpoint derives them here."""
    indexes: dict[str, dict[_QuotaAccountIdentity, int]] = {}
    for row in latest_rows:
        provider = _canonical_quota_provider(row.get("provider"))
        indexes.setdefault(provider, {})[_quota_account_identity(row)] = 0
    for accounts in indexes.values():
        for index, account in enumerate(sorted(accounts, key=_quota_account_sort_key), start=1):
            accounts[account] = index
    return indexes


def _burn_seconds_until(resets_at: Any, *, now: datetime) -> float | None:
    if not isinstance(resets_at, str):
        return None
    parsed = _rfc3339_order_key(resets_at)
    if parsed is None:
        return None
    seconds = (parsed[0] - now).total_seconds()
    return seconds if seconds > 0 else None


def _burn_reset_datetime(resets_at: Any) -> datetime | None:
    if not isinstance(resets_at, str):
        return None
    parsed = _rfc3339_order_key(resets_at)
    return None if parsed is None else parsed[0]


def _burn_projection_status(
    *,
    latest_pct: float | None,
    latest_observed: datetime,
    window_kind: str,
    pct_per_hour: float | None,
    resets_at: Any,
    seconds_to_reset: float | None,
    observation_count: int,
    now: datetime,
) -> tuple[str, str, float | None]:
    """Classify one window's forecast. Returns ``(status, reason,
    seconds_to_empty)``; ``seconds_to_empty`` is set only for
    ``projected_before_reset``.

    The order is the honesty order: a balance is never a window; a stale
    reading asserts nothing about now (not even "exhausted"); a cap that is
    at its cap is exhausted; without a positive measured rate there is no
    forecast; without a future reset there is nothing to forecast against;
    and only then does the arithmetic decide whether the window empties
    first or resets first. The arithmetic itself is the 2026-09-02
    estimator: rate at the observed active-use rate, no forecast beyond the
    window's own reset.
    """
    if window_kind in _BURN_BALANCE_WINDOW_KINDS:
        return "not_applicable", "balance_window", None
    if latest_pct is None:
        return "not_applicable", "no_utilization_ratio", None
    if (now - latest_observed).total_seconds() > _PRIVATE_LEDGER_MAX_AGE_SECONDS:
        return "stale", "observation_stale", None
    reset_at = _burn_reset_datetime(resets_at)
    if reset_at is not None and reset_at <= now and reset_at > latest_observed:
        return "stale", "reset_passed_since_observation", None
    if latest_pct >= 100:
        return "exhausted", "used_at_cap", None
    if pct_per_hour is None or pct_per_hour <= 0:
        reason = "single_observation" if observation_count < 2 else "no_positive_rate"
        return "insufficient_observations", reason, None
    if reset_at is None:
        return "not_applicable", "no_reset_reported", None
    if seconds_to_reset is None:
        return "not_applicable", "reset_not_in_future", None
    if seconds_to_reset > _BURN_MAX_WINDOW_SECONDS:
        return "not_applicable", "window_longer_than_seven_days", None
    seconds_to_empty = (100.0 - latest_pct) / pct_per_hour * 3600.0
    # Both legs are anchored at the latest observation: the ETA extrapolates
    # from it, so the reset must be measured from it too. ``seconds_to_reset``
    # (anchored at now) is the serialized countdown, not the comparison leg —
    # comparing across anchors called an ETA that precedes the reset
    # "resets_first" whenever the observation was older than the countdown.
    observed_seconds_to_reset = (reset_at - latest_observed).total_seconds()
    if seconds_to_empty <= observed_seconds_to_reset:
        return "projected_before_reset", "empty_before_reset_at_observed_rate", seconds_to_empty
    return "resets_first", "reset_before_empty_at_observed_rate", None


def _build_quota_burn_projection(
    rows: list[dict[str, Any]],
    *,
    generated_at: datetime,
    account_indexes: dict[str, dict[_QuotaAccountIdentity, int]] | None = None,
) -> dict[str, Any]:
    """Project quota observations into per-window burn series.

    One series per account window (the same identity rule the subscription
    charts use — no cross-account merging). Every observation carries its
    used percentage and observed timestamp; burn deltas are the positive
    monotonic differences between successive observations of the same
    window, so a reset reads as a gap rather than a negative burn. The
    exhaustion projection divides only by a strictly positive derived rate
    and never extends past the window's own ``resets_at``: if the honest
    ETA lands after the reset, the row says ``resets_first`` instead of
    inventing a lockout that will not happen.

    ``account_indexes`` is the authoritative roster the subscriptions
    endpoint numbers accounts against. When it is supplied, a series whose
    account is absent from it is historical-only (observed inside the burn
    read's baseline margin or default window but outside the dashboard
    roster) and is left out rather than inserted and re-sorted — inserting
    renumbered the accounts the subscriptions endpoint had already
    numbered. When it is ``None`` the ordinals are derived from the rows
    themselves by the same shared sort.
    """
    grouped: dict[_QuotaSeriesIdentity, list[dict[str, Any]]] = {}
    for row in rows:
        observed_at = _fact_datetime(row.get("observed_at"))
        if observed_at is None:
            continue
        if not _quota_observation_is_informative(row):
            continue
        if _is_retired_opencode_go_estimate(row):
            continue
        grouped.setdefault(_quota_series_identity(row), []).append(row)

    ordinals: dict[str, dict[_QuotaAccountIdentity, int]]
    if account_indexes is None:
        ordinals = {}
        for identity in grouped:
            ordinals.setdefault(identity[0], {})[identity[6]] = 0
        for provider_accounts in ordinals.values():
            for index, account in enumerate(
                sorted(provider_accounts, key=_quota_account_sort_key), start=1
            ):
                provider_accounts[account] = index
    else:
        ordinals = {provider: dict(accounts) for provider, accounts in account_indexes.items()}
        grouped = {
            identity: window_rows
            for identity, window_rows in grouped.items()
            if identity[6] in ordinals.get(identity[0], {})
        }

    series: list[dict[str, Any]] = []
    for identity in sorted(grouped, key=_quota_identity_sort_key):
        provider, quota_name, harness, quota_scope, window_kind, unit, account = identity
        window_rows = sorted(grouped[identity], key=_quota_observation_order_key)
        latest_row = window_rows[-1]
        rate_rows = window_rows
        if (
            provider == "opencode-go"
            and _quota_source_surface(latest_row) == "opencode_go_usage_api"
        ):
            for index in range(len(window_rows) - 1, -1, -1):
                if _quota_source_surface(window_rows[index]) != "opencode_go_usage_api":
                    rate_rows = window_rows[index + 1 :]
                    break
        observations = [
            {
                "observed_at": row.get("observed_at"),
                "used_pct": _quota_utilization_pct(row),
            }
            for row in window_rows
        ]
        deltas: list[dict[str, Any]] = []
        previous: tuple[float, datetime] | None = None
        for row in rate_rows:
            pct = _quota_utilization_pct(row)
            observed_at = _fact_datetime(row.get("observed_at"))
            if pct is None or previous is None:
                previous = (pct, observed_at) if pct is not None else None
                continue
            previous_pct, previous_at = previous
            gap_seconds = (observed_at - previous_at).total_seconds()
            if gap_seconds <= 0 or gap_seconds > _BURN_MAX_SAMPLE_GAP_SECONDS:
                # Either out-of-order duplicates or a sampling gap so wide
                # the difference is not a burn measurement.
                previous = (pct, observed_at)
                continue
            rise = pct - previous_pct
            if rise > 0:
                deltas.append(
                    {
                        "from_observed_at": _iso_z(previous_at),
                        "to_observed_at": row.get("observed_at"),
                        "rise_pct": round(rise, 4),
                        "gap_seconds": int(gap_seconds),
                        "pct_per_hour": round(rise / (gap_seconds / 3600.0), 4),
                    }
                )
            # A fall (reset) or a flat step advances the anchor without
            # producing a delta; only genuine rises measure burn.
            previous = (pct, observed_at)

        latest_pct = _quota_utilization_pct(latest_row)
        latest_observed = _fact_datetime(latest_row.get("observed_at"))
        rate_window = deltas[-3:]
        if rate_window:
            # "While it burns": rises over the gaps they were measured
            # across — idle spans between deltas do not dilute the rate,
            # because the panel's question is how fast the window moves
            # when it is moving.
            total_rise = sum(float(delta["rise_pct"]) for delta in rate_window)
            total_seconds = sum(float(delta["gap_seconds"]) for delta in rate_window)
            pct_per_hour = (total_rise / (total_seconds / 3600.0)) if total_seconds > 0 else None
        else:
            pct_per_hour = None
        resets_at = latest_row.get("resets_at")
        seconds_to_reset = _burn_seconds_until(resets_at, now=generated_at)
        status, reason, seconds_to_empty = _burn_projection_status(
            latest_pct=latest_pct,
            latest_observed=latest_observed,
            window_kind=window_kind,
            pct_per_hour=pct_per_hour,
            resets_at=resets_at,
            seconds_to_reset=seconds_to_reset,
            observation_count=len(rate_rows),
            now=generated_at,
        )
        exhaustion_at: str | None = None
        if seconds_to_empty is not None:
            exhaustion_at = _iso_z(latest_observed + timedelta(seconds=seconds_to_empty))
        account_index = ordinals[provider][account]
        entry: dict[str, Any] = {
            "provider": provider,
            "label": _quota_provider_label(provider),
            "quota_name": quota_name,
            "harness": harness,
            "quota_scope": quota_scope,
            "window_kind": window_kind,
            "unit": unit,
            # Public account identity: the same ordinal and count the
            # subscriptions endpoint serializes for this provider.
            "provider_account_index": account_index,
            "provider_account_count": len(ordinals[provider]),
            "measurement_confidence": latest_row.get("measurement_confidence"),
            "confidence_status": _quota_series_confidence(window_rows),
            **_quota_public_metadata(latest_row),
            "resets_at": resets_at,
            "seconds_to_reset": int(seconds_to_reset) if seconds_to_reset is not None else None,
            "latest_observed_at": latest_row.get("observed_at"),
            "latest_used_pct": latest_pct,
            "observation_count": len(observations),
            "burn_rate_pct_per_hour": round(pct_per_hour, 4) if pct_per_hour is not None else None,
            "rate_window_deltas": len(rate_window),
            # `exhausted` is the raw reading (at cap in the latest sample);
            # `projection_status` is the honest claim, which a stale reading
            # or a balance window can withhold.
            "exhausted": status != "stale" and latest_pct is not None and latest_pct >= 100,
            "projection_status": status,
            "projection_reason": reason,
            "projected_exhaustion_at": exhaustion_at,
            "deltas": deltas,
            "observations": observations,
        }
        series.append(entry)

    return {
        "generated_at": _iso_z(generated_at),
        "description": (
            "Per-account quota windows of every kind the ledger observes — "
            "five-hour and weekly model windows, model-specific caps, monthly "
            "tool quotas, and account balances: observed used percent over "
            "time, the burn rate at the observed active-use rate (rises over "
            "the gaps they were measured across), and an exhaustion "
            "projection that never extends past the window's own reset. "
            "projection_status says which claim the row can make; balances "
            "and lifetime windows carry no forecast and say so."
        ),
        "projection_statuses": list(_BURN_PROJECTION_STATUSES),
        "series": series,
    }


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


def _quota_provider_state(row: dict[str, Any]) -> str | None:
    value = row.get("x_provider_state")
    return value if isinstance(value, str) and value in _QUOTA_PROVIDER_STATES else None


def _quota_source_surface(row: dict[str, Any]) -> str | None:
    value = row.get("x_source_surface")
    return value if isinstance(value, str) and value in _QUOTA_SOURCE_SURFACES else None


def _quota_percent_resolution(row: dict[str, Any]) -> int | None:
    value = row.get("x_percent_resolution")
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if value in _QUOTA_PERCENT_RESOLUTIONS else None


def _quota_public_metadata(row: dict[str, Any]) -> dict[str, Any]:
    """The bounded, allowlisted reader metadata every quota surface may
    publish for an observation: which reader surface produced it, the
    percent resolution it honours, and the provider's own window state.
    Anything outside the allowlists serializes as null."""
    return {
        "source_surface": _quota_source_surface(row),
        "percent_resolution": _quota_percent_resolution(row),
        "provider_state": _quota_provider_state(row),
    }


def _is_retired_opencode_go_estimate(row: dict[str, Any]) -> bool:
    """The first-use USD replay of OpenCode Go usage events (retired by
    keeper acceptance 2026-09-14). It omitted the provider's model cost
    multipliers and anchored the weekly/monthly windows on first use rather
    than Monday 00:00 UTC / the subscription anniversary, so its figures
    are not allowance readings. The predicate is explicit and provider
    scoped: an ``opencode-go`` row labelled ``estimated`` or denominated in
    USD. Provider-observed percent rows of either reader surface (cookie or
    native) are legitimate and stay; other providers are never touched. The
    raw ledger keeps these rows unpruned for audit; no projection uses
    them."""
    if _canonical_quota_provider(row.get("provider")) != "opencode-go":
        return False
    confidence = str(row.get("measurement_confidence") or "").casefold()
    unit = str(row.get("unit") or "").casefold()
    return confidence == "estimated" or unit == "usd"


def _drop_retired_opencode_go_estimates(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [row for row in rows if not _is_retired_opencode_go_estimate(row)]


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


def _quota_observation_is_informative(row: dict[str, Any]) -> bool:
    """An observation with neither used nor remaining NOR an explicit
    provider_state/coverage marker is a historical null (e.g. the
    upstream-retired codex session window, whose old all-None rows stay
    newest forever because collection stopped emitting that window). It is
    not a card, not an ``unavailable_series`` entry, and not an observation
    worth counting — the same rule ``latest`` applies. A null row that
    CARRIES provider_state ("inactive_or_not_reported") or coverage is an
    honest signal and stays."""
    return (
        row.get("used_value") is not None
        or row.get("remaining_value") is not None
        or row.get("x_provider_state") is not None
        or row.get("x_coverage") is not None
    )


def _drop_retired_spark_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop Codex Spark quota rows at the API boundary (keeper ruling
    2026-09-06: the model is unused and its cards are noise). Collection
    stopped emitting spark_* rows the same day, but a source-only cut is not
    enough — the latest-per-identity rule would keep the final historical
    row newest forever, the exact stale-card failure mode documented in
    ``_quota_observation_is_informative``. The ledger keeps its history
    unpruned; the cards, reset rail, charts, and burn panel do not."""
    return [
        row
        for row in rows
        if not str(row.get("quota_name") or "").startswith("spark_")
    ]


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
    """Project canonical quota observations into complete UTC buckets.

    The grid is ``_BUCKET_MINUTES`` wide (keeper-ruled 2026-09-02) and shared
    with :func:`_build_unified_time_series`, so the status strip, the token
    charts and the quota charts all align on the same bucket starts.
    """
    bucket_count, bucket_starts = _bucket_grid(window_start, window_end)
    grouped: dict[_QuotaSeriesIdentity, list[dict[str, Any]]] = {}
    provider_rows: dict[str, list[dict[str, Any]]] = {}
    provider_raw_ids: dict[str, set[str]] = {}

    for row in rows:
        observed_at = _fact_datetime(row.get("observed_at"))
        if observed_at < window_start or observed_at >= window_end:
            continue
        if not _quota_observation_is_informative(row):
            continue
        if _is_retired_opencode_go_estimate(row):
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
            bucket_index = _bucket_index(observed_at, window_start)
            if 0 <= bucket_index < bucket_count:
                # The greatest deterministic key is the last observation in a tied bucket.
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
                **_quota_public_metadata(latest),
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
        "bucket_minutes": _BUCKET_MINUTES,
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
    search_ledger: Optional[str] = None,
) -> Flask:
    resolved_ledger_path = _resolve_ledger_path(atrium_root, ledger)
    resolved_unified_usage = unified_usage_ledger or os.environ.get("UNIFIED_USAGE_LEDGER_PATH")
    resolved_quota_ledger = quota_ledger or os.environ.get("QUOTA_LEDGER_PATH")
    resolved_billing_ledger = billing_ledger or os.environ.get("BILLING_LEDGER_PATH")
    resolved_search_ledger = search_ledger or os.environ.get("SEARCH_LEDGER_PATH")
    app = Flask(__name__)
    # Keeper, 2026-09-25 (msg 1552976803495280683, "option A ... lets do it"):
    # the page's fixed questions are answered from pre-built responses the
    # renderer (prebuilt_views) keeps in Redis. Only the serving dashboard sets
    # this; the renderer's own app must always run the routes.
    app.config["SERVE_PREBUILT_VIEWS"] = os.environ.get(prebuilt_views.SERVE_ENV) == "serve"

    @app.before_request
    def _serve_prebuilt_view():
        if not app.config.get("SERVE_PREBUILT_VIEWS") or request.method != "GET":
            return None
        stored = prebuilt_views.lookup(request.path, request.query_string.decode("latin-1"))
        if stored is None:
            return None
        response = app.response_class(stored["body"], status=stored["status"], content_type=stored["content_type"])
        response.headers["X-AI-Usage-Prebuilt"] = (
            f"rendered_at={stored['rendered_at']}; age_seconds={stored['age_seconds']:.0f}"
        )
        return response

    def _load_ledger_rows() -> List[Dict[str, Any]]:
        return _load_rows(resolved_ledger_path)

    @app.route("/")
    def dashboard_page():
        # The frontend is a React + Vite app (frontend/) that builds into
        # static/app/; this route serves the built entry document. The
        # after_request cache policy still stamps it no-store (it is not a
        # /static/ path), while the hashed asset files it names may cache —
        # a rebuild changes the hashes, so a cached asset can never hide a
        # deploy (keeper report 2026-08-30).
        return app.send_static_file("app/index.html")

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
            series_end = _floor_to_bucket(generated_at)
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
        identity_stats: dict[str, Any] = {}
        try:
            rows = _private_facts(
                resolved_unified_usage,
                fact_type="usage_event_v1",
                filters=typed_filters,
                cutoff=cutoff,
                before=query_before,
                identity_stats=identity_stats,
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
            # Same number format as /api/billing: ``str(Decimal)`` emits
            # scientific notation for tiny sums ("5E-7"), _decimal_text does not.
            totals[field] = _decimal_text(
                sum((Decimal(str(row.get(field))) for row in rows if row.get(field) is not None), Decimal("0"))
            )
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
        # Pricing groups the SAME rows a third way, keyed additionally by each
        # request's long-context basis, because the >272k-prompt test is
        # per request and cannot be recovered from the (provider, model)
        # aggregate the ledger view reports.
        priced_grouped: Dict[tuple[str, str, str], Dict[str, Any]] = {}
        # The same price buckets per listed row and per provider, so the
        # provider/model table and the provider shares carry their own
        # list-price equivalent (UI review item approved 2026-09-25).
        row_price_buckets: Dict[tuple[str, str], Dict[tuple[str, str, str], Dict[str, Any]]] = {}
        provider_price_buckets: Dict[str, Dict[tuple[str, str, str], Dict[str, Any]]] = {}
        by_harness: Dict[str, Dict[str, Any]] = {}
        by_provider: Dict[str, Dict[str, Any]] = {}
        for row in rows:
            # Pricing stays keyed on the raw label (the rate table's aliases
            # decide what a Meridian label may be priced as).
            _accumulate_price_bucket(priced_grouped, row)
            raw_provider = str(row.get("provider") or "unknown")
            list_provider, account_index, provider_label = _usage_provider_identity(row)
            key = (
                provider_label,
                str(row.get("model_reported") or row.get("model_requested") or "unknown"),
            )
            bucket = grouped.setdefault(
                key,
                {
                    "provider": list_provider,
                    "provider_canonical": _canonical_quota_provider(list_provider),
                    "provider_label": provider_label,
                    "provider_account_index": account_index,
                    "provider_account_count": (
                        _ANTHROPIC_ACCOUNT_COUNT if account_index is not None else None
                    ),
                    "provider_raw_ids": set(),
                    "model": key[1],
                    "events": 0,
                    "api_modes": {},
                    **{field: 0 for field in token_fields},
                    **{field: Decimal("0") for field in cost_fields},
                },
            )
            bucket["provider_raw_ids"].add(raw_provider)
            bucket["events"] += 1
            _accumulate_price_bucket(row_price_buckets.setdefault(key, {}), row)
            # Endpoint/protocol attribution — restored upstream for Hermes on
            # 2026-08-29; NULL means the source genuinely does not know.
            mode_key = str(row.get("api_mode") or "unknown")
            bucket["api_modes"][mode_key] = bucket["api_modes"].get(mode_key, 0) + 1
            for field in token_fields:
                bucket[field] += int(row.get(field) or 0)
            for field in cost_fields:
                if row.get(field) is not None:
                    bucket[field] += Decimal(str(row[field]))
            harness = str(row.get("harness") or "unknown")
            harness_bucket = by_harness.setdefault(
                harness,
                {
                    "harness": harness,
                    "events": 0,
                    **{field: 0 for field in token_fields},
                    **{field: Decimal("0") for field in cost_fields},
                },
            )
            harness_bucket["events"] += 1
            for field in token_fields:
                harness_bucket[field] += int(row.get(field) or 0)
            for field in cost_fields:
                if row.get(field) is not None:
                    harness_bucket[field] += Decimal(str(row[field]))
            # Provider-level throughput for the window donuts, grouped by the
            # canonical provider: "openai" and "openai-codex" are one
            # subscription and must be one arc, not two. The canonical key is
            # resolved here rather than in the browser so the donut legend can
            # join to a quota row without a second copy of the alias table;
            # the raw ids merged into the row ride along for the ledger view.
            canonical_provider = _usage_canonical_provider(row)
            provider_bucket = by_provider.setdefault(
                canonical_provider,
                {
                    "provider": canonical_provider,
                    "provider_canonical": canonical_provider,
                    "provider_raw_ids": set(),
                    "events": 0,
                    "substituted_events": 0,
                    **{field: 0 for field in token_fields},
                },
            )
            provider_bucket["provider_raw_ids"].add(raw_provider)
            provider_bucket["events"] += 1
            _accumulate_price_bucket(provider_price_buckets.setdefault(canonical_provider, {}), row)
            if _is_substituted_event(row):
                provider_bucket["substituted_events"] += 1
            for field in token_fields:
                provider_bucket[field] += int(row.get(field) or 0)
        for bucket in by_provider.values():
            bucket["provider_raw_ids"] = sorted(bucket["provider_raw_ids"])
            bucket["total_tokens"] = sum(
                bucket[field] for field in token_fields if field != "reasoning_tokens"
            )
            # Cached vs fresh throughput is the distinction the donut draws: a
            # cache read is billed and served differently from fresh input.
            bucket["cached_tokens"] = bucket["cache_read_tokens"]
            bucket["fresh_tokens"] = bucket["total_tokens"] - bucket["cache_read_tokens"]
            estimate = estimate_list_price(
                _finalize_price_buckets(provider_price_buckets.get(bucket["provider"], {}))
            )
            bucket["list_price_usd"] = estimate["estimate_usd"]
            bucket["list_price_priced_share"] = estimate["priced_token_share"]
        for key, bucket in grouped.items():
            bucket["provider_raw_ids"] = sorted(bucket["provider_raw_ids"])
            estimate = estimate_list_price(_finalize_price_buckets(row_price_buckets.get(key, {})))
            bucket["list_price_usd"] = estimate["estimate_usd"]
            # Share of this row's tokens that had a rate; 0 means the row is
            # unpriced, not free.
            bucket["list_price_priced_share"] = estimate["priced_token_share"]
        for bucket in (*grouped.values(), *by_harness.values()):
            bucket["total_tokens"] = sum(bucket[field] for field in token_fields if field != "reasoning_tokens")
            for field in cost_fields:
                bucket[field] = _decimal_text(bucket[field])
        totals["list_price"] = estimate_list_price(_finalize_price_buckets(priced_grouped))
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
            # Contract v2 stamp: totals count physical requests; the
            # observation count and suppressed copies stay visible so the v1
            # number is always reproducible.
            "identity": identity_stats,
            "by_harness": sorted(by_harness.values(), key=lambda item: item["harness"]),
            "by_provider": sorted(
                by_provider.values(),
                key=lambda item: (-item["total_tokens"], item["provider"]),
            ),
            "by_provider_model": sorted(
                grouped.values(), key=lambda item: (item["provider"], item["provider_label"], item["model"])
            ),
        }
        if series_start is not None and series_end is not None:
            payload["time_series"] = _build_unified_time_series(
                rows,
                window_start=series_start,
                window_end=series_end,
                generated_at=generated_at,
            )
            # Daily list-price estimates for the spend sparkline (keeper polish
            # round 2026-08-30). Day buckets are UTC calendar days spanning the
            # queried hour window, so an hours=168 query yields 8 buckets with
            # a partial first and last day. Each bucket reuses
            # estimate_list_price over its own (provider, model) aggregates —
            # unpriced tokens stay coverage per day, never a guess.
            payload["list_price_daily"] = _build_list_price_daily(
                rows, window_start=series_start, window_end=series_end
            )
        return jsonify(payload)

    @app.route("/api/rulings")
    def api_rulings():
        """Weekly ratification counts, no pricing join.

        Works with no usage ledger configured: the count of recorded keeper
        rulings is a fact on its own, and the join is the optional part.
        """
        weeks, weeks_error = _weeks_argument()
        if weeks_error is not None:
            return weeks_error
        source, source_error = _rulings_source_argument()
        if source_error is not None:
            return source_error
        try:
            scan, scan_path = _rulings_scan_with_path(atrium_root=atrium_root, source=source)
        except RulingsSourceUnavailable as exc:
            return jsonify({"error": str(exc)}), 503
        payload = build_weekly_rulings(scan, weeks=weeks)
        payload["coverage"]["scan_read"] = scan_path
        return jsonify(payload)

    @app.route("/api/rulings-per-dollar")
    def api_rulings_per_dollar():
        """Rulings per ISO week against the same week's list-price estimate.

        Keeper ruling 2026-09-10: the keeper's unit of produced value is a
        ratification, so judgment-per-dollar is computed here instead of
        mined. The dollars are the list-price ESTIMATE at the standard
        service tier over canonical usage rows — not billed cost — and the
        numerator counts rulings that were RECORDED, which is why the
        coverage block travels with the series.
        """
        weeks, weeks_error = _weeks_argument()
        if weeks_error is not None:
            return weeks_error
        source, source_error = _rulings_source_argument()
        if source_error is not None:
            return source_error
        if not resolved_unified_usage:
            return jsonify({"error": "unified usage ledger is not configured"}), 404
        try:
            scan, scan_path = _rulings_scan_with_path(atrium_root=atrium_root, source=source)
        except RulingsSourceUnavailable as exc:
            return jsonify({"error": str(exc)}), 503
        generated_at = datetime.now(timezone.utc)
        ledger_path = Path(resolved_unified_usage).expanduser()
        try:
            if ledger_path.suffix.casefold() in _SQLITE_LEDGER_SUFFIXES:
                # One set of price buckets per week, never every row of the
                # window: eight weeks of rows went past the private API's
                # 100k-row cap the day the endpoint shipped, and it returned
                # 413 from then on. Redis holds the buckets (keeper,
                # 2026-09-25, msg 1552965574315868321); without a usable copy
                # each week is read and elected on its own.
                start_days = recent_week_start_dates(weeks, generated_at)
                cached = usage_cache.week_price_buckets(ledger_path, start_days)
                if cached is not None:
                    week_buckets, price_path = cached
                else:
                    week_buckets = {
                        start_day: week_price_buckets_from_ledger(
                            ledger_path, start_day, max_rows=_PRIVATE_API_MAX_ROWS
                        )[0]
                        for start_day in start_days
                    }
                    price_path = {"read_path": "sqlite_per_week"}
                payload = build_rulings_per_dollar(
                    scan, None, weeks=weeks, now=generated_at, week_buckets=week_buckets
                )
            else:
                window_start, window_end = usage_window(weeks, generated_at)
                rows = _private_facts(
                    resolved_unified_usage,
                    fact_type="usage_event_v1",
                    filters={},
                    cutoff=window_start,
                    before=window_end,
                )
                payload = build_rulings_per_dollar(scan, rows, weeks=weeks, now=generated_at)
                price_path = {"read_path": "jsonl"}
        except UnsupportedLedgerSuffix:
            return jsonify({"error": "unsupported ledger suffix"}), 400
        except (PrivateLedgerQueryTooLarge, UsageWeekTooLarge):
            return jsonify({"error": "ledger query exceeds private API row limit"}), 413
        except (MalformedLedgerError, ValidationError, IdentityConflictError):
            return jsonify({"error": "configured private ledger is unavailable"}), 503
        payload["coverage"]["scan_read"] = scan_path
        payload["coverage"]["price_read"] = price_path
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
            series_end = _floor_to_bucket(generated_at)
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
        rows = _drop_retired_spark_rows(rows)
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
        latest = _latest_rows_per_identity(rows)

        # Account ordinals for the reset rail. account_ref itself stays private
        # (it is deliberately absent from QUOTA_RESPONSE_FIELDS), but two
        # Anthropic subscriptions have to read as two chips, not one merged
        # number. `latest` holds exactly one row per series identity and that
        # identity contains the account, so the account set here is identical to
        # the one the quota charts index, and the shared sort key makes the
        # ordinals identical too. /api/quota-burn numbers against this same
        # roster (see _ACCOUNT_ROSTER_HOURS), so "Claude 2" is one identity
        # on every surface. The roster is numbered before the retired
        # OpenCode Go estimates are dropped: those rows are not readings, but
        # they are evidence the account exists, so its presence (and the
        # weekly panel's excluded-account row) survives until native
        # readings re-establish it.
        latest_account_indexes = _quota_account_ordinals(latest)
        retired_estimate_rows = sum(_is_retired_opencode_go_estimate(row) for row in rows)
        rows = _drop_retired_opencode_go_estimates(rows)
        latest = _drop_retired_opencode_go_estimates(latest)

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
            projected.update(_quota_public_metadata(row))
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
                project(row) for row in latest if _quota_observation_is_informative(row)
            ],
            "observations": [project(row) for row in rows] if include_history else [],
            # Retired rows the read matched but no cut projects; the ledger
            # keeps them, the panel says how many it is not showing.
            "exclusions": {"retired_opencode_go_estimate_rows": retired_estimate_rows},
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

    @app.route("/api/quota-burn")
    def api_quota_burn():
        if not resolved_quota_ledger:
            return jsonify({"error": "quota ledger is not configured"}), 404
        hours, hours_error = _hours_argument()
        if hours_error is not None:
            return hours_error
        generated_at = datetime.now(timezone.utc)
        if hours is None:
            cutoff = generated_at - timedelta(days=_DEFAULT_PRIVATE_API_WINDOW_DAYS)
            query_before = None
        else:
            # One extra baseline margin beyond the requested window so the
            # earliest in-window observation has a predecessor to rise from
            # (the ledger's hourly cadence is the floor); deltas whose
            # ``to_observed_at`` falls outside the window are still honest
            # history and stay — the margin only widens sampling context.
            cutoff = generated_at - timedelta(hours=hours, seconds=_BURN_BASELINE_MARGIN_SECONDS)
            # Inclusive-now, same rule as /api/subscriptions.
            try:
                query_before = generated_at + timedelta(microseconds=1)
            except OverflowError:
                query_before = generated_at
        # The account roster is the dashboard's subscriptions window
        # (``/api/subscriptions?hours=168``: from the bucket floor of now),
        # so the ordinals here are the ordinals there. One query covers both
        # the roster and the (narrower) burn window.
        roster_cutoff = _floor_to_bucket(generated_at) - timedelta(hours=_ACCOUNT_ROSTER_HOURS)
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
                cutoff=min(cutoff, roster_cutoff),
                before=query_before,
                order="desc",
            )
        except UnsupportedLedgerSuffix:
            return jsonify({"error": "unsupported ledger suffix"}), 400
        except PrivateLedgerQueryTooLarge:
            return jsonify({"error": "ledger query exceeds private API row limit"}), 413
        except (MalformedLedgerError, ValidationError, IdentityConflictError):
            return jsonify({"error": "configured private ledger is unavailable"}), 503
        rows = _drop_retired_spark_rows(rows)
        rows.sort(key=_quota_observation_order_key, reverse=True)
        generated_at_key = _datetime_rfc3339_order_key(generated_at)

        def observed_key(row: dict[str, Any]) -> tuple[datetime, Decimal] | None:
            return _rfc3339_order_key(row.get("observed_at"))

        roster_rows = [
            row
            for row in rows
            if (key := observed_key(row)) is not None
            and key[0] >= roster_cutoff
            and (generated_at_key is None or key <= generated_at_key)
        ]
        burn_rows = [
            row
            for row in rows
            if (key := observed_key(row)) is not None and key[0] >= cutoff
        ]
        # Roster before the drop (account presence), series after it.
        account_indexes = _quota_account_ordinals(_latest_rows_per_identity(roster_rows))
        retired_estimate_rows = sum(_is_retired_opencode_go_estimate(row) for row in burn_rows)
        payload = _build_quota_burn_projection(
            _drop_retired_opencode_go_estimates(burn_rows),
            generated_at=generated_at,
            account_indexes=account_indexes,
        )
        payload["exclusions"] = {"retired_opencode_go_estimate_rows": retired_estimate_rows}
        return jsonify(payload)

    @app.route("/api/weekly-utilization")
    def api_weekly_utilization():
        """Percent of each primary weekly allowance used now and left unused
        at observed resets (keeper request 2026-09-14). Semantics live in
        ``weekly_utilization``; this route owns the bounded private read,
        the inclusive-now cut and the 168h account roster."""
        # Imported here: the pure module reuses this module's quota helpers,
        # so a top-level import would be circular.
        from .weekly_utilization import (
            WEEKS_DEFAULT,
            WEEKS_MAX,
            WEEKS_MIN,
            build_weekly_utilization,
            weekly_lookback_days,
        )

        if not resolved_quota_ledger:
            return jsonify({"error": "quota ledger is not configured"}), 404
        weeks_text = request.args.get("weeks")
        try:
            weeks = WEEKS_DEFAULT if weeks_text is None else int(weeks_text)
        except ValueError:
            return jsonify({"error": "weeks must be an integer"}), 400
        if not WEEKS_MIN <= weeks <= WEEKS_MAX:
            return jsonify({"error": f"weeks must be between {WEEKS_MIN} and {WEEKS_MAX}"}), 400
        if not Path(resolved_quota_ledger).expanduser().exists():
            return jsonify({"error": "quota ledger is not available"}), 404
        generated_at = datetime.now(timezone.utc)
        cutoff = generated_at - timedelta(days=weekly_lookback_days(weeks))
        roster_cutoff = _floor_to_bucket(generated_at) - timedelta(hours=_ACCOUNT_ROSTER_HOURS)
        try:
            query_before = generated_at + timedelta(microseconds=1)
        except OverflowError:
            query_before = generated_at
        try:
            rows = _private_facts(
                resolved_quota_ledger,
                fact_type="quota_observation_v1",
                filters={},
                cutoff=min(cutoff, roster_cutoff),
                before=query_before,
                order="desc",
            )
        except UnsupportedLedgerSuffix:
            return jsonify({"error": "unsupported ledger suffix"}), 400
        except PrivateLedgerQueryTooLarge:
            return jsonify({"error": "ledger query exceeds private API row limit"}), 413
        except (MalformedLedgerError, ValidationError, IdentityConflictError):
            return jsonify({"error": "configured private ledger is unavailable"}), 503
        rows = _drop_retired_spark_rows(rows)
        rows.sort(key=_quota_observation_order_key, reverse=True)
        generated_at_key = _datetime_rfc3339_order_key(generated_at)
        # Inclusive-now, same rule as /api/subscriptions: no future-dated
        # fact reaches the roster or the projection.
        rows = [
            row
            for row in rows
            if (key := _rfc3339_order_key(row.get("observed_at"))) is not None
            and (generated_at_key is None or key <= generated_at_key)
        ]
        roster_rows = [
            row for row in rows if _rfc3339_order_key(row.get("observed_at"))[0] >= roster_cutoff
        ]
        return jsonify(
            build_weekly_utilization(
                rows,
                generated_at=generated_at,
                weeks=weeks,
                account_indexes=_quota_account_ordinals(_latest_rows_per_identity(roster_rows)),
            )
        )

    @app.route("/api/opencode-go-usage")
    def api_opencode_go_usage():
        """Current UTC-week model explanation beside native Go quota state.

        Quota authority remains the collected provider observation. This
        read-only view never calls the provider or changes either ledger.
        """
        from .go_usage import build_go_usage

        if not resolved_unified_usage or not resolved_quota_ledger:
            return jsonify({"error": "Go usage and quota ledgers are not configured"}), 404
        ledger_paths = tuple(
            Path(path).expanduser()
            for path in (resolved_unified_usage, resolved_quota_ledger)
        )
        try:
            for path in ledger_paths:
                if not stat.S_ISREG(path.stat().st_mode):
                    return jsonify({"error": "configured private ledger is unavailable"}), 503
        except FileNotFoundError:
            return jsonify({"error": "Go usage or quota ledger is not available"}), 404
        except OSError:
            return jsonify({"error": "configured private ledger is unavailable"}), 503
        generated_at = datetime.now(timezone.utc)
        week_start = generated_at.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=generated_at.weekday())
        before = generated_at + timedelta(microseconds=1)
        try:
            usage_rows = _private_facts(
                resolved_unified_usage,
                fact_type="usage_event_v1",
                filters={"provider": "opencode-go"},
                cutoff=week_start,
                before=before,
                readonly_jsonl=True,
            )
            quota_rows = _private_facts(
                resolved_quota_ledger,
                fact_type="quota_observation_v1",
                filters={"provider": "opencode-go"},
                cutoff=week_start,
                before=before,
                readonly_jsonl=True,
            )
            if len(usage_rows) + len(quota_rows) > _PRIVATE_API_MAX_ROWS:
                raise PrivateLedgerQueryTooLarge("combined Go query exceeds row limit")
        except UnsupportedLedgerSuffix:
            return jsonify({"error": "unsupported ledger suffix"}), 400
        except PrivateLedgerQueryTooLarge:
            return jsonify({"error": "ledger query exceeds private API row limit"}), 413
        except (MalformedLedgerError, ValidationError, IdentityConflictError):
            return jsonify({"error": "configured private ledger is unavailable"}), 503
        except FileNotFoundError:
            return jsonify({"error": "Go usage or quota ledger is not available"}), 404
        except OSError:
            return jsonify({"error": "configured private ledger is unavailable"}), 503
        return jsonify(build_go_usage(usage_rows, quota_rows, generated_at=generated_at))

    @app.route("/api/searches")
    def api_searches():
        """Web search/extract calls per backend per day, with rate limits.

        Keeper ruling 2026-09-10: the masthead composer hit an exhausted Z.ai
        weekly web search limit that nothing was counting down to. This is the
        panel's data source — call volume by backend and UTC day, plus the
        rate-limited, error, and fallback counts that make an approaching
        limit visible before it stops a run.
        """
        if not resolved_search_ledger:
            return jsonify({"error": "search ledger is not configured"}), 404
        days, days_error = _days_argument()
        if days_error is not None:
            return days_error
        window_days = days or 7
        generated_at = datetime.now(timezone.utc)
        cutoff = generated_at - timedelta(days=window_days)
        typed_filters = {
            field: requested
            for field in ("provider", "harness", "purpose")
            if (requested := request.args.get(field)) is not None
        }
        try:
            rows = _private_facts(
                resolved_search_ledger,
                fact_type="search_event_v1",
                filters=typed_filters,
                cutoff=cutoff,
                order="asc",
            )
        except UnsupportedLedgerSuffix:
            return jsonify({"error": "unsupported ledger suffix"}), 400
        except PrivateLedgerQueryTooLarge:
            return jsonify({"error": "ledger query exceeds private API row limit"}), 413
        except (MalformedLedgerError, ValidationError, IdentityConflictError):
            return jsonify({"error": "configured private ledger is unavailable"}), 503
        except FileNotFoundError:
            # Nothing collected yet is a legible empty panel, not an outage:
            # the Hermes feed and this ledger can be enabled in either order.
            rows = []
        return jsonify(
            {
                **build_search_summary(rows, days=window_days, now=generated_at),
                "generated_at": _iso_z(generated_at),
            }
        )

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

    @app.route("/api/health")
    def api_health():
        """Is the AI usage ledger system working (keeper, 2026-09-25): the
        service, each ledger and its collector, the Redis copies, the systemd
        units and timers, and the Hermes watchdog jobs. See health.py."""
        from . import health

        now = datetime.now(timezone.utc)

        def fresh(path, heartbeat, lanes):
            return _ledger_freshness(path, now=now, heartbeat_name=heartbeat, lane_health_name=lanes) if path else None

        payload, status = health.build_health(
            ledgers={
                "usage": (resolved_unified_usage, fresh(resolved_unified_usage, "usage-collect.ok", "lanes-usage.json")),
                "quota": (resolved_quota_ledger, fresh(resolved_quota_ledger, "quota-collect.ok", "lanes-quota.json")),
                "billing": (resolved_billing_ledger, None),
                "search": (resolved_search_ledger, None),
            },
            redis_socket=os.environ.get(usage_cache.SOCKET_ENV),
            cron_jobs_path=os.environ.get("AI_USAGE_HERMES_CRON_JOBS", health.HERMES_CRON_JOBS),
        )
        response = jsonify(payload)
        response.status_code = status
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.route("/api/subscription-fees")
    def api_subscription_fees():
        """What each AI subscription costs, from the billing facts the Atrium
        receipts manifest imports (keeper msg 1553037883747598450, 2026-09-25):
        the latest plan charge per subscription with its weekly and monthly
        equivalents, and the last 31 days of overage, top-ups and one-offs."""
        from .subscription_fees import summarize

        if not resolved_billing_ledger:
            return jsonify({"error": "billing ledger is not configured"}), 404
        try:
            days = int(request.args.get("days", "100"))
        except ValueError:
            return jsonify({"error": "days must be an integer"}), 400
        if not 1 <= days <= 400:
            return jsonify({"error": "days must be between 1 and 400"}), 400
        now = datetime.now(timezone.utc)
        try:
            rows = _private_facts(
                resolved_billing_ledger,
                fact_type="billing_fact_v1",
                filters={},
                cutoff=now - timedelta(days=days),
                order="asc",
            )
        except UnsupportedLedgerSuffix:
            return jsonify({"error": "unsupported ledger suffix"}), 400
        except PrivateLedgerQueryTooLarge:
            return jsonify({"error": "ledger query exceeds private API row limit"}), 413
        except (MalformedLedgerError, ValidationError, IdentityConflictError):
            return jsonify({"error": "configured private ledger is unavailable"}), 503
        payload = summarize(rows, now=now, other_since=now - timedelta(days=31))
        payload["window"] = {"days": days, "other_charges_days": 31}
        payload["generated_at"] = now.isoformat().replace("+00:00", "Z")
        return jsonify(payload)

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
    search_ledger: Optional[str] = None,
) -> None:
    app = create_app(
        atrium_root=atrium_root,
        ledger=ledger,
        unified_usage_ledger=unified_usage_ledger,
        quota_ledger=quota_ledger,
        billing_ledger=billing_ledger,
        search_ledger=search_ledger,
    )
    app.run(host=host, port=port)

