"""Subscription fees as billing facts (keeper, 2026-09-25, Discord unified usage
ledger thread msg 1553037883747598450: "we should record what I pay each month
per subscription. The monthly fees should be in my emails")."""
from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from codex_usage_tracker.canonical_ledger import append_sqlite_facts
from codex_usage_tracker.dashboard import create_app
from codex_usage_tracker.subscription_fees import (
    ManifestError,
    fact_from_entry,
    parse_description_code,
    read_manifest,
    read_stated_rates,
    stated_rate_from_entry,
    summarize,
)

TODAY = datetime.now(timezone.utc).date()


def _day(offset: int) -> str:
    return (TODAY + timedelta(days=offset)).isoformat()


def _manifest(tmp_path: Path) -> Path:
    entries = [
        {"receipt_id": "2704-9084-8390", "subscription": "claude-1", "kind": "plan", "plan": "Claude Max 20x",
         "charged_at": _day(-52), "amount": "200.00", "currency": "USD",
         "period_start": _day(-52), "period_end": _day(-22)},
        {"receipt_id": "2707-1111-2222", "subscription": "claude-1", "kind": "plan", "plan": "Claude Max 20x",
         "charged_at": _day(-22), "amount": "200.00", "currency": "USD",
         "period_start": _day(-22), "period_end": _day(8)},
        {"receipt_id": "2046-9705-0000", "subscription": "claude-1", "kind": "overage", "plan": "Extra usage",
         "charged_at": _day(-3), "amount": "28.23", "currency": "USD"},
        {"receipt_id": "2345-1754", "subscription": "kimi", "kind": "plan", "plan": "Allegro",
         "charged_at": _day(-40), "amount": "99.00", "currency": "USD",
         "period_start": _day(-40), "period_end": _day(-10)},
        {"receipt_id": "2289-2633", "subscription": "opencode-go", "kind": "plan", "plan": "OpenCode Go",
         "charged_at": _day(-5), "amount": "10.00", "currency": "USD"},
        {"receipt_id": "or-1", "subscription": "openrouter", "kind": "topup", "plan": "Credits",
         "charged_at": _day(-2), "amount": "50.00", "currency": "USD"},
        {"receipt_id": "rf-1", "subscription": "codex", "kind": "refund", "plan": "ChatGPT Pro",
         "charged_at": _day(-1), "amount": "15.00", "currency": "USD"},
    ]
    path = tmp_path / "receipts.jsonl"
    path.write_text("// comment lines are allowed\n" + "\n".join(json.dumps(entry) for entry in entries) + "\n")
    return path


def test_entries_become_billing_facts_with_a_parseable_code(tmp_path):
    facts = read_manifest(_manifest(tmp_path))
    assert len(facts) == 7
    first = facts[0]
    assert first["provider"] == "anthropic" and first["transaction_kind"] == "charge" and first["status"] == "posted"
    assert first["source_billing_fact_id"] == "claude-1:2704-9084-8390"
    assert parse_description_code(first["description_code"]) == ("claude-1", "plan", "Claude Max 20x")
    refund = facts[-1]
    assert refund["transaction_kind"] == "refund" and refund["amount"].startswith("-")


@pytest.mark.parametrize("entry, fragment", [
    ({"subscription": "netflix", "kind": "plan", "charged_at": "2026-09-01", "amount": "1"}, "unknown subscription"),
    ({"subscription": "codex", "kind": "rate", "charged_at": "2026-09-01", "amount": "1"}, "unknown kind"),
    ({"subscription": "codex", "kind": "plan", "charged_at": "2026-09-01", "amount": "-1"}, "only a refund"),
    ({"subscription": "codex", "kind": "plan", "charged_at": "2026-09-01T10:00:00", "amount": "1"}, "no UTC offset"),
    ({"subscription": "codex", "kind": "plan", "amount": "1"}, "charged_at is missing"),
])
def test_bad_entries_are_rejected_with_a_reason(entry, fragment):
    with pytest.raises(ManifestError) as excinfo:
        fact_from_entry(entry)
    assert fragment in str(excinfo.value)


