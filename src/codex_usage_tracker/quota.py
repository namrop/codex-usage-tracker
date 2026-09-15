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

try:  # list_pricing is the pricing source of truth; a missing entry means "unpriced".
    from .list_pricing import _rates_for as _list_price_rates_for
except ImportError:  # pragma: no cover - only when list_pricing is absent entirely
    _list_price_rates_for = None

OPENROUTER_CREDITS_URL = "https://openrouter.ai/api/v1/credits"
OPENROUTER_KEY_URL = "https://openrouter.ai/api/v1/key"
DEEPSEEK_BALANCE_URL = "https://api.deepseek.com/user/balance"
KIMI_CODING_USAGE_URL = "https://api.kimi.com/coding/v1/usages"
Z_AI_QUOTA_URL = "https://api.z.ai/api/monitor/usage/quota/limit"
OPENCODE_GO_QUOTA_URL = "https://opencode.ai/_server/"
OPENCODE_GO_USAGE_URL = "https://opencode.ai/zen/go/v1/usage"
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
        # A window the plan no longer reports (the session window retired
        # upstream 2026-07-12) must not become an all-None observation — it
        # rendered as a permanent "Unavailable" card (keeper defect report
        # 2026-08-30). Absent window = no row, not an alarming null row.
        if snapshot.get(used_field) is not None
    ]
    # Codex Spark windows are deliberately not emitted (keeper ruling
    # 2026-09-06): the model is unused and its cards read as noise. The raw
    # snapshot fields still land in the legacy record (ledger.py unchanged),
    # but no spark_five_hour/spark_week quota rows are written. The dashboard
    # additionally drops historical spark rows at the API boundary so the
    # final emitted row cannot linger as a permanent stale card.
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
    # An account over its limit reports a negative balance / limit_remaining.
    # The canonical contract keeps remaining_value non-negative, so the signed
    # provider figure travels in an extension (the DeepSeek lane's convention)
    # and the canonical field clamps at zero instead of ceding the lane.
    provider_balance = None
    balance = None
    if total is not None and spent is not None:
        provider_balance = decimal_string(
            Decimal(str(total)) - Decimal(str(spent)), "provider_balance", allow_negative=True
        )
        balance = max(Decimal("0"), Decimal(provider_balance))
    provider_limit_remaining = decimal_string(
        key.get("limit_remaining"), "provider_limit_remaining", allow_negative=True
    )
    key_remaining = (
        None
        if provider_limit_remaining is None
        else max(Decimal("0"), Decimal(provider_limit_remaining))
    )
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
            x_provider_balance=provider_balance,
            x_overdrawn=provider_balance is not None and Decimal(provider_balance) < 0,
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
            remaining=key_remaining,
            used=key.get("usage"),
            x_provider_limit_remaining=provider_limit_remaining,
            x_overdrawn=(
                provider_limit_remaining is not None and Decimal(provider_limit_remaining) < 0
            ),
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
        # The provider reports the raw call count, which overshoots the cap by
        # the calls that were in flight when the limit tripped (live
        # 2026-09-10: currentValue 4001 against usage 4000, remaining 0). The
        # canonical contract keeps used <= limit and remaining >= 0, so the
        # raw figure travels in an extension (the OpenRouter negative-balance
        # convention) and the canonical field clamps at the cap. Ceding the
        # lane here marked the whole subscriptions surface stale for the rest
        # of the month, which hid every provider's charts, not just Z.AI's.
        provider_used = used
        if used > limit:
            used = limit
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
                x_provider_used=decimal_string(provider_used, "provider_used"),
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


# Native usage API (keeper acceptance 2026-09-14): ``usage.{rolling,weekly,
# monthly}`` → the same quota_name vocabulary the cookie reader established,
# so the cutover continues one historical series per window. ``status`` maps
# to the public provider_state vocabulary.
_OPENCODE_GO_USAGE_WINDOWS: tuple[tuple[str, str, str], ...] = (
    ("rolling", "five_hour", "rolling"),
    ("weekly", "week", "fixed"),
    ("monthly", "month", "fixed"),
)
_OPENCODE_GO_USAGE_STATES = {"ok": "ok", "rate-limited": "rate_limited"}


