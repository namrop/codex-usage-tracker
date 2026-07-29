"""Command-line interface for codex usage tracker."""

from __future__ import annotations

import argparse
from dataclasses import asdict
import json
import logging
import os
import threading
import signal
import sys
import time as time_module
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from .canonical_ledger import audit_sqlite_ledger, export_sqlite_to_jsonl, migrate_jsonl_to_sqlite
from .fetcher import fetch_usage
from .git_autocommit import commit_ledger
from .ledger import append_row, reconcile_snapshot_windows
from .public_projection import write_public_projection
from .unified_public_projection import write_unified_public_projection

LOGGER = logging.getLogger(__name__)


DEFAULT_ATRIUM_ROOT = "/Users/luisramirez/Digital_Workspace"
DEFAULT_LEDGER_RELATIVE_PATH = "12_runtime/ledgers/codex_usage/codex_usage_ledger.jsonl"
DEFAULT_SOL_ATRIUM_ROOT = "/srv/pharos/atrium/canon"
DEFAULT_SOL_HERMES_HOME = os.environ.get("HERMES_HOME") or "/var/lib/hermes/primary"
DEFAULT_CLAUDE_PROBE_DIR = "~/.local/state/codex-usage-tracker/claude-probe"
DEFAULT_UNIFIED_USAGE_LEDGER = "~/.local/state/codex-usage-tracker/usage_events.sqlite3"
DEFAULT_QUOTA_LEDGER = "~/.local/state/codex-usage-tracker/quota_observations.sqlite3"
DEFAULT_BILLING_LEDGER = "~/.local/state/codex-usage-tracker/billing_facts.sqlite3"
CANONICAL_FACT_TYPES = ("usage_event_v1", "quota_observation_v1", "billing_fact_v1")


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
    normalized = reconcile_snapshot_windows({"raw_payload": payload})
    print(f"plan_type: {payload.get('plan_type')}")
    print(f"session_used_pct: {normalized.get('session_used_pct')}")
    print(f"weekly_used_pct: {normalized.get('weekly_used_pct')}")
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


def cmd_write_public_usage_projection(args: argparse.Namespace) -> int:
    try:
        output_path = write_unified_public_projection(
            args.usage_ledger,
            args.public_projection,
            quota_ledger_path=args.quota_ledger,
            hours=args.hours,
            source=args.public_projection_source,
        )
    except Exception as exc:
        print(f"Failed to write public usage projection: {type(exc).__name__}", file=sys.stderr)
        return 1
    print(f"public_projection: {output_path}")
    return 0


def cmd_dashboard(args: argparse.Namespace) -> int:
    from .dashboard import run_dashboard

    run_dashboard(
        atrium_root=args.atrium_root,
        ledger=args.ledger,
        unified_usage_ledger=args.unified_usage_ledger,
        quota_ledger=args.quota_ledger,
        billing_ledger=args.billing_ledger,
        host=args.host,
        port=args.port,
    )
    return 0


def _print_compact_json(payload: dict) -> None:
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")))


def cmd_migrate_ledger(args: argparse.Namespace) -> int:
    try:
        result = migrate_jsonl_to_sqlite(
            args.source_jsonl,
            args.destination_sqlite,
            fact_type=args.fact_type,
            dry_run=args.dry_run,
        )
    except Exception as exc:
        print(f"migrate-ledger failed: {type(exc).__name__}", file=sys.stderr)
        return 1
    _print_compact_json(
        {
            "counts": asdict(result),
            "dry_run": args.dry_run,
            "fact_type": args.fact_type,
            "paths": {
                "source_jsonl": str(Path(args.source_jsonl).expanduser()),
                "destination_sqlite": str(Path(args.destination_sqlite).expanduser()),
            },
        }
    )
    return 0


def cmd_export_ledger(args: argparse.Namespace) -> int:
    try:
        count = export_sqlite_to_jsonl(
            args.source_sqlite,
            args.destination_jsonl,
            fact_type=args.fact_type,
        )
    except Exception as exc:
        print(f"export-ledger failed: {type(exc).__name__}", file=sys.stderr)
        return 1
    _print_compact_json(
        {
            "counts": {"exported": count},
            "fact_type": args.fact_type,
            "paths": {
                "source_sqlite": str(Path(args.source_sqlite).expanduser()),
                "destination_jsonl": str(Path(args.destination_jsonl).expanduser()),
            },
        }
    )
    return 0