def test_import_is_idempotent_and_the_api_summarizes_per_subscription(tmp_path):
    manifest = _manifest(tmp_path)
    ledger = tmp_path / "billing.sqlite3"
    first = append_sqlite_facts(ledger, read_manifest(manifest), fact_type="billing_fact_v1")
    again = append_sqlite_facts(ledger, read_manifest(manifest), fact_type="billing_fact_v1")
    assert (first.appended, again.appended, again.replayed) == (7, 0, 7)

    client = create_app(billing_ledger=str(ledger)).test_client()
    data = client.get("/api/subscription-fees").get_json()
    subs = {row["subscription"]: row for row in data["subscriptions"]}
    assert set(subs) == {"claude-1", "kimi", "opencode-go"}
    claude = subs["claude-1"]
    # The latest plan charge wins; weekly = 200 x 7 / 30 days.
    assert claude["charged_at"].startswith(_day(-22)) and claude["period_days"] == 30.0
    # A monthly period: the charge is the monthly fee, weekly is x 12 / 52.
    assert claude["fee_per_month"] == "200.00" and claude["fee_per_week"] == "46.15"
    assert claude["period_current"] is True
    assert subs["kimi"]["period_current"] is False
    # No period on the receipt: monthly by assumption (x 12 / 52).
    assert subs["opencode-go"]["fee_per_week"] == "2.31" and subs["opencode-go"]["period_current"] is None
    kinds = {(row["subscription"], row["kind"]) for row in data["other_charges"]}
    assert kinds == {("claude-1", "overage"), ("openrouter", "topup"), ("codex", "refund")}
    assert client.get("/api/subscription-fees?days=0").status_code == 400
    assert create_app().test_client().get("/api/subscription-fees").status_code == 404
    # The existing billing panel sees the same facts.
    assert len(client.get("/api/billing?days=90").get_json()["transactions"]) == 7


def test_a_non_monthly_period_is_scaled_by_its_own_length():
    now = datetime.now(timezone.utc)
    annual = fact_from_entry({"subscription": "codex", "kind": "plan", "plan": "Annual", "charged_at": "2026-01-01",
                              "amount": "520.00", "period_start": "2026-01-01", "period_end": "2027-01-01"})
    [row] = summarize([annual], now=now)["subscriptions"]
    assert row["fee_per_week"] == "9.97" and row["fee_per_month"] == "43.21"  # 520 x 7 / 365, x 52 / 12


def test_rows_say_why_they_have_no_list_price():
    """Keeper msg 1553067845766742228: Suno, Venice.ai and Brave join the file.
    Brave's usage is tracked (search ledger) but its bill is already per request."""
    now = datetime.now(timezone.utc)
    facts = [
        fact_from_entry({"subscription": key, "kind": "plan", "plan": plan, "charged_at": "2026-09-01", "amount": amount})
        for key, plan, amount in (("brave", "Brave Search API Pro (per request)", "31.82"), ("suno", "Suno Premier", "256.00"),
                                  ("claude-1", "Claude Max 20x", "200.00"))
    ]
    rows = {row["subscription"]: row for row in summarize(facts, now=now)["subscriptions"]}
    assert (rows["brave"]["usage_tracked"], rows["brave"]["list_price_note"], rows["brave"]["label"]) == (True, "billed per request", "Brave Search")
    assert (rows["suno"]["usage_tracked"], rows["suno"]["list_price_note"]) == (False, "not in usage ledger")
    assert (rows["claude-1"]["usage_tracked"], rows["claude-1"]["list_price_note"]) == (True, None)


def test_summarize_ignores_rows_without_a_manifest_code():
    now = datetime.now(timezone.utc)
    assert summarize([{"description_code": "INV-42", "occurred_at": "2026-09-01T00:00:00Z", "amount": "1"}], now=now) == {
        "subscriptions": [], "other_charges": [],
    }


def test_cli_billing_import_dry_run_then_write(tmp_path):
    manifest = _manifest(tmp_path)
    ledger = tmp_path / "billing.sqlite3"
    append_sqlite_facts(ledger, [], fact_type="billing_fact_v1")
    base = [sys.executable, "-m", "codex_usage_tracker.cli", "billing-import", "--manifest", str(manifest),
            "--billing-ledger", str(ledger)]
    dry = subprocess.run([*base, "--dry-run"], capture_output=True, text=True, check=True)
    assert json.loads(dry.stdout)["appended"] == 7
    wrote = subprocess.run(base, capture_output=True, text=True, check=True)
    assert json.loads(wrote.stdout)["appended"] == 7
    bad = tmp_path / "bad.jsonl"
    bad.write_text('{"subscription": "codex"}\n')
    failed = subprocess.run([*base[:5], str(bad), "--billing-ledger", str(ledger)], capture_output=True, text=True)
    assert failed.returncode == 2 and "line 1" in failed.stderr


