"""Receipt scan for the monthly job (keeper, 2026-09-25, msg 1553058626350874726:
"pocket and elevenlabs should go in the reciepts file. we should also have the
monthly job"). Bodies below keep the shape of the real receipt emails in the
mailroom store, with amounts and numbers changed."""
from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from datetime import date, datetime, timezone
from pathlib import Path

from codex_usage_tracker.receipt_scan import (
    Email,
    clean_body,
    missing_rebills,
    parse_google_play_receipt,
    parse_stripe_receipt,
    parse_zai_receipt,
    read_emails,
    scan,
)
from codex_usage_tracker.subscription_fees import read_manifest

ANTHROPIC_PLAN = """Anthropic, PBC (https://anthropic.com)
Anthropic, PBC
Receipt from Anthropic, PBC $200.00 Paid September 3, 2026 (invoice illustration [https://x] Download invoice (https://x) Download receipt (https://x) Receipt number 2111-2222-3333 Invoice number AAAA-0015 Payment method - 6701
Receipt #2111-2222-3333 Sep 3–Oct 3, 2026 Max plan - 20x Qty 1 $200.00 Total $200.00 Amount paid $200.00 Questions? Visit our support site (https://x)
"""
ANTHROPIC_RECHARGE = """Anthropic, PBC
Receipt from Anthropic, PBC $28.23 Paid September 3, 2026 Receipt number 2444-5555-6666 Invoice number AAAA-0016 Payment method - 6701
Receipt #2444-5555-6666 Auto recharge extra usage, Individual plan Qty 1 $28.23 Total $28.23 Amount paid $28.23
"""
ELEVENLABS = """Eleven Labs Inc.
Receipt from Eleven Labs Inc. $6.00 Paid September 25, 2026 Receipt number 2077-8888-9999 Invoice number R-0006 Payment method - 4773
Receipt #2077-8888-9999 Aug 25–Sep 25, 2026 PAYG Credit Usage Qty 0 $0.00 Rollover Credit Usage Qty 1,340 $0.00 $0.00 each Seat Credit Usage Qty 0 $0.00 Sep 25–Oct 25, 2026 Starter (per subscription) Qty 1 $6.00 Total $6.00 Amount paid $6.00 Questions?
"""
OPENCODE = """OpenCode
Receipt from OpenCode $5.00 Paid July 8, 2026 Receipt number 2289-0000 Invoice number S-0001 Payment method Link
Receipt #2289-0000 Jul 8–Aug 8, 2026 OpenCode Go Qty 1 $10.00 Subtotal $10.00 First month 50% off (50% off) -$5.00 Total $5.00 Amount paid $5.00
"""
KIMI_UNKNOWN_ITEM = """NOVASCENT PRIVATE LIMITED
Receipt from NOVASCENT PRIVATE LIMITED $199.00 Paid October 2, 2026 Receipt number 2161-0001 Invoice number X-0002 Payment method - 2591
Receipt #2161-0001 Oct 2–Nov 2, 2026 Kimi Pro Annual Upgrade Credit Qty 1 $199.00 Total $199.00 Amount paid $199.00
"""
ZAI_HTML = """<html><body><div>Z.ai Receipt</div><p>Receipt from zai $144 Paid August 24, 2026</p><p>Receipt number RCPT-6258308-202608-0001</p>
<p>Invoice number INV-6258308-202608-0001</p><table><tr><td>Subscription (GLM Coding Max)</td><td>$144</td><td>Qty 1</td></tr></table>
<p>Total $144</p><p>Amount paid $144</p></body></html>"""
POCKET = """Google Play
Thank you
Your trial of Pocket - AI Personal Assistant has ended. Your subscription
continues at the price of $199.99/year. Manage your subscriptions
Order number: GPA.3368-0000-0000-00000..0
Order date: Sep 14, 2026 10:55:01 PM EDT
Item Price
Pocket Pro (Pocket - AI Personal Assistant) $199.99/year
Auto-renewing subscription
Tax: $0.00
Total: $199.99/year
"""
OTHER_PLAY = POCKET.replace("Pocket Pro (Pocket - AI Personal Assistant)", "YouTube Premium").replace("Pocket - AI Personal Assistant", "YouTube")


