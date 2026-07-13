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


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _base_event(*, namespace: str, event_id: str, harness: str, occurred: Any, recorded: Any, purpose: str="main") -> dict[str,Any]:
    return {
      "fact_type":"usage_event_v1","schema_version":1,"source_namespace":namespace,"source_event_id":event_id,
      "harness":harness,"purpose":purpose,"record_kind":"api_attempt","occurred_at":canonical_timestamp(occurred),
      "recorded_at":canonical_timestamp(recorded),"usage_source":"provider_reported","usage_completeness":"complete",
      "measurement_confidence":"exact","cost_status":"unknown",
    }


def collect_hermes_usage(db_path: str | Path, source_namespace: str, *, batch_size: int=500) -> Iterator[dict[str,Any]]:
    """Stream all Hermes event rows from SQLite opened with mode=ro."""
    if not Path(db_path).expanduser().exists(): return
    con=_ro(db_path)
    try:
        cursor=con.execute("SELECT rowid AS _immutable_rowid, * FROM llm_usage_events ORDER BY rowid")
        while True:
            batch=cursor.fetchmany(max(1,batch_size))
            if not batch: break
            for raw in batch:
                r=dict(raw); historical=r.get("record_kind")=="historical_aggregate"
                status=r.get("cost_status") or "unknown"
                estimated=_cost(r.get("estimated_cost_usd")); actual=_cost(r.get("actual_cost_usd"))
                if status=="included" or r.get("billing_mode") in {"subscription_included","subscription"}:
                    status="included"; estimated=actual=None
                elif status=="reconstructed": status="estimated" if estimated is not None else ("actual" if actual is not None else "unknown")
                event_id=str(r.get("event_uid") or f"sqlite-row:{r['_immutable_rowid']}")
                occurred=_timestamp(r.get("timestamp"),r.get("created_at"))
                recorded=_timestamp(r.get("created_at"),r.get("timestamp"))
                row=_base_event(namespace=source_namespace,event_id=event_id,harness="hermes",occurred=occurred,recorded=recorded,purpose=r.get("purpose") or ("historical_backfill" if historical else "main"))
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


def collect_claude_usage(root: str | Path, source_namespace: str) -> Iterator[dict[str,Any]]:
    """Read provider assistant receipts only; transcript content is never projected."""
    root_path=Path(root).expanduser()
    if not root_path.exists(): return
    for path in sorted(root_path.rglob("*.jsonl")):
        final: dict[tuple[str,str],tuple[int,dict[str,Any]]]={}
        for sequence,item in enumerate(_read_claude_file(path)):
            message=item.get("message")
            if item.get("type")!="assistant" or not isinstance(message,dict): continue
            message_id=message.get("id"); session_id=item.get("sessionId") or item.get("session_id")
            model=message.get("model") or item.get("model")
            if not message_id or not session_id or model=="<synthetic>": continue
            # Later streaming snapshots replace earlier snapshots for this provider message.
            final[(str(session_id),str(message_id))]=(sequence,item)
        for (_,message_id),(sequence,item) in sorted(final.items(),key=lambda pair:pair[1][0]):
            message=item["message"]; usage=message.get("usage") or {}
            output_details = usage.get("output_tokens_details") or {}
            reasoning_tokens = _usage_value(usage, "thinking_tokens", "reasoning_tokens")
            if reasoning_tokens is None and isinstance(output_details, dict):
                reasoning_tokens = _usage_value(output_details, "thinking_tokens", "reasoning_tokens")
            occurred=item.get("timestamp") or message.get("timestamp") or datetime.fromtimestamp(path.stat().st_mtime,timezone.utc).isoformat()
            is_subagent=bool(item.get("agentId") or item.get("isSidechain") or "/subagents/" in path.as_posix() or item.get("path"))
            row=_base_event(namespace=source_namespace,event_id=f"{item.get('sessionId') or item.get('session_id')}:{message_id}",harness="claude_code",occurred=occurred,recorded=occurred,purpose="subagent" if is_subagent else "main")
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


def collect_opencode_usage(db_paths: Iterable[str | Path], source_namespace: str) -> Iterator[dict[str,Any]]:
    for db_path in db_paths:
        path=Path(db_path).expanduser()
        if not path.exists(): continue
        con=_ro(path)
        try:
            namespace=f"{source_namespace}:{path.stem}"
            for message_id,r,session in _message_candidates(con):
                data=r["_data"]; tokens=data.get("tokens") or {}; cache=tokens.get("cache") or {}
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
