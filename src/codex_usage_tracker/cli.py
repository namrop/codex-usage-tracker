"""Command-line interface for codex usage tracker."""

from __future__ import annotations

import argparse
import json
import logging
import os
import threading
import signal
import sys
import time as time_module
from datetime import datetime, timedelta
from typing import Optional

from .fetcher import fetch_usage
from .git_autocommit import commit_ledger
from .ledger import append_row
from .public_projection import write_public_projection

LOGGER = logging.getLogger(__name__)


DEFAULT_ATRIUM_ROOT = "/Users/luisramirez/Digital_Workspace"
DEFAULT_LEDGER_RELATIVE_PATH = "12_runtime/ledgers/codex_usage/codex_usage_ledger.jsonl"


def _resolve_ledger_path(atrium_root: str, cli_value: Optional[str]) -> str:
    if cli_value:
        return cli_value
    env_value = os.environ.get("CODEX_USAGE_LEDGER_PATH")
    if env_value:
        return env_value
    return f"{atrium_root.rstrip('/')}/{DEFAULT_LEDGER_RELATIVE_PATH}"


def _sleep_until_next_hour() -> float:
    now = datetime.now()
    next_hour = now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    return (next_hour - now).total_seconds()


def _print_summary(payload: dict) -> None:
    rate_limit = payload.get("rate_limit")
    if not isinstance(rate_limit, dict):
        rate_limit = {}
    primary = rate_limit.get("primary_window")
    if not isinstance(primary, dict):
        primary = {}
    secondary = rate_limit.get("secondary_window")
    if not isinstance(secondary, dict):
        secondary = {}

    print(f"plan_type: {payload.get('plan_type')}")
    print(f"session_used_pct: {primary.get('used_percent')}")
    print(f"weekly_used_pct: {secondary.get('used_percent')}")
    credits = payload.get("credits")
    if isinstance(credits, dict):
        print(f"credits_balance: {credits.get('balance')}")
        print(f"credits_has_credits: {credits.get('has_credits')}")


def _write_public_projection_for_args(args: argparse.Namespace, ledger_path: str) -> Optional[str]:
    if getattr(args, "no_public_projection", False):
        return None
    projection_path = getattr(args, "public_projection", None)
    if not projection_path:
        return None
    output_path = write_public_projection(
        ledger_path,
        projection_path=projection_path,
        limit=getattr(args, "public_projection_limit", 168),
        source=getattr(args, "public_projection_source", "sol-public-projection"),
    )
    return str(output_path)


def cmd_fetch(args: argparse.Namespace) -> int:
    ledger_path = _resolve_ledger_path(args.atrium_root, args.ledger)
    payload = fetch_usage()
    if payload is None:
        print("Failed to fetch usage payload.", file=sys.stderr)
        return 1

    try:
        append_row(payload, ledger_path)
    except Exception as exc:
        print(f"Failed to append ledger row: {exc}", file=sys.stderr)
        return 1

    failed = False
    projection_path = None
    try:
        projection_path = _write_public_projection_for_args(args, ledger_path)
    except Exception as exc:
        print(f"Failed to write public projection: {exc}", file=sys.stderr)
        failed = True

    try:
        _print_summary(payload)
    except Exception as exc:
        print(f"Failed to render usage summary: {exc}", file=sys.stderr)
        failed = True

    if projection_path:
        print(f"public_projection: {projection_path}")
    return 1 if failed else 0


def cmd_dump_raw(_: argparse.Namespace) -> int:
    payload = fetch_usage()
    if payload is None:
        print("Failed to fetch usage payload.", file=sys.stderr)
        return 1
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


def cmd_commit_ledger(args: argparse.Namespace) -> int:
    ledger_path = _resolve_ledger_path(args.atrium_root, args.ledger)
    try:
        result = commit_ledger(
            repo_root=args.atrium_root,
            ledger_path=ledger_path,
            message=args.message,
            dry_run=args.dry_run,
        )
    except Exception as exc:
        print(f"Failed to commit ledger: {exc}", file=sys.stderr)
        return 1

    print(result.message)
    if result.commit_sha:
        print(f"commit_sha: {result.commit_sha}")
    return 0


def cmd_write_public_projection(args: argparse.Namespace) -> int:
    ledger_path = _resolve_ledger_path(args.atrium_root, args.ledger)
    try:
        output_path = write_public_projection(
            ledger_path,
            projection_path=args.public_projection,
            limit=args.public_projection_limit,
            source=args.public_projection_source,
        )
    except Exception as exc:
        print(f"Failed to write public projection: {exc}", file=sys.stderr)
        return 1
    print(f"public_projection: {output_path}")
    return 0