def _email(text: str, sender: str, subject: str, account: str = "personal-gmail", html: bool = False, eid: str = "e") -> Email:
    return Email(eid, account, sender, subject, datetime(2026, 9, 25, tzinfo=timezone.utc), clean_body(text, html=html))


def test_stripe_receipts_parse_with_the_period_of_each_line():
    plan = parse_stripe_receipt(clean_body(ANTHROPIC_PLAN, html=False))
    assert (plan.receipt_id, plan.paid_on, str(plan.amount_paid)) == ("2111-2222-3333", date(2026, 9, 3), "200.00")
    assert plan.items == [("Max plan - 20x", plan.amount_paid, (date(2026, 9, 3), date(2026, 10, 3)))]
    eleven = parse_stripe_receipt(clean_body(ELEVENLABS, html=False))
    charged = [item for item in eleven.items if item[1] > 0]
    assert charged == [("Starter (per subscription)", charged[0][1], (date(2026, 9, 25), date(2026, 10, 25)))]
    assert len(eleven.items) == 4
    opencode = parse_stripe_receipt(clean_body(OPENCODE, html=False))
    assert str(opencode.amount_paid) == "5.00" and opencode.items[0][0] == "OpenCode Go"


def test_zai_and_google_play_receipts_parse():
    zai = parse_zai_receipt(clean_body(ZAI_HTML, html=True))
    assert (zai.receipt_id, zai.paid_on, str(zai.amount_paid)) == ("RCPT-6258308-202608-0001", date(2026, 8, 24), "144")
    assert zai.items[0][0] == "Subscription (GLM Coding Max)"
    play = parse_google_play_receipt(clean_body(POCKET, html=False))
    assert play.receipt_id == "GPA.3368-0000-0000-00000..0" and play.paid_on == date(2026, 9, 14)
    assert play.items[0][2] == (date(2026, 9, 14), date(2027, 9, 14))


