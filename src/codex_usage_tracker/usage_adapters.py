"""Read-only adapters for Hermes, Claude Code, and OpenCode usage facts."""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
import json
from pathlib import Path
import sqlite3
from typing import Any, Iterator, Iterable

from .canonical_ledger import MalformedLedgerError, canonical_timestamp, decimal_string, normalize_fact


def _ro(path: str | Path) -> sqlite3.Connection:
    uri=Path(path).expanduser().resolve().as_uri()+"?mode=ro"
    connection=sqlite3.connect(uri,uri=True)
    connection.row_factory=sqlite3.Row
    return connection


def _timestamp(value: Any, fallback: Any) -> str:
    value = fallback if value in (None, "") else value
    if isinstance(value,(int,float)) and value > 10_000_000_000: value=float(value)/1000
    return canonical_timestamp(value)


def _cost(value: Any) -> str | None:
    return decimal_string(value,"cost") if value is not None else None


def _epoch_seconds(value: Any) -> float | None:
    """Return epoch seconds for a numeric (s or ms) or ISO timestamp, else None."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        numeric = float(value)
        return numeric / 1000 if numeric > 10_000_000_000 else numeric
    text = str(value).strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


_HERMES_PURPOSE_MAP = {
    "aux:vision": "vision",
    "aux:compression": "compression",
    "aux:approval": "x_aux_approval",
    "aux:title_generation": "x_aux_title_generation",
}


def _hermes_purpose(value: Any, *, historical: bool) -> str:
    """Translate the sidecar purpose vocabulary into the canonical contract set.

    The Hermes sidecar records auxiliary lanes as ``aux:*``; the canonical
    ledger contract only allows the fixed purpose set plus ``x_`` extension
    names. Exact semantic matches map to their canonical bucket, everything
    else becomes a named ``x_aux_*`` extension so no lane is silently
    relabeled as something it is not.
    """
    purpose = _optional_text(value) or ("historical_backfill" if historical else "main")
    return _HERMES_PURPOSE_MAP.get(purpose, purpose)


def _base_event(*, namespace: str, event_id: str, harness: str, occurred: Any, recorded: Any, purpose: str="main") -> dict[str,Any]:
    return {
      "fact_type":"usage_event_v1","schema_version":1,"source_namespace":namespace,"source_event_id":event_id,
      "harness":harness,"purpose":purpose,"record_kind":"api_attempt","occurred_at":canonical_timestamp(occurred),
      "recorded_at":canonical_timestamp(recorded),"usage_source":"provider_reported","usage_completeness":"complete",
      "measurement_confidence":"exact","cost_status":"unknown",
    }


def collect_hermes_usage(db_path: str | Path, source_namespace: str, *, batch_size: int=500) -> Iterator[dict[str,Any]]:
    """Stream all Hermes event rows from SQLite opened with mode=ro.

    Hermes moved usage events out of state.db into the sidecar ledger
    (usage_events.db, table usage_events, keeper ruling 2026-08-24) and the
    old state.db table was dropped when that Hermes generation retired.
    Both layouts are supported: the sidecar is detected by its table, and a
    database that exposes neither table yields zero rows (a missing source is
    an empty lane, not a run-killing error under strict-sources).
    """
    expanded=Path(db_path).expanduser()
    if not expanded.exists(): return
    con=_ro(db_path)
    try:
        tables={row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        if "usage_events" in tables:
            cursor=con.execute("SELECT rowid AS _immutable_rowid, * FROM usage_events ORDER BY rowid")
        elif "llm_usage_events" in tables:
            cursor=con.execute("SELECT rowid AS _immutable_rowid, * FROM llm_usage_events ORDER BY rowid")
        else:
            return
        while True:
            batch=cursor.fetchmany(max(1,batch_size))
            if not batch: break
            for raw in batch:
                r=dict(raw); historical=r.get("record_kind")=="historical_aggregate"
                status=r.get("cost_status") or "unknown"
                if "cost_usd_micro" in r:
                    micro=r.get("cost_usd_micro")
                    cost_text=None if micro is None else f"{int(micro)/1_000_000:.6f}"
                    if status=="included":
                        estimated=actual=None
                    elif cost_text is not None:
                        estimated=cost_text; actual=None
                    else:
                        estimated=actual=None
                else:
                    estimated=_cost(r.get("estimated_cost_usd")); actual=_cost(r.get("actual_cost_usd"))
                    if status=="reconstructed": status="estimated" if estimated is not None else ("actual" if actual is not None else "unknown")
                if status=="included" or r.get("billing_mode") in {"subscription_included","subscription"}:
                    status="included"; estimated=actual=None
                event_id=str(r.get("event_uid") or f"sqlite-row:{r['_immutable_rowid']}")
                occurred=_timestamp(r.get("timestamp"),r.get("created_at"))
                recorded=_timestamp(r.get("created_at"),r.get("timestamp"))
                row=_base_event(namespace=source_namespace,event_id=event_id,harness="hermes",occurred=occurred,recorded=recorded,purpose=_hermes_purpose(r.get("purpose"), historical=historical))
                row.update({
                  "event_uid":_optional_text(r.get("event_uid")),"surface":_optional_text(r.get("source")),"session_id":_optional_text(r.get("session_id")),
                  "provider":_optional_text(r.get("provider")),"model_requested":_optional_text(r.get("model")),"model_reported":None,
                  "api_mode":_optional_text(r.get("api_mode")),"billing_mode":_optional_text(r.get("billing_mode")),
                  "record_kind":r.get("record_kind") or "api_attempt","request_status":None if historical else _optional_text(r.get("request_status")),
                  "error_class":_optional_text(r.get("error_class")),"latency_ms":None if historical else r.get("latency_ms"),
                  "input_tokens":r.get("input_tokens"),"cache_read_tokens":r.get("cache_read_tokens"),
                  "cache_write_tokens":r.get("cache_write_tokens"),"output_tokens":r.get("output_tokens"),
                  "reasoning_tokens":r.get("reasoning_tokens"),"usage_source":r.get("usage_source") or ("reconstructed" if historical else "provider_reported"),
                  "usage_completeness":"unknown","measurement_confidence":r.get("measurement_confidence") or "unknown",
                  "missing_fields":[],"attribution_gaps":[name for name,value in (("provider",r.get("provider")),("model_requested",r.get("model")),("model_reported",None)) if value is None],
                  "estimated_cost_usd":estimated,"actual_cost_usd":actual,"cost_status":status,"cost_source":_optional_text(r.get("cost_source")),
                  "pricing_version":_optional_text(r.get("pricing_version")),"reconstructed_call_count":r.get("api_call_index") if historical else None,
                })
                yield normalize_fact(row)
    finally: con.close()


def _read_claude_file(path: Path) -> list[dict[str,Any]]:
    lines=path.read_text(encoding="utf-8").splitlines(); parsed=[]
    last_nonempty=max((i for i,line in enumerate(lines) if line.strip()),default=-1)
    for index,line in enumerate(lines):
        if not line.strip(): continue
        try: value=json.loads(line)
        except json.JSONDecodeError as exc:
            if index==last_nonempty: continue
            raise MalformedLedgerError(f"{path}:{index+1}: malformed interior JSON: {exc.msg}") from exc
        if isinstance(value,dict): parsed.append(value)
    return parsed


def _usage_value(usage: dict[str,Any], *names: str) -> int | None:
    for name in names:
        value=usage.get(name)
        if value is not None: return int(value)
    return None


def collect_claude_usage(
    root: str | Path,
    source_namespace: str,
    *,
    settle_seconds: float = 300.0,
) -> Iterator[dict[str,Any]]:
    """Read stable provider receipts; transcript content is never projected.

    Claude Code can append several assistant receipts for one provider message.
    The latest stable snapshot supplies both receipt time and usage. A lone
    non-terminal receipt in a recently modified file is provisional and is
    deferred to a later collection pass.
    """
    root_path=Path(root).expanduser()
    if not root_path.exists(): return
    now_timestamp = datetime.now(timezone.utc).timestamp()
    for path in sorted(root_path.rglob("*.jsonl")):
        try:
            quiescent = now_timestamp - path.stat().st_mtime >= settle_seconds
        except OSError:
            continue
        grouped: dict[tuple[str,str],list[tuple[int,dict[str,Any]]]]={}
        for sequence,item in enumerate(_read_claude_file(path)):
            message=item.get("message")
            if item.get("type")!="assistant" or not isinstance(message,dict): continue
            message_id=message.get("id"); session_id=item.get("sessionId") or item.get("session_id")
            model=message.get("model") or item.get("model")
            if not message_id or not session_id or model=="<synthetic>": continue
            grouped.setdefault((str(session_id),str(message_id)),[]).append((sequence,item))
        selected: list[tuple[int,dict[str,Any]]] = []
        for entries in grouped.values():
            terminal = [entry for entry in entries if (entry[1].get("message") or {}).get("stop_reason")]
            if terminal:
                chosen = terminal[-1]
            else:
                usage_snapshots = {
                    json.dumps((entry[1].get("message") or {}).get("usage") or {}, sort_keys=True, separators=(",", ":"))
                    for entry in entries
                }
                if len(entries) == 1 and not quiescent:
                    continue
                if len(usage_snapshots) > 1 and not quiescent:
                    continue
                chosen = entries[-1]
            selected.append(chosen)
        for sequence,item in sorted(selected,key=lambda entry:entry[0]):
            message=item["message"]; usage=message.get("usage") or {}
            output_details = usage.get("output_tokens_details") or {}
            reasoning_tokens = _usage_value(usage, "thinking_tokens", "reasoning_tokens")
            if reasoning_tokens is None and isinstance(output_details, dict):
                reasoning_tokens = _usage_value(output_details, "thinking_tokens", "reasoning_tokens")
            occurred=item.get("timestamp") or message.get("timestamp") or datetime.fromtimestamp(path.stat().st_mtime,timezone.utc).isoformat()
            is_subagent=bool(item.get("agentId") or item.get("isSidechain") or "/subagents/" in path.as_posix() or item.get("path"))
            row=_base_event(namespace=source_namespace,event_id=f"{item.get('sessionId') or item.get('session_id')}:{message['id']}",harness="claude_code",occurred=occurred,recorded=occurred,purpose="subagent" if is_subagent else "main")
            row.update({
              "session_id":str(item.get("sessionId") or item.get("session_id")),"provider":"anthropic",
              "model_requested":message.get("model") or item.get("model"),"model_reported":message.get("model") or item.get("model"),
              "api_mode":"anthropic_messages","billing_mode":"subscription_included","request_status":"ok" if message.get("stop_reason") else "unknown",
              "input_tokens":_usage_value(usage,"input_tokens"),"cache_write_tokens":_usage_value(usage,"cache_creation_input_tokens","cache_write_input_tokens"),
              "cache_read_tokens":_usage_value(usage,"cache_read_input_tokens","cache_read_tokens"),"output_tokens":_usage_value(usage,"output_tokens"),
              "reasoning_tokens":reasoning_tokens,"missing_fields":[],"attribution_gaps":[],
              "cost_status":"included","cost_source":"subscription","estimated_cost_usd":None,"actual_cost_usd":None,
            })
            yield normalize_fact(row)


def _tables(con: sqlite3.Connection) -> set[str]:
    return {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _message_candidates(con: sqlite3.Connection) -> Iterable[tuple[str,dict[str,Any],dict[str,Any]]]:
    tables=_tables(con); sessions={str(r["id"]):dict(r) for r in con.execute("SELECT * FROM session")}
    current_by_session: dict[str,list[tuple[str,dict[str,Any]]]]={}
    if "message" in tables:
        for raw in con.execute("SELECT * FROM message ORDER BY time_created, id"):
            r=dict(raw)
            try: data=json.loads(r.get("data") or "{}")
            except json.JSONDecodeError: continue
            if data.get("role")=="assistant": current_by_session.setdefault(str(r["session_id"]),[]).append((str(r["id"]),{**r,"_data":data}))
    for session_id,items in current_by_session.items():
        for message_id,r in items: yield message_id,r,sessions.get(session_id,{})
    if "session_message" in tables:
        for raw in con.execute("SELECT * FROM session_message ORDER BY time_created, id"):
            r=dict(raw); session_id=str(r["session_id"])
            if session_id in current_by_session: continue
            try: data=json.loads(r.get("data") or "{}")
            except json.JSONDecodeError: continue
            role=data.get("role") or r.get("type")
            if role=="assistant": yield str(r["id"]),{**r,"_data":data},sessions.get(session_id,{})


def collect_opencode_usage(
    db_paths: Iterable[str | Path],
    source_namespace: str,
    *,
    settle_seconds: float = 300.0,
) -> Iterator[dict[str,Any]]:
    """Project settled assistant messages only; in-flight rows are deferred.

    OpenCode writes an assistant message row as soon as generation starts and
    keeps rewriting it: tokens stay zero, ``time.completed`` is unset, and a
    terminal error may only appear tens of seconds later. Projecting that
    snapshot mints a canonical fact that the next collection pass contradicts,
    which quarantines the identity forever. A row without a completion marker
    is therefore provisional and is deferred to a later pass until its source
    row has been quiet for ``settle_seconds`` (the same escape hatch the Claude
    adapter uses, so a row that never completes is still eventually recorded).
    """
    now_timestamp = datetime.now(timezone.utc).timestamp()
    for db_path in db_paths:
        path=Path(db_path).expanduser()
        if not path.exists(): continue
        con=_ro(path)
        try:
            namespace=f"{source_namespace}:{path.stem}"
            for message_id,r,session in _message_candidates(con):
                data=r["_data"]; tokens=data.get("tokens") or {}; cache=tokens.get("cache") or {}
                timing=data.get("time") if isinstance(data.get("time"),dict) else {}
                if not timing.get("completed"):
                    touched=_epoch_seconds(r.get("time_updated"))
                    if touched is None: touched=_epoch_seconds(r.get("time_created"))
                    if touched is not None and now_timestamp-touched < settle_seconds:
                        continue
                reasoning=_usage_value(tokens,"reasoning"); output=_usage_value(tokens,"output")
                canonical_output=None if output is None and reasoning is None else (output or 0)+(reasoning or 0)
                model_data = data.get("model") if isinstance(data.get("model"), dict) else {}
                provider=data.get("providerID") or model_data.get("providerID")
                model=data.get("modelID") or model_data.get("modelID") or model_data.get("id")
                occurred=_timestamp((data.get("time") or {}).get("completed") or r.get("time_updated") or r.get("time_created"),datetime.now(timezone.utc).isoformat())
                is_subagent=bool(session.get("parent_id") or (session.get("agent") and session.get("agent") not in {"build","plan","main"}))
                row=_base_event(namespace=namespace,event_id=message_id,harness="opencode",occurred=occurred,recorded=occurred,purpose="subagent" if is_subagent else "main")
                cost=_cost(data.get("cost")); go=provider=="opencode-go"
                row.update({
                  "session_id":r.get("session_id"),"provider":provider,"model_requested":model,"model_reported":model,
                  "request_status":"ok" if not data.get("error") else "error","input_tokens":_usage_value(tokens,"input"),
                  "cache_read_tokens":_usage_value(cache,"read"),"cache_write_tokens":_usage_value(cache,"write"),
                  "output_tokens":canonical_output,"reasoning_tokens":reasoning,"missing_fields":[],"attribution_gaps":[],
                  "estimated_cost_usd":None if go else cost,"actual_cost_usd":None,"cost_status":"included" if go else ("estimated" if cost is not None else "unknown"),
                  "cost_source":"opencode-go-quota" if go else ("opencode-local-calculation" if cost is not None else None),
                })
                if go and cost is not None: row["x_opencode_quota_cost_usd"]=cost
                yield normalize_fact(row)
        finally: con.close()

# Friendly adapter aliases.
iter_hermes_usage = collect_hermes_usage
iter_claude_usage = collect_claude_usage
iter_opencode_usage = collect_opencode_usage
