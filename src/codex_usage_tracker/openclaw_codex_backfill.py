"""One-time backfill: OpenClaw Codex turns before the rollout cutover (SIL-77).

Keeper decision, 2026-10-22, Paperclip SIL-77 interaction 790feff0 (Luis):
option B, "Replace: zero out the 255 transcript rows, import all 1,828
per-call rows".

Before ``CODEX_ROLLOUT_CUTOVER`` the ``openclaw`` lane counted a Codex turn
from OpenClaw's one transcript usage row, which undercounts the turn (see
``openclaw_usage``). This tool, for every Codex turn that started before the
cutover and has a Codex rollout:

- appends a ``correction`` that reverses the ledger's transcript row for that
  turn exactly (every token field negated, same occurrence instant, pointing
  at the row it reverses), and
- appends the rollout's per-call rows, in the same namespace and shape the
  lane uses after the cutover (``sol:openclaw:<agent>:codex-rollout``).

The ledger is append-only: the reversed rows stay; readers sum the pair to
zero. A correction carries ``x_corrected_record_kind`` so list pricing prices
it on the reversed row's own context basis (an ``api_attempt`` with a
multi-million-token prompt was priced long-context; its reversal must be too).

The transcript row's link to its Codex turn is the message ``idempotencyKey``
(``codex-app-server:<thread>:<turn>:assistant``) in the agent's live store or,
for rows OpenClaw has since rewritten away, in its
``openclaw-agent.sqlite.pre-startup-migration-*.bak``. A row with no such link
is left alone.

Guards (the tool refuses rather than half-applies):
- every transcript usage row the lane still emits for a pre-cutover Codex turn
  that has a rollout is already in the ledger (otherwise the lane would add it
  after its reversal was written);
- no pre-cutover Codex turn has had a call in the last ``QUIET_MINUTES`` (a
  turn still running could grow after the import);
- every reversal target exists and is not itself a correction.

Identities and ``recorded_at`` are fixed, so a re-run replays instead of
appending. Reading is read-only throughout; ``--apply`` makes one transactional
append under the ledger lock.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
import sys
from typing import Any

from .canonical_ledger import (
    TOKEN_FIELDS,
    append_sqlite_facts,
    canonical_timestamp,
    normalize_fact,
)
from .openclaw_usage import (
    CODEX_ROLLOUT_CUTOVER,
    DEFAULT_OPENCLAW_HOME,
    _cutover_ms,
    _ro,
    _session_surfaces,
    codex_app_server_turn,
    codex_rollout_calls,
    collect_openclaw_usage,
    openclaw_stores,
    uuid7_ms,
)

BACKFILL_RECORDED_AT = "2026-10-22T19:30:00Z"
BACKFILL_NAMESPACE_SUFFIX = "codex-backfill"
QUIET_MINUTES = 60
_BACKUP_GLOB = "openclaw-agent.sqlite.pre-startup-migration-*.bak"
# Fields of the reversed row that describe a different observation, not this
# correction: OpenClaw's own id and cost figure.
_DROP_FROM_REVERSAL = ("x_openclaw_response_id", "x_openclaw_cost_usd", "event_uid", "provider_request_id")


class BackfillRefused(RuntimeError):
    """A guard failed; nothing was appended."""


def _store_links(path: Path, agent: str, keys: dict, threads: dict, *, static: bool = False) -> None:
    if static:
        # A migration backup is a closed snapshot: read it as immutable so no
        # -shm/-wal is ever created beside it.
        connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro&immutable=1", uri=True)
        connection.row_factory = sqlite3.Row
    else:
        connection = _ro(path)
    try:
        surfaces = _session_surfaces(connection)
        for row in connection.execute(
            "SELECT session_id, event_json FROM transcript_events "
            "WHERE event_json LIKE '%codex-app-server:%'"
        ):
            try:
                event = json.loads(row["event_json"])
            except (TypeError, ValueError):
                continue
            message = event.get("message") if isinstance(event, dict) else None
            if not isinstance(message, dict):
                continue
            turn = codex_app_server_turn(message)
            if turn is None:
                continue
            session_id = str(row["session_id"])
            keys.setdefault((agent, f"{session_id}:{event.get('id')}"), turn)
            threads.setdefault((agent, turn[0]), (session_id, *surfaces.get(session_id, (None, False))))
    finally:
        connection.close()


def turn_links(home: str | Path) -> tuple[dict, dict]:
    """``(agent, source_event_id) -> (thread, turn)`` and the thread->session
    map, from each live store and then its migration backups."""
    keys: dict[tuple[str, str], tuple[str, str]] = {}
    threads: dict[tuple[str, str], tuple[str, str | None, bool]] = {}
    for agent, path in openclaw_stores(home):
        _store_links(path, agent, keys, threads)
        for backup in sorted(path.parent.glob(_BACKUP_GLOB)):
            _store_links(backup, agent, keys, threads, static=True)
    return keys, threads


def _ledger_rows(ledger: Path, namespaces: list[str]) -> dict[tuple[str, str], dict[str, Any]]:
    uri = ledger.resolve().as_uri() + "?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    try:
        marks = ",".join("?" for _ in namespaces)
        found: dict[tuple[str, str], dict[str, Any]] = {}
        for namespace, payload in connection.execute(
            f"SELECT source_namespace, canonical_json FROM facts "
            f"WHERE fact_type = 'usage_event_v1' AND source_namespace IN ({marks})",
            namespaces,
        ):
            fact = json.loads(payload)
            found[(namespace, fact["source_event_id"])] = fact
        return found
    finally:
        connection.close()


def _reversal(target: dict[str, Any], turn: tuple[str, str], agent: str) -> dict[str, Any]:
    row = {key: value for key, value in target.items() if key not in _DROP_FROM_REVERSAL}
    for field in TOKEN_FIELDS:
        if row.get(field) is not None:
            row[field] = -int(row[field])
    for field in ("estimated_cost_usd", "actual_cost_usd"):
        if row.get(field) is not None:
            raise BackfillRefused(f"reversal target carries {field}; not handled")
    row.update({
        "source_namespace": f"{target['source_namespace']}:{BACKFILL_NAMESPACE_SUFFIX}",
        "source_event_id": f"supersede:{target['source_event_id']}",
        "record_kind": "correction",
        "recorded_at": BACKFILL_RECORDED_AT,
        "latency_ms": None,
        "corrects_source_namespace": target["source_namespace"],
        "corrects_source_event_id": target["source_event_id"],
        "x_corrected_record_kind": target.get("record_kind"),
        "x_correction_reason": "SIL-77: OpenClaw transcript usage for a Codex turn, superseded by the turn's Codex rollout calls",
        "x_codex_thread_id": turn[0], "x_codex_turn_id": turn[1], "x_openclaw_agent": agent,
    })
    return normalize_fact(row)


def plan_backfill(
    home: str | Path,
    ledger: str | Path,
    *,
    source_prefix: str = "sol:openclaw",
    cutover: str = CODEX_ROLLOUT_CUTOVER,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Everything ``--apply`` would append, and the figures behind it."""
    now = now or datetime.now(timezone.utc)
    cut = _cutover_ms(cutover)
    ledger_path = Path(ledger).expanduser()
    keys, threads = turn_links(home)

    counts = {"files_read": 0, "calls": 0, "before_cutover": 0, "duplicate_response": 0, "malformed": 0}
    calls = list(codex_rollout_calls(
        home, source_prefix, threads, counts,
        counted=lambda turn_id: (uuid7_ms(turn_id) or cut) < cut,
    ))
    turns: dict[tuple[str, str, str], int] = {}
    latest = None
    for call in calls:
        key = (call["x_openclaw_agent"], call["x_codex_thread_id"], call["x_codex_turn_id"])
        turns[key] = turns.get(key, 0) + sum(int(call.get(f) or 0) for f in ("input_tokens", "cache_read_tokens", "cache_write_tokens", "output_tokens"))
        stamp = datetime.fromisoformat(call["occurred_at"].replace("Z", "+00:00"))
        latest = stamp if latest is None or stamp > latest else latest
    if latest is not None and latest > now - timedelta(minutes=QUIET_MINUTES):
        raise BackfillRefused(f"a pre-cutover Codex turn had a call at {latest.isoformat()}; it may still be running")

    agents = sorted({agent for agent, _path in openclaw_stores(home)})
    transcript_namespaces = [f"{source_prefix}:{agent}" for agent in agents]
    ledger_rows = _ledger_rows(ledger_path, transcript_namespaces + [
        f"{ns}:{BACKFILL_NAMESPACE_SUFFIX}" for ns in transcript_namespaces
    ] + [f"{ns}:codex-rollout" for ns in transcript_namespaces])

    # Guard: the lane emits nothing for these turns that the ledger lacks.
    lane_rows = list(collect_openclaw_usage(home, source_prefix, codex_rollout_cutover=cutover))
    missing = []
    for row in lane_rows:
        agent = row.get("x_openclaw_agent")
        turn = keys.get((agent, row["source_event_id"]))
        if turn is not None and (agent, *turn) in turns and (row["source_namespace"], row["source_event_id"]) not in ledger_rows:
            missing.append(row["source_event_id"])
    if missing:
        raise BackfillRefused(f"{len(missing)} transcript rows for covered turns are not in the ledger yet: {missing[:3]}")

    reversals, reversed_turns, unlinked = [], set(), []
    for (namespace, identity), fact in sorted(ledger_rows.items()):
        if namespace not in transcript_namespaces or fact.get("record_kind") == "correction":
            continue
        agent = namespace.rsplit(":", 1)[1]
        turn = keys.get((agent, fact["source_event_id"]))
        if turn is None or (agent, *turn) not in turns:
            if fact.get("provider") in ("openai", "openai-codex"):
                unlinked.append(fact["source_event_id"])
            continue
        reversals.append(_reversal(fact, turn, agent))
        reversed_turns.add((agent, *turn))

    tok = lambda row: sum(int(row.get(f) or 0) for f in ("input_tokens", "cache_read_tokens", "cache_write_tokens", "output_tokens"))  # noqa: E731
    summary = {
        "cutover": canonical_timestamp(cutover),
        "rollout": {**counts, "turns": len(turns), "tokens": sum(turns.values())},
        "reversals": {"rows": len(reversals), "turns": len(reversed_turns), "tokens": -sum(map(tok, reversals)),
                      "by_provider": _by(reversals, "provider")},
        "calls": {"rows": len(calls), "tokens": sum(map(tok, calls)),
                  "in_reversed_turns": sum(1 for c in calls if (c["x_openclaw_agent"], c["x_codex_thread_id"], c["x_codex_turn_id"]) in reversed_turns)},
        "unlinked_codex_rows_left_alone": len(unlinked),
        "net_added_tokens": sum(map(tok, calls)) + sum(map(tok, reversals)),
    }
    return {"summary": summary, "facts": reversals + calls}


def _by(rows: list[dict[str, Any]], field: str) -> dict[str, int]:
    out: dict[str, int] = {}
    for row in rows:
        out[str(row.get(field))] = out.get(str(row.get(field)), 0) + 1
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="One-time OpenClaw Codex backfill (SIL-77)")
    parser.add_argument("--openclaw-home", default=DEFAULT_OPENCLAW_HOME)
    parser.add_argument("--usage-ledger", required=True)
    parser.add_argument("--source-prefix", default="sol")
    parser.add_argument("--apply", action="store_true", help="append (default: report only)")
    args = parser.parse_args(argv)
    try:
        plan = plan_backfill(args.openclaw_home, args.usage_ledger, source_prefix=f"{args.source_prefix}:openclaw")
    except BackfillRefused as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    report: dict[str, Any] = {"summary": plan["summary"], "applied": False}
    if args.apply:
        result = append_sqlite_facts(args.usage_ledger, plan["facts"], fact_type="usage_event_v1")
        report["applied"] = True
        report["append"] = {"discovered": result.discovered, "appended": result.appended, "replayed": result.replayed}
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
