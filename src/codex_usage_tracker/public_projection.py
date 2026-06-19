"""Public-safe Codex token chart projection writer."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from .token_correlation import build_token_correlation_rows, resolve_state_db_path

PUBLIC_PROJECTION_FILENAME = "codex_token_chart_public.json"
DEFAULT_PUBLIC_PROJECTION_SOURCE = "sol-public-projection"


def default_public_projection_path(ledger_path: str) -> Path:
    """Return the default public projection path next to the private ledger."""
    return Path(ledger_path).expanduser().parent / PUBLIC_PROJECTION_FILENAME


def _read_usage_rows(ledger_path: str) -> List[Dict[str, Any]]:
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
    return rows


def summarize_public_rows(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Build the summary card payload consumed by the Namrop tracker."""
    total_tokens = sum(int(row.get("total_tokens") or 0) for row in rows)
    api_calls = sum(int(row.get("api_calls") or 0) for row in rows)
    latest = rows[-1] if rows else None
    return {
        "updated_at": latest.get("window_end") if latest else None,
        "window_count": len(rows),
        "total_tokens": total_tokens,
        "api_calls": api_calls,
        "latest_total_tokens": int(latest.get("total_tokens") or 0) if latest else 0,
        "latest_cache_hit_pct": latest.get("cache_hit_pct") if latest else None,
    }


def build_public_projection(
    ledger_path: str,
    *,
    state_db_path: Optional[str] = None,
    limit: int = 168,
    source: str = DEFAULT_PUBLIC_PROJECTION_SOURCE,
) -> Dict[str, Any]:
    """Build a public-safe token chart payload from private local ledgers.

    The returned structure intentionally includes only derived aggregate rows and
    summary cards. It does not project raw usage snapshots, raw API payloads,
    transcript/session bodies, credentials, or unrestricted local paths.
    """
    rows = build_token_correlation_rows(
        _read_usage_rows(ledger_path),
        state_db_path=resolve_state_db_path(state_db_path),
        limit=limit,
    )
    return {
        "rows": rows,
        "summary": summarize_public_rows(rows),
        "source": source,
    }


def write_public_projection(
    ledger_path: str,
    *,
    projection_path: Optional[str] = None,
    state_db_path: Optional[str] = None,
    limit: int = 168,
    source: str = DEFAULT_PUBLIC_PROJECTION_SOURCE,
) -> Path:
    """Write the public-safe token chart projection JSON and return its path."""
    output_path = Path(projection_path).expanduser() if projection_path else default_public_projection_path(ledger_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    payload = build_public_projection(
        ledger_path,
        state_db_path=state_db_path,
        limit=limit,
        source=source,
    )
    output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return output_path
