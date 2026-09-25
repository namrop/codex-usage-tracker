"""Find AI subscription receipts in the oasis-mailroom store and turn them into
receipts-manifest lines.

Keeper, 2026-09-25, Discord unified usage ledger thread, msg
1553058626350874726: "pocket and elevenlabs should go in the reciepts file. we
should also have the monthly job". The monthly job syncs receipt mail into the
mailroom store, runs this scan with ``--append``, imports the manifest into the
billing ledger and commits it.

Rules:
- The mailroom store is opened read-only.
- Only vendors with a rule below are recorded, and only when the receipt parses
  completely (receipt number, paid date, amount paid, a recognised line item).
  Anything else from a vendor in the file is reported as needing a look, never
  guessed.
- A receipt already in the manifest (same receipt number) is skipped. A plan
  receipt matching an existing plan line with no receipt number (a card
  authorization for the same payment: same subscription and amount, within two
  days) is reported as corroboration, not added, so one payment is never
  recorded twice.
- A second plan charge within 25 days of the previous one (an upgrade or a
  proration looks exactly like a plan charge on these receipts) needs a look.
- ``charged_at`` is the receipt's own "Paid" date.
"""
from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .subscription_fees import STATED_RATE, SUBSCRIPTIONS, manifest_entries

MAILROOM_DB = "/var/lib/pharos/services/oasis-mailroom/state/oasis-mailroom.sqlite3"
MAILROOM_ROOT = "/var/lib/pharos/services/oasis-mailroom"

_MONEY = r"\$(-?[\d,]+(?:\.\d+)?)"
_LONG_DATE = r"([A-Z][a-z]+ \d{1,2}, \d{4})"
_PERIOD_RE = re.compile(r"([A-Z][a-z]{2} \d{1,2}(?:, \d{4})?)\s*[–-]\s*([A-Z][a-z]{2} \d{1,2}, \d{4})")
_STRIPE_HEADER_RE = re.compile(r"Receipt from (?P<merchant>.+?) " + _MONEY + r" Paid " + _LONG_DATE)
_RECEIPT_NUMBER_RE = re.compile(r"Receipt number ([A-Za-z0-9-]+)")
_AMOUNT_PAID_RE = re.compile(r"Amount paid " + _MONEY)
_ITEM_RE = re.compile(r"(?P<desc>[^$§]+?) Qty (?P<qty>[\d,]+) \$(?P<amount>-?[\d,]+\.\d\d)(?: \$[\d,]+\.\d\d each)?")


@dataclass
class Email:
    email_id: str
    account_id: str
    from_addr: str
    subject: str
    received: datetime | None
    text: str


@dataclass
class ScanResult:
    added: list[dict[str, Any]] = field(default_factory=list)
    needs_look: list[dict[str, Any]] = field(default_factory=list)
    corroborated: list[dict[str, Any]] = field(default_factory=list)
    already_recorded: int = 0
    scanned: int = 0
    missing_rebills: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "added": self.added, "needs_look": self.needs_look, "corroborated": self.corroborated,
            "already_recorded": self.already_recorded, "scanned": self.scanned,
            "missing_rebills": self.missing_rebills,
        }


def _decimal(text: str) -> Decimal:
    return Decimal(text.replace(",", ""))


def _money_text(value: Decimal) -> str:
    return str(value.quantize(Decimal("0.01")))


def _parse_long_date(text: str) -> date:
    return datetime.strptime(text, "%B %d, %Y").date()


def _parse_short_date(text: str, year: int | None = None) -> date:
    if "," in text:
        return datetime.strptime(text, "%b %d, %Y").date()
    return datetime.strptime(f"{text}, {year}", "%b %d, %Y").date()


def clean_body(raw: str, *, html: bool) -> str:
    if html:
        raw = re.sub(r"<(style|script)[^>]*?>.*?</\1>", " ", raw, flags=re.S | re.I)
        raw = re.sub(r"<[^>]+>", " ", raw)
        for entity, char in (("&nbsp;", " "), ("&amp;", "&"), ("&#39;", "'"), ("&quot;", '"'), ("&ndash;", "–")):
            raw = raw.replace(entity, char)
    raw = re.sub(r"\(\s*https?://[^)]*\)", " ", raw)
    raw = re.sub(r"https?://\S+", " ", raw)
    return " ".join(raw.split())