def cmd_dashboard(args: argparse.Namespace) -> int:
    from .dashboard import run_dashboard

    run_dashboard(
        atrium_root=args.atrium_root,
        ledger=args.ledger,
        host=args.host,
        port=args.port,
    )
    return 0


def cmd_daemon(args: argparse.Namespace) -> int:
    ledger_path = _resolve_ledger_path(args.atrium_root, args.ledger)
    stop_event = threading.Event()

    def _shutdown(_: int, __: object) -> None:
        stop_event.set()
        print(f"{datetime.now().isoformat()} shutdown requested. Stopping daemon.")

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    while not stop_event.is_set():
        delay = _sleep_until_next_hour()
        if delay > 0:
            stop_event.wait(delay)
        if stop_event.is_set():
            break

        payload = fetch_usage()
        now = datetime.now().isoformat()
        if payload is None:
            print(f"{now} fetch failed; will retry at next hour boundary.")
            continue
        try:
            append_row(payload, ledger_path)
        except Exception as exc:
            print(f"{now} failed to append ledger row: {exc}")
            continue

        print(f"{now} usage snapshot saved to {ledger_path}")
        try:
            projection_path = _write_public_projection_for_args(args, ledger_path)
        except Exception as exc:
            print(f"{now} failed to write public projection: {exc}")
            projection_path = None
        if projection_path:
            print(f"{now} public projection saved to {projection_path}")
        try:
            _print_summary(payload)
        except Exception as exc:
            print(f"{now} failed to render usage summary: {exc}")

    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Track Codex usage snapshots.")
    subparsers = parser.add_subparsers(dest="command", required=True)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--ledger", dest="ledger", default=None, help="Path to ledger JSONL file")
    common.add_argument(
        "--atrium-root",
        dest="atrium_root",
        default=DEFAULT_ATRIUM_ROOT,
        help=f"Atrium root path (default: {DEFAULT_ATRIUM_ROOT})",
    )
    common.add_argument(
        "--public-projection",
        dest="public_projection",
        default=None,
        help="Path for public-safe derived token chart JSON to write after fetch/daemon appends",
    )
    common.add_argument(
        "--public-projection-source",
        dest="public_projection_source",
        default="sol-public-projection",
        help="Source marker to embed in the public projection payload",
    )
    common.add_argument(
        "--public-projection-limit",
        dest="public_projection_limit",
        type=int,
        default=168,
        help="Maximum derived hourly windows to project (default: 168)",
    )
    common.add_argument(
        "--no-public-projection",
        dest="no_public_projection",
        action="store_true",
        help="Skip writing the public-safe derived token chart JSON after fetch/daemon writes",
    )

    fetch_parser = subparsers.add_parser("fetch", parents=[common], help="Fetch usage and append a row")
    fetch_parser.set_defaults(func=cmd_fetch)

    daemon_parser = subparsers.add_parser("daemon", parents=[common], help="Run hourly daemon")
    daemon_parser.set_defaults(func=cmd_daemon)

    dump_parser = subparsers.add_parser("dump-raw", parents=[common], help="Fetch and print raw JSON payload")
    dump_parser.set_defaults(func=cmd_dump_raw)

    commit_parser = subparsers.add_parser(
        "commit-ledger",
        parents=[common],
        help="Validate and commit only the Codex usage ledger if it changed",
    )
    commit_parser.add_argument(
        "--message",
        dest="message",
        default="Update Codex usage ledger",
        help="Git commit message for ledger commits",
    )
    commit_parser.add_argument(
        "--dry-run",
        dest="dry_run",
        action="store_true",
        help="Validate and report what would be committed without staging or committing",
    )
    commit_parser.set_defaults(func=cmd_commit_ledger)

    public_projection_parser = subparsers.add_parser(
        "write-public-projection",
        parents=[common],
        help="Write the public-safe derived Codex token chart JSON without fetching a new usage snapshot",
    )
    public_projection_parser.set_defaults(func=cmd_write_public_projection)

    dashboard_parser = subparsers.add_parser("dashboard", help="Run the web dashboard")
    dashboard_parser.add_argument("--ledger", dest="ledger", default=None, help="Path to ledger JSONL file")
    dashboard_parser.add_argument(
        "--atrium-root",
        dest="atrium_root",
        default=DEFAULT_ATRIUM_ROOT,
        help=f"Atrium root path (default: {DEFAULT_ATRIUM_ROOT})",
    )
    dashboard_parser.add_argument(
        "--host",
        dest="host",
        default="127.0.0.1",
        help="Host for Flask server (default: 127.0.0.1)",
    )
    dashboard_parser.add_argument(
        "--port",
        dest="port",
        type=int,
        default=5174,
        help="Port for Flask server (default: 5174)",
    )
    dashboard_parser.set_defaults(func=cmd_dashboard)

    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)

    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
