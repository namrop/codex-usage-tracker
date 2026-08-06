"""Canonical quota observations for subscription and direct-provider accounts."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import fcntl
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import time
from typing import Any, Iterable, Mapping
import uuid
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx

from .canonical_ledger import ValidationError, canonical_timestamp, decimal_string, normalize_fact
from .ledger import reconcile_snapshot_windows

OPENROUTER_CREDITS_URL = "https://openrouter.ai/api/v1/credits"
OPENROUTER_KEY_URL = "https://openrouter.ai/api/v1/key"
DEEPSEEK_BALANCE_URL = "https://api.deepseek.com/user/balance"
KIMI_CODING_USAGE_URL = "https://api.kimi.com/coding/v1/usages"
Z_AI_QUOTA_URL = "https://api.z.ai/api/monitor/usage/quota/limit"
OPENCODE_GO_QUOTA_URL = "https://opencode.ai/_server/"
GO_WINDOWS: tuple[tuple[str, timedelta, Decimal], ...] = (
    ("five_hour", timedelta(hours=5), Decimal("12")),
    ("week", timedelta(days=7), Decimal("30")),
    ("month", timedelta(days=30), Decimal("60")),
)
MAX_CODEX_QUOTA_LEDGER_BYTES = 64 * 1024 * 1024
MAX_CODEX_QUOTA_SNAPSHOTS = 10_000
MAX_CODEX_QUOTA_FACTS = 40_000


def _now(value: str | datetime | None = None) -> str:
    if value is None:
        value = datetime.now(timezone.utc).isoformat()
    if isinstance(value, datetime):
        value = value.isoformat()
    return canonical_timestamp(value)


def _quota(
    *,
    namespace: str,
    observation_id: str,
    harness: str,
    observed_at: str | datetime,
    provider: str,
    quota_name: str,
    window_kind: str = "unknown",
    unit: str = "provider_unit",
    confidence: str = "exact",
    limit: Any = None,
    remaining: Any = None,
    used: Any = None,
    resets_at: Any = None,
    window_started_at: Any = None,
    window_ends_at: Any = None,
    account_ref: str | None = None,
    **extensions: Any,
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "fact_type": "quota_observation_v1",
        "schema_version": 1,
        "source_namespace": namespace,
        "source_observation_id": observation_id,
        "harness": harness,
        "observed_at": _now(observed_at),
        "provider": provider,
        "quota_name": quota_name,
        "quota_scope": "account",
        "window_kind": window_kind,
        "window_started_at": window_started_at,
        "window_ends_at": window_ends_at,
        "resets_at": resets_at,
        "limit_value": decimal_string(limit, "limit_value"),
        "remaining_value": decimal_string(remaining, "remaining_value"),
        "used_value": decimal_string(used, "used_value"),
        "unit": unit,
        "measurement_confidence": confidence,
        "account_ref": account_ref,
        "provider_payload_ref": None,
    }
    if any(not key.startswith("x_") for key in extensions):
        raise ValidationError("quota extensions must use x_ namespaced fields")
    row.update(extensions)
    return normalize_fact(row)


def _remaining_percent(used: Any) -> str | None:
    value = decimal_string(used, "used_value")
    if value is None:
        return None
    return decimal_string(max(Decimal("0"), Decimal("100") - Decimal(value)), "remaining_value")


def _reset(value: Any) -> str | None:
    if value is None:
        return None
    return canonical_timestamp(value)


def _codex_snapshot_observations(
    snapshot: Mapping[str, Any],
    source_namespace: str,
) -> list[dict[str, Any]]:
    observed = snapshot.get("fetched_at")
    if not observed:
        raise ValueError("Codex snapshot has no fetched_at")
    snapshot_id = str(snapshot.get("id") or _now(observed))
    rows = [
        _quota(
            namespace=source_namespace,
            observation_id=f"{snapshot_id}:{name}",
            harness="codex",
            observed_at=observed,
            provider="openai",
            quota_name=name,
            window_kind="rolling",
            unit="percent",
            limit="100",
            used=snapshot.get(used_field),
            remaining=_remaining_percent(snapshot.get(used_field)),
            resets_at=_reset(snapshot.get(reset_field)),
            x_plan_type=snapshot.get("plan_type"),
        )
        for name, used_field, reset_field in (
            ("five_hour", "session_used_pct", "session_reset_at"),
            ("week", "weekly_used_pct", "weekly_reset_at"),
        )
    ]
    if "spark_session_used_pct" in snapshot or "spark_weekly_used_pct" in snapshot:
        for name, used_field, reset_field in (
            ("spark_five_hour", "spark_session_used_pct", "spark_session_reset_at"),
            ("spark_week", "spark_weekly_used_pct", "spark_weekly_reset_at"),
        ):
            rows.append(
                _quota(
                    namespace=source_namespace,
                    observation_id=f"{snapshot_id}:{name}",
                    harness="codex",
                    observed_at=observed,
                    provider="openai",
                    quota_name=name,
                    window_kind="rolling",
                    unit="percent",
                    limit="100",
                    used=snapshot.get(used_field),
                    remaining=_remaining_percent(snapshot.get(used_field)),
                    resets_at=_reset(snapshot.get(reset_field)),
                    x_plan_type=snapshot.get("plan_type"),
                )
            )
    return rows


def codex_quota_observations(
    ledger_path: str | Path,
    source_namespace: str,
    *,
    include_history: bool = False,
    history_since: str | datetime | None = None,
) -> list[dict[str, Any]]:
    """Project the newest snapshot, or a bounded retained history after a cutoff."""
    path = Path(ledger_path).expanduser()
    if not path.exists():
        return []
    if path.stat().st_size > MAX_CODEX_QUOTA_LEDGER_BYTES:
        raise ValueError("Codex snapshot ledger exceeds the bounded input size")
    cutoff = _parse_dt(history_since) if history_since is not None else None
    if include_history and cutoff is None:
        raise ValueError("history_since is required for Codex quota history")
    if not include_history and cutoff is not None:
        raise ValueError("history_since requires include_history")
    snapshots: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as fp:
        for number, line in enumerate(fp, 1):
            if not line.strip():
                continue
            if len(snapshots) >= MAX_CODEX_QUOTA_SNAPSHOTS:
                raise ValueError("Codex snapshot ledger exceeds the bounded snapshot count")
            try:
                value = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path}:{number}: malformed JSON: {exc.msg}") from exc
            if not isinstance(value, dict):
                raise ValueError(f"{path}:{number}: snapshot must be an object")
            snapshots.append(reconcile_snapshot_windows(value))
    if not snapshots:
        return []
    snapshots.sort(
        key=lambda item: _parse_dt(item.get("fetched_at")) or datetime.min.replace(tzinfo=timezone.utc)
    )
    if include_history:
        assert cutoff is not None
        selected = [
            snapshot
            for snapshot in snapshots
            if (observed := _parse_dt(snapshot.get("fetched_at"))) is not None and observed > cutoff
        ]
    else:
        selected = snapshots[-1:]
    rows: list[dict[str, Any]] = []
    for snapshot in selected:
        rows.extend(_codex_snapshot_observations(snapshot, source_namespace))
        if len(rows) > MAX_CODEX_QUOTA_FACTS:
            raise ValueError("Codex quota history exceeds the bounded fact count")
    return rows


def _payload(response: Any) -> Mapping[str, Any]:
    response.raise_for_status()
    value = response.json()
    if not isinstance(value, Mapping):
        raise ValueError("provider response must be a JSON object")
    return value


def collect_openrouter_quota(
    api_key: str,
    source_namespace: str,
    *,
    observed_at: str | datetime | None = None,
    timeout: float = 30.0,
) -> list[dict[str, Any]]:
    """Fetch OpenRouter account credits and the active API-key quota directly."""
    observed = _now(observed_at)
    headers = {"Authorization": f"Bearer {api_key}"}
    with httpx.Client(timeout=timeout) as client:
        credits_payload = _payload(client.get(OPENROUTER_CREDITS_URL, headers=headers))
        key_payload = _payload(client.get(OPENROUTER_KEY_URL, headers=headers))
    credits = credits_payload.get("data") if isinstance(credits_payload.get("data"), Mapping) else credits_payload
    key = key_payload.get("data") if isinstance(key_payload.get("data"), Mapping) else key_payload
    total = credits.get("total_credits")
    spent = credits.get("total_usage")
    balance = None
    if total is not None and spent is not None:
        balance = decimal_string(Decimal(str(total)) - Decimal(str(spent)), "remaining_value")
    rows = [
        _quota(
            namespace=source_namespace,
            observation_id=f"{observed}:credit_balance",
            harness="provider_api",
            observed_at=observed,
            provider="openrouter",
            quota_name="credit_balance",
            window_kind="lifetime",
            unit="credits",
            limit=total,
            remaining=balance,
            used=spent,
        ),
        _quota(
            namespace=source_namespace,
            observation_id=f"{observed}:api_key_quota",
            harness="provider_api",
            observed_at=observed,
            provider="openrouter",
            quota_name="api_key_quota",
            window_kind="unknown",
            unit="credits",
            limit=key.get("limit"),
            remaining=key.get("limit_remaining"),
            used=key.get("usage"),
        ),
    ]
    return rows


def collect_deepseek_quota(
    api_key: str,
    source_namespace: str,
    *,
    observed_at: str | datetime | None = None,
    timeout: float = 30.0,
) -> list[dict[str, Any]]:
    """Fetch DeepSeek's account balance without retaining credentials or raw data."""
    observed = _now(observed_at)
    with httpx.Client(timeout=timeout) as client:
        payload = _payload(
            client.get(
                DEEPSEEK_BALANCE_URL,
                headers={"Accept": "application/json", "Authorization": f"Bearer {api_key}"},
            )
        )
    balances = payload.get("balance_infos")
    if not isinstance(balances, list):
        balances = []
    rows: list[dict[str, Any]] = []
    for index, balance in enumerate(balances):
        if not isinstance(balance, Mapping):
            continue
        currency = str(balance.get("currency") or "provider_unit").lower()
        unit = "usd" if currency == "usd" else "provider_unit"
        provider_balance = decimal_string(
            balance.get("total_balance"),
            "provider_balance",
            allow_negative=True,
        )
        remaining = (
            None
            if provider_balance is None
            else max(Decimal("0"), Decimal(provider_balance))
        )
        rows.append(
            _quota(
                namespace=source_namespace,
                observation_id=f"{observed}:balance:{index}:{currency}",
                harness="provider_api",
                observed_at=observed,
                provider="deepseek",
                quota_name="account_balance",
                window_kind="lifetime",
                unit=unit,
                remaining=remaining,
                x_currency=currency.upper(),
                x_is_available=payload.get("is_available"),
                x_provider_balance=provider_balance,
                x_granted_balance=decimal_string(
                    balance.get("granted_balance"),
                    "granted_balance",
                    allow_negative=True,
                ),
                x_topped_up_balance=decimal_string(
                    balance.get("topped_up_balance"),
                    "topped_up_balance",
                    allow_negative=True,
                ),
            )
        )
    return rows


