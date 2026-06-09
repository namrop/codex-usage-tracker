"""Small JSONL helpers for model-routing ledgers."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Iterable, List

SECRET_KEY_PARTS = ("api_key", "token", "secret", "authorization", "bearer", "password")


def contains_secret_like_key(payload: Any) -> bool:
    if isinstance(payload, dict):
        for key, value in payload.items():
            lowered = str(key).lower()
            if any(part in lowered for part in SECRET_KEY_PARTS):
                return True
            if contains_secret_like_key(value):
                return True
    elif isinstance(payload, list):
        return any(contains_secret_like_key(item) for item in payload)
    return False


def append_jsonl(path: str, row: Dict[str, Any]) -> Dict[str, Any]:
    if contains_secret_like_key(row):
        raise ValueError("Refusing to write secret-like fields to model-routing ledger")
    target = Path(path).expanduser()
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("a", encoding="utf-8") as fp:
        fp.write(json.dumps(row, ensure_ascii=False, sort_keys=True))
        fp.write("\n")
    return row


def read_jsonl(path: str) -> List[Dict[str, Any]]:
    target = Path(path).expanduser()
    if not target.exists():
        return []
    rows: List[Dict[str, Any]] = []
    with target.open("r", encoding="utf-8") as fp:
        for line in fp:
            text = line.strip()
            if not text:
                continue
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict):
                rows.append(parsed)
    return rows


def newest_first(rows: Iterable[Dict[str, Any]], timestamp_key: str = "started_at") -> List[Dict[str, Any]]:
    return sorted(rows, key=lambda row: str(row.get(timestamp_key) or ""), reverse=True)
