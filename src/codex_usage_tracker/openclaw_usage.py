"""Read-only adapters for OpenClaw model calls and web searches.

Keeper, 2026-09-25, Discord unified usage ledger thread, msg
1553067845766742228: "I completely forgot about openclaw. that's not tracked at
all now right?" It was not: the OpenClaw gateway on Sol (user unit
``openclaw-gateway.service``) calls Z.AI, Kimi, OpenCode Go, Codex, DeepSeek
and local models with no record in the usage ledger, and its web searches go
to Brave (same msg: "brave needs usage tracking too").

OpenClaw keeps one SQLite store per agent at
``<home>/agents/<agent>/agent/openclaw-agent.sqlite``. Table
``transcript_events`` holds pi-style transcript events. Every assistant message
carries ``provider``, ``model``, ``api``, ``usage`` (``input``, ``output``,
``cacheRead``, ``cacheWrite``, ``reasoningTokens``, ``totalTokens``, ``cost``)
and usually ``responseId``. A web search is a ``toolResult`` message whose
``details`` name the search provider, result count and latency.

Rules:
- The stores are a live application's databases: opened read-only (mode=ro).
- An assistant message whose token fields are all zero is not a model call
  (the Codex runtime mirrors each tool step as a zero-usage message).
- Codex runtime turns (``api == "openai-chatgpt-responses"``) carry the whole
  turn's usage in one message, many model calls in one row. They are recorded
  as ``historical_aggregate`` so pricing never classifies the summed prompt as
  one long-context request.
- Provider labels map to the ledger's names only where the route is the same
  subscription (OpenClaw ``kimi`` is the Kimi Code subscription Hermes calls
  ``kimi-coding``; the Codex runtime is ``openai-codex``). Anything else keeps
  OpenClaw's own name, so an unknown route stays visible instead of being
  priced as something it is not.
- Meridian routes are counted through their Claude SDK receipt (identity rule
  v2.1); OpenClaw rows for them are skipped and counted in the diagnostics.
- ``openclaw/delivery-mirror`` rows are OpenClaw's own delivery copies, not
  model calls.
- ``responseId`` is kept as ``x_openclaw_response_id``: visible, never joined.
- Search events carry only a SHA-256 of the query, never its text.

Codex runtime turns (SIL-77, 2026-10-22). The transcript's one usage row per
Codex turn is not the turn's usage: it undercounts. Measured over every
rollout under ``agents/*/agent/codex-home`` on 2026-10-22, the 238 turns that
have both a transcript row and a rollout held 72.7M tokens in the transcript
and 246.7M in the rollout's per-call records (3.4x). A further 28.1M sat in turns with no
transcript usage row at all (Codex's own ``codex-auto-review`` guardian
calls, and turns whose terminal message carried none). The rollout's
``token_usage_record`` is Codex's own per-model-call receipt: one per OpenAI
response, with the ``resp_`` id, the turn id and exact usage.
- From ``CODEX_ROLLOUT_CUTOVER`` on, a Codex turn is counted from its rollout,
  one ``api_attempt`` per response, and the transcript row for the same turn
  is skipped (``skipped["codex_rollout"]``). Both sides decide with one
  predicate on data each side holds: the turn's start, read from its UUIDv7
  turn id (the transcript names it in the message ``idempotencyKey``
  ``codex-app-server:<thread>:<turn>:assistant``). A turn is never counted
  from both, and the skip never depends on whether a rollout file survives.
- Before the cutover nothing changes: those transcript rows are already in
  the ledger and the ledger is append-only. Their undercount is a historical
  gap, to be closed (or not) by a separate, keeper-approved backfill.
- The OpenClaw ``openai`` provider is the ``openai:default`` OAuth profile,
  the ChatGPT subscription, whatever its ``api`` label says; rollout rows are
  ``openai-codex``, subscription-included.
- The ``claude-cli`` runtime runs the Claude CLI, which writes its own
  transcript under ``~/.claude/projects``; the Claude Code lane counts it from
  there with its Anthropic ``msg_`` id. The one OpenClaw row so far
  (2026-10-06T07:41:58Z, 99,718 cache-write tokens) has its receipt in
  ``sol:claude-code`` (``66ec0d76-…:msg_011Cfkc8…``): same instant, same
  tokens. OpenClaw rows for it are skipped (``skipped["claude_cli_receipt"]``);
  that one row stays in the ledger, which is append-only.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
from typing import Any, Iterator

from .canonical_ledger import ValidationError, canonical_timestamp, decimal_string, normalize_fact

DEFAULT_OPENCLAW_HOME = "~/.openclaw"
OPENCLAW_USAGE_LANE = "openclaw"
OPENCLAW_SEARCH_LANE = "openclaw_search"
_STORE_GLOB = "agents/*/agent/openclaw-agent.sqlite"
_ROLLOUT_GLOB = "agents/*/agent/codex-home/sessions/**/rollout-*.jsonl"
_CODEX_RUNTIME_API = "openai-chatgpt-responses"
_CODEX_APP_SERVER = "codex-app-server"
# Codex turns that START at or after this instant are counted from their
# rollout; earlier turns keep their transcript row. Set past the deploy so no
# run on the old code can see a turn the new code also counts.
CODEX_ROLLOUT_CUTOVER = "2026-10-22T20:00:00Z"
# A rollout file last written before the cutover cannot hold a counted call.
_ROLLOUT_MTIME_SLACK = timedelta(minutes=10)
# OpenClaw provider -> the ledger's provider label, for routes that are the
# same subscription or API the ledger already names.
_PROVIDER_LABELS = {
    "zai": "zai",
    "kimi": "kimi-coding",
    "opencode-go": "opencode-go",
    "deepseek": "deepseek",
    "openrouter": "openrouter",
    "openai-api": "openai",
}
_SUBSCRIPTION_PROVIDERS = frozenset({"zai", "kimi-coding", "opencode-go", "openai-codex"})
_RELAY_PROVIDERS = frozenset({"meridian-primary", "meridian-yugen"})
_OWN_TRANSCRIPT_RUNTIMES = frozenset({"claude-cli"})
_NOT_MODEL_CALLS = frozenset({("openclaw", "delivery-mirror")})
_SURFACES = frozenset({"cron", "discord", "openresponses", "openresponses-user", "subagent", "heartbeat"})
_TOKEN_KEYS = ("input", "output", "cacheRead", "cacheWrite")
_SEARCH_TOOLS = frozenset({"web_search"})


def openclaw_stores(home: str | Path) -> list[tuple[str, Path]]:
    """``(agent, store path)`` for every agent store under ``home``."""
    root = Path(home).expanduser()
    if not root.is_dir():
        return []
    return [(path.parent.parent.name, path) for path in sorted(root.glob(_STORE_GLOB)) if path.is_file()]


def _ro(path: Path) -> sqlite3.Connection:
    """Read-only. The collectors run with a read-only home, where SQLite
    cannot create a missing -shm: while the gateway holds a store open its
    -wal/-shm exist and a plain read-only open works. With no -wal at all the
    store was closed cleanly and the file is complete, so it is read as
    immutable instead of failing the lane."""
    uri = path.resolve().as_uri()
    try:
        connection = sqlite3.connect(uri + "?mode=ro", uri=True)
        connection.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchall()
    except sqlite3.OperationalError:
        if Path(str(path) + "-wal").exists():
            raise
        connection = sqlite3.connect(uri + "?mode=ro&immutable=1", uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def _int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _occurred(event: dict[str, Any], created_ms: Any) -> str:
    stamp = event.get("timestamp")
    if stamp:
        return canonical_timestamp(stamp)
    return _created(created_ms)


def _created(created_ms: Any) -> str:
    return canonical_timestamp(datetime.fromtimestamp(int(created_ms) / 1000, timezone.utc).isoformat())


def _session_surfaces(connection: sqlite3.Connection) -> dict[str, tuple[str | None, bool]]:
    surfaces: dict[str, tuple[str | None, bool]] = {}
    try:
        rows = connection.execute("SELECT session_id, session_key FROM session_windows").fetchall()
    except sqlite3.DatabaseError:
        return surfaces
    for row in rows:
        parts = str(row["session_key"] or "").split(":")
        surface = parts[2] if len(parts) > 2 and parts[2] in _SURFACES else ("other" if len(parts) > 2 else None)
        surfaces[str(row["session_id"])] = (surface, "subagent" in parts[2:])
    return surfaces


def _events(connection: sqlite3.Connection) -> Iterator[tuple[str, dict[str, Any], Any]]:
    for row in connection.execute(
        "SELECT session_id, seq, event_json, created_at FROM transcript_events ORDER BY created_at, session_id, seq"
    ):
        try:
            event = json.loads(row["event_json"])
        except (TypeError, ValueError):
            continue
        if isinstance(event, dict) and event.get("type") == "message" and isinstance(event.get("message"), dict):
            yield str(row["session_id"]), event, row["created_at"]


def _cutover_ms(cutover: str) -> int:
    return int(datetime.fromisoformat(canonical_timestamp(cutover).replace("Z", "+00:00")).timestamp() * 1000)


def uuid7_ms(value: Any) -> int | None:
    """Unix milliseconds embedded in a UUIDv7, else ``None``."""
    digits = str(value or "").replace("-", "").lower()
    if len(digits) != 32 or digits[12] != "7":
        return None
    try:
        int(digits, 16)
    except ValueError:
        return None
    return int(digits[:12], 16)


def codex_app_server_turn(message: dict[str, Any]) -> tuple[str, str] | None:
    """``(thread_id, turn_id)`` of a Codex app-server mirror message."""
    parts = str(message.get("idempotencyKey") or "").split(":")
    if len(parts) >= 3 and parts[0] == _CODEX_APP_SERVER and parts[1] and parts[2]:
        return parts[1], parts[2]
    return None


def _rollout_counted(turn_id: str, cutover_ms: int) -> bool:
    started = uuid7_ms(turn_id)
    return started is not None and started >= cutover_ms


def collect_openclaw_usage(
    home: str | Path = DEFAULT_OPENCLAW_HOME,
    source_prefix: str = "sol:openclaw",
    *,
    codex_rollout_cutover: str = CODEX_ROLLOUT_CUTOVER,
) -> Iterator[dict[str, Any]]:
    """One ``usage_event_v1`` fact per OpenClaw model call (or Codex turn).

    Returns ``{"skipped": {...}, "stores": [...]}`` as the generator's
    ``StopIteration`` value for the lane report.
    """
    cutover_ms = _cutover_ms(codex_rollout_cutover)
    skipped = {"zero_usage": 0, "meridian_relay": 0, "not_model_call": 0, "codex_rollout": 0, "claude_cli_receipt": 0}
    stores: list[str] = []
    # (agent, Codex thread) -> (OpenClaw session, surface, subagent), for the
    # rollout rows' attribution.
    threads: dict[tuple[str, str], tuple[str, str | None, bool]] = {}
    for agent, path in openclaw_stores(home):
        stores.append(str(path))
        connection = _ro(path)
        try:
            surfaces = _session_surfaces(connection)
            for session_id, event, created_ms in _events(connection):
                message = event["message"]
                turn = codex_app_server_turn(message)
                if turn is not None:
                    threads.setdefault((agent, turn[0]), (session_id, *surfaces.get(session_id, (None, False))))
                if message.get("role") != "assistant" or not isinstance(message.get("usage"), dict):
                    continue
                usage = message["usage"]
                raw_provider = _text(message.get("provider"))
                model = _text(message.get("model"))
                if (raw_provider, model) in _NOT_MODEL_CALLS:
                    skipped["not_model_call"] += 1
                    continue
                if not any(_int(usage.get(key)) for key in _TOKEN_KEYS):
                    skipped["zero_usage"] += 1
                    continue
                if raw_provider in _RELAY_PROVIDERS:
                    skipped["meridian_relay"] += 1
                    continue
                if raw_provider in _OWN_TRANSCRIPT_RUNTIMES:
                    skipped["claude_cli_receipt"] += 1
                    continue
                if turn is not None and _rollout_counted(turn[1], cutover_ms):
                    skipped["codex_rollout"] += 1
                    continue
                api = _text(message.get("api"))
                codex_turn = raw_provider == "openai" and api == _CODEX_RUNTIME_API
                provider = "openai-codex" if codex_turn else _PROVIDER_LABELS.get(raw_provider or "", raw_provider)
                included = provider in _SUBSCRIPTION_PROVIDERS
                surface, subagent = surfaces.get(session_id, (None, False))
                occurred = _occurred(event, created_ms)
                recorded = _created(created_ms)
                row: dict[str, Any] = {
                    "fact_type": "usage_event_v1", "schema_version": 1,
                    "source_namespace": f"{source_prefix}:{agent}",
                    "source_event_id": f"{session_id}:{event.get('id')}",
                    "harness": "openclaw", "purpose": "subagent" if subagent else "main",
                    "record_kind": "historical_aggregate" if codex_turn else "api_attempt",
                    "occurred_at": occurred, "recorded_at": recorded,
                    "usage_source": "provider_reported", "usage_completeness": "complete",
                    "measurement_confidence": "exact",
                    "surface": surface, "session_id": session_id,
                    "provider": provider, "model_requested": model, "model_reported": model,
                    "api_mode": api, "billing_mode": "subscription_included" if included else None,
                    "request_status": "error" if message.get("stopReason") == "error" else "ok",
                    "input_tokens": _int(usage.get("input")), "output_tokens": _int(usage.get("output")),
                    "cache_read_tokens": _int(usage.get("cacheRead")), "cache_write_tokens": _int(usage.get("cacheWrite")),
                    "reasoning_tokens": _int(usage.get("reasoningTokens")),
                    "missing_fields": [], "attribution_gaps": [name for name, value in (("provider", provider), ("model_requested", model), ("model_reported", model)) if value is None],
                    "cost_status": "included" if included else "unknown",
                    "cost_source": "subscription" if included else None,
                    "estimated_cost_usd": None, "actual_cost_usd": None,
                    "x_openclaw_agent": agent,
                }
                if raw_provider != provider:
                    row["x_openclaw_provider"] = raw_provider
                response_id = _text(message.get("responseId"))
                if response_id is not None:
                    row["x_openclaw_response_id"] = response_id
                cost = usage.get("cost") if isinstance(usage.get("cost"), dict) else {}
                total = cost.get("total")
                if not included and total not in (None, 0, "0"):
                    row["x_openclaw_cost_usd"] = decimal_string(total, "cost")
                yield normalize_fact(row)
        finally:
            connection.close()
    codex = {"files_read": 0, "calls": 0, "before_cutover": 0, "duplicate_response": 0, "malformed": 0}
    yield from _codex_rollout_calls(home, source_prefix, cutover_ms, threads, codex)
    return {"skipped": skipped, "stores": stores, "codex_rollouts": codex}


def _codex_usage(usage: Any) -> dict[str, int | None] | None:
    """Codex usage -> ledger token fields. Codex's ``input_tokens`` includes
    the cached part; the ledger's input and cache-read fields are additive."""
    if not isinstance(usage, dict):
        return None
    total_in = _int(usage.get("input_tokens"))
    cached = _int(usage.get("cached_input_tokens")) or 0
    output = _int(usage.get("output_tokens"))
    if total_in is None or output is None or cached > total_in or min(total_in, output, cached) < 0:
        return None
    return {
        "input_tokens": total_in - cached, "cache_read_tokens": cached,
        "cache_write_tokens": _int(usage.get("cache_write_input_tokens")) or 0,
        "output_tokens": output, "reasoning_tokens": _int(usage.get("reasoning_output_tokens")),
    }


