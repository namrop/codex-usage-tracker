"""Flask dashboard for Codex usage ledger inspection."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
import json
import os
from typing import Any, Dict, List, Optional

from flask import Flask, jsonify, render_template


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
                    "plan_type": None,
                }
            )

        first = rows[-1].get("fetched_at")
        last = current.get("fetched_at")
        return jsonify(
            {
                "total_rows": len(rows),
                "first_fetched_at": first,
                "last_fetched_at": last,
                "current_session_used_pct": _to_float(current.get("session_used_pct")),
                "current_weekly_used_pct": _to_float(current.get("weekly_used_pct")),
                "current_spark_session_used_pct": _to_float(current.get("spark_session_used_pct")),
                "current_spark_weekly_used_pct": _to_float(current.get("spark_weekly_used_pct")),
                "plan_type": current.get("plan_type"),
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

    return app


def run_dashboard(atrium_root: str, ledger: Optional[str], host: str, port: int) -> None:
    app = create_app(atrium_root=atrium_root, ledger=ledger)
    app.run(host=host, port=port)

