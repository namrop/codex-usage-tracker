"""Read-only adapter for Google Antigravity CLI (``agy``) model calls.

Paperclip SIL-78, 2026-10-22: "ensure agy usage is accounted for in the
ai-usage tracker". Before this lane only bridged agy traffic reached the
ledger (Hermes ``custom:meridian-agy`` and OpenClaw ``meridian-agy`` rows,
recorded by the calling harness). agy run directly (Paperclip seats through
``agy-paperclip``, interactive or ``agy -p`` runs) had no record at all.

agy keeps one SQLite store per conversation at
``<home>/conversations/<conversation_id>.db`` (default home
``~/.gemini/antigravity-cli``). Table ``gen_metadata`` holds one protobuf
blob per model generation. No schema ships with the CLI. The fields below
were read off the wire and validated on Sol (2026-10-22) against Hermes'
provider-reported rows for the same calls through the Meridian bridge:
252 of 287 bridged generations matched a Hermes/OpenClaw row exactly on
(input, output, cache read). The remaining 35 were Meridian summing two
generations into one client response (4 Hermes rows are exact pair sums),
or setup probes no harness recorded.

``gen_metadata.data``: field 1 is the generation, whose
- field 4 is per-call usage: 1 model enum, 2 input tokens (uncached),
  3 output tokens (= 9 thinking + 10 response on every observed row),
  5 cache-read tokens, 6 API provider enum, 7 message id, 11 the Gemini
  response id;
- field 19 is the served model name (``gemini-3.8-flash-n``,
  ``claude-opus-4-6-thinking``: agy also serves Claude on the Google plan);
- field 20 repeats ``{key, value}`` pairs (``request_id``, ``trajectory_id``,
  ``used_claude``, ...).

``steps.metadata`` repeats the same usage (field 9) on the step that holds
the response and carries its timestamps; field 7 is the completion time
(median 0.08 s, max 0.35 s before Hermes' own accounting time over 244
matched calls), and is used as ``occurred_at``.
A generation whose response step has no completion time yet (a live
conversation mid-write) is deferred to a later tick, never dated from
another field: a later re-read with a different time would be a payload
conflict on an append-only ledger.

Rules:
- Stores are a live application's databases: opened read-only. A store that
  cannot be opened is reported as a skipped file (count-only lane warning,
  as for malformed Claude transcripts) and read again next tick.
- Bridged conversations are skipped (counted in the diagnostics): Meridian
  runs agy in its own ``antigravity-workspaces-*`` workspace and the calling
  harness already records the call. Counting both would double-count.
- A generation with no token counts is not a model call.
- Any usage field outside the mapped set is listed in
  ``x_antigravity_unmapped_usage_fields`` rather than guessed into a column
  (field 4 would be cache write in the upstream Codeium layout; it has never
  been observed here, so it is not assumed).
- The Gemini response id is kept as ``x_antigravity_response_id``: visible,
  never joined (no other observer emits it).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
from typing import Any, Iterator
from urllib.parse import unquote, urlparse

from .canonical_ledger import canonical_timestamp, normalize_fact

DEFAULT_ANTIGRAVITY_HOME = "~/.gemini/antigravity-cli"
ANTIGRAVITY_USAGE_LANE = "antigravity"
ANTIGRAVITY_PROVIDER = "antigravity"
# Meridian's antigravity backend creates one workspace per conversation under
# ``<state dir>/antigravity-workspaces-<hash>/conversation-<id>``.
_RELAY_WORKSPACE_MARKER = "antigravity-workspaces-"
_PAPERCLIP_WORKSPACE_MARKER = "/paperclip-learning/"
# usage field -> meaning, for fields this adapter maps (see module docstring).
_MAPPED_USAGE_FIELDS = frozenset({1, 2, 3, 5, 6, 9, 10})
_STEP_COMPLETED_FIELD = 7
# A WAL this size or smaller is the 32-byte header with no frames: it adds no
# pages, so the main file alone is the complete database.
_WAL_HEADER_BYTES = 32


class _WireError(ValueError):
    pass


def _varint(data: bytes, index: int) -> tuple[int, int]:
    result = shift = 0
    while True:
        if index >= len(data):
            raise _WireError("truncated varint")
        byte = data[index]
        index += 1
        result |= (byte & 0x7F) << shift
        shift += 7
        if not byte & 0x80:
            return result, index
        if shift > 63:
            raise _WireError("varint too long")


def _message(data: bytes | None) -> list[tuple[int, int, Any]]:
    """Decode one protobuf message into ``(field, wire_type, value)`` triples."""
    if not data:
        return []
    out: list[tuple[int, int, Any]] = []
    index = 0
    while index < len(data):
        key, index = _varint(data, index)
        field, wire = key >> 3, key & 7
        if field == 0:
            raise _WireError("field 0")
        if wire == 0:
            value, index = _varint(data, index)
        elif wire == 1:
            value, index = data[index:index + 8], index + 8
        elif wire == 2:
            length, index = _varint(data, index)
            value, index = data[index:index + length], index + length
        elif wire == 5:
            value, index = data[index:index + 4], index + 4
        else:
            raise _WireError(f"wire type {wire}")
        if index > len(data):
            raise _WireError("truncated field")
        out.append((field, wire, value))
    return out


def _first(message: list[tuple[int, int, Any]], field: int, wire: int) -> Any:
    for f, w, value in message:
        if f == field and w == wire:
            return value
    return None


def _text(value: Any) -> str | None:
    if not isinstance(value, (bytes, bytearray)):
        return None
    try:
        text = bytes(value).decode("utf-8").strip()
    except UnicodeDecodeError:
        return None
    return text or None


def _timestamp(value: Any) -> tuple[int, int] | None:
    """``(seconds, nanos)`` of a protobuf Timestamp: integers, never a float,
    so the same blob always renders the same ``occurred_at``."""
    try:
        stamp = _message(value)
    except _WireError:
        return None
    seconds = _first(stamp, 1, 0)
    if not seconds:
        return None
    return seconds, _first(stamp, 2, 0) or 0


def _iso(stamp: tuple[int, int]) -> str:
    seconds, nanos = stamp
    moment = datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=seconds, microseconds=nanos // 1000)
    return canonical_timestamp(moment.isoformat())


def _file_uris(data: bytes, depth: int = 0) -> list[str]:
    """Every ``file://`` string anywhere in a protobuf blob."""
    found: list[str] = []
    try:
        message = _message(data)
    except _WireError:
        return found
    for _field, wire, value in message:
        if wire != 2:
            continue
        text = _text(value)
        if text and text.startswith("file://"):
            found.append(text)
        elif depth < 8:
            found.extend(_file_uris(value, depth + 1))
    return found