def _codex_rollout_calls(
    home: str | Path,
    source_prefix: str,
    cutover_ms: int,
    threads: dict[tuple[str, str], tuple[str, str | None, bool]],
    counts: dict[str, int],
) -> Iterator[dict[str, Any]]:
    """One ``api_attempt`` per Codex ``token_usage_record`` in a turn that
    started at or after the cutover."""
    root = Path(home).expanduser()
    if not root.is_dir():
        return
    oldest_mtime = cutover_ms / 1000 - _ROLLOUT_MTIME_SLACK.total_seconds()
    seen: set[str] = set()
    for path in sorted(root.glob(_ROLLOUT_GLOB)):
        try:
            if not path.is_file() or path.stat().st_mtime < oldest_mtime:
                continue
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        counts["files_read"] += 1
        agent = path.relative_to(root).parts[1]
        source: Any = None
        originator = None
        models: dict[str, str] = {}
        for line in lines:
            try:
                record = json.loads(line)
            except ValueError:
                counts["malformed"] += 1
                continue
            if not isinstance(record, dict) or not isinstance(record.get("payload"), dict):
                continue
            payload = record["payload"]
            kind = record.get("type")
            if kind == "session_meta":
                source, originator = payload.get("source"), _text(payload.get("originator"))
            elif kind == "turn_context" and _text(payload.get("turn_id")) and _text(payload.get("model")):
                models[str(payload["turn_id"])] = str(payload["model"])
            elif kind == "token_usage_record":
                thread_id, turn_id = _text(payload.get("thread_id")), _text(payload.get("turn_id"))
                response_id = _text(payload.get("response_id"))
                if not (thread_id and turn_id and response_id):
                    counts["malformed"] += 1
                    continue
                if not _rollout_counted(turn_id, cutover_ms):
                    counts["before_cutover"] += 1
                    continue
                if response_id in seen:
                    counts["duplicate_response"] += 1
                    continue
                tokens = _codex_usage(payload.get("usage"))
                stamp = record.get("timestamp")
                if tokens is None or not stamp:
                    counts["malformed"] += 1
                    continue
                seen.add(response_id)
                # A guardian (auto-review) thread has no OpenClaw session of
                # its own: its session stays unknown, the thread id is kept.
                session_id, surface, subagent = threads.get((agent, thread_id), (None, None, False))
                guardian = isinstance(source, dict) and "subagent" in source
                model = models.get(turn_id)
                row: dict[str, Any] = {
                    "fact_type": "usage_event_v1", "schema_version": 1,
                    "source_namespace": f"{source_prefix}:{agent}:codex-rollout",
                    "source_event_id": f"{thread_id}:{response_id}",
                    "harness": "openclaw", "purpose": "subagent" if guardian or subagent else "main",
                    "record_kind": "api_attempt",
                    "occurred_at": canonical_timestamp(stamp), "recorded_at": canonical_timestamp(stamp),
                    "usage_source": "provider_reported", "usage_completeness": "complete",
                    "measurement_confidence": "exact",
                    "surface": surface, "session_id": session_id,
                    # Codex's OpenAI response id: the physical request.
                    "provider_request_id": response_id,
                    "provider": "openai-codex", "model_requested": model, "model_reported": model,
                    "billing_mode": "subscription_included", "request_status": "ok",
                    **tokens,
                    "missing_fields": [],
                    "attribution_gaps": [] if model else ["model_requested", "model_reported"],
                    "cost_status": "included", "cost_source": "subscription",
                    "estimated_cost_usd": None, "actual_cost_usd": None,
                    "x_openclaw_agent": agent, "x_codex_thread_id": thread_id, "x_codex_turn_id": turn_id,
                }
                if originator:
                    row["x_codex_originator"] = originator
                if guardian:
                    row["x_codex_source"] = json.dumps(source, sort_keys=True)
                try:
                    fact = normalize_fact(row)
                except ValidationError:
                    counts["malformed"] += 1
                    continue
                counts["calls"] += 1
                yield fact


