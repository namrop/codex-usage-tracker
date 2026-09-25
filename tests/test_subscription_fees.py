"""Subscription fees as billing facts (keeper, 2026-09-25, Discord unified usage
ledger thread msg 1553037883747598450: "we should record what I pay each month
per subscription. The monthly fees should be in my emails")."""
from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from codex_usage_tracker.canonical_ledger import append_sqlite_facts
from codex_usage_tracker.dashboard import create_app
from codex_usage_tracker.subscription_fees import (
    ManifestError,
    fact_from_entry,
    parse_description_code,
    read_manifest,
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