def read_emails(db: str, root: str, since: datetime) -> list[Email]:
    """Receipt-shaped emails received since ``since`` (read-only)."""
    connection = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            "select id, account_id, from_addr, subject, received_at, text_blob_path, html_blob_path,"
            " normalized_summary from email_messages where lower(coalesce(subject, '')) like '%receipt%'"
        ).fetchall()
    finally:
        connection.close()
    emails: list[Email] = []
    for row in rows:
        try:
            received = parsedate_to_datetime(row["received_at"]) if row["received_at"] else None
        except (TypeError, ValueError):
            received = None
        if received is not None and received.tzinfo is None:
            received = received.replace(tzinfo=timezone.utc)  # "-0000": UTC, zone unstated
        if received is None or received < since:
            continue
        text = ""
        for key, html in (("text_blob_path", False), ("html_blob_path", True)):
            path = row[key]
            if not path:
                continue
            full = Path(path) if path.startswith("/") else Path(root) / path
            try:
                text = clean_body(full.read_text(encoding="utf-8", errors="replace"), html=html)
            except OSError:
                continue
            if text:
                break
        emails.append(Email(
            email_id=row["id"], account_id=row["account_id"] or "", from_addr=row["from_addr"] or "",
            subject=row["subject"] or "", received=received, text=text or (row["normalized_summary"] or ""),
        ))
    emails.sort(key=lambda email: email.received or datetime.min.replace(tzinfo=timezone.utc))
    return emails


# --- parsing -----------------------------------------------------------------

@dataclass
class Receipt:
    merchant: str
    receipt_id: str
    paid_on: date
    amount_paid: Decimal
    items: list[tuple[str, Decimal, tuple[date, date] | None]]


def parse_stripe_receipt(text: str) -> Receipt | None:
    """A Stripe-hosted receipt (Anthropic, Moonshot/Novascent, OpenCode,
    OpenRouter, ElevenLabs): header, receipt number, line items, amount paid."""
    header = _STRIPE_HEADER_RE.search(text)
    number = _RECEIPT_NUMBER_RE.search(text)
    paid = _AMOUNT_PAID_RE.search(text)
    if not (header and number and paid):
        return None
    receipt_id = number.group(1)
    paid_on = _parse_long_date(header.group(3))
    start = text.find(f"Receipt #{receipt_id}")
    if start < 0:
        return None
    block = text[start + len(f"Receipt #{receipt_id}"):]
    end = re.search(r" (?:Subtotal|Total) \$", block)
    block = block[: end.start()] if end else block
    periods: list[tuple[int, tuple[date, date]]] = []

    def mark(match: re.Match[str]) -> str:
        end_date = _parse_short_date(match.group(2))
        start_date = _parse_short_date(match.group(1), end_date.year)
        if start_date > end_date:  # "Dec 5–Jan 5, 2027"
            start_date = start_date.replace(year=end_date.year - 1)
        periods.append((len(periods), (start_date, end_date)))
        return f" §{len(periods) - 1}§ "

    marked = _PERIOD_RE.sub(mark, block)
    items: list[tuple[str, Decimal, tuple[date, date] | None]] = []
    for match in _ITEM_RE.finditer(marked):
        before = marked[: match.start("desc")]
        markers = re.findall(r"§(\d+)§", before)
        period = periods[int(markers[-1])][1] if markers else None
        items.append((" ".join(match.group("desc").split()), _decimal(match.group("amount")), period))
    return Receipt(header.group("merchant").strip(), receipt_id, paid_on, _decimal(paid.group(1)), items)


def parse_zai_receipt(text: str) -> Receipt | None:
    header = re.search(r"Receipt from zai " + _MONEY + r" Paid " + _LONG_DATE, text)
    number = re.search(r"Receipt number (RCPT-[\w-]+)", text)
    paid = _AMOUNT_PAID_RE.search(text)
    if not (header and number and paid):
        return None
    items: list[tuple[str, Decimal, tuple[date, date] | None]] = [
        (f"Subscription ({m.group(1)})", _decimal(m.group(2)), None)
        for m in re.finditer(r"Subscription \(([^)]+)\) " + _MONEY, text)
    ]
    return Receipt("zai", number.group(1), _parse_long_date(header.group(2)), _decimal(paid.group(1)), items)


