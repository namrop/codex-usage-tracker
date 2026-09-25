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
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional, Sequence

from .canonical_ledger import audit_sqlite_ledger, export_sqlite_to_jsonl, migrate_jsonl_to_sqlite
from .claude_instances import ClaudeInstance, parse_claude_instance_declaration
from .fetcher import fetch_usage
from .git_autocommit import commit_ledger
from .ledger import append_row, reconcile_snapshot_windows
from .public_projection import write_public_projection
from .rulings import (
    DEFAULT_SERVICE_URL,
    DEFAULT_WEEKS as DEFAULT_RULING_WEEKS,
    MAX_WEEKS as MAX_RULING_WEEKS,
    RULING_SOURCES,
)
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
DEFAULT_SEARCH_LEDGER = "~/.local/state/codex-usage-tracker/search_events.sqlite3"
DEFAULT_SEARCH_AUDIT = f"{DEFAULT_SOL_HERMES_HOME}/logs/search_audit.jsonl"
# Seven days is the window the Z.ai web search/reader limit resets on, and
# the one the keeper asked to be able to see coming.
DEFAULT_SEARCH_SUMMARY_DAYS = 7
MAX_SEARCH_SUMMARY_DAYS = 92
CANONICAL_FACT_TYPES = (
    "usage_event_v1", "quota_observation_v1", "billing_fact_v1", "search_event_v1",
)


class _ClaudeInstanceAction(argparse.Action):
    """Parse repeatable declarations and reject duplicate stable suffixes."""

    def __call__(
        self,
        parser: argparse.ArgumentParser,
        namespace: argparse.Namespace,
        values: str | Sequence[Any] | None,
        option_string: str | None = None,
    ) -> None:
        if not isinstance(values, str):
            parser.error(f"{option_string or self.dest}: expected one declaration")
        try:
            instance = parse_claude_instance_declaration(values)
        except ValueError as exc:
            parser.error(f"{option_string or self.dest}: {exc}")
        current: list[ClaudeInstance] = list(getattr(namespace, self.dest, None) or [])
        if any(item.source_suffix == instance.source_suffix for item in current):
            parser.error(
                f"{option_string or self.dest}: duplicate Claude source suffix: "
                f"{instance.source_suffix}"
            )
        current.append(instance)
        setattr(namespace, self.dest, current)


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
    if getattr(args, "no_public_projection", False):
        print("public_projection: skipped (--no-public-projection)")
        return 0
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
        search_ledger=args.search_ledger,
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


_MAX_UNMAPPED_SCAN_HOURS = 8760
# The canonical query is itself bounded (see canonical_ledger._SQLITE_QUERY_LIMIT);
# stay inside it and report when the scan hit the ceiling rather than
# presenting a truncated inventory as complete.
_MAX_UNMAPPED_SCAN_EVENTS = 100_000


def _bounded_scan_hours(value: str) -> int:
    hours = int(value)
    if not 1 <= hours <= _MAX_UNMAPPED_SCAN_HOURS:
        raise argparse.ArgumentTypeError(
            f"hours must be an integer from 1 through {_MAX_UNMAPPED_SCAN_HOURS}"
        )
    return hours


def _bounded_weeks(value: str) -> int:
    weeks = int(value)
    if not 1 <= weeks <= MAX_RULING_WEEKS:
        raise argparse.ArgumentTypeError(
            f"weeks must be an integer from 1 through {MAX_RULING_WEEKS}"
        )
    return weeks


def cmd_usage_cache_refresh(args: argparse.Namespace) -> int:
    """Rebuild the dashboard's Redis copies from the ledgers.

    Each copy is refreshed on its own: one failing leaves the others current
    (and the dashboard reads that one's source), and the exit status is 1.
    """
    from .usage_cache import refresh, refresh_quota

    parts = [("usage", lambda: refresh(args.ledger, args.socket, span_hours=args.hours, force=args.force))]
    if args.quota_ledger:
        parts.append(("quota", lambda: refresh_quota(args.quota_ledger, args.socket, force=args.force)))
    status = 0
    for name, run in parts:
        try:
            summary = run()
        except Exception as exc:  # noqa: BLE001 - reported, and the next copy still refreshes
            status = 1
            summary = {"status": "failed", "error": f"{type(exc).__name__}: {exc}"}
        print(json.dumps({"copy": name, **summary}, sort_keys=True), flush=True)
    return status


