"""Cross-harness collection orchestration.

Collectors are read-only with respect to harness stores. The only writes are the
canonical usage, quota, and optional billing ledgers supplied by the caller.
"""
from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import time
import types
from typing import Any, Callable, Iterable, Mapping

from .claude_instances import (
    ClaudeInstance,
    default_claude_credentials_path,
    normalize_claude_instances,
)
from .canonical_ledger import (
    AppendResult,
    ConflictSummary,
    TOKEN_FIELDS,
    ValidationError,
    _jcs_dumps,
    _read_facts_without_side_effects,
    append_facts,
    append_facts_quarantined,
    append_facts_quarantined_reconciled,
    append_sqlite_facts,
    append_sqlite_facts_quarantined,
    append_sqlite_facts_quarantined_reconciled,
    canonical_json,
    normalize_fact,
    query_sqlite_facts,
)
from .quota import (
    ClaudeProbeBlocked,
    ClaudeTokenExpired,
    codex_quota_observations,
    collect_claude_code_quota,
    collect_claude_code_quota_api,
    collect_deepseek_quota,
    collect_kimi_code_quota,
    collect_opencode_go_quota as collect_exact_opencode_go_quota,
    collect_opencode_go_usage_quota,
    collect_openrouter_quota,
    collect_z_ai_quota,
)
from .search_events import (
    DEFAULT_SEARCH_LEDGER,
    SEARCH_LANE,
    SEARCH_QUOTA_LANE,
    _watermark_path,
    collect_hermes_search_events,
    default_search_audit_path,
    derive_search_quota_observations,
    read_watermark,
    write_watermark,
)
from .usage_adapters import collect_claude_usage, collect_hermes_usage, collect_opencode_usage
from .openclaw_usage import (
    OPENCLAW_SEARCH_LANE,
    OPENCLAW_USAGE_LANE,
    collect_openclaw_searches,
    collect_openclaw_usage,
)

ALLOWED_DOTENV_KEYS = frozenset(
    {
        "OPENROUTER_API_KEY",
        "DEEPSEEK_API_KEY",
        "KIMI_CODING_API_KEY",
        "Z_AI_API_KEY",
        "ZAI_API_KEY",
        "OPENCODE_GO_WORKSPACE_ID",
        "OPENCODE_GO_AUTH_COOKIE",
        "OPENCODE_GO_API_KEY",
    }
)