def _workspaces(connection: sqlite3.Connection) -> list[str]:
    try:
        row = connection.execute("SELECT data FROM trajectory_metadata_blob").fetchone()
    except sqlite3.DatabaseError:
        return []
    if not row or not row[0]:
        return []
    return [unquote(urlparse(uri).path) for uri in _file_uris(bytes(row[0]))]


def _is_relay(workspaces: list[str]) -> bool:
    return any(part.startswith(_RELAY_WORKSPACE_MARKER) for path in workspaces for part in Path(path).parts)


def _surface(workspaces: list[str]) -> str:
    return "paperclip" if any(_PAPERCLIP_WORKSPACE_MARKER in path for path in workspaces) else "cli"


def _summaries(home: Path) -> dict[str, dict[str, Any]]:
    path = home / "conversation_summaries.db"
    if not path.is_file():
        return {}
    try:
        connection = _ro(path)
    except sqlite3.DatabaseError:
        return {}
    try:
        rows = connection.execute(
            "SELECT conversation_id, workspace_uris, parent_conversation_id FROM conversation_summaries"
        ).fetchall()
    except sqlite3.DatabaseError:
        return {}
    finally:
        connection.close()
    out: dict[str, dict[str, Any]] = {}
    for row in rows:
        try:
            uris = json.loads(row["workspace_uris"] or "[]")
        except ValueError:
            uris = []
        out[str(row["conversation_id"])] = {
            "workspaces": [unquote(urlparse(u).path) for u in uris if isinstance(u, str)],
            "parent": row["parent_conversation_id"] or None,
        }
    return out