def cmd_rulings_cache_refresh(args: argparse.Namespace) -> int:
    """Walk the canon for ruling ledgers and store the scan in Redis."""
    from .usage_cache import refresh_rulings

    print(json.dumps({"copy": "rulings", **refresh_rulings(args.atrium_root, args.socket)}, sort_keys=True))
    return 0


def cmd_unmapped_models(args: argparse.Namespace) -> int:
    """Private, read-only inventory of provider/model IDs no allowlist covers.

    Two independent allowlists decide whether a model is visible: the public
    projection's reviewed label map and the list-price rate tables. A model
    switch upstream silently lands in "Other" and/or in a provider default
    until somebody traces it by hand — this command is that trace, on demand.

    It is VISIBILITY, not automation. It reads one ledger, prints raw private
    IDs to the operator's own terminal, and writes nothing anywhere. Seeing an
    ID here does not make it publishable: adding it to the public map stays a
    deliberate, reviewed edit.
    """
    from .canonical_ledger import query_sqlite_facts, query_sqlite_physical_requests
    from .list_pricing import _rates_for
    from .unified_public_projection import _public_model_label, _public_provider_label

    try:
        facts = query_sqlite_facts(
            args.sqlite,
            fact_type="usage_event_v1",
            order="desc",
            limit=1,
        )
    except Exception as exc:
        print(f"unmapped-models failed: {type(exc).__name__}", file=sys.stderr)
        return 1

    def occurred(fact: dict[str, Any]) -> str:
        return str(fact.get("occurred_at") or fact.get("recorded_at") or "")

    timestamps = [occurred(fact) for fact in facts if occurred(fact)]
    latest = max(timestamps) if timestamps else None
    window: dict[str, Any] = {"hours": args.hours, "start": None, "end": latest}
    start_at = None
    if latest is not None:
        # The window is anchored on the newest fact rather than on wall clock:
        # a stalled collector should still produce a report instead of an
        # empty one that reads like "nothing unmapped".
        end_at = datetime.fromisoformat(latest.replace("Z", "+00:00"))
        start_at = end_at - timedelta(hours=args.hours)
        window["start"] = start_at.isoformat().replace("+00:00", "Z")

        # Apply the time window in SQLite before decoding facts. Like the
        # dashboard, use the online integrity reader (hash, identity and
        # typed columns remain checked), not a full per-event schema audit.
        try:
            # Contract v2: token volumes per id count physical requests.
            facts, _identity = query_sqlite_physical_requests(
                args.sqlite,
                occurred_or_observed_at_gte=window["start"],
                order="desc",
                limit=_MAX_UNMAPPED_SCAN_EVENTS,
                contract_validation=False,
            )
        except Exception as exc:
            print(f"unmapped-models failed: {type(exc).__name__}", file=sys.stderr)
            return 1

    groups: dict[tuple[str, str], dict[str, Any]] = {}
    for fact in facts:
        stamp = occurred(fact)
        if start_at is not None and stamp:
            if datetime.fromisoformat(stamp.replace("Z", "+00:00")) < start_at:
                continue
        provider = str(fact.get("provider") or "unknown")
        # The read path prefers model_reported (what actually served) over
        # model_requested, so the inventory must classify the same identity.
        model = str(fact.get("model_reported") or fact.get("model_requested") or "unknown")
        group = groups.setdefault(
            (provider, model),
            {
                "provider": provider,
                "model": model,
                "events": 0,
                "total_tokens": 0,
                "first_seen": stamp or None,
                "last_seen": stamp or None,
            },
        )
        group["events"] += 1
        group["total_tokens"] += sum(
            int(fact.get(field) or 0)
            for field in ("input_tokens", "cache_read_tokens", "cache_write_tokens", "output_tokens")
        )
        if stamp:
            group["first_seen"] = min(group["first_seen"] or stamp, stamp)
            group["last_seen"] = max(group["last_seen"] or stamp, stamp)

    unmapped: list[dict[str, Any]] = []
    for (provider, model), group in groups.items():
        public_label = _public_model_label(model)
        provider_label = _public_provider_label(provider)
        rates = _rates_for(provider, model)
        publishable = public_label is not None and provider_label != "Other"
        if publishable and rates is not None:
            continue
        unmapped.append({
            **group,
            "public_provider_label": provider_label,
            "public_label": public_label,
            "list_priced": rates is not None,
        })
    unmapped.sort(key=lambda row: (-row["total_tokens"], -row["events"], row["provider"], row["model"]))

    _print_compact_json({
        "window": window,
        "counts": {
            "scanned_events": len(facts),
            "scanned_pairs": len(groups),
            "unmapped_pairs": len(unmapped),
        },
        # True when the bounded scan hit its ceiling: the newest
        # _MAX_UNMAPPED_SCAN_EVENTS rows were read, so older pairs may be
        # missing. Never present a truncated inventory as complete.
        "scan_truncated": len(facts) >= _MAX_UNMAPPED_SCAN_EVENTS,
        "paths": {"sqlite": str(Path(args.sqlite).expanduser())},
        "unmapped": unmapped,
    })
    return 0


