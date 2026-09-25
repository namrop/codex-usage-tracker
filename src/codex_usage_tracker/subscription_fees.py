"""What Luis pays per AI subscription, as billing facts.

Keeper, 2026-09-25 (Discord unified usage ledger thread, msg
1553037883747598450): "we should record what I pay each month per
subscription. The monthly fees should be in my emails."

The source of truth is a receipts manifest in the Atrium (one JSON object per
line, each a charge read off a receipt email). ``billing-import`` turns it into
``billing_fact_v1`` facts; ``/api/subscription-fees`` reads them back per
subscription. A rate reconstructed by arithmetic never goes in.

A ``stated_rate`` line is the rate Luis gave himself for a subscription whose
charges are not in his mail (keeper, 2026-09-25, msg 1553054862856556656:
"chatgpt pro is $200/month, it's on another card that is my mother's"). It is
not a charge: ``billing-import`` skips it, so it never enters the billing
ledger or its totals. ``/api/subscription-fees`` reads it straight from the
manifest and uses it as the fee when it is newer than the latest plan charge
(or there is none), marked ``fee_source: "stated"``.

Manifest line::

    {"receipt_id": "2704-9084-8390", "subscription": "claude-1", "kind": "plan",
     "plan": "Claude Max 20x", "charged_at": "2026-08-03", "amount": "200.00",
     "currency": "USD", "period_start": "2026-08-03", "period_end": "2026-09-03",
     "evidence": "personal-gmail email_..."}
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable, Mapping

MANIFEST_NAMESPACE = "atrium:ai_subscription_receipts"

# Subscription key -> (canonical provider, account ordinal or None, display name).
# The ordinals are the quota surfaces' "Claude 1" / "Claude 2".
SUBSCRIPTIONS: dict[str, tuple[str, int | None, str]] = {
    "claude-1": ("anthropic", 1, "Claude 1"),
    "claude-2": ("anthropic", 2, "Claude 2"),
    "codex": ("openai-codex", None, "Codex"),
    "z-ai": ("z-ai-glm", None, "Z.AI"),
    "kimi": ("kimi-k3-coding", None, "Kimi K3"),
    "opencode-go": ("opencode-go", None, "OpenCode Go"),
    "openrouter": ("openrouter", None, "OpenRouter"),
    "deepseek": ("deepseek", None, "DeepSeek"),
}

# plan: the recurring subscription charge. overage: usage billed on top of a
# plan (Claude extra usage). topup: prepaid credit. proration / one_time:
# charges that are not the recurring rate. refund: money back (negative).
KINDS = frozenset({"plan", "overage", "topup", "proration", "one_time", "refund"})
STATED_RATE = "stated_rate"


class ManifestError(ValueError):
    pass


def _timestamp(value: Any, field: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ManifestError(f"{field} is missing")
    try:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ManifestError(f"{field} is not an ISO date: {text}") from exc
    if len(text) == 10:
        # A bare receipt date: the charge day, at 00:00 UTC.
        moment = moment.replace(tzinfo=timezone.utc)
    elif moment.tzinfo is None:
        raise ManifestError(f"{field} has no UTC offset: {text}")
    return moment.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def description_code(subscription: str, kind: str, plan: str | None) -> str:
    """``claude-1|plan|Claude Max 20x``: what the billing API returns per row."""
    return "|".join([subscription, kind, (plan or "").replace("|", "/")])


def parse_description_code(code: Any) -> tuple[str, str, str] | None:
    parts = str(code or "").split("|")
    if len(parts) != 3 or parts[0] not in SUBSCRIPTIONS or parts[1] not in KINDS:
        return None
    return parts[0], parts[1], parts[2]


def fact_from_entry(entry: Mapping[str, Any]) -> dict[str, Any]:
    subscription = str(entry.get("subscription") or "")
    if subscription not in SUBSCRIPTIONS:
        raise ManifestError(f"unknown subscription {subscription!r}")
    kind = str(entry.get("kind") or "")
    if kind not in KINDS:
        raise ManifestError(f"unknown kind {kind!r}")
    try:
        amount = Decimal(str(entry.get("amount")))
    except (InvalidOperation, TypeError) as exc:
        raise ManifestError(f"amount is not a number: {entry.get('amount')!r}") from exc
    if kind == "refund":
        amount = -abs(amount)
    elif amount < 0:
        raise ManifestError("only a refund may be negative")
    occurred_at = _timestamp(entry.get("charged_at"), "charged_at")
    receipt = str(entry.get("receipt_id") or "").strip() or None
    identity = receipt or hashlib.sha256(
        f"{subscription}|{kind}|{occurred_at}|{amount}".encode()
    ).hexdigest()[:24]
    provider, _index, _name = SUBSCRIPTIONS[subscription]
    fact: dict[str, Any] = {
        "fact_type": "billing_fact_v1",
        "schema_version": 1,
        "source_namespace": MANIFEST_NAMESPACE,
        "source_billing_fact_id": f"{subscription}:{identity}",
        "provider": provider,
        "occurred_at": occurred_at,
        "transaction_kind": "refund" if kind == "refund" else "charge",
        "status": "posted",
        "amount": str(amount),
        "currency": str(entry.get("currency") or "USD").upper(),
        "billing_period_start": (
            _timestamp(entry["period_start"], "period_start") if entry.get("period_start") else None
        ),
        "billing_period_end": (
            _timestamp(entry["period_end"], "period_end") if entry.get("period_end") else None
        ),
        "invoice_id": receipt,
        "provider_receipt_id": receipt,
        "description_code": description_code(subscription, kind, entry.get("plan")),
    }
    return fact


def _manifest_entries(path: str | Path) -> Iterable[tuple[int, dict[str, Any]]]:
    for number, line in enumerate(Path(path).expanduser().read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip() or line.lstrip().startswith("//"):
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ManifestError(f"line {number}: not JSON ({exc.msg})") from exc
        if not isinstance(entry, dict):
            raise ManifestError(f"line {number}: not a JSON object")
        yield number, entry


def read_manifest(path: str | Path) -> list[dict[str, Any]]:
    """The manifest's charges as billing facts. Stated rates are not charges
    and are skipped (see ``read_stated_rates``)."""
    facts: list[dict[str, Any]] = []
    for number, entry in _manifest_entries(path):
        if entry.get("kind") == STATED_RATE:
            continue
        try:
            facts.append(fact_from_entry(entry))
        except ManifestError as exc:
            raise ManifestError(f"line {number}: {exc}") from exc
    return facts


def stated_rate_from_entry(entry: Mapping[str, Any]) -> dict[str, Any]:
    subscription = str(entry.get("subscription") or "")
    if subscription not in SUBSCRIPTIONS:
        raise ManifestError(f"unknown subscription {subscription!r}")
    if entry.get("per") != "month":
        raise ManifestError("a stated rate must say per: month")
    try:
        amount = Decimal(str(entry.get("amount")))
    except (InvalidOperation, TypeError) as exc:
        raise ManifestError(f"amount is not a number: {entry.get('amount')!r}") from exc
    if amount < 0:
        raise ManifestError("a stated rate cannot be negative")
    return {
        "subscription": subscription,
        "plan": str(entry.get("plan") or "") or None,
        "amount": str(amount),
        "currency": str(entry.get("currency") or "USD").upper(),
        "stated_at": _timestamp(entry.get("stated_at"), "stated_at"),
    }


def read_stated_rates(path: str | Path) -> list[dict[str, Any]]:
    rates: list[dict[str, Any]] = []
    for number, entry in _manifest_entries(path):
        if entry.get("kind") != STATED_RATE:
            continue
        try:
            rates.append(stated_rate_from_entry(entry))
        except ManifestError as exc:
            raise ManifestError(f"line {number}: {exc}") from exc
    return rates


def _period_days(row: Mapping[str, Any]) -> float | None:
    start, end = row.get("billing_period_start"), row.get("billing_period_end")
    if not start or not end:
        return None
    try:
        days = (
            datetime.fromisoformat(str(end).replace("Z", "+00:00"))
            - datetime.fromisoformat(str(start).replace("Z", "+00:00"))
        ).total_seconds() / 86400
    except ValueError:
        return None
    return days if days > 0 else None


def summarize(
    rows: Iterable[Mapping[str, Any]],
    *,
    now: datetime,
    other_since: datetime | None = None,
    stated_rates: Iterable[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    """Per subscription: the latest plan charge and its weekly equivalent, plus
    every overage, top-up, proration, one-time charge and refund in the rows
    (since ``other_since`` when given). A stated rate newer than the latest
    plan charge, or for a subscription with none, is the fee instead."""
    since = other_since.astimezone(timezone.utc).isoformat().replace("+00:00", "Z") if other_since else ""
    latest: dict[str, Mapping[str, Any]] = {}
    other: list[dict[str, Any]] = []
    for row in rows:
        parsed = parse_description_code(row.get("description_code"))
        if parsed is None:
            continue
        subscription, kind, plan = parsed
        if kind == "plan":
            current = latest.get(subscription)
            if current is None or str(row["occurred_at"]) > str(current["occurred_at"]):
                latest[subscription] = row
        elif str(row.get("occurred_at") or "") >= since:
            other.append({
                "subscription": subscription,
                "label": SUBSCRIPTIONS[subscription][2],
                "kind": kind,
                "plan": plan or None,
                "occurred_at": row.get("occurred_at"),
                "amount": str(row.get("amount")),
                "currency": row.get("currency"),
            })
    stated: dict[str, Mapping[str, Any]] = {}
    for rate in stated_rates:
        current = stated.get(rate["subscription"])
        if current is None or str(rate["stated_at"]) > str(current["stated_at"]):
            stated[rate["subscription"]] = rate
    subscriptions: list[dict[str, Any]] = []
    for key, (provider, index, label) in SUBSCRIPTIONS.items():
        row = latest.get(key)
        rate = stated.get(key)
        if rate is not None and (row is None or str(rate["stated_at"]) > str(row["occurred_at"])):
            amount = Decimal(str(rate["amount"]))
            subscriptions.append({
                "subscription": key,
                "provider": provider,
                "provider_account_index": index,
                "label": label,
                "plan": rate.get("plan"),
                "amount": str(amount),
                "currency": rate.get("currency"),
                "fee_source": "stated",
                "stated_at": rate["stated_at"],
                "charged_at": None,
                "billing_period_start": None,
                "billing_period_end": None,
                "period_days": None,
                "fee_per_week": str((amount * Decimal(12) / Decimal(52)).quantize(Decimal("0.01"))),
                "fee_per_month": str(amount.quantize(Decimal("0.01"))),
                "period_current": None,
            })
            continue
        if row is None:
            continue
        amount = Decimal(str(row["amount"]))
        days = _period_days(row)
        # A monthly charge (no period on the receipt, or a 27-32 day period)
        # IS the monthly fee, and its weekly equivalent is x 12 / 52 (the
        # mailroom cost-baseline convention). Any other period is scaled by
        # its own length.
        if days is None or 27 <= days <= 32:
            monthly = amount
            weekly = amount * Decimal(12) / Decimal(52)
        else:
            weekly = amount * Decimal(7) / Decimal(str(days))
            monthly = weekly * Decimal(52) / Decimal(12)
        end = row.get("billing_period_end")
        paid_through = None
        if end:
            paid_through = datetime.fromisoformat(str(end).replace("Z", "+00:00")) > now
        subscriptions.append({
            "subscription": key,
            "provider": provider,
            "provider_account_index": index,
            "label": label,
            "plan": parse_description_code(row["description_code"])[2] or None,  # type: ignore[index]
            "amount": str(amount),
            "currency": row.get("currency"),
            "fee_source": "receipt",
            "charged_at": row.get("occurred_at"),
            "billing_period_start": row.get("billing_period_start"),
            "billing_period_end": end,
            "period_days": round(days, 2) if days else None,
            "fee_per_week": str(weekly.quantize(Decimal("0.01"))),
            "fee_per_month": str(monthly.quantize(Decimal("0.01"))),
            # True while the latest charge's period is still running; False
            # means no later charge is recorded yet, not that it lapsed.
            "period_current": paid_through,
        })
    other.sort(key=lambda item: str(item["occurred_at"]), reverse=True)
    return {"subscriptions": subscriptions, "other_charges": other}
