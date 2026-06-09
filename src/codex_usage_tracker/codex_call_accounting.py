"""Normalize Codex token/accounting rows from Hermes session correlation."""

from __future__ import annotations

import hashlib
from typing import Any, Dict, Iterable, List, Optional

from .token_correlation import build_token_correlation_rows


def _stable_hourly_id(window_start: str, window_end: str) -> str:
    digest = hashlib.sha256(f"{window_start}|{window_end}|openai-codex|hourly_correlation".encode("utf-8")).hexdigest()
    return f"codex-hourly-{digest[:24]}"


def _single_model(models: Any) -> Optional[str]:
    if isinstance(models, list) and len(models) == 1:
        return str(models[0])
    return None


def build_codex_accounting_rows(
    usage_rows: Iterable[Dict[str, Any]],
    *,
    state_db_path: Optional[str] = None,
    limit: int = 168,
) -> List[Dict[str, Any]]:
    """Return normalized Codex accounting rows from currently available data.

    These rows are derived from Hermes session/window correlation. They are not
    provider-response receipts and intentionally do not assign direct dollar
    spend to Codex subscription usage.
    """
    rows: List[Dict[str, Any]] = []
    for row in build_token_correlation_rows(usage_rows, state_db_path=state_db_path, limit=limit):
        window_start = str(row.get("window_start") or "")
        window_end = str(row.get("window_end") or "")
        models = row.get("models", []) if isinstance(row.get("models", []), list) else []
        rows.append(
            {
                "id": _stable_hourly_id(window_start, window_end),
                "started_at": window_start,
                "completed_at": window_end,
                "provider": "openai-codex",
                "model": _single_model(models),
                "models": models,
                "session_id": None,
                "route_decision_id": None,
                "codex_usage_window_start": window_start,
                "codex_usage_window_end": window_end,
                "api_calls": int(row.get("api_calls") or 0),
                "codex_sessions": int(row.get("codex_sessions") or 0),
                "input_tokens": int(row.get("input_tokens") or 0),
                "cache_read_tokens": int(row.get("cache_read_tokens") or 0),
                "cache_write_tokens": int(row.get("cache_write_tokens") or 0),
                "output_tokens": int(row.get("output_tokens") or 0),
                "reasoning_tokens": int(row.get("reasoning_tokens") or 0),
                "prompt_tokens": int(row.get("prompt_tokens") or 0),
                "total_tokens": int(row.get("total_tokens") or 0),
                "cache_hit_pct": row.get("cache_hit_pct"),
                "source": "hourly_correlation",
                "quota_session_delta_pct": row.get("session_delta_pct"),
                "quota_weekly_delta_pct": row.get("weekly_delta_pct"),
                "latency_ms": None,
                "error_class": None,
            }
        )
    return rows
