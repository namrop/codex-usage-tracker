"""Flask dashboard for Codex usage ledger inspection."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
import json
import os
from typing import Any, Dict, List, Optional

from flask import Flask, jsonify, render_template

from .capability_matrix import get_capability_matrix
from .codex_call_accounting import build_codex_accounting_rows
from .jsonl_store import newest_first, read_jsonl
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


DEFAULT_ATRIUM_ROOT = "/Users/luisramirez/Digital_Workspace"
DEFAULT_LEDGER_RELATIVE_PATH = "12_runtime/ledgers/codex_usage/codex_usage_ledger.jsonl"


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


def create_app(atrium_root: str = DEFAULT_ATRIUM_ROOT, ledger: Optional[str] = None) -> Flask:
    resolved_ledger_path = _resolve_ledger_path(atrium_root, ledger)
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
        return jsonify(latest_budget_state(read_provider_spend_rows(atrium_root)))

    @app.route("/api/capability-matrix")
    def api_capability_matrix():
        return jsonify(get_capability_matrix())

    @app.route("/api/provider-spend")
    def api_provider_spend():
        return jsonify(summarize_provider_spend(read_provider_spend_rows(atrium_root)))

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


def run_dashboard(atrium_root: str, ledger: Optional[str], host: str, port: int) -> None:
    app = create_app(atrium_root=atrium_root, ledger=ledger)
    app.run(host=host, port=port)

