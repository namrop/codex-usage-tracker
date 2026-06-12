"""Ledger writer for Codex usage snapshots."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional


def _to_int(value: Any) -> Optional[int]:
    try:
        if value is None:
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def _to_float(value: Any, default: Optional[float] = 0.0) -> Optional[float]:
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _extract_reset_credits_available(data: Dict[str, Any]) -> Optional[int]:
    reset_credits = data.get("rate_limit_reset_credits", {})
    if not isinstance(reset_credits, dict):
        return None
    return _to_int(reset_credits.get("available_count"))


def _window_after_fields(reset_at: Optional[int], now_epoch: float, prefix: str) -> Dict[str, Optional[float]]:
    if reset_at is None:
        return {
            f"{prefix}_reset_after_seconds": None,
            f"hours_until_{prefix}_reset": None,
        }
    seconds = max(0, int(reset_at - now_epoch))
    return {
        f"{prefix}_reset_after_seconds": seconds,
        f"hours_until_{prefix}_reset": round(seconds / 3600.0, 3),
    }


def _normalize_rate_limit_entry(limit_name: str, rate_limit: Any) -> Dict[str, Any]:
    if not isinstance(rate_limit, dict):
        rate_limit = {}
    primary = rate_limit.get("primary_window", {})
    if not isinstance(primary, dict):
        primary = {}
    secondary = rate_limit.get("secondary_window", {})
    if not isinstance(secondary, dict):
        secondary = {}
    return {
        "limit_name": limit_name,
        "session_used_pct": _to_float(primary.get("used_percent"), None),
        "weekly_used_pct": _to_float(secondary.get("used_percent"), None),
        "session_reset_at": _to_int(primary.get("reset_at")),
        "weekly_reset_at": _to_int(secondary.get("reset_at")),
        "allowed": bool(rate_limit.get("allowed", True)),
        "limit_reached": bool(rate_limit.get("limit_reached", False)),
    }


def _extract_additional_rate_limits(data: Dict[str, Any]) -> list[Dict[str, Any]]:
    additional = data.get("additional_rate_limits", [])
    if not isinstance(additional, list):
        return []

    normalized = []
    for entry in additional:
        if not isinstance(entry, dict):
            continue
        limit_name = str(entry.get("limit_name") or "")
        if not limit_name:
            continue
        normalized.append(_normalize_rate_limit_entry(limit_name, entry.get("rate_limit", {})))
    return normalized


def _extract_spark_limits(additional_rate_limits: list[Dict[str, Any]]) -> Dict[str, Optional[float]]:
    matching = None
    for entry in additional_rate_limits:
        if entry.get("limit_name") == "GPT-5.3-Codex-Spark":
            matching = entry
            break

    if matching is None:
        return {
            "spark_session_used_pct": None,
            "spark_weekly_used_pct": None,
            "spark_session_reset_at": None,
            "spark_weekly_reset_at": None,
        }

    return {
        "spark_session_used_pct": _to_float(matching.get("session_used_pct"), None),
        "spark_weekly_used_pct": _to_float(matching.get("weekly_used_pct"), None),
        "spark_session_reset_at": _to_int(matching.get("session_reset_at")),
        "spark_weekly_reset_at": _to_int(matching.get("weekly_reset_at")),
    }


def append_row(data: Dict[str, Any], ledger_path: str) -> Dict[str, Any]:
    rate_limit = data.get("rate_limit", {})
    if not isinstance(rate_limit, dict):
        rate_limit = {}

    primary = rate_limit.get("primary_window", {})
    if not isinstance(primary, dict):
        primary = {}
    secondary = rate_limit.get("secondary_window", {})
    if not isinstance(secondary, dict):
        secondary = {}

    credits = data.get("credits", {})
    if not isinstance(credits, dict):
        credits = {}

    additional_rate_limits = _extract_additional_rate_limits(data)
    spark_limits = _extract_spark_limits(additional_rate_limits)
    reset_credits_available = _extract_reset_credits_available(data)
    path = Path(ledger_path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)

    fetched_at = datetime.now(timezone.utc)
    now_epoch = fetched_at.timestamp()
    session_reset_at = _to_int(primary.get("reset_at"))
    weekly_reset_at = _to_int(secondary.get("reset_at"))
    session_used_pct = _to_float(primary.get("used_percent"), None)
    weekly_used_pct = _to_float(secondary.get("used_percent"), None)
    allowed = bool(rate_limit.get("allowed", True))
    limit_reached = bool(rate_limit.get("limit_reached", False))
    unknown_state = session_used_pct is None or weekly_used_pct is None or not isinstance(data.get("rate_limit"), dict)

    row: Dict[str, Any] = {
        "id": str(uuid.uuid4()),
        "fetched_at": fetched_at.isoformat(),
        "plan_type": str(data.get("plan_type", "")),
        "allowed": allowed,
        "limit_reached": limit_reached,
        "session_used_pct": session_used_pct,
        "weekly_used_pct": weekly_used_pct,
        "session_reset_at": session_reset_at,
        "weekly_reset_at": weekly_reset_at,
        **_window_after_fields(session_reset_at, now_epoch, "session"),
        **_window_after_fields(weekly_reset_at, now_epoch, "weekly"),
        "credits_balance": str(credits.get("balance", "")),
        "credits_has_credits": bool(credits.get("has_credits", False)),
        "rate_limit_reset_credits_available": reset_credits_available,
        **spark_limits,
        "additional_rate_limits_normalized": additional_rate_limits,
        "raw_payload_present": bool(data),
        "unknown_state": unknown_state,
        "raw_payload": data,
    }

    with path.open("a", encoding="utf-8") as fp:
        fp.write(json.dumps(row, ensure_ascii=False))
        fp.write("\n")

    return row

