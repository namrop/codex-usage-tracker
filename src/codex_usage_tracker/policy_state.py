"""Quota burn projection and protected reserve policy state."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence

DEFAULT_PROTECTED_RESERVE_PCT = 20.0
DEFAULT_WEEKLY_BUDGET_CAP_USD = 100.0
DEFAULT_CODEX_SUBSCRIPTION_WEEKLY_USD = 46.15
DEFAULT_EMERGENCY_BUFFER_USD = 10.0


def _parse_ts(raw: Any) -> Optional[datetime]:
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        parsed = datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
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
    if numeric != numeric:
        return None
    return numeric


def _sorted_samples(usage_rows: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    samples: List[Dict[str, Any]] = []
    for row in usage_rows:
        dt = _parse_ts(row.get("fetched_at"))
        if dt is None:
            continue
        clone = dict(row)
        clone["_fetched_dt"] = dt
        samples.append(clone)
    samples.sort(key=lambda row: row["_fetched_dt"])
    return samples


def _delta(left: Any, right: Any) -> Optional[float]:
    start = _to_float(left)
    end = _to_float(right)
    if start is None or end is None:
        return None
    return round(end - start, 3)


def _hours_between(start: datetime, end: datetime) -> float:
    return max((end - start).total_seconds() / 3600.0, 0.0)


def _window_stats(samples: Sequence[Dict[str, Any]], end_index: int, horizon_hours: int) -> tuple[Optional[float], Optional[float], float]:
    if end_index <= 0:
        return None, None, 0.0
    end_dt = samples[end_index]["_fetched_dt"]
    positive = 0.0
    first_index = end_index
    for idx in range(end_index - 1, -1, -1):
        left = samples[idx]
        right = samples[idx + 1]
        if _hours_between(left["_fetched_dt"], end_dt) > horizon_hours:
            break
        first_index = idx
        diff = _delta(left.get("weekly_used_pct"), right.get("weekly_used_pct"))
        if diff is not None and diff > 0:
            positive += diff
    if first_index == end_index:
        return None, None, 0.0
    net = _delta(samples[first_index].get("weekly_used_pct"), samples[end_index].get("weekly_used_pct"))
    span_hours = _hours_between(samples[first_index]["_fetched_dt"], samples[end_index]["_fetched_dt"])
    return round(positive, 3), net, span_hours


def _hours_until_reset(row: Dict[str, Any], now_dt: datetime) -> Optional[float]:
    direct = _to_float(row.get("hours_until_weekly_reset"))
    if direct is not None:
        return max(direct, 0.0)
    reset_at = _to_float(row.get("weekly_reset_at"))
    if reset_at is None:
        return None
    return max((reset_at - now_dt.timestamp()) / 3600.0, 0.0)


def build_burn_projection_rows(
    usage_rows: Iterable[Dict[str, Any]],
    *,
    horizons_hours: Sequence[int] = (6, 12, 24, 48, 168),
    protected_reserve_pct: float = DEFAULT_PROTECTED_RESERVE_PCT,
) -> List[Dict[str, Any]]:
    samples = _sorted_samples(usage_rows)
    if not samples:
        return []

    preserve_threshold = 100.0 - protected_reserve_pct
    rows: List[Dict[str, Any]] = []
    for idx, sample in enumerate(samples):
        current_weekly = _to_float(sample.get("weekly_used_pct"))
        current_session = _to_float(sample.get("session_used_pct"))
        computed_at = datetime.now(timezone.utc).isoformat()
        output: Dict[str, Any] = {
            "computed_at": computed_at,
            "window_end": sample["_fetched_dt"].isoformat(),
            "weekly_used_pct": current_weekly,
            "session_used_pct": current_session,
        }
        span_for_confidence = 0.0
        for horizon in horizons_hours:
            positive, net, span = _window_stats(samples, idx, int(horizon))
            output[f"positive_weekly_burn_last_{horizon}h_pct"] = positive
            if horizon == 24:
                output["net_weekly_delta_last_24h_pct"] = net
                span_for_confidence = span
                if positive is None or span <= 0:
                    rate = None
                else:
                    rate = round(positive / span, 3)
                output["positive_weekly_burn_rate_24h_pct_per_hour"] = rate

        hours_until_reset = _hours_until_reset(sample, sample["_fetched_dt"])
        rate_24h = output.get("positive_weekly_burn_rate_24h_pct_per_hour")
        if current_weekly is None or rate_24h is None or hours_until_reset is None:
            projected = None
            hours_until_crossing = None
        else:
            projected = round(current_weekly + rate_24h * hours_until_reset, 3)
            if current_weekly >= preserve_threshold:
                hours_until_crossing = 0.0
            elif rate_24h > 0:
                hours_until_crossing = round((preserve_threshold - current_weekly) / rate_24h, 3)
            else:
                hours_until_crossing = None

        if current_weekly is None:
            confidence = "low"
        elif span_for_confidence >= 24:
            confidence = "high"
        elif span_for_confidence >= 6:
            confidence = "medium"
        else:
            confidence = "low"

        output.update(
            {
                "projected_weekly_used_at_reset_pct": projected,
                "projected_hours_until_reserve_crossing": hours_until_crossing,
                "projection_confidence": confidence,
            }
        )
        rows.append(output)
    return rows


def latest_burn_projection(usage_rows: Iterable[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    rows = build_burn_projection_rows(usage_rows)
    return rows[-1] if rows else None


def latest_policy_state(
    usage_rows: Iterable[Dict[str, Any]],
    *,
    protected_reserve_pct: float = DEFAULT_PROTECTED_RESERVE_PCT,
) -> Dict[str, Any]:
    samples = _sorted_samples(usage_rows)
    computed_at = datetime.now(timezone.utc).isoformat()
    if not samples:
        return {
            "computed_at": computed_at,
            "policy_mode": "unknown",
            "protected_reserve_pct": protected_reserve_pct,
            "codex_default_until_weekly_used_pct": 100.0 - protected_reserve_pct,
            "recommended_default_provider": None,
            "recommended_fallback_provider": "deepseek-v4-pro",
            "reason_codes": ["no_usage_rows"],
        }

    latest = samples[-1]
    weekly = _to_float(latest.get("weekly_used_pct"))
    session = _to_float(latest.get("session_used_pct"))
    allowed = latest.get("allowed")
    if allowed is None:
        raw_rate_limit = latest.get("raw_payload", {}).get("rate_limit", {}) if isinstance(latest.get("raw_payload"), dict) else {}
        allowed = raw_rate_limit.get("allowed", True) if isinstance(raw_rate_limit, dict) else True
    allowed = bool(allowed)
    limit_reached = bool(latest.get("limit_reached", False))
    hours_until_reset = _hours_until_reset(latest, latest["_fetched_dt"])
    projection = latest_burn_projection(samples)
    projected = projection.get("projected_weekly_used_at_reset_pct") if projection else None
    projected_crossing = projection.get("projected_hours_until_reserve_crossing") if projection else None
    preserve_threshold = 100.0 - protected_reserve_pct
    caution_threshold = max(0.0, preserve_threshold - 10.0)
    emergency_threshold = 90.0
    reason_codes: List[str] = []

    if weekly is None or session is None:
        return {
            "computed_at": computed_at,
            "policy_mode": "unknown",
            "protected_reserve_pct": protected_reserve_pct,
            "codex_default_until_weekly_used_pct": preserve_threshold,
            "weekly_used_pct": weekly,
            "session_used_pct": session,
            "hours_until_weekly_reset": hours_until_reset,
            "projected_weekly_used_at_reset_pct": projected,
            "projected_hours_until_reserve_crossing": projected_crossing,
            "recommended_default_provider": None,
            "recommended_fallback_provider": "deepseek-v4-pro",
            "reason_codes": ["latest_usage_unknown"],
        }

    mode = "normal"
    if not allowed:
        reason_codes.append("codex_not_allowed")
    if limit_reached:
        reason_codes.append("codex_limit_reached")
    if weekly >= emergency_threshold:
        reason_codes.append("weekly_at_or_above_emergency_threshold")
    if reason_codes:
        mode = "emergency"
    elif weekly >= preserve_threshold:
        mode = "preserve"
        reason_codes.append("weekly_at_or_above_preserve_threshold")
    elif weekly >= caution_threshold:
        mode = "caution"
        reason_codes.append("weekly_at_or_above_caution_threshold")
    elif projected is not None and projected >= preserve_threshold:
        mode = "caution"
        reason_codes.append("projection_crosses_preserve_threshold")
    else:
        reason_codes.append("codex_below_reserve_risk")

    if mode == "normal":
        default_provider = "openai-codex"
    elif mode == "caution":
        default_provider = "openai-codex"
    else:
        default_provider = "deepseek-v4-pro"

    return {
        "computed_at": computed_at,
        "policy_mode": mode,
        "protected_reserve_pct": protected_reserve_pct,
        "codex_default_until_weekly_used_pct": preserve_threshold,
        "weekly_used_pct": weekly,
        "session_used_pct": session,
        "hours_until_weekly_reset": hours_until_reset,
        "projected_weekly_used_at_reset_pct": projected,
        "projected_hours_until_reserve_crossing": projected_crossing,
        "projection_confidence": projection.get("projection_confidence") if projection else "low",
        "recommended_default_provider": default_provider,
        "recommended_fallback_provider": "deepseek-v4-pro",
        "reason_codes": reason_codes,
    }