def test_scan_adds_skips_corroborates_and_flags():
    manifest = [
        {"receipt_id": "2704-9084-8390", "subscription": "claude-1", "kind": "plan", "charged_at": "2026-08-03",
         "amount": "200.00", "period_end": "2026-09-03"},
        {"subscription": "claude-2", "kind": "plan", "plan": "Claude Max (card authorization)",
         "charged_at": "2026-09-05", "amount": "200.00"},
        {"receipt_id": "2345-1754", "subscription": "kimi", "kind": "plan", "charged_at": "2026-09-20", "amount": "99.00"},
        {"subscription": "codex", "kind": "stated_rate", "amount": "200.00", "per": "month", "stated_at": "2026-09-25"},
    ]
    claude2 = ANTHROPIC_PLAN.replace("2111-2222-3333", "2837-0752-5703").replace("September 3", "September 5").replace("Sep 3–Oct 3", "Sep 5–Oct 5")
    emails = [
        _email(ANTHROPIC_PLAN, '"Anthropic, PBC" <invoice+statements@mail.anthropic.com>', "Your receipt from Anthropic, PBC #2111-2222-3333", eid="a"),
        _email(ANTHROPIC_RECHARGE, '"Anthropic, PBC" <invoice+statements@mail.anthropic.com>', "Your receipt", eid="b"),
        _email(claude2, '"Anthropic, PBC" <invoice+statements@mail.anthropic.com>', "Your receipt", account="yugen-harborlight", eid="c"),
        _email(ELEVENLABS, '"Eleven Labs Inc." <invoice+statements+acct_1@stripe.com>', "Your receipt from Eleven Labs Inc.", account="yugen-harborlight", eid="d"),
        _email(ZAI_HTML, '"Z.ai" <notifications@account.z.ai>', "Your zai invoice & receipt", html=True, eid="e"),
        _email("Receipt from zai Receipt #1839-2526 Amount paid $144.00", "zai <receipts+acct_1@stripe.com>", "Your zai receipt [#1839-2526]", eid="f"),
        _email(POCKET, "Google Play <googleplay-noreply@google.com>", "Your Google Play Order Receipt from Sep 14, 2026", eid="g"),
        _email(OTHER_PLAY, "Google Play <googleplay-noreply@google.com>", "Your Google Play Order Receipt from Sep 11, 2026", eid="h"),
        _email(KIMI_UNKNOWN_ITEM, "NOVASCENT PRIVATE LIMITED <invoice+statements+acct_2@stripe.com>", "Your receipt from NOVASCENT", eid="i"),
        _email(ANTHROPIC_PLAN.replace("2111-2222-3333", "2704-9084-8390"), '"Anthropic, PBC" <invoice+statements@mail.anthropic.com>', "Your receipt", eid="j"),
        _email("Your receipt from OpenAI", "OpenAI <noreply@tm.openai.com>", "Your ChatGPT receipt", eid="k"),
    ]
    result = scan(emails, manifest, today=date(2026, 10, 10))
    added = {(entry["subscription"], entry["kind"], entry["plan"], entry["charged_at"], entry["amount"]) for entry in result.added}
    assert added == {
        ("claude-1", "plan", "Claude Max 20x", "2026-09-03", "200.00"),
        ("claude-1", "overage", "Auto-recharge extra usage", "2026-09-03", "28.23"),
        ("elevenlabs", "plan", "ElevenLabs Starter", "2026-09-25", "6.00"),
        ("z-ai", "plan", "GLM Coding Max", "2026-08-24", "144.00"),
        ("pocket", "plan", "Pocket Pro", "2026-09-14", "199.99"),
    }
    claude1 = next(entry for entry in result.added if entry["receipt_id"] == "2111-2222-3333")
    assert (claude1["period_start"], claude1["period_end"]) == ("2026-09-03", "2026-10-03")
    assert claude1["evidence"] == "personal-gmail a (receipts-scan)"
    # The Claude 2 receipt is the same payment as the recorded card authorization.
    assert [(row["receipt_id"], row["matches"]) for row in result.corroborated] == [("2837-0752-5703", "2026-09-05")]
    assert result.already_recorded == 1
    problems = {row["vendor"]: row["problem"] for row in result.needs_look}
    assert "line items not recognised" in problems["kimi"]
    assert "no parsing rule" in problems["codex"]
    # Every added line imports as a billing fact.
    path = Path(__file__).parent / "_scan_tmp.jsonl"
    try:
        path.write_text("\n".join(json.dumps(entry) for entry in result.added) + "\n")
        assert len(read_manifest(path)) == 5
    finally:
        path.unlink()


def test_a_second_plan_charge_inside_a_period_needs_a_look():
    manifest = [{"receipt_id": "RCPT-1", "subscription": "z-ai", "kind": "plan", "charged_at": "2026-08-20", "amount": "64.80"}]
    email = _email(ZAI_HTML, '"Z.ai" <notifications@account.z.ai>', "Your zai invoice", html=True)
    result = scan([email], manifest, today=date(2026, 9, 1))
    assert result.added == [] and "within 25 days" in result.needs_look[0]["problem"]


def test_missing_rebills_are_named_once_after_the_grace_period():
    entries = [
        {"subscription": "kimi", "kind": "plan", "charged_at": "2026-08-02", "period_end": "2026-09-02"},
        {"subscription": "claude-2", "kind": "plan", "charged_at": "2026-09-05"},
        {"subscription": "pocket", "kind": "plan", "charged_at": "2026-09-14", "period_end": "2027-09-14"},
        {"subscription": "opencode-go", "kind": "plan", "charged_at": "2026-06-08", "period_end": "2026-07-08"},
    ]
    missing = missing_rebills(entries, today=date(2026, 9, 25))
    assert [(row["label"], row["expected"]) for row in missing] == [("Kimi K3", "2026-09-02")]
    # Within the grace period nothing is named; after the window it drops off.
    assert missing_rebills(entries, today=date(2026, 9, 5)) == []
    assert [row["subscription"] for row in missing_rebills(entries, today=date(2026, 10, 13))] == ["claude-2"]