def cmd_rulings_per_dollar(args: argparse.Namespace) -> int:
    """Rulings per ISO week against that week's list-price inference estimate.

    Keeper ruling 2026-09-10 ("we need to make ratification a first class
    thing"): tokens proxy cost, cost proxies value used, value used proxies
    value produced — and the keeper's unit of produced value is a
    ratification. This prints the join rather than leaving it to be mined by
    hand.

    Read-only and bounded: it scans the Atrium for ``ruling_ledger: v1``
    files (or asks Atrium Service when ``--rulings-source service``) and
    reads the canonical usage ledger for the same weeks. It writes nothing.
    """
    from .rulings import (
        RulingsSourceUnavailable,
        build_rulings_per_dollar,
        load_rulings,
        usage_window,
    )

    try:
        scan = load_rulings(
            atrium_root=args.atrium_root,
            source=args.rulings_source,
            service_url=args.service_url,
        )
    except RulingsSourceUnavailable as exc:
        print(f"rulings-per-dollar failed: {exc}", file=sys.stderr)
        return 1
    except ValueError as exc:
        print(f"rulings-per-dollar failed: {exc}", file=sys.stderr)
        return 1

    from .rulings import read_usage_rows

    window_start, window_end = usage_window(args.weeks)
    rows: list[dict[str, Any]] | None
    ledger_path = Path(args.usage_ledger).expanduser()
    if not ledger_path.exists():
        # No usage ledger on this host: print the counts with null prices
        # rather than a $0 denominator, which would read as free judgment.
        print(
            f"no usage ledger at {ledger_path}: printing counts without prices",
            file=sys.stderr,
        )
        rows = None
    else:
        try:
            rows = read_usage_rows(ledger_path, start=window_start, end=window_end)
        except Exception as exc:
            print(
                f"rulings-per-dollar failed reading usage ledger: {type(exc).__name__}",
                file=sys.stderr,
            )
            return 1

    payload = build_rulings_per_dollar(scan, rows, weeks=args.weeks)
    if args.json:
        _print_compact_json(payload)
        return 0

    coverage = payload["coverage"]
    header = (
        f"{'week':10} {'rulings':>7} {'list price':>12} {'per $1k':>9} "
        f"{'output tok':>12}  classes"
    )
    print(header)
    print("-" * len(header))
    for entry in payload["series"]:
        price = entry.get("list_price_usd")
        ratio = entry.get("rulings_per_1k_usd")
        output_tokens = entry.get("output_tokens")
        classes = " ".join(
            f"{name}:{count}"
            for name, count in sorted(entry["rulings_by_class"].items())
            if count
        )
        # "—" for a ratio means the week has no denominator (no priced
        # traffic), which is not the same statement as a ratio of zero.
        week_label = entry["week"] + ("*" if entry["partial"] else "")
        price_text = f"${price}" if price is not None else "—"
        ratio_text = f"{ratio:.2f}" if ratio is not None else "—"
        tokens_text = f"{output_tokens:,}" if output_tokens is not None else "—"
        print(
            f"{week_label:10} {entry['rulings_total']:>7} {price_text:>12} "
            f"{ratio_text:>9} {tokens_text:>12}  {classes}"
        )
    print()
    print(
        f"weeks={payload['weeks']} tz={payload['timezone']} source={coverage['source']} "
        f"ledgers={coverage['ledgers_scanned']} entries={coverage['entries_parsed']} "
        f"parse_errors={coverage['parse_errors']} duplicates={coverage['duplicate_uids']}"
    )
    for detail in coverage["parse_error_details"]:
        location = f"{detail['path']}:{detail['line']}" if detail.get("line") else detail["path"]
        print(f"  parse error: {location}: {detail['reason']}", file=sys.stderr)
    print("* partial week (still in progress).")
    print(f"list price is an ESTIMATE at the {coverage['rate_basis']} service tier, not billed cost.")
    print("counts RECORDED rulings, not keeper adjudication traffic.")
    return 0