def _z_ai_decimal(value: Any, field: str) -> Decimal:
    try:
        rendered = decimal_string(value, field)
    except (TypeError, ValueError):
        raise ValueError(f"Z.AI {field} is malformed") from None
    if rendered is None:
        raise ValueError(f"Z.AI {field} is missing")
    return Decimal(rendered)


def _z_ai_reset(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError("Z.AI nextResetTime is malformed")
    if isinstance(value, str) and ("T" in value or "t" in value):
        try:
            return canonical_timestamp(value)
        except ValueError:
            raise ValueError("Z.AI nextResetTime is malformed") from None
    try:
        milliseconds = Decimal(str(value))
        if not milliseconds.is_finite() or milliseconds < 0:
            raise ValueError
        reset = datetime.fromtimestamp(float(milliseconds / 1000), timezone.utc)
        return canonical_timestamp(reset.isoformat())
    except (InvalidOperation, OSError, OverflowError, TypeError, ValueError):
        raise ValueError("Z.AI nextResetTime is malformed") from None


def _z_ai_limit_kind(entry: Mapping[str, Any]) -> tuple[str, int, int] | None:
    labels = {
        value
        for field in ("type", "name")
        if isinstance((value := entry.get(field)), str)
        and value in {"TOKENS_LIMIT", "TIME_LIMIT"}
    }
    if len(labels) > 1:
        raise ValueError("Z.AI limit type is ambiguous")
    if not labels:
        return None
    label = next(iter(labels))
    unit_value = _z_ai_decimal(entry.get("unit"), "unit")
    number_value = _z_ai_decimal(entry.get("number"), "number")
    if unit_value != unit_value.to_integral_value() or number_value != number_value.to_integral_value():
        raise ValueError("Z.AI limit unit and number must be integers")
    unit, number = int(unit_value), int(number_value)
    recognized = {
        ("TOKENS_LIMIT", 3, 5): "five_hour",
        ("TOKENS_LIMIT", 6, 1): "week",
        ("TIME_LIMIT", 5, 1): "web_search_month",
    }
    quota_name = recognized.get((label, unit, number))
    if quota_name is None:
        return None
    return quota_name, unit, number


def collect_z_ai_quota(
    api_key: str,
    source_namespace: str,
    *,
    observed_at: str | datetime | None = None,
    timeout: float = 30.0,
) -> list[dict[str, Any]]:
    """Fetch exact Z.AI coding-plan and web-search quota counters."""
    observed = _now(observed_at)
    with httpx.Client(timeout=timeout) as client:
        payload = _payload(
            client.get(
                Z_AI_QUOTA_URL,
                headers={"Accept": "application/json", "Authorization": f"Bearer {api_key}"},
            )
        )
    code = payload.get("code")
    valid_code = (
        not isinstance(code, bool)
        and isinstance(code, (int, float, Decimal))
        and Decimal(str(code)) == Decimal("200")
    )
    if payload.get("success") is not True or not valid_code:
        raise ValueError("Z.AI quota response reported failure")
    data = payload.get("data")
    limits = data.get("limits") if isinstance(data, Mapping) else None
    if not isinstance(limits, list):
        raise ValueError("Z.AI quota response has no limits array")

    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    required = {"five_hour", "week", "web_search_month"}
    for entry in limits:
        if not isinstance(entry, Mapping):
            raise ValueError("Z.AI quota limit is malformed")
        kind = _z_ai_limit_kind(entry)
        if kind is None:
            continue
        quota_name, unit, number = kind
        if quota_name in seen:
            raise ValueError(f"Z.AI {quota_name} quota is ambiguous")
        seen.add(quota_name)
        extensions: dict[str, Any] = {
            "x_source_surface": "z_ai_usage_quota",
            "x_provider_unit": unit,
            "x_provider_number": number,
        }
        if "level" in entry:
            level = entry["level"]
            if level is not None and not isinstance(level, (str, int, float, bool)):
                raise ValueError(f"Z.AI {quota_name} level is malformed")
            extensions["x_provider_level"] = level

        if quota_name in {"five_hour", "week"}:
            used = _z_ai_decimal(entry.get("percentage"), f"{quota_name} percentage")
            if used > 100:
                raise ValueError(f"Z.AI {quota_name} percentage exceeds 100")
            rows.append(
                _quota(
                    namespace=source_namespace,
                    observation_id=f"{observed}:{quota_name}",
                    harness="z_ai_api",
                    observed_at=observed,
                    provider="z-ai",
                    quota_name=quota_name,
                    window_kind="rolling",
                    unit="percent",
                    limit=100,
                    used=used,
                    remaining=Decimal("100") - used,
                    resets_at=_z_ai_reset(entry.get("nextResetTime")),
                    **extensions,
                )
            )
            continue

        used = _z_ai_decimal(entry.get("currentValue"), "web_search_month currentValue")
        limit = _z_ai_decimal(entry.get("usage"), "web_search_month usage")
        if used > limit:
            raise ValueError("Z.AI web_search_month used value exceeds its limit")
        derived_remaining = limit - used
        if entry.get("remaining") is None:
            remaining = derived_remaining
        else:
            remaining = _z_ai_decimal(entry.get("remaining"), "web_search_month remaining")
            if remaining != derived_remaining:
                raise ValueError("Z.AI web_search_month quota values are inconsistent")
        rows.append(
            _quota(
                namespace=source_namespace,
                observation_id=f"{observed}:{quota_name}",
                harness="z_ai_api",
                observed_at=observed,
                provider="z-ai",
                quota_name=quota_name,
                window_kind="fixed",
                unit="requests",
                limit=limit,
                used=used,
                remaining=remaining,
                resets_at=_z_ai_reset(entry.get("nextResetTime")),
                **extensions,
            )
        )
    if seen != required:
        missing = ", ".join(sorted(required - seen))
        raise ValueError(f"Z.AI quota response omitted required limits: {missing}")
    return rows


_OPENCODE_GO_TEXT_LIMIT = 1024 * 1024
_OPENCODE_GO_NUMBER = r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?"


def _opencode_go_text_payload(text: str) -> dict[str, dict[str, str]]:
    """Extract only the two numeric fields from bounded server-function output."""
    if len(text) > _OPENCODE_GO_TEXT_LIMIT:
        raise ValueError("OpenCode Go quota response is too large")
    payload: dict[str, dict[str, str]] = {}
    for field in ("rollingUsage", "weeklyUsage", "monthlyUsage"):
        object_pattern = re.compile(
            rf"{field}\s*:\s*(?:\$R\[\d+\]\s*=\s*)?\{{([^{{}}]{{0,512}})\}}"
        )
        objects = object_pattern.findall(text)
        if len(objects) != 1:
            raise ValueError(f"OpenCode Go {field} window is missing or ambiguous")
        body = objects[0]
        values: dict[str, str] = {}
        for value_field in ("usagePercent", "resetInSec"):
            matches = re.findall(
                rf"(?:^|,)\s*{value_field}\s*:\s*({_OPENCODE_GO_NUMBER})\s*(?=,|$)",
                body,
            )
            if len(matches) != 1:
                raise ValueError(
                    f"OpenCode Go {field} {value_field} is missing or ambiguous"
                )
            values[value_field] = matches[0]
        payload[field] = values
    return payload


def _opencode_go_decimal(value: Any, window: str, field: str) -> Decimal:
    if isinstance(value, bool):
        raise ValueError(f"OpenCode Go {window} {field} is malformed")
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise ValueError(f"OpenCode Go {window} {field} is malformed") from None
    if not parsed.is_finite():
        raise ValueError(f"OpenCode Go {window} {field} is malformed")
    return parsed


def collect_opencode_go_quota(
    workspace_id: str,
    auth_cookie: str,
    source_namespace: str,
    *,
    observed_at: str | datetime | None = None,
    timeout: float = 30.0,
) -> list[dict[str, Any]]:
    """Fetch exact rolling, weekly, and monthly OpenCode Go quota percentages."""
    observed = _now(observed_at)
    try:
        with httpx.Client(timeout=timeout) as client:
            response = client.get(
                OPENCODE_GO_QUOTA_URL,
                params={"id": "lite.subscription.get", "args": json.dumps([workspace_id])},
                headers={
                    "Accept": "application/json",
                    "Cookie": f"auth={auth_cookie}",
                    "Referer": f"https://opencode.ai/workspace/{workspace_id}/go",
                    "X-Server-Id": "lite.subscription.get",
                    "X-Server-Instance": "codex-usage-tracker",
                },
            )
            response.raise_for_status()
            try:
                payload = response.json()
            except Exception:
                text = getattr(response, "text", None)
                if callable(text):
                    text = text()
                if not isinstance(text, str):
                    raise ValueError("OpenCode Go quota response is not readable")
                payload = _opencode_go_text_payload(text)
        if not isinstance(payload, Mapping):
            raise ValueError("OpenCode Go quota response is not an object")
    except Exception:
        raise ValueError("OpenCode Go quota request failed") from None

    observed_dt = datetime.fromisoformat(observed.replace("Z", "+00:00"))
    rows: list[dict[str, Any]] = []
    windows = (
        ("rollingUsage", "five_hour", "rolling"),
        ("weeklyUsage", "week", "fixed"),
        ("monthlyUsage", "month", "fixed"),
    )
    for response_name, quota_name, window_kind in windows:
        window = payload.get(response_name)
        if not isinstance(window, Mapping):
            raise ValueError(f"OpenCode Go {quota_name} window is missing")
        used = _opencode_go_decimal(window.get("usagePercent"), quota_name, "usagePercent")
        reset_seconds = _opencode_go_decimal(
            window.get("resetInSec"), quota_name, "resetInSec"
        )
        if used < 0 or used > 100:
            raise ValueError(f"OpenCode Go {quota_name} usagePercent is outside [0, 100]")
        if reset_seconds < 0:
            raise ValueError(f"OpenCode Go {quota_name} resetInSec is negative")
        try:
            resets_at = _now(observed_dt + timedelta(seconds=float(reset_seconds)))
        except (OverflowError, OSError, ValueError):
            raise ValueError(f"OpenCode Go {quota_name} resetInSec is malformed") from None
        rows.append(
            _quota(
                namespace=source_namespace,
                observation_id=f"{observed}:{quota_name}",
                harness="opencode_go_api",
                observed_at=observed,
                provider="opencode-go",
                quota_name=quota_name,
                window_kind=window_kind,
                unit="percent",
                limit=Decimal("100"),
                used=used,
                remaining=Decimal("100") - used,
                resets_at=resets_at,
                x_source_surface="opencode_go_server_function",
            )
        )
    return rows


def _kimi_quota_detail(detail: Any, label: str) -> tuple[str, str, str, str | None]:
    if not isinstance(detail, Mapping):
        raise ValueError(f"Kimi {label} quota detail is missing")
    limit = decimal_string(detail.get("limit"), "limit_value")
    used = decimal_string(detail.get("used"), "used_value")
    remaining = decimal_string(detail.get("remaining"), "remaining_value")
    if sum(value is not None for value in (limit, used, remaining)) < 2:
        raise ValueError(f"Kimi {label} quota values are malformed or ambiguous")

    limit_value = Decimal(limit) if limit is not None else None
    used_value = Decimal(used) if used is not None else None
    remaining_value = Decimal(remaining) if remaining is not None else None
    if limit_value is not None and used_value is not None and used_value > limit_value:
        raise ValueError(f"Kimi {label} quota used value exceeds its limit")
    if (
        limit_value is not None
        and remaining_value is not None
        and remaining_value > limit_value
    ):
        raise ValueError(f"Kimi {label} quota remaining value exceeds its limit")

    if limit_value is None:
        assert used_value is not None and remaining_value is not None
        limit_value = used_value + remaining_value
    elif used_value is None:
        assert remaining_value is not None
        used_value = limit_value - remaining_value
    elif remaining_value is None:
        remaining_value = limit_value - used_value
    elif limit_value != used_value + remaining_value:
        raise ValueError(f"Kimi {label} quota values are inconsistent")

    limit = decimal_string(limit_value, "limit_value")
    used = decimal_string(used_value, "used_value")
    remaining = decimal_string(remaining_value, "remaining_value")
    assert limit is not None and used is not None and remaining is not None
    return limit, used, remaining, _reset(detail.get("resetTime"))


def collect_kimi_code_quota(
    api_key: str,
    source_namespace: str,
    *,
    observed_at: str | datetime | None = None,
    timeout: float = 30.0,
) -> list[dict[str, Any]]:
    """Fetch Kimi Code's weekly and five-hour coding allowance counters."""
    observed = _now(observed_at)
    with httpx.Client(timeout=timeout) as client:
        payload = _payload(
            client.get(
                KIMI_CODING_USAGE_URL,
                headers={"Accept": "application/json", "Authorization": f"Bearer {api_key}"},
            )
        )

    weekly_limit, weekly_used, weekly_remaining, weekly_reset = _kimi_quota_detail(
        payload.get("usage"), "weekly"
    )
    five_hour_windows: list[Mapping[str, Any]] = []
    limits = payload.get("limits")
    if isinstance(limits, list):
        for item in limits:
            if not isinstance(item, Mapping):
                continue
            window = item.get("window")
            if not isinstance(window, Mapping):
                continue
            duration = window.get("duration")
            time_unit = window.get("timeUnit")
            if (duration, time_unit) in {
                (300, "TIME_UNIT_MINUTE"),
                ("300", "TIME_UNIT_MINUTE"),
                (5, "TIME_UNIT_HOUR"),
                ("5", "TIME_UNIT_HOUR"),
            }:
                five_hour_windows.append(item)
    if len(five_hour_windows) != 1:
        raise ValueError("Kimi five-hour quota window is missing or ambiguous")
    five_hour_detail = five_hour_windows[0].get("detail")
    if not isinstance(five_hour_detail, Mapping):
        raise ValueError("Kimi five-hour quota detail is missing")
    five_hour_inactive = all(
        five_hour_detail.get(field) is None for field in ("limit", "used", "remaining")
    )
    if five_hour_inactive:
        five_limit = five_used = five_remaining = None
        five_reset = _reset(five_hour_detail.get("resetTime"))
    else:
        five_limit, five_used, five_remaining, five_reset = _kimi_quota_detail(
            five_hour_detail, "five-hour"
        )

    return [
        _quota(
            namespace=source_namespace,
            observation_id=f"{observed}:five_hour",
            harness="kimi_code",
            observed_at=observed,
            provider="kimi-coding",
            quota_name="five_hour",
            window_kind="rolling",
            unit="provider_unit",
            confidence="unknown" if five_hour_inactive else "exact",
            limit=five_limit,
            used=five_used,
            remaining=five_remaining,
            resets_at=five_reset,
            **(
                {"x_provider_state": "inactive_or_not_reported"}
                if five_hour_inactive
                else {}
            ),
        ),
        _quota(
            namespace=source_namespace,
            observation_id=f"{observed}:week",
            harness="kimi_code",
            observed_at=observed,
            provider="kimi-coding",
            quota_name="week",
            window_kind="fixed",
            unit="provider_unit",
            limit=weekly_limit,
            used=weekly_used,
            remaining=weekly_remaining,
            resets_at=weekly_reset,
        ),
    ]


def _parse_dt(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def derive_opencode_go_quotas(
    usage_facts: Iterable[Mapping[str, Any]],
    source_namespace: str,
    *,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """Estimate Go subscription windows from OpenCode's local quota-cost values."""
    observed_dt = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    facts = [row for row in usage_facts if row.get("provider") == "opencode-go"]
    harnesses = sorted({str(row.get("harness")) for row in facts if row.get("x_opencode_quota_cost_usd") is not None})
    rows: list[dict[str, Any]] = []
    for name, duration, limit in GO_WINDOWS:
        start = observed_dt - duration
        used = Decimal("0")
        valued_events = 0
        for fact in facts:
            occurred = _parse_dt(fact.get("occurred_at"))
            if occurred is None or occurred < start or occurred > observed_dt:
                continue
            value = fact.get("x_opencode_quota_cost_usd")
            if value is None:
                continue
            try:
                used += Decimal(str(value))
            except InvalidOperation:
                continue
            valued_events += 1
        rows.append(
            _quota(
                namespace=source_namespace,
                observation_id=f"{_now(observed_dt)}:{name}",
                harness="opencode",
                observed_at=observed_dt,
                provider="opencode-go",
                quota_name=name,
                window_kind="rolling",
                unit="usd",
                confidence="estimated",
                limit=limit,
                used=used,
                remaining=max(Decimal("0"), limit - used),
                window_started_at=start.isoformat(),
                window_ends_at=observed_dt.isoformat(),
                x_coverage={
                    "status": "partial",
                    "harnesses_with_values": harnesses,
                    "valued_events": valued_events,
                },
            )
        )
    return rows


_CLAUDE_USAGE_WINDOWS = (
    ("Current session", "five_hour"),
    ("Current week (all models)", "seven_day"),
    ("Current week (Fable)", "seven_day_fable"),
)


def _claude_usage_values(screen: str) -> dict[str, tuple[Decimal, str]]:
    """Parse each Claude ``/usage`` section without crossing heading boundaries."""
    lines = screen.splitlines()
    labels = {label for label, _ in _CLAUDE_USAGE_WINDOWS}
    headings = [(index, line.strip()) for index, line in enumerate(lines) if line.strip() in labels]
    by_label = {label: index for index, label in headings}
    for required in ("Current session", "Current week (all models)"):
        if required not in by_label:
            raise ValueError(f"Claude Code /usage output omitted {required}")

    values: dict[str, tuple[Decimal, str]] = {}
    ordered = sorted(headings)
    for position, (start, label) in enumerate(ordered):
        end = ordered[position + 1][0] if position + 1 < len(ordered) else len(lines)
        block = "\n".join(lines[start + 1 : end])
        used_match = re.search(r"(\d+(?:\.\d+)?)% used", block)
        reset_match = re.search(r"^\s*Resets\s+(.+?)\s*$", block, re.MULTILINE)
        if not used_match or not reset_match:
            raise ValueError(f"Claude Code /usage section {label} is incomplete")
        quota_name = dict(_CLAUDE_USAGE_WINDOWS)[label]
        values[quota_name] = (Decimal(used_match.group(1)), reset_match.group(1))
    return values


def _unlock_close(fd: int) -> None:
    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
    except Exception:
        pass
    finally:
        try:
            os.close(fd)
        except OSError:
            pass


def _tmux_capture(session_name: str) -> str:
    completed = subprocess.run(
        ["tmux", "capture-pane", "-t", session_name, "-p", "-S", "-120"],
        check=True,
        capture_output=True,
        text=True,
        timeout=5.0,
    )
    return completed.stdout


def _tmux_keys(session_name: str, *keys: str) -> None:
    subprocess.run(
        ["tmux", "send-keys", "-t", session_name, *keys],
        check=True,
        capture_output=True,
        text=True,
        timeout=5.0,
    )


def capture_claude_code_usage_screen(
    *,
    claude_command: str | Path = "claude",
    probe_dir: str | Path,
    timeout: float = 45.0,
    claude_config_dir: str | Path | None = None,
    account_ref: str | None = None,
) -> str:
    """Open Claude Code's authenticated TUI and capture its built-in ``/usage`` view.

    The probe runs in safe mode, performs no model turn, and reuses one stable
    session ID in a tracker-owned private directory so repeated collections do
    not flood Claude Code's session history. ``CLAUDE_CONFIG_DIR`` is attached
    to the pane command itself so account selection does not depend on tmux's
    inherited server environment.
    """
    config_target: Path | None = None
    if claude_config_dir is not None:
        raw_config = os.fspath(claude_config_dir)
        if not isinstance(raw_config, str) or not raw_config.strip() or "\x00" in raw_config:
            raise ValueError("Claude config directory must be a nonempty path")
        try:
            config_target = Path(raw_config).expanduser()
        except (KeyError, RuntimeError) as exc:
            raise ValueError("Claude config directory could not be expanded") from exc
        if not config_target.is_absolute():
            raise ValueError("Claude config directory must be absolute or user-home expandable")
        config_target = config_target.resolve(strict=False)
    if account_ref is not None and (not isinstance(account_ref, str) or not account_ref):
        raise ValueError("Claude quota account_ref must be a nonempty string")

    raw_target = Path(probe_dir).expanduser()
    if raw_target.is_symlink():
        raise ValueError("Claude Code probe directory must not be a symlink")
    raw_target.mkdir(parents=True, exist_ok=True, mode=0o700)
    target = raw_target.resolve()
    if target.stat().st_uid != os.getuid():
        raise ValueError("Claude Code probe directory must be owned by the current user")
    os.chmod(target, 0o700)
    lock_path = target.with_name(f".{target.name}.lock")
    lock_flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0)
    if hasattr(os, "O_NOFOLLOW"):
        lock_flags |= os.O_NOFOLLOW
    elif lock_path.is_symlink():
        raise ValueError("Claude Code probe lock must not be a symlink")
    try:
        lock_fd = os.open(lock_path, lock_flags, 0o600)
    except OSError as exc:
        if lock_path.is_symlink():
            raise ValueError("Claude Code probe lock must not be a symlink") from exc
        raise
    lock_stat = os.fstat(lock_fd)
    if not stat.S_ISREG(lock_stat.st_mode) or lock_stat.st_uid != os.getuid():
        os.close(lock_fd)
        raise ValueError("Claude Code probe lock must be a current-user regular file")
    os.fchmod(lock_fd, 0o600)
    fcntl.flock(lock_fd, fcntl.LOCK_EX)
    if any(target.iterdir()):
        _unlock_close(lock_fd)
        raise ValueError("Claude Code probe directory must be empty")
    stable_seed = f"codex-usage-tracker:{target}"
    if config_target is not None or account_ref is not None:
        stable_seed = f"{stable_seed}:{config_target or ''}:{account_ref or ''}"
    stable_session_id = str(uuid.uuid5(uuid.NAMESPACE_URL, stable_seed))
    tmux_session = f"claude-usage-{os.getpid()}-{uuid.uuid4().hex[:8]}"
    command = [
        "tmux", "new-session", "-d", "-s", tmux_session,
        "-x", "140", "-y", "50", "-c", str(target),
    ]
    if config_target is not None:
        # This assignment belongs to the pane command itself. Passing it only
        # to subprocess.run would fail when tmux reuses an existing server.
        command.extend(("env", f"CLAUDE_CONFIG_DIR={config_target}"))
    command.extend(
        (str(claude_command), "--safe-mode", "--session-id", stable_session_id)
    )
    started = time.monotonic()
    usage_sent = False
    try:
        subprocess.run(command, check=True, capture_output=True, text=True, timeout=5.0)
        while time.monotonic() - started < timeout:
            screen = _tmux_capture(tmux_session)
            if "Quick safety check" in screen and "Yes, I trust this folder" in screen:
                _tmux_keys(tmux_session, "Enter")
                time.sleep(0.5)
                continue
            if not usage_sent and "❯" in screen:
                _tmux_keys(tmux_session, "/usage", "Enter")
                usage_sent = True
                time.sleep(0.5)
                continue
            if usage_sent and "Current session" in screen and "Current week (all models)" in screen:
                try:
                    _claude_usage_values(screen)
                except ValueError:
                    time.sleep(0.25)
                    continue
                return screen
            time.sleep(0.25)
        raise TimeoutError("Claude Code /usage screen did not become ready")
    finally:
        try:
            _tmux_keys(tmux_session, "Escape")
            _tmux_keys(tmux_session, "/exit", "Enter")
        except Exception:
            pass
        try:
            subprocess.run(
                ["tmux", "kill-session", "-t", tmux_session],
                check=False,
                capture_output=True,
                text=True,
                timeout=5.0,
            )
        except Exception:
            pass
        finally:
            _unlock_close(lock_fd)


def _claude_reset_at(reset_text: str, observed: datetime) -> str | None:
    match = re.fullmatch(r"\s*(.+?)\s*\(([^()]+)\)\s*", reset_text)
    if not match:
        return None
    clock_text, timezone_name = match.groups()
    try:
        zone = ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError:
        return None
    local_observed = observed.astimezone(zone)
    for date_format in ("%b %d, %I:%M%p", "%b %d, %I%p"):
        try:
            parsed = datetime.strptime(
                f"{local_observed.year} {clock_text}", f"%Y {date_format}"
            )
        except ValueError:
            continue
        candidate = parsed.replace(tzinfo=zone)
        if candidate < local_observed - timedelta(days=30):
            candidate = candidate.replace(year=candidate.year + 1)
        return canonical_timestamp(candidate.isoformat())
    for time_format in ("%I:%M%p", "%I%p"):
        try:
            parsed = datetime.strptime(clock_text, time_format)
        except ValueError:
            continue
        candidate = local_observed.replace(
            hour=parsed.hour, minute=parsed.minute, second=0, microsecond=0
        )
        if candidate <= local_observed:
            candidate += timedelta(days=1)
        return canonical_timestamp(candidate.isoformat())
    return None


def collect_claude_code_quota(
    source_namespace: str,
    *,
    claude_command: str | Path = "claude",
    probe_dir: str | Path,
    timeout: float = 45.0,
    observed_at: str | datetime | None = None,
    claude_config_dir: str | Path | None = None,
    account_ref: str | None = None,
) -> list[dict[str, Any]]:
    """Collect subscription limits from Claude Code CLI's own ``/usage`` view."""
    observed = _now(observed_at)
    observed_dt = _parse_dt(observed)
    if observed_dt is None:
        raise ValueError("observed_at must be a timezone-aware timestamp")
    screen = capture_claude_code_usage_screen(
        claude_command=claude_command,
        probe_dir=probe_dir,
        timeout=timeout,
        claude_config_dir=claude_config_dir,
        account_ref=account_ref,
    )
    rows: list[dict[str, Any]] = []
    values = _claude_usage_values(screen)
    for quota_name, (used, reset_text) in values.items():
        resets_at = _claude_reset_at(reset_text, observed_dt)
        if resets_at is None:
            raise ValueError(f"Claude Code /usage reset time for {quota_name} is unparseable")
        rows.append(
            _quota(
                namespace=source_namespace,
                observation_id=f"{observed}:{quota_name}",
                harness="claude_code",
                observed_at=observed,
                provider="anthropic",
                quota_name=quota_name,
                window_kind="rolling",
                unit="percent",
                confidence="exact",
                limit=Decimal("100"),
                used=used,
                remaining=max(Decimal("0"), Decimal("100") - used),
                resets_at=resets_at,
                account_ref=account_ref,
                x_source_surface="claude_code_cli_usage",
            )
        )
    return rows


# Naming alias used by collector integrations.
collect_codex_quota = codex_quota_observations