def load_allowed_dotenv(
    path: str | Path | None,
    *,
    allowed_keys: Iterable[str] = ALLOWED_DOTENV_KEYS,
    environ: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Parse only explicitly allowed dotenv keys without expansion or execution.

    Existing environment values win. ``export`` is accepted, but interpolation,
    command substitution, multiline values, and arbitrary dotenv keys are not.
    """
    result = dict(environ if environ is not None else os.environ)
    if path is None:
        return result
    target = Path(path).expanduser()
    if not target.exists():
        return result
    allowed = set(allowed_keys)
    for number, raw in enumerate(target.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            raise ValueError(f"{target}:{number}: expected KEY=VALUE")
        key, value = line.split("=", 1)
        key = key.strip()
        if key not in allowed:
            continue
        value = value.strip()
        if "\n" in value or "\r" in value:
            raise ValueError(f"{target}:{number}: multiline values are not allowed")
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        # Values stay literal: no ${...}, backtick, or $(...) evaluation.
        result.setdefault(key, value)
    return result


def _source_result(discovered: int) -> dict[str, int]:
    return {"discovered": discovered}


def _collect_claude_quota_with_probe_fallback(
    namespace: str,
    *,
    credentials_path: str | Path,
    claude_command: str | Path,
    probe_dir: str | Path,
    timeout: float,
    claude_config_dir: str | Path | None = None,
    account_ref: str | None = None,
) -> list[dict[str, Any]]:
    """Collect Claude quota from the OAuth usage API, tmux probe on expiry only.

    The OAuth usage API is the primary surface (keeper-ruled 2026-08-29). Only
    an expired access token — detected locally before the request or reported
    by a live 401 — falls back to the tmux ``/usage`` probe: launching the
    real CLI refreshes the OAuth token as a side effect, and that CLI-owned
    refresh is deliberately the tracker's only refresh mechanism.

    Every retry this lane gets lives inside this call: the API collector's
    single transport retry, and the probe's own bounded ``TimeoutError``
    retries. The lane wrapper runs it exactly once (``retries_inside``) so
    a blocked or timed-out probe is never relaunched 3x3 times, and a
    ``ClaudeProbeBlocked`` screen is terminal — the lane cedes on first
    sight. Every other failure propagates and cedes the lane.
    """
    try:
        return collect_claude_code_quota_api(
            namespace,
            credentials_path=credentials_path,
            account_ref=account_ref,
        )
    except ClaudeTokenExpired:
        return collect_claude_code_quota(
            namespace,
            claude_command=claude_command,
            probe_dir=probe_dir,
            timeout=timeout,
            claude_config_dir=claude_config_dir,
            account_ref=account_ref,
        )


def _ledger_backend(path: str | Path) -> str:
    suffix = Path(path).expanduser().suffix.casefold()
    if suffix == ".jsonl":
        return "jsonl"
    if suffix in {".sqlite3", ".sqlite", ".db"}:
        return "sqlite"
    raise ValueError(f"unsupported canonical ledger suffix: {suffix or '<none>'}")


def _append_canonical_ledger(
    path: str | Path,
    facts: Iterable[dict[str, Any]],
    *,
    fact_type: str,
    dry_run: bool,
):
    """Dispatch only explicitly supported ledger suffixes with a type binding."""
    backend = _ledger_backend(path)
    rows = list(facts)
    if backend == "sqlite":
        return append_sqlite_facts(path, rows, fact_type=fact_type, dry_run=dry_run)
    if any(not isinstance(row, dict) or row.get("fact_type") != fact_type for row in rows):
        raise ValidationError(f"ledger requires fact_type {fact_type}")
    return append_facts(path, rows, dry_run=dry_run)


def _append_canonical_ledger_quarantined(
    path: str | Path,
    facts: Iterable[dict[str, Any]],
    *,
    fact_type: str,
    dry_run: bool,
    equivalent_changed_fields: frozenset[str] = frozenset(),
) -> tuple[AppendResult, tuple[ConflictSummary, ...]]:
    """Persist one source batch without weakening the strict ledger APIs."""
    backend = _ledger_backend(path)
    rows = list(facts)
    if backend == "sqlite":
        return append_sqlite_facts_quarantined(
            path,
            rows,
            fact_type=fact_type,
            dry_run=dry_run,
            equivalent_changed_fields=equivalent_changed_fields,
        )
    if any(not isinstance(row, dict) or row.get("fact_type") != fact_type for row in rows):
        raise ValidationError(f"ledger requires fact_type {fact_type}")
    return append_facts_quarantined(
        path,
        rows,
        dry_run=dry_run,
        equivalent_changed_fields=equivalent_changed_fields,
    )


def _append_canonical_ledger_quarantined_reconciled(
    path: str | Path,
    facts: Iterable[dict[str, Any]],
    *,
    fact_type: str,
    dry_run: bool,
    reconcile: Any,
    equivalent_changed_fields: frozenset[str] = frozenset(),
    reconcile_existing: Any = None,
) -> tuple[AppendResult, tuple[ConflictSummary, ...], Any]:
    """Run semantic reconciliation inside the canonical writer boundary."""
    backend = _ledger_backend(path)
    rows = list(facts)
    if backend == "sqlite":
        return append_sqlite_facts_quarantined_reconciled(
            path,
            rows,
            fact_type=fact_type,
            reconcile=reconcile,
            dry_run=dry_run,
            equivalent_changed_fields=equivalent_changed_fields,
            reconcile_existing=reconcile_existing,
        )
    if any(not isinstance(row, dict) or row.get("fact_type") != fact_type for row in rows):
        raise ValidationError(f"ledger requires fact_type {fact_type}")
    return append_facts_quarantined_reconciled(
        path,
        rows,
        reconcile=reconcile,
        dry_run=dry_run,
        equivalent_changed_fields=equivalent_changed_fields,
    )


def _aggregate_append_results(
    batches: Iterable[tuple[AppendResult, int]],
) -> dict[str, int]:
    discovered = appended = replayed = quarantined = 0
    for result, conflict_count in batches:
        discovered += result.discovered
        appended += result.appended
        replayed += result.replayed
        quarantined += conflict_count
    summary = {
        "discovered": discovered,
        "appended": appended,
        "replayed": replayed,
    }
    if quarantined:
        summary["quarantined"] = quarantined
    return summary


def _validate_distinct_ledger_paths(bindings: list[tuple[str | Path, str]]) -> None:
    resolved: list[tuple[Path, str]] = []
    for raw_path, fact_type in bindings:
        path = Path(raw_path).expanduser()
        canonical = path.resolve(strict=False)
        for other, other_type in resolved:
            aliases = canonical == other
            if not aliases and os.path.lexists(path) and os.path.lexists(other):
                try:
                    aliases = os.path.samefile(path, other)
                except OSError:
                    aliases = False
            if aliases:
                raise ValueError(
                    "canonical usage, quota, billing, and search ledgers must use "
                    f"distinct paths ({fact_type}, {other_type})"
                )
        resolved.append((canonical, fact_type))


def _preflight_ledger_binding(path: str | Path, fact_type: str, *, dry_run: bool) -> None:
    target = Path(path).expanduser()
    backend = _ledger_backend(target)
    if not os.path.lexists(target):
        return
    if backend == "sqlite":
        if dry_run:
            append_sqlite_facts(target, [], fact_type=fact_type, dry_run=True)
        else:
            query_sqlite_facts(target, fact_type=fact_type, limit=0)
        return
    rows = _read_facts_without_side_effects(target)
    if any(row.get("fact_type") != fact_type for row in rows):
        raise ValidationError(f"ledger requires fact_type {fact_type}")


_CLAUDE_FINALIZATION_MUTABLE_FIELDS = frozenset(
    {"occurred_at", "recorded_at", "request_status", *TOKEN_FIELDS}
)


def _fact_sha256(row: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_json(dict(row)).encode("utf-8")).hexdigest()


def _identity_sha256(row: Mapping[str, Any]) -> str:
    """Hash the identity exactly as canonical_ledger's ConflictSummary does.

    Both hash the JCS encoding of ``[namespace, id]`` so an adjudication
    recorded here matches the identity a quarantined conflict reports.
    """
    namespace = str(row.get("source_namespace") or "")
    event_id = str(row.get("source_event_id") or "")
    return hashlib.sha256(_jcs_dumps([namespace, event_id]).encode("utf-8")).hexdigest()


_ADJUDICATION_STORE_NAME = "adjudications.sqlite3"
_HEALTH_DIR_NAME = "health"
_ADJUDICATION_SCHEMA = """
CREATE TABLE IF NOT EXISTS adjudications (
    identity_sha256 TEXT NOT NULL,
    incoming_sha256 TEXT NOT NULL,
    source TEXT NOT NULL,
    resolution TEXT NOT NULL,
    existing_sha256 TEXT NOT NULL,
    changed_fields TEXT NOT NULL,
    first_recorded_at TEXT NOT NULL,
    PRIMARY KEY (identity_sha256, incoming_sha256)
)
"""


def _default_adjudication_store(ledger: str | Path) -> Path:
    return Path(ledger).expanduser().parent / _ADJUDICATION_STORE_NAME


def _default_report_dir(ledger: str | Path) -> Path:
    return Path(ledger).expanduser().parent / _HEALTH_DIR_NAME


def record_adjudications(
    store: str | Path,
    resolutions: list[dict[str, Any]],
    *,
    now: str | None = None,
) -> list[dict[str, Any]]:
    """Record each (identity, incoming sha) adjudication exactly once.

    This is a tracker-private sidecar, deliberately outside the canonical
    ledger schema: it holds no facts, only the durable statement that a
    conflicting re-derivation was already adjudicated. It runs after the
    canonical writer released its transaction so a rolled-back append can
    never leave a phantom adjudication behind.
    """
    if not resolutions:
        return []
    target = Path(store).expanduser()
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    timestamp = now or datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    connection = sqlite3.connect(target)
    try:
        os.chmod(target, 0o600)
        connection.execute("PRAGMA busy_timeout = 30000")
        connection.execute("PRAGMA journal_mode = DELETE")
        connection.execute(_ADJUDICATION_SCHEMA)
        recorded: list[dict[str, Any]] = []
        with connection:
            for resolution in resolutions:
                key = (
                    str(resolution.get("identity_sha256")),
                    str(resolution.get("incoming_sha256")),
                )
                existing = connection.execute(
                    "SELECT first_recorded_at FROM adjudications"
                    " WHERE identity_sha256 = ? AND incoming_sha256 = ?",
                    key,
                ).fetchone()
                if existing is None:
                    connection.execute(
                        "INSERT INTO adjudications(identity_sha256, incoming_sha256, source,"
                        " resolution, existing_sha256, changed_fields, first_recorded_at)"
                        " VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (
                            *key,
                            str(resolution.get("source") or ""),
                            str(resolution.get("resolution") or ""),
                            str(resolution.get("existing_sha256") or ""),
                            json.dumps(list(resolution.get("changed_fields") or [])),
                            timestamp,
                        ),
                    )
                    first_recorded_at = timestamp
                    newly_recorded = True
                else:
                    first_recorded_at = str(existing[0])
                    newly_recorded = False
                recorded.append(
                    {
                        **resolution,
                        "first_recorded_at": first_recorded_at,
                        "newly_recorded": newly_recorded,
                    }
                )
        return recorded
    finally:
        connection.close()
        if os.path.lexists(target):
            os.chmod(target, 0o600)


SCOPE_HEARTBEATS = {
    "usage": "usage-collect.ok",
    "quota": "quota-collect.ok",
    "searches": "search-collect.ok",
}
LANE_HEALTH_SCHEMA_VERSION = 1

# A failing lane is retried before it is ceded (keeper-ruled 2026-08-29):
# three attempts total, with a short growing pause between them
# (backoff * attempt_number → 2s then 4s by default). Tests inject their
# own ``lane_sleep`` through ``collect_all``.
LANE_ATTEMPTS = 3
LANE_BACKOFF_SECONDS = 2.0


def _default_lane_sleep(seconds: float) -> None:
    time.sleep(seconds)


class EmptyLaneResult(ValueError):
    """A ``require_rows`` lane read deterministically returned nothing.

    Terminal at the lane level: an empty exact result is not a transient
    fault, so it short-circuits to the estimated fallback without backoff.
    """


# Exceptions the lane wrapper never retries: the screen (or emptiness) will
# not change on its own, and every retry would only burn its full timeout.
_LANE_TERMINAL_EXCEPTIONS: tuple[type[BaseException], ...] = (
    ClaudeProbeBlocked,
    EmptyLaneResult,
)

DEFAULT_USAGE_LEDGER = "~/.local/state/codex-usage-tracker/usage_events.sqlite3"


def _materialize_lane_read(value: Any) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Materialize a lane read, keeping a generator adapter's return value.

    Adapters that are generators (Claude usage) return per-run diagnostics —
    ``{"skipped_files": [...]}`` — as their ``StopIteration`` value, which a
    plain ``list()`` would discard. Plain iterables carry no diagnostics.
    """
    if not isinstance(value, types.GeneratorType):
        return list(value), {}
    rows: list[dict[str, Any]] = []
    while True:
        try:
            rows.append(next(value))
        except StopIteration as stop:
            diagnostics = stop.value if isinstance(stop.value, dict) else {}
            return rows, diagnostics


# Lanes a scope no longer runs, by scope, with the reason. Lane health keeps
# every lane it has seen so a source that silently stops stays visible; a
# lane removed on purpose must be retired here or it reads stale forever.
RETIRED_LANES: dict[str, dict[str, str]] = {
    "quota": {
        "opencode": (
            "retired 2026-09-15 (aa583c3): OpenCode Go quota moved to the native "
            "opencode_go_quota lane"
        ),
    },
}


def lane_health_name(scope: str) -> str:
    return f"lanes-{scope}.json"


def _write_health_json(directory: Path, name: str, payload: Mapping[str, Any]) -> Path:
    target = directory / name
    temporary = directory / f".{name}.tmp"
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.chmod(temporary, 0o644)
    os.replace(temporary, target)
    return target


def write_collect_report(report_dir: str | Path, report: Mapping[str, Any]) -> Path:
    """Persist one per-run collection diagnostic report atomically."""
    directory = Path(report_dir).expanduser()
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    scope = str(report.get("scope") or "all")
    return _write_health_json(directory, f"collect-{scope}-last.json", report)


def update_lane_health(
    report_dir: str | Path,
    scope: str,
    lanes: Mapping[str, Mapping[str, Any]],
    *,
    now: str | None = None,
) -> Path:
    """Merge this run's per-lane outcome into the durable lane health file.

    The per-run report is overwritten every run, so a lane that failed today
    would erase the record of when it last worked. Lane health is therefore
    read-modify-write: ``last_success_at`` survives failing runs, and a lane
    absent from this run (a source that is no longer configured) keeps its last
    known state instead of silently disappearing.
    """
    directory = Path(report_dir).expanduser()
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    name = lane_health_name(scope)
    timestamp = now or datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    previous: dict[str, Any] = {}
    try:
        loaded = json.loads((directory / name).read_text(encoding="utf-8"))
        if isinstance(loaded, dict) and isinstance(loaded.get("lanes"), dict):
            previous = {
                str(key): value
                for key, value in loaded["lanes"].items()
                if isinstance(value, dict)
            }
    except (OSError, ValueError):
        previous = {}
    merged: dict[str, Any] = dict(previous)
    # A lane this scope deliberately stopped running is recorded as retired,
    # with the reason, instead of reading "stale" forever (keeper, 2026-09-25:
    # "why does it always say opencode is stale?").
    for lane, reason in RETIRED_LANES.get(scope, {}).items():
        if lane in merged and lane not in lanes:
            merged[lane] = {**merged[lane], "retired": reason}
    for lane, outcome in lanes.items():
        prior = previous.get(lane) if isinstance(previous.get(lane), dict) else {}
        succeeded = bool(outcome.get("succeeded"))
        merged[lane] = {
            "last_run_at": timestamp,
            "last_success_at": (
                timestamp if succeeded else prior.get("last_success_at")
            ),
            "last_error_class": outcome.get("last_error_class"),
            "rows_written": int(outcome.get("rows_written") or 0),
            "quarantined_count": int(outcome.get("quarantined_count") or 0),
            "discovered": int(outcome.get("discovered") or 0),
            # Transcripts this run skipped for a malformed interior line
            # (Claude usage lanes); the lane still lands its other files.
            "skipped_files": int(outcome.get("skipped_files") or 0),
            # A ceded lane exhausted its retries this run and was skipped so
            # the rest of the collection could land. ``last_success_at``
            # deliberately does not advance — freshness and the watchdog key
            # off it.
            "ceded": bool(outcome.get("ceded")),
        }
    return _write_health_json(
        directory,
        name,
        {
            "schema_version": LANE_HEALTH_SCHEMA_VERSION,
            "scope": scope,
            "updated_at": timestamp,
            "lanes": merged,
        },
    )


def mark_run_started(report_dir: str | Path, scope: str) -> list[Path]:
    """Touch ``<heartbeat>.started`` when a scope's run begins.

    Readers judge staleness by one collector tick (keeper, 2026-09-25, Discord
    unified usage ledger thread: "staleness should only be reported if it is
    over 15 minutes, or one ledger tick"). A usage run takes 8-25 minutes, so
    a heartbeat older than a tick is only a missed tick when no run has
    started since; this marker is how readers tell the two apart.
    """
    directory = Path(report_dir).expanduser()
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    names = (
        list(SCOPE_HEARTBEATS.values())
        if scope == "all"
        else [SCOPE_HEARTBEATS[scope]] if scope in SCOPE_HEARTBEATS else []
    )
    touched: list[Path] = []
    for name in names:
        target = directory / f"{name.removesuffix('.ok')}.started"
        with open(target, "a", encoding="utf-8"):
            pass
        os.utime(target, None)
        os.chmod(target, 0o644)
        touched.append(target)
    return touched


def touch_heartbeat(report_dir: str | Path, scope: str) -> list[Path]:
    """Touch the ``.ok`` heartbeats for a scope whose canonical writes landed.

    The heartbeat means "this scope's canonical ledger append completed", not
    "every lane was perfect". A single quarantined identity used to suppress
    the heartbeat for days because the nix runner only touched the file after a
    zero exit, which made the watchdog and the dashboard report a dead pipeline
    while every other lane was writing normally.
    """
    directory = Path(report_dir).expanduser()
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    names = (
        list(SCOPE_HEARTBEATS.values())
        if scope == "all"
        else [SCOPE_HEARTBEATS[scope]] if scope in SCOPE_HEARTBEATS else []
    )
    touched: list[Path] = []
    for name in names:
        target = directory / name
        with open(target, "a", encoding="utf-8"):
            pass
        os.utime(target, None)
        os.chmod(target, 0o644)
        touched.append(target)
    return touched


def _load_reconcile_window(
    connection: sqlite3.Connection,
    incoming: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Read matching baselines by index, then corrections in one pass.

    Corrections may have arbitrary identities and harnesses. All corrections
    targeting the batch must be considered, not just generated suffixes.
    The fixed v1 schema has no correction-target index: scan that JSON field
    once per batch, NOT once per 500 incoming identities. No schema migration
    or changed accounting semantics are needed.
    """
    from .canonical_ledger import _integrity_check_stored_row

    identities = sorted({
        (str(row.get("source_namespace")), str(row.get("source_event_id")))
        for row in incoming
    })
    if not identities:
        return []
    columns = (
        "f.canonical_json, f.canonical_sha256, f.fact_type, f.source_namespace, "
        "f.source_identity, f.occurred_or_observed_at, f.provider, f.harness, "
        "f.purpose, f.model, f.quota_name, f.account_ref, f.transaction_kind, "
        "f.transaction_status, f.invoice_id, f.line_item_id"
    )
    found: dict[tuple[str, str], dict[str, Any]] = {}
    for start in range(0, len(identities), 400):
        window = identities[start : start + 400]
        placeholders = ",".join("(?,?)" for _ in window)
        # A VALUES-driven join produces indexed point lookups on older SQLite
        # too; a row-value IN list can instead produce a full table scan.
        query = (
            f"WITH wanted(ns, id) AS (VALUES {placeholders}) SELECT {columns} "
            "FROM wanted CROSS JOIN facts AS f "
            "WHERE f.source_namespace = wanted.ns AND f.source_identity = wanted.id"
        )
        for stored in connection.execute(query, [v for pair in window for v in pair]):
            row = _integrity_check_stored_row(stored, "usage_event_v1")
            found[(row["source_namespace"], row["source_event_id"])] = row

    targets = set(identities)
    for stored in connection.execute(
        f"SELECT {columns} FROM facts AS f "
        "WHERE json_extract(f.canonical_json, '$.record_kind') = 'correction' "
        "ORDER BY f.ingestion_sequence"
    ):
        row = _integrity_check_stored_row(stored, "usage_event_v1")
        if (row.get("corrects_source_namespace"), row.get("corrects_source_event_id")) in targets:
            found[(row["source_namespace"], row["source_event_id"])] = row
    return list(found.values())


def _reconcile_finalized_claude_rows(
    existing: list[dict[str, Any]],
    rows: Iterable[dict[str, Any]],
) -> tuple[list[dict[str, Any]], tuple[int, list[dict[str, Any]]]]:
    """Converge Claude stream re-derivations against the canonical snapshot.

    The caller supplies matching baselines and all corrections targeting them
    from one snapshot while holding the writer boundary. The original attempt
    remains immutable; monotonic growth is
    expressed only as deterministic signed token deltas. A strictly poorer
    re-derivation (a source regression, e.g. a transcript that lost its terminal
    receipt to a torn write) keeps the canonical fact and is adjudicated instead
    of quarantined. Differing incoming variants, mixed-direction changes,
    unrelated mutations, and non-generated corrections stay quarantined.
    """
    incoming = [normalize_fact(row) for row in rows]
    existing = [normalize_fact(row) for row in existing]
    if not existing:
        return incoming, (0, [])

    baselines: dict[tuple[str, str], dict[str, Any]] = {}
    corrections: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in existing:
        if row.get("record_kind") == "correction":
            target_namespace = row.get("corrects_source_namespace")
            target_event = row.get("corrects_source_event_id")
            if isinstance(target_namespace, str) and isinstance(target_event, str):
                corrections.setdefault((target_namespace, target_event), []).append(row)
            continue
        identity = (str(row.get("source_namespace")), str(row.get("source_event_id")))
        baselines[identity] = row

    grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in incoming:
        identity = (str(row.get("source_namespace")), str(row.get("source_event_id")))
        grouped.setdefault(identity, []).append(row)

    reconciled: list[dict[str, Any]] = []
    generated = 0
    resolutions: list[dict[str, Any]] = []
    for identity, variants in grouped.items():
        if len({canonical_json(row) for row in variants}) > 1:
            # Preserve all variants so the canonical writer quarantines the
            # ambiguous identity instead of manufacturing multiple deltas.
            reconciled.extend(variants)
            continue
        row = variants[0]
        baseline = baselines.get(identity)
        if baseline is None:
            reconciled.extend(variants)
            continue
        changed_fields = sorted(
            key for key in set(baseline) | set(row) if baseline.get(key) != row.get(key)
        )
        target_corrections = corrections.get(identity, [])
        safe_generated_corrections = all(
            correction.get("x_claude_stream_finalization") is True
            for correction in target_corrections
        )
        mutable = (
            bool(changed_fields)
            and set(changed_fields).issubset(_CLAUDE_FINALIZATION_MUTABLE_FIELDS)
            and baseline.get("harness") == "claude_code"
            and row.get("harness") == "claude_code"
            and safe_generated_corrections
        )
        # The effective state is the immutable baseline plus every generated
        # delta correction already recorded against it.
        deltas: dict[str, int] = {}
        for field in TOKEN_FIELDS:
            effective = int(baseline.get(field) or 0) + sum(
                int(correction.get(field) or 0) for correction in target_corrections
            )
            deltas[field] = int(row.get(field) or 0) - effective
        effective_status = (
            "ok"
            if any(
                correction.get("x_final_request_status") == "ok"
                for correction in target_corrections
            )
            else baseline.get("request_status")
        )
        incoming_status = row.get("request_status")
        tokens_grew = any(delta > 0 for delta in deltas.values())
        tokens_shrank = any(delta < 0 for delta in deltas.values())
        finalizing = (
            mutable
            and baseline.get("request_status") == "unknown"
            and incoming_status == "ok"
            and not tokens_shrank
        )
        # A source regression is strictly lesser AND unable to claim
        # completion: nothing grew, the incoming row is not ``ok``, and either
        # a token field shrank or a settled ``ok`` receipt was lost. Two
        # differing rows that both claim ``ok`` are competing observations, not
        # a regression, and stay quarantined.
        regressing = (
            mutable
            and not finalizing
            and not tokens_grew
            and incoming_status != "ok"
            and (tokens_shrank or effective_status == "ok")
        )
        if not (finalizing or regressing):
            reconciled.extend(variants)
            continue

        # Replay one immutable baseline per exact incoming duplicate so the
        # generic writer preserves its discovered/replayed accounting.
        reconciled.extend([baseline] * len(variants))
        incoming_sha = _fact_sha256(row)
        if regressing:
            # The canonical fact is richer than the source can now prove. Keep
            # it, and let the caller adjudicate this (identity, incoming sha)
            # once so the identity stops failing strict runs.
            resolutions.append(
                {
                    "source": "claude_code",
                    "resolution": "source_regression",
                    "identity_sha256": _identity_sha256(row),
                    "existing_sha256": _fact_sha256(baseline),
                    "incoming_sha256": incoming_sha,
                    "changed_fields": changed_fields,
                }
            )
            continue
        # A prior generated finalization proves idempotence when the incoming
        # totals already match the effective state; only further monotonic
        # growth earns an additional delta correction.
        if not target_corrections or tokens_grew:
            finalization_suffix = (
                "stream-finalization"
                if not target_corrections
                else f"stream-finalization:{incoming_sha[:16]}"
            )
            correction = dict(row)
            correction.update(
                {
                    # The correction is the rest of the SAME turn: it belongs
                    # in the baseline's time bucket, not the finalization's
                    # (a turn streaming at 13:59:50Z and finalizing at
                    # 14:00:20Z otherwise puts most of its tokens in the next
                    # hour, and a public-projection window edge between the
                    # pair loses the correction's attribution). recorded_at
                    # stays the finalization time.
                    "occurred_at": baseline["occurred_at"],
                    "source_event_id": f"{row['source_event_id']}:{finalization_suffix}",
                    "event_uid": None,
                    "logical_call_id": None,
                    "provider_request_id": None,
                    "record_kind": "correction",
                    "request_status": None,
                    "error_class": None,
                    "latency_ms": None,
                    "attempt_no": None,
                    "reconstructed_call_count": None,
                    "estimated_cost_usd": None,
                    "actual_cost_usd": None,
                    "cost_source": None,
                    "pricing_version": None,
                    "cost_status": "included",
                    "corrects_source_namespace": row["source_namespace"],
                    "corrects_source_event_id": row["source_event_id"],
                    "x_claude_stream_finalization": True,
                    "x_final_request_status": "ok",
                    "x_finalized_source_sha256": incoming_sha,
                }
            )
            for field, delta in deltas.items():
                correction[field] = delta
            reconciled.append(normalize_fact(correction))
            generated += 1
        resolutions.append(
            {
                "source": "claude_code",
                "resolution": "canonical_correction",
                "identity_sha256": _identity_sha256(row),
                "existing_sha256": _fact_sha256(baseline),
                "incoming_sha256": incoming_sha,
                "changed_fields": changed_fields,
            }
        )
    return reconciled, (generated, resolutions)


class StrictSourcesError(RuntimeError):
    """Strict-source policy failure carrying sanitized lane diagnostics.

    Since the graceful-lane ruling (2026-08-29), a lane that fails to read
    or persist is retried and then ceded — it degrades the run's lane
    accounting but never fails the run. This exception is therefore raised
    only for unadjudicated identity conflicts: quarantined re-derivations
    that retrying cannot resolve and that require adjudication.

    The CLI prints the exception type only for arbitrary failures because
    exception messages can embed credentials and private paths. This
    subclass carries the structured warnings payload (source names and
    error type names) which is safe to surface: one journal line then
    names the failing lane instead of a bare RuntimeError.

    ``report_path`` points at the per-run diagnostic report (conflict
    identities, changed fields, and adjudications) so the journal line can
    route an operator to the full detail without embedding it.
    """

    def __init__(self, warnings: list[dict[str, str]], *, report_path: str | None = None):
        super().__init__("collection incomplete under strict source policy")
        self.warnings = warnings
        self.report_path = report_path


# Active-window tick freshness: the five-minute timer must not re-probe an
# account whose latest observation is newer than this (the tick and the
# hourly collector can race near a cadence boundary; the ledger already
# carries the answer).
_BURN_TICK_FRESH_SECONDS = 180


def _claude_window_is_burning(
    quota_ledger: str | Path,
    *,
    account_ref: str | None,
    now: datetime,
) -> bool:
    """True when this Claude account's five-hour window shows active burn.

    Read-only against the live quota ledger: the account's newest five_hour
    observation must report usage, carry a ``resets_at`` still in the
    future, and be older than the tick-freshness bound. An absent or
    unreadable ledger reads as not burning — the tick stands down rather
    than probing on a guess (the hourly collector owns discovery).
    """
    target = Path(quota_ledger).expanduser()
    if target.suffix != ".sqlite3" or not target.exists():
        return False
    filters = {"provider": "anthropic", "quota_name": "five_hour"}
    if account_ref:
        filters["account_ref"] = account_ref
    try:
        rows = query_sqlite_facts(
            target,
            fact_type="quota_observation_v1",
            filters=filters,
            order="desc",
            # Several accounts share the newest timestamps; the exact
            # account match is applied below because a NULL account_ref
            # cannot be expressed as a string equality filter.
            limit=20,
            contract_validation=False,
        )
    except Exception:
        return False
    # Exact account identity: the primary account's series carries a NULL
    # account_ref, so an omitted filter must not let another account's
    # observation decide this account's gate.
    rows = [
        row
        for row in rows
        if (row.get("account_ref") or None) == (account_ref or None)
    ]
    if not rows:
        return False
    row = rows[0]
    used = row.get("used_value")
    try:
        if used is None or Decimal(str(used)) <= 0:
            return False
    except Exception:
        return False
    resets_at = row.get("resets_at")
    if not isinstance(resets_at, str):
        return False
    try:
        parsed_reset = datetime.fromisoformat(resets_at.replace("Z", "+00:00"))
    except ValueError:
        return False
    if parsed_reset.tzinfo is None or parsed_reset <= now:
        return False
    observed_at = row.get("observed_at")
    if not isinstance(observed_at, str):
        return False
    try:
        parsed_observed = datetime.fromisoformat(observed_at.replace("Z", "+00:00"))
    except ValueError:
        return False
    if parsed_observed.tzinfo is None:
        return False
    age = (now - parsed_observed).total_seconds()
    # Fresh-enough observations (the hourly collector just wrote one) stand
    # the tick down; a genuinely older observation means the window is
    # burning on tick cadence and needs a new sample.
    return age > _BURN_TICK_FRESH_SECONDS


def collect_all(
    *,
    state_db: str | Path,
    claude_root: str | Path = "~/.claude/projects",
    claude_instances: (
        Iterable[ClaudeInstance | Mapping[str, Any] | tuple[Any, Any]]
        | Mapping[str, Any]
        | None
    ) = None,
    opencode_dbs: Iterable[str | Path],
    codex_ledger: str | Path,
    usage_ledger: str | Path,
    quota_ledger: str | Path,
    billing_ledger: str | Path | None = None,
    dotenv: str | Path | None = None,
    claude_quota_command: str | Path | None = None,
    claude_probe_dir: str | Path = "~/.local/state/codex-usage-tracker/claude-probe",
    claude_quota_timeout: float = 45.0,
    live_quota: bool = True,
    dry_run: bool = False,
    environment: Mapping[str, str] | None = None,
    source_prefix: str = "sol",
    scope: str = "all",
    codex_quota_history: bool = False,
    codex_quota_history_since: str | None = None,
    strict_sources: bool = False,
    adjudication_store: str | Path | None = None,
    report_dir: str | Path | None = None,
    lane_attempts: int = LANE_ATTEMPTS,
    lane_backoff_seconds: float = LANE_BACKOFF_SECONDS,
    lane_sleep: Callable[[float], None] | None = None,
    quota_lanes: Iterable[str] | None = None,
    when_active: bool = False,
    search_ledger: str | Path | None = None,
    search_audit: str | Path | None = None,
    openclaw_home: str | Path | None = None,
    usage_since: float | None = None,
    claude_retry_files: Iterable[str] = (),
    retry_files_out: list[str] | None = None,
) -> dict[str, Any]:
    """Collect independently persistable usage and/or quota source batches.

    A lane that fails to read or persist is retried (``lane_attempts`` total
    attempts, pausing ``lane_backoff_seconds * attempt_number`` between them)
    and then ceded: skipped for this run while every remaining lane still
    collects, persists, and reports. The run itself always completes — even
    when every lane cedes — so the heartbeat and per-lane health stay honest
    and the external watchdog owns the alerting.
    """
    configured_claude_instances = normalize_claude_instances(claude_instances)
    claude_usage_source_keys = (
        {instance.source_key for instance in configured_claude_instances}
        if configured_claude_instances
        else {"claude_code"}
    )
    if scope not in {"all", "usage", "quota", "searches"}:
        raise ValueError("scope must be one of: all, usage, quota, searches")
    if codex_quota_history:
        if scope != "quota":
            raise ValueError("Codex quota history requires scope quota")
        if live_quota:
            raise ValueError("Codex quota history requires live quota to be disabled")
        if not codex_quota_history_since:
            raise ValueError("Codex quota history requires history-since")
    elif codex_quota_history_since is not None:
        raise ValueError("history-since requires Codex quota history")
    collect_usage_scope = scope in {"all", "usage"}
    collect_quota_scope = scope in {"all", "quota"}
    collect_search_scope = scope in {"all", "searches"}
    search_target = Path(
        DEFAULT_SEARCH_LEDGER if search_ledger is None else search_ledger
    ).expanduser()
    audit_target = (
        Path(search_audit).expanduser()
        if search_audit is not None
        else default_search_audit_path(environment)
    )

    # Reject aliasing and wrong pre-existing type bindings before reading any
    # in-scope source or writing any in-scope destination.
    bindings: list[tuple[str | Path, str]] = []
    if collect_usage_scope:
        bindings.append((usage_ledger, "usage_event_v1"))
    if collect_quota_scope:
        bindings.append((quota_ledger, "quota_observation_v1"))
    if scope == "all" and billing_ledger is not None:
        bindings.append((billing_ledger, "billing_fact_v1"))
    if collect_search_scope:
        bindings.append((search_target, "search_event_v1"))
        # The search lane derives quota observations from rate-limited events,
        # so the quota ledger is bound and preflighted even when the quota
        # scope is not the one running.
        if not collect_quota_scope:
            bindings.append((quota_ledger, "quota_observation_v1"))
    _validate_distinct_ledger_paths(bindings)
    for ledger_path, ledger_fact_type in bindings:
        _ledger_backend(ledger_path)
        _preflight_ledger_binding(ledger_path, ledger_fact_type, dry_run=dry_run)
    if not dry_run:
        try:
            mark_run_started(
                Path(report_dir).expanduser()
                if report_dir is not None
                else _default_report_dir(
                    usage_ledger
                    if collect_usage_scope
                    else quota_ledger if collect_quota_scope else search_target
                ),
                scope,
            )
        except OSError:
            pass  # a read-only health directory must not fail the collection

    env = (
        load_allowed_dotenv(dotenv, environ=environment)
        if collect_quota_scope and not codex_quota_history
        else {}
    )
    sources: dict[str, dict[str, int]] = {}
    lanes: dict[str, dict[str, Any]] = {}
    warnings: list[dict[str, str]] = []
    conflicts: list[dict[str, Any]] = []
    stabilized_replays: list[dict[str, Any]] = []
    resolutions: list[dict[str, Any]] = []
    generated_corrections = 0

    sleep = lane_sleep if lane_sleep is not None else _default_lane_sleep
    attempts = max(1, int(lane_attempts))

    def _lane(source: str) -> dict[str, Any]:
        return lanes.setdefault(
            source,
            {
                "succeeded": False,
                "last_error_class": None,
                "rows_written": 0,
                "quarantined_count": 0,
                "discovered": 0,
                "skipped_files": 0,
                "ceded": False,
            },
        )

    def _attempt_lane(
        operation: Callable[[], Any], *, retries_inside: bool = False
    ) -> tuple[Any, str | None]:
        """Run one lane operation with bounded retries and growing backoff.

        Returns ``(value, None)`` on success or ``(None, error_class)`` after
        the final attempt fails. Only the exception's type name escapes —
        messages can embed credentials and private paths.

        ``retries_inside`` marks an operation that already retries itself
        (the Claude quota lane: API transport retry, probe TimeoutError
        retries); it runs exactly once so the two layers never multiply.
        ``_LANE_TERMINAL_EXCEPTIONS`` end the loop on first sight.
        """
        error_class: str | None = None
        total = 1 if retries_inside else attempts
        for attempt in range(1, total + 1):
            if attempt > 1:
                sleep(lane_backoff_seconds * (attempt - 1))
            try:
                return operation(), None
            except _LANE_TERMINAL_EXCEPTIONS as exc:
                return None, type(exc).__name__
            except Exception as exc:
                error_class = type(exc).__name__
        return None, error_class

    def _cede_lane(source: str, error_class: str) -> None:
        """Give up on one lane for this run without failing the collection."""
        lane = _lane(source)
        lane["succeeded"] = False
        lane["ceded"] = True
        lane["last_error_class"] = error_class
        warnings.append({"source": source, "error": error_class})

    def collect_source(
        source: str,
        collect: Any,
        *,
        require_rows: bool = False,
        retries_inside: bool = False,
    ) -> list[dict[str, Any]]:
        lane = _lane(source)

        def read() -> tuple[list[dict[str, Any]], dict[str, Any]]:
            rows, diagnostics = _materialize_lane_read(collect())
            if require_rows and not rows:
                raise EmptyLaneResult(f"{source} returned no rows")
            return rows, diagnostics

        outcome, error_class = _attempt_lane(read, retries_inside=retries_inside)
        if error_class is not None:
            sources[source] = _source_result(0)
            _cede_lane(source, error_class)
            return []
        rows, diagnostics = outcome
        skipped = diagnostics.get("skipped_files") or []
        lane["skipped_files"] = len(skipped)
        if retry_files_out is not None and source in claude_usage_source_keys:
            retry_files_out.extend(str(Path(item["path"]).resolve()) for item in skipped)
        if skipped:
            # Count only: the paths are private and the report is not.
            warnings.append(
                {
                    "source": source,
                    "error": "MalformedLedgerError",
                    "skipped_files": str(len(skipped)),
                }
            )
        sources[source] = _source_result(len(rows))
        lane["discovered"] = len(rows)
        # A clean read is already a success; a persisted lane's write side
        # re-judges it below, and a read-only lane must not read as
        # never-healthy.
        lane["succeeded"] = True
        lane["last_error_class"] = None
        return rows

    def persist_source(
        source: str,
        path: str | Path,
        rows: Iterable[dict[str, Any]],
        *,
        fact_type: str,
    ) -> tuple[AppendResult, int]:
        nonlocal generated_corrections
        lane = _lane(source)
        if lane["ceded"]:
            # The read side already ceded this lane for this run; an empty
            # append would only dress the cede up as ledger activity.
            return AppendResult(discovered=0, appended=0, replayed=0), 0
        prepared_rows = list(rows)
        hermes_sidecar_source = source == "hermes" and fact_type == "usage_event_v1"
        claude_reconciled = (
            source in claude_usage_source_keys and fact_type == "usage_event_v1"
        )
        equivalent_fields = (
            frozenset({"occurred_at", "recorded_at"})
            if claude_reconciled
            else (
                # The Hermes sidecar stores exact integer micro-USD; canonical
                # rows ingested from the retired state.db layout carry
                # full-precision floats. Sub-micro-dollar drift on the same
                # identity is a unit rounding difference, not a content change.
                # Likewise a cost refiled by its sidecar cost_status (actual
                # vs estimated vs the unknown-only x_hermes_cost_usd) is the
                # same figure under a different label, not a new fact.
                frozenset({"estimated_cost_usd", "actual_cost_usd", "x_hermes_cost_usd"})
                if hermes_sidecar_source
                else frozenset()
            )
        )

        def write() -> tuple[AppendResult, tuple[ConflictSummary, ...], Any]:
            # Canonical appends are replay-safe, so retrying one batch after a
            # torn attempt replays what already landed instead of duplicating.
            if claude_reconciled:
                return _append_canonical_ledger_quarantined_reconciled(
                    path,
                    prepared_rows,
                    fact_type=fact_type,
                    dry_run=dry_run,
                    reconcile=_reconcile_finalized_claude_rows,
                    equivalent_changed_fields=equivalent_fields,
                    reconcile_existing=_load_reconcile_window,
                )
            batch_result, batch_summaries = _append_canonical_ledger_quarantined(
                path,
                prepared_rows,
                fact_type=fact_type,
                dry_run=dry_run,
                equivalent_changed_fields=equivalent_fields,
            )
            return batch_result, batch_summaries, None

        outcome, error_class = _attempt_lane(write)
        if error_class is not None:
            _cede_lane(source, error_class)
            return AppendResult(discovered=0, appended=0, replayed=0), 0
        append_result, summaries, reconciliation = outcome
        if reconciliation is not None:
            generated, reconciled_resolutions = reconciliation
            generated_corrections += generated
            for resolution in reconciled_resolutions:
                entry = {**resolution, "source": source}
                if entry.get("resolution") == "source_regression":
                    resolutions.append(entry)
                else:
                    stabilized_replays.append(entry)
        quarantined = [summary for summary in summaries if summary.resolution == "quarantined"]
        stabilized = [summary for summary in summaries if summary.resolution == "canonical_replay"]
        lane["rows_written"] = append_result.appended
        lane["quarantined_count"] = len(quarantined)
        if quarantined:
            lane["last_error_class"] = "IdentityConflictError"
        # A persisted lane succeeds only if it also wrote cleanly. A read
        # failure already recorded its error class and yielded no rows.
        lane["succeeded"] = not quarantined and lane["last_error_class"] is None
        if quarantined:
            warnings.append(
                {
                    "source": source,
                    "error": "IdentityConflictError",
                    "quarantined": str(len(quarantined)),
                }
            )
        for summary in quarantined:
            conflicts.append(
                {
                    "source": source,
                    "error": "IdentityConflictError",
                    "identity_sha256": summary.identity_sha256,
                    "existing_sha256": summary.existing_sha256,
                    "incoming_sha256": summary.incoming_sha256,
                    "changed_fields": list(summary.changed_fields),
                }
            )
        for summary in stabilized:
            stabilized_replays.append(
                {
                    "source": source,
                    "resolution": "canonical_replay",
                    "identity_sha256": summary.identity_sha256,
                    "existing_sha256": summary.existing_sha256,
                    "incoming_sha256": summary.incoming_sha256,
                    "changed_fields": list(summary.changed_fields),
                }
            )
        return append_result, len(quarantined)

    paths: dict[str, str] = {}
    result: dict[str, Any] = {
        "dry_run": dry_run,
        "scope": scope,
        "paths": paths,
        "sources": sources,
        "warnings": warnings,
        "conflicts": conflicts,
        "stabilized_replays": stabilized_replays,
        "resolutions": resolutions,
    }
    opencode: list[dict[str, Any]] | None = None
    quota_batches: list[tuple[AppendResult, int]] = []

    def _with_since(bound: Callable[..., Any], *args: Any) -> Callable[[], Any]:
        # Preserve the original no-cutoff call shape. A cutoff is never silently
        # dropped on TypeError: that is an adapter defect, not a full-scan hint.
        kwargs: dict[str, Any] = {} if usage_since is None else {"since": usage_since}
        if bound is collect_claude_usage and claude_retry_files:
            kwargs["retry_files"] = claude_retry_files
        return lambda: bound(*args, **kwargs)

    if collect_usage_scope:
        hermes = collect_source(
            "hermes",
            _with_since(collect_hermes_usage, state_db, f"{source_prefix}:hermes"),
        )
        claude_sources: list[tuple[str, list[dict[str, Any]]]] = []
        if configured_claude_instances:
            for instance in configured_claude_instances:
                source = instance.source_key
                rows = collect_source(
                    source,
                    _with_since(
                        collect_claude_usage,
                        Path(instance.config_dir) / "projects",
                        instance.usage_namespace(source_prefix),
                    ),
                )
                claude_sources.append((source, rows))
        else:
            claude_sources.append(
                (
                    "claude_code",
                    collect_source(
                        "claude_code",
                        _with_since(
                            collect_claude_usage, claude_root, f"{source_prefix}:claude-code"
                        ),
                    ),
                )
            )
        opencode = collect_source(
            "opencode",
            lambda: collect_opencode_usage(opencode_dbs, f"{source_prefix}:opencode"),
        )
        # OpenClaw's model calls (keeper msg 1553067845766742228: "I completely
        # forgot about openclaw. that's not tracked at all now right?").
        openclaw_rows: list[dict[str, Any]] = []
        if openclaw_home is not None:
            openclaw_rows = collect_source(
                OPENCLAW_USAGE_LANE,
                lambda: collect_openclaw_usage(openclaw_home, f"{source_prefix}:openclaw"),
            )
        usage_batches = [
            persist_source("hermes", usage_ledger, hermes, fact_type="usage_event_v1"),
            *(
                persist_source(source, usage_ledger, rows, fact_type="usage_event_v1")
                for source, rows in claude_sources
            ),
            persist_source("opencode", usage_ledger, opencode, fact_type="usage_event_v1"),
        ]
        if openclaw_home is not None:
            usage_batches.append(
                persist_source(OPENCLAW_USAGE_LANE, usage_ledger, openclaw_rows, fact_type="usage_event_v1")
            )
        result["usage"] = _aggregate_append_results(usage_batches)
        if usage_since is not None:
            # The fast tick is incremental: report the epoch cutoff it ran
            # with so the next tick can chain from this report.
            result["usage"]["usage_since"] = usage_since
        result["generated_corrections"] = generated_corrections
        paths["usage_ledger"] = str(Path(usage_ledger).expanduser())
        if resolutions and not dry_run:
            store = (
                Path(adjudication_store).expanduser()
                if adjudication_store is not None
                else _default_adjudication_store(usage_ledger)
            )
            # Record-once, then quiet: an identity whose incoming sha is already
            # adjudicated stays visible in the report but never re-warns.
            result["resolutions"] = resolutions = record_adjudications(store, resolutions)
            paths["adjudication_store"] = str(store)

    if collect_quota_scope:
        quota_sources: list[tuple[str, list[dict[str, Any]]]] = []
        if codex_quota_history:
            codex_history = codex_quota_observations(
                codex_ledger,
                f"{source_prefix}:codex-quota",
                include_history=True,
                history_since=codex_quota_history_since,
            )
            sources["codex_quota"] = _source_result(len(codex_history))
            quota_sources.append(("codex_quota", codex_history))
        else:
            quota_sources.append(
                (
                    "codex_quota",
                    collect_source(
                        "codex_quota",
                        lambda: codex_quota_observations(
                            codex_ledger,
                            f"{source_prefix}:codex-quota",
                        ),
                    ),
                )
            )
            # OpenCode Go (keeper acceptance 2026-09-14): the provider's usage
            # API is the quota authority. The native API-key reader is
            # preferred; the cookie reader remains only where no native key
            # is configured. A failing reader cedes the lane and the ledger
            # keeps its last good reading — the retired first-use USD replay
            # is never a fallback, and without either credential the lane
            # reports unavailable rather than a fresh estimate.
            opencode_go_api_key = env.get("OPENCODE_GO_API_KEY")
            opencode_go_workspace = env.get("OPENCODE_GO_WORKSPACE_ID")
            opencode_go_cookie = env.get("OPENCODE_GO_AUTH_COOKIE")
            selected_opencode_go = (
                quota_lanes is None or "opencode_go_quota" in quota_lanes
            )
            opencode_go_rows: list[dict[str, Any]] = []
            if live_quota and selected_opencode_go and opencode_go_api_key:
                opencode_go_rows = collect_source(
                    "opencode_go_quota",
                    lambda: collect_opencode_go_usage_quota(
                        opencode_go_api_key,
                        f"{source_prefix}:opencode-go",
                    ),
                    require_rows=True,
                )
            elif (
                live_quota
                and selected_opencode_go
                and opencode_go_workspace
                and opencode_go_cookie
            ):
                opencode_go_rows = collect_source(
                    "opencode_go_quota",
                    lambda: collect_exact_opencode_go_quota(
                        opencode_go_workspace,
                        opencode_go_cookie,
                        f"{source_prefix}:opencode-go",
                    ),
                    require_rows=True,
                )
            else:
                # Not configured is not a failure (the stand-down convention
                # of the active-window tick): reported, not ceded.
                sources["opencode_go_quota"] = _source_result(0)
            if opencode_go_rows:
                quota_sources.append(("opencode_go_quota", opencode_go_rows))

            if live_quota:
                # Five-minute active-window tick (keeper-approved burn panel,
                # 2026-09-02): when the caller gates on activity, a Claude
                # account is only probed when its own five-hour window is
                # demonstrably burning; every other lane stands down for
                # this run.
                tick_now = datetime.now(timezone.utc)
                claude_gate_open = (
                    (not when_active)
                    or _claude_window_is_burning(
                        quota_ledger, account_ref=None, now=tick_now
                    )
                )
                if claude_quota_command and not dry_run:
                    if configured_claude_instances:
                        for instance in configured_claude_instances:
                            source = instance.quota_source_key
                            if quota_lanes is not None and source not in quota_lanes:
                                sources[source] = _source_result(0)
                                continue
                            if when_active and not _claude_window_is_burning(
                                quota_ledger,
                                account_ref=instance.account_ref,
                                now=tick_now,
                            ):
                                # Standing down is not a failure: the window
                                # is idle and the hourly collector owns
                                # discovery.
                                sources[source] = _source_result(0)
                                continue
                            quota_sources.append(
                                (
                                    source,
                                    collect_source(
                                        source,
                                        lambda instance=instance: _collect_claude_quota_with_probe_fallback(
                                            instance.quota_namespace(source_prefix),
                                            credentials_path=instance.credentials_path,
                                            claude_command=claude_quota_command,
                                            probe_dir=instance.probe_dir(claude_probe_dir),
                                            timeout=claude_quota_timeout,
                                            claude_config_dir=instance.quota_config_dir,
                                            account_ref=instance.account_ref,
                                        ),
                                        retries_inside=True,
                                    ),
                                )
                            )
                    elif claude_gate_open:
                        quota_sources.append(
                            (
                                "claude_code_quota",
                                collect_source(
                                    "claude_code_quota",
                                    lambda: _collect_claude_quota_with_probe_fallback(
                                        f"{source_prefix}:claude-code-quota",
                                        credentials_path=default_claude_credentials_path(),
                                        claude_command=claude_quota_command,
                                        probe_dir=claude_probe_dir,
                                        timeout=claude_quota_timeout,
                                    ),
                                    retries_inside=True,
                                ),
                            )
                        )
                    else:
                        sources["claude_code_quota"] = _source_result(0)
                elif claude_quota_command:
                    # The Claude `/usage` capture creates a probe directory, lock,
                    # and tmux session; dry-run remains process/filesystem safe.
                    if configured_claude_instances:
                        for instance in configured_claude_instances:
                            sources[instance.quota_source_key] = _source_result(0)
                    else:
                        sources["claude_code_quota"] = _source_result(0)
                # The active-window tick is a Claude-only lane: provider API
                # quotas stand down under --when-active (their windows do not
                # burn on minutes).
                openrouter_key = (
                    env.get("OPENROUTER_API_KEY") if live_quota and not when_active else None
                )
                if openrouter_key:
                    quota_sources.append(
                        (
                            "openrouter_quota",
                            collect_source(
                                "openrouter_quota",
                                lambda: collect_openrouter_quota(
                                    openrouter_key,
                                    f"{source_prefix}:openrouter-quota",
                                ),
                            ),
                        )
                    )
                deepseek_key = (
                    env.get("DEEPSEEK_API_KEY") if live_quota and not when_active else None
                )
                if deepseek_key:
                    quota_sources.append(
                        (
                            "deepseek_quota",
                            collect_source(
                                "deepseek_quota",
                                lambda: collect_deepseek_quota(
                                    deepseek_key,
                                    f"{source_prefix}:deepseek-quota",
                                ),
                            ),
                        )
                    )

                kimi_key = (
                    env.get("KIMI_CODING_API_KEY") if live_quota and not when_active else None
                )
                if kimi_key:
                    quota_sources.append(
                        (
                            "kimi_code_quota",
                            collect_source(
                                "kimi_code_quota",
                                lambda: collect_kimi_code_quota(
                                    kimi_key,
                                    f"{source_prefix}:kimi-code-quota",
                                ),
                            ),
                        )
                    )

                z_ai_key = (
                    (env.get("Z_AI_API_KEY") or env.get("ZAI_API_KEY"))
                    if live_quota and not when_active
                    else None
                )
                if z_ai_key:
                    quota_sources.append(
                        (
                            "z_ai_quota",
                            collect_source(
                                "z_ai_quota",
                                lambda: collect_z_ai_quota(
                                    z_ai_key,
                                    f"{source_prefix}:z-ai-quota",
                                ),
                            ),
                        )
                    )

        quota_batches += [
            persist_source(
                source,
                quota_ledger,
                rows,
                fact_type="quota_observation_v1",
            )
            for source, rows in quota_sources
        ]
        result["quotas"] = _aggregate_append_results(quota_batches)
        paths["quota_ledger"] = str(Path(quota_ledger).expanduser())

    if collect_search_scope:
        # Web search/extract accounting (keeper ruling 2026-09-10). The feed is
        # written by Hermes; until that lane ships, and whenever it is rotated
        # away, a missing file is an empty lane rather than a failure.
        watermark_file = _watermark_path(search_target, audit_target)
        previous_watermark = read_watermark(watermark_file)
        read_diagnostics: dict[str, Any] = {}

        def _read_search_events():
            # ``yield from`` keeps the adapter's StopIteration diagnostics —
            # byte offset, inode, malformed count — which the watermark needs
            # and which collect_source would otherwise drop on the floor.
            captured = yield from collect_hermes_search_events(
                audit_target,
                f"{source_prefix}:hermes-search",
                watermark=previous_watermark,
            )
            read_diagnostics.clear()
            read_diagnostics.update(captured)
            return captured

        search_facts = collect_source(SEARCH_LANE, _read_search_events)
        search_batches = [
            persist_source(
                SEARCH_LANE, search_target, search_facts, fact_type="search_event_v1"
            )
        ]
        # OpenClaw's web searches (Brave; keeper msg 1553067845766742228:
        # "brave needs usage tracking too"). Its store is re-read whole each
        # run; canonical identity makes that a replay.
        if openclaw_home is not None:
            openclaw_searches = collect_source(
                OPENCLAW_SEARCH_LANE,
                lambda: collect_openclaw_searches(openclaw_home, f"{source_prefix}:openclaw-search"),
            )
            search_batches.append(
                persist_source(
                    OPENCLAW_SEARCH_LANE, search_target, openclaw_searches, fact_type="search_event_v1"
                )
            )
        # A rate-limited event that names a reset instant is the only thing
        # this lane can say about a quota it cannot probe; everything else the
        # rate_limited count in the search ledger already carries.
        derived = derive_search_quota_observations(
            search_facts, f"{source_prefix}:hermes-search"
        )
        quota_batches.append(
            persist_source(
                SEARCH_QUOTA_LANE,
                quota_ledger,
                derived,
                fact_type="quota_observation_v1",
            )
        )
        result["quotas"] = _aggregate_append_results(quota_batches)
        result["searches"] = {
            **_aggregate_append_results(search_batches),
            "derived_quota_observations": len(derived),
            "malformed_lines": int(read_diagnostics.get("malformed_lines") or 0),
        }
        paths["search_ledger"] = str(search_target)
        paths["quota_ledger"] = str(Path(quota_ledger).expanduser())
        # Advance the watermark only once both appends committed. A ceded lane
        # leaves it where it was: the next run re-reads the same bytes, and
        # canonical identity makes that a replay rather than a duplicate.
        if (
            read_diagnostics.get("inode") is not None
            and not dry_run
            and not _lane(SEARCH_LANE)["ceded"]
            and not _lane(SEARCH_QUOTA_LANE)["ceded"]
        ):
            try:
                write_watermark(
                    watermark_file,
                    {
                        key: read_diagnostics.get(key)
                        for key in ("next_offset", "inode", "size", "last_observed_at")
                    },
                )
                paths["search_watermark"] = str(watermark_file)
            except OSError:
                # A read-only state directory must not fail a run whose
                # canonical appends already landed.
                pass

    if scope == "all" and billing_ledger is not None:
        billing_result = _append_canonical_ledger(
            billing_ledger,
            [],
            fact_type="billing_fact_v1",
            dry_run=dry_run,
        )
        paths["billing_ledger"] = str(Path(billing_ledger).expanduser())
        result["billing"] = asdict(billing_result)

    result["lanes"] = lanes
    ceded_lanes = sorted(name for name, lane in lanes.items() if lane.get("ceded"))
    result["ceded"] = ceded_lanes
    result["counts"] = {
        "ceded": len(ceded_lanes),
        "conflicts": len(conflicts),
        "stabilized_replays": len(stabilized_replays),
        "resolutions": len(resolutions),
        "resolutions_recorded": sum(
            1 for entry in resolutions if entry.get("newly_recorded")
        ),
        "warnings": len(warnings),
    }
    if not dry_run:
        directory = (
            Path(report_dir).expanduser()
            if report_dir is not None
            else _default_report_dir(
                usage_ledger
                if collect_usage_scope
                else quota_ledger if collect_quota_scope else search_target
            )
        )
        generated_at = (
            datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        )
        try:
            paths["report"] = str(
                write_collect_report(
                    directory,
                    {"generated_at": generated_at, **result},
                )
            )
            paths["lane_health"] = str(
                update_lane_health(directory, scope, lanes, now=generated_at)
            )
            # Reached only after every in-scope canonical append committed, and
            # deliberately before the strict-sources exit: the heartbeat states
            # that canonical writes landed, not that every lane was perfect.
            heartbeats = touch_heartbeat(directory, scope)
            if heartbeats:
                paths["heartbeats"] = [str(entry) for entry in heartbeats]
        except OSError:
            # A missing or read-only health directory must not turn a
            # successful collection into a failed one.
            pass
    # A ceded lane never fails the run (keeper-ruled 2026-08-29): its retries
    # are exhausted, its health record carries the error class, and the
    # watchdog owns the alerting. Strict sources still refuses unadjudicated
    # identity conflicts — retrying cannot resolve those.
    if strict_sources and conflicts:
        raise StrictSourcesError(warnings, report_path=paths.get("report"))
    return result