def collect_openclaw_searches(
    home: str | Path = DEFAULT_OPENCLAW_HOME,
    source_prefix: str = "sol:openclaw-search",
) -> Iterator[dict[str, Any]]:
    """One ``search_event_v1`` fact per OpenClaw ``web_search`` tool result."""
    for agent, path in openclaw_stores(home):
        connection = _ro(path)
        try:
            for session_id, event, created_ms in _events(connection):
                message = event["message"]
                if message.get("role") != "toolResult" or message.get("toolName") not in _SEARCH_TOOLS:
                    continue
                details = message.get("details") if isinstance(message.get("details"), dict) else {}
                query = details.get("query")
                row: dict[str, Any] = {
                    "fact_type": "search_event_v1", "schema_version": 1,
                    "source_namespace": f"{source_prefix}:{agent}",
                    "source_search_event_id": f"{session_id}:{event.get('id')}",
                    "harness": "openclaw",
                    "observed_at": _occurred(event, created_ms),
                    "provider": _text(details.get("provider")) or "unknown",
                    "purpose": str(message.get("toolName")),
                    "status": "error" if message.get("isError") else "ok",
                    "session_id": session_id,
                    "query_sha256": hashlib.sha256(str(query).encode("utf-8")).hexdigest() if query else None,
                    "result_count": _int(details.get("count")),
                    "latency_ms": _int(details.get("tookMs")),
                }
                yield normalize_fact(row)
        finally:
            connection.close()