def parse_google_play_receipt(text: str) -> Receipt | None:
    number = re.search(r"Order number: (GPA\.[\w.-]+)", text)
    ordered = re.search(r"Order date: ([A-Z][a-z]{2} \d{1,2}, \d{4})", text)
    total = re.search(r"Total: " + _MONEY + r"(?:/(year|month))?", text)
    if not (number and ordered and total):
        return None
    paid_on = datetime.strptime(ordered.group(1), "%b %d, %Y").date()
    items = []
    for match in re.finditer(r"Item Price (?P<item>.+?) " + _MONEY + r"/(year|month)", text):
        per = match.group(3)
        end = paid_on.replace(year=paid_on.year + 1) if per == "year" else _add_month(paid_on)
        items.append((" ".join(match.group("item").split()), _decimal(match.group(2)), (paid_on, end)))
    return Receipt("Google Play", number.group(1), paid_on, _decimal(total.group(1)), items)


def _add_month(day: date) -> date:
    year, month = (day.year + 1, 1) if day.month == 12 else (day.year, day.month + 1)
    for candidate in (day.day, 30, 29, 28):
        try:
            return day.replace(year=year, month=month, day=candidate)
        except ValueError:
            continue
    raise ValueError(day)


# --- vendor rules ------------------------------------------------------------

def _classify(email: Email) -> tuple[str, Receipt | None, list[dict[str, Any]] | None, str | None]:
    """(vendor, receipt, entries, problem). ``vendor`` is empty when the email
    is not from a vendor in the file; ``entries`` is None when it needs a look."""
    sender = email.from_addr.lower()
    subject = email.subject
    if "mail.anthropic.com" in sender:
        subscription = {"personal-gmail": "claude-1", "yugen-harborlight": "claude-2"}.get(email.account_id)
        if subscription is None:
            return "anthropic", None, None, f"Anthropic receipt in mailbox {email.account_id}, which maps to no Claude account"
        return "anthropic", *_stripe_entries(email, subscription, _anthropic_item)
    if "moonshot" in sender or "novascent" in sender:
        return "kimi", *_stripe_entries(email, "kimi", _named_plan("kimi", "Kimi membership"))
    if "anoma.ly" in sender:
        return "opencode-go", *_stripe_entries(email, "opencode-go", _named_plan("opencode go", "OpenCode Go"))
    if "openrouter" in sender:
        return "openrouter", *_stripe_entries(email, "openrouter", _openrouter_item)
    if "eleven labs" in sender or "elevenlabs" in sender:
        return "elevenlabs", *_stripe_entries(email, "elevenlabs", _elevenlabs_item)
    # Keeper msg 1553067845766742228 ("yeah, those should go in too").
    if "brave software" in sender:
        return "brave", *_stripe_entries(email, "brave", _brave_item)
    if "invoice+statements@suno.com" in sender:
        return "suno", *_stripe_entries(email, "suno", _suno_item)
    if "invoice+statements@venice.ai" in sender:
        return "venice", *_stripe_entries(email, "venice", _venice_item)
    if "account.z.ai" in sender:
        receipt = parse_zai_receipt(email.text)
        if receipt is None:
            return "z-ai", None, None, "Z.ai receipt did not parse"
        plans = [desc for desc, amount, _ in receipt.items if amount > 0]
        if len(plans) != 1 or not plans[0].startswith("Subscription (GLM Coding"):
            return "z-ai", receipt, None, f"Z.ai line items not recognised: {plans}"
        plan = plans[0][len("Subscription ("):-1]
        return "z-ai", receipt, [_entry(email, receipt, "z-ai", "plan", plan, None)], None
    if subject.startswith("Your zai receipt [#"):
        return "", None, [], None  # Stripe's copy of the Z.ai RCPT receipt: same payment
    if "googleplay-noreply@google.com" in sender and "order receipt" in subject.lower():
        receipt = parse_google_play_receipt(email.text)
        if receipt is None or not any("pocket" in desc.lower() for desc, _, _ in receipt.items):
            return "", None, [], None  # another Play purchase
        if len(receipt.items) != 1:
            return "pocket", receipt, None, "Google Play receipt with Pocket and other items"
        desc, _amount, period = receipt.items[0]
        plan = re.sub(r"\s*\(Pocket - AI Personal Assistant\)", "", desc) or desc
        return "pocket", receipt, [_entry(email, receipt, "pocket", "plan", plan, period)], None
    if "openai" in sender or "deepseek" in sender:
        vendor = "codex" if "openai" in sender else "deepseek"
        return vendor, None, None, "no parsing rule for this vendor's receipts yet"
    return "", None, [], None


