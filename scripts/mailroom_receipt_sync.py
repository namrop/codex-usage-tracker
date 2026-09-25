"""Paced, incremental Gmail sync of receipt mail into the oasis-mailroom store.

Run with the mailroom's own venv (it imports ``oasis_mailroom`` from the
checkout):

    /srv/pharos/repos/oasis-mailroom/.venv/bin/python mailroom_receipt_sync.py \
        --query 'newer_than:45d subject:receipt' --account personal-gmail --account yugen-harborlight

Why not ``oasis-mailroom sync-gmail``: that command lists INBOX only, fetches
every listed message again even when the store already holds it, and has no
pacing, so a few hundred receipts trip Gmail's per-user rate limit (seen
2026-09-25). This lists the query across All Mail, skips messages already in
the store, fetches the rest one by one with a short pause and a backoff on rate
limits, and imports each through ``import_eml_bytes``, the same intake path
``sync-gmail`` uses. Gmail access is read-only; the only write is the
mailroom's own store.

Prints one JSON object: per account, listed / already stored / imported, and
any errors. Exits 1 if an account could not be read.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from pathlib import Path

PAUSE_SECONDS = 0.35
RATE_LIMIT_WAIT_SECONDS = 65
RATE_LIMIT_RETRIES = 5
MAX_MESSAGES = 1000


def _is_rate_limit(exc: Exception) -> bool:
    status = getattr(getattr(exc, "resp", None), "status", None)
    return status in (403, 429) and "rate" in str(exc).lower()


def _call(request):
    for attempt in range(RATE_LIMIT_RETRIES + 1):
        try:
            return request.execute()
        except Exception as exc:  # googleapiclient.errors.HttpError
            if attempt < RATE_LIMIT_RETRIES and _is_rate_limit(exc):
                time.sleep(RATE_LIMIT_WAIT_SECONDS)
                continue
            raise
    raise AssertionError("unreachable")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", default="/srv/pharos/repos/oasis-mailroom")
    parser.add_argument("--config", default="config/accounts.local.json", help="Relative to --repo")
    parser.add_argument("--query", required=True)
    parser.add_argument("--account", action="append", required=True)
    args = parser.parse_args(argv)

    repo = Path(args.repo)
    sys.path.insert(0, str(repo / "src"))
    from oasis_mailroom.config import load_config
    from oasis_mailroom.gmail_adapter import build_gmail_service
    from oasis_mailroom.gmail_sync import _decode_gmail_raw
    from oasis_mailroom.intake import import_eml_bytes
    from oasis_mailroom.store import MailroomStore

    cfg = load_config(str(repo / args.config))
    db_path = Path(cfg.default_db_path).expanduser()
    store = MailroomStore.open(db_path, Path(cfg.default_blob_root).expanduser())
    report: dict[str, dict] = {}
    failed = False
    for account_id in args.account:
        row = report.setdefault(account_id, {"listed": 0, "already_stored": 0, "imported": 0, "errors": []})
        try:
            account = cfg.account(account_id)
            service = build_gmail_service(account)
            ids: list[str] = []
            token = None
            while len(ids) < MAX_MESSAGES:
                response = _call(service.users().messages().list(
                    userId="me", q=args.query, maxResults=100, pageToken=token))
                ids += [item["id"] for item in response.get("messages", [])]
                token = response.get("nextPageToken")
                if not token:
                    break
            row["listed"] = len(ids)
            reader = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
            try:
                stored = {r[0] for r in reader.execute(
                    "select provider_message_id from email_messages where account_id = ?", (account_id,))}
            finally:
                reader.close()
            for gmail_id in ids:
                if gmail_id in stored:
                    row["already_stored"] += 1
                    continue
                time.sleep(PAUSE_SECONDS)
                try:
                    detail = _call(service.users().messages().get(userId="me", id=gmail_id, format="raw"))
                    import_eml_bytes(
                        store, account, _decode_gmail_raw(detail["raw"]),
                        source=f"gmail:{account.id}:{gmail_id}", provider_message_id=gmail_id,
                        provider_labels=detail.get("labelIds", []), thread_id=detail.get("threadId"),
                    )
                    row["imported"] += 1
                except Exception as exc:
                    row["errors"].append(f"{gmail_id}: {type(exc).__name__}: {str(exc)[:160]}")
        except Exception as exc:
            failed = True
            row["errors"].append(f"account unreadable: {type(exc).__name__}: {str(exc)[:200]}")
    print(json.dumps(report, sort_keys=True))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