def cmd_collect_searches(args: argparse.Namespace) -> int:
    """Ingest the Hermes web search/extract audit feed into its own ledger.

    The searches scope of ``collect-all`` with only the arguments that scope
    reads, so a timer can run it without naming the usage or Claude lanes it
    would ignore anyway.
    """
    from .collector import collect_all

    try:
        result = collect_all(
            state_db=args.state_db,
            opencode_dbs=[],
            codex_ledger=args.state_db,
            usage_ledger=args.usage_ledger,
            quota_ledger=args.quota_ledger,
            search_ledger=args.search_ledger,
            search_audit=args.search_audit,
            scope="searches",
            live_quota=False,
            dry_run=args.dry_run,
            source_prefix=args.source_prefix,
        )
    except Exception as exc:
        print(f"collect-searches failed: {type(exc).__name__}", file=sys.stderr)
        return 1
    print(_collect_summary_line(result), file=sys.stderr)
    _print_compact_json(
        {k: v for k, v in result.items() if k not in ("resolutions", "conflicts")}
    )
    return 0


def cmd_searches(args: argparse.Namespace) -> int:
    """Read-only: calls per backend per day, rate limits, and fallbacks.

    Keeper ruling 2026-09-10 ("the ai usage ledger should start tracking
    searches as well in a new table"): the masthead composer hit an exhausted
    Z.ai weekly web search limit that nothing was counting down to. This is
    the surface that counts it. It writes nothing.
    """
    from .canonical_ledger import _SQLITE_QUERY_LIMIT as SQLITE_QUERY_LIMIT
    from .canonical_ledger import query_sqlite_facts
    from .search_events import build_search_summary

    target = Path(args.sqlite).expanduser()
    if not target.exists():
        # An absent ledger is "nothing collected yet", not a failure: the
        # Hermes feed and this lane can be enabled in either order.
        _print_compact_json(
            {
                "days": args.days,
                "totals": {"calls": 0, "rate_limited": 0, "errors": 0, "fallbacks": 0},
                "backends": [],
                "daily": [],
                "paths": {"sqlite": str(target)},
                "ledger_present": False,
                "truncated": False,
            }
        )
        return 0
    cutoff = datetime.now(timezone.utc) - timedelta(days=args.days)
    # The canonical query is itself bounded. Say so when the window filled it
    # rather than presenting a truncated count as the whole window.
    limit = SQLITE_QUERY_LIMIT
    try:
        facts = query_sqlite_facts(
            target,
            fact_type="search_event_v1",
            occurred_or_observed_at_gte=cutoff.isoformat().replace("+00:00", "Z"),
            order="asc",
            limit=limit,
        )
        summary = build_search_summary(facts, days=args.days)
    except Exception as exc:
        print(f"searches failed: {type(exc).__name__}", file=sys.stderr)
        return 1
    _print_compact_json(
        {
            **summary,
            "paths": {"sqlite": str(target)},
            "ledger_present": True,
            "truncated": len(facts) >= limit,
        }
    )
    return 0