def _mailroom(tmp_path: Path) -> tuple[Path, Path]:
    root = tmp_path / "mailroom"
    (root / "blobs").mkdir(parents=True)
    db = root / "store.sqlite3"
    con = sqlite3.connect(db)
    con.execute("create table email_messages (id text, account_id text, from_addr text, subject text, received_at text,"
                " text_blob_path text, html_blob_path text, normalized_summary text, provider_message_id text)")
    rows = [
        ("email_1", "personal-gmail", '"Anthropic, PBC" <invoice+statements@mail.anthropic.com>',
         "Your receipt from Anthropic, PBC #2111-2222-3333", "Thu, 03 Sep 2026 18:13:55 +0000", ANTHROPIC_PLAN, None),
        ("email_2", "personal-gmail", '"Z.ai" <notifications@account.z.ai>', "Your zai invoice & receipt",
         "Mon, 24 Aug 2026 10:02:44 +0800", None, ZAI_HTML),
        ("email_3", "personal-gmail", '"Anthropic, PBC" <invoice+statements@mail.anthropic.com>',
         "Your receipt from Anthropic, PBC #2000-0000-0001", "Mon, 01 Jun 2026 18:13:55 +0000",
         ANTHROPIC_PLAN.replace("2111-2222-3333", "2000-0000-0001").replace("September 3", "June 1").replace("Sep 3–Oct 3", "Jun 1–Jul 1"), None),
        ("email_4", "personal-gmail", "Shop <x@shop.example>", "Newsletter", "Thu, 03 Sep 2026 18:13:55 +0000", "hello", None),
        ("email_5", "personal-gmail", "Shop <x@shop.example>", "Your receipt from Shop", "Fri, 04 Sep 2026 18:13:55 -0000", "no rule", None),
    ]
    for eid, account, sender, subject, received, text, html in rows:
        text_path = html_path = None
        if text:
            (root / "blobs" / f"{eid}.txt").write_text(text)
            text_path = f"blobs/{eid}.txt"
        if html:
            (root / "blobs" / f"{eid}.html").write_text(html)
            html_path = str(root / "blobs" / f"{eid}.html")
        con.execute("insert into email_messages values (?,?,?,?,?,?,?,?,?)",
                    (eid, account, sender, subject, received, text_path, html_path, None, eid))
    con.commit()
    con.close()
    return db, root


def test_read_emails_takes_receipts_in_the_window_only(tmp_path):
    db, root = _mailroom(tmp_path)
    emails = read_emails(str(db), str(root), datetime(2026, 8, 1, tzinfo=timezone.utc))
    # A "-0000" date parses without a zone and still sorts with the others.
    assert [email.email_id for email in emails] == ["email_2", "email_1", "email_5"]
    assert "Receipt from zai $144" in emails[0].text


def test_cli_scan_appends_once_and_the_manifest_imports(tmp_path):
    db, root = _mailroom(tmp_path)
    manifest = tmp_path / "receipts.jsonl"
    manifest.write_text("// comment\n" + json.dumps({"subscription": "codex", "kind": "stated_rate", "plan": "ChatGPT Pro",
                                                     "amount": "200.00", "per": "month", "stated_at": "2026-09-25"}) + "\n")
    command = [sys.executable, "-m", "codex_usage_tracker.cli", "receipts-scan", "--manifest", str(manifest),
               "--mailroom-db", str(db), "--mailroom-root", str(root), "--since-days", "3650"]
    dry = json.loads(subprocess.run(command, check=True, capture_output=True, text=True).stdout)
    assert len(dry["added"]) == 3 and dry["appended"] == 0
    first = json.loads(subprocess.run(command + ["--append"], check=True, capture_output=True, text=True).stdout)
    again = json.loads(subprocess.run(command + ["--append"], check=True, capture_output=True, text=True).stdout)
    assert (first["appended"], again["appended"], again["already_recorded"]) == (3, 0, 3)
    assert len(read_manifest(manifest)) == 3
    assert manifest.read_text().startswith("// comment\n")