# Keeper, 2026-09-25, msg 1553054862856556656: "chatgpt pro is $200/month, it's
# on another card that is my mother's". A stated rate is his figure for a fee
# whose charges are not in his mail: never a billing fact, still the card's fee.
STATED_CODEX = {"subscription": "codex", "kind": "stated_rate", "plan": "ChatGPT Pro", "amount": "200.00",
                "currency": "USD", "per": "month", "stated_at": _day(0), "evidence": "keeper msg"}


def test_a_stated_rate_is_the_fee_but_never_a_billing_fact(tmp_path, monkeypatch):
    manifest = tmp_path / "receipts.jsonl"
    kimi = {"receipt_id": "2345-1754", "subscription": "kimi", "kind": "plan", "plan": "Allegro",
            "charged_at": _day(-20), "amount": "99.00", "currency": "USD"}
    manifest.write_text("\n".join(json.dumps(entry) for entry in (kimi, STATED_CODEX)) + "\n")
    facts = read_manifest(manifest)
    assert [fact["provider"] for fact in facts] == ["kimi-k3-coding"]
    assert [rate["subscription"] for rate in read_stated_rates(manifest)] == ["codex"]

    ledger = tmp_path / "billing.sqlite3"
    append_sqlite_facts(ledger, facts, fact_type="billing_fact_v1")
    monkeypatch.setenv("AI_SUBSCRIPTION_RECEIPTS_MANIFEST", str(manifest))
    client = create_app(billing_ledger=str(ledger)).test_client()
    data = client.get("/api/subscription-fees").get_json()
    assert data["stated_rates_read"] == "ok"
    subs = {row["subscription"]: row for row in data["subscriptions"]}
    codex = subs["codex"]
    assert (codex["fee_source"], codex["fee_per_month"], codex["fee_per_week"]) == ("stated", "200.00", "46.15")
    assert codex["charged_at"] is None and codex["period_current"] is None and codex["plan"] == "ChatGPT Pro"
    assert subs["kimi"]["fee_source"] == "receipt"
    # The billing ledger and its totals hold only the posted charge.
    billing = client.get("/api/billing?days=90").get_json()
    assert len(billing["transactions"]) == 1 and Decimal(billing["totals_by_currency"][0]["amount"]) == Decimal("99")


def test_the_newer_of_a_stated_rate_and_a_receipt_wins():
    now = datetime.now(timezone.utc)
    receipt = fact_from_entry({"subscription": "codex", "kind": "plan", "plan": "ChatGPT Pro", "charged_at": _day(-3),
                               "amount": "200.00"})
    older = stated_rate_from_entry({**STATED_CODEX, "amount": "20.00", "stated_at": _day(-10)})
    newer = stated_rate_from_entry({**STATED_CODEX, "amount": "250.00", "stated_at": _day(0)})
    [row] = summarize([receipt], now=now, stated_rates=[older])["subscriptions"]
    assert (row["fee_source"], row["amount"]) == ("receipt", "200.00")
    [row] = summarize([receipt], now=now, stated_rates=[older, newer])["subscriptions"]
    assert (row["fee_source"], row["amount"]) == ("stated", "250.00")


@pytest.mark.parametrize("change, fragment", [
    ({"subscription": "netflix"}, "unknown subscription"),
    ({"per": "week"}, "per: month"),
    ({"amount": "-5"}, "cannot be negative"),
    ({"stated_at": None}, "stated_at is missing"),
])
def test_bad_stated_rates_are_rejected(change, fragment):
    with pytest.raises(ManifestError) as excinfo:
        stated_rate_from_entry({**STATED_CODEX, **change})
    assert fragment in str(excinfo.value)


def test_no_manifest_means_no_stated_rates(tmp_path, monkeypatch):
    monkeypatch.delenv("AI_SUBSCRIPTION_RECEIPTS_MANIFEST", raising=False)
    ledger = tmp_path / "billing.sqlite3"
    append_sqlite_facts(ledger, read_manifest(_manifest(tmp_path)), fact_type="billing_fact_v1")
    data = create_app(atrium_root=str(tmp_path), billing_ledger=str(ledger)).test_client().get(
        "/api/subscription-fees").get_json()
    assert data["stated_rates_read"] == "no manifest"
    assert all(row["fee_source"] == "receipt" for row in data["subscriptions"])