def _bounded_search_days(value: str) -> int:
    days = int(value)
    if not 1 <= days <= MAX_SEARCH_SUMMARY_DAYS:
        raise argparse.ArgumentTypeError(
            f"days must be an integer from 1 through {MAX_SEARCH_SUMMARY_DAYS}"
        )
    return days


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
            claude_instances=args.claude_instances,
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
            quota_lanes=args.quota_lanes,
            when_active=args.when_active,
            search_ledger=args.search_ledger,
            search_audit=args.search_audit,
        )
    except Exception as exc:
        print(f"collect-all failed: {type(exc).__name__}", file=sys.stderr)
        warnings = getattr(exc, "warnings", None)
        if isinstance(warnings, list) and warnings:
            # Sanitized lane diagnostics: source names and error type names
            # only — never raw exception messages (credential/path risk).
            print(json.dumps(warnings, sort_keys=True), file=sys.stderr)
        report_path = getattr(exc, "report_path", None)
        if isinstance(report_path, str) and report_path:
            print(f"collect-all report: {report_path}", file=sys.stderr)
        return 1
    print(_collect_summary_line(result), file=sys.stderr)
    # Journal-friendly stdout (keeper ruling 2026-09-06): the per-identity
    # `resolutions`/`conflicts` arrays run to ~800KB on churny runs and exist
    # only for post-hoc diagnosis, which the persisted per-run report already
    # carries (health/collect-<scope>-last.json, written with the full
    # payload). Echoing them here only bloated the systemd journal. Counts
    # stay; detail lives in the report.
    _print_compact_json(
        {k: v for k, v in result.items() if k not in ("resolutions", "conflicts")}
    )
    return 0