# Keeper msg 1553067845766742228 ("yeah, those should go in too"): Suno,
# Venice.ai and Brave. Bodies keep the live receipts' shape.
BRAVE = """Brave Software, Inc.
Receipt from Brave Software, Inc. $31.82 Paid September 1, 2026 Receipt number 2710-0000 Invoice number T-0007 Payment method - 3341
Receipt #2710-0000 Aug 1–Sep 1, 2026 Pro (per request) Qty 6,363 $31.82 $0.005 each Total $31.82 Amount paid $31.82 Questions?
"""
SUNO = """Suno
Receipt from Suno $256.00 Paid August 2, 2026 Receipt number 2482-0000-0000 Invoice number S-0002 Payment method - 4388
Receipt #2482-0000-0000 Aug 2, 2026 Discount for unused credits Qty 1 -$32.00 Aug 2, 2026–Aug 2, 2027 Suno Premier Qty 1 $288.00 Total $256.00 Amount paid $256.00
"""
VENICE = """Venice.ai
Receipt from Venice.ai $10.00 Paid August 3, 2026 Receipt number 2401-0000 Invoice number V-0004 Payment method - 4388
Receipt #2401-0000 Venice Credits Qty 1,000 $10.00 $0.01 each Total $10.00 Amount paid $10.00
"""


def test_brave_suno_and_venice_receipts():
    emails = [
        _email(BRAVE, '"Brave Software, Inc." <invoice+statements+acct_1MBh97BLZ1V6RGX9@stripe.com>',
               "Your receipt from Brave Software, Inc. #2710-0000", account="yugen-harborlight", eid="b"),
        _email(SUNO, "Suno <invoice+statements@suno.com>", "Your receipt from Suno #2482-0000-0000", account="yugen-harborlight", eid="s"),
        _email(VENICE, '"Venice.ai" <invoice+statements@venice.ai>', "Your receipt from Venice.ai #2401-0000", account="yugen-harborlight", eid="v"),
        _email("What's new", "Suno <suno@creators.suno.com>", "Your receipt for creating", eid="n"),
    ]
    result = scan(emails, [], today=date(2026, 9, 25))
    by = {entry["subscription"]: entry for entry in result.added}
    assert set(by) == {"brave", "suno", "venice"} and result.needs_look == []
    brave = by["brave"]
    assert (brave["kind"], brave["plan"], brave["charged_at"], brave["amount"]) == (
        "plan", "Brave Search API Pro (per request)", "2026-09-01", "31.82")
    # Billed in arrears: the receipt's period is the usage billed, kept in the
    # evidence; no paid-through date, so the next bill is expected Oct 1.
    assert "period_end" not in brave and brave["evidence"].endswith("usage 2026-08-01 to 2026-09-01, 6,363 requests")
    assert missing_rebills([brave], today=date(2026, 9, 25)) == []
    assert [row["expected"] for row in missing_rebills([brave], today=date(2026, 10, 9))] == ["2026-10-01"]
    suno = by["suno"]
    # The discount line is not a charge; the entry records what was paid.
    assert (suno["plan"], suno["amount"], suno["period_end"]) == ("Suno Premier", "256.00", "2027-08-02")
    assert (by["venice"]["kind"], by["venice"]["plan"], by["venice"]["amount"]) == ("topup", "Venice credits", "10.00")