def _opencode_go_usage_percent(value: Any, window: str) -> Decimal:
    """A whole percentage point in [0, 100], as the server floor-rounds and caps.

    Only JSON numbers are accepted (never bool, strings or null); a fraction
    is rejected rather than rounded so the whole-point resolution this reader
    labels its rows with is never a claim the row does not honour.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"OpenCode Go {window} percent is malformed")
    parsed = Decimal(str(value))
    if not parsed.is_finite():
        raise ValueError(f"OpenCode Go {window} percent is malformed")
    if parsed < 0 or parsed > 100:
        raise ValueError(f"OpenCode Go {window} percent is outside [0, 100]")
    if parsed != parsed.to_integral_value():
        raise ValueError(f"OpenCode Go {window} percent is not a whole point")
    return parsed.to_integral_value()


def _opencode_go_usage_reset(value: Any, window: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"OpenCode Go {window} resetsAt is malformed")
    try:
        return canonical_timestamp(value)
    except (ValidationError, ValueError):
        raise ValueError(f"OpenCode Go {window} resetsAt is malformed") from None


def collect_opencode_go_usage_quota(
    api_key: str,
    source_namespace: str,
    *,
    observed_at: str | datetime | None = None,
    timeout: float = 30.0,
) -> list[dict[str, Any]]:
    """Read OpenCode Go's rolling, weekly and monthly allowance from its usage API.

    One authenticated GET (``Authorization: Bearer <OPENCODE_GO_API_KEY>``,
    no cookies or workspace id). The provider's ``status`` and absolute
    ``resetsAt`` are authoritative and travel as given; ``percent`` is the
    server's floor-rounded, 100-capped whole point, so the rows are labelled
    ``x_percent_resolution = 1`` and never claim an unrounded cost. Window
    bounds are captured only where the provider defines them: the weekly
    (Monday 00:00 UTC) and monthly (subscription anniversary) windows end at
    their reset; the rolling five-hour window claims no bound, because an
    idle account reads ``now + 5h`` without any cycle having opened. Every
    HTTP, transport, decode or shape failure raises a sanitized ``ValueError``
    that carries neither the key nor the provider's message, so the lane
    cedes and the ledger keeps its last good reading.
    """
    observed = _now(observed_at)
    try:
        with httpx.Client(timeout=timeout) as client:
            response = client.get(
                OPENCODE_GO_USAGE_URL,
                headers={"Accept": "application/json", "Authorization": f"Bearer {api_key}"},
            )
            response.raise_for_status()
            payload = response.json()
    except Exception:
        raise ValueError("OpenCode Go usage request failed") from None
    if not isinstance(payload, Mapping):
        raise ValueError("OpenCode Go usage response is not an object")
    usage = payload.get("usage")
    if not isinstance(usage, Mapping):
        raise ValueError("OpenCode Go usage response has no usage object")

    rows: list[dict[str, Any]] = []
    for response_name, quota_name, window_kind in _OPENCODE_GO_USAGE_WINDOWS:
        window = usage.get(response_name)
        if not isinstance(window, Mapping):
            raise ValueError(f"OpenCode Go {quota_name} window is missing")
        used = _opencode_go_usage_percent(window.get("percent"), quota_name)
        status = window.get("status")
        provider_state = (
            _OPENCODE_GO_USAGE_STATES.get(status) if isinstance(status, str) else None
        )
        if provider_state is None:
            raise ValueError(f"OpenCode Go {quota_name} status is malformed")
        resets_at = _opencode_go_usage_reset(window.get("resetsAt"), quota_name)
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
                window_ends_at=resets_at if window_kind == "fixed" else None,
                x_source_surface="opencode_go_usage_api",
                x_percent_resolution=1,
                x_provider_state=provider_state,
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


OPENCODE_GO_ESTIMATE_LOOKBACK = timedelta(days=31)
_OPENCODE_GO_LIST_PRICE_FIELDS = (
    ("input_tokens", "input"),
    ("cache_read_tokens", "cache_read"),
    ("cache_write_tokens", "cache_write"),
    # Canonical output_tokens already includes reasoning; reasoning is NOT
    # added again here.
    ("output_tokens", "output"),
)


def _opencode_go_event_cost(row: Mapping[str, Any]) -> tuple[Decimal | None, bool]:
    """Value one opencode-go usage event in quota USD.

    Returns ``(cost, list_priced)``. The OpenCode harness records the
    provider's own quota cost (``x_opencode_quota_cost_usd``) and that wins.
    Any other harness (Hermes routes through opencode-go too) is valued at the
    underlying model's list price from ``list_pricing``; a model without a
    rate — or a missing ``opencode-go`` pricing entry — is unvalued, never
    silently priced.
    """
    value = row.get("x_opencode_quota_cost_usd")
    if value is not None:
        try:
            return Decimal(str(value)), False
        except InvalidOperation:
            return None, False
    if _list_price_rates_for is None:
        return None, False
    model = row.get("model_reported") or row.get("model_requested") or ""
    try:
        rates = _list_price_rates_for("opencode-go", str(model))
    except Exception:
        rates = None
    if not isinstance(rates, Mapping):
        return None, False
    try:
        total = Decimal("0")
        for field, rate in _OPENCODE_GO_LIST_PRICE_FIELDS:
            total += Decimal(int(row.get(field) or 0)) * Decimal(str(rates[rate]))
    except (KeyError, InvalidOperation, TypeError, ValueError):
        return None, False
    return total / Decimal(1_000_000), True


def _first_use_window_open(
    occurrences: Iterable[datetime], duration: timedelta, now: datetime
) -> datetime | None:
    """Replay first-use anchored windows; return the open window's start, if any.

    A window opens at the first event after the previous window closed
    (``open + duration``) and closes ``duration`` later. ``occurrences`` must
    be in time order.
    """
    open_at: datetime | None = None
    for occurred in occurrences:
        if open_at is None or occurred >= open_at + duration:
            open_at = occurred
    if open_at is not None and now < open_at + duration:
        return open_at
    return None


def derive_opencode_go_quotas(
    usage_facts: Iterable[Mapping[str, Any]],
    source_namespace: str,
    *,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """Estimate Go subscription windows from opencode-go usage events.

    RETIRED FROM COLLECTION (keeper acceptance 2026-09-14): the shared Go
    allowance consumes ``round(cost * model.costMultiplier)`` and the weekly
    and monthly windows are calendar-anchored, neither of which this replay
    models, so ``collect_all`` no longer emits these rows and every operator
    projection excludes the ones already in the ledger
    (``dashboard._is_retired_opencode_go_estimate``). The pure function stays
    for audit and its existing utility tests only.

    OpenCode Go's windows are not trailing slices: each opens at first use
    after the previous window closed and runs for its full duration (the exact
    collector reads the provider's ``resetInSec``). The estimate replays that
    anchoring over every opencode-go event in time order — OpenCode-harness
    rows carrying the provider's quota cost and any other harness's rows
    valued at list price — and reports the window open at ``now``. With no
    window open the estimate is 0 used / full remaining with null bounds.
    Events are de-duplicated by canonical identity so the in-run adapter rows
    and a ledger read of the same history never double count.
    """
    observed_dt = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    events: list[tuple[datetime, Decimal | None, bool, str]] = []
    seen: set[tuple[str, str]] = set()
    for row in usage_facts:
        if row.get("provider") != "opencode-go":
            continue
        identity = (str(row.get("source_namespace")), str(row.get("source_event_id")))
        if identity in seen:
            continue
        seen.add(identity)
        occurred = _parse_dt(row.get("occurred_at"))
        if occurred is None or occurred > observed_dt:
            continue
        cost, list_priced = _opencode_go_event_cost(row)
        events.append((occurred, cost, list_priced, str(row.get("harness"))))
    events.sort(key=lambda entry: entry[0])
    harnesses = sorted({harness for _, cost, _, harness in events if cost is not None})
    rows: list[dict[str, Any]] = []
    for name, duration, limit in GO_WINDOWS:
        open_at = _first_use_window_open((occurred for occurred, *_ in events), duration, observed_dt)
        used = Decimal("0")
        valued_events = unvalued_events = list_priced_events = 0
        close_at: datetime | None = None
        if open_at is not None:
            close_at = open_at + duration
            for occurred, cost, list_priced, _ in events:
                if occurred < open_at or occurred >= close_at:
                    continue
                if cost is None:
                    unvalued_events += 1
                    continue
                used += cost
                valued_events += 1
                if list_priced:
                    list_priced_events += 1
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
                resets_at=None if close_at is None else close_at.isoformat(),
                window_started_at=None if open_at is None else open_at.isoformat(),
                window_ends_at=None if close_at is None else close_at.isoformat(),
                x_coverage={
                    "status": "partial",
                    "anchor": "first_use_estimate",
                    "harnesses_with_values": harnesses,
                    "valued_events": valued_events,
                    "unvalued_events": unvalued_events,
                    "list_priced_events": list_priced_events,
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


class ClaudeProbeBlocked(RuntimeError):
    """The ``/usage`` TUI probe reached a screen it cannot drive past.

    Raised instead of ``TimeoutError`` so the retry wrapper does not spend two
    more windows on a screen that will not change on its own: a login menu, a
    first-run onboarding step, or a CLI that already exited. The lane records
    the class, and the last captured screen is persisted beside the probe lock
    so the next real occurrence is legible without re-driving the TUI.
    """


# Screens that no keystroke from this probe can resolve. Each marker maps to the
# reason surfaced in the exception message.
_CLAUDE_PROBE_BLOCKERS: tuple[tuple[str, str], ...] = (
    ("Select login method", "login required"),
    ("Paste code here if prompted", "browser login pending"),
    ("Choose the text style", "first-run onboarding (theme picker)"),
    ("Not logged in", "login required"),
    # Claude Code 2.1.257 opens ``/usage`` as a Settings dialog; when the
    # usage endpoint is rate limited that dialog shows an error with "r to
    # retry", hides the composer, and never renders "Current session".
    ("Usage endpoint is rate limited", "usage endpoint rate limited"),
)

# The ``/usage`` Settings dialog: its tab strip, or its retry/cancel footer.
_CLAUDE_USAGE_DIALOG_RE = re.compile(
    r"Settings\s+Status\s+Config\s+Usage\s+Stats|\br to retry\b"
)
_CLAUDE_DIALOG_ERROR_RE = re.compile(r"^\s*Error:\s*(.+?)\s*$", re.MULTILINE)

# The composer is a line that starts with the prompt cursor and is not a menu
# option. Claude Code draws the same cursor glyph in front of the highlighted
# entry of every dialog (numbered onboarding menus, the yes/no trust prompt),
# which is why gating on the bare glyph typed ``/usage`` into a menu.
_CLAUDE_COMPOSER_RE = re.compile(r"^❯(?!\s*\d+\.)(?!\s*(?:No|Yes)\b)", re.MULTILINE)

# Seconds after sending ``/usage`` before it is sent again if the usage view has
# not rendered and the composer is visible (the first send was swallowed).
CLAUDE_USAGE_RESEND_AFTER = 5.0
CLAUDE_USAGE_MAX_SENDS = 3


def _claude_prompt_ready(screen: str) -> bool:
    return _CLAUDE_COMPOSER_RE.search(screen) is not None


def _claude_probe_blocker(screen: str) -> str | None:
    for marker, reason in _CLAUDE_PROBE_BLOCKERS:
        if marker in screen:
            return reason
    # Any other error inside the usage dialog is equally undriveable: the
    # composer is hidden so the resend path cannot fire, and each attempt
    # would otherwise burn its full timeout.
    if _CLAUDE_USAGE_DIALOG_RE.search(screen):
        error = _CLAUDE_DIALOG_ERROR_RE.search(screen)
        if error:
            return f"usage dialog error: {error.group(1)}"
    return None


def _claude_trust_dialog_keys(screen: str) -> tuple[str, ...]:
    """Keys that select "Yes, I trust this folder" in the workspace trust prompt.

    Enter confirms the highlighted option, and Claude Code highlights "No, exit"
    by default (observed on 2.1.257), so a bare Enter exits the CLI and the
    probe times out on an empty pane.
    """
    for line in screen.splitlines():
        if "❯" in line and "Yes, I trust this folder" in line:
            return ("Enter",)
    return ("Down", "Enter")


def _persist_probe_screen(target: Path, screen: str) -> None:
    """Keep the last screen of a failed probe beside the probe lock (0600).

    Lines carrying URLs are dropped: a login screen embeds an OAuth state/code
    in its link, and nothing in the usage view is a URL.
    """
    kept = [line for line in screen.splitlines() if "http" not in line]
    path = target.with_name(f".{target.name}.last-screen.txt")
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0), 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write("\n".join(kept).rstrip() + "\n")
    except OSError:
        pass


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
    usage_sends = 0
    usage_sent_at = 0.0
    screen = ""
    try:
        subprocess.run(command, check=True, capture_output=True, text=True, timeout=5.0)
        while time.monotonic() - started < timeout:
            try:
                screen = _tmux_capture(tmux_session)
            except subprocess.CalledProcessError as exc:
                _persist_probe_screen(target, screen)
                raise ClaudeProbeBlocked(
                    "Claude Code exited before the /usage screen rendered"
                ) from exc
            blocker = _claude_probe_blocker(screen)
            if blocker is not None:
                _persist_probe_screen(target, screen)
                raise ClaudeProbeBlocked(f"Claude Code /usage probe blocked: {blocker}")
            if "Quick safety check" in screen and "Yes, I trust this folder" in screen:
                _tmux_keys(tmux_session, *_claude_trust_dialog_keys(screen))
                time.sleep(0.5)
                continue
            if usage_sends and "Current session" in screen and "Current week (all models)" in screen:
                try:
                    _claude_usage_values(screen)
                except ValueError:
                    time.sleep(0.25)
                    continue
                return screen
            resend_due = (
                usage_sends > 0
                and usage_sends < CLAUDE_USAGE_MAX_SENDS
                and time.monotonic() - usage_sent_at >= CLAUDE_USAGE_RESEND_AFTER
            )
            if (usage_sends == 0 or resend_due) and _claude_prompt_ready(screen):
                _tmux_keys(tmux_session, "/usage", "Enter")
                usage_sends += 1
                usage_sent_at = time.monotonic()
                time.sleep(0.5)
                continue
            time.sleep(0.25)
        _persist_probe_screen(target, screen)
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


CLAUDE_USAGE_PROBE_ATTEMPTS = 3
CLAUDE_USAGE_PROBE_RETRY_BACKOFF = 3.0


def capture_claude_code_usage_screen_with_retry(
    *,
    claude_command: str | Path = "claude",
    probe_dir: str | Path,
    timeout: float = 45.0,
    claude_config_dir: str | Path | None = None,
    account_ref: str | None = None,
    attempts: int = CLAUDE_USAGE_PROBE_ATTEMPTS,
    backoff: float = CLAUDE_USAGE_PROBE_RETRY_BACKOFF,
) -> str:
    """Retry only the bounded-wait failure mode of the ``/usage`` TUI probe.

    Probe durations are bimodal: a cold Claude Code TUI occasionally misses the
    fixed window while a warm one settles in seconds, so a second attempt
    converges where a longer single timeout only spends more wall clock. Only
    ``TimeoutError`` retries — a probe-directory, lock, or tmux failure is a
    real fault and must surface on its first occurrence.
    """
    total = max(1, int(attempts))
    last: TimeoutError | None = None
    for attempt in range(total):
        try:
            return capture_claude_code_usage_screen(
                claude_command=claude_command,
                probe_dir=probe_dir,
                timeout=timeout,
                claude_config_dir=claude_config_dir,
                account_ref=account_ref,
            )
        except TimeoutError as exc:
            last = exc
            if attempt + 1 >= total:
                break
            time.sleep(max(0.0, backoff))
    assert last is not None
    raise last


def collect_claude_code_quota(
    source_namespace: str,
    *,
    claude_command: str | Path = "claude",
    probe_dir: str | Path,
    timeout: float = 45.0,
    observed_at: str | datetime | None = None,
    claude_config_dir: str | Path | None = None,
    account_ref: str | None = None,
    attempts: int = CLAUDE_USAGE_PROBE_ATTEMPTS,
    retry_backoff: float = CLAUDE_USAGE_PROBE_RETRY_BACKOFF,
) -> list[dict[str, Any]]:
    """Collect subscription limits from Claude Code CLI's own ``/usage`` view."""
    observed = _now(observed_at)
    observed_dt = _parse_dt(observed)
    if observed_dt is None:
        raise ValueError("observed_at must be a timezone-aware timestamp")
    screen = capture_claude_code_usage_screen_with_retry(
        claude_command=claude_command,
        probe_dir=probe_dir,
        timeout=timeout,
        claude_config_dir=claude_config_dir,
        account_ref=account_ref,
        attempts=attempts,
        backoff=retry_backoff,
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


CLAUDE_OAUTH_USAGE_URL = "https://api.anthropic.com/api/oauth/usage"

# ``limits[].kind`` → the quota_name vocabulary the tmux ``/usage`` probe
# established, so the API cutover continues the same historical series.
_CLAUDE_OAUTH_LIMIT_KINDS = {
    "session": "five_hour",
    "weekly_all": "seven_day",
}


class ClaudeTokenExpired(Exception):
    """The stored Claude OAuth access token is expired (locally or live 401).

    The tracker never refreshes tokens itself — a tracker-side refresh could
    rotate the refresh token and log the operator's CLI out. Callers fall back
    to the tmux ``/usage`` probe, whose CLI launch refreshes the token as a
    side effect.
    """


def _read_claude_access_token(credentials_path: str | Path) -> str:
    """Read the Claude Code OAuth access token, refusing an expired one."""
    target = Path(credentials_path).expanduser()
    payload = json.loads(target.read_text(encoding="utf-8"))
    oauth = payload.get("claudeAiOauth") if isinstance(payload, Mapping) else None
    if not isinstance(oauth, Mapping):
        raise ValueError("Claude credentials file has no claudeAiOauth object")
    token = oauth.get("accessToken")
    if not isinstance(token, str) or not token:
        raise ValueError("Claude credentials file has no accessToken")
    expires_at = oauth.get("expiresAt")
    if (
        isinstance(expires_at, (int, float))
        and not isinstance(expires_at, bool)
        and expires_at / 1000 < time.time()
    ):
        raise ClaudeTokenExpired("Claude OAuth access token is expired")
    return token


def collect_claude_code_quota_api(
    source_namespace: str,
    *,
    credentials_path: str | Path,
    account_ref: str | None = None,
    observed_at: str | datetime | None = None,
    timeout: float = 10.0,
) -> list[dict[str, Any]]:
    """Collect subscription limits from Anthropic's OAuth usage API.

    This is the same read-only endpoint Claude Code's TUI polls for its
    ``/usage`` view. The tracker reads the stored access token and performs
    one GET — it must never refresh tokens and never POST anywhere; a
    tracker-side refresh could rotate the refresh token and log the
    operator's CLI out. An expired token (checked locally before the request,
    or reported by a live 401) raises :class:`ClaudeTokenExpired` so the
    caller can decide whether to fall back to the TUI probe.
    """
    observed = _now(observed_at)
    token = _read_claude_access_token(credentials_path)
    response: Any = None
    for attempt in range(2):
        try:
            with httpx.Client(timeout=timeout) as client:
                response = client.get(
                    CLAUDE_OAUTH_USAGE_URL,
                    headers={"Authorization": f"Bearer {token}"},
                )
            break
        except httpx.TransportError:
            # At most one retry, for a transient transport fault only. The
            # bimodal-timeout pathology of the TUI probe does not exist on a
            # plain GET, so no multi-attempt wrapper belongs here.
            if attempt:
                raise
    if response.status_code == 401:
        raise ClaudeTokenExpired("Claude OAuth usage API rejected the access token (401)")
    payload = _payload(response)
    limits = payload.get("limits")
    if not isinstance(limits, list):
        raise ValueError("Claude OAuth usage payload has no limits array")
    rows: list[dict[str, Any]] = []
    seen_kinds: set[str] = set()
    for entry in limits:
        if not isinstance(entry, Mapping):
            continue
        kind = entry.get("kind")
        if kind in _CLAUDE_OAUTH_LIMIT_KINDS:
            quota_name = _CLAUDE_OAUTH_LIMIT_KINDS[kind]
        elif kind == "weekly_scoped":
            scope = entry.get("scope")
            model = scope.get("model") if isinstance(scope, Mapping) else None
            display_name = model.get("display_name") if isinstance(model, Mapping) else None
            if not isinstance(display_name, str) or not display_name:
                raise ValueError(
                    "Claude OAuth usage weekly_scoped limit has no model display_name"
                )
            quota_name = f"seven_day_{display_name.lower()}"
        else:
            # Codenamed experimental windows come and go; only the stable
            # kinds are canonical.
            continue
        seen_kinds.add(str(kind))
        percent = entry.get("percent")
        if not isinstance(percent, (int, float)) or isinstance(percent, bool):
            raise ValueError(f"Claude OAuth usage limit {quota_name} has no percent")
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
                limit="100",
                used=str(percent),
                remaining=_remaining_percent(percent),
                resets_at=_reset(entry.get("resets_at")),
                account_ref=account_ref,
                x_source_surface="claude_oauth_usage_api",
            )
        )
    for required in ("session", "weekly_all"):
        if required not in seen_kinds:
            raise ValueError(f"Claude OAuth usage payload omitted {required}")
    return rows


# Naming alias used by collector integrations.
collect_codex_quota = codex_quota_observations