def _ro(path: Path) -> sqlite3.Connection:
    """Read-only, as in the OpenClaw adapter: a plain read-only open while agy
    holds the store (its -wal/-shm exist); immutable when no -wal is left.

    agy also leaves header-only WALs beside a stale -shm on closed stores
    (seen on Sol 2026-10-22: a read-only open fails with "locking protocol"
    because the read-only collector cannot rebuild the -shm). A header-only
    WAL holds no frames, so those are read as immutable too.
    """
    uri = path.resolve().as_uri()
    try:
        connection = sqlite3.connect(uri + "?mode=ro", uri=True)
        connection.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchall()
    except sqlite3.OperationalError:
        wal = Path(str(path) + "-wal")
        if wal.exists() and wal.stat().st_size > _WAL_HEADER_BYTES:
            raise
        connection = sqlite3.connect(uri + "?mode=ro&immutable=1", uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def antigravity_stores(home: str | Path) -> list[tuple[str, Path]]:
    """``(conversation_id, store path)`` for every conversation store."""
    root = Path(home).expanduser() / "conversations"
    if not root.is_dir():
        return []
    return [(path.stem, path) for path in sorted(root.glob("*.db")) if path.is_file()]


def _step_times(connection: sqlite3.Connection) -> dict[str, tuple[int, int]]:
    """Response id -> earliest completion time of a step carrying it."""
    times: dict[str, tuple[int, int]] = {}
    for row in connection.execute("SELECT metadata FROM steps WHERE metadata IS NOT NULL"):
        try:
            meta = _message(bytes(row[0]))
        except _WireError:
            continue
        usage = _first(meta, 9, 2)
        stamp = _timestamp(_first(meta, _STEP_COMPLETED_FIELD, 2))
        if usage is None or not stamp:
            continue
        try:
            response_id = _text(_first(_message(usage), 11, 2))
        except _WireError:
            continue
        if response_id and (response_id not in times or stamp < times[response_id]):
            times[response_id] = stamp
    return times


def collect_antigravity_usage(
    home: str | Path = DEFAULT_ANTIGRAVITY_HOME,
    source_prefix: str = "sol:antigravity",
) -> Iterator[dict[str, Any]]:
    """One ``usage_event_v1`` fact per direct agy model generation.

    Returns ``{"skipped": {...}, "stores": n, "skipped_files": [...]}`` as
    the generator's ``StopIteration`` value for the lane report.
    """
    root = Path(home).expanduser()
    skipped = {"relay_conversation": 0, "zero_usage": 0, "unparsable": 0, "unreadable_store": 0,
               "deferred_no_completed_step": 0}
    skipped_files: list[dict[str, str]] = []
    stores = antigravity_stores(root)
    summaries = _summaries(root) if stores else {}
    for conversation_id, path in stores:
        connection = None
        try:
            connection = _ro(path)
            generations = connection.execute("SELECT idx, data FROM gen_metadata ORDER BY idx").fetchall()
        except sqlite3.DatabaseError:
            if connection is not None:
                connection.close()
            skipped["unreadable_store"] += 1
            skipped_files.append({"path": str(path)})
            continue
        try:
            summary = summaries.get(conversation_id, {})
            workspaces = _workspaces(connection) or summary.get("workspaces", [])
            if _is_relay(workspaces):
                skipped["relay_conversation"] += len(generations)
                continue
            step_times = _step_times(connection)
            for row in generations:
                try:
                    generation = _message(_first(_message(bytes(row["data"] or b"")), 1, 2))
                    usage = _message(_first(generation, 4, 2))
                    pairs = [_message(value) for f, w, value in generation if f == 20 and w == 2]
                except _WireError:
                    skipped["unparsable"] += 1
                    continue
                counts = {f: value for f, w, value in usage if w == 0}
                if not any(counts.get(field) for field in (2, 3, 5)):
                    skipped["zero_usage"] += 1
                    continue
                labels = {_text(_first(p, 1, 2)): _text(_first(p, 2, 2)) for p in pairs}
                response_id = _text(_first(usage, 11, 2))
                stamp = step_times.get(response_id or "")
                if stamp is None:
                    skipped["deferred_no_completed_step"] += 1
                    continue
                model = _text(_first(generation, 19, 2))
                request_id = labels.get("request_id")
                fact: dict[str, Any] = {
                    "fact_type": "usage_event_v1", "schema_version": 1,
                    "source_namespace": source_prefix,
                    "source_event_id": f"{conversation_id}:{request_id or row['idx']}",
                    "harness": "antigravity",
                    "purpose": "subagent" if summary.get("parent") else "main",
                    "record_kind": "api_attempt",
                    "occurred_at": _iso(stamp), "recorded_at": _iso(stamp),
                    # agy reports no cache-write count; the column stays null
                    # (missing), so the record is partial, not complete.
                    "usage_source": "provider_reported", "usage_completeness": "partial",
                    "measurement_confidence": "exact",
                    "surface": _surface(workspaces), "session_id": conversation_id,
                    "provider": ANTIGRAVITY_PROVIDER, "model_requested": None, "model_reported": model,
                    "api_mode": None, "billing_mode": "subscription_included", "request_status": "ok",
                    "input_tokens": counts.get(2, 0), "output_tokens": counts.get(3, 0),
                    "cache_read_tokens": counts.get(5, 0), "cache_write_tokens": None,
                    "reasoning_tokens": counts.get(9),
                    "missing_fields": ["cache_write_tokens"],
                    "attribution_gaps": ["model_requested"] + ([] if model else ["model_reported"]),
                    "cost_status": "included", "cost_source": "subscription",
                    "estimated_cost_usd": None, "actual_cost_usd": None,
                }
                if response_id:
                    fact["x_antigravity_response_id"] = response_id
                if labels.get("model_enum"):
                    fact["x_antigravity_model_enum"] = labels["model_enum"]
                if labels.get("used_claude") == "true":
                    fact["x_antigravity_used_claude"] = True
                unmapped = sorted(f for f, value in counts.items() if f not in _MAPPED_USAGE_FIELDS and value)
                if unmapped:
                    fact["x_antigravity_unmapped_usage_fields"] = {str(f): counts[f] for f in unmapped}
                yield normalize_fact(fact)
        finally:
            connection.close()
    return {"skipped": skipped, "stores": len(stores), "skipped_files": skipped_files}