def _stripe_entries(email: Email, subscription: str, rule) -> tuple[Receipt | None, list[dict[str, Any]] | None, str | None]:
    receipt = parse_stripe_receipt(email.text)
    if receipt is None:
        return None, None, "receipt did not parse"
    charged = [(desc, amount, period) for desc, amount, period in receipt.items if amount > 0]
    kinds = [rule(desc) for desc, _, _ in charged]
    if len(charged) != 1 or kinds[0] is None:
        return receipt, None, f"line items not recognised: {[desc for desc, _, _ in charged]}"
    desc, _amount, period = charged[0]
    kind, plan = kinds[0]
    entry = _entry(email, receipt, subscription, kind, plan, None if subscription in _BILLED_IN_ARREARS else period)
    if subscription in _BILLED_IN_ARREARS and period:
        # The receipt's period is the usage it bills, not time paid ahead:
        # recorded in the evidence, so the next bill is expected a month on.
        quantity = re.search(re.escape(desc) + r" Qty ([\d,]+)", email.text)
        entry["evidence"] += (f"; usage {period[0].isoformat()} to {period[1].isoformat()}"
                              + (f", {quantity.group(1)} requests" if quantity else ""))
    return receipt, [entry], None


def _anthropic_item(desc: str) -> tuple[str, str] | None:
    lower = desc.lower()
    if lower.startswith("max plan"):
        tier = re.search(r"(\d+x)", desc)
        return "plan", f"Claude Max {tier.group(1)}" if tier else "Claude Max"
    if "extra usage" in lower:
        return "overage", "Auto-recharge extra usage" if "auto recharge" in lower else "Prepaid extra usage"
    return None


def _named_plan(item: str, label: str):
    """The line item must be exactly ``item``: anything else (an upgrade, an
    annual plan, a credit) needs a look rather than passing as the plan."""
    def rule(desc: str) -> tuple[str, str] | None:
        return ("plan", label) if desc.lower() == item else None
    return rule


def _openrouter_item(desc: str) -> tuple[str, str] | None:
    return ("topup", "Credits") if "credits" in desc.lower() else None


# Subscriptions billed monthly in arrears for metered use.
_BILLED_IN_ARREARS = frozenset({"brave"})


def _brave_item(desc: str) -> tuple[str, str] | None:
    """Brave Search API bills requests monthly in arrears: "Pro (per request)
    Qty 6,363 $31.82 $0.005 each". The bill is the plan charge."""
    return ("plan", f"Brave Search API {desc}") if desc.endswith("(per request)") else None


def _suno_item(desc: str) -> tuple[str, str] | None:
    return ("plan", desc) if desc.lower() in {"suno pro", "suno premier"} else None


def _venice_item(desc: str) -> tuple[str, str] | None:
    return ("topup", "Venice credits") if desc.lower() == "venice credits" else None


def _elevenlabs_item(desc: str) -> tuple[str, str] | None:
    match = re.match(r"(.+?) \(per subscription\)", desc)
    return ("plan", f"ElevenLabs {match.group(1)}") if match else None


def _entry(email: Email, receipt: Receipt, subscription: str, kind: str, plan: str,
           period: tuple[date, date] | None) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "receipt_id": receipt.receipt_id, "subscription": subscription, "kind": kind, "plan": plan,
        "charged_at": receipt.paid_on.isoformat(), "amount": _money_text(receipt.amount_paid), "currency": "USD",
    }
    if period:
        entry["period_start"], entry["period_end"] = period[0].isoformat(), period[1].isoformat()
    entry["confidence"] = "receipt"
    entry["evidence"] = f"{email.account_id} {email.email_id} (receipts-scan)"
    return entry


# --- scan --------------------------------------------------------------------

def _day(text: Any) -> date | None:
    try:
        return date.fromisoformat(str(text)[:10])
    except ValueError:
        return None


