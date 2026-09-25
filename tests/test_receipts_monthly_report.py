"""The monthly receipts job's report (keeper msg 1553058626350874726): silent
when there is nothing to say, one line per thing otherwise."""
from __future__ import annotations

import runpy
from pathlib import Path

JOB = runpy.run_path(str(Path(__file__).resolve().parents[1] / "scripts" / "receipts_monthly.py"))
format_report = JOB["format_report"]


def test_nothing_new_is_silent():
    assert format_report({"added": [], "needs_look": [], "missing_rebills": [], "appended": 0}, commit=None, problems=[]) == ""


def test_each_finding_is_one_line():
    scan = {
        "added": [
            {"subscription": "claude-1", "amount": "200.00", "kind": "plan", "charged_at": "2026-10-03"},
            {"subscription": "openrouter", "amount": "26.49", "kind": "topup", "charged_at": "2026-10-07"},
        ],
        "needs_look": [{"vendor": "kimi", "amount": "199.00", "received": "2026-10-02", "subject": "Your receipt from NOVASCENT",
                        "account": "personal-gmail", "email": "email_x", "problem": "line items not recognised: ['Kimi Pro Annual']"}],
        "missing_rebills": [{"label": "OpenCode Go", "last_charge": "2026-09-08", "expected": "2026-10-08"}],
    }
    report = format_report(scan, commit="abc1234", problems=["Mail sync yugen-harborlight: 1 message(s) not fetched"])
    lines = report.splitlines()
    assert lines[0] == "AI subscription receipts (monthly)"
    assert lines[1] == ("- Added 2 charges to the receipts file: Claude 1 $200.00 plan (Oct 3) · "
                        "OpenRouter $26.49 topup (Oct 7). Canon abc1234.")
    assert lines[2].startswith("- Needs a look: Kimi K3 $199.00, 2026-10-02") and lines[2].endswith("Not recorded.")
    assert lines[3] == "- No rebill in mail: OpenCode Go (last charge Sep 8, expected around Oct 8)."
    assert lines[4] == "- Mail sync yugen-harborlight: 1 message(s) not fetched"
