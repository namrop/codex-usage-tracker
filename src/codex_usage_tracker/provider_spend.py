"""Direct provider spend ledger helpers and budget rollups."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional

from .jsonl_store import newest_first, read_jsonl
from .policy_state import (
    DEFAULT_CODEX_SUBSCRIPTION_WEEKLY_USD,
    DEFAULT_EMERGENCY_BUFFER_USD,
    DEFAULT_WEEKLY_BUDGET_CAP_USD,
)

MODEL_ROUTING_RELATIVE_DIR = "12_runtime/ledgers/model_routing"
DIRECT_PROVIDER_SPEND_LEDGER = f"{MODEL_ROUTING_RELATIVE_DIR}/direct_provider_spend_ledger.jsonl"
ROUTING_DECISION_LEDGER = f"{MODEL_ROUTING_RELATIVE_DIR}/routing_decision_ledger.jsonl"
TASK_OUTCOME_LEDGER = f"{MODEL_ROUTING_RELATIVE_DIR}/task_outcome_ledger.jsonl"
POLICY_STATE_LEDGER = f"{MODEL_ROUTING_RELATIVE_DIR}/policy_state_ledger.jsonl"


def model_routing_ledger_path(atrium_root: str, relative_path: str) -> str:
    return f"{atrium_root.rstrip('/')}/{relative_path}"


def _to_float(value: Any) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


def read_provider_spend_rows(atrium_root: str) -> List[Dict[str, Any]]:
    return read_jsonl(model_routing_ledger_path(atrium_root, DIRECT_PROVIDER_SPEND_LEDGER))


def summarize_provider_spend(rows: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    rows_list = list(rows)
    total = round(sum(_to_float(row.get("billed_usd")) for row in rows_list), 6)
    by_provider: Dict[str, float] = {}
    by_model: Dict[str, float] = {}
    for row in rows_list:
        amount = _to_float(row.get("billed_usd"))
        provider = str(row.get("provider") or "unknown")
        model = str(row.get("model") or "unknown")
        by_provider[provider] = round(by_provider.get(provider, 0.0) + amount, 6)
        by_model[f"{provider}/{model}"] = round(by_model.get(f"{provider}/{model}", 0.0) + amount, 6)
    return {
        "total_billed_usd": total,
        "by_provider_usd": by_provider,
        "by_model_usd": by_model,
        "rows": newest_first(rows_list),
    }


def _week_bounds(now: Optional[datetime] = None) -> tuple[datetime, datetime]:
    current = now or datetime.now(timezone.utc)
    current = current.astimezone(timezone.utc)
    start = current.replace(hour=0, minute=0, second=0, microsecond=0)
    start = start.replace(day=start.day)  # keep mypy/linters calm; weekday arithmetic below owns actual start
    start = current.replace(hour=0, minute=0, second=0, microsecond=0)
    start = start.fromtimestamp(start.timestamp() - start.weekday() * 86400, timezone.utc)
    end = start.fromtimestamp(start.timestamp() + 7 * 86400, timezone.utc)
    return start, end


def latest_budget_state(
    spend_rows: Iterable[Dict[str, Any]],
    *,
    budget_cap_usd: float = DEFAULT_WEEKLY_BUDGET_CAP_USD,
    codex_subscription_allocated_usd: float = DEFAULT_CODEX_SUBSCRIPTION_WEEKLY_USD,
    emergency_buffer_usd: float = DEFAULT_EMERGENCY_BUFFER_USD,
) -> Dict[str, Any]:
    week_start, week_end = _week_bounds()
    direct_spend = round(sum(_to_float(row.get("billed_usd")) for row in spend_rows), 6)
    experiment_spend = 0.0
    projected_total = round(codex_subscription_allocated_usd + direct_spend + experiment_spend, 6)
    remaining = round(budget_cap_usd - projected_total, 6)
    if remaining <= 0:
        mode = "hard_stop"
    elif remaining <= emergency_buffer_usd:
        mode = "preserve"
    elif remaining <= emergency_buffer_usd * 2:
        mode = "watch"
    else:
        mode = "healthy"
    return {
        "week_start": week_start.isoformat(),
        "week_end": week_end.isoformat(),
        "budget_cap_usd": budget_cap_usd,
        "codex_subscription_allocated_usd": codex_subscription_allocated_usd,
        "direct_provider_spend_usd": direct_spend,
        "experiment_spend_usd": experiment_spend,
        "emergency_buffer_usd": emergency_buffer_usd,
        "projected_total_agentic_spend_usd": projected_total,
        "remaining_budget_usd": remaining,
        "budget_mode": mode,
    }