def scan(emails: Iterable[Email], manifest_lines: Sequence[Mapping[str, Any]], *, today: date) -> ScanResult:
    result = ScanResult()
    known_ids = {str(entry.get("receipt_id")) for entry in manifest_lines if entry.get("receipt_id")}
    recorded: list[Mapping[str, Any]] = [entry for entry in manifest_lines if entry.get("kind") != STATED_RATE]
    for email in emails:
        vendor, receipt, entries, problem = _classify(email)
        if not vendor:
            continue
        result.scanned += 1
        if receipt is not None and receipt.receipt_id in known_ids:
            result.already_recorded += 1
            continue
        where = {"vendor": vendor, "email": email.email_id, "account": email.account_id,
                 "received": email.received.date().isoformat() if email.received else None,
                 "subject": email.subject[:90]}
        if entries is None:
            result.needs_look.append({**where, "problem": problem,
                                      "amount": _money_text(receipt.amount_paid) if receipt else None})
            continue
        for entry in entries:
            if entry["kind"] == "plan":
                paid = _day(entry["charged_at"])
                same_payment = [
                    line for line in recorded
                    if line.get("subscription") == entry["subscription"] and line.get("kind") == "plan"
                    and not line.get("receipt_id") and str(line.get("amount")) == entry["amount"]
                    and (d := _day(line.get("charged_at"))) is not None and paid is not None
                    and abs((d - paid).days) <= 2
                ]
                if same_payment:
                    result.corroborated.append({**where, "receipt_id": entry["receipt_id"],
                                                "matches": same_payment[0].get("charged_at")})
                    known_ids.add(entry["receipt_id"])
                    continue
                previous = [
                    d for line in recorded
                    if line.get("subscription") == entry["subscription"] and line.get("kind") == "plan"
                    and (d := _day(line.get("charged_at"))) is not None and paid is not None
                    and timedelta(0) <= paid - d < timedelta(days=25)
                ]
                if previous:
                    result.needs_look.append({**where, "amount": entry["amount"], "problem":
                                              f"second {entry['subscription']} plan charge within 25 days of "
                                              f"{max(previous).isoformat()} (upgrade or proration?)"})
                    continue
            result.added.append(entry)
            recorded.append(entry)
            known_ids.add(entry["receipt_id"])
    result.missing_rebills = missing_rebills(recorded, today=today)
    return result


def missing_rebills(entries: Iterable[Mapping[str, Any]], *, today: date,
                    grace_days: int = 5, window_days: int = 40) -> list[dict[str, Any]]:
    """Plans whose paid period ended (or whose monthly charge is a month old)
    more than ``grace_days`` ago with no later plan charge, reported for
    ``window_days`` so a monthly run names each missed rebill about once."""
    latest: dict[str, Mapping[str, Any]] = {}
    for entry in entries:
        if entry.get("kind") != "plan":
            continue
        key = str(entry.get("subscription"))
        if key not in latest or str(entry.get("charged_at")) > str(latest[key].get("charged_at")):
            latest[key] = entry
    missing = []
    for key, entry in sorted(latest.items()):
        charged = _day(entry.get("charged_at"))
        period_end = _day(entry.get("period_end")) if entry.get("period_end") else None
        if period_end is not None and (charged is None or period_end > charged):
            expected = period_end
        else:
            # No paid-through date, or a bill for a period already over
            # (billed in arrears): the next bill comes a month after this one.
            expected = _add_month(charged) if charged else None
        if expected is None:
            continue
        overdue = (today - expected).days
        if grace_days < overdue <= window_days:
            missing.append({"subscription": key, "label": SUBSCRIPTIONS.get(key, (None, None, key))[2],
                            "last_charge": entry.get("charged_at"), "expected": expected.isoformat()})
    return missing


def read_manifest_entries(path: str | Path) -> list[dict[str, Any]]:
    return [entry for _number, entry in manifest_entries(path)]


def append_entries(path: str | Path, entries: Iterable[Mapping[str, Any]]) -> int:
    lines = [json.dumps(dict(entry), ensure_ascii=False) for entry in entries]
    if not lines:
        return 0
    target = Path(path)
    existing = target.read_text(encoding="utf-8")
    separator = "" if existing.endswith("\n") or not existing else "\n"
    with target.open("a", encoding="utf-8") as handle:
        handle.write(separator + "\n".join(lines) + "\n")
    return len(lines)