def cmd_audit_ledger(args: argparse.Namespace) -> int:
    try:
        count = audit_sqlite_ledger(args.sqlite, fact_type=args.fact_type)
    except Exception as exc:
        print(f"audit-ledger failed: {type(exc).__name__}", file=sys.stderr)
        return 1
    _print_compact_json(
        {
            "counts": {"audited": count},
            "fact_type": args.fact_type,
            "paths": {"sqlite": str(Path(args.sqlite).expanduser())},
        }
    )
    return 0


def cmd_collect_all(args: argparse.Namespace) -> int:
    from .collector import collect_all

    opencode_dbs = args.opencode_db or [
        "~/.local/share/opencode/opencode-stable.db",
        "~/.local/share/opencode/opencode-local.db",
    ]
    try:
        result = collect_all(
            state_db=args.state_db,
            claude_root=args.claude_root,
            opencode_dbs=opencode_dbs,
            codex_ledger=args.codex_ledger,
            usage_ledger=args.usage_ledger,
            quota_ledger=args.quota_ledger,
            billing_ledger=args.billing_ledger,
            dotenv=args.dotenv,
            claude_quota_command=args.claude_command,
            claude_probe_dir=args.claude_probe_dir,
            claude_quota_timeout=args.claude_quota_timeout,
            live_quota=not args.no_live_quota,
            dry_run=args.dry_run,
            source_prefix=args.source_prefix,
            scope=args.scope,
            codex_quota_history=args.codex_quota_history,
            codex_quota_history_since=args.codex_quota_history_since,
            strict_sources=args.strict_sources,
        )
    except Exception as exc:
        print(f"collect-all failed: {type(exc).__name__}", file=sys.stderr)
        return 1
    _print_compact_json(result)
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

    unified_public_parser = subparsers.add_parser(
        "write-public-usage-projection",
        help="Write a public-safe aggregate from the type-bound usage SQLite ledger",
    )
    unified_public_parser.add_argument(
        "--usage-ledger",
        default=os.environ.get("UNIFIED_USAGE_LEDGER_PATH") or DEFAULT_UNIFIED_USAGE_LEDGER,
        help="Type-bound usage_event_v1 SQLite ledger",
    )
    unified_public_parser.add_argument(
        "--quota-ledger",
        default=None,
        help="Optional type-bound quota_observation_v1 SQLite ledger",
    )
    unified_public_parser.add_argument(
        "--public-projection", required=True, help="Destination public JSON artifact"
    )
    unified_public_parser.add_argument(
        "--public-projection-source",
        default="unified-usage-public-projection",
        help="Public source marker embedded in the projection",
    )
    unified_public_parser.add_argument(
        "--hours", type=int, default=168, help="Complete UTC hourly buckets (1-168; default: 168)"
    )
    unified_public_parser.set_defaults(func=cmd_write_public_usage_projection)

    migrate_parser = subparsers.add_parser(
        "migrate-ledger",
        help="Migrate a canonical JSONL ledger to a type-bound SQLite ledger",
    )
    migrate_parser.add_argument("--source-jsonl", required=True, help="Canonical JSONL source path")
    migrate_parser.add_argument(
        "--destination-sqlite", required=True, help="Canonical SQLite destination path"
    )
    migrate_parser.add_argument("--fact-type", required=True, choices=CANONICAL_FACT_TYPES)
    migrate_parser.add_argument(
        "--dry-run", action="store_true", help="Validate and summarize without creating artifacts"
    )
    migrate_parser.set_defaults(func=cmd_migrate_ledger)

    export_parser = subparsers.add_parser(
        "export-ledger",
        help="Export a type-bound canonical SQLite ledger to JSONL",
    )
    export_parser.add_argument("--source-sqlite", required=True, help="Canonical SQLite source path")
    export_parser.add_argument(
        "--destination-jsonl", required=True, help="Canonical JSONL destination path"
    )
    export_parser.add_argument("--fact-type", required=True, choices=CANONICAL_FACT_TYPES)
    export_parser.set_defaults(func=cmd_export_ledger)

    audit_parser = subparsers.add_parser(
        "audit-ledger",
        help="Audit a type-bound canonical SQLite ledger",
    )
    audit_parser.add_argument("--sqlite", required=True, help="Canonical SQLite ledger path")
    audit_parser.add_argument("--fact-type", required=True, choices=CANONICAL_FACT_TYPES)
    audit_parser.set_defaults(func=cmd_audit_ledger)

    collect_parser = subparsers.add_parser(
        "collect-all",
        help="Collect canonical usage and quota facts and bind the billing ledger",
    )
    collect_parser.add_argument(
        "--state-db",
        default=os.environ.get("HERMES_STATE_DB_PATH") or f"{DEFAULT_SOL_HERMES_HOME}/state.db",
        help="Hermes state.db path",
    )
    collect_parser.add_argument("--claude-root", default="~/.claude/projects", help="Claude Code projects root")
    collect_parser.add_argument(
        "--opencode-db",
        action="append",
        default=None,
        help="OpenCode SQLite path; repeat for stable/local stores",
    )
    collect_parser.add_argument(
        "--codex-ledger",
        default=os.environ.get("CODEX_USAGE_LEDGER_PATH")
        or f"{DEFAULT_SOL_ATRIUM_ROOT}/{DEFAULT_LEDGER_RELATIVE_PATH}",
        help="Existing Codex snapshot ledger (read only)",
    )
    collect_parser.add_argument(
        "--usage-ledger",
        default=os.environ.get("UNIFIED_USAGE_LEDGER_PATH") or DEFAULT_UNIFIED_USAGE_LEDGER,
        help="Canonical private usage ledger (SQLite by default; JSONL compatible)",
    )
    collect_parser.add_argument(
        "--quota-ledger",
        default=os.environ.get("QUOTA_LEDGER_PATH") or DEFAULT_QUOTA_LEDGER,
        help="Canonical private quota ledger (SQLite by default; JSONL compatible)",
    )
    collect_parser.add_argument(
        "--billing-ledger",
        default=os.environ.get("BILLING_LEDGER_PATH") or DEFAULT_BILLING_LEDGER,
        help="Canonical private billing ledger (SQLite by default; JSONL compatible)",
    )
    collect_parser.add_argument(
        "--dotenv",
        default=f"{DEFAULT_SOL_HERMES_HOME}/.env",
        help="Optional allowlisted provider credential dotenv",
    )
    collect_parser.add_argument(
        "--claude-command",
        default="claude",
        help="Claude Code CLI executable used to capture the authenticated /usage view",
    )
    collect_parser.add_argument(
        "--claude-probe-dir",
        default=DEFAULT_CLAUDE_PROBE_DIR,
        help="Private tracker-owned working directory for the Claude Code /usage probe",
    )
    collect_parser.add_argument(
        "--claude-quota-timeout",
        type=float,
        default=25.0,
        help="Seconds to wait for Claude Code's /usage view",
    )
    collect_parser.add_argument("--source-prefix", default="sol", help="Stable source namespace prefix")
    collect_parser.add_argument(
        "--scope",
        choices=("all", "usage", "quota"),
        default="all",
        help="Collect both lanes or only the independently scheduled usage/quota lane",
    )
    collect_parser.add_argument(
        "--strict-sources",
        action="store_true",
        help="Exit nonzero after any source warning or quarantined identity",
    )
    collect_parser.add_argument(
        "--codex-quota-history",
        action="store_true",
        help="Run a source-isolated idempotent recovery from retained legacy Codex snapshots",
    )
    collect_parser.add_argument(
        "--codex-quota-history-since",
        default=None,
        help="Required exclusive ISO-8601 cutoff for bounded Codex quota history recovery",
    )
    collect_parser.add_argument(
        "--no-live-quota",
        action="store_true",
        help="Skip the Claude Code /usage probe and provider network quota fetches",
    )
    collect_parser.add_argument("--dry-run", action="store_true", help="Validate and summarize without ledger writes")
    collect_parser.set_defaults(func=cmd_collect_all)

    dashboard_parser = subparsers.add_parser("dashboard", help="Run the web dashboard")
    dashboard_parser.add_argument("--ledger", dest="ledger", default=None, help="Path to ledger JSONL file")
    dashboard_parser.add_argument(
        "--unified-usage-ledger", default=None, help="Private canonical usage SQLite/JSONL ledger"
    )
    dashboard_parser.add_argument(
        "--quota-ledger", default=None, help="Private canonical quota SQLite/JSONL ledger"
    )
    dashboard_parser.add_argument(
        "--billing-ledger", default=None, help="Private canonical billing SQLite/JSONL ledger"
    )
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
