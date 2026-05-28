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


def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _extract_spark_limits(data: Dict[str, Any]) -> Dict[str, Optional[float]]:
    additional = data.get("additional_rate_limits", [])
    if not isinstance(additional, list):
        return {
            "spark_session_used_pct": None,
            "spark_weekly_used_pct": None,
            "spark_session_reset_at": None,
            "spark_weekly_reset_at": None,
        }

    matching = None
    for entry in additional:
        if not isinstance(entry, dict):
            continue
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

    spark_rate_limit = matching.get("rate_limit", {})
    if not isinstance(spark_rate_limit, dict):
        spark_rate_limit = {}

    spark_primary = spark_rate_limit.get("primary_window", {})
    if not isinstance(spark_primary, dict):
        spark_primary = {}
    spark_secondary = spark_rate_limit.get("secondary_window", {})
    if not isinstance(spark_secondary, dict):
        spark_secondary = {}

    return {
        "spark_session_used_pct": _to_float(spark_primary.get("used_percent"), None),
        "spark_weekly_used_pct": _to_float(spark_secondary.get("used_percent"), None),
        "spark_session_reset_at": _to_int(spark_primary.get("reset_at")),
        "spark_weekly_reset_at": _to_int(spark_secondary.get("reset_at")),
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

    spark_limits = _extract_spark_limits(data)
    path = Path(ledger_path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)

    row: Dict[str, Any] = {
        "id": str(uuid.uuid4()),
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "plan_type": str(data.get("plan_type", "")),
        "session_used_pct": _to_float(primary.get("used_percent")),
        "weekly_used_pct": _to_float(secondary.get("used_percent")),
        "session_reset_at": _to_int(primary.get("reset_at")),
        "weekly_reset_at": _to_int(secondary.get("reset_at")),
        "credits_balance": str(credits.get("balance", "")),
        "credits_has_credits": bool(credits.get("has_credits", False)),
        **spark_limits,
        "raw_payload": data,
    }

    with path.open("a", encoding="utf-8") as fp:
        fp.write(json.dumps(row, ensure_ascii=False))
        fp.write("\n")

    return row

