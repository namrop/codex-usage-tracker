"""Monthly AI subscription receipts job (keeper, 2026-09-25, Discord unified
usage ledger thread, msg 1553058626350874726: "we should also have the monthly
job").

1. Sync the last 45 days of receipt mail for both mailboxes into the
   oasis-mailroom store (paced, All Mail, already-stored messages skipped).
2. ``receipts-scan --append``: add new charges to the Atrium receipts manifest.
3. If anything was added: import the manifest into the billing ledger and
   commit the manifest in the canon.
4. Print a short report. Nothing to say (no new charge, nothing unparsed, no
   missed rebill) prints nothing, so the Hermes no-agent job stays silent.
   A failed step prints what failed and exits 1.

Run by the Hermes cron entrypoint ``$HERMES_HOME/scripts/ai_usage_receipts_monthly.py``.
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
from datetime import date
from pathlib import Path
from typing import Any, Mapping

TRACKER = Path("/srv/pharos/repos/codex-usage-tracker")
MAILROOM = Path("/srv/pharos/repos/oasis-mailroom")
CANON = Path("/srv/pharos/atrium/canon")
MANIFEST_REL = "07_systems/ai_usage_tracker/billing/ai_subscription_receipts.jsonl"
BILLING_LEDGER = "/home/luis/.local/state/codex-usage-tracker/billing_facts.sqlite3"
GIT = "/run/current-system/sw/bin/git"
QUERY = "newer_than:45d subject:receipt"
ACCOUNTS = ("personal-gmail", "yugen-harborlight")
LABELS = {
    "claude-1": "Claude 1", "claude-2": "Claude 2", "codex": "Codex", "z-ai": "Z.AI", "kimi": "Kimi K3",
    "opencode-go": "OpenCode Go", "openrouter": "OpenRouter", "deepseek": "DeepSeek", "pocket": "Pocket",
    "elevenlabs": "ElevenLabs",
}


def _day(text: str | None) -> str:
    try:
        return date.fromisoformat(str(text)[:10]).strftime("%b %-d")
    except ValueError:
        return str(text)


def format_report(scan: Mapping[str, Any], *, commit: str | None, problems: list[str]) -> str:
    lines: list[str] = []
    added = scan.get("added") or []
    if added:
        charges = [f"{LABELS.get(e['subscription'], e['subscription'])} ${e['amount']} {e['kind']} ({_day(e['charged_at'])})"
                   for e in added]
        lines.append(f"Added {len(added)} charge{'s' if len(added) != 1 else ''} to the receipts file: " + " · ".join(charges)
                     + (f". Canon {commit}." if commit else "."))
    for row in scan.get("needs_look") or []:
        amount = f" ${row['amount']}" if row.get("amount") else ""
        lines.append(f"Needs a look: {LABELS.get(row['vendor'], row['vendor'])}{amount}, {row.get('received')} "
                     f"\"{row.get('subject')}\" ({row['account']} {row['email']}): {row['problem']}. Not recorded.")
    for row in scan.get("missing_rebills") or []:
        lines.append(f"No rebill in mail: {row['label']} (last charge {_day(row['last_charge'])}, "
                     f"expected around {_day(row['expected'])}).")
    lines += problems
    if not lines:
        return ""
    return "AI subscription receipts (monthly)\n" + "\n".join(f"- {line}" for line in lines)


def _run(command: list[str], *, cwd: Path, timeout: int) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, cwd=str(cwd), capture_output=True, text=True, timeout=timeout)


def _commit_manifest(count: int) -> str:
    message = (f"ai usage: {count} subscription charge{'s' if count != 1 else ''} from receipt mail "
               f"(monthly receipts job, keeper msg 1553058626350874726)")
    _run([GIT, "add", "--", MANIFEST_REL], cwd=CANON, timeout=120).check_returncode()
    for attempt in range(3):
        result = _run([GIT, "commit", "-q", "--only", "-m", message, "--", MANIFEST_REL], cwd=CANON, timeout=300)
        if result.returncode == 0:
            return _run([GIT, "rev-parse", "--short", "HEAD"], cwd=CANON, timeout=30).stdout.strip()
        if "index.lock" not in (result.stderr + result.stdout) or attempt == 2:
            raise RuntimeError(f"canon commit failed: {(result.stderr or result.stdout).strip()[-300:]}")
        time.sleep(10)
    raise AssertionError("unreachable")


def main() -> int:
    problems: list[str] = []
    sync = _run([str(MAILROOM / ".venv/bin/python"), str(TRACKER / "scripts/mailroom_receipt_sync.py"),
                 "--repo", str(MAILROOM), "--query", QUERY, *[arg for a in ACCOUNTS for arg in ("--account", a)]],
                cwd=MAILROOM, timeout=1500)
    if sync.returncode != 0:
        problems.append(f"Mail sync failed, so this scan used the mail already stored: "
                        f"{(sync.stdout or sync.stderr).strip()[-300:]}")
    else:
        for account, row in json.loads(sync.stdout).items():
            if row.get("errors"):
                problems.append(f"Mail sync {account}: {len(row['errors'])} message(s) not fetched, first: {row['errors'][0]}")
    manifest = str(CANON / MANIFEST_REL)
    scan_run = _run([str(TRACKER / ".venv/bin/codex-usage-tracker"), "receipts-scan", "--manifest", manifest,
                     "--since-days", "45", "--append"], cwd=TRACKER, timeout=600)
    if scan_run.returncode != 0:
        print(format_report({}, commit=None, problems=problems + [
            f"Receipts scan failed: {(scan_run.stderr or scan_run.stdout).strip()[-400:]}"]))
        return 1
    scan = json.loads(scan_run.stdout)
    commit = None
    if scan.get("appended"):
        imported = _run([str(TRACKER / ".venv/bin/codex-usage-tracker"), "billing-import", "--manifest", manifest,
                         "--billing-ledger", BILLING_LEDGER], cwd=TRACKER, timeout=600)
        if imported.returncode != 0:
            problems.append(f"Billing import failed (the receipts file has the lines): {imported.stderr.strip()[-300:]}")
        try:
            commit = _commit_manifest(int(scan["appended"]))
        except (RuntimeError, subprocess.SubprocessError) as exc:
            problems.append(f"Receipts file updated but not committed: {exc}")
    report = format_report(scan, commit=commit, problems=problems)
    if report:
        print(report)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