def _collect_summary_line(result: dict[str, Any]) -> str:
    """One journal-friendly line naming what this run converged or could not."""
    counts = result.get("counts") or {}
    usage = result.get("usage") or {}
    quotas = result.get("quotas") or {}
    searches = result.get("searches") or {}
    fields = [
        f"scope={result.get('scope', 'all')}",
        f"appended={int(usage.get('appended', 0)) + int(quotas.get('appended', 0)) + int(searches.get('appended', 0))}",
        f"replayed={int(usage.get('replayed', 0)) + int(quotas.get('replayed', 0)) + int(searches.get('replayed', 0))}",
        f"quarantined={counts.get('conflicts', 0)}",
        f"corrections={result.get('generated_corrections', 0)}",
        f"regressions={counts.get('resolutions', 0)}",
        f"newly_adjudicated={counts.get('resolutions_recorded', 0)}",
        f"warnings={counts.get('warnings', 0)}",
    ]
    if searches:
        # The search lane is the only one whose feed can be silently absent,
        # so the journal names what it read even when it read nothing.
        fields.append(f"searches={searches.get('appended', 0)}")
        if searches.get("derived_quota_observations"):
            fields.append(f"search_quota={searches['derived_quota_observations']}")
        if searches.get("malformed_lines"):
            fields.append(f"search_malformed={searches['malformed_lines']}")
    lanes = result.get("lanes") or {}
    if lanes:
        degraded = sorted(
            name for name, lane in lanes.items() if not (lane or {}).get("succeeded")
        )
        fields.append(f"lanes={len(lanes) - len(degraded)}/{len(lanes)}")
        if degraded:
            # Name the lane, not just the count: a healthy heartbeat with one
            # degraded lane is the exact state this pipeline used to hide.
            fields.append("degraded=" + ",".join(degraded))
        ceded = sorted(
            name for name, lane in lanes.items() if (lane or {}).get("ceded")
        )
        if ceded:
            # A ceded lane exhausted its retries and was skipped; the run
            # still exits 0, so the journal line is where the cede surfaces.
            fields.append("ceded=" + ",".join(ceded))
    report = (result.get("paths") or {}).get("report")
    if report:
        fields.append(f"report={report}")
    return "collect-all: " + " ".join(fields)


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

    unmapped_parser = subparsers.add_parser(
        "unmapped-models",
        help=(
            "Read-only private inventory of provider/model IDs with no reviewed "
            "public label and/or no list price, with counts"
        ),
    )
    unmapped_parser.add_argument(
        "--sqlite",
        default=os.environ.get("UNIFIED_USAGE_LEDGER_PATH") or DEFAULT_UNIFIED_USAGE_LEDGER,
        help="Type-bound usage_event_v1 SQLite ledger to read",
    )
    unmapped_parser.add_argument(
        "--hours",
        type=_bounded_scan_hours,
        default=168,
        help=(
            f"Hours back from the newest fact in the ledger "
            f"(1-{_MAX_UNMAPPED_SCAN_HOURS}; default: 168)"
        ),
    )
    unmapped_parser.set_defaults(func=cmd_unmapped_models)

    cache_parser = subparsers.add_parser(
        "usage-cache-refresh",
        help="Rebuild the dashboard's Redis copy of recent elected usage rows from the SQLite ledger",
    )
    cache_parser.add_argument(
        "--ledger",
        default=os.environ.get("UNIFIED_USAGE_LEDGER_PATH") or DEFAULT_UNIFIED_USAGE_LEDGER,
        help="Type-bound usage_event_v1 SQLite ledger to read (read-only)",
    )
    cache_parser.add_argument(
        "--socket",
        default=os.environ.get("AI_USAGE_REDIS_SOCKET") or "/run/redis-ai-usage/redis.sock",
        help="Redis unix socket",
    )
    cache_parser.add_argument("--hours", type=int, default=192, help="Span to cache, back from now (default: 192)")
    cache_parser.add_argument(
        "--quota-ledger",
        default=None,
        help="Also copy the last 36 days of this quota_observation_v1 SQLite ledger (read-only)",
    )
    cache_parser.add_argument(
        "--force", action="store_true", help="Rebuild even when the ledger file has not changed"
    )
    cache_parser.set_defaults(func=cmd_usage_cache_refresh)

    rulings_cache_parser = subparsers.add_parser(
        "rulings-cache-refresh",
        help="Walk the Atrium canon for ruling ledgers and store the scan in the dashboard's Redis",
    )
    rulings_cache_parser.add_argument("--atrium-root", required=True, help="Atrium canon root to scan (read-only)")
    rulings_cache_parser.add_argument(
        "--socket",
        default=os.environ.get("AI_USAGE_REDIS_SOCKET") or "/run/redis-ai-usage/redis.sock",
        help="Redis unix socket",
    )
    rulings_cache_parser.set_defaults(func=cmd_rulings_cache_refresh)

    rulings_parser = subparsers.add_parser(
        "rulings-per-dollar",
        help=(
            "Weekly keeper ratifications joined to the same weeks' list-price "
            "inference estimate (read-only)"
        ),
    )
    rulings_parser.add_argument(
        "--weeks",
        type=_bounded_weeks,
        default=DEFAULT_RULING_WEEKS,
        help=f"ISO weeks back, including the current one (1-{MAX_RULING_WEEKS}; default: {DEFAULT_RULING_WEEKS})",
    )
    rulings_parser.add_argument(
        "--json", action="store_true", help="Emit the compact JSON payload instead of a table"
    )
    rulings_parser.add_argument(
        "--atrium-root",
        dest="atrium_root",
        default=DEFAULT_SOL_ATRIUM_ROOT,
        help=f"Atrium root to scan for ruling ledgers (default: {DEFAULT_SOL_ATRIUM_ROOT})",
    )
    rulings_parser.add_argument(
        "--rulings-source",
        dest="rulings_source",
        choices=RULING_SOURCES,
        default="canon",
        help=(
            "Where ruling entries come from: 'canon' scans the Atrium directly "
            "(default), 'service' calls Atrium Service's /rulings endpoint"
        ),
    )
    rulings_parser.add_argument(
        "--service-url",
        dest="service_url",
        default=DEFAULT_SERVICE_URL,
        help=f"Atrium Service base URL for --rulings-source service (default: {DEFAULT_SERVICE_URL})",
    )
    rulings_parser.add_argument(
        "--usage-ledger",
        dest="usage_ledger",
        default=os.environ.get("UNIFIED_USAGE_LEDGER_PATH") or DEFAULT_UNIFIED_USAGE_LEDGER,
        help="Type-bound usage_event_v1 ledger to price the weeks against",
    )
    rulings_parser.set_defaults(func=cmd_rulings_per_dollar)

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
        "--claude-instance",
        dest="claude_instances",
        action=_ClaudeInstanceAction,
        default=None,
        metavar="SOURCE_SUFFIX=CONFIG_DIR",
        help=(
            "Claude Code config instance; repeat for multiple accounts. Each usage root is "
            "CONFIG_DIR/projects. When omitted, --claude-root and --claude-probe-dir retain "
            "legacy behavior"
        ),
    )
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
        default=45.0,
        help="Seconds to wait for Claude Code's /usage view",
    )
    collect_parser.add_argument("--source-prefix", default="sol", help="Stable source namespace prefix")
    collect_parser.add_argument(
        "--scope",
        choices=("all", "usage", "quota", "searches"),
        default="all",
        help=(
            "Collect every lane or only the independently scheduled "
            "usage/quota/searches lane"
        ),
    )
    collect_parser.add_argument(
        "--search-ledger",
        default=os.environ.get("SEARCH_LEDGER_PATH") or DEFAULT_SEARCH_LEDGER,
        help="Canonical private web search/extract ledger (SQLite by default; JSONL compatible)",
    )
    collect_parser.add_argument(
        "--search-audit",
        default=os.environ.get("SEARCH_AUDIT_PATH") or DEFAULT_SEARCH_AUDIT,
        help="Hermes search audit feed to ingest incrementally (a missing file is an empty lane)",
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
    collect_parser.add_argument(
        "--quota-lanes",
        dest="quota_lanes",
        action="append",
        default=None,
        metavar="SOURCE",
        help=(
            "Collect only the named quota lanes (repeatable; e.g. "
            "claude_code_quota / claude_code_secondary_quota); other lanes "
            "report zero rows without probing"
        ),
    )
    collect_parser.add_argument(
        "--when-active",
        action="store_true",
        help=(
            "Gate live Claude quota probes on the account's own five-hour "
            "window showing active burn in the quota ledger (five-minute "
            "active-window tick)"
        ),
    )
    collect_parser.set_defaults(func=cmd_collect_all)

    collect_searches_parser = subparsers.add_parser(
        "collect-searches",
        help="Ingest the Hermes web search/extract audit feed into the search ledger",
    )
    collect_searches_parser.add_argument(
        "--search-ledger",
        default=os.environ.get("SEARCH_LEDGER_PATH") or DEFAULT_SEARCH_LEDGER,
        help="Canonical private web search/extract ledger",
    )
    collect_searches_parser.add_argument(
        "--search-audit",
        default=os.environ.get("SEARCH_AUDIT_PATH") or DEFAULT_SEARCH_AUDIT,
        help="Hermes search audit feed to ingest incrementally",
    )
    collect_searches_parser.add_argument(
        "--quota-ledger",
        default=os.environ.get("QUOTA_LEDGER_PATH") or DEFAULT_QUOTA_LEDGER,
        help=(
            "Canonical quota ledger receiving the exhaustion observations "
            "derived from rate-limited search events"
        ),
    )
    collect_searches_parser.add_argument(
        "--usage-ledger",
        default=os.environ.get("UNIFIED_USAGE_LEDGER_PATH") or DEFAULT_UNIFIED_USAGE_LEDGER,
        help="Unused by this scope; accepted so the aliasing preflight sees the same paths",
    )
    collect_searches_parser.add_argument(
        "--state-db",
        default=os.environ.get("HERMES_STATE_DB_PATH") or f"{DEFAULT_SOL_HERMES_HOME}/state.db",
        help="Unused by this scope; accepted for symmetry with collect-all",
    )
    collect_searches_parser.add_argument(
        "--source-prefix", default="sol", help="Stable source namespace prefix"
    )
    collect_searches_parser.add_argument(
        "--dry-run", action="store_true", help="Validate and summarize without ledger writes"
    )
    collect_searches_parser.set_defaults(func=cmd_collect_searches)

    searches_parser = subparsers.add_parser(
        "searches",
        help=(
            "Read-only summary of web search/extract calls per backend per day, "
            "with rate-limited and fallback counts"
        ),
    )
    searches_parser.add_argument(
        "--sqlite",
        default=os.environ.get("SEARCH_LEDGER_PATH") or DEFAULT_SEARCH_LEDGER,
        help="Type-bound search_event_v1 SQLite ledger to read",
    )
    searches_parser.add_argument(
        "--days",
        type=_bounded_search_days,
        default=DEFAULT_SEARCH_SUMMARY_DAYS,
        help=(
            f"Calendar days back from today, UTC "
            f"(1-{MAX_SEARCH_SUMMARY_DAYS}; default: {DEFAULT_SEARCH_SUMMARY_DAYS})"
        ),
    )
    searches_parser.set_defaults(func=cmd_searches)

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
        "--search-ledger",
        default=None,
        help="Private canonical web search/extract SQLite/JSONL ledger",
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
